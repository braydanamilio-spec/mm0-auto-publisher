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


# Mốc NGỪNG thử lại, TÁCH RIÊNG TỪNG PROJECT (xem _retry).
# 24/8 — lỗi tiềm ẩn do chính bản vá "đệm âm" ban đầu gây ra: dùng MỘT mốc chung cho cả 3 project.
# A cạn hạn mức (chuyện đang xảy ra hằng ngày) sẽ khoá luôn đường thử-lại của B và C suốt 30 phút,
# trong khi B/C vẫn còn hạn mức và cơn 429 của chúng chỉ là burst thoáng qua — thử lại là qua.
# Hậu quả nếu để nguyên: một nhịp nghẽn ở C là RỚT LUÔN lượt đăng của video đó, không thử lại.
_CAN_QUOTA_P: dict = {}


class _CanQuotaCompat(dict):
    """Giữ tên cũ `_CAN_QUOTA["den"]` chạy được (test/di sản) — trỏ vào bucket chung '?'."""
    def __getitem__(self, k):
        return _CAN_QUOTA_P.get("?", {}).get(k, 0.0)

    def __setitem__(self, k, v):
        _CAN_QUOTA_P.setdefault("?", {})[k] = v


_CAN_QUOTA = _CanQuotaCompat()


def _moc(p: str) -> float:
    return float(_CAN_QUOTA_P.get(p, {}).get("den", 0.0))


def _retry_C(fn, tries: int = 5):
    """Lối thử-lại cho project C (yt_queue/videos) — sổ nghỉ riêng, không dính mốc của A."""
    return _retry(fn, tries, p="C")


def _la_quota(e) -> bool:
    t = str(e)
    return ("RESOURCE_EXHAUSTED" in t or "Quota exceeded" in t or "429" in t
            or type(e).__name__ == "ResourceExhausted")


def _retry(fn, tries: int = 5, p: str = "A"):
    """Thử lại khi Firestore 429/RESOURCE_EXHAUSTED (burst đọc/ghi dồn, hết quota tạm) -> KHÔNG để
    burst thoáng qua làm CRASH cả tiến trình (đã gây publish.yml/stats.yml lỗi 'Quota exceeded' hoàn
    toàn không cần thiết — quota tự hồi trong vài giây). Cùng mẫu với render-pipeline/firestore_bridge.py."""
    import time as _t
    # 24/8 — PHÂN BIỆT "BURST THOÁNG QUA" VỚI "CẠN HẠN MỨC CẢ NGÀY".
    # Hai thứ này cùng ra mã 429 nhưng chữa ngược nhau: burst thì thử lại vài giây là qua, còn cạn
    # hạn mức ngày thì thử lại 5 lần chỉ để đốt thêm 5 lượt đọc (lượt hỏng VẪN TÍNH vào hạn mức) và
    # kéo dài mỗi lệnh thêm ~22s. Đo phiên 08:47: A cạn từ 09:18 mà cả hệ vẫn đập vào A suốt phiên —
    # sáng hôm sau vừa reset là bị đốt lại ngay, đúng cảnh "khởi động ngày mới cái đốt sạch quota".
    # Nay: gặp 429 lần đầu là ghi nhớ; trong 30' sau đó KHÔNG thử lại nữa, ném luôn cho tầng trên
    # chuyển sang gương/đệm. Burst thật vẫn được thử lại như cũ (vì mốc nghỉ chỉ đặt khi ĐÃ hết lượt).
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            s = str(e)
            _la_quota = ("RESOURCE_EXHAUSTED" in s or "Quota exceeded" in s or "429" in s)
            if _la_quota and _t.time() < _moc(p):
                raise                       # project này đang nghỉ -> đừng thử lại, đừng đốt thêm
            if _la_quota and i < tries - 1:
                _t.sleep(1.5 * (i + 1)); continue
            if _la_quota:
                _CAN_QUOTA_P.setdefault(p, {})["den"] = _t.time() + 1800
                print(f"   ⛔ Project {p} cạn hạn mức — ngừng thử lại 30 phút "
                      f"(mỗi lượt hỏng vẫn tính vào trần). Project khác KHÔNG bị ảnh hưởng.")
            raise


