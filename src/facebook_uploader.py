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
import requests

GRAPH = "https://graph.facebook.com/v21.0"


def upload_video(file_path: str, meta: dict, page_id: str, page_token: str) -> dict:
    """Đăng video thường lên Page. Trả về {"id": ...}."""
    desc = f"{meta['title']}\n\n{meta['description']}"
    with open(file_path, "rb") as f:
        r = requests.post(
            f"{GRAPH}/{page_id}/videos",
            data={"title": meta["title"][:255], "description": desc, "access_token": page_token},
            files={"source": f},
            timeout=1800,
        )
    r.raise_for_status()
    data = r.json()
    return {"id": data.get("id"), "url": f"https://facebook.com/{data.get('id')}"}


def upload_reel(file_path: str, meta: dict, page_id: str, page_token: str) -> dict:
    """
    Đăng Reels (video dọc/short) theo quy trình 3 pha của Graph API.
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

    # PHA 2: upload nhị phân
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


def upload(file_path: str, meta: dict, page_id: str, page_token: str) -> dict:
    if meta.get("type") == "short":
        return upload_reel(file_path, meta, page_id, page_token)
    return upload_video(file_path, meta, page_id, page_token)
