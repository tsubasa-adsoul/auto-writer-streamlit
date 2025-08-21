# streamlit_app.py
# ------------------------------------------------------------
# Auto Writer for WordPress
# - 必須: キーワード -> ①読者像 ②ニーズ ③構成 ④本文 をAIが自動生成
# - 追記/禁止事項をプロンプトに注入
# - ?rest_route= を優先してWPへ draft 投稿（403回避）
# - ローカル運用（外部DBなし） / アイキャッチなし
# ------------------------------------------------------------
import json
import re
from datetime import datetime, timezone, time as dt_time
from typing import Dict, Any, List, Optional

import requests
from requests.auth import HTTPBasicAuth
import streamlit as st

# ==============================
# 基本設定
# ==============================
st.set_page_config(page_title="WP Auto Writer", page_icon="📝", layout="wide")
st.title("📝 WordPress Auto Writer（キーワード→①〜④ 自動生成）")

# ------------------------------
# Secrets 読み込み
# ------------------------------
if "wp_configs" not in st.secrets:
    st.error("Secrets に [wp_configs] がありません。App settings → Secrets で登録してください。")
    st.stop()

WP_CONFIGS: Dict[str, Dict[str, str]] = st.secrets["wp_configs"]  # 複数サイト対応
GEMINI_KEY = st.secrets.get("google", {}).get("gemini_api_key_1", None)
if not GEMINI_KEY:
    st.warning("Gemini APIキー（google.gemini_api_key_1）が未設定です。生成系は動作しません。")

# ------------------------------
# WP エンドポイント補助
# ------------------------------
def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"

def api_candidates(base: str, route: str) -> List[str]:
    base = ensure_trailing_slash(base); route = route.lstrip("/")
    # ?rest_route= を優先（Xserver対策）
    return [f"{base}?rest_route=/{route}", f"{base}wp-json/{route}"]

def wp_get(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str]) -> requests.Response:
    for url in api_candidates(base, route):
        r = requests.get(url, auth=auth, headers=headers, timeout=20)
        if r.status_code == 200:
            return r
    return r

def wp_post(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str],
            json_payload: Dict[str, Any]) -> requests.Response:
    for url in api_candidates(base, route):
        r = requests.post(url, auth=auth, headers=headers, json=json_payload, timeout=30)
        if r.status_code in (200, 201):
            return r
    return r

