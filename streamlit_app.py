# streamlit_app.py
# ------------------------------------------------------------
# WordPress Auto Poster (Local JSON Profiles, No Supabase)
# - Secrets: WP接続情報のみ（複数サイト）
# - プロファイル: ローカルJSONをUIで読み込み/保存（エクスポート）
# - 記事: プロファイル＋禁止事項を使って本文を組み立て→WPに下書き投稿
# - 画像(アイキャッチ)は未搭載（手作成前提）
# ------------------------------------------------------------
import io
import json
from datetime import datetime, timezone, time as dt_time
from typing import Dict, Any, List, Optional

import requests
from requests.auth import HTTPBasicAuth
import streamlit as st

# -------------------------
# UI 基本設定
# -------------------------
st.set_page_config(page_title="WP Auto Poster (Local JSON)", page_icon="📝", layout="wide")
st.title("📝 WordPress Auto Poster — Local JSON 版（アイキャッチ無し）")

# -------------------------
# HTTP ヘッダ（UA 明示）
# -------------------------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (AutoWriter/Streamlit)",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# -------------------------
# ユーティリティ
# -------------------------
def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"

def api_url(base: str, route: str, prefer_rest_route: bool = True) -> List[str]:
    """route は 'wp/v2/...' で渡す。?rest_route= を優先"""
    base = ensure_trailing_slash(base)
    route = route.lstrip("/")
    candidates = [f"?rest_route=/{route}", f"wp-json/{route}"]
    return [base + p for p in (candidates if prefer_rest_route else reversed(candidates))]

def wp_get(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str]) -> requests.Response:
    for url in api_url(base, route, True):
        r = requests.get(url, auth=auth, headers=headers, timeout=20)
        st.write(f"GET {url} → {r.status_code}")
        if r.status_code == 200:
            return r
    return r

def wp_post(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str],
            json_payload: Optional[Dict[str, Any]] = None) -> requests.Response:
    for url in api_url(base, route, True):
        r = requests.post(url, auth=auth, headers=headers, json=json_payload, timeout=30)
        st.write(f"POST {url} → {r.status_code}")
        if r.status_code in (200, 201):
            return r
    return r

def wp_put(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str],
           json_payload: Optional[Dict[str, Any]] = None) -> requests.Response:
    for url in api_url(base, route, True):
        r = requests.put(url, auth=auth, headers=headers, json=json_payload, timeout=30)
        st.write(f"PUT {url} → {r.status_code}")
        if r.status_code in (200, 201):
            return r
    return r

def build_payload(title: str, content: str, status: str,
                  slug: str = "", excerpt: str = "",
                  schedule_dt: Optional[datetime] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"title": title, "content": content, "status": status}
    if slug.strip():
        payload["slug"] = slug.strip()
    if excerpt.strip():
        payload["excerpt"] = excerpt.strip()
    if status == "future" and schedule_dt:
        payload["date_gmt"] = schedule_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return payload

def auto_slug(s: str) -> str:
    import re, unicodedata
    # 日本語→ASCII近似 + 非英数ハイフン化 + 連続ハイフン整理 + 前後ハイフン除去
    norm = unicodedata.normalize("NFKD", s)
    ascii_s = norm.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9\-]+", "-", ascii_s).lower()
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:80] if len(slug) > 80 else slug

# -------------------------
# Secrets 読み込み（必須）
# -------------------------
if "wp_configs" not in st.secrets:
    st.error("Secrets に [wp_configs] がありません。Settings → Secrets に登録してください。")
    st.stop()

WP_CONFIGS: Dict[str, Dict[str, str]] = st.secrets["wp_configs"]  # type: ignore

# -------------------------
# セッション初期化（ローカルJSONの格納先）
# -------------------------
DEFAULT_PROFILE = {
    "profile_name": "デフォルト",
    "prompt_reader": "",
    "prompt_needs": "",
    "prompt_outline": "",
    "prompt_body": "",
    "lead_instruction": "※H2直下の導入文ルール: 『この記事では～』は使わず、見出しに自然に入る書き出しにする。",
    "summary_instruction": "※まとめルール: ポエム禁止。箇条書きで要点と注意点を端的に締める。",
    "default_options": {
        "require_table": False,         # 比較表必須など
        "ban_tags": ["script", "iframe", "h1"],  # 除外タグ
    },
    "default_banned_items": []
}

if "profiles" not in st.session_state:
    st.session_state.profiles: List[Dict[str, Any]] = [DEFAULT_PROFILE.copy()]
if "active_idx" not in st.session_state:
    st.session_state.active_idx = 0
if "articles_log" not in st.session_state:
    st.session_state.articles_log: List[Dict[str, Any]] = []  # 画面下部に表示

# -------------------------
# サイドバー：接続先＆疎通
# -------------------------
st.sidebar.header("接続先（WP）")
site_keys = sorted(WP_CONFIGS.keys())
site_key = st.sidebar.selectbox("投稿先サイト", site_keys, index=0)
cfg = WP_CONFIGS[site_key]
base_url = ensure_trailing_slash(cfg.get("url", ""))
AUTH = HTTPBasicAuth(cfg.get("user", ""), cfg.get("password", ""))

