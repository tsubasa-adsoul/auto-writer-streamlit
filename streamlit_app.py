# streamlit_app.py
# ------------------------------------------------------------
# Auto Poster for WordPress (REST API, ?rest_route= 優先)
# - 複数サイト切替（st.secrets[wp_configs]）
# - 接続テスト（未認証/認証）
# - 投稿作成：下書き / 即公開 / 予約投稿
# - 画像アップ & アイキャッチ設定
# - Xserverの /wp-json/ が弾かれる環境でも ?rest_route= でフォールバック
# ------------------------------------------------------------
import io
import json
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

st.caption(
    "・/wp-json/ が403でも ?rest_route= で回避\n"
    "・Xserver などで App Password を使う前提（.htaccess で Authorization を PHP に渡す）"
)

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
    """
    WordPress REST に叩くURL候補を返す。
    route は 'wp/v2/users/me' のように 'wp/v2/...' で渡す。
    prefer_rest_route=True なら ?rest_route= を先に試す（WAF回避用）
    """
    base = ensure_trailing_slash(base)
    route = route.lstrip("/")
    candidates = [f"?rest_route=/{route}", f"wp-json/{route}"]
    return [base + p for p in (candidates if prefer_rest_route else reversed(candidates))]


def wp_get(base: str, route: str, auth: HTTPBasicAuth, headers: Dict[str, str], timeout: int = 20) -> requests.Response:
    last = None
    for url in api_url(base, route, True):
        r = requests.get(url, auth=auth, headers=headers, timeout=timeout)
        st.write(f"GET {url} → {r.status_code}")
        if r.status_code == 200:
            return r
        last = r
    return last


