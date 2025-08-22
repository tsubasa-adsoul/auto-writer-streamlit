# streamlit_app.py
# ------------------------------------------------------------
# WP Auto Writer (Final One‑Shot)
# - ④本文ポリシーは .txt 読み込み（AIで④は生成しない）
# - ①読者像 / ②ニーズ / ③構成 をAI生成
# - 記事（リード→本文→まとめ）は 1 回のリクエストで一括生成
# - 禁止事項は手入力のみ（アップロードなし）
# - プリセット：.txt読み込み→選択→編集→上書き/削除（defaultは削除不可）
# - F5対策：policies_cache.json に保存/復元
# - WP投稿は ?rest_route= 優先で下書き/予約/公開に対応
# ------------------------------------------------------------

from __future__ import annotations
import re
import json
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone, time as dt_time
from typing import Dict, Any, List

import requests
from requests.auth import HTTPBasicAuth
import streamlit as st

# ==============================
# 基本設定
# ==============================
st.set_page_config(page_title="WP Auto Writer", page_icon="📝", layout="wide")
st.title("📝 WP Auto Writer — 一括生成（④は .txt 読み込み）")

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
    base = ensure_trailing_slash(base)
    route = route.lstrip("/")
    return [f"{base}?rest_route=/{route}", f"{base}wp-json/{route}"]  # ?rest_route= 優先


def wp_get(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str]) -> requests.Response:
    for url in api_candidates(base, route):
        r = requests.get(url, auth=auth, headers=headers, timeout=20)
        if r.status_code == 200:
            return r
    return r


def wp_post(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str], json_payload: Dict[str, Any]) -> requests.Response:
    for url in api_candidates(base, route):
        r = requests.post(url, auth=auth, headers=headers, json=json_payload, timeout=45)
        if r.status_code in (200, 201):
            return r
    return r