st.sidebar.divider()
st.sidebar.subheader("疎通チェック")
if st.sidebar.button("🔍 未認証 /wp-json/"):
    r = requests.get(base_url + "wp-json/", headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    st.sidebar.write(f"GET /wp-json/ → {r.status_code}")
    st.sidebar.caption(r.text[:200])

if st.sidebar.button("🔐 認証 /users/me"):
    r = wp_get(base_url, "wp/v2/users/me", AUTH, HEADERS)
    if r.status_code == 200:
        st.sidebar.success("認証OK")
    elif r.status_code == 401:
        st.sidebar.error("401 未ログイン：user_login名/アプリパス確認")
    elif r.status_code == 403:
        st.sidebar.error("403 Forbidden：国外IP/経路ブロック解除要")
    else:
        st.sidebar.warning(f"{r.status_code}")

# -------------------------
# カラム割り（左:プロファイル / 中:記事 / 右:投稿）
# -------------------------
colL, colM, colR = st.columns([1.1, 1.6, 1.0])

# ===== 左カラム：プロファイル（ローカルJSON） =====
with colL:
    st.header("1) プロファイル（ローカルJSON）")

    # 読み込み
    uploaded = st.file_uploader("プロファイルJSONを読み込み", type=["json"])
    if uploaded is not None:
        try:
            data = json.load(uploaded)
            # 互換: 単体 or 複数
            if isinstance(data, dict) and "profile_name" in data:
                st.session_state.profiles = [data]
            elif isinstance(data, list):
                st.session_state.profiles = data
            else:
                st.warning("JSON形式が不正です。配列 or 単一オブジェクトにしてください。")
            st.session_state.active_idx = 0
            st.success("読み込み完了")
        except Exception as e:
            st.error(f"JSON読み込みエラー: {e}")

    # 選択/追加/削除
    names = [p.get("profile_name", f"Profile {i+1}") for i, p in enumerate(st.session_state.profiles)]
    idx = st.selectbox("編集するプロファイル", list(range(len(names))), format_func=lambda i: names[i],
                       index=st.session_state.active_idx)
    st.session_state.active_idx = idx

    prof = st.session_state.profiles[idx]

    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("＋ プロファイルを追加"):
            st.session_state.profiles.append(DEFAULT_PROFILE.copy())
            st.session_state.active_idx = len(st.session_state.profiles) - 1
    with c2:
        if st.button("🗑️ このプロファイルを削除", disabled=(len(st.session_state.profiles) <= 1)):
            del st.session_state.profiles[idx]
            st.session_state.active_idx = 0
            st.stop()

    # 編集フォーム
    prof["profile_name"] = st.text_input("名称", prof.get("profile_name", ""))
    prof["prompt_reader"] = st.text_area("① 読者像", prof.get("prompt_reader", ""), height=100)
    prof["prompt_needs"] = st.text_area("② ニーズ", prof.get("prompt_needs", ""), height=100)
    prof["prompt_outline"] = st.text_area("③ 構成（見出し案 / HTML可）", prof.get("prompt_outline", ""), height=140)
    prof["prompt_body"] = st.text_area("④ 本文ポリシー（書き方）", prof.get("prompt_body", ""), height=120)
    prof["lead_instruction"] = st.text_area("リード指示", prof.get("lead_instruction", ""), height=80)
    prof["summary_instruction"] = st.text_area("まとめ指示", prof.get("summary_instruction", ""), height=80)

    st.markdown("**デフォルト設定（任意）**")
    dopt = prof.setdefault("default_options", {"require_table": False, "ban_tags": ["script", "iframe", "h1"]})
    dopt["require_table"] = st.checkbox("比較表を必須にする", value=dopt.get("require_table", False))
    ban_tags_str = ", ".join(dopt.get("ban_tags", []))
    ban_tags_str = st.text_input("禁止タグ（カンマ区切り）", value=ban_tags_str)
    dopt["ban_tags"] = [t.strip() for t in ban_tags_str.split(",") if t.strip()]

    def_banned = prof.setdefault("default_banned_items", [])
    def_banned_text = st.text_area("デフォルト禁止事項（1行＝1項目）", "\n".join(def_banned), height=100)
    prof["default_banned_items"] = [l.strip() for l in def_banned_text.splitlines() if l.strip()]

    # エクスポート（JSONダウンロード）
    st.download_button(
        "💾 このプロファイル群をJSONとして保存",
        data=json.dumps(st.session_state.profiles, ensure_ascii=False, indent=2),
        file_name="profiles.json",
        mime="application/json",
        use_container_width=True
    )

# ===== 中カラム：記事入力（禁止事項 合成） =====
with colM:
    st.header("2) 記事入力（リード/まとめ自動結合）")

    # キーワード & 禁止事項
    keyword = st.text_input("キーワード（任意）", "")
    banned_text = st.text_area("禁止事項（1行＝1項目 / ここに書くと今回の記事だけに適用）",
                               "", height=100)
    # 合体（プロファイルのデフォルト + 今回）
    merged_banned = prof.get("default_banned_items", []) + \
                    [l.strip() for l in banned_text.splitlines() if l.strip()]

    # タイトル / スラッグ / 抜粋
    title = st.text_input("タイトル", "")
    slug = st.text_input("スラッグ（空なら自動生成）", "")
    c3, c4 = st.columns([1, 1])
    with c3:
        if st.button("🔤 タイトルからスラッグ自動生成"):
            st.experimental_rerun() if not title else None
            slug = auto_slug(title)
            st.session_state["__slug_suggestion"] = slug
    if "__slug_suggestion" in st.session_state:
        if not slug:
            slug = st.session_state["__slug_suggestion"]

    excerpt = st.text_area("抜粋（メタディスクリプション）", "", height=80)

    # 構成と本文（リード/まとめ付き）
    st.caption("※ ③構成 と ④本文の“方針”は左カラムのプロファイルに記入。ここでは実際の本文HTMLを入力します。")
    lead = st.text_area("リード（プロファイルの指示を踏まえて手書き or 生成結果を貼付）", "", height=120)
    outline_html = st.text_area("構成（HTML可 / H2,H3…）", "", height=140)
    body_html = st.text_area("本文（HTML）", "", height=260)
    summary = st.text_area("まとめ（プロファイルの指示に沿って箇条書き等）", "", height=120)

    # 組み立て（プレビュー用）
    assembled = ""
    if st.button("👀 プレビュー生成"):
        parts = []
        if lead.strip():
            parts.append(lead.strip())
        if outline_html.strip():
            parts.append(outline_html.strip())
        if body_html.strip():
            parts.append(body_html.strip())
        if summary.strip():
            parts.append(summary.strip())

        assembled = "\n\n".join(parts)

        # 簡易検査：禁止タグ/禁止語
        ban_tags = prof.get("default_options", {}).get("ban_tags", [])
        issues = []
        for t in ban_tags:
            if f"<{t}" in assembled.lower() or f"</{t}>" in assembled.lower():
                issues.append(f"禁止タグ <{t}> を含んでいます。")
        for ng in merged_banned:
            if ng in assembled:
                issues.append(f"禁止語句を含む: {ng}")

        if issues:
            st.warning("検査結果:\n- " + "\n- ".join(issues))
        st.markdown("#### プレビュー")
        st.write(assembled, unsafe_allow_html=True)

# ===== 右カラム：投稿（下書き / 予約） =====
with colR:
    st.header("3) 投稿（WP下書き）")

    status = st.selectbox("公開状態", ["draft", "future", "publish"], index=0)
    sched_date = st.date_input("予約日（future用）")
    sched_time = st.time_input("予約時刻（future用）", value=dt_time(9, 0))

    st.caption("※ 今回は**アイキャッチ未搭載**（手作成）。")
    do_post = st.button("📝 下書き/投稿する", type="primary", use_container_width=True)

    if do_post:
        # 本文を合体
        parts = []
        if lead.strip():
            parts.append(lead.strip())
        if outline_html.strip():
            parts.append(outline_html.strip())
        if body_html.strip():
            parts.append(body_html.strip())
        if summary.strip():
            parts.append(summary.strip())
        content_html = "\n\n".join(parts).strip()

        # 最低限チェック
        if not title.strip():
            st.error("タイトルは必須です。")
            st.stop()
        if not content_html:
            st.error("本文が空です。")
            st.stop()

        # スラッグ自動
        slug_final = slug.strip() or auto_slug(title)

        # 予約
        schedule_dt_local = None
        if status == "future":
            schedule_dt_local = datetime.combine(sched_date, sched_time)

        payload = build_payload(
            title=title.strip(),
            content=content_html,
            status=status,
            slug=slug_final,
            excerpt=excerpt.strip(),
            schedule_dt=schedule_dt_local
        )

        # POST
        r_post = wp_post(base_url, "wp/v2/posts", AUTH, HEADERS, json_payload=payload)
        if r_post.status_code not in (200, 201):
            st.error(f"投稿失敗: {r_post.status_code}")
            st.text(r_post.text[:500])
            st.stop()

        post = r_post.json()
        post_id = post.get("id")
        post_link = post.get("link", "")
        st.success(f"投稿成功（ID: {post_id}）")
        st.write("記事URL:", post_link)

        # ログ
        st.session_state.articles_log.insert(0, {
            "title": title.strip(),
            "site": site_key,
            "status": status,
            "post_id": post_id,
            "link": post_link,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "keyword": keyword,
            "profile": prof.get("profile_name", ""),
            "banned_used": merged_banned,
        })

# ===== 下部：実行ログ =====
st.divider()
st.subheader("履歴（最新10件）")
if st.session_state.articles_log:
    import pandas as pd
    df = pd.DataFrame(st.session_state.articles_log[:10])
    st.dataframe(df, use_container_width=True)
    st.download_button(
        "🧾 履歴をJSONで保存",
        data=json.dumps(st.session_state.articles_log, ensure_ascii=False, indent=2),
        file_name="articles_log.json",
        mime="application/json"
    )
else:
    st.caption("まだ履歴はありません。")