def _sa_client(key_env: str, proj_env: str):
    """Tạo client từ 1 cặp secret (key file + project id). None nếu chưa cấu hình đủ / file rỗng / hỏng
    -> caller fallback A, KHÔNG crash publisher (an toàn khi secret chưa set).
    19/8: TRƯỚC ĐÂY fallback HOÀN TOÀN IM LẶNG — không log gì -> nếu Project C cấu hình sai (key hỏng/
    thiếu Firestore database) thì auto-fallback về A ÂM THẦM MÃI MÃI, không ai biết, tới khi A cạn quota
    mới lộ ra (đúng sự cố 19/8: Shard C = 0 hoạt động, A bị 53K reads). Giờ LUÔN in cảnh báo khi fallback."""
    key = os.environ.get(key_env)
    project = os.environ.get(proj_env)
    if not (key and project and os.path.exists(key)):
        print(f"⚠️ {proj_env}: thiếu {key_env}/{proj_env} -> fallback Project A"); return None
    try:
        if os.path.getsize(key) < 10:       # file rỗng (secret chưa set) -> bỏ qua
            print(f"⚠️ {key_env}: file rỗng (secret chưa set) -> fallback Project A"); return None
        creds = service_account.Credentials.from_service_account_file(key)
        c = firestore.Client(project=project, credentials=creds)
    except Exception as e:
        print(f"⚠️ {proj_env} ({project}) không tạo được client ({e}) -> fallback Project A — "
              f"KIỂM: key đúng project chưa?")
        return None
    try:
        next(c.collection("_ping").limit(1).stream(), None)   # ép kiểm database TỒN TẠI ngay (Client() không tự validate) -> lộ lỗi sớm
    except Exception as e:
        _s = str(e)
        if "429" in _s or "RESOURCE_EXHAUSTED" in _s or "Quota exceeded" in _s:
            # 24/8 tối — PHÁ THẾ CÁCH LY: lượt ping hỏng vì shard CẠN HẠN MỨC NGÀY, nhưng nhánh này
            # xử như "cấu hình sai" rồi trả None ⇒ mọi lệnh của shard đó đổ sang **project A**.
            # Tức B cạn là kéo A cạn theo — đúng cái vòng luẩn quẩn "một project cạn giết cả hệ" mà
            # kiến trúc 3-project sinh ra để chặn. Cạn hạn mức KHÁC cấu hình sai: project vẫn đúng,
            # client vẫn dùng được, chỉ là hôm nay hết lượt. Trả client về cho tầng trên tự
            # retry/failover sang B2 — KHÔNG rơi về A.
            print(f"⚠️ {proj_env} ({project}) CẠN HẠN MỨC ngày (429) — vẫn dùng client này, "
                  f"KHÔNG rơi về Project A (giữ thế cách ly). Tầng trên sẽ retry / lật gương.")
            return c
        print(f"⚠️ {proj_env} ({project}) lỗi kết nối thật ({e}) -> fallback Project A — KIỂM: key đúng project chưa? Firestore database đã tạo trong project này chưa?")
        return None
    return c


