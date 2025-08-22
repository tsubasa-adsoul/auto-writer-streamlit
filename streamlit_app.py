
# streamlit_app.py
# ------------------------------------------------------------
# WP Auto Writer — 一気通貫・完全版
# - ④本文ポリシーは .txt を読み込み・編集・保存（プリセット管理&削除・F5対策）
# - ①読者像 / ②ニーズ / ③構成 をAI生成（任意）
# - 記事（リード→本文→まとめ）は 1 回のリクエストで一括生成
# - 禁止事項は記事ごとに任意入力（アップロードなし）
# - Secrets から複数の WordPress 接続 [wp_configs] を読み込み、ドラフト保存
# - ?rest_route= 優先で WordPress REST と疎通
# ------------------------------------------------------------
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

WP_CONFIGS: Dict[str, Dict[str, str]] = st.secrets["wp_configs"]
GEMINI_KEY = st.secrets.get("google", {}).get("gemini_api_key_1")

if not GEMINI_KEY:
    st.warning("Gemini APIキー（google.gemini_api_key_1）が未設定です。生成機能は動作しません。")

# ------------------------------
# WP エンドポイント補助
# ------------------------------
def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"

def api_candidates(base: str, route: str) -> List[str]:
    base = ensure_trailing_slash(base); route = route.lstrip("/")
    return [f"{base}?rest_route=/{route}", f"{base}wp-json/{route}"]  # ?rest_route= 優先

def wp_get(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str]) -> requests.Response:
    last = None
    for url in api_candidates(base, route):
        r = requests.get(url, auth=auth, headers=headers, timeout=20)
        last = r
        if r.status_code == 200:
            return r
    return last

def wp_post(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str],
            json_payload: Dict[str, Any]) -> requests.Response:
    last = None
    for url in api_candidates(base, route):
        r = requests.post(url, auth=auth, headers=headers, json=json_payload, timeout=45)
        last = r
        if r.status_code in (200, 201):
            return r
    return last

