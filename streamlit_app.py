# streamlit_app.py
# ------------------------------------------------------------
# WP Auto Writer (Final One‑Shot / 互換・完全版 / 統合ポリシー版)
# - ④ポリシーは .txt を「1ファイル=1区分」で保持（中に [リード文]/[本文指示]/[まとめ文] を含める）
#   ※区切りが無い古い .txt は「本文のみ」として互換運用（リード/まとめは既定文を適用）
# - ①読者像 / ②ニーズ / ③構成 をAI生成（H2は最小/最大数を強制遵守）
# - 記事（リード→本文→まとめ）は 1 回のリクエストで一括生成
# - 禁止事項は手入力のみ（アップロードなし）
# - ポリシープリセット：.txt読み込み→選択→編集→上書き/削除→ローカルキャッシュでF5後も維持
# - ?rest_route= 優先でWP下書き/予約/公開（403回避）
# - カテゴリ選択：Secretsの `wp_configs.<site>.categories` があれば使用 / 無ければRESTで取得
# - 公開状態：日本語UI（下書き/予約投稿/公開）→ API送信値は英語にマップ
# ------------------------------------------------------------
from __future__ import annotations

import re
import json
from pathlib import Path
from datetime import datetime, timezone, time as dt_time
from typing import Dict, Any, List, Tuple

import requests
from requests.auth import HTTPBasicAuth
import streamlit as st

# ==============================
# 基本設定
# ==============================
st.set_page_config(page_title="WP Auto Writer", page_icon="📝", layout="wide")
st.title("📝 WP Auto Writer — 一括生成")

# ------------------------------
# Secrets 読み込み
# ------------------------------
if "wp_configs" not in st.secrets:
    st.error("Secrets に [wp_configs] がありません。App settings → Secrets で登録してください。")
    st.stop()

WP_CONFIGS: Dict[str, Dict[str, Any]] = st.secrets["wp_configs"]  # 複数サイト対応
GEMINI_KEY = st.secrets.get("google", {}).get("gemini_api_key_1", None)
if not GEMINI_KEY:
    st.warning("Gemini APIキー（google.gemini_api_key_1）が未設定です。生成機能は動作しません。")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (AutoWriter/Streamlit)",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ------------------------------
# WP エンドポイント補助
# ------------------------------
def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"

def api_candidates(base: str, route: str) -> List[str]:
    base = ensure_trailing_slash(base)
    route = route.lstrip("/")
    return [f"{base}?rest_route=/{route}", f"{base}wp-json/{route}"]  # ?rest_route= 優先

def wp_get(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str]) -> requests.Response | None:
    last = None
    for url in api_candidates(base, route):
        r = requests.get(url, auth=auth, headers=headers, timeout=20)
        last = r
        if r.status_code == 200:
            return r
    return last

def wp_post(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str],
            json_payload: Dict[str, Any]) -> requests.Response | None:
    last = None
    for url in api_candidates(base, route):
        r = requests.post(url, auth=auth, headers=headers, json=json_payload, timeout=45)
        last = r
        if r.status_code in (200, 201):
            return r
    return last

# ------------------------------
# 生成ユーティリティ / バリデータ
# ------------------------------
ALLOWED_TAGS = ['h2', 'h3', 'p', 'strong', 'em', 'ul', 'ol', 'li', 'table', 'tr', 'th', 'td']  # <br>禁止
MAX_H2 = 8
H2_RE = re.compile(r'(<h2>.*?</h2>)', re.IGNORECASE | re.DOTALL)

def simplify_html(html: str) -> str:
    tags = re.findall(r'</?(\w+)[^>]*>', html)
    for tag in set(tags):
        if tag.lower() not in ALLOWED_TAGS:
            html = re.sub(rf'</?{tag}[^>]*>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<br\s*/?>', '', html, flags=re.IGNORECASE)  # 絶対禁止
    return html

