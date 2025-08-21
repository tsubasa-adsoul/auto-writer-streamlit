# streamlit_app.py
# ------------------------------------------------------------
# WP Auto Writer (Policy via .txt, Local-Only, No DB)
# - キーワード必須 → ①〜④（読者像/ニーズ/構成/本文方針）をAI生成
# - ④本文ポリシーは .txt を読み込み（複数可）→ 選択・編集・書き出し
# - 禁止事項は画面入力（1行=1項目）＋（任意）.txt 取込み
# - ?rest_route= 優先でWPへ draft/post（403回避）
# - アイキャッチ：未搭載（手作成運用）
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
st.title("📝 WP Auto Writer — .txt ポリシー対応（完全版）")

# ------------------------------
# Secrets 読み込み
# ------------------------------
if "wp_configs" not in st.secrets:
    st.error("Secrets に [wp_configs] がありません。App settings → Secrets で登録してください。")
    st.stop()

WP_CONFIGS: Dict[str, Dict[str, str]] = st.secrets["wp_configs"]  # 複数サイト対応
GEMINI_KEY = st.secrets.get("google", {}).get("gemini_api_key_1", None)
if not GEMINI_KEY:
    st.warning("Gemini APIキー（google.gemini_api_key_1）が未設定です。生成機能は動作しません。")

# ------------------------------
# WP エンドポイント補助
# ------------------------------
def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"

def api_candidates(base: str, route: str) -> List[str]:
    base = ensure_trailing_slash(base); route = route.lstrip("/")
    # ?rest_route= を優先（Xserver 等の403回避）
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
# 生成ユーティリティ
# ------------------------------
ALLOWED_TAGS = ['h2','h3','p','br','strong','em','ul','ol','li','table','tr','th','td']
MAX_H2 = 8

def simplify_html(html: str) -> str:
    # 許可タグ以外を除去（属性は許容 / 必要に応じて強化可）
    tags = re.findall(r'</?(\w+)[^>]*>', html)
    for tag in set(tags):
        if tag.lower() not in ALLOWED_TAGS:
            html = re.sub(rf'</?{tag}[^>]*>', '', html, flags=re.IGNORECASE)
    return html

def limit_h2_count(html: str, max_count: int = MAX_H2) -> str:
    h2s = re.findall(r'(<h2>.*?</h2>)', html, flags=re.DOTALL | re.IGNORECASE)
    if len(h2s) <= max_count:
        return html
    # 先頭 max_count 件のみ採用（本文が長く暴走するのを防ぐ）
    kept = "".join(h2s[:max_count]) + "\n"
    return kept

def generate_permalink(keyword_or_title: str) -> str:
    """日本語→英数ハイフン化。50字程度に短縮。"""
    import unicodedata
    s = keyword_or_title.lower()
    subs = {
        '先払い買取':'sakibarai-kaitori','先払い':'sakibarai','買取':'kaitori','口コミ':'kuchikomi',
        '評判':'hyoban','体験談':'taiken','レビュー':'review','比較':'hikaku','査定':'satei',
        'おすすめ':'osusume','ランキング':'ranking','評価':'hyoka','申込':'moushikomi','方法':'houhou',
        '流れ':'nagare','手順':'tejun'
    }
    for jp,en in subs.items(): s = s.replace(jp,en)
    s = unicodedata.normalize('NFKD', s)
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    s = re.sub(r'-{2,}', '-', s)
    if len(s) > 50:
        parts = s.split('-')
        s = '-'.join(parts[:5])
    return s or f"post-{int(datetime.now().timestamp())}"

