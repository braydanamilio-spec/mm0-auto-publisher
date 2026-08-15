"""
youtube_uploader.py — Upload video lên YouTube bằng OAuth refresh token của TỪNG kênh.

Vì sao OAuth (không phải service account)?
  - Chỉ chủ kênh mới upload được. Service account KHÔNG sở hữu kênh YouTube.
  - Bạn chạy auth_setup.py 1 LẦN cho mỗi kênh -> lấy refresh_token -> lưu Secret.
    Từ đó hệ thống tự làm mới access token, đăng mãi mãi không cần đăng nhập lại.

Chi phí quota: mỗi lần upload ~1.600 đơn vị. Mặc định 10.000/ngày => ~6 video/ngày/kênh.
"""

from __future__ import annotations
import os

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube",
          "https://www.googleapis.com/auth/youtube.force-ssl"]  # force-ssl cần cho captions


class QuotaExceeded(Exception):
    """Hết quota YouTube ngày hôm nay — KHÔNG phải lỗi của video.
    Bắt riêng để giữ video ở trạng thái pending (thử lại ngày mai), không đánh failed."""


def _client(client_id: str, client_secret: str, refresh_token: str):
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(
    file_path: str,
    meta: dict,
    yt_conf: dict,
    creds_env: dict,
    publish_at_iso: str | None = None,
    thumbnail_path: str | None = None,
    captions: list[dict] | None = None,
    playlist: str | None = None,
) -> dict:
    """
    Upload 1 video. Trả về {"id": <videoId>, "url": ...} hoặc raise.
    - meta: từ metadata.build_metadata()
    - yt_conf: block youtube trong channels.yaml (category_id, privacy, ...)
    - creds_env: {"client_id":..., "client_secret":..., "refresh_token":...}
    - publish_at_iso: nếu privacy=private + có giá trị -> lên lịch công khai (native scheduling)
    - thumbnail_path: nếu có -> đặt thumbnail tùy chỉnh sau khi upload.
    """
    svc = _client(creds_env["client_id"], creds_env["client_secret"], creds_env["refresh_token"])

    status = {
        "privacyStatus": yt_conf.get("privacy", "public"),
        "selfDeclaredMadeForKids": bool(yt_conf.get("made_for_kids", False)),
    }
    # Lên lịch công khai bằng chính YouTube (an toàn nhất): privacy=private + publishAt
    if publish_at_iso and yt_conf.get("use_native_schedule"):
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at_iso

    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta.get("tags", []),
            "categoryId": str(yt_conf.get("category_id", "27")),
            "defaultLanguage": yt_conf.get("default_language", "en"),
            "defaultAudioLanguage": yt_conf.get("default_language", "en"),  # giúp đề xuất đúng US
        },
        "status": status,
    }

    media = MediaFileUpload(file_path, chunksize=1024 * 1024 * 8, resumable=True)
    request = svc.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    retries = 0
    while response is None:
        try:
            _status, response = request.next_chunk()
        except HttpError as e:
            # 5xx -> thử lại
            if e.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                continue
            # hết quota -> ném QuotaExceeded (giữ video pending, thử lại ngày mai)
            body = (getattr(e, "content", b"") or b"").decode("utf-8", "ignore").lower()
            if e.resp.status == 403 and ("quota" in body or "dailylimit" in body):
                raise QuotaExceeded(str(e))
            raise
    vid = response["id"]

    # Đặt thumbnail tùy chỉnh (long-form nên có; short thường bỏ qua)
    if thumbnail_path:
        try:
            svc.thumbnails().set(
                videoId=vid, media_body=MediaFileUpload(thumbnail_path)
            ).execute()
        except HttpError as e:
            # kênh chưa xác minh có thể chưa được đặt thumbnail tùy chỉnh -> không chặn
            print(f"     ⚠️  Không đặt được thumbnail: {e}")

    # Upload phụ đề (captions) — mỗi ngôn ngữ 1 track
    for cap in captions or []:
        try:
            svc.captions().insert(
                part="snippet",
                body={"snippet": {
                    "videoId": vid,
                    "language": cap.get("language", "en"),
                    "name": cap.get("name", ""),
                    "isDraft": False,
                }},
                media_body=MediaFileUpload(cap["path"]),
            ).execute()
            print(f"     ✅ Phụ đề [{cap.get('language','en')}] đã lên.")
        except HttpError as e:
            print(f"     ⚠️  Không upload được phụ đề: {e}")

    # Thêm vào playlist (tự tạo nếu chưa có)
    if playlist:
        try:
            pid = _ensure_playlist(svc, playlist, yt_conf.get("privacy", "public"))
            svc.playlistItems().insert(part="snippet", body={"snippet": {
                "playlistId": pid,
                "resourceId": {"kind": "youtube#video", "videoId": vid},
            }}).execute()
            print(f"     ✅ Đã thêm vào playlist: {playlist!r}")
        except HttpError as e:
            print(f"     ⚠️  Không thêm được playlist: {e}")

    return {"id": vid, "url": f"https://youtu.be/{vid}"}


def _ensure_playlist(svc, title: str, privacy: str = "public") -> str:
    """Tìm playlist theo tên (của kênh) hoặc tạo mới. Trả playlistId."""
    res = svc.playlists().list(part="snippet", mine=True, maxResults=50).execute()
    for it in res.get("items", []):
        if it["snippet"]["title"].strip().lower() == title.strip().lower():
            return it["id"]
    created = svc.playlists().insert(
        part="snippet,status",
        body={"snippet": {"title": title}, "status": {"privacyStatus": privacy}},
    ).execute()
    return created["id"]