def validate_article(html: str) -> List[str]:
    warns: List[str] = []
    if re.search(r'<h4|<script|<style', html, flags=re.IGNORECASE):
        warns.append("禁止タグ（h4/script/style）が含まれています。")
    if re.search(r'<br\s*/?>', html, flags=re.IGNORECASE):
        warns.append("<br> タグは使用禁止です。すべて <p> に置き換えてください。")

    h2_iter = list(re.finditer(r'(<h2>.*?</h2>)', html, flags=re.DOTALL | re.IGNORECASE))
    for i, m in enumerate(h2_iter):
        start = m.end()
        end = h2_iter[i + 1].start() if i + 1 < len(h2_iter) else len(html)
        section = html[start:end]
        if not re.search(r'<(ul|ol|table)\b', section, flags=re.IGNORECASE):
            warns.append("H2セクションに表（table）または箇条書き（ul/ol）が不足しています。")

    h3_positions = list(re.finditer(r'(<h3>.*?</h3>)', html, flags=re.DOTALL | re.IGNORECASE))
    for i, m in enumerate(h3_positions):
        start = m.end()
        next_head = re.search(r'(<h2>|<h3>)', html[start:], flags=re.IGNORECASE)
        end = start + next_head.start() if next_head else len(html)
        block = html[start:end]
        p_count = len(re.findall(r'<p>.*?</p>', block, flags=re.DOTALL | re.IGNORECASE))
        if p_count < 3 or p_count > 6:
            warns.append("各<h3>直下は4〜5文（<p>）が目安です。分量を調整してください。")

    for p in re.findall(r'<p>(.*?)</p>', html, flags=re.DOTALL | re.IGNORECASE):
        text = re.sub(r'<.*?>', '', p)
        if len(text.strip()) > 55:
            warns.append("一文が55文字を超えています。短く区切ってください。")
            break

    plain = re.sub(r'<.*?>', '', html)
    if len(plain.strip()) > 6000:
        warns.append("記事全体が6000文字を超えています。要約・整理してください。")
    return warns

def count_h2(html: str) -> int:
    return len(H2_RE.findall(html or ""))

def trim_h2_max(structure_html: str, max_count: int) -> str:
    parts = H2_RE.split(structure_html)
    out: List[str] = []
    h2_seen = 0
    i = 0
    while i < len(parts):
        chunk = parts[i]
        if H2_RE.match(chunk or ""):
            h2_seen += 1
            if h2_seen <= max_count:
                out.append(chunk)
                if i + 1 < len(parts):
                    out.append(parts[i + 1])
            i += 2
            continue
        else:
            if h2_seen == 0:
                out.append(chunk)
            i += 1
    return "".join(out)

