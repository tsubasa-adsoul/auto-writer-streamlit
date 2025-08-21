import streamlit as st
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime

st.set_page_config(page_title="Auto Writer (MVP)", page_icon="📝", layout="centered")
st.title("📝 Auto Writer — MVP")

# --- Secrets 読み込み ---
if "wp_configs" not in st.secrets:
    st.error("Secrets に [wp_configs] がありません。Settings → Secrets に登録してください。")
    st.stop()

wp_configs = {k: dict(v) for k, v in st.secrets["wp_configs"].items()}
site_keys = list(wp_configs.keys())
if not site_keys:
    st.error("wp_configs が空です。最低1サイトは登録してください。")
    st.stop()

# （任意）Geminiキーが入っているかだけ確認（使わないけど、配線チェック）
gemini_key = st.secrets.get("google", {}).get("gemini_api_key_1", None)

st.caption("Secrets 読み取りOK ✅" + ("" if gemini_key else "（Geminiキー未設定）"))

# --- サイト選択 ---
site_key = st.selectbox("投稿先サイト", site_keys)
cfg = wp_configs[site_key]
base_url = cfg["url"].rstrip("/") + "/"

st.write(f"**URL:** {base_url}")
st.write(f"**User:** {cfg['user']}")

# --- 接続テスト ---
st.subheader("1) 接続テスト")
if st.button("🔍 /wp-json/wp/v2/users/me で認証チェック"):
    try:
        r = requests.get(base_url + "wp-json/wp/v2/users/me",
                         auth=HTTPBasicAuth(cfg["user"], cfg["password"]),
                         timeout=20,
                         verify=True)
        if r.status_code == 200:
            me = r.json()
            st.success(f"認証OK！ID={me.get('id')}, 名称={me.get('name')}")
        else:
            st.error(f"NG: {r.status_code}\n{r.text[:500]}")
    except Exception as e:
        st.error(f"通信エラー: {e}")

st.divider()

# --- 下書き作成（LLMなし／手入力テスト） ---
st.subheader("2) 下書き作成テスト（LLMなし）")
title = st.text_input("タイトル", value=f"テスト投稿 {datetime.now():%Y-%m-%d %H:%M}")
slug = st.text_input("スラッグ（空なら自動）", value="")
excerpt = st.text_area("ディスクリプション（抜粋）", placeholder="空欄だと未設定のまま")
content = st.text_area("本文（HTML可）", height=200, value="<h2>はじめに</h2><p>これは接続確認用のテスト下書きです。</p>")
eyecatch_off = st.checkbox("アイキャッチは付けない（MVPでは常に未設定）", value=True)

if st.button("✍️ 下書きを作成する（draft）", type="primary"):
    payload = {
        "title": title,
        "content": content,
        "status": "draft",
    }
    if slug.strip():
        payload["slug"] = slug.strip()
    if excerpt.strip():
        payload["excerpt"] = excerpt.strip()

    try:
        r = requests.post(base_url + "wp-json/wp/v2/posts",
                          json=payload,
                          auth=HTTPBasicAuth(cfg["user"], cfg["password"]),
                          headers={"User-Agent": "streamlit-mvp"},
                          timeout=30,
                          verify=True)
        if r.status_code in (200, 201):
            data = r.json()
            st.success("下書き作成に成功！")
            st.write("URL:", data.get("link", "（未取得）"))
            st.json({k: data.get(k) for k in ["id", "slug", "status", "date", "link"]})
        else:
            st.error(f"投稿失敗: {r.status_code}\n{r.text[:800]}")
    except Exception as e:
        st.error(f"通信エラー: {e}")

st.divider()
st.caption("まずはこのMVPで『接続→下書き作成』が通るかを確認。通ったら段階プロンプト/禁止事項/リード・まとめ自動付与を足していきます。")

