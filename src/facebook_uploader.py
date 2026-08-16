"""
facebook_uploader.py — Đăng video lên Facebook Page bằng Graph API.

Cần:
  - PAGE_ID của Page.
  - PAGE ACCESS TOKEN (long-lived, không hết hạn nếu lấy đúng cách — xem SETUP.md).

Hai loại:
  - Video dài  -> POST /{page_id}/videos       (multipart, gọn nhẹ)
  - Reels(short)-> Reels API 3 pha (start -> upload -> finish)

Lưu ý: token Page long-lived KHÔNG hết hạn khi lấy từ System User (Business),
hoặc hết ~60 ngày nếu lấy từ user token — SETUP.md hướng dẫn cách lấy loại bền.
"""

from __future__ import annotations
import os
import time
import requests

GRAPH = "https://graph.facebook.com/v21.0"


def post_comment(object_id: str, message: str, page_token: str) -> str | None:
    """Đăng 1 BÌNH LUẬN (chứa link tiếp thị) lên video/bài — dùng cho Reels vì caption không bấm link được.
    An toàn: lỗi -> trả None (không chặn luồng đăng)."""
    try:
        r = requests.post(f"{GRAPH}/{object_id}/comments",
                          data={"message": message, "access_token": page_token}, timeout=60)
        r.raise_for_status()
        return r.json().get("id")
    except Exception as e:
        print(f"     ⚠️ FB post_comment lỗi: {e}")
        return None


def wait_video_ready(video_id: str, page_token: str,
                     poll_seconds: int = 15, max_polls: int = 80) -> bool:
    """Chờ FB KÉO + XỬ LÝ xong video (khi đăng bằng file_url, FB tải ngầm).
    Trả True nếu 'ready'. Dùng để KHÔNG thu hồi link Drive trước khi FB kéo xong
    (video 2-5GB có thể mất nhiều phút). max_polls*poll_seconds ~ 20 phút.
    An toàn: lỗi mạng khi poll -> bỏ qua vòng đó, thử lại; hết giờ -> trả False."""
    for _ in range(max_polls):
        try:
            st = requests.get(f"{GRAPH}/{video_id}",
                              params={"fields": "status", "access_token": page_token},
                              timeout=60).json()
            vs = ((st.get("status") or {}).get("video_status")) or ""
            if vs == "ready":
                return True
            if vs == "error":
                return False
        except Exception:
            pass
        time.sleep(poll_seconds)
    return False


def upload_video(file_path: str, meta: dict, page_id: str, page_token: str,
                 video_url: str | None = None) -> dict:
    """Đăng video thường lên Page.
    - video_url có sẵn -> FB TỰ KÉO từ link (không tải-lại qua cron -> tối ưu cho video 2-5GB).
    - không -> upload bytes (stream, không nạp hết vào RAM)."""
    desc = f"{meta['title']}\n\n{meta['description']}"
    common = {"title": meta["title"][:255], "description": desc, "access_token": page_token}
    if video_url:
        r = requests.post(f"{GRAPH}/{page_id}/videos", data={**common, "file_url": video_url}, timeout=600)
    else:
        with open(file_path, "rb") as f:
            r = requests.post(f"{GRAPH}/{page_id}/videos", data=common, files={"source": f}, timeout=1800)
    r.raise_for_status()
    data = r.json()
    return {"id": data.get("id"), "url": f"https://facebook.com/{data.get('id')}"}


def upload_reel(file_path: str | None, meta: dict, page_id: str, page_token: str,
                video_url: str | None = None) -> dict:
    """
    Đăng Reels (video dọc/short) theo quy trình 3 pha của Graph API.
    - video_url có sẵn -> PHA 2 gửi header `file_url` để FB tự kéo (không tải-lại bytes).
    - không -> upload nhị phân từ file_path.
    """
    # PHA 1: start -> lấy video_id + upload_url
    start = requests.post(
        f"{GRAPH}/{page_id}/video_reels",
        data={"upload_phase": "start", "access_token": page_token},
        timeout=120,
    )
    start.raise_for_status()
    s = start.json()
    video_id = s["video_id"]
    upload_url = s["upload_url"]

    # PHA 2: upload — ưu tiên hosted file_url (FB tự kéo), fallback upload nhị phân
    if video_url:
        up = requests.post(
            upload_url,
            headers={"Authorization": f"OAuth {page_token}", "file_url": video_url},
            timeout=300,
        )
    else:
        size = os.path.getsize(file_path)
        with open(file_path, "rb") as f:
            up = requests.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {page_token}",
                    "offset": "0",
                    "file_size": str(size),
                },
                data=f,
                timeout=1800,
            )
    up.raise_for_status()

    # PHA 3: finish + publish
    desc = f"{meta['title']}\n\n{meta['description']}"
    finish = requests.post(
        f"{GRAPH}/{page_id}/video_reels",
        params={
            "access_token": page_token,
            "video_id": video_id,
            "upload_phase": "finish",
            "video_state": "PUBLISHED",
            "description": desc[:2200],
        },
        timeout=120,
    )
    finish.raise_for_status()
    return {"id": video_id, "url": f"https://facebook.com/reel/{video_id}"}


def upload(file_path: str | None, meta: dict, page_id: str, page_token: str,
           video_url: str | None = None) -> dict:
    # Short -> ưu tiên Reels (đúng định dạng, phân phối tốt hơn); long -> video Page thường.
    if meta.get("type") == "short":
        try:
            return upload_reel(file_path, meta, page_id, page_token, video_url=video_url)
        except Exception as e:
            # An toàn: nếu Reels API lỗi (vd không hỗ trợ hosted url) -> quay về video thường (vẫn đăng được).
            print(f"     ⚠️ Reels lỗi ({e}) -> đăng dạng video thường.")
            return upload_video(file_path, meta, page_id, page_token, video_url=video_url)
    return upload_video(file_path, meta, page_id, page_token, video_url=video_url)