HEADERS = {
    "User-Agent": "Mozilla/5.0 (AutoWriter/Streamlit)",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ------------------------------
# 生成ユーティリティ / バリデータ
# ------------------------------
ALLOWED_TAGS = ['h2','h3','p','strong','em','ul','ol','li','table','tr','th','td']  # <br>禁止
MAX_H2 = 8

def simplify_html(html: str) -> str:
    # 許可外タグは除去、<br>も禁止
    tags = re.findall(r'</?(\w+)[^>]*>', html)
    for tag in set(tags):
        if tag.lower() not in ALLOWED_TAGS:
            html = re.sub(rf'</?{tag}[^>]*>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<br\s*/?>', '', html, flags=re.IGNORECASE)
    return html

def limit_h2_count(html: str, max_count: int = MAX_H2) -> str:
    h2s = re.findall(r'(<h2>.*?</h2>)', html, flags=re.DOTALL | re.IGNORECASE)
    if len(h2s) <= max_count:
        return html
    return "".join(h2s[:max_count]) + "\n"

def generate_permalink(keyword_or_title: str) -> str:
    import unicodedata
    s = keyword_or_title.lower()
    subs = {
        '先払い買取':'sakibarai-kaitori','先払い':'sakibarai','買取':'kaitori','口コミ':'kuchikomi',
        '評判':'hyoban','体験談':'taiken','レビュー':'review','比較':'hikaku','査定':'satei',
        'おすすめ':'osusume','ランキング':'ranking','評価':'hyoka','申込':'moushikomi','方法':'houhou',
        '流れ':'nagare','手順':'tejun','審査':'shinsa','注意点':'chuiten'
    }
    for jp,en in subs.items():
        s = s.replace(jp,en)
    s = unicodedata.normalize('NFKD', s)
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    s = re.sub(r'-{2,}', '-', s)
    if len(s) > 50:
        parts = s.split('-')
        s = '-'.join(parts[:5])
    return s or f"post-{int(datetime.now().timestamp())}"

def validate_article(html: str) -> List[str]:
    warns: List[str] = []
    if re.search(r'<h4|<script|<style', html, flags=re.IGNORECASE):
        warns.append("禁止タグ（h4/script/style）が含まれています。")
    if re.search(r'<br\s*/?>', html, flags=re.IGNORECASE):
        warns.append("<br> タグは使用禁止です。<p>で区切ってください。")

    # H2ごとに表 or 箇条書きの有無を確認
    h2_iter = list(re.finditer(r'(<h2>.*?</h2>)', html, flags=re.DOTALL | re.IGNORECASE))
    for i, m in enumerate(h2_iter):
        start = m.end()
        end = h2_iter[i+1].start() if i+1 < len(h2_iter) else len(html)
        section = html[start:end]
        if not re.search(r'<(ul|ol|table)\b', section, flags=re.IGNORECASE):
            warns.append("H2セクションに表（table）または箇条書き（ul/ol）が不足しています。")

    # 一文55字以内（ざっくり）
    for p in re.findall(r'<p>(.*?)</p>', html, flags=re.DOTALL | re.IGNORECASE):
        text = re.sub(r'<.*?>', '', p).strip()
        if len(text) > 55:
            warns.append("一文が55字を超えています。短く区切ってください。")
            break
    return warns

# ------------------------------
# Gemini 呼び出し
# ------------------------------
def call_gemini(prompt: str, temperature: float = 0.2) -> str:
    if not GEMINI_KEY:
        raise RuntimeError("Gemini APIキーが未設定です。Secrets に google.gemini_api_key_1 を追加してください。")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent?key={GEMINI_KEY}"
    payload = {
        "contents":[{"parts":[{"text": prompt}]}],
        "generationConfig": {"temperature": temperature}
    }
    r = requests.post(endpoint, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini エラー: {r.status_code} / {r.text[:500]}")
    j = r.json()
    return j["candidates"][0]["content"]["parts"][0]["text"]

# ------------------------------
# 一括生成プロンプト
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
- 日本語で出力

# 出力フォーマット（厳守）
① 読者像:
- ...

② ニーズ:
- ...

③ 構成（HTML）:
<h2>...</h2>
<h3>...</h3>
""".strip()

def prompt_full_article(keyword: str, structure_html: str, policy_text: str, banned: List[str]) -> str:
    banned_block = "\n".join([f"・{b}" for b in banned]) if banned else "（なし）"
    return f"""
# 役割: SEOライター
# 任務: 構成（<h2>,<h3>）に沿って、リード→本文→まとめ まで一気通貫でHTMLを作成。
# 重要: 本文ポリシーおよび禁止事項を必ず順守。

# 本文ポリシー（厳守／一部抜粋例）
{policy_text}

# 禁止事項（絶対に書かない）
{banned_block}

# 入力
- キーワード: {keyword}
- 構成（HTML）:
{structure_html}

# 出力仕様（厳守）
- 最初に <h2>はじめに</h2> を置き、直後にリード文を <p> で複数行
- 各 <h2> セクション冒頭に短い導入の <p> を入れる
- 各 <h3> の直下には 4〜5文の <p> を並べる
- 末尾に <h2>まとめ</h2> を置き、要点を簡潔に整理（箇条書き2–3個も可）
- 許可タグ: {', '.join(ALLOWED_TAGS)} （<br>は使用禁止）
- <h1>/<h4>/<script>/<style> は禁止
- 日本語で出力
- 出力は本文HTMLのみ

# 出力:
""".strip()

def prompt_title(keyword: str, content_dir: str) -> str:
    return f"""
# 役割: SEO編集者
# 指示: 32文字以内・日本語・【】や｜禁止。自然にキーワードを含めクリックしたくなる1本だけ。
# 入力: キーワード={keyword} / 方向性={content_dir}
# 出力: タイトルのみ
""".strip()

def prompt_description(keyword: str, content_dir: str, title: str) -> str:
    return f"""
# 役割: SEO編集者
# 指示: 120字以内。定型「〜を解説/紹介」禁止。数字や具体メリットを入れてCTRを高める。
# 入力: キーワード={keyword} / タイトル={title} / 方向性={content_dir}
# 出力: 説明文のみ
""".strip()

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
            json.dump({"policy_store": policy_store, "active_policy": active_policy}, f,
                      ensure_ascii=False, indent=2)
    except Exception as e:
        st.warning(f"ポリシーキャッシュ保存エラー: {e}")

# ------------------------------
# サイドバー：接続先
# ------------------------------
st.sidebar.header("接続先（WP）")
site_key = st.sidebar.selectbox("投稿先サイト", sorted(WP_CONFIGS.keys()))
cfg = WP_CONFIGS[site_key]
BASE = ensure_trailing_slash(cfg["url"])
AUTH = HTTPBasicAuth(cfg["user"], cfg["password"])

if st.sidebar.button("🔐 認証テスト /users/me"):
    r = wp_get(BASE, "wp/v2/users/me", AUTH, HEADERS)
    st.sidebar.code(f"GET users/me → {r.status_code}")
    st.sidebar.caption(r.text[:300])

# ------------------------------
# セッション初期化
# ------------------------------
DEFAULT_POLICY_NAME = "default"
DEFAULT_POLICY_TXT = (
    "・プロンプト③で出力された <h2> と <h3> 構成を維持し、それぞれの直下に <p> タグで本文を記述\n"
    "・最初に<h2>はじめに</h2>を置き、共感→メリット提示→行動喚起を<p>で複数行\n"
    "・各 <h2> の冒頭に短い導入<p>を入れる\n"
    "・各 <h3> の直下には4～5文（400文字程度）の<p>を並べる\n"
    "・最後に<h2>まとめ</h2>を置き、要点の箇条書き(2–3)を含めつつ簡潔に総括\n"
    "・<h4>、<script>、<style>、<br> は禁止\n"
    "・一文は55文字以内。1文1<p>\n"
    "・必要に応じて<ul>/<ol>/<table>で比較や要点を整理\n"
    "・各H2セクションには表（table）または箇条書き（ul/ol）を1つ以上含める\n"
    "・事実関係が曖昧な場合は「不明/公式未記載」と明示\n"
    "・PREP法/SDS法を適宜使い、冗長表現を避け、横文字は使用しない"
)

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
ss.setdefault("title", "")
ss.setdefault("excerpt", "")

# F5対策：キャッシュ読込（あれば上書き）
cached = load_policies_from_cache()
if cached:
    if isinstance(cached.get("policy_store"), dict) and cached["policy_store"]:
        ss["policy_store"] = cached["policy_store"]
    ap = cached.get("active_policy")
    if ap in ss["policy_store"]:
        ss["active_policy"] = ap
        ss["policy_text"] = ss["policy_store"][ap]

# ==============================
# 3カラム：入力 / 生成&プレビュー / 投稿
# ==============================
colL, colM, colR = st.columns([1.25, 1.6, 1.0])

# ------ 左：入力 / ポリシー管理(.txt) ------
with colL:
    st.header("1) 入力 & ポリシー管理（.txt）")

    keyword = st.text_input("必須キーワード", placeholder="例：PAIDY 審査")
    extra_points = st.text_area("特に加えてほしい内容（任意）", height=96)

    st.markdown("### 🚫 禁止事項（任意・1行=1項目）")
    ss["banned_text"] = st.text_area("禁止ワード・禁止表現", value=ss["banned_text"], height=120)
    banned_list = [l.strip() for l in ss["banned_text"].splitlines() if l.strip()]

    st.divider()
    st.subheader("④ 本文ポリシー（.txt 読み込み→選択→編集→保存）")

    pol_files = st.file_uploader("policy*.txt（複数可）を読み込む", type=["txt"], accept_multiple_files=True)
    if pol_files:
        for f in pol_files:
            try:
                txt = f.read().decode("utf-8", errors="ignore").strip()
                name = f.name.rsplit(".", 1)[0]
                ss["policy_store"][name] = txt
                ss["active_policy"] = name
                ss["policy_text"] = txt
            except Exception as e:
                st.warning(f"{f.name}: 読み込み失敗 ({e})")
        save_policies_to_cache(ss["policy_store"], ss["active_policy"])

    names = sorted(ss["policy_store"].keys())
    sel = st.selectbox("適用するポリシー", names,
                       index=names.index(ss["active_policy"]) if ss["active_policy"] in names else 0)
    if sel != ss["active_policy"]:
        ss["active_policy"] = sel
        ss["policy_text"] = ss["policy_store"][sel]
        save_policies_to_cache(ss["policy_store"], ss["active_policy"])

    st.markdown("### ✏️ 本文ポリシー（編集可）")
    ss["policy_text"] = st.text_area(
        "ここをそのまま使う or 必要なら書き換え",
        value=ss["policy_text"],
        height=220
    )

    cA, cB, cC = st.columns([1,1,1])
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
            use_container_width=True
        )
    with cC:
        if ss["active_policy"] != DEFAULT_POLICY_NAME:
            if st.button("このプリセットを削除"):
                try:
                    del ss["policy_store"][ss["active_policy"]]
                except KeyError:
                    pass
                # デフォルトにフェイルセーフ復帰
                ss["active_policy"] = DEFAULT_POLICY_NAME
                if DEFAULT_POLICY_NAME not in ss["policy_store"]:
                    ss["policy_store"][DEFAULT_POLICY_NAME] = DEFAULT_POLICY_TXT
                ss["policy_text"] = ss["policy_store"][DEFAULT_POLICY_NAME]
                save_policies_to_cache(ss["policy_store"], ss["active_policy"])
                st.warning("プリセットを削除しました。")

# ------ 中：生成 & プレビュー ------
with colM:
    st.header("2) 生成 & プレビュー（記事は一括生成）")
    max_h2 = st.number_input("H2の最大数（③構成）", min_value=3, max_value=12, value=MAX_H2, step=1)

    gen123 = st.button("①〜③（読者像/ニーズ/構成）を生成")
    if gen123:
        if not keyword.strip():
            st.error("キーワードは必須です。"); st.stop()
        outline_raw = call_gemini(prompt_outline_123(keyword, extra_points, banned_list, max_h2))
        readers = re.search(r'①[^\n]*\n(.+?)\n\n②', outline_raw, flags=re.DOTALL)
        needs   = re.search(r'②[^\n]*\n(.+?)\n\n③', outline_raw, flags=re.DOTALL)
        struct  = re.search(r'③[^\n]*\n(.+)$',       outline_raw, flags=re.DOTALL)

        ss["readers"] = (readers.group(1).strip() if readers else "")
        ss["needs"]   = (needs.group(1).strip()   if needs   else "")
        structure_html = (struct.group(1).strip() if struct else "").replace("\r","")
        structure_html = simplify_html(structure_html)
        structure_html = limit_h2_count(structure_html, max_h2)
        ss["structure_html"] = structure_html

    readers_txt   = st.text_area("① 読者像（編集可）", value=ss["readers"], height=110)
    needs_txt     = st.text_area("② ニーズ（編集可）",   value=ss["needs"],   height=110)
    structure_html= st.text_area("③ 構成（HTML / 編集可）", value=ss["structure_html"], height=200)
    ss["readers"], ss["needs"], ss["structure_html"] = readers_txt, needs_txt, structure_html

    # 一括生成
    if st.button("🧠 記事を一括生成（リード→本文→まとめ）", type="primary", use_container_width=True):
        if not keyword.strip():
            st.error("キーワードは必須です。"); st.stop()
        if not ss["structure_html"].strip():
            st.error("先に ③構成 を用意してください。生成 or 手入力でもOKです。"); st.stop()

        full_html = call_gemini(
            prompt_full_article(
                keyword=keyword,
                structure_html=ss["structure_html"],
                policy_text=ss["policy_text"],
                banned=banned_list
            ),
            temperature=0.3
        )
        full_html = simplify_html(full_html)
        full_html = limit_h2_count(full_html, max_h2)
        ss["assembled_html"] = full_html
        ss["edited_html"] = full_html  # 初期値としてコピー

    if ss["assembled_html"]:
        st.markdown("#### 👀 プレビュー")
        st.write(ss["assembled_html"], unsafe_allow_html=True)
        issues = validate_article(ss["assembled_html"])
        if issues:
            st.warning("検査結果:\n- " + "\n- ".join(issues))

    with st.expander("✏️ プレビューを編集（この内容を下書きに送付）", expanded=False):
        st.caption("※ ここでの修正が最終本文になります。HTMLで編集可。")
        ss["edited_html"] = st.text_area("編集用HTML", value=ss["edited_html"], height=420)

# ------ 右：タイトル/説明 → 投稿 ------
with colR:
    st.header("3) タイトル/説明 → 投稿")

    content_dir = (ss["readers"] + "\n" + ss["needs"] + "\n" + ss["policy_text"]).strip()
    content_source = ss.get("edited_html","").strip()

    colT1, colT2 = st.columns([1,1])
    with colT1:
        if st.button("SEOタイトル自動生成"):
            if not content_source:
                st.warning("先に本文（編集後）を用意してください。")
            else:
                t = call_gemini(prompt_title(keyword, content_dir)).strip()
                t = re.sub(r'[【】｜\n\r]', '', t)[:32]
                ss["title"] = t
    with colT2:
        if st.button("メタディスクリプション自動生成"):
            if not content_source:
                st.warning("先に本文（編集後）を用意してください。")
            else:
                t = ss.get("title","") or f"{keyword}に関するポイント"
                d = call_gemini(prompt_description(keyword, content_dir, t)).strip()
                ss["excerpt"] = re.sub(r'[\n\r]', '', d)[:120]

    title = st.text_input("タイトル", value=ss.get("title",""))
    slug  = st.text_input("スラッグ（空なら自動）", value="")
    excerpt = st.text_area("ディスクリプション（抜粋）", value=ss.get("excerpt",""), height=88)

    status = st.selectbox("公開状態", ["draft","future","publish"], index=0)
    sched_date = st.date_input("予約日（future用）")
    sched_time = st.time_input("予約時刻（future用）", value=dt_time(9,0))

    if st.button("📝 WPに下書き/投稿する", type="primary", use_container_width=True):
        if not keyword.strip():
            st.error("キーワードは必須です。"); st.stop()
        if not title.strip():
            st.error("タイトルは必須です。"); st.stop()
        content_html = ss.get("edited_html","").strip()
        if not content_html:
            st.error("本文が未生成です。『記事を一括生成』を実行し、必要なら編集してください。"); st.stop()

        content_html = simplify_html(content_html)

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
        if r is None or r.status_code not in (200,201):
            st.error(f"投稿失敗: {getattr(r,'status_code', 'no-response')}")
            st.code(getattr(r,'text', '')[:1000])
            st.stop()
        data = r.json()
        st.success(f"投稿成功！ID={data.get('id')} / status={data.get('status')}")
        st.write("URL:", data.get("link",""))
        st.json({k: data.get(k) for k in ["id","slug","status","date","link"]})