def _dem(p: str, r: int = 0, w: int = 0) -> None:
    """Ghi nhận lượt đọc/ghi vào sổ quota. Nhập muộn để tránh vòng import (quota_guard cần
    chính module này để lấy client). Sổ hỏng thì im lặng — không được làm gãy việc chính."""
    try:
        import quota_guard
        quota_guard.dem(p, r=r, w=w)
    except Exception:
        pass


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
        doc = _retry_C(lambda: self.pub.collection("videos").document(file_id).get())
        return doc.to_dict() if doc.exists else None

    def upsert_video(self, file_id: str, data: dict):
        data = {**data, "updated_at": datetime.now(timezone.utc).isoformat()}
        _retry_C(lambda: self.pub.collection("videos").document(file_id).set(data, merge=True))

    def sig_exists(self, channel: str, sig: str, owner: str | None = None) -> str | None:
        """Đã ingest video có vân tay này cho kênh này chưa? Trả drive_file_id nếu có."""
        q = (self.pub.collection("videos")
             .where("channel", "==", channel).where("sig", "==", sig))
        if owner:
            q = q.where("owner", "==", owner)
        docs = _retry(lambda: list(q.limit(1).stream()))
        return docs[0].id if docs else None

    def list_videos(self, channel: str | None = None) -> list[dict]:
        col = self.pub.collection("videos")
        query = col.where("channel", "==", channel) if channel else col
        out = []
        for d in _retry(lambda: list(query.stream())):
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
        doc = _retry_C(lambda: self.pub.collection("counters").document(cid).get())
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
        _retry_C(lambda: self.pub.collection("counters").document(cid).set(data, merge=True))

    # ---------- QUOTA THEO OAuth CLIENT (mỗi project 10.000/ngày ~ 6 upload) (OWNED -> C) ----------
    def _client_qid(self, client_id: str, day: datetime) -> str:
        safe = "".join(c if c.isalnum() else "_" for c in (client_id or "?"))[:40]
        return f"{safe}_{day.strftime('%Y%m%d')}"

    def client_uploads_today(self, client_id: str, day: datetime) -> int:
        d = _retry_C(lambda: self.pub.collection("quota").document(self._client_qid(client_id, day)).get())
        return (d.to_dict() or {}).get("uploads", 0) if d.exists else 0

    def bump_client_uploads(self, client_id: str, day: datetime, owner: str | None = None):
        data = {"client": (client_id or "")[:60], "uploads": firestore.Increment(1),
                "updated_at": datetime.now(timezone.utc).isoformat()}
        if owner:
            data["owner"] = owner
        _retry_C(lambda: self.pub.collection("quota").document(self._client_qid(client_id, day)).set(data, merge=True))

    def last_upload_at(self, channel: str, day: datetime, owner: str | None = None):
        c = self.get_counters(channel, day, owner)
        v = c.get("last_upload_at")
        return datetime.fromisoformat(v) if v else None

    # ---------- HÀNG ĐỢI ĐĂNG FB/IG ĐỘC LẬP (social_queue) (OWNED -> C) ----------
    def list_social_queue(self) -> list[dict]:
        """Item đang chờ đăng FB/IG độc lập (không gắn YouTube)."""
        q = self.pub.collection("social_queue").where("status", "in", ["pending", "processing"])
        out = []
        for d in _retry(lambda: list(q.stream())):
            row = d.to_dict(); row["id"] = d.id; out.append(row)
        return out

    def update_queue(self, doc_id: str, patch: dict):
        _retry_C(lambda: self.pub.collection("social_queue").document(doc_id).set(
            {**patch, "updated_at": datetime.now(timezone.utc).isoformat()}, merge=True))

    # ---------- HÀNG ĐỢI ĐĂNG YOUTUBE TỪ DRIVE (Content Hub, yt_queue) (OWNED -> C) ----------
    def list_yt_queue(self) -> list[dict]:
        q = self.pub.collection("yt_queue").where("status", "in", ["pending", "processing"])
        out = []
        for d in _retry(lambda: list(q.stream())):
            row = d.to_dict(); row["id"] = d.id; out.append(row)
        return out

    def update_yt_queue(self, doc_id: str, patch: dict):
        _retry_C(lambda: self.pub.collection("yt_queue").document(doc_id).set(
            {**patch, "updated_at": datetime.now(timezone.utc).isoformat()}, merge=True))

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
        return _retry_C(lambda: _claim(tx))   # 429 giữa transaction -> KHÔNG commit gì, retry an toàn (idempotent)

    # ---------- DOC tổng quát (settings/config, storage/pool ...) (SHARED -> A) ----------
    def get_doc(self, collection: str, doc_id: str) -> dict | None:
        # 24/8: lượt publish 11:50Z chết ngay ở dòng đầu `get_doc("settings","overrides")` — một doc
        # CẤU HÌNH TUỲ CHỌN, thiếu nó thì chạy mặc định là xong, mà lại đủ sức giết cả lượt đăng.
        # A cạn -> trả None và báo, để lượt đăng vẫn đi tiếp bằng gương/mặc định.
        try:
            d = _retry(lambda: self.db.collection(collection).document(doc_id).get())
        except Exception as e:
            if not _la_quota(e):
                raise
            print(f"   ⚠️ A cạn hạn mức — bỏ qua {collection}/{doc_id}, chạy bằng mặc định.")
            return None
        _dem("A", r=1)
        return d.to_dict() if d.exists else None

    def set_doc(self, collection: str, doc_id: str, data: dict):
        _retry(lambda: self.db.collection(collection).document(doc_id).set(
            {**data, "updated_at": datetime.now(timezone.utc).isoformat()}, merge=True))
        _dem("A", w=1)

    # ---------- CONNECTIONS (token do Cloudflare Worker ghi) (SHARED -> A) ----------
    #
    # 24/8 — THỦ PHẠM LÀM PROJECT A CẠN 50K ĐỌC/NGÀY (publish chết 11/12 lượt gần nhất, thoát ngay
    # ở lệnh đọc ĐẦU TIÊN với 429). Đo trên code + số lượt cron 24h qua:
    #   publish 36 lượt · publish_social 33 · thumbnail_requests 37 · guardian 20 · stats 4 ≈ 130 lượt
    #   mỗi lượt gọi list_connections 2-3 LẦN (main.py 451/452, publish_social 55/59/122,
    #   storage.py 195) — mỗi lần QUÉT LẠI cả bảng (~55 youtube / ~73 drive), CHƯA kể
    #   get_connection gọi RIÊNG cho từng kênh trong vòng lặp 55 kênh.
    #   => ~250-300 lượt đọc A cho MỘT lượt cron × 130 lượt/ngày ≈ 35-40K, cộng dashboard là vỡ trần.
    # Bản chất: connections CHỈ đổi khi người dùng bấm kết nối/ngắt (vài lần/tuần), mà ta đọc lại
    # hàng trăm lần mỗi ngày. Nay đệm THEO TIẾN TRÌNH: quét đúng 1 lần/loại/tiến trình, mọi lời gọi
    # sau ăn đệm; get_connection lấy từ chính đệm đó thay vì đọc doc lẻ.
    #   1 lượt publish: ~300 đọc -> ~128 đọc (giảm 57%), không đổi hành vi vì token không đổi giữa chừng.
    _CONN_CACHE: dict = {}

    def get_connection(self, channel: str, kind: str = "youtube") -> dict | None:
        """Đọc token đã kết nối qua dashboard. None nếu chưa kết nối.

        Lấy từ đệm danh sách nếu đã quét loại này rồi -> 0 lượt đọc thêm. Chưa quét thì đọc doc lẻ
        (rẻ hơn quét cả bảng khi chỉ cần 1 kênh)."""
        cached = self._CONN_CACHE.get(kind)
        if cached is not None:
            return cached.get(f"{channel}_{kind}")
        try:
            doc = _retry(lambda: self.db.collection("connections").document(f"{channel}_{kind}").get())
        except Exception as e:
            if not _la_quota(e):
                raise
            # A cạn -> kéo cả loại này từ gương một lần, các kênh sau ăn đệm (0 lượt đọc A)
            return (self._CONN_CACHE.get(kind) or {}).get(f"{channel}_{kind}") \
                if self._guong_connections(kind) else None
        _dem("A", r=1)
        return doc.to_dict() if doc.exists else None

    _GUONG_CHET: dict = {}      # loại -> đã biết gương hỏng ở lượt chạy này (xem _guong_connections)

    def _guong_connections(self, kind: str) -> list[dict]:
        """Đọc connection từ GƯƠNG ở project B khi A cạn hạn mức.

        24/8 — vì sao bắt buộc: chẩn đoán lượt publish 11:50Z in rõ `A ❌ CẠN · B còn · C còn`.
        Token YouTube/Facebook chỉ nằm ở A, nên A cạn là **không đăng được video nào** dù render
        vẫn chạy và kho Drive vẫn đẩy được (khâu Drive đã có gương từ 23/8). A trở thành điểm chết
        đơn của cả khâu đăng bài — đúng cảnh "render làm gì khi không đăng được".
        Gương `connections_mirror` do render plan chép mỗi phiên; rules B khoá kín nên token không
        lộ thêm. Đọc gương là 1 lượt quét trên B (B còn hạn mức) thay vì chết cứng."""
        # 24/8 tối — MỘT LƯỢT PUBLISH DỘI 112 LẦN VÀO ĐÂY. Log lượt 18:25Z có đúng 112 dòng
        # `⚠️ gương connections ở B cũng lỗi: 429`. Hàm này được gọi cho TỪNG kênh, và khi cả A lẫn
        # B đều cạn thì mỗi kênh lại đi hỏi B một lần nữa — 112 lượt đọc hỏng, mà **lượt hỏng vẫn bị
        # trừ hạn mức**, cộng thêm mấy vòng `_retry` 1,5s mỗi lượt. Hỏng vì cạn hạn mức là trạng thái
        # của CẢ TIẾN TRÌNH, biết một lần là đủ.
        if self._GUONG_CHET.get(kind):
            return []
        try:
            out = []
            for d in client_render_jobs().collection("connections_mirror").stream():
                c = d.to_dict() or {}
                if str(c.get("kind", "")) == kind and c.get("refresh_token"):
                    out.append({**c, "_id": d.id})
            if out:
                print(f"   🪞 A cạn — dùng GƯƠNG connections ở B: {len(out)} bản ghi '{kind}'.")
                self._CONN_CACHE[kind] = {r["_id"]: r for r in out}
            return out
        except Exception as e:
            self._GUONG_CHET[kind] = True
            print(f"   ⚠️ gương connections ở B cũng lỗi: {str(e)[:70]} "
                  f"— NGỪNG hỏi gương cho loại '{kind}' ở lượt chạy này (mỗi lượt hỏng vẫn bị trừ hạn mức).")
            return []

    def list_connections(self, kind: str, force: bool = False) -> list[dict]:
        """Danh sách connection theo loại (vd 'drive') do Worker ghi. ĐỆM 1 lần/tiến trình.

        force=True để đọc lại thật (dùng khi vừa ghi xong và cần thấy ngay)."""
        if not force and kind in self._CONN_CACHE:
            return list(self._CONN_CACHE[kind].values())
        q = self.db.collection("connections").where("kind", "==", kind)
        # kèm _id: cần doc id để ghi ngược trạng thái sức khoẻ (set_drive_health) mà KHÔNG phải đọc lại.
        try:
            rows = [{**(d.to_dict() or {}), "_id": d.id} for d in _retry(lambda: list(q.stream()))]
        except Exception as e:
            if not _la_quota(e):
                raise
            return self._guong_connections(kind)      # A cạn -> gương ở B, đừng chết cả lượt đăng
        self._CONN_CACHE[kind] = {r["_id"]: r for r in rows}
        _dem("A", r=max(1, len(rows)))
        return rows

    def set_drive_health(self, conn_id: str, owner: str, name: str, ok: bool, err: str = "", prev=None):
        """Ghi sức khoẻ 1 kho Drive — CHỈ KHI ĐỔI TRẠNG THÁI, nên bình thường tốn 0 lượt ghi.

        Vì sao cần: dashboard trước đây chỉ biết token sống/chết khi user tự bấm nút 🩺, nên kho bị
        thu hồi token vẫn hiện 'đã kết nối' (sự cố 21/8: 1 kho lỗi invalid_grant suốt nhiều giờ mà
        nhìn vào dashboard không thấy gì). Publisher vốn ĐÃ gọi Drive mỗi phiên -> tận dụng luôn kết
        quả đó làm health check, không tốn thêm 1 lệnh gọi API nào.

        prev = giá trị 'health' đọc được từ chính doc connection (đã nằm sẵn trong bộ nhớ) -> so sánh
        tại chỗ, khỏi đọc thêm. Giống nhau thì KHÔNG ghi gì."""
        new = "ok" if ok else "dead"
        if prev == new:
            return False
        at = datetime.now(timezone.utc).isoformat()
        patch = {"health": new, "health_at": at, "health_err": ("" if ok else str(err)[:200])}
        try:
            if conn_id:
                _retry(lambda: self.db.collection("connections").document(conn_id).set(patch, merge=True))
            # mirror sang storage_accounts — ĐÚNG doc dashboard đã đọc sẵn -> hiện trạng thái mà
            # dashboard không phải đọc thêm collection nào.
            if owner and name:
                _retry(lambda: self.db.collection("storage_accounts")
                       .document(f"{owner}__{name}").set(patch, merge=True))
        except Exception:
            return False
        return True

    # ---------- STATS (view / sub) ----------
    def set_channel_stats(self, channel: str, data: dict, owner: str | None = None):
        data = {**data, "channel": channel, "updated_at": datetime.now(timezone.utc).isoformat()}
        if owner:
            data["owner"] = owner
        docid = f"{owner}__{channel}" if owner else channel
        _retry(lambda: self.db.collection("channels").document(docid).set(data, merge=True))   # SHARED -> A (dashboard đọc)

    def set_channel_health(self, channel: str, data: dict, owner: str | None = None):
        """Ghi trạng thái KẾT NỐI/vận hành để trang 'Kết nối API' hiển thị realtime."""
        d = {**data, "channel": channel}
        if owner:
            d["owner"] = owner
        docid = f"{owner}__{channel}" if owner else channel
        _retry(lambda: self.db.collection("channels").document(docid).set(d, merge=True))      # SHARED -> A

    def all_counters_today(self, day: datetime) -> dict:
        """Bộ đếm đăng hôm nay của mọi kênh: {channel: {yt, fb}} (cho dashboard). (OWNED -> C)"""
        suffix = day.strftime("%Y%m%d")
        out = {}
        for d in _retry(lambda: list(self.pub.collection("counters").stream())):
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
        for d in _retry(lambda: list(q.stream())):
            r = d.to_dict()
            yid = ((r.get("results") or {}).get("youtube") or {}).get("id")
            if yid:
                out.append((d.id, yid))
        return out

    def set_video_stats(self, doc_id: str, stats: dict):
        _retry_C(lambda: self.pub.collection("videos").document(doc_id).set(
            {"stats": {**stats, "updated_at": datetime.now(timezone.utc).isoformat()}},
            merge=True,
        ))

def chan_doan_429() -> str:
    """CẠN QUOTA THÌ PHẢI BIẾT CẠN Ở ĐÂU (24/8).

    Trước đây publish chỉ in "Firestore hết hạn mức hôm nay (429 Quota exceeded)" — không nói project
    nào, nên mỗi lần sự cố lại mất hàng giờ đoán A hay B hay C. Ba project có ba trần riêng và ba
    thủ phạm khác nhau; đoán sai là sửa nhầm chỗ.
    Khi đã dính 429 thì thêm 3 lượt đọc thăm dò không đáng gì, mà đổi lại biết chính xác chỗ nghẽn.
    """
    ra = []
    for ten, fn in (("A (dashboard/keys/connections)", client),
                    ("B (render_jobs)", client_render_jobs),
                    ("C (yt_queue/videos)", client_publish)):
        try:
            fn().collection("_ping").document("probe").get()
            ra.append(f"   {ten}: còn hạn mức")
        except Exception as e:
            xau = "CẠN 429" if ("429" in str(e) or type(e).__name__ == "ResourceExhausted") else f"lỗi {str(e)[:40]}"
            ra.append(f"   {ten}: ❌ {xau}")
    return "\n".join(ra)
