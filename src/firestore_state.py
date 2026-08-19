"""
firestore_state.py — Lưu TRẠNG THÁI & đồng bộ DASHBOARD trên Firestore (free).

Firestore là "nguồn sự thật" duy nhất cho:
  - Hàng đợi video (đã/chưa đăng, đã lên lịch, lỗi).
  - Bộ đếm đăng theo ngày (để giữ trần an toàn).
  - Dữ liệu dashboard đọc realtime.

Dùng chung service account với Drive (biến GOOGLE_APPLICATION_CREDENTIALS).

TÁCH PROJECT (chống nghẽn quota — xem render-pipeline/SHARD_C_SETUP.md):
  - self.db  = Project A (SHARED: settings, connections, channels, storage_reservations — do dashboard/Worker ghi).
  - self.pub = Project C (OWNED: videos, counters, quota, yt_queue, social_queue — publisher tự ghi).
             Bật SHARD_PUBLISH=1 (khi đã migrate A->C) mới trỏ C; chưa bật -> C=A (backward-compatible).
  - render_jobs sống ở Project B -> auto_enqueue dùng client_render_jobs().

Collections:
  videos/{drive_file_id}   -> 1 video
  counters/{CHANNEL_YYYYMMDD} -> {yt, fb, last_upload_at}
"""

from __future__ import annotations
import os
from datetime import datetime, timezone

from google.cloud import firestore
from google.oauth2 import service_account


def _sa_client(key_env: str, proj_env: str):
    """Tạo client từ 1 cặp secret (key file + project id). None nếu chưa cấu hình đủ / file rỗng / hỏng
    -> caller fallback A, KHÔNG crash publisher (an toàn khi secret chưa set)."""
    key = os.environ.get(key_env)
    project = os.environ.get(proj_env)
    if not (key and project and os.path.exists(key)):
        return None
    try:
        if os.path.getsize(key) < 10:       # file rỗng (secret chưa set) -> bỏ qua
            return None
        creds = service_account.Credentials.from_service_account_file(key)
        return firestore.Client(project=project, credentials=creds)
    except Exception:
        return None


def client() -> firestore.Client:
    """Project A (SHARED config: settings/connections/channels/storage_reservations)."""
    key = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    project = os.environ.get("FIREBASE_PROJECT_ID")
    creds = service_account.Credentials.from_service_account_file(key)
    return firestore.Client(project=project, credentials=creds)


def client_publish() -> firestore.Client:
    """Project C (OWNED: videos/counters/quota/yt_queue/social_queue). Bật SHARD_PUBLISH=1 mới tách; chưa thì = A."""
    if os.environ.get("SHARD_PUBLISH") == "1":
        c = _sa_client("GOOGLE_APPLICATION_CREDENTIALS_C", "FIREBASE_PROJECT_ID_C")
        if c is not None:
            return c
    return client()


def client_render_jobs() -> firestore.Client:
    """Project B (render_jobs). Publisher đọc/ghi cờ 'queued' phải trỏ B, KHÔNG theo C."""
    c = _sa_client("GOOGLE_APPLICATION_CREDENTIALS_B", "FIREBASE_PROJECT_ID_B")
    return c if c is not None else client()