# ------------------------------
# Gemini 呼び出し
# ------------------------------
def call_gemini(prompt: str, temperature: float = 0.2) -> str:
    if not GEMINI_KEY:
        raise RuntimeError("Gemini APIキーが未設定です。Secrets に google.gemini_api_key_1 を追加してください。")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent?key={GEMINI_KEY}"
    payload = {"contents":[{"parts":[{"text": prompt}]}], "generationConfig": {"temperature": temperature}}
    r = requests.post(endpoint, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini エラー: {r.status_code} / {r.text[:500]}")
    j = r.json()
    return j["candidates"][0]["content"]["parts"][0]["text"]

# ------------------------------
# プロンプト群（①〜④→リード→本文→まとめ、タイトル/説明）
# ------------------------------
def prompt_outline(keyword: str, extra: str, banned: List[str], max_h2: int) -> str:
    banned_block = "\n".join([f"・{b}" for b in banned]) if banned else "（なし）"
    return f"""
# 役割
あなたは日本語SEOに強いWeb編集者。キーワードから「①読者像」「②ニーズ」「③構成(HTML)」「④本文ポリシー」を作る。

# 入力
- キーワード: {keyword}
- 追加要素: {extra or "（指定なし）"}
- 禁止事項（絶対に含めない）:
{banned_block}

# 制約
- ①/②は150字程度で箇条書き
- ③は <h2>,<h3> のみ（<h1>禁止）。H2は最大 {max_h2} 個
- H2直下の導入文では「この記事では〜」等の定型句を使わない方針（後工程で反映）
- ④は文体/禁止語/表の扱いなどを箇条書きで

# 出力フォーマット（厳守）
① 読者像:
- ...

② ニーズ:
- ...

③ 構成（HTML）:
<h2>...</h2>
<h3>...</h3>

④ 本文ポリシー:
- ...
""".strip()

def prompt_lead(keyword: str, content_direction: str, structure_html: str) -> str:
    return f"""
# 役割: SEOライター
# 指示: 「{keyword}」のリード文を作成。必ず<h2>はじめに</h2>→<p>…</p>複数で書く。
# ルール:
- 読者の悩みに共感→本文で得られる具体メリット2つ以上→興味喚起→行動喚起の一文
- 一文につき<p>1つ。装飾タグは最小限

# 記事の方向性:
{content_direction}

# 構成案:
{structure_html}

# 出力:
""".strip()

def prompt_body(keyword: str, structure_html: str, policy_bullets: str, banned: List[str]) -> str:
    banned_block = "\n".join([f"・{b}" for b in banned]) if banned else "（なし）"
    return f"""
# 役割: SEOライター
# 任務: 構成（<h2>,<h3>）に沿って本文HTMLのみを書く（<h1>禁止）

# 厳守ルール
- H2直下導入で「この記事では〜」等の定型句を使わない
- 許可タグ: {', '.join(ALLOWED_TAGS)}（それ以外は出力しない）
- 事実は曖昧に書かない。不明は「不明/公式未記載」と明記
- 禁止事項（絶対NG）:
{banned_block}

# 文体/方針（箇条書き）
{policy_bullets}

# 入力
- キーワード: {keyword}
- 構成（HTML）:
{structure_html}

# 出力（本文HTMLのみ）:
""".strip()

def prompt_summary(keyword: str, content_dir: str, article_html: str) -> str:
    return f"""
# 役割: SEOライター
# 指示: 「{keyword}」の記事のまとめをHTMLで作成
# 形式: 先頭に<h2>まとめ</h2>。一文1<p>。箇条書き2-3可。<br>禁止。広告/PR文言禁止。

# 記事の方向性:
{content_dir}

# 参考（本文）:
{article_html}

# 出力:
""".strip()

def generate_seo_title(keyword: str, content_dir: str) -> str:
    p = f"""
# 役割: SEO編集者
# 指示: 32文字以内・日本語・【】や｜禁止。自然にキーワードを含めクリックしたくなる1本だけ。
# 入力: キーワード={keyword} / 方向性={content_dir}
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
# サイト選択 & 疎通
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
# セッション（ポリシー/禁止事項）
# ==============================
if "policy_store" not in st.session_state:
    # {name: text}
    st.session_state.policy_store = {"標準": "- 事実は曖昧にしない\n- <h1>は出力しない\n- 箇条書きを適宜活用"}
if "active_policy" not in st.session_state:
    st.session_state.active_policy = "標準"
if "policy_text" not in st.session_state:
    st.session_state.policy_text = st.session_state.policy_store["標準"]
if "banned_master" not in st.session_state:
    st.session_state.banned_master: List[str] = []

# ==============================
# 3カラム：入力 / 生成&プレビュー / 投稿
# ==============================
colL, colM, colR = st.columns([1.25, 1.6, 1.0])

# ------ 左：入力 / ポリシー管理(.txt) ------
with colL:
    st.header("1) 入力 & ポリシー管理（.txt）")

    # キーワード & 追加要素
    keyword = st.text_input("必須キーワード", placeholder="例：先払い買取 口コミ")
    extra_points = st.text_area("特に加えてほしい内容（任意）", height=100)

    # 禁止事項（手入力）
    banned_text = st.text_area("禁止事項（1行=1項目 / 厳守）", height=120)
    manual_banned = [l.strip() for l in banned_text.splitlines() if l.strip()]

    # 禁止事項 .txt 取込み（任意）
    st.caption("（任意）禁止事項.txt を読み込む → 行単位で結合されます。")
    banned_files = st.file_uploader("banned.txt（複数可）", type=["txt"], accept_multiple_files=True)
    file_banned: List[str] = []
    if banned_files:
        for f in banned_files:
            try:
                txt = f.read().decode("utf-8", errors="ignore")
                file_banned.extend([l.strip() for l in txt.splitlines() if l.strip()])
            except Exception as e:
                st.warning(f"{f.name}: 読み込み失敗 ({e})")

    merged_banned = st.session_state.banned_master + file_banned + manual_banned

    st.divider()
    st.subheader("④ 本文ポリシー（.txt でインポート/編集/保存）")

    # .txt 取込み（複数）
    pol_files = st.file_uploader("policy*.txt（複数可）を読み込む", type=["txt"], accept_multiple_files=True)
    if pol_files:
        for f in pol_files:
            try:
                txt = f.read().decode("utf-8", errors="ignore").strip()
                name = f.name.rsplit(".", 1)[0]
                st.session_state.policy_store[name] = txt
            except Exception as e:
                st.warning(f"{f.name}: 読み込み失敗 ({e})")

    # 選択
    names = sorted(st.session_state.policy_store.keys())
    sel = st.selectbox("適用するポリシー", names, index=names.index(st.session_state.active_policy) if st.session_state.active_policy in names else 0)
    if sel != st.session_state.active_policy:
        st.session_state.active_policy = sel
        st.session_state.policy_text = st.session_state.policy_store[sel]

    # 編集
    policy_txt = st.text_area("本文ポリシー（編集可 / ここが④）", value=st.session_state.policy_text, height=140)
    st.session_state.policy_text = policy_txt

    cA, cB, cC = st.columns([1,1,1])
    with cA:
        if st.button("この内容でプリセットを上書き保存"):
            st.session_state.policy_store[st.session_state.active_policy] = st.session_state.policy_text
            st.success(f"『{st.session_state.active_policy}』を更新しました。")
    with cB:
        if st.button("新規プリセットとして保存"):
            new_name = f"custom-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            st.session_state.policy_store[new_name] = st.session_state.policy_text
            st.session_state.active_policy = new_name
            st.success(f"新規プリセット『{new_name}』を作成しました。")
    with cC:
        st.download_button(
            "現在の本文ポリシーを policy.txt に書き出し",
            data=st.session_state.policy_text,
            file_name="policy.txt",
            mime="text/plain",
            use_container_width=True
        )

    st.caption("※ サイトごとに .txt を用意して読み込めば、個性（文体ルール）を使い回せます。")

# ------ 中：生成 & プレビュー ------
with colM:
    st.header("2) 生成 & プレビュー")

    # ①〜④ 生成
    max_h2 = st.number_input("H2の最大数", min_value=3, max_value=12, value=MAX_H2, step=1)
    if st.button("①〜④（読者像/ニーズ/構成/本文ポリシー）を生成"):
        if not keyword.strip():
            st.error("キーワードは必須です。"); st.stop()
        outline_raw = call_gemini(prompt_outline(keyword, extra_points, merged_banned, max_h2))
        # ざっくり抽出
        readers = re.search(r'①[^\n]*\n(.+?)\n\n②', outline_raw, flags=re.DOTALL)
        needs   = re.search(r'②[^\n]*\n(.+?)\n\n③', outline_raw, flags=re.DOTALL)
        struct  = re.search(r'③[^\n]*\n(.+?)\n\n④', outline_raw, flags=re.DOTALL)
        policy  = re.search(r'④[^\n]*\n(.+)$',       outline_raw, flags=re.DOTALL)

        st.session_state["readers"] = (readers.group(1).strip() if readers else "")
        st.session_state["needs"]   = (needs.group(1).strip()   if needs   else "")
        structure_html = (struct.group(1).strip() if struct else "").replace("\r","")
        structure_html = simplify_html(structure_html)
        structure_html = limit_h2_count(structure_html, max_h2)
        st.session_state["structure_html"] = structure_html
        # ④は「生成された方針」より .txt を優先する運用にするため、UI上は参考表示のみに留める
        st.session_state["policy_generated"] = (policy.group(1).strip() if policy else "")

    # 手直しエディタ
    readers_txt   = st.text_area("① 読者像（編集可）", value=st.session_state.get("readers",""), height=110)
    needs_txt     = st.text_area("② ニーズ（編集可）",   value=st.session_state.get("needs",""),   height=110)
    structure_html= st.text_area("③ 構成（HTML / 編集可）", value=st.session_state.get("structure_html",""), height=160)
    st.expander("参考：④ 本文ポリシー（AIが出した案）", expanded=False).write(st.session_state.get("policy_generated","") or "（未生成）")

    colM1, colM2, colM3 = st.columns([1,1,1])
    with colM1:
        gen_lead = st.button("リード生成")
    with colM2:
        gen_body = st.button("本文生成")
    with colM3:
        gen_summary = st.button("まとめ生成")

    if gen_lead:
        content_dir = readers_txt + "\n" + needs_txt + "\n" + st.session_state.policy_text
        lead_html = call_gemini(prompt_lead(keyword, content_dir, structure_html))
        st.session_state["lead_html"] = simplify_html(lead_html)

    if gen_body:
        policy_bullets = st.session_state.policy_text or "- 事実は曖昧にしない\n- <h1>禁止\n- 箇条書きを適宜活用"
        body_html = call_gemini(prompt_body(keyword, structure_html, policy_bullets, merged_banned))
        body_html = simplify_html(body_html); body_html = limit_h2_count(body_html, max_h2)
        st.session_state["body_html"] = body_html

    if gen_summary:
        content_dir = readers_txt + "\n" + needs_txt + "\n" + st.session_state.policy_text
        article_for_summary = (st.session_state.get("lead_html","") + "\n" + st.session_state.get("body_html",""))
        summary_html = call_gemini(prompt_summary(keyword, content_dir, article_for_summary))
        st.session_state["summary_html"] = simplify_html(summary_html)

    # プレビュー & 編集
    assembled = ""
    for key in ["lead_html","body_html","summary_html"]:
        if st.session_state.get(key):
            assembled += st.session_state[key].strip() + "\n\n"
    if assembled:
        st.markdown("#### 👀 プレビュー")
        st.write(assembled, unsafe_allow_html=True)
    st.session_state["assembled_html"] = assembled.strip()

    with st.expander("✏️ プレビューを編集（この内容を下書きに送付）", expanded=False):
        st.caption("※ ここでの修正が最終本文になります。HTMLで編集可。")
        st.session_state["edited_html"] = st.text_area(
            "編集用HTML",
            value=st.session_state.get("edited_html", st.session_state.get("assembled_html","")),
            height=420
        )
        st.session_state["use_edited"] = st.checkbox("編集したHTMLを採用する", value=True)

# ------ 右：タイトル/説明 → 投稿 ------
with colR:
    st.header("3) タイトル/説明 → 投稿")

    content_dir = (st.session_state.get("readers","") + "\n" +
                   st.session_state.get("needs","") + "\n" +
                   st.session_state.policy_text)
    content_source = st.session_state.get("edited_html") or st.session_state.get("assembled_html","")

    colT1, colT2 = st.columns([1,1])
    with colT1:
        if st.button("SEOタイトル自動生成"):
            if not content_source.strip():
                st.warning("先に本文（編集後）を用意してください。")
            else:
                st.session_state["title"] = generate_seo_title(keyword, content_dir)
    with colT2:
        if st.button("メタディスクリプション自動生成"):
            t = st.session_state.get("title","") or f"{keyword}に関するポイント"
            if not content_source.strip():
                st.warning("先に本文（編集後）を用意してください。")
            else:
                st.session_state["excerpt"] = generate_seo_description(keyword, content_dir, t)

    title = st.text_input("タイトル", value=st.session_state.get("title",""))
    slug  = st.text_input("スラッグ（空なら自動）", value="")
    excerpt = st.text_area("ディスクリプション（抜粋）", value=st.session_state.get("excerpt",""), height=80)

    status = st.selectbox("公開状態", ["draft","future","publish"], index=0)
    sched_date = st.date_input("予約日（future用）")
    sched_time = st.time_input("予約時刻（future用）", value=dt_time(9,0))

    if st.button("📝 WPに下書き/投稿する", type="primary", use_container_width=True):
        if not keyword.strip():
            st.error("キーワードは必須です。"); st.stop()
        if not title.strip():
            st.error("タイトルは必須です。"); st.stop()

        content_html = (st.session_state.get("edited_html") if st.session_state.get("use_edited")
                        else st.session_state.get("assembled_html","")).strip()
        if not content_html:
            st.error("本文が未生成です。『リード/本文/まとめ』を生成し、必要なら編集してください。"); st.stop()

        content_html = simplify_html(content_html)

        # 予約日時（GMT）
        date_gmt = None
        if status == "future":
            from datetime import datetime as dt
            dt_local = dt.combine(sched_date, sched_time)
            date_gmt = dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        final_slug = (slug.strip() or generate_permalink(title or keyword))

        payload = {
            "title": title.strip(),
            "content": content_html,
            "status": status,
            "slug": final_slug,
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