# ------------------------------
# Gemini 呼び出し
# ------------------------------
def call_gemini(prompt: str, temperature: float = 0.2) -> str:
    if not GEMINI_KEY:
        raise RuntimeError("Gemini APIキーが未設定です。Secrets に google.gemini_api_key_1 を追加してください。")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent?key={GEMINI_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": temperature}}
    r = requests.post(endpoint, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini エラー: {r.status_code} / {r.text[:500]}")
    j = r.json()
    return j["candidates"][0]["content"]["parts"][0]["text"]

# ------------------------------
# プロンプト群
# ------------------------------
def prompt_outline_123(keyword: str, extra: str, banned: List[str], min_h2: int, max_h2: int) -> str:
    banned_block = "\n".join([f"・{b}" for b in banned]) if banned else "（なし）"
    return f"""
# 役割
あなたは日本語SEOに強いWeb編集者。キーワードから「①読者像」「②ニーズ」「③構成(HTML)」を作る。④は不要。

# 入力
- キーワード: {keyword}
- 追加要素: {extra or "（指定なし）"}
- 禁止事項（絶対に含めない）:
{banned_block}

# 制約
- ①/②は150字程度で箇条書き
- ③は <h2>,<h3> のみ（<h1>禁止）
- H2は最低 {min_h2} 個、最大 {max_h2} 個
- 各<h2>の下に<h3>は必ず3つ以上
- H2直下で「この記事では〜」などの定型句は使わない（後工程で導入を付ける）

# 出力フォーマット（厳守）
① 読者像:
- ...

② ニーズ:
- ...

③ 構成（HTML）:
<h2>...</h2>
<h3>...</h3>
""".strip()

def prompt_fill_h2(keyword: str, existing_structure_html: str, need: int) -> str:
    return f"""
# 役割: SEO編集者
# 指示: 既存の構成（<h2>,<h3>）に不足があるため、追加のH2ブロックをちょうど {need} 個だけ作る。
# 厳守:
- 出力は追加分のみ。前後の説明や余計な文章は出さない
- 各ブロックは <h2>見出し</h2> の直後に <h3> を3つ以上
- すべて日本語。<h1>は禁止。<br>は禁止
- それぞれの<h2>に自然に「{keyword}」を含める

# 既存の構成（参考・重複は避ける）
{existing_structure_html}

# 出力（追加分のみ）
""".strip()

# ------------------------------
# タイトル/説明
# ------------------------------
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

def generate_permalink(keyword_or_title: str) -> str:
    import unicodedata
    import re as _re
    s = keyword_or_title.lower()
    subs = {
        '先払い買取': 'sakibarai-kaitori', '先払い': 'sakibarai', '買取': 'kaitori', '口コミ': 'kuchikomi',
        '評判': 'hyoban', '体験談': 'taiken', 'レビュー': 'review', '比較': 'hikaku', '査定': 'satei',
        'おすすめ': 'osusume', 'ランキング': 'ranking', '評価': 'hyoka', '申込': 'moushikomi', '方法': 'houhou',
        '流れ': 'nagare', '手順': 'tejun'
    }
    for jp, en in subs.items():
        s = s.replace(jp, en)
    s = unicodedata.normalize('NFKD', s)
    s = _re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    s = _re.sub(r'-{2,}', '-', s)
    if len(s) > 50:
        parts = s.split('-')
        s = '-'.join(parts[:5])
    return s or f"post-{int(datetime.now().timestamp())}"

# ------------------------------
# ポリシー（統合）管理
# ------------------------------
CACHE_PATH = Path("./policies_cache.json")
DEFAULT_PRESET_NAME = "default"

# 1区分（統合）で保持。ファイルもこの形式で保存・読込する
DEFAULT_POLICY_TXT = """[リード文]
# リード文の作成指示:
・読者の悩みや不安を共感的に表現すること（例：「〜でお困りではありませんか」）
・この記事を読むことで得られる具体的なメリットを2つ以上提示すること
・「実は」「なんと」などの興味を引く表現を使うこと
・最後に行動を促す一文を入れること（例：「ぜひ最後までお読みください」）

[本文指示]
# 本文の作成指示:
・プロンプト③で出力された <h2> と <h3> 構成を維持し、それぞれの直下に <p> タグで本文を記述
・各 <h2> の冒頭に「ここでは、〜について解説します」形式の導入段落を3行程度 <p> タグで挿入する
・各 <h3> の直下には4～5文程度（400文字程度）の詳細な解説を記述
・<h4>、<script>、<style> などは禁止
・一文は55文字以内に収めること
・一文ごとに独立した<p>タグで記述すること（<br>タグは絶対に使用禁止）
・一つの文章が終わるごとに改行すること
・必要に応じて<ul>、<ol>、<li>、<table>、<tr>、<th>、<td>タグを使用して分かりやすく情報を整理すること
・各H2セクションには必ず1つ以上の表（table）または箇条書き（ul/ol）を含めること
・手続きの比較、メリット・デメリット、専門家比較、費用比較などは必ず表形式で整理すること
・メリット・デメリット比較や専門家比較は必ず以下の形式で表を作成すること：
　<table><tr><th>項目</th><th>選択肢1</th><th>選択肢2</th></tr><tr><th>メリット</th><td>内容</td><td>内容</td></tr></table>
・表のHTMLタグ（table, tr, th, td）を正確に使用すること
・表形式が適している情報は必ず表で整理すること
・メリット・デメリットの比較は必ず表形式で作成すること
・【メリット】【デメリット】のような明確な区分を使用すること
・PREP法もしくはSDS法で書くこと
・横文字を使用しないこと
・冗長表現を使用しないこと
・「です」「ましょう」「ます」「ください」など、様々な語尾のバリエーションを使用してください
・記事全体は6000文字に収めること
・具体例や注意点、実際の手続き方法を豊富に含め、実践的で有益な情報を提供すること
・専門的でありながら分かりやすい解説を心がけること
・情報量を増やすため、各セクションで詳細な説明と複数の具体例を含めること

[まとめ文]
# まとめ文の作成指示:
・必ず最初に<h2>まとめ</h2>を出力すること
・一文ごとに独立した<p>タグで記述すること（<br>タグは絶対に使用禁止）
・記事の要点を箇条書きで2-3個簡潔にリストも用いて文中に挿入すること
・内容は300文字程度にすること
"""

SECTION_MARKERS = ("[リード文]", "[本文指示]", "[まとめ文]")

def extract_sections(policy_text: str) -> Tuple[str, str, str]:
    """統合ポリシーから [リード文]/[本文指示]/[まとめ文] を抽出（無ければ空文字）"""
    def _find(label: str) -> str:
        m = re.search(rf"\[{label}\](.*?)(?=\[[^\]]+\]|$)", policy_text, flags=re.DOTALL)
        return (m.group(1).strip() if m else "")
    # 後方互換：どのラベルも無い場合は、本文のみ扱い（リード/まとめはデフォルト）
    if not any(x in policy_text for x in SECTION_MARKERS):
        return "", policy_text.strip(), ""
    return _find("リード文"), _find("本文指示"), _find("まとめ文")

def prompt_full_article_unified(keyword: str,
                                unified_policy_text: str,
                                structure_html: str,
                                readers_txt: str,
                                needs_txt: str,
                                banned: List[str]) -> str:
    lead_pol, body_pol, summary_pol = extract_sections(unified_policy_text)
    # 後方互換：リード/まとめが空なら既定で補完
    if not lead_pol:
        lead_pol = """# リード文の作成指示:
・読者の悩みや不安を共感的に表現すること
・記事で得られる具体的メリットを2つ以上
・最後に行動を促す一文
"""
    if not summary_pol:
        summary_pol = """# まとめ文の作成指示:
・最初に<h2>まとめ</h2>
・要点を2-3個リストで挿入
・約300文字
"""
    lead_pol = lead_pol.replace("{keyword}", keyword)
    body_pol = body_pol.replace("{keyword}", keyword)
    summary_pol = summary_pol.replace("{keyword}", keyword)
    banned_block = "\n".join([f"・{b}" for b in banned]) if banned else "（なし）"
    return f"""
# 命令書:
あなたはSEOに特化した日本語のプロライターです。
以下の構成案と各ポリシーに従い、「{keyword}」の記事を
**リード文 → 本文 → まとめ**まで一気通貫でHTMLのみ出力してください。

# リード文ポリシー（厳守）
{lead_pol}

# 本文ポリシー（厳守）
{body_pol}

# まとめ文ポリシー（厳守）
{summary_pol}

# 禁止事項（絶対に含めない）
{banned_block}

# 記事の方向性（参考）
[読者像]
{readers_txt}

[ニーズ]
{needs_txt}

# 構成案（この<h2><h3>構成を厳密に守る）
{structure_html}

# 出力
（HTMLのみを出力）
""".strip()

# ------------------------------
# キャッシュ I/O（統合テキストをそのまま保存）
# ------------------------------
def load_policies_from_cache() -> Dict[str, Any] | None:
    try:
        if CACHE_PATH.exists():
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        st.warning(f"ポリシーキャッシュ読込エラー: {e}")
    return None

def save_policies_to_cache(store: Dict[str, str], active_name: str):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"policy_store": store, "active_policy": active_name}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.warning(f"ポリシーキャッシュ保存エラー: {e}")

# ------------------------------
# サイト選択 & 疎通
# ------------------------------
st.sidebar.header("接続先（WP）")
site_key = st.sidebar.selectbox("投稿先サイト", sorted(WP_CONFIGS.keys()))
cfg = WP_CONFIGS[site_key]
BASE = ensure_trailing_slash(cfg["url"])
AUTH = HTTPBasicAuth(cfg["user"], cfg["password"])

if st.sidebar.button("🔐 認証 /users/me"):
    r = wp_get(BASE, "wp/v2/users/me", AUTH, HEADERS)
    st.sidebar.code(f"GET users/me → {r.status_code if r else 'N/A'}")
    st.sidebar.caption((r.text[:300] if r is not None else "No response"))

# ------------------------------
# セッション初期化（統合版）
# ------------------------------
if "policy_store" not in st.session_state or not isinstance(st.session_state.policy_store, dict):
    st.session_state.policy_store = {DEFAULT_PRESET_NAME: DEFAULT_POLICY_TXT}

if "active_policy" not in st.session_state:
    st.session_state.active_policy = DEFAULT_PRESET_NAME

# キャッシュ読み込み
cached = load_policies_from_cache()
if cached:
    cache_store = cached.get("policy_store")
    if isinstance(cache_store, dict) and cache_store:
        st.session_state.policy_store = cache_store
    ap = cached.get("active_policy")
    if ap in st.session_state.policy_store:
        st.session_state.active_policy = ap

# default 補完
if DEFAULT_PRESET_NAME not in st.session_state.policy_store:
    st.session_state.policy_store[DEFAULT_PRESET_NAME] = DEFAULT_POLICY_TXT
    if st.session_state.active_policy not in st.session_state.policy_store:
        st.session_state.active_policy = DEFAULT_PRESET_NAME

# 編集用 state を1本化
cur_txt = st.session_state.policy_store[st.session_state.active_policy]
st.session_state.setdefault("policy_text", cur_txt)
st.session_state.setdefault("banned_text", "")

# ==============================
# 3カラム：入力 / 生成&プレビュー / 投稿
# ==============================
colL, colM, colR = st.columns([1.3, 1.6, 1.1])

# ------ 左：入力 / ポリシー管理(.txt) ------
with colL:
    st.header("1) 入力 & ポリシー管理（.txt）")

    keyword = st.text_input("必須キーワード", placeholder="例：先払い買取 口コミ")
    extra_points = st.text_area("特に加えてほしい内容（任意）", height=100)

    st.markdown("### 🚫 禁止事項（任意・1行=1項目）")
    banned_text = st.text_area("禁止ワード・禁止表現", value=st.session_state.get("banned_text", ""), height=120)
    st.session_state["banned_text"] = banned_text
    merged_banned = [l.strip() for l in banned_text.splitlines() if l.strip()]

    st.divider()
    st.subheader("④ 文章ポリシー")

    # .txt 読み込み（複数可 / 丸ごと保存）
    pol_files = st.file_uploader("policy*.txt（複数可）を読み込む", type=["txt"], accept_multiple_files=True)
    if pol_files:
        for f in pol_files:
            try:
                raw = f.read().decode("utf-8", errors="ignore").strip()
                name = f.name.rsplit(".", 1)[0]
                st.session_state.policy_store[name] = raw  # 丸ごと1区分として保存
                st.session_state.active_policy = name
                st.session_state.policy_text = raw
            except Exception as e:
                st.warning(f"{f.name}: 読み込み失敗 ({e})")
        save_policies_to_cache(st.session_state.policy_store, st.session_state.active_policy)

    # プリセット選択
    names = sorted(st.session_state.policy_store.keys())
    sel_index = names.index(st.session_state.active_policy) if st.session_state.active_policy in names else 0
    sel_name = st.selectbox("適用するポリシー", names, index=sel_index)
    if sel_name != st.session_state.active_policy:
        st.session_state.active_policy = sel_name
        st.session_state.policy_text = st.session_state.policy_store[sel_name]
        save_policies_to_cache(st.session_state.policy_store, st.session_state.active_policy)

    # 編集（1テキスト）
    st.markdown("### ✏️ ポリシー本文（そのまま編集可）")
    st.session_state.policy_text = st.text_area(
        "ポリシー本文（[リード文] / [本文指示] / [まとめ文] を含めて1ファイル）",
        value=st.session_state.get("policy_text", ""),
        height=420
    )

    cA, cB, cC, cD = st.columns([1, 1, 1, 1])
    with cA:
        if st.button("この内容で上書き保存"):
            st.session_state.policy_store[st.session_state.active_policy] = st.session_state.policy_text
            save_policies_to_cache(st.session_state.policy_store, st.session_state.active_policy)
            st.success(f"『{st.session_state.active_policy}』を更新しました。")
    with cB:
        st.download_button(
            "この内容を .txt で保存（統合）",
            data=st.session_state.get("policy_text", ""),
            file_name=f"{st.session_state.active_policy}.txt",
            mime="text/plain",
            use_container_width=True
        )
    with cC:
        can_delete = (
            st.session_state.active_policy != DEFAULT_PRESET_NAME and
            len(st.session_state.policy_store) > 1 and
            st.session_state.active_policy in st.session_state.policy_store
        )
        if can_delete and st.button("このプリセットを削除"):
            del st.session_state.policy_store[st.session_state.active_policy]
            fallback = DEFAULT_PRESET_NAME if DEFAULT_PRESET_NAME in st.session_state.policy_store else None
            if not fallback:
                st.session_state.policy_store[DEFAULT_PRESET_NAME] = DEFAULT_POLICY_TXT
                fallback = DEFAULT_PRESET_NAME
            st.session_state.active_policy = fallback
            st.session_state.policy_text = st.session_state.policy_store[fallback]
            save_policies_to_cache(st.session_state.policy_store, st.session_state.active_policy)
            st.warning("プリセットを削除しました。")
    with cD:
        if st.button("🔁 プリセットを初期状態に戻す"):
            st.session_state.policy_store = {DEFAULT_PRESET_NAME: DEFAULT_POLICY_TXT}
            st.session_state.active_policy = DEFAULT_PRESET_NAME
            st.session_state.policy_text = DEFAULT_POLICY_TXT
            save_policies_to_cache(st.session_state.policy_store, st.session_state.active_policy)
            st.success("初期状態にリセットしました。")

# ------ 中：生成 & プレビュー ------
with colM:
    st.header("2) 生成 & プレビュー")

    # H2最小/最大
    max_h2 = st.number_input("H2の最大数", min_value=3, max_value=12, value=MAX_H2, step=1)
    min_h2 = st.number_input("H2の最小数", min_value=1, max_value=12, value=3, step=1)
    if min_h2 > max_h2:
        st.warning("⚠️ H2の最小数が最大数を上回っています。最小≦最大 になるよう調整してください。")

    # ①〜③ 生成
    if st.button("①〜③（読者像/ニーズ/構成）を生成"):
        if not keyword.strip():
            st.error("キーワードは必須です。")
            st.stop()
        outline_raw = call_gemini(prompt_outline_123(keyword, extra_points, merged_banned, min_h2, max_h2))

        readers = re.search(r'①[^\n]*\n(.+?)\n\n②', outline_raw, flags=re.DOTALL)
        needs = re.search(r'②[^\n]*\n(.+?)\n\n③', outline_raw, flags=re.DOTALL)
        struct = re.search(r'③[^\n]*\n(.+)$', outline_raw, flags=re.DOTALL)

        st.session_state["readers"] = (readers.group(1).strip() if readers else "")
        st.session_state["needs"] = (needs.group(1).strip() if needs else "")
        structure_html = (struct.group(1).strip() if struct else "").replace("\r", "")
        structure_html = simplify_html(structure_html)

        # H2本数の調整（過多→カット、不足→追補→再カット保険）
        if count_h2(structure_html) > max_h2:
            structure_html = trim_h2_max(structure_html, max_h2)

        current_h2 = count_h2(structure_html)
        if current_h2 < min_h2:
            need = min_h2 - current_h2
            add = call_gemini(prompt_fill_h2(keyword, structure_html, need)).strip()
            add = simplify_html(add)
            if count_h2(add) > 0:
                structure_html = (structure_html.rstrip() + "\n\n" + add.strip())

        if count_h2(structure_html) > max_h2:
            structure_html = trim_h2_max(structure_html, max_h2)

        st.session_state["structure_html"] = structure_html

    # 手直し
    readers_txt = st.text_area("① 読者像（編集可）", value=st.session_state.get("readers", ""), height=110)
    needs_txt = st.text_area("② ニーズ（編集可）", value=st.session_state.get("needs", ""), height=110)
    structure_html = st.text_area("③ 構成（HTML / 編集可）", value=st.session_state.get("structure_html", ""), height=180)

    # 記事を一括生成（リード→本文→まとめ）
    if st.button("🪄 記事を一括生成（リード→本文→まとめ）", type="primary", use_container_width=True):
        if not keyword.strip():
            st.error("キーワードは必須です。")
            st.stop()
        if not structure_html.strip():
            st.error("③構成（HTML）が必要です。①〜③を生成し、必要なら編集してください。")
            st.stop()

        full = call_gemini(prompt_full_article_unified(
            keyword=keyword,
            unified_policy_text=st.session_state.policy_text,  # 統合テキスト
            structure_html=structure_html,
            readers_txt=readers_txt,
            needs_txt=needs_txt,
            banned=merged_banned
        ))
        full = simplify_html(full)
        st.session_state["assembled_html"] = full
        st.session_state["edited_html"] = full
        st.session_state["use_edited"] = True

    # プレビュー & 編集
    assembled = st.session_state.get("assembled_html", "")
    if assembled:
        st.markdown("#### 👀 プレビュー（一括生成結果）")
        st.write(assembled, unsafe_allow_html=True)
        issues = validate_article(assembled)
        if issues:
            st.warning("検査結果:\n- " + "\n- ".join(issues))

    with st.expander("✏️ プレビューを編集（この内容を下書きに送付）", expanded=False):
        st.caption("※ ここでの修正が最終本文になります。HTMLで編集可。")
        st.session_state["edited_html"] = st.text_area(
            "編集用HTML",
            value=st.session_state.get("edited_html", assembled),
            height=420
        )
        st.session_state["use_edited"] = st.checkbox("編集したHTMLを採用する", value=True)

# ------ 右：タイトル/説明 → 投稿 ------
with colR:
    st.header("3) タイトル/説明 → 投稿")

    content_dir = (st.session_state.get("readers", "") + "\n" +
                   st.session_state.get("needs", "") + "\n" +
                   (st.session_state.get("policy_text", "")))
    content_source = st.session_state.get("edited_html") or st.session_state.get("assembled_html", "")

    colT1, colT2 = st.columns([1, 1])
    with colT1:
        if st.button("SEOタイトル自動生成"):
            if not content_source.strip():
                st.warning("先に本文（編集後）を用意してください。")
            else:
                st.session_state["title"] = generate_seo_title(keyword, content_dir)
    with colT2:
        if st.button("メタディスクリプション自動生成"):
            t = st.session_state.get("title", "") or f"{keyword}に関するポイント"
            if not content_source.strip():
                st.warning("先に本文（編集後）を用意してください。")
            else:
                st.session_state["excerpt"] = generate_seo_description(keyword, content_dir, t)

    title = st.text_input("タイトル", value=st.session_state.get("title", ""))
    slug = st.text_input("スラッグ（空なら自動）", value="")
    excerpt = st.text_area("ディスクリプション（抜粋）", value=st.session_state.get("excerpt", ""), height=80)

    # ▼ カテゴリーUI
    def fetch_categories(base_url: str, auth: HTTPBasicAuth) -> List[Tuple[str, int]]:
        try:
            r = wp_get(base_url, "wp/v2/categories?per_page=100&_fields=id,name", auth, HEADERS)
            if r is not None and r.status_code == 200:
                data = r.json()
                pairs = [(c.get("name", "(no name)"), int(c.get("id"))) for c in data if c.get("id") is not None]
                return sorted(pairs, key=lambda x: x[0])
        except Exception:
            pass
        return []

    cfg_cats_map: Dict[str, int] = dict(cfg.get("categories", {}))
    cats: List[Tuple[str, int]] = []
    if cfg_cats_map:
        cats = sorted([(name, int(cid)) for name, cid in cfg_cats_map.items()], key=lambda x: x[0])
    else:
        sc_map: Dict[str, int] = st.secrets.get("wp_categories", {}).get(site_key, {})
        if sc_map:
            cats = sorted([(name, int(cid)) for name, cid in sc_map.items()], key=lambda x: x[0])
        else:
            cats = fetch_categories(BASE, AUTH)

    cat_labels = [name for (name, _cid) in cats]
    sel_labels: List[str] = st.multiselect("カテゴリー（複数可）", cat_labels, default=[])
    selected_cat_ids: List[int] = [cid for (name, cid) in cats if name in sel_labels]
    if not cats:
        st.info("このサイトで選べるカテゴリーが見つかりませんでした。Secretsの `wp_configs.<site_key>.categories` を確認してください。")

    # 公開状態（日本語ラベル → API値）
    status_options = {"下書き": "draft", "予約投稿": "future", "公開": "publish"}
    status_label = st.selectbox("公開状態", list(status_options.keys()), index=0)
    status = status_options[status_label]
    sched_date = st.date_input("予約日（future用）")
    sched_time = st.time_input("予約時刻（future用）", value=dt_time(9, 0))

    # 投稿
    if st.button("📝 WPに下書き/投稿する", type="primary", use_container_width=True):
        if not keyword.strip():
            st.error("キーワードは必須です。")
            st.stop()
        if not title.strip():
            st.error("タイトルは必須です。")
            st.stop()

        content_html = (st.session_state.get("edited_html") if st.session_state.get("use_edited")
                        else st.session_state.get("assembled_html", "")).strip()
        if not content_html:
            st.error("本文が未生成です。『①〜③生成→記事を一括生成』の順で作成してください。")
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
            "excerpt": excerpt.strip()
        }
        if date_gmt:
            payload["date_gmt"] = date_gmt
        if selected_cat_ids:
            payload["categories"] = selected_cat_ids

        r = wp_post(BASE, "wp/v2/posts", AUTH, HEADERS, json_payload=payload)
        if r is None or r.status_code not in (200, 201):
            st.error(f"投稿失敗: {r.status_code if r else 'N/A'}")
            if r is not None:
                st.code(r.text[:1000])
            st.stop()

        data = r.json()
        st.success(f"投稿成功！ID={data.get('id')} / status={data.get('status')}")
        st.write("URL:", data.get("link", ""))
        st.json({k: data.get(k) for k in ["id", "slug", "status", "date", "link"]})
