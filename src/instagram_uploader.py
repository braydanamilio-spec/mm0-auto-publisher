"""
instagram_uploader.py — Đăng Reels lên Instagram Business qua Graph API.

Cần:
  - ig_user_id  : ID tài khoản Instagram Business (lấy tự động khi kết nối FB — nằm ở Page).
  - page_token  : Page access token (IG dùng chung token của Page liên kết).
  - video_url   : LINK CÔNG KHAI tới video (IG tự tải từ link này — không upload bytes).

Quy trình 3 bước chuẩn của Meta:
  1) Tạo "container" (media) với video_url + caption.
  2) Chờ container xử lý xong (poll status_code = FINISHED).
  3) media_publish để đăng thật.

Lưu ý: Instagram BẮT BUỘC dùng URL công khai (không cho upload nhị phân trực tiếp).
Hệ tạo link Drive công khai tạm thời rồi thu hồi sau khi đăng (main.py lo phần đó).
"""

from __future__ import annotations
import time
import requests

GRAPH = "https://graph.facebook.com/v21.0"


def upload(video_url: str, meta: dict, ig_user_id: str, page_token: str,
           poll_seconds: int = 6, max_polls: int = 40) -> dict:
    caption = f"{meta['title']}\n\n{meta['description']}".strip()[:2200]

    # 1) Tạo container (mọi video coi là REELS)
    r = requests.post(f"{GRAPH}/{ig_user_id}/media", data={
        "media_type": "REELS", "video_url": video_url,
        "caption": caption, "access_token": page_token,
    }, timeout=120)
    r.raise_for_status()
    container_id = r.json().get("id")
    if not container_id:
        raise RuntimeError("IG: không tạo được container.")

    # 2) Chờ Instagram tải + xử lý video xong
    for _ in range(max_polls):
        st = requests.get(f"{GRAPH}/{container_id}", params={
            "fields": "status_code,status", "access_token": page_token,
        }, timeout=60).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError(f"IG xử lý video lỗi: {st.get('status')}")
        time.sleep(poll_seconds)
    else:
        raise RuntimeError("IG: quá thời gian chờ xử lý video.")

    # 3) Đăng thật
    pub = requests.post(f"{GRAPH}/{ig_user_id}/media_publish", data={
        "creation_id": container_id, "access_token": page_token,
    }, timeout=120)
    pub.raise_for_status()
    mid = pub.json().get("id")
    return {"id": mid, "url": f"https://www.instagram.com/reel/{mid}"}
