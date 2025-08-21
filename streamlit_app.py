# streamlit_app.py
# ------------------------------------------------------------
# Auto Poster for WordPress (REST API, ?rest_route= 優先)
# - 複数サイト切替（st.secrets[wp_configs]）
# - 接続テスト（未認証/認証）
# - 投稿作成：下書き / 即公開 / 予約投稿
# - 画像アップ & アイキャッチ設定
# ------------------------------------------------------------
import io
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import requests
from requests.auth import HTTPBasicAuth
import streamlit as st


# -------------------------
# UI 基本設定
# -------------------------
st.set_page_config(page_title="WP Auto Poster", page_icon="📝", layout="centered")
st.title("📝 WordPress Auto Poster — 完全版")


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
            json_payload: Optional[Dict[str, Any]] = None,
            files: Optional[Dict[str, Any]] = None) -> requests.Response:
    for url in api_url(base, route, True):
        if files is not None:
            h2 = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            r = requests.post(url, auth=auth, headers=h2, files=files, timeout=30)
        else:
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


# -------------------------
# 設定読み込み（secrets.toml）
# -------------------------
if "wp_configs" not in st.secrets:
    st.error("Secrets に [wp_configs] がありません。Settings → Secrets に登録してください。")
    st.stop()

WP_CONFIGS: Dict[str, Dict[str, str]] = st.secrets["wp_configs"]  # type: ignore

site_keys = sorted(WP_CONFIGS.keys())
site_key = st.sidebar.selectbox("投稿先サイト", site_keys, index=0)
cfg = WP_CONFIGS[site_key]

base_url = ensure_trailing_slash(cfg.get("url", ""))
user = cfg.get("user", "")
password = cfg.get("password", "")

AUTH = HTTPBasicAuth(user, password)


# -------------------------
# 疎通・診断
# -------------------------
st.header("1) 疎通チェック")

if st.button("🔍 未認証 /wp-json/"):
    r = requests.get(base_url + "wp-json/", headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    st.write("GET /wp-json/ →", r.status_code)
    st.text(r.text[:300])

if st.button("🔐 認証 /users/me"):
    r = wp_get(base_url, "wp/v2/users/me", AUTH, HEADERS)
    st.text(r.text[:500])
    if r.status_code == 200:
        st.success("認証OK！")
    elif r.status_code == 401:
        st.error("401 未ログイン：user_login名とアプリパスを確認してください。")
    elif r.status_code == 403:
        st.error("403 Forbidden：送信元IP/経路ブロック。国外IP制限をOFFに。")
    else:
        st.warning(f"{r.status_code}：本文を確認してください。")


# -------------------------
# 投稿フォーム
# -------------------------
st.header("2) 投稿作成")

with st.form("post_form"):
    title = st.text_input("タイトル", "")
    slug = st.text_input("スラッグ（任意）", "")
    excerpt = st.text_area("抜粋（任意）", "")
    content = st.text_area("本文（HTML可）", height=300, value="<p>本文テスト</p>")

    status = st.selectbox("公開状態", ["draft", "publish", "future"], index=0)
    schedule_date = st.date_input("予約日（future用）")
    schedule_time = st.time_input("予約時刻（future用）")

    eyecatch_file = st.file_uploader("アイキャッチ画像（任意）", type=["jpg", "jpeg", "png"])
    submitted = st.form_submit_button("記事を作成")

if submitted:
    # 予約投稿
    schedule_dt_local = None
    if status == "future":
        schedule_dt_local = datetime.combine(schedule_date, schedule_time)

    payload = build_payload(title, content, status, slug, excerpt, schedule_dt_local)
    r_post = wp_post(base_url, "wp/v2/posts", AUTH, HEADERS, json_payload=payload)

    if r_post.status_code not in (200, 201):
        st.error(f"投稿失敗: {r_post.status_code}")
        st.text(r_post.text[:500])
        st.stop()

    post = r_post.json()
    post_id = post.get("id")
    st.success(f"記事作成成功（ID: {post_id}）")
    st.write("記事URL:", post.get("link", ""))

    # アイキャッチ画像
    if eyecatch_file is not None:
        filename = getattr(eyecatch_file, "name", "eyecatch.jpg")
        img_bytes = eyecatch_file.read()
        files = {"file": (filename, io.BytesIO(img_bytes), "image/jpeg")}

        r_media = wp_post(base_url, "wp/v2/media", AUTH, HEADERS, files=files)
        if r_media.status_code in (200, 201):
            media_id = r_media.json().get("id")
            r_set = wp_put(base_url, f"wp/v2/posts/{post_id}", AUTH, HEADERS,
                           json_payload={"featured_media": media_id})
            if r_set.status_code in (200, 201):
                st.success("アイキャッチ設定に成功しました。")