HEADERS = {
    "User-Agent": "Mozilla/5.0 (AutoWriter/Streamlit)",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ------------------------------
# 生成ユーティリティ（ベース方針を継承）
# ------------------------------
ALLOWED_TAGS = ['h2','h3','p','br','strong','em','ul','ol','li','table','tr','th','td']  # :contentReference[oaicite:1]{index=1}

def simplify_html(html: str) -> str:
    for tag in re.findall(r'</?(\w+)[^>]*>', html):
        if tag.lower() not in ALLOWED_TAGS:
            html = re.sub(rf'</?{tag}[^>]*>', '', html, flags=re.IGNORECASE)
    return html

MAX_H2 = 8
def limit_h2_count(html: str, max_count: int = MAX_H2) -> str:
    h2s = re.findall(r'(<h2>.*?</h2>)', html, flags=re.DOTALL | re.IGNORECASE)
    if len(h2s) <= max_count:
        return html
    # 先頭max個だけ残す（ベース実装と同趣旨） :contentReference[oaicite:2]{index=2} :contentReference[oaicite:3]{index=3}
    return "".join(h2s[:max_count]) + "\n"

def generate_permalink(keyword: str) -> str:
    """
    かな→ローマ字/英数に落としてスラッグ生成（50文字程度で短縮）
    ベースの考え方を踏襲（一般語置換→かなローマ字→連続ハイフン整理） :contentReference[oaicite:4]{index=4}
    """
    import unicodedata
    base = keyword.lower()
    subs = {
        '先払い買取':'sakibarai-kaitori','先払い':'sakibarai','買取':'kaitori','口コミ':'kuchikomi',
        '評判':'hyoban','体験談':'taiken','レビュー':'review','比較':'hikaku','査定':'satei',
        'おすすめ':'osusume','ランキング':'ranking','評価':'hyoka','申込':'moushikomi','方法':'houhou',
        '流れ':'nagare','手順':'tejun'
    }
    for jp,en in subs.items(): base = base.replace(jp,en)
    base = unicodedata.normalize('NFKD', base)
    # 非英数はハイフン化
    base = re.sub(r'[^a-z0-9]+', '-', base).strip('-')
    base = re.sub(r'-{2,}', '-', base)
    if len(base) > 50:
        parts = base.split('-'); base = '-'.join(parts[:5])
    return base or f"post-{int(datetime.now().timestamp())}"

# ------------------------------
# Gemini 呼び出し（ベース準拠の payload） :contentReference[oaicite:5]{index=5}
# ------------------------------
def call_gemini(prompt: str, temperature: float = 0.1) -> str:
    if not GEMINI_KEY:
        raise RuntimeError("Gemini APIキーが未設定です。Secrets に google.gemini_api_key_1 を追加してください。")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent?key={GEMINI_KEY}"
    payload = {"contents":[{"parts":[{"text": prompt}]}], "generationConfig": {"temperature": temperature}}
    r = requests.post(endpoint, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini エラー: {r.status_code} / {r.text[:400]}")
    j = r.json()
    return j["candidates"][0]["content"]["parts"][0]["text"]

# ------------------------------
# プロンプト（①〜④をキーワードから生成）
# ------------------------------
def prompt_outline(keyword: str, extra: str, banned: List[str], max_h2: int) -> str:
    banned_block = "\n".join([f"・{b}" for b in banned]) if banned else "・（特になし）"
    return f"""
# 役割
あなたは日本語SEOに強いWeb編集者です。キーワードから読者像→ニーズ→構成（H2/H3）→本文方針の“設計”を行います。

# 入力
- キーワード: {keyword}
- 追加してほしい要素/論点: {extra or "（指定なし）"}
- 禁止事項（絶対に含めない）:
{banned_block}

# 制約
- 最初に「①読者像」「②ニーズ」を150字程度で箇条書き
- 次に「③構成（HTML）」として <h2>,<h3> で見出しだけを列挙（<h1>禁止）
- H2は最大{max_h2}個まで（それを超える案は却下）
- H2直下の導入文では「この記事では〜」の定型を使わない方針で（後工程の執筆時に反映）
- 最後に「④本文ポリシー」を箇条書きで（文体・禁止語・表の扱いなど）

# 出力フォーマット（厳守）
① 読者像:
- ...

② ニーズ:
- ...

③ 構成（HTML）:
<h2>…</h2>
<h3>…</h3>

④ 本文ポリシー:
- ...
""".strip()

def prompt_lead(keyword: str, content_direction: str, structure_html: str) -> str:
    # 要望1のテンプレを踏襲：必ず <h2>はじめに</h2> → <p>… で出力 :contentReference[oaicite:6]{index=6}
    return f"""
# 命令書:
あなたはSEOに特化したプロライターです。以下の構成案をもとに「{keyword}」のリード文を作成してください。

# 出力形式（厳守）:
・必ず最初に<h2>はじめに</h2>を出力すること
・その直後に<p>で本文を出力すること
・一文ごとに独立した<p>タグで記述すること

# リードの作成指示:
・読者の悩みや不安に共感する
・本文で得られる具体的なメリットを2つ以上
・興味喚起の表現を適度に使用
・最後に行動喚起の一文

# 記事の方向性:
{content_direction}

# 構成案:
{structure_html}

# 出力:
""".strip()

def prompt_body(keyword: str, structure_html: str, policy_bullets: str, banned: List[str]) -> str:
    banned_block = "\n".join([f"・{b}" for b in banned]) if banned else "（なし）"
    return f"""
# あなたの役割
構成（<h2>,<h3>）に沿って、本文HTMLのみを書きます。<h1>禁止。

# 厳守ルール
- H2直下の導入文で「この記事では〜」等の定型句を使わない
- 許可タグ: {', '.join(ALLOWED_TAGS)}（それ以外は出力しない）
- 具体的な事実は曖昧に書かない。不明は「不明」「公式未記載」と明記
- 禁止事項（絶対に含めない）:
{banned_block}

# 文体・方針（ポリシー）
{policy_bullets}

# 入力
- キーワード: {keyword}
- 構成（HTML）:
{structure_html}

# 出力（本文HTMLのみ）:
""".strip()

def prompt_summary(keyword: str, content_dir: str, article_html: str) -> str:
    # 要望1の方針：まとめは<h2>まとめ</h2>＋箇条書き含む、広告系文言は禁止 :contentReference[oaicite:7]{index=7}
    return f"""
# 命令書:
あなたはSEOに特化したプロライターです。「{keyword}」の記事のまとめをHTMLで作成。

# 出力形式（厳守）:
・最初に<h2>まとめ</h2>
・一文ごとに<p>タグ。<br>は禁止
・要点の箇条書きを2-3個、適宜文中に

# 禁止事項:
・広告/PR/アフィリエイト関連の文言は一切禁止

# 記事の方向性:
{content_dir}

# 参考（本文）:
{article_html}

# 出力:
""".strip()

def generate_seo_title(keyword: str, content_dir: str) -> str:
    p = f"""
# 役割: SEO編集者
# 指示: 32文字以内、日本語、記号【】｜禁止。自然にキーワードを含め、思わずクリックしたくなる1本だけ。
# 入力: キーワード={keyword} / 記事の方向性={content_dir}
# 出力: タイトルのみ
"""
    title = call_gemini(p).strip()
    title = re.sub(r'[【】｜\n\r]', '', title)
    return title[:32]

def generate_seo_description(keyword: str, content_dir: str, title: str) -> str:
    p = f"""
# 役割: SEO編集者
# 指示: 120字以内。定型「〜を解説/紹介」禁止。数字や具体メリットを入れてCTRを高める。
# 入力: キーワード={keyword} / タイトル={title} / 方向性={content_dir}
# 出力: 説明文のみ
"""
    desc = call_gemini(p).strip()
    return re.sub(r'[\n\r]', '', desc)[:120]

# ==============================
# サイドバー：サイト選択 & 疎通
# ==============================
st.sidebar.header("接続先（WP）")
site_key = st.sidebar.selectbox("投稿先サイト", sorted(WP_CONFIGS.keys()))
cfg = WP_CONFIGS[site_key]
BASE = ensure_trailing_slash(cfg["url"])
AUTH = HTTPBasicAuth(cfg["user"], cfg["password"])

if st.sidebar.button("🔐 認証 /users/me"):
    r = wp_get(BASE, "wp/v2/users/me", AUTH, HEADERS)
    st.sidebar.code(f"GET users/me → {r.status_code}")
    st.sidebar.caption(r.text[:300])

# ==============================
# 入力（左） / 生成とプレビュー（中） / 投稿（右）
# ==============================
colL, colM, colR = st.columns([1.2, 1.6, 1.0])

with colL:
    st.header("1) 入力")
    keyword = st.text_input("必須キーワード（記事の主軸）", placeholder="例：先払い買取 口コミ")
    extra_points = st.text_area("特に加えてほしい内容（任意）", height=100)
    banned_text = st.text_area("禁止事項（1行=1項目 / 厳守）", height=120)
    banned_list = [l.strip() for l in banned_text.splitlines() if l.strip()]
    max_h2 = st.number_input("H2の最大数", min_value=3, max_value=12, value=MAX_H2, step=1)

    gen_outline_btn = st.button("①〜④（読者像/ニーズ/構成/本文方針）を生成")

with colM:
    st.header("2) 生成 & プレビュー")
    if gen_outline_btn:
        if not keyword.strip():
            st.error("キーワードは必須です。")
            st.stop()
        outline_raw = call_gemini(prompt_outline(keyword, extra_points, banned_list, max_h2))
        # パート抽出（シンプルな区切り）
        part1 = re.search(r'①.*?③', outline_raw, flags=re.DOTALL)
        part2 = re.search(r'③.*?④', outline_raw, flags=re.DOTALL)
        part3 = re.search(r'④.*', outline_raw, flags=re.DOTALL)

        readers = re.search(r'①[^\n]*\n(.+?)\n\n②', outline_raw, flags=re.DOTALL)
        needs = re.search(r'②[^\n]*\n(.+?)\n\n③', outline_raw, flags=re.DOTALL)
        structure = re.search(r'③[^\n]*\n(.+?)\n\n④', outline_raw, flags=re.DOTALL)
        policy = re.search(r'④[^\n]*\n(.+)$', outline_raw, flags=re.DOTALL)

        st.session_state["readers"] = readers.group(1).strip() if readers else ""
        st.session_state["needs"] = needs.group(1).strip() if needs else ""
        structure_html = (structure.group(1).strip() if structure else "").replace("\r","")
        structure_html = simplify_html(structure_html)
        structure_html = limit_h2_count(structure_html, max_h2)
        st.session_state["structure_html"] = structure_html
        st.session_state["policy"] = (policy.group(1).strip() if policy else "")

    # 手直し用エディタ
    readers_txt = st.text_area("① 読者像（編集可）", value=st.session_state.get("readers",""), height=120)
    needs_txt = st.text_area("② ニーズ（編集可）", value=st.session_state.get("needs",""), height=120)
    structure_html = st.text_area("③ 構成（HTML / 編集可）", value=st.session_state.get("structure_html",""), height=160)
    policy_txt = st.text_area("④ 本文ポリシー（編集可）", value=st.session_state.get("policy",""), height=140)

    colM1, colM2 = st.columns([1,1])
    with colM1:
        gen_lead = st.button("リード生成")
    with colM2:
        gen_body = st.button("本文生成")

    if gen_lead:
        content_dir = readers_txt + "\n" + needs_txt + "\n" + policy_txt
        lead_html = call_gemini(prompt_lead(keyword, content_dir, structure_html))
        st.session_state["lead_html"] = simplify_html(lead_html)

    if gen_body:
        policy_bullets = policy_txt if policy_txt.strip() else "- 事実は曖昧にしない\n- <h1>禁止\n- 箇条書きを適宜活用"
        body_html = call_gemini(prompt_body(keyword, structure_html, policy_bullets, banned_list))
        body_html = simplify_html(body_html)
        body_html = limit_h2_count(body_html, max_h2)
        st.session_state["body_html"] = body_html

    # まとめ生成
    gen_summary = st.button("まとめ生成")
    if gen_summary:
        content_dir = readers_txt + "\n" + needs_txt + "\n" + policy_txt
        article_for_summary = (st.session_state.get("lead_html","") + "\n" +
                               st.session_state.get("body_html",""))
        summary_html = call_gemini(prompt_summary(keyword, content_dir, article_for_summary))
        st.session_state["summary_html"] = simplify_html(summary_html)

    # プレビュー
    assembled = ""
    for key in ["lead_html","body_html","summary_html"]:
        if st.session_state.get(key):
            assembled += st.session_state[key].strip() + "\n\n"
    if assembled:
        st.markdown("#### 👀 プレビュー")
        st.write(assembled, unsafe_allow_html=True)
    st.session_state["assembled_html"] = assembled.strip()

with colR:
    st.header("3) 投稿（draft / 予約可）")
    # タイトル & ディスクリプション
    colT1, colT2 = st.columns([1,1])
    with colT1:
        if st.button("SEOタイトルを自動生成"):
            content_dir = (st.session_state.get("readers","") + "\n" +
                           st.session_state.get("needs",""))
            st.session_state["title"] = generate_seo_title(keyword, content_dir)
    with colT2:
        if st.button("メタディスクリプション自動生成"):
            t = st.session_state.get("title","") or f"{keyword}に関するポイント"
            content_dir = (st.session_state.get("readers","") + "\n" +
                           st.session_state.get("needs",""))
            st.session_state["excerpt"] = generate_seo_description(keyword, content_dir, t)

    title = st.text_input("タイトル", value=st.session_state.get("title",""))
    slug = st.text_input("スラッグ（空なら自動）", value="")
    excerpt = st.text_area("ディスクリプション（抜粋）", value=st.session_state.get("excerpt",""), height=80)

    status = st.selectbox("公開状態", ["draft","future","publish"], index=0)
    sched_date = st.date_input("予約日（future用）")
    sched_time = st.time_input("予約時刻（future用）", value=dt_time(9,0))

    do_post = st.button("📝 WPに下書き/投稿する", type="primary", use_container_width=True)

    if do_post:
        if not keyword.strip():
            st.error("キーワードは必須です。"); st.stop()
        if not title.strip():
            st.error("タイトルは必須です。"); st.stop()
        content_html = st.session_state.get("assembled_html","")
        if not content_html:
            st.error("本文が未生成です。『リード/本文/まとめ』を生成してください。"); st.stop()

        # 最終クリーニング
        content_html = simplify_html(content_html)

        # 予約日時
        date_gmt = None
        if status == "future":
            dt_local = datetime.combine(sched_date, sched_time)
            date_gmt = dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        payload = {
            "title": title.strip(),
            "content": content_html,
            "status": status,
            "slug": (slug.strip() or generate_permalink(keyword)),
            "excerpt": excerpt.strip()
        }
        if date_gmt:
            payload["date_gmt"] = date_gmt

        r = wp_post(BASE, "wp/v2/posts", AUTH, HEADERS, json_payload=payload)
        if r.status_code not in (200,201):
            st.error(f"投稿失敗: {r.status_code}")
            st.code(r.text[:1000])
            st.stop()
        data = r.json()
        st.success(f"投稿成功！ID={data.get('id')} / status={data.get('status')}")
        st.write("URL:", data.get("link",""))
        st.json({k: data.get(k) for k in ["id","slug","status","date","link"]})