def wp_post(
    base: str,
    route: str,
    auth: HTTPBasicAuth,
    headers: Dict[str, str],
    json_payload: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> requests.Response:
    last = None
    for url in api_url(base, route, True):
        if files is not None:
            # multipart時は Content-Type をrequestsに任せるため除去
            h2 = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            r = requests.post(url, auth=auth, headers=h2, files=files, timeout=timeout)
        else:
            r = requests.post(url, auth=auth, headers=headers, json=json_payload, timeout=timeout)
        st.write(f"POST {url} → {r.status_code}")
        if r.status_code in (200, 201):
            return r
        last = r
    return last


def wp_put(
    base: str,
    route: str,
    auth: HTTPBasicAuth,
    headers: Dict[str, str],
    json_payload: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> requests.Response:
    last = None
    for url in api_url(base, route, True):
        r = requests.put(url, auth=auth, headers=headers, json=json_payload, timeout=timeout)
        st.write(f"PUT {url} → {r.status_code}")
        if r.status_code in (200, 201):
            return r
        last = r
    return last


def to_wp_utc_iso(dt_local: datetime) -> str:
    """
    予約投稿用に WP (UTC) 形式の ISO8601 を返す。
    st.date_input / st.time_input から組み立てたローカル時刻を UTC に変換。
    """
    # ユーザーのサーバ時刻は JST を想定（Asia/Tokyo）
    JST = timezone.utc.fromutc(datetime.utcnow()).tzinfo  # ダミー
    # Streamlitはtz naiveなので、そのまま "一旦JSTとして" 解釈→UTCへ
    jst_offset = 9  # +09:00 固定（要件どおり、利用者は日本前提）
    dt_jst = dt_local.replace(tzinfo=timezone.utc).astimezone(timezone.utc)  # 一旦UTC扱い
    # シンプルに 9時間引いてUTC化（実務上は zoneinfo を推奨）
    dt_utc = dt_local.replace(tzinfo=timezone.utc)  # ここでは既にUTC前提で書く
    # 実用上は、入力値を「JSTとして扱い → UTCに-9h」へ直す：
    dt_utc = (dt_local.replace(tzinfo=None) - st.timedelta(hours=jst_offset))  # type: ignore
    # しかし Streamlit には timedelta 直は無いので、簡略化：手計算
    # → 上の実装はやや複雑なので、ここは後段のシンプル版を採用
    return dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def build_payload(
    title: str,
    content: str,
    status: str,
    slug: str = "",
    excerpt: str = "",
    schedule_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"title": title, "content": content, "status": status}
    if slug.strip():
        payload["slug"] = slug.strip()
    if excerpt.strip():
        payload["excerpt"] = excerpt.strip()

    # 予約投稿
    if status == "future" and schedule_dt:
        # WordPressは date_gmt / date どちらかをUTCで渡すのが確実
        # ここでは UTC の date_gmt を付与
        payload["date_gmt"] = schedule_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return payload


# -------------------------
# 設定読み込み（secrets.toml）
# -------------------------
if "wp_configs" not in st.secrets:
    st.error("secrets.toml に [wp_configs] が見つかりません。")
    st.stop()

WP_CONFIGS: Dict[str, Dict[str, str]] = st.secrets["wp_configs"]  # type: ignore

site_keys = sorted(WP_CONFIGS.keys())
site_key = st.sidebar.selectbox("投稿先サイトを選択", site_keys, index=0)
cfg = WP_CONFIGS[site_key]

base_url = ensure_trailing_slash(cfg.get("url", ""))
user = cfg.get("user", "")
password = cfg.get("password", "")

st.sidebar.markdown("**現在のサイト**")
st.sidebar.code(
    f"url: {base_url}\nuser: {user}\npassword: {'*' * len(password) if password else ''}",
    language="bash",
)

if not (base_url and user and password):
    st.error("選択サイトの url / user / password が secrets.toml に設定されていません。")
    st.stop()

AUTH = HTTPBasicAuth(user, password)


# -------------------------
# 疎通・診断
# -------------------------
st.header("1) 疎通チェック / 診断")

col_a, col_b = st.columns(2)

with col_a:
    st.subheader("🔎 未認証GET（/wp-json/）")
    if st.button("未認証GETを実行"):
        try:
            r = requests.get(
                base_url + "wp-json/",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                },
                timeout=15,
            )
            st.write("GET /wp-json/ →", r.status_code)
            st.text(r.text[:300])
            if r.status_code == 200:
                st.success("200 OK：送信元IP/UAではブロックされていません。")
            elif r.status_code == 403:
                st.error("403 Forbidden（Xserver青ページ）：送信元IP/経路でブロックされています。国外IP制限/アクセス制限の見直しが必要です。")
            else:
                st.warning(f"{r.status_code}：本文の先頭を確認してください。")
        except Exception as e:
            st.exception(e)

with col_b:
    st.subheader("🔐 認証GET（/users/me）")
    if st.button("認証GETを実行"):
        # ?rest_route= 優先で 2経路を自動試行
        r = wp_get(base_url, "wp/v2/users/me", AUTH, HEADERS)
        st.text(r.text[:500])
        if r.status_code == 200:
            st.success("認証OK！この設定で投稿できます。")
        elif r.status_code == 401:
            st.error("401 未ログイン：user_login名とアプリケーションパスワードを再確認してください。")
        elif r.status_code == 403:
            st.error("403 Forbidden：送信元IP/経路ブロック。国外IP制限/アクセス制限を見直してください。")
        else:
            st.warning(f"{r.status_code}：レスポンス本文を確認。")


# -------------------------
# 投稿フォーム
# -------------------------
st.header("2) 投稿作成")

with st.form("post_form", clear_on_submit=False):
    title = st.text_input("タイトル", value="", placeholder="記事タイトル")
    slug = st.text_input("スラッグ（任意）", value="", placeholder="post-slug")
    excerpt = st.text_area("抜粋（任意）", value="", placeholder="検索結果用の短い要約")
    content = st.text_area("本文（HTML可）", value="", height=300, placeholder="<p>本文HTML or テキスト</p>")

    col1, col2 = st.columns(2)
    with col1:
        status = st.selectbox("公開状態", ["draft", "publish", "future"], index=0, help="draft=下書き / publish=即公開 / future=予約投稿")

    with col2:
        schedule_date = st.date_input("予約日（future選択時）")
        schedule_time = st.time_input("予約時刻（future選択時）")

    # 画像アップロード（任意）
    st.subheader("アイキャッチ画像（任意）")
    eyecatch_file = st.file_uploader("画像ファイルを選択（JPG/PNG）", type=["jpg", "jpeg", "png"])

    # 送信
    submitted = st.form_submit_button("記事を作成する")

if submitted:
    if not title.strip() or not content.strip():
        st.error("タイトルと本文は必須です。")
        st.stop()

    # 予約投稿の日時構築
    schedule_dt_local = None
    if status == "future":
        try:
            schedule_dt_local = datetime.combine(schedule_date, schedule_time)
        except Exception:
            st.error("予約日時の組み立てに失敗しました。日付と時刻を確認してください。")
            st.stop()

    payload = build_payload(title=title, content=content, status=status, slug=slug, excerpt=excerpt, schedule_dt=schedule_dt_local)

    # 1) 投稿作成
    st.info("投稿を作成中…")
    r_post = wp_post(base_url, "wp/v2/posts", AUTH, HEADERS, json_payload=payload)
    st.text(r_post.text[:500])

    if r_post.status_code not in (200, 201):
        st.error(f"投稿失敗: {r_post.status_code}")
        st.stop()

    post = r_post.json()
    post_id = post.get("id")
    st.success(f"記事作成成功（ID: {post_id}）")
    st.write("記事URL:", post.get("link", "（未取得）"))

    # 2) 画像アップ（任意）→ アイキャッチ設定
    if eyecatch_file is not None:
        try:
            filename = getattr(eyecatch_file, "name", "eyecatch.jpg")
            img_bytes = eyecatch_file.read()
            files = {"file": (filename, io.BytesIO(img_bytes), "image/jpeg")}

            st.info("画像をアップロード中…")
            r_media = wp_post(base_url, "wp/v2/media", AUTH, HEADERS, files=files)
            st.text(r_media.text[:400])

            if r_media.status_code not in (200, 201):
                st.warning(f"画像アップロードに失敗: {r_media.status_code}")
            else:
                media_id = r_media.json().get("id")
                st.success(f"画像アップロード成功（ID: {media_id}）")

                # アイキャッチ紐付け
                st.info("アイキャッチを設定中…")
                r_set = wp_put(base_url, f"wp/v2/posts/{post_id}", AUTH, HEADERS, json_payload={"featured_media": media_id})
                st.text(r_set.text[:300])
                if r_set.status_code in (200, 201):
                    st.success("アイキャッチ設定に成功しました。")
                else:
                    st.warning(f"アイキャッチ設定に失敗: {r_set.status_code}")
        except Exception as e:
            st.exception(e)

    st.balloons()
    st.success("完了しました。")


# -------------------------
# ヒント / トラブルシュート
# -------------------------
with st.expander("🛠 トラブルシュート（開く）", expanded=False):
    st.markdown(
        """
**403（青ページ）** → サーバ側のアクセス制限。`?rest_route=` が通るなら本アプリは動作可。  
**401** → user_login と App Password を再確認（メールアドレスではなく *ユーザー名*）。  

**Xserverの .htaccess 例（重要）**：
```apache
# Authorization を PHP に渡す
SetEnvIf Authorization "(.*)" HTTP_AUTHORIZATION=$1
<IfModule mod_rewrite.c>
RewriteEngine On
RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]
</IfModule>