HEADERS = {
    "User-Agent": "Mozilla/5.0 (AutoWriter/Streamlit)",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ------------------------------
# 生成ユーティリティ / バリデータ
# ------------------------------
ALLOWED_TAGS = ["h2", "h3", "p", "strong", "em", "ul", "ol", "li", "table", "tr", "th", "td"]  # <br>禁止
MAX_H2 = 8


def simplify_html(html: str) -> str:
    # 許可外タグの除去 & <br>禁止
    tags = re.findall(r"</?(\w+)[^>]*>", html)
    for tag in set(tags):
        if tag.lower() not in ALLOWED_TAGS:
            html = re.sub(rf"</?{tag}[^>]*>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "", html, flags=re.IGNORECASE)
    return html


def limit_h2_count(html: str, max_count: int = MAX_H2) -> str:
    h2s = re.findall(r"(<h2>.*?</h2>)", html, flags=re.DOTALL | re.IGNORECASE)
    if len(h2s) <= max_count:
        return html
    # 先頭のH2だけ残す（本文は構成通りに再生成済みの想定）
    parts = re.split(r"(<h2>.*?</h2>)", html, flags=re.DOTALL | re.IGNORECASE)
    kept, cnt = [], 0
    for p in parts:
        if re.match(r"<h2>.*?</h2>", p, flags=re.IGNORECASE):
            if cnt < max_count:
                kept.append(p)
            cnt += 1
        else:
            kept.append(p)
    return "".join(kept)


def generate_permalink(keyword_or_title: str) -> str:
    import unicodedata as _ud
    s = keyword_or_title.lower()
    subs = {
        "先払い買取": "sakibarai-kaitori",
        "先払い": "sakibarai",
        "買取": "kaitori",
        "口コミ": "kuchikomi",
        "評判": "hyoban",
        "体験談": "taiken",
        "レビュー": "review",
        "比較": "hikaku",
        "査定": "satei",
        "おすすめ": "osusume",
        "ランキング": "ranking",
        "評価": "hyoka",
        "申込": "moushikomi",
        "方法": "houhou",
        "流れ": "nagare",
        "手順": "tejun",
    }
    for jp, en in subs.items():
        s = s.replace(jp, en)
    s = _ud.normalize("NFKD", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if len(s) > 50:
        parts = s.split("-")
        s = "-".join(parts[:5])
    return s or f"post-{int(datetime.now().timestamp())}"


def validate_article(html: str) -> List[str]:
    warns: List[str] = []
    if re.search(r"<h4|<script|<style", html, flags=re.IGNORECASE):
        warns.append("禁止タグ（h4/script/style）が含まれています。")
    if re.search(r"<br\s*/?>", html, flags=re.IGNORECASE):
        warns.append("<br> タグは使用禁止です。すべて <p> に置き換えてください。")

    # H2セクション内にul/ol/tableのいずれかを含むか
    h2_iter = list(re.finditer(r"(<h2>.*?</h2>)", html, flags=re.DOTALL | re.IGNORECASE))
    for i, m in enumerate(h2_iter):
        start = m.end()
        end = h2_iter[i + 1].start() if i + 1 < len(h2_iter) else len(html)
        section = html[start:end]
        if not re.search(r"<(ul|ol|table)\b", section, flags=re.IGNORECASE):
            warns.append("H2セクションに表（table）または箇条書き（ul/ol）が不足しています。")

    # h3直下の<p>数（目安）
    h3_positions = list(re.finditer(r"(<h3>.*?</h3>)", html, flags=re.DOTALL | re.IGNORECASE))
    for i, m in enumerate(h3_positions):
        start = m.end()
        next_head = re.search(r"(<h2>|<h3>)", html[start:], flags=re.IGNORECASE)
        end = start + next_head.start() if next_head else len(html)
        block = html[start:end]
        p_count = len(re.findall(r"<p>.*?</p>", block, flags=re.DOTALL | re.IGNORECASE))
        if p_count < 3 or p_count > 6:
            warns.append("各<h3>直下は4〜5文（<p>）が目安です。分量を調整してください。")

    # 一文55文字以内（概算）
    for p in re.findall(r"<p>(.*?)</p>", html, flags=re.DOTALL | re.IGNORECASE):
        text = re.sub(r"<.*?>", "", p)
        if len(text.strip()) > 55:
            warns.append("一文が55文字を超えています。短く区切ってください。")
            break

    # 全体6000文字以内
    plain = re.sub(r"<.*?>", "", html)
    if len(plain.strip()) > 6000:
        warns.append("記事全体が6000文字を超えています。要約・整理してください。")
    return warns

# ------------------------------
# Gemini 呼び出し
# ------------------------------

def call_gemini(prompt: str, temperature: float = 0.2) -> str:
    if not GEMINI_KEY:
        raise RuntimeError("Gemini APIキーが未設定です。Secrets に google.gemini_api_key_1 を追加してください。")
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent?key={GEMINI_KEY}"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": temperature}}
    r = requests.post(endpoint, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini エラー: {r.status_code} / {r.text[:500]}")
    j = r.json()
    return j["candidates"][0]["content"]["parts"][0]["text"]

# ------------------------------
# プロンプト群（④なし / 一括生成）
# ------------------------------

def prompt_outline_123(keyword: str, extra: str, banned: List[str], max_h2: int) -> str:
    banned_block = "\n".join([f"・{b}" for b in banned]) if banned else "（なし）"
    return f"""
# 役割
あなたは日本語SEOに強いWeb編集者。キーワードから「①読者像」「②ニーズ」「③構成(HTML)」を作る。

# 入力
- キーワード: {keyword}
- 追加要素: {extra or "（指定なし）"}
- 禁止事項（絶対に含めない）:
{banned_block}

# 制約
- ①/②は150字程度で箇条書き
- ③は <h2>,<h3> のみ（<h1>禁止）。H2は最大 {max_h2} 個
- H2直下の導入文では「この記事では〜」等の定型句を使わない方針（後工程で反映）

# 出力フォーマット（厳守）
① 読者像:
- ...

② ニーズ:
- ...

③ 構成（HTML）:
<h2>...</h2>
<h3>...</h3>
""".strip()


def prompt_full_article(
    keyword: str,
    policy_text: str,
    structure_html: str,
    readers_txt: str,
    needs_txt: str,
    banned: List[str],
) -> str:
    banned_block = "\n".join([f"・{b}" for b in banned]) if banned else "（なし）"
    return f"""
# 命令書:
あなたはSEOに特化したプロライターです。
以下の構成案と本文ポリシーに従い、「{keyword}」の記事を
**リード文 → 本文 → まとめ**まで一気通貫でHTML出力してください。

# 出力形式（厳守）:
- 先頭に必ず <h2>はじめに</h2> を置き、その直後にリード文を <p> で複数出力すること
- 各 <h2> の冒頭には導入段落（3行程度）を <p> で置くこと
- 各 <h3> 直下には 4〜5 文（≈400字）の解説を <p> で出力すること
- 最後に必ず <h2>まとめ</h2> を置き、一文1<p> で要点をまとめ、必要に応じて箇条書きを入れること
- 一文は55文字以内。1文=1<p>。<br> は絶対に使用禁止
- 許可タグは {', '.join(ALLOWED_TAGS)} のみ（これ以外は出力しない）
- <h1>, <h4>, <script>, <style> は出力禁止

# 本文ポリシー（厳守）:
{policy_text}

# 禁止事項（絶対に含めない）:
{banned_block}

# 記事の方向性（参考）:
[読者像]
{readers_txt}

[ニーズ]
{needs_txt}

# 構成案（この<h2><h3>構成を厳密に守る）:
{structure_html}

# 出力:
（HTMLのみを出力）
""".strip()


def generate_seo_title(keyword: str, content_dir: str) -> str:
    p = f"""
# 役割: SEO編集者
# 指示: 32文字以内・日本語・【】や｜禁止。自然にキーワードを含めクリックしたくなる1本だけ。
# 入力: キーワード={keyword} / 方向性={content_dir}
# 出力: タイトルのみ
"""
    title = call_gemini(p).strip()
    title = re.sub(r"[【】｜\n\r]", "", title)
    return title[:32]


def generate_seo_description(keyword: str, content_dir: str, title: str) -> str:
    p = f"""
# 役割: SEO編集者
# 指示: 120字以内。定型「〜を解説/紹介」禁止。数字や具体メリットを入れてCTRを高める。
# 入力: キーワード={keyword} / タイトル={title} / 方向性={content_dir}
# 出力: 説明文のみ
"""
    desc = call_gemini(p).strip()
    return re.sub(r"[\n\r]", "", desc)[:120]

# ------------------------------
# ローカルキャッシュ（F5対策）
# ------------------------------
CACHE_PATH = Path("./policies_cache.json")


def load_policies_from_cache():
    try:
        if CACHE_PATH.exists():
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        st.warning(f"ポリシーキャッシュ読込エラー: {e}")
    return None


def save_policies_to_cache(policy_store: dict, active_policy: str):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"policy_store": policy_store, "active_policy": active_policy}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.warning(f"ポリシーキャッシュ保存エラー: {e}")

# ------------------------------
# サイト選択 & 疎通
# ------------------------------
st.sidebar.header("接続先（WP）")
site_key = st.sidebar.selectbox("投稿先サイト", sorted(WP_CONFIGS.keys()))
cfg = WP_CONFIGS[site_key]
BASE = ensure_trailing_slash(cfg["url"])  # 例: https://example.com/
AUTH = HTTPBasicAuth(cfg["user"], cfg["password"])  # Application Password 推奨

if st.sidebar.button("🔐 認証 /users/me"):
    r = wp_get(BASE, "wp/v2/users/me", AUTH, HEADERS)
    st.sidebar.code(f"GET users/me → {r.status_code}")
    st.sidebar.caption(r.text[:300])

# ------------------------------
# セッション（ポリシー/禁止事項）
# ------------------------------
DEFAULT_POLICY_NAME = "default"
DEFAULT_POLICY_TXT = (
    "・プロンプト③で出力された <h2> と <h3> 構成を維持し、それぞれの直下に <p> タグで本文を記述\n"
    "・各 <h2> の冒頭に導入段落（3行程度）を <p> で挿入\n"
    "・各 <h3> の直下には4～5文程度（400字程度）の詳細な解説を記述\n"
    "・<h4>、<script>、<style> などは禁止\n"
    "・一文は55文字以内に収めること\n"
    "・一文ごとに独立した<p>タグで記述（<br>タグは禁止）\n"
    "・必要に応じて<ul>、<ol>、<li>、<table>、<tr>、<th>、<td>タグで情報整理\n"
    "・各H2セクションには必ず1つ以上の表（table）または箇条書き（ul/ol）を含める\n"
    "・比較が適する情報は必ず<table>で整理（メリデメ等）\n"
    "・PREP法もしくはSDS法で書く\n"
    "・横文字を使用しない\n"
    "・冗長表現を使用しない\n"
    "・語尾バリエーションを持たせる\n"
    "・記事全体は6000文字以内\n"
    "・先頭に<h2>はじめに</h2>を置き、リード文を<p>で複数行\n"
    "・末尾に<h2>まとめ</h2>を置き、一文1<p>＋必要に応じて箇条書き\n"
)

# 初期化（KeyError対策を兼ねて setdefault 利用）
ss = st.session_state
ss.setdefault("policy_store", {DEFAULT_POLICY_NAME: DEFAULT_POLICY_TXT})
ss.setdefault("active_policy", DEFAULT_POLICY_NAME)
ss.setdefault("policy_text", ss["policy_store"][DEFAULT_POLICY_NAME])
ss.setdefault("banned_text", "")
ss.setdefault("readers", "")
ss.setdefault("needs", "")
ss.setdefault("structure_html", "")
ss.setdefault("assembled_html", "")
ss.setdefault("edited_html", "")
ss.setdefault("use_edited", True)

# F5対策：キャッシュ読込（あれば上書き）
cached = load_policies_from_cache()
if cached and isinstance(cached, dict):
    if isinstance(cached.get("policy_store"), dict):
        ss["policy_store"].update(cached["policy_store"])  # 既存にマージ
    active = cached.get("active_policy")
    if active and active in ss["policy_store"]:
        ss["active_policy"] = active
        ss["policy_text"] = ss["policy_store"][active]

# ==============================
# 3カラム：入力 / 生成&プレビュー / 投稿
# ==============================
colL, colM, colR = st.columns([1.25, 1.6, 1.05])

# ------ 左：入力 / ポリシー管理(.txt) ------
with colL:
    st.header("1) 入力 & ポリシー管理（.txt）")

    # キーワード & 追加要素
    keyword = st.text_input("必須キーワード", placeholder="例：先払い買取 口コミ")
    extra_points = st.text_area("特に加えてほしい内容（任意）", height=96)

    # 禁止事項（手入力のみ）
    st.markdown("### 🚫 禁止事項（任意・1行=1項目）")
    banned_text = st.text_area("禁止ワード・禁止表現", value=ss.get("banned_text", ""), height=120)
    ss["banned_text"] = banned_text
    merged_banned = [l.strip() for l in banned_text.splitlines() if l.strip()]

    st.divider()
    st.subheader("④ 本文ポリシー（.txt 読み込み→選択→編集→保存）")

    # .txt 読み込み（複数可）
    pol_files = st.file_uploader("policy*.txt（複数可）を読み込む", type=["txt"], accept_multiple_files=True)
    if pol_files:
        for f in pol_files:
            try:
                txt = f.read().decode("utf-8", errors="ignore").strip()
                name = f.name.rsplit(".", 1)[0]  # 例: sato-policy
                ss["policy_store"][name] = txt
                # 読み込んだものをアクティブに
                ss["active_policy"] = name
                ss["policy_text"] = txt
            except Exception as e:
                st.warning(f"{f.name}: 読み込み失敗 ({e})")
        save_policies_to_cache(ss["policy_store"], ss["active_policy"])

    # プリセット選択
    names = sorted(ss["policy_store"].keys())
    try:
        sel_index = names.index(ss["active_policy"]) if ss["active_policy"] in names else 0
    except ValueError:
        sel_index = 0
        ss["active_policy"] = names[0]
        ss["policy_text"] = ss["policy_store"][names[0]]

    sel = st.selectbox("適用するポリシー", names, index=sel_index)
    if sel != ss["active_policy"]:
        ss["active_policy"] = sel
        ss["policy_text"] = ss["policy_store"][sel]
        save_policies_to_cache(ss["policy_store"], ss["active_policy"])

    # 編集
    st.markdown("### ✏️ 本文ポリシー（編集可）")
    policy_txt = st.text_area(
        "ここをそのまま使う or 必要なら書き換え",
        value=ss.get("policy_text", DEFAULT_POLICY_TXT),
        height=220,
    )
    ss["policy_text"] = policy_txt

    cA, cB, cC = st.columns([1, 1, 1])
    with cA:
        if st.button("この内容でプリセットを上書き保存"):
            ss["policy_store"][ss["active_policy"]] = ss["policy_text"]
            save_policies_to_cache(ss["policy_store"], ss["active_policy"])
            st.success(f"『{ss['active_policy']}』を更新しました。")
    with cB:
        st.download_button(
            "この内容を .txt で保存",
            data=ss["policy_text"],
            file_name=f"{ss['active_policy']}.txt",
            mime="text/plain",
            use_container_width=True,
        )
    with cC:
        if ss["active_policy"] != DEFAULT_POLICY_NAME:
            if st.button("このプリセットを削除"):
                try:
                    del ss["policy_store"][ss["active_policy"]]
                except KeyError:
                    pass
                # デフォルトへ安全にフォールバック
                ss["active_policy"] = DEFAULT_POLICY_NAME
                ss["policy_text"] = ss["policy_store"].get(DEFAULT_POLICY_NAME, DEFAULT_POLICY_TXT)
                save_policies_to_cache(ss["policy_store"], ss["active_policy"])
                st.warning("プリセットを削除しました。")

# ------ 中：生成 & プレビュー ------
with colM:
    st.header("2) 生成 & プレビュー（一括）")

    max_h2 = st.number_input("H2の最大数", min_value=3, max_value=12, value=MAX_H2, step=1)
    c1, c2 = st.columns([1, 1])

    with c1:
        if st.button("①〜③（読者像/ニーズ/構成）を生成"):
            if not keyword.strip():
                st.error("キーワードは必須です。")
            else:
                outline_raw = call_gemini(prompt_outline_123(keyword, extra_points, merged_banned, max_h2))
                readers = re.search(r"①[^\n]*\n(.+?)\n\n②", outline_raw, flags=re.DOTALL)
                needs = re.search(r"②[^\n]*\n(.+?)\n\n③", outline_raw, flags=re.DOTALL)
                struct = re.search(r"③[^\n]*\n(.+)$", outline_raw, flags=re.DOTALL)

                ss["readers"] = (readers.group(1).strip() if readers else "")
                ss["needs"] = (needs.group(1).strip() if needs else "")
                structure_html = (struct.group(1).strip() if struct else "").replace("\r", "")
                structure_html = simplify_html(structure_html)
                structure_html = limit_h2_count(structure_html, max_h2)
                ss["structure_html"] = structure_html

    readers_txt = st.text_area("① 読者像（編集可）", value=ss.get("readers", ""), height=110)
    needs_txt = st.text_area("② ニーズ（編集可）", value=ss.get("needs", ""), height=110)
    structure_html = st.text_area("③ 構成（HTML / 編集可）", value=ss.get("structure_html", ""), height=180)

    with c2:
        if st.button("記事を一括生成（リード→本文→まとめ）", type="primary"):
            if not keyword.strip():
                st.error("キーワードは必須です。")
            elif not structure_html.strip():
                st.error("まず③の構成（HTML）を用意してください。")
            else:
                one_shot = call_gemini(
                    prompt_full_article(keyword, ss["policy_text"], structure_html, readers_txt, needs_txt, merged_banned)
                )
                one_shot = simplify_html(one_shot)
                one_shot = limit_h2_count(one_shot, max_h2)
                ss["assembled_html"] = one_shot
                ss["edited_html"] = one_shot
                ss["use_edited"] = True

    # プレビュー & 検査
    assembled = ss.get("assembled_html", "").strip()
    if assembled:
        st.markdown("#### 👀 プレビュー")
        st.write(assembled, unsafe_allow_html=True)
        issues = validate_article(assembled)
        if issues:
            st.warning("検査結果:\n- " + "\n- ".join(issues))

    with st.expander("✏️ プレビューを編集（この内容を下書きに送付）", expanded=False):
        st.caption("※ ここでの修正が最終本文になります。HTMLで編集可。")
        ss["edited_html"] = st.text_area(
            "編集用HTML",
            value=ss.get("edited_html", ss.get("assembled_html", "")),
            height=420,
        )
        ss["use_edited"] = st.checkbox("編集したHTMLを採用する", value=ss.get("use_edited", True))

# ------ 右：タイトル/説明 → 投稿 ------
with colR:
    st.header("3) タイトル/説明 → 投稿")

    content_dir = (ss.get("readers", "") + "\n" + ss.get("needs", "") + "\n" + ss.get("policy_text", ""))
    content_source = ss.get("edited_html") or ss.get("assembled_html", "")

    colT1, colT2 = st.columns([1, 1])
    with colT1:
        if st.button("SEOタイトル自動生成"):
            if not content_source.strip():
                st.warning("先に本文（編集後）を用意してください。")
            else:
                ss["title"] = generate_seo_title(keyword, content_dir)
    with colT2:
        if st.button("メタディスクリプション自動生成"):
            t = ss.get("title", "") or f"{keyword}に関するポイント"
            if not content_source.strip():
                st.warning("先に本文（編集後）を用意してください。")
            else:
                ss["excerpt"] = generate_seo_description(keyword, content_dir, t)

    title = st.text_input("タイトル", value=ss.get("title", ""))
    slug = st.text_input("スラッグ（空なら自動）", value="")
    # （既存）ディスクリプション入力
    excerpt = st.text_area("ディスクリプション（抜粋）", value=ss.get("excerpt", ""), height=80)

    # ▼ここから：カテゴリーUI（cfg.categories → wp_categories → REST の順で取得）
    def fetch_categories(base_url: str, auth: HTTPBasicAuth) -> list[tuple[str, int]]:
        """RESTでカテゴリ一覧を取得して (label, id) のリストを返す。失敗なら空。"""
        try:
            r = wp_get(base_url, "wp/v2/categories?per_page=100&_fields=id,name", auth, HEADERS)
            if r is not None and r.status_code == 200:
                data = r.json()
                pairs = [(c.get("name", "(no name)"), int(c.get("id"))) for c in data if c.get("id") is not None]
                return sorted(pairs, key=lambda x: x[0])
        except Exception:
            pass
        return []

    # 1) Secrets: [wp_configs.<site_key>].categories を最優先
    cfg_cats_map: dict[str, int] = dict(cfg.get("categories", {}))  # cfg は WP_CONFIGS[site_key]
    cats: list[tuple[str, int]] = []
    if cfg_cats_map:
        cats = sorted([(name, int(cid)) for name, cid in cfg_cats_map.items()], key=lambda x: x[0])
    else:
        # 2) Secrets: [wp_categories.<site_key>] フォールバック
        sc_map: dict[str, int] = st.secrets.get("wp_categories", {}).get(site_key, {})
        if sc_map:
            cats = sorted([(name, int(cid)) for name, cid in sc_map.items()], key=lambda x: x[0])
        else:
            # 3) 最後の手段：RESTで取得
            cats = fetch_categories(BASE, AUTH)

    # UI
    cat_labels = [name for (name, _cid) in cats]
    default_labels: list[str] = []  # 既定選択したいラベルがあれば入れる（例: ["先払い買取コラム"]）
    sel_labels: list[str] = st.multiselect("カテゴリー（複数可）", cat_labels, default=default_labels)
    selected_cat_ids: list[int] = [cid for (name, cid) in cats if name in sel_labels]
    if not cats:
        st.info("このサイトで選べるカテゴリーが見つかりませんでした。Secretsの `wp_configs.<site_key>.categories` を確認してください。")

    # （既存）公開状態などはこの下に続く
    status_options = {
        "下書き": "draft",
        "予約投稿": "future",
        "公開": "publish"
    }

    # セレクトボックスは日本語表示
    status_label = st.selectbox("公開状態", list(status_options.keys()), index=0)

    # 実際に送信する値は英語
    status = status_options[status_label]
    sched_date = st.date_input("予約日（予約投稿用）")
    sched_time = st.time_input("予約時刻（予約投稿用）", value=dt_time(9, 0))

    if st.button("📝 WPに下書き/投稿する", type="primary", use_container_width=True):
        if not keyword.strip():
            st.error("キーワードは必須です。")
            st.stop()
        if not title.strip():
            st.error("タイトルは必須です。")
            st.stop()

        content_html = (ss.get("edited_html") if ss.get("use_edited") else ss.get("assembled_html", "")).strip()
        if not content_html:
            st.error("本文が未生成です。『記事を一括生成』で作成し、必要なら編集してください。")
            st.stop()

        content_html = simplify_html(content_html)

        date_gmt = None
        if status == "future":
            from datetime import datetime as _dt
            dt_local = _dt.combine(sched_date, sched_time)
            date_gmt = dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        final_slug = (slug.strip() or generate_permalink(title or keyword))

    payload = {
        "title": title.strip(),
        "content": content_html,
        "status": status,
        "slug": final_slug,
        "excerpt": excerpt.strip(),
    }
    if date_gmt:
        payload["date_gmt"] = date_gmt

    # カテゴリ（ID配列）
    if selected_cat_ids:
        payload["categories"] = selected_cat_ids


        r = wp_post(BASE, "wp/v2/posts", AUTH, HEADERS, json_payload=payload)
        if r.status_code not in (200, 201):
            st.error(f"投稿失敗: {r.status_code}")
            st.code(r.text[:1000])
            st.stop()
        data = r.json()
        st.success(f"投稿成功！ID={data.get('id')} / status={data.get('status')}")
        st.write("URL:", data.get("link", ""))
        st.json({k: data.get(k) for k in ["id", "slug", "status", "date", "link"]})

# 以上
