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
import time

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload, MediaInMemoryUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
VIDEO_MIME = ("video/mp4", "video/quicktime", "video/x-matroska", "video/webm")
TOKEN_URI = "https://oauth2.googleapis.com/token"
_RETRIES = 5   # googleapiclient tự backoff cho 429/5xx khi execute(num_retries=...)


def _q(name: str) -> str:
    """Escape tên file cho Drive query (tên có dấu ' hoặc \\ làm hỏng cú pháp -> sai/miss file)."""
    return str(name).replace("\\", "\\\\").replace("'", "\\'")


def _service():
    """Drive qua SERVICE ACCOUNT (đọc folder được share) — mặc định."""
    key_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    creds = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _oauth_service(creds: dict):
    """Drive qua OAuth CỦA TỪNG TÀI KHOẢN (để dùng đủ 15GB + XOÁ được file mình sở hữu)."""
    c = Credentials(
        token=None, refresh_token=creds["refresh_token"],
        client_id=creds["client_id"], client_secret=creds["client_secret"],
        token_uri=TOKEN_URI, scopes=SCOPES,
    )
    return build("drive", "v3", credentials=c, cache_discovery=False)


class Drive:
    def __init__(self, service=None):
        self.svc = service or _service()
        self._folder_cache: dict[str, str] = {}

    @classmethod
    def from_oauth(cls, creds: dict):
        """Tạo Drive dùng token OAuth của 1 tài khoản kho lưu trữ."""
        return cls(_oauth_service(creds))

    # ---- dung lượng tài khoản (chỉ đúng khi dùng OAuth của chính acc đó) ----
    def usage(self) -> dict:
        q = self.svc.about().get(fields="storageQuota").execute().get("storageQuota", {})
        limit = int(q.get("limit", 0)) if q.get("limit") else 0
        used = int(q.get("usage", 0))
        return {"used": used, "limit": limit, "free": (limit - used) if limit else None}

    # ---- tìm / tạo folder con theo tên ----
    def child_folder(self, parent_id: str, name: str, create: bool = True) -> str | None:
        cache_key = f"{parent_id}/{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]
        q = (
            f"'{parent_id}' in parents and name = '{_q(name)}' "
            "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )

        def _find_oldest():
            # Lấy NHIỀU rồi chọn folder CŨ NHẤT (deterministic) -> reader & writer luôn trùng folder,
            # tránh cảnh "upload vào folder trùng A, list đọc folder B" khi có bản sao do race.
            res = self.svc.files().list(
                q=q, fields="files(id,name,createdTime)", pageSize=10,
                orderBy="createdTime").execute(num_retries=_RETRIES)
            fs = res.get("files", [])
            return fs[0]["id"] if fs else None

        fid = _find_oldest()
        if not fid and create:
            meta = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            self.svc.files().create(body=meta, fields="id").execute(num_retries=_RETRIES)
            # Re-query sau khi tạo: nếu tiến trình khác cũng vừa tạo (race) -> vẫn chốt CÙNG folder cũ nhất.
            fid = _find_oldest()
        if not fid:
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
                fields="nextPageToken, files(id,name,mimeType,size,parents,modifiedTime,createdTime)",
                pageToken=token,
                pageSize=100,
            ).execute(num_retries=_RETRIES)
            for f in res.get("files", []):
                if f.get("mimeType") in VIDEO_MIME:
                    items.append(f)
            token = res.get("nextPageToken")
            if not token:
                break
        return items

    # ---- tìm 1 file theo tên trong folder (trả id hoặc None) ----
    def find_file(self, parent_id: str, name: str) -> str | None:
        q = f"'{parent_id}' in parents and name = '{_q(name)}' and trashed = false"
        res = self.svc.files().list(q=q, fields="files(id)", pageSize=1).execute(num_retries=_RETRIES)
        files = res.get("files", [])
        return files[0]["id"] if files else None

    # ---- đọc sidecar JSON (nếu có) cạnh video ----
    def read_sidecar(self, parent_id: str, video_name: str) -> dict:
        base = video_name.rsplit(".", 1)[0]
        q = f"'{parent_id}' in parents and name = '{_q(base)}.json' and trashed = false"
        res = self.svc.files().list(q=q, fields="files(id)", pageSize=1).execute(num_retries=_RETRIES)
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

    # ---- upload 1 file vào folder đích (dùng cho archive kho lạnh) ----
    def upload_file(self, dest_folder_id: str, local_path: str, name: str | None = None) -> dict:
        name = name or os.path.basename(local_path)
        return self.svc.files().create(
            body={"name": name, "parents": [dest_folder_id]},
            media_body=MediaFileUpload(local_path, resumable=True, chunksize=1024 * 1024 * 8),
            fields="id",
        ).execute()

    # ---- di chuyển file sang _POSTED / _FAILED ----
    def move(self, file_id: str, channel_root_id: str, target: str = "_POSTED"):
        dest = self.child_folder(channel_root_id, target)
        f = self.svc.files().get(fileId=file_id, fields="parents").execute(num_retries=_RETRIES)
        prev = ",".join(f.get("parents", []))
        self.svc.files().update(
            fileId=file_id, addParents=dest, removeParents=prev, fields="id,parents"
        ).execute(num_retries=_RETRIES)

    # ---- chuyển file sang 1 folder bất kỳ (theo id folder đích) ----
    def move_to_folder(self, file_id: str, dest_folder_id: str):
        f = self.svc.files().get(fileId=file_id, fields="parents").execute(num_retries=_RETRIES)
        prev = ",".join(f.get("parents", []))
        self.svc.files().update(
            fileId=file_id, addParents=dest_folder_id, removeParents=prev, fields="id"
        ).execute(num_retries=_RETRIES)

    # ---- XOÁ file (chỉ được khi token là chủ sở hữu file) ----
    def delete(self, file_id: str):
        self.svc.files().delete(fileId=file_id).execute(num_retries=_RETRIES)

    # ---- LINK CÔNG KHAI TẠM (để FB/IG tự kéo video — KHÔNG tải-lại qua cron) ----
    def make_public(self, file_id: str) -> str:
        """Cho 'anyone with link' đọc -> trả URL tải trực tiếp (dùng cho FB file_url / IG video_url)."""
        try:
            self.svc.permissions().create(
                fileId=file_id, body={"type": "anyone", "role": "reader"},
                fields="id").execute(num_retries=_RETRIES)
        except Exception as e:
            # 'đã public sẵn' -> bỏ qua; lỗi thật (quyền/quota) -> báo để tầng trên biết link có thể KHÔNG công khai.
            if "already" not in str(e).lower() and "duplicate" not in str(e).lower():
                print(f"     ⚠️ make_public {file_id}: {e}")
        # URL tải trực tiếp, bỏ qua trang quét virus với confirm=t
        return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"

    def _anyone_perms(self, file_id: str) -> list[str]:
        """Liệt kê MỌI permission type=anyone (có phân trang) -> tránh sót khi >100 quyền."""
        ids, token = [], None
        while True:
            res = self.svc.permissions().list(
                fileId=file_id, fields="nextPageToken, permissions(id,type)",
                pageSize=100, pageToken=token).execute(num_retries=_RETRIES)
            ids += [p["id"] for p in res.get("permissions", []) if p.get("type") == "anyone"]
            token = res.get("nextPageToken")
            if not token:
                break
        return ids

    def make_private(self, file_id: str) -> bool:
        """Thu hồi MỌI quyền 'anyone' sau khi đăng xong (bảo mật).
        Retry + verify: trả True nếu chắc chắn không còn quyền anyone; False nếu THẤT BẠI
        (tầng trên nên ghi cờ cần-thu-hồi để sweeper quét lại — không để file public vĩnh viễn)."""
        for attempt in range(4):
            try:
                ids = self._anyone_perms(file_id)
                if not ids:
                    return True   # đã sạch
                for pid in ids:
                    self.svc.permissions().delete(
                        fileId=file_id, permissionId=pid).execute(num_retries=_RETRIES)
                # verify: list lại, còn sót thì thử vòng sau
                if not self._anyone_perms(file_id):
                    return True
            except Exception as e:
                print(f"     ⚠️ make_private lần {attempt+1} lỗi: {e}")
            time.sleep(2 * (attempt + 1))
        print(f"     ❌ make_private THẤT BẠI cho {file_id} — file có thể CÒN công khai, cần thu hồi lại!")
        return False

    # ---- liệt kê file trong 1 subfolder theo tên (vd _POSTED) ----
    def list_folder_videos(self, root_id: str, subfolder: str) -> list[dict]:
        fid = self.child_folder(root_id, subfolder, create=False)
        return self._list_videos(fid) if fid else []

    # ---- ĐẨY video (+ sidecar) từ máy lên _QUEUE/<type> ----
    def upload_to_queue(self, channel_root_id: str, local_path: str, vtype: str,
                        sidecar: dict | None = None, thumbnail_path: str | None = None,
                        subtitle_path: str | None = None) -> dict:
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
        # LINK FULL QUYỀN MẶC ĐỊNH: ai có link đều XEM được -> mở link không cần "request access".
        try:
            self.svc.permissions().create(fileId=created["id"], body={"role": "reader", "type": "anyone"}, fields="id").execute()
        except Exception:
            pass

        if sidecar:
            blob = json.dumps(sidecar, ensure_ascii=False, indent=2).encode("utf-8")
            self.svc.files().create(
                body={"name": f"{base}.json", "parents": [folder]},
                media_body=MediaInMemoryUpload(blob, mimetype="application/json"),
                fields="id",
            ).execute()

        if thumbnail_path and os.path.exists(thumbnail_path):
            ext = os.path.splitext(thumbnail_path)[1] or ".jpg"
            _th = self.svc.files().create(
                body={"name": f"{base}{ext}", "parents": [folder]},
                media_body=MediaFileUpload(thumbnail_path, resumable=True),
                fields="id",
            ).execute()
            # Trả id ẢNH về cho caller -> lưu vào bản ghi job -> dashboard hiện được thumbnail để
            # user tự soi và bấm "tạo lại" nếu xấu. Không có id thì web không biết lấy ảnh ở đâu.
            created["thumb_id"] = (_th or {}).get("id", "")

        if subtitle_path and os.path.exists(subtitle_path):
            ext = os.path.splitext(subtitle_path)[1] or ".srt"
            self.svc.files().create(
                body={"name": f"{base}{ext}", "parents": [folder]},
                media_body=MediaFileUpload(subtitle_path, resumable=True),
                fields="id",
            ).execute()
        return created
