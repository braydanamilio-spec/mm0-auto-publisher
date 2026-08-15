"""
firestore_state.py — Lưu TRẠNG THÁI & đồng bộ DASHBOARD trên Firestore (free).

Firestore là "nguồn sự thật" duy nhất cho:
  - Hàng đợi video (đã/chưa đăng, đã lên lịch, lỗi).
  - Bộ đếm đăng theo ngày (để giữ trần an toàn).
  - Dữ liệu dashboard đọc realtime.

Dùng chung service account với Drive (biến GOOGLE_APPLICATION_CREDENTIALS).

Collections:
  videos/{drive_file_id}   -> 1 video
  counters/{CHANNEL_YYYYMMDD} -> {yt, fb, last_upload_at}
"""

from __future__ import annotations
import os
from datetime import datetime, timezone

from google.cloud import firestore
from google.oauth2 import service_account


def client() -> firestore.Client:
    key = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    project = os.environ.get("FIREBASE_PROJECT_ID")
    creds = service_account.Credentials.from_service_account_file(key)
    return firestore.Client(project=project, credentials=creds)


class State:
    def __init__(self):
        self.db = client()

    # ---------- VIDEOS ----------
    def get_video(self, file_id: str) -> dict | None:
        doc = self.db.collection("videos").document(file_id).get()
        return doc.to_dict() if doc.exists else None

    def upsert_video(self, file_id: str, data: dict):
        data = {**data, "updated_at": datetime.now(timezone.utc).isoformat()}
        self.db.collection("videos").document(file_id).set(data, merge=True)

    def sig_exists(self, channel: str, sig: str, owner: str | None = None) -> str | None:
        """Đã ingest video có vân tay này cho kênh này chưa? Trả drive_file_id nếu có."""
        q = (self.db.collection("videos")
             .where("channel", "==", channel).where("sig", "==", sig))
        if owner:
            q = q.where("owner", "==", owner)
        for d in q.limit(1).stream():
            return d.id
        return None

    def list_videos(self, channel: str | None = None) -> list[dict]:
        col = self.db.collection("videos")
        query = col.where("channel", "==", channel) if channel else col
        out = []
        for d in query.stream():
            row = d.to_dict()
            row["id"] = d.id
            out.append(row)
        return out

    # ---------- COUNTERS (trần an toàn / ngày) ----------
    # owner != None -> multi-tenant: doc id có tiền tố uid + field owner
    def _counter_id(self, channel: str, day: datetime, owner: str | None = None) -> str:
        d = day.strftime('%Y%m%d')
        return f"{owner}__{channel}__{d}" if owner else f"{channel}_{d}"

    def get_counters(self, channel: str, day: datetime, owner: str | None = None) -> dict:
        cid = self._counter_id(channel, day, owner)
        doc = self.db.collection("counters").document(cid).get()
        return doc.to_dict() if doc.exists else {"yt": 0, "fb": 0, "last_upload_at": None}

    def bump_counters(self, channel: str, day: datetime, yt: int = 0, fb: int = 0,
                      owner: str | None = None):
        cid = self._counter_id(channel, day, owner)
        data = {
            "channel": channel,
            "yt": firestore.Increment(yt),
            "fb": firestore.Increment(fb),
            "last_upload_at": datetime.now(timezone.utc).isoformat(),
        }
        if owner:
            data["owner"] = owner
        self.db.collection("counters").document(cid).set(data, merge=True)

    def last_upload_at(self, channel: str, day: datetime, owner: str | None = None):
        c = self.get_counters(channel, day, owner)
        v = c.get("last_upload_at")
        return datetime.fromisoformat(v) if v else None

    # ---------- DOC tổng quát (settings/config, storage/pool ...) ----------
    def get_doc(self, collection: str, doc_id: str) -> dict | None:
        d = self.db.collection(collection).document(doc_id).get()
        return d.to_dict() if d.exists else None

    def set_doc(self, collection: str, doc_id: str, data: dict):
        self.db.collection(collection).document(doc_id).set(
            {**data, "updated_at": datetime.now(timezone.utc).isoformat()}, merge=True)

    # ---------- CONNECTIONS (token do Cloudflare Worker ghi) ----------
    def get_connection(self, channel: str, kind: str = "youtube") -> dict | None:
        """Đọc token đã kết nối qua dashboard. None nếu chưa kết nối."""
        doc = self.db.collection("connections").document(f"{channel}_{kind}").get()
        return doc.to_dict() if doc.exists else None

    def list_connections(self, kind: str) -> list[dict]:
        """Danh sách connection theo loại (vd 'drive') do Worker ghi."""
        out = []
        for d in self.db.collection("connections").where("kind", "==", kind).stream():
            out.append(d.to_dict())
        return out

    # ---------- STATS (view / sub) ----------
    def set_channel_stats(self, channel: str, data: dict, owner: str | None = None):
        data = {**data, "channel": channel, "updated_at": datetime.now(timezone.utc).isoformat()}
        if owner:
            data["owner"] = owner
        docid = f"{owner}__{channel}" if owner else channel
        self.db.collection("channels").document(docid).set(data, merge=True)

    def set_channel_health(self, channel: str, data: dict, owner: str | None = None):
        """Ghi trạng thái KẾT NỐI/vận hành để trang 'Kết nối API' hiển thị realtime."""
        d = {**data, "channel": channel}
        if owner:
            d["owner"] = owner
        docid = f"{owner}__{channel}" if owner else channel
        self.db.collection("channels").document(docid).set(d, merge=True)

    def all_counters_today(self, day: datetime) -> dict:
        """Bộ đếm đăng hôm nay của mọi kênh: {channel: {yt, fb}} (cho dashboard)."""
        suffix = day.strftime("%Y%m%d")
        out = {}
        for d in self.db.collection("counters").stream():
            if not d.id.endswith(suffix):
                continue
            data = d.to_dict() or {}
            # dùng field 'channel' đã lưu (chuẩn cho cả id {channel}_{date} lẫn {uid}__{channel}__{date})
            key = data.get("channel") or d.id.rsplit("_", 1)[0].rstrip("_")
            out[key] = data
        return out

    def posted_youtube(self, channel: str, owner: str | None = None) -> list[tuple[str, str]]:
        """Trả [(doc_id, youtube_video_id)] cho video đã đăng có id YouTube."""
        q = (self.db.collection("videos")
             .where("channel", "==", channel).where("status", "==", "posted"))
        if owner:
            q = q.where("owner", "==", owner)
        out = []
        for d in q.stream():
            r = d.to_dict()
            yid = ((r.get("results") or {}).get("youtube") or {}).get("id")
            if yid:
                out.append((d.id, yid))
        return out

    def set_video_stats(self, doc_id: str, stats: dict):
        self.db.collection("videos").document(doc_id).set(
            {"stats": {**stats, "updated_at": datetime.now(timezone.utc).isoformat()}},
            merge=True,
        )