class State:
    def __init__(self):
        self.db = client()            # A: SHARED (settings/connections/channels/storage_reservations)
        self.pub = client_publish()   # C: OWNED (videos/counters/quota/yt_queue/social_queue)

    # ---------- VIDEOS (OWNED -> C) ----------
    def get_video(self, file_id: str) -> dict | None:
        doc = self.pub.collection("videos").document(file_id).get()
        return doc.to_dict() if doc.exists else None

    def upsert_video(self, file_id: str, data: dict):
        data = {**data, "updated_at": datetime.now(timezone.utc).isoformat()}
        self.pub.collection("videos").document(file_id).set(data, merge=True)

    def sig_exists(self, channel: str, sig: str, owner: str | None = None) -> str | None:
        """Đã ingest video có vân tay này cho kênh này chưa? Trả drive_file_id nếu có."""
        q = (self.pub.collection("videos")
             .where("channel", "==", channel).where("sig", "==", sig))
        if owner:
            q = q.where("owner", "==", owner)
        for d in q.limit(1).stream():
            return d.id
        return None

    def list_videos(self, channel: str | None = None) -> list[dict]:
        col = self.pub.collection("videos")
        query = col.where("channel", "==", channel) if channel else col
        out = []
        for d in query.stream():
            row = d.to_dict()
            row["id"] = d.id
            out.append(row)
        return out

    # ---------- COUNTERS (trần an toàn / ngày) (OWNED -> C) ----------
    # owner != None -> multi-tenant: doc id có tiền tố uid + field owner
    def _counter_id(self, channel: str, day: datetime, owner: str | None = None) -> str:
        d = day.strftime('%Y%m%d')
        return f"{owner}__{channel}__{d}" if owner else f"{channel}_{d}"

    def get_counters(self, channel: str, day: datetime, owner: str | None = None) -> dict:
        cid = self._counter_id(channel, day, owner)
        doc = self.pub.collection("counters").document(cid).get()
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
        self.pub.collection("counters").document(cid).set(data, merge=True)

    # ---------- QUOTA THEO OAuth CLIENT (mỗi project 10.000/ngày ~ 6 upload) (OWNED -> C) ----------
    def _client_qid(self, client_id: str, day: datetime) -> str:
        safe = "".join(c if c.isalnum() else "_" for c in (client_id or "?"))[:40]
        return f"{safe}_{day.strftime('%Y%m%d')}"

    def client_uploads_today(self, client_id: str, day: datetime) -> int:
        d = self.pub.collection("quota").document(self._client_qid(client_id, day)).get()
        return (d.to_dict() or {}).get("uploads", 0) if d.exists else 0

    def bump_client_uploads(self, client_id: str, day: datetime, owner: str | None = None):
        data = {"client": (client_id or "")[:60], "uploads": firestore.Increment(1),
                "updated_at": datetime.now(timezone.utc).isoformat()}
        if owner:
            data["owner"] = owner
        self.pub.collection("quota").document(self._client_qid(client_id, day)).set(data, merge=True)

    def last_upload_at(self, channel: str, day: datetime, owner: str | None = None):
        c = self.get_counters(channel, day, owner)
        v = c.get("last_upload_at")
        return datetime.fromisoformat(v) if v else None

    # ---------- HÀNG ĐỢI ĐĂNG FB/IG ĐỘC LẬP (social_queue) (OWNED -> C) ----------
    def list_social_queue(self) -> list[dict]:
        """Item đang chờ đăng FB/IG độc lập (không gắn YouTube)."""
        out = []
        for d in self.pub.collection("social_queue").where("status", "in", ["pending", "processing"]).stream():
            row = d.to_dict(); row["id"] = d.id; out.append(row)
        return out

    def update_queue(self, doc_id: str, patch: dict):
        self.pub.collection("social_queue").document(doc_id).set(
            {**patch, "updated_at": datetime.now(timezone.utc).isoformat()}, merge=True)

    # ---------- HÀNG ĐỢI ĐĂNG YOUTUBE TỪ DRIVE (Content Hub, yt_queue) (OWNED -> C) ----------
    def list_yt_queue(self) -> list[dict]:
        out = []
        for d in self.pub.collection("yt_queue").where("status", "in", ["pending", "processing"]).stream():
            row = d.to_dict(); row["id"] = d.id; out.append(row)
        return out

    def update_yt_queue(self, doc_id: str, patch: dict):
        self.pub.collection("yt_queue").document(doc_id).set(
            {**patch, "updated_at": datetime.now(timezone.utc).isoformat()}, merge=True)

    # ---------- KHOÁ CHỐNG ĐĂNG TRÙNG (claim atomic qua transaction) (OWNED -> C) ----------
    def claim_item(self, collection: str, doc_id: str, now: datetime, lease_min: int = 15) -> bool:
        """Giành quyền xử lý 1 item. True nếu claim được (pending, hoặc processing đã treo quá lease).
        Chống 2 lần cron chồng nhau cùng đăng 1 video. (yt_queue/social_queue -> Project C)"""
        ref = self.pub.collection(collection).document(doc_id)
        tx = self.pub.transaction()

        @firestore.transactional
        def _claim(transaction):
            snap = ref.get(transaction=transaction)
            d = snap.to_dict() or {}
            st = d.get("status")
            if st == "pending":
                transaction.update(ref, {"status": "processing", "lease_at": now.isoformat()})
                return True
            if st == "processing":       # tiến trình trước có thể đã crash -> reclaim nếu lease cũ
                la = d.get("lease_at")
                try:
                    stale = (not la) or (now - datetime.fromisoformat(str(la).replace("Z", "+00:00"))).total_seconds() > lease_min * 60
                except Exception:
                    stale = True
                if stale:
                    transaction.update(ref, {"status": "processing", "lease_at": now.isoformat()})
                    return True
            return False
        return _claim(tx)

    # ---------- DOC tổng quát (settings/config, storage/pool ...) (SHARED -> A) ----------
    def get_doc(self, collection: str, doc_id: str) -> dict | None:
        d = self.db.collection(collection).document(doc_id).get()
        return d.to_dict() if d.exists else None

    def set_doc(self, collection: str, doc_id: str, data: dict):
        self.db.collection(collection).document(doc_id).set(
            {**data, "updated_at": datetime.now(timezone.utc).isoformat()}, merge=True)

    # ---------- CONNECTIONS (token do Cloudflare Worker ghi) (SHARED -> A) ----------
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
        self.db.collection("channels").document(docid).set(data, merge=True)   # SHARED -> A (dashboard đọc)

    def set_channel_health(self, channel: str, data: dict, owner: str | None = None):
        """Ghi trạng thái KẾT NỐI/vận hành để trang 'Kết nối API' hiển thị realtime."""
        d = {**data, "channel": channel}
        if owner:
            d["owner"] = owner
        docid = f"{owner}__{channel}" if owner else channel
        self.db.collection("channels").document(docid).set(d, merge=True)      # SHARED -> A

    def all_counters_today(self, day: datetime) -> dict:
        """Bộ đếm đăng hôm nay của mọi kênh: {channel: {yt, fb}} (cho dashboard). (OWNED -> C)"""
        suffix = day.strftime("%Y%m%d")
        out = {}
        for d in self.pub.collection("counters").stream():
            if not d.id.endswith(suffix):
                continue
            data = d.to_dict() or {}
            # dùng field 'channel' đã lưu (chuẩn cho cả id {channel}_{date} lẫn {uid}__{channel}__{date})
            key = data.get("channel") or d.id.rsplit("_", 1)[0].rstrip("_")
            out[key] = data
        return out

    def posted_youtube(self, channel: str, owner: str | None = None) -> list[tuple[str, str]]:
        """Trả [(doc_id, youtube_video_id)] cho video đã đăng có id YouTube. (OWNED -> C)"""
        q = (self.pub.collection("videos")
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
        self.pub.collection("videos").document(doc_id).set(
            {"stats": {**stats, "updated_at": datetime.now(timezone.utc).isoformat()}},
            merge=True,
        )
