"""
drive_client.py — Đọc/ghi Google Drive bằng SERVICE ACCOUNT.

Vì sao service account?
  - Chạy trên GitHub Actions không có người bấm đăng nhập.
  - Bạn chỉ cần SHARE folder gốc "MM0-PUBLISH/<KÊNH>" cho email service account
    (quyền Editor) là nó đọc + di chuyển file được.

Cấu trúc folder mỗi kênh trên Drive:
    <KÊNH>/
      _QUEUE/        <- video CHƯA đăng
        long/
        short/
      _POSTED/       <- tự động chuyển vào đây SAU KHI đăng thành công
      _FAILED/       <- chuyển vào đây nếu lỗi nhiều lần (để bạn kiểm tra)

Sidecar metadata (tùy chọn): cùng tên file .json bên cạnh video.
"""

from __future__ import annotations
import io
import json
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload, MediaInMemoryUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
VIDEO_MIME = ("video/mp4", "video/quicktime", "video/x-matroska", "video/webm")


def _service():
    key_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    creds = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


class Drive:
    def __init__(self):
        self.svc = _service()
        self._folder_cache: dict[str, str] = {}

    # ---- tìm / tạo folder con theo tên ----
    def child_folder(self, parent_id: str, name: str, create: bool = True) -> str | None:
        cache_key = f"{parent_id}/{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]
        q = (
            f"'{parent_id}' in parents and name = '{name}' "
            "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        res = self.svc.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
        files = res.get("files", [])
        if files:
            fid = files[0]["id"]
        elif create:
            meta = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            fid = self.svc.files().create(body=meta, fields="id").execute()["id"]
        else:
            return None
        self._folder_cache[cache_key] = fid
        return fid

    # ---- liệt kê video trong _QUEUE (long + short) ----
    def list_queue(self, channel_root_id: str) -> list[dict]:
        queue = self.child_folder(channel_root_id, "_QUEUE")
        out = []
        for sub in ("long", "short"):
            folder = self.child_folder(queue, sub)
            for f in self._list_videos(folder):
                f["type"] = sub
                out.append(f)
        return out

    def _list_videos(self, folder_id: str) -> list[dict]:
        items, token = [], None
        while True:
            res = self.svc.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id,name,mimeType,size,parents)",
                pageToken=token,
                pageSize=100,
            ).execute()
            for f in res.get("files", []):
                if f.get("mimeType") in VIDEO_MIME:
                    items.append(f)
            token = res.get("nextPageToken")
            if not token:
                break
        return items

    # ---- tìm 1 file theo tên trong folder (trả id hoặc None) ----
    def find_file(self, parent_id: str, name: str) -> str | None:
        q = f"'{parent_id}' in parents and name = '{name}' and trashed = false"
        res = self.svc.files().list(q=q, fields="files(id)", pageSize=1).execute()
        files = res.get("files", [])
        return files[0]["id"] if files else None

    # ---- đọc sidecar JSON (nếu có) cạnh video ----
    def read_sidecar(self, parent_id: str, video_name: str) -> dict:
        base = video_name.rsplit(".", 1)[0]
        q = f"'{parent_id}' in parents and name = '{base}.json' and trashed = false"
        res = self.svc.files().list(q=q, fields="files(id)", pageSize=1).execute()
        files = res.get("files", [])
        if not files:
            return {}
        data = self.svc.files().get_media(fileId=files[0]["id"]).execute()
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {}

    # ---- tải video về file tạm để upload ----
    def download(self, file_id: str, dest_path: str) -> str:
        req = self.svc.files().get_media(fileId=file_id)
        with io.FileIO(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, req, chunksize=1024 * 1024 * 8)
            done = False
            while not done:
                _status, done = downloader.next_chunk()
        return dest_path

    # ---- di chuyển file sang _POSTED / _FAILED ----
    def move(self, file_id: str, channel_root_id: str, target: str = "_POSTED"):
        dest = self.child_folder(channel_root_id, target)
        f = self.svc.files().get(fileId=file_id, fields="parents").execute()
        prev = ",".join(f.get("parents", []))
        self.svc.files().update(
            fileId=file_id, addParents=dest, removeParents=prev, fields="id,parents"
        ).execute()

    # ---- ĐẨY video (+ sidecar) từ máy lên _QUEUE/<type> ----
    def upload_to_queue(self, channel_root_id: str, local_path: str, vtype: str,
                        sidecar: dict | None = None, thumbnail_path: str | None = None) -> dict:
        """
        Dùng bởi enqueue.py: sau khi render xong, đẩy file lên đúng _QUEUE/long|short.
        Kèm sidecar .json và thumbnail (nếu có). Trả về {"id":..., "name":...}.
        """
        queue = self.child_folder(channel_root_id, "_QUEUE")
        folder = self.child_folder(queue, "short" if vtype == "short" else "long")
        name = os.path.basename(local_path)
        base = name.rsplit(".", 1)[0]

        media = MediaFileUpload(local_path, resumable=True, chunksize=1024 * 1024 * 8)
        created = self.svc.files().create(
            body={"name": name, "parents": [folder]},
            media_body=media, fields="id,name",
        ).execute()

        if sidecar:
            blob = json.dumps(sidecar, ensure_ascii=False, indent=2).encode("utf-8")
            self.svc.files().create(
                body={"name": f"{base}.json", "parents": [folder]},
                media_body=MediaInMemoryUpload(blob, mimetype="application/json"),
                fields="id",
            ).execute()

        if thumbnail_path and os.path.exists(thumbnail_path):
            ext = os.path.splitext(thumbnail_path)[1] or ".jpg"
            self.svc.files().create(
                body={"name": f"{base}{ext}", "parents": [folder]},
                media_body=MediaFileUpload(thumbnail_path, resumable=True),
                fields="id",
            ).execute()
        return created
