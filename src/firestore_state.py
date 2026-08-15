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
    def _counter_id(self, channel: str, day: datetime) -> str:
        return f"{channel}_{day.strftime('%Y%m%d')}"

    def get_counters(self, channel: str, day: datetime) -> dict:
        cid = self._counter_id(channel, day)
        doc = self.db.collection("counters").document(cid).get()
        return doc.to_dict() if doc.exists else {"yt": 0, "fb": 0, "last_upload_at": None}

    def bump_counters(self, channel: str, day: datetime, yt: int = 0, fb: int = 0):
        cid = self._counter_id(channel, day)
        ref = self.db.collection("counters").document(cid)
        ref.set(
            {
                "channel": channel,
                "yt": firestore.Increment(yt),
                "fb": firestore.Increment(fb),
                "last_upload_at": datetime.now(timezone.utc).isoformat(),
            },
            merge=True,
        )

    def last_upload_at(self, channel: str, day: datetime) -> datetime | None:
        c = self.get_counters(channel, day)
        v = c.get("last_upload_at")
        return datetime.fromisoformat(v) if v else None

    # ---------- DOC tổng quát (settings/config, storage/pool ...) ----------
    def set_doc(self, collection: str, doc_id: str, data: dict):
        self.db.collection(collection).document(doc_id).set(
            {**data, "updated_at": datetime.now(timezone.utc).isoformat()}, merge=True)

    # ---------- CONNECTIONS (token do Cloudflare Worker ghi) ----------
    def get_connection(self, channel: str, kind: str = "youtube") -> dict | None:
        """Đọc token đã kết nối qua dashboard. None nếu chưa kết nối."""
        doc = self.db.collection("connections").document(f"{channel}_{kind}").get()
        return doc.to_dict() if doc.exists else None

    # ---------- STATS (view / sub) ----------
    def set_channel_stats(self, channel: str, data: dict):
        data = {**data, "channel": channel, "updated_at": datetime.now(timezone.utc).isoformat()}
        self.db.collection("channels").document(channel).set(data, merge=True)

    def set_channel_health(self, channel: str, data: dict):
        """Ghi trạng thái KẾT NỐI/vận hành để trang 'Kết nối API' hiển thị realtime."""
        self.db.collection("channels").document(channel).set(
            {**data, "channel": channel}, merge=True)

    def all_counters_today(self, day: datetime) -> dict:
        """Bộ đếm đăng hôm nay của mọi kênh: {channel: {yt, fb}} (cho dashboard)."""
        suffix = day.strftime("%Y%m%d")
        out = {}
        for d in self.db.collection("counters").stream():
            if d.id.endswith(suffix):
                out[d.id[: -(len(suffix) + 1)]] = d.to_dict()
        return out

    def posted_youtube(self, channel: str) -> list[tuple[str, str]]:
        """Trả [(doc_id, youtube_video_id)] cho video đã đăng có id YouTube."""
        q = (self.db.collection("videos")
             .where("channel", "==", channel).where("status", "==", "posted"))
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
