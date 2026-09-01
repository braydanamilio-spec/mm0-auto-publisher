"""
storage.py — Quản lý HỒ CHỨA nhiều tài khoản Drive free (pool) + kho lạnh backup.

- Mỗi tài khoản pool = 1 OAuth Drive riêng -> dùng ĐỦ 15GB của acc đó + XOÁ được file.
- Chọn tài khoản còn trống nhất (dưới cap_gb) để đẩy video mới vào.
- Cung cấp Drive client cho cleanup / enqueue.
"""

from __future__ import annotations
import os

import yaml

from drive_client import Drive

CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "storage.yaml")
GB = 1_000_000_000

# Dung lượng TẠM CHIẾM trong phiên chạy (drive.usage() cập nhật trễ sau upload).
# Nhờ vậy khi đẩy nhiều video liên tiếp, acc không bị chọn quá tay -> chia đều, không tràn.
_RESERVED: dict[str, int] = {}
_STATE = None
_RES_TTL_MIN = 30   # reservation quá 30' coi như job crash -> bỏ (tự dọn, không kẹt chỗ vĩnh viễn)


def _state():
    global _STATE
    if _STATE is None:
        from firestore_state import State
        _STATE = State()
    return _STATE


# CHIA SẺ reservation qua Firestore: TẮT mặc định để TIẾT KIỆM Firestore (100% free) — Drive mới 2% đầy + đã
# chia đều theo kênh nên gần như không có 2 luồng cùng nhét 1 kho gần đầy. Bật lại (SHARED_RESERVATION=1) khi kho gần đầy.
_SHARE_RES = os.environ.get("SHARED_RESERVATION") == "1"


def reserve(root: str, nbytes: int) -> None:
    """GIỮ CHỖ tạm (local trong phiên; chia sẻ Firestore chỉ khi bật _SHARE_RES)."""
    _RESERVED[root] = _RESERVED.get(root, 0) + int(nbytes)
    if not _SHARE_RES:
        return
    try:
        from datetime import datetime, timezone
        from google.cloud import firestore as _fs
        _state().db.collection("storage_reservations").document(root).set(
            {"bytes": _fs.Increment(int(nbytes)), "at": datetime.now(timezone.utc).isoformat()}, merge=True)
    except Exception:
        pass


def release(root: str, nbytes: int) -> None:
    _RESERVED[root] = max(0, _RESERVED.get(root, 0) - int(nbytes))
    if not _SHARE_RES:
        return
    try:
        from datetime import datetime, timezone
        from google.cloud import firestore as _fs
        _state().db.collection("storage_reservations").document(root).set(
            {"bytes": _fs.Increment(-int(nbytes)), "at": datetime.now(timezone.utc).isoformat()}, merge=True)
    except Exception:
        pass


def _shared_reservations() -> dict:
    """Reservation treo của các luồng khác (Firestore). TẮT -> {} (tiết kiệm read); ranked_accounts vẫn dùng live-usage + local."""
    if not _SHARE_RES:
        return {}
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_RES_TTL_MIN)).isoformat()
        out = {}
        for d in _state().db.collection("storage_reservations").stream():
            x = d.to_dict() or {}
            if (x.get("at") or "") >= cutoff:
                out[d.id] = max(0, x.get("bytes", 0) or 0)
        return out
    except Exception:
        return {}


def load_config() -> dict:
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve(acc: dict) -> dict | None:
    """Đổi *_env -> giá trị thật. Trả None nếu thiếu secret (bỏ qua acc đó)."""
    root = os.environ.get(acc["root_env"])
    cid = os.environ.get(acc["client_id_env"])
    csec = os.environ.get(acc["client_secret_env"])
    ref = os.environ.get(acc["refresh_token_env"])
    if not (root and cid and csec and ref):
        return None
    return {
        "name": acc.get("name", "acc"),
        "root": root,
        "cap_gb": acc.get("cap_gb", 14),
        "creds": {"client_id": cid, "client_secret": csec, "refresh_token": ref},
    }


_POOL_CACHE = {"at": 0.0, "val": None}
_RAI = {"n": 0}      # bộ đếm lượt đẩy -> rải kho không dồn một chỗ (xem ranked_accounts)
_A_CAN = {"den": 0.0}      # mốc thời gian NGỪNG hỏi project A (đặt khi A trả 429)


def _het_han_muc(e) -> bool:
    t = str(e).lower()
    return ("429" in t or "quota exceeded" in t or "resource_exhausted" in t
            or type(e).__name__ == "ResourceExhausted")
POOL_TTL = 1800    # giây (23/8: 10' -> 30'; danh sách kho gần như bất biến, kho mới nhận sau ≤30')


# ── BẢN GHI KHO HỎNG CẤU TRÚC: LOẠI TỪ GỐC, KHÔNG PHẢI "NGỦ 12H" (26/8/2026) ─────────────────
# Anh nhắc nhiều lần: ADISONDURHAM báo hỏng, em bảo đã xử lý, rồi nó vẫn báo. Đây là lý do:
#   • `_bao_kho_chet` cho kho chết NGỦ 12 TIẾNG rồi tự thử lại — đúng cho kho token hết hạn, vì
#     anh kết nối lại là nó sống, không phải nhớ xoá cờ;
#   • nhưng bản ghi ADISONDURHAM có `root: "undefined"` — chuỗi "undefined" lọt qua mọi bộ lọc
#     `if c.get("root")` vì nó TRUTHY. Thư mục đó không tồn tại và sẽ không bao giờ tồn tại.
#     Kết nối lại tạo bản ghi MỚI; bản hỏng nằm nguyên đó, cứ 12 tiếng lại được thử một lần, hỏng
#     một lần, ghi log một lần — vô hạn.
# Hỏng CẤU TRÚC khác hẳn hỏng TẠM THỜI: nó phải bị loại ngay từ khâu đọc danh sách, không được
# đếm là kho còn chỗ, không được tốn một lượt gọi Drive nào.
_ROOT_RAC = {"undefined", "null", "none", "nan", "false", "0", "-"}
_DA_KEU_ROOT_RAC = set()


def _root_xai_duoc(c: dict) -> bool:
    r = str(c.get("root") or "").strip()
    if r and r.lower() not in _ROOT_RAC:
        return True
    ten = str(c.get("channel") or c.get("name") or "?")
    if ten not in _DA_KEU_ROOT_RAC:
        _DA_KEU_ROOT_RAC.add(ten)
        print(f"  🧟 bản ghi kho '{ten}' hỏng cấu trúc (root={r!r}) — LOẠI HẲN, không tính là kho. "
              f"Muốn dùng lại thì xoá bản ghi này ở dashboard rồi Kết nối lại.")
    return False


def _trong_ho(c: dict) -> bool:
    """Bản ghi này có được tính vào HỒ KHO không.

    27/8 — cơ chế chống trùng ở connect-worker KHÔNG XOÁ bản ghi trùng (xoá thì video cũ ghi nhãn
    đó mất đường tra), mà rút chúng khỏi hồ bằng cờ `pool: false` + `trung_voi`. Cờ ấy chỉ có
    nghĩa nếu MỌI nơi đọc hồ đều tôn trọng nó — đánh dấu mà bên đọc vẫn tính thì vẫn đếm nhầm,
    vẫn tông vào bản ghi mang refresh_token chết, tức là không sửa được gì.
    Mặc định TÍNH: 93 bản ghi đang chạy chưa có trường này, và vắng trường không phải là bị loại.
    """
    return c.get("pool") is not False


def _hot(lenh: str, tham: dict, timeout: int = 20) -> dict:
    """Gọi Worker /api/hot — dùng chung cho đọc/ghi bộ nhớ D1. KHÔNG đụng Firestore."""
    import json as _json
    import os as _os
    import urllib.request as _u
    _k = _os.environ.get("HOT_KEY", "")
    if not _k:
        return {}
    _url = (_os.environ.get("HOT_URL")
            or "https://mm0-connect.adisondurham-ef1.workers.dev/api/hot")
    try:
        _req = _u.Request(_url, method="POST",
                          data=_json.dumps({"lenh": lenh, "tham": tham}).encode(),
                          headers={"content-type": "application/json", "x-hot-key": _k,
                                   # thiếu User-Agent -> Cloudflare chặn mã 1010, trả 403 y như sai khoá
                                   "user-agent": "MM0-Pipeline/1.0"})
        with _u.urlopen(_req, timeout=timeout) as _r:
            return _json.loads(_r.read().decode("utf-8", "ignore")) or {}
    except Exception:
        return {}


_D1_KEY = "kho_pool"


def _kho_tu_d1() -> list:
    """Danh sách kho lấy từ bảng `bo_nho` trong D1 — KHÔNG đụng Firestore một câu nào.

    ── VÌ SAO (1/9/2026) ───────────────────────────────────────────────────────────────────
    Anh: *"bữa e kêu có cách ko ảnh hưởng mà, tìm hướng tối ưu."* Đúng — và hôm nay đã trả giá
    cho việc chưa làm: Firestore cạn hạn mức đúng lúc bước đẩy kho chạy, `pool_accounts()` đọc
    rỗng, `enqueue.py` kết luận "chưa kết nối tài khoản kho nào", và **17 lượt render xong không
    lên được Drive** (buglog 7cw).

    `_kho_tu_kv()` sinh ra đúng cho tình huống này nhưng khoá KV `conn:` chỉ được ghi LÚC KẾT NỐI
    tài khoản, mà các kho của anh kết nối trước khi cơ chế KV ra đời (31/8) — nên nó rỗng, và
    đường sống cuối im lặng vô dụng.

    Đường này KHÔNG cần deploy Worker (không có token Cloudflare ở máy) vì lệnh `nho_ghi`/`nho_doc`
    đã có sẵn. Và nó TỰ ĐẦY: mỗi lần đọc Firestore thành công, danh sách được ghi lại vào D1
    (`_luu_kho_vao_d1`). Nghĩa là chỉ cần Firestore sống MỘT lần là từ đó không cần nó nữa.

    Hạn: danh sách có thể cũ vài giờ nếu anh vừa nối kho mới. Chấp nhận được — hàm gọi nó vốn đã
    đệm mười phút, và phần chọn kho nào còn trống vẫn hỏi dung lượng THẬT qua Drive API mỗi lần.
    """
    d = _hot("nho_doc", {"k": _D1_KEY})
    js = (d or {}).get("js") or ""
    if not js:
        return []
    try:
        import json as _json
        accs = _json.loads(js)
    except Exception:
        return []
    ra = [a for a in accs
          if a.get("root") and (a.get("creds") or {}).get("refresh_token")]
    if ra:
        print(f"   💾 KHO LẤY TỪ D1: {len(ra)} tài khoản (không đụng Firestore).")
    return ra


def _luu_kho_vao_d1(accs: list) -> None:
    """Ghi danh sách kho vào D1 để lần sau không cần Firestore. Hỏng thì im lặng bỏ qua."""
    if not accs:
        return
    try:
        import json as _json
        from datetime import datetime, timezone
        _hot("nho_ghi", {"k": _D1_KEY, "js": _json.dumps(accs),
                         "at": datetime.now(timezone.utc).isoformat()})
    except Exception:
        pass


def _kho_tu_kv() -> list:
    """Danh sách kho lấy từ KV của Worker — KHÔNG đụng Firestore một câu nào.

    31/8 — Trước đây khối này là "lớp cứu cuối", chỉ chạy sau khi đã thử Firestore A, gương B,
    rồi B2. Mà chú thích ngay đầu `firestore_pool_accounts` đã tự nói ra sự thật: đây là "chỗ
    ĐỐT QUOTA NẶNG NHẤT của cả hệ", đọc trọn collection ~70 kho, và ở đỉnh tải thì cạn hạn mức
    50K/ngày chỉ sau bốn tiếng.
    Nghĩa là hệ tiêu hạn mức để đi tìm một thứ mà KV đang giữ sẵn và cho không. Nay đảo lại:
    KV đi TRƯỚC, Firestore chỉ dùng khi KV không có gì.

    Đổi lại thì gì? KV có thể chậm hơn thực tế vài phút khi anh vừa kết nối một kho mới. Nhưng
    hàm này vốn đã đệm mười phút, tức đã chấp nhận đúng độ trễ ấy từ đầu — nên không mất gì
    thêm, mà đổi được việc số liệu kho luôn đọc được kể cả khi Firestore cạn.
    """
    # Sự cố 16:0x — 26 video render xong đều mang bước "chưa đẩy Drive": A cạn, gương B cạn,
    # B2 cũ -> trắng tay -> enqueue hiểu là "không có kho nào" -> video nằm lại trong artifact.
    # Nhưng Worker CÓ bản sao thẻ kết nối trong KV, và KV **không đụng Firestore một câu nào**.
    # Đây là đường sống cuối cùng, chỉ dùng khi mọi đường Firestore đã tắt.
    try:
        import json as _json
        import urllib.request as _u
        _k = os.environ.get("HOT_KEY", "")
        if _k:
            _req = _u.Request(
                (os.environ.get('HOT_URL') or 'https://mm0-connect.adisondurham-ef1.workers.dev/api/hot')
                .replace("/api/hot", "/api/drive-pool"),
                method="POST", data=b"{}",
                headers={"content-type": "application/json", "x-hot-key": _k,
                         # thiếu User-Agent thì Cloudflare chặn mã 1010, trả 403 y như sai khoá
                         "user-agent": "MM0-Pipeline/1.0"})
            with _u.urlopen(_req, timeout=20) as _r:
                _d = _json.loads(_r.read().decode("utf-8", "ignore")) or {}
            _accs = _d.get("accounts") or []
            if _accs:
                print(f"   🆘 KHO LẤY TỪ KV CỦA WORKER: {len(_accs)} tài khoản "
                      f"(Firestore tắt cả 3 đường — đây là lớp cứu cuối, KHÔNG đụng Firestore).")
                _POOL_CACHE["at"], _POOL_CACHE["val"] = _t.time(), _accs
                return _accs
    except Exception:
        pass
    return []


def firestore_pool_accounts() -> list[dict]:
    """Tài khoản Drive đã 'Kết nối' qua dashboard (Firestore) — token do Worker ghi.

    CÓ ĐỆM 10 PHÚT — đây là chỗ ĐỐT QUOTA NẶNG NHẤT của cả hệ (phát hiện 21/8):
    hàm này đọc TOÀN BỘ collection connections (~70 kho Drive), mà nó nằm trong đường enqueue nên
    được gọi MỖI LẦN ĐẨY 1 VIDEO. Ở đỉnh 172 video/giờ = ~12.000 lượt đọc/giờ -> cạn hạn mức
    50K/ngày của Project A chỉ sau ~4 tiếng. Đúng thứ làm publish + render chết ngày 20/8.

    Danh sách kho gần như không đổi trong một phiên render, nên đệm 10 phút là an toàn tuyệt đối:
    kho mới kết nối chậm nhất 10 phút là nhận ra. Phần chọn kho nào còn trống vẫn hỏi dung lượng
    THẬT qua Drive API mỗi lần (không đệm) -> KHÔNG ảnh hưởng việc rải video hay phát hiện kho đầy."""
    import time as _t
    if _POOL_CACHE["val"] is not None and (_t.time() - _POOL_CACHE["at"]) < POOL_TTL:
        return _POOL_CACHE["val"]
    # ══ KV TRƯỚC, FIRESTORE SAU ════════════════════════════════════════════════════════════
    # Anh hỏi vì sao suốt ngày cạn hạn mức. Đây là câu trả lời: hàm này đọc trọn collection
    # ~70 kho và nằm trong đường đẩy video, nên ở đỉnh tải nó một mình đốt hết 50K lượt đọc/ngày.
    # Mà bản sao danh sách ấy vẫn nằm trong KV của Worker, đọc không tốn hạn mức Firestore nào.
    # Nên hỏi KV trước; chỉ khi KV trống mới đụng tới Firestore.
    _kv = _kho_tu_kv()
    if _kv:
        _POOL_CACHE["at"], _POOL_CACHE["val"] = _t.time(), _kv
        return _kv
    # D1 SAU KV, TRƯỚC FIRESTORE. Cả hai đều không đụng hạn mức Firestore; D1 có lợi thế là nó
    # TỰ ĐẦY từ lần đọc Firestore thành công gần nhất, nên nó có dữ liệu kể cả với những kho nối
    # trước khi cơ chế KV ra đời — đúng lỗ hổng đã làm 17 lượt render không đẩy được kho hôm nay.
    _d1 = _kho_tu_d1()
    if _d1:
        _POOL_CACHE["at"], _POOL_CACHE["val"] = _t.time(), _d1
        return _d1
    # 22/8 tối: Firestore A nghẽn 1 nhịp -> except trả [] -> enqueue hiểu là "0 kho" -> 9 video
    # EMPIREUSA QC 98 vừa render xong bị TỪ CHỐI đẩy Drive (mất trắng công render). Lỗi mạng/quota
    # KHÔNG BAO GIỜ được dịch thành "không có kho": thử lại 2 lần (8s/25s — enqueue chỉ chạy 1
    # lần/video nên đợi rẻ hơn nhiều so với vứt video), vẫn lỗi thì dùng ĐỆM CŨ dù quá hạn TTL
    # (danh sách kho gần như không đổi trong phiên); chỉ khi cả đời tiến trình chưa từng đọc được
    # mới đành trả [].
    # ĐỌC SNAPSHOT 1-DOC TRƯỚC (23/8 — cắt ~14.000 đọc/phiên trên A): render plan gói cả danh sách
    # kho vào connections_mirror/__snap__ ở B. 1 lượt đọc thay vì quét 70 doc, lại KHÔNG đụng A.
    # 23/8 chiều: B CŨNG cạn quota đọc -> lượt đọc snapshot này ném 429 -> rơi xuống nhánh A (cũng
    # cạn) -> enqueue nhận [] -> "Không kho nào đủ chỗ" -> render xong mà vứt. Nên thử B TRƯỚC, hụt
    # thì thử thẳng B2 (bản gương, plan cập nhật mỗi phiên) trước khi đụng A.
    def _snap_from(_cl):
        _sd = _cl.collection("connections_mirror").document("snap_kho").get()
        if not _sd.exists:
            return []
        _sx = _sd.to_dict() or {}
        return [{"name": c.get("channel", "drive"), "root": c["root"], "cap_gb": c.get("cap_gb", 14),
                 "owner": c.get("owner"), "email": c.get("email", ""),
                 "creds": {"client_id": c["client_id"], "client_secret": c["client_secret"],
                           "refresh_token": c["refresh_token"]}}
                for c in (_sx.get("accs") or [])
                if c.get("refresh_token") and _root_xai_duoc(c) and _trong_ho(c) and c.get("client_id")]

    def _b2_client():
        # 24/8 tối — B2 BỊ BỎ QUA IM LẶNG. Bước "Sao lưu kho key" của job plan không truyền
        # `FIREBASE_PROJECT_ID_B2`, nên hàm này trả None và vòng lặp `continue` KHÔNG in gì; log chỉ
        # có đúng một dòng "đọc danh sách kho ở B hụt" rồi thẳng tới "❌ không đọc được kho Drive
        # nào". Nhìn log thì tưởng đã thử cả B2. Mà ngay trong FILE NÀY, khối B2 phía dưới lại
        # MẶC ĐỊNH "mm0-shard-b2" khi thiếu env — hai chỗ cùng việc, hai hành vi khác nhau.
        pid = os.environ.get("FIREBASE_PROJECT_ID_B2") or "mm0-shard-b2"
        sa = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_B")
        if not (sa and os.path.exists(sa)):
            print("   ⚠️ B2: thiếu GOOGLE_APPLICATION_CREDENTIALS_B -> bỏ qua đường dự phòng B2.")
            return None
        from google.cloud import firestore as _fs
        from google.oauth2 import service_account as _sa
        return _fs.Client(project=pid, credentials=_sa.Credentials.from_service_account_file(sa))

    for _which in ("B", "B2"):
        try:
            if _which == "B":
                from firestore_state import client_render_jobs
                _cl = client_render_jobs()
            else:
                _cl = _b2_client()
                if _cl is None:
                    continue          # lý do đã in trong _b2_client, không im lặng nữa
            _out = _snap_from(_cl)
            if _out:
                if _which == "B2":
                    print(f"   🔀 danh sách kho lấy từ B2 (B nghẽn) — {len(_out)} kho")
                _POOL_CACHE["at"], _POOL_CACHE["val"] = _t.time(), _out
                return _out
        except Exception as e:
            print(f"   ⚠️ đọc danh sách kho ở {_which} hụt ({str(e)[:60]})")

    try:
        from firestore_state import client_render_jobs
        _sd = client_render_jobs().collection("connections_mirror").document("snap_kho").get()
        if _sd.exists:
            _sx = _sd.to_dict() or {}
            _out = [{"name": c.get("channel", "drive"), "root": c["root"], "cap_gb": c.get("cap_gb", 14),
                     "owner": c.get("owner"), "email": c.get("email", ""),
                     "creds": {"client_id": c["client_id"], "client_secret": c["client_secret"],
                               "refresh_token": c["refresh_token"]}}
                    for c in (_sx.get("accs") or [])
                    if c.get("refresh_token") and _root_xai_duoc(c) and _trong_ho(c) and c.get("client_id")]
            if _out:
                _POOL_CACHE["at"], _POOL_CACHE["val"] = _t.time(), _out
                return _out
    except Exception:
        pass
    last = None
    # 24/8 — ĐỆM ÂM: KHI A ĐÃ CẠN THÌ ĐỪNG ĐẬP VÀO A NỮA.
    # Đo ở phiên 08:47: mọi luồng in `🪞 A nghẽn — dùng GƯƠNG kho ở B` ngay từ 09:18, tức A chết
    # chưa đầy 90 phút sau mốc reset. Nhưng code vẫn thử lại A: mỗi luồng, mỗi 30' (hết TTL đệm),
    # 3 lần liên tiếp × ~73 doc = 18 luồng × 5 lần × 3 × 73 ≈ 20.000 LƯỢT ĐỌC HỎNG mỗi phiên, cộng
    # 33s ngủ chờ mỗi vòng. Lượt đọc hỏng vì hết hạn mức VẪN TÍNH VÀO hạn mức -> hệ tự đập cho A
    # chết sâu hơn, và sáng hôm sau vừa reset là bị đốt lại ngay.
    # Nay: hễ biết A cạn thì cả tiến trình đi thẳng sang GƯƠNG ở B, không thử lại A nữa.
    _thu_A = not (_A_CAN["den"] and _t.time() < _A_CAN["den"])
    if not _thu_A:
        last = "A đang trong 30' nghỉ (vừa cạn hạn mức) — đi thẳng sang đệm/gương"
    for wait in ((0, 8, 25) if _thu_A else ()):
        if wait:
            _t.sleep(wait)
        try:
            from firestore_state import State
            out = []
            for c in State().list_connections("drive"):
                if c.get("refresh_token") and _root_xai_duoc(c) and _trong_ho(c):
                    out.append({
                        "name": c.get("channel", "drive"),
                        "root": c["root"], "cap_gb": c.get("cap_gb", 14),
                        "owner": c.get("owner"), "email": c.get("email", ""),
                        "creds": {"client_id": c["client_id"], "client_secret": c["client_secret"],
                                  "refresh_token": c["refresh_token"]},
                    })
            _POOL_CACHE["at"], _POOL_CACHE["val"] = _t.time(), out
            # GHI LẠI VÀO D1: đọc Firestore được MỘT lần là từ đó không cần nó nữa. Đây là chỗ
            # bịt lỗ hổng đã làm 17 lượt render không đẩy được kho hôm nay (buglog 7cw).
            _luu_kho_vao_d1(out)
            return out
        except Exception as e:
            last = e
            if _het_han_muc(e):
                # cạn hạn mức thì thử lại chỉ tổ đốt thêm — nghỉ A 30' rồi dùng gương
                _A_CAN["den"] = _t.time() + 1800
                print("   ⛔ A đã cạn hạn mức — ngừng hỏi A 30 phút, dùng gương ở B.")
                break
    if _POOL_CACHE["val"] is not None:
        print(f"   ⚠️ Đọc danh sách kho lỗi ({str(last)[:60]}) — dùng đệm cũ {len(_POOL_CACHE['val'])} kho.")
        return _POOL_CACHE["val"]
    # 23/8: A cạn quota đọc CẢ NGÀY (không phải 1 nhịp) -> retry vô ích. Cứu cánh cuối: GƯƠNG
    # connections_mirror ở B (render plan chép sang mỗi phiên khi A còn thở; rules B khóa kín, chỉ
    # service account đọc). B sống độc lập A -> khâu đẩy kho hết điểm-chết-đơn.
    def _mirror_rows(db, label):
        out = []
        for d in db.collection("connections_mirror").stream():
            c = d.to_dict() or {}
            if c.get("refresh_token") and _root_xai_duoc(c) and _trong_ho(c):
                out.append({
                    "name": c.get("channel", "drive"),
                    "root": c["root"], "cap_gb": c.get("cap_gb", 14),
                    "owner": c.get("owner"), "email": c.get("email", ""),
                    "creds": {"client_id": c["client_id"], "client_secret": c["client_secret"],
                              "refresh_token": c["refresh_token"]},
                })
        if out:
            print(f"   🪞 A nghẽn — dùng GƯƠNG kho ở {label}: {len(out)} tài khoản.")
            _POOL_CACHE["at"], _POOL_CACHE["val"] = _t.time(), out
            _luu_kho_vao_d1(out)      # gương đọc được cũng ghi vào D1
        return out
    try:
        from firestore_state import client_render_jobs
        rows = _mirror_rows(client_render_jobs(), "B")
        if rows:
            return rows
    except Exception as e:
        print(f"   ⚠️ Gương kho B cũng lỗi: {str(e)[:70]}")
    # 23/8: B cạn nốt -> thử B2 dự phòng (mm0-shard-b2, cùng service account B — pipeline gương sẵn)
    try:
        key = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_B")
        if key and os.path.exists(key):
            from google.oauth2 import service_account
            from google.cloud import firestore as _fs
            b2 = _fs.Client(project=(os.environ.get('FIREBASE_PROJECT_ID_B2') or 'mm0-shard-b2'),
                            credentials=service_account.Credentials.from_service_account_file(key))
            rows = _mirror_rows(b2, "B2")
            if rows:
                return rows
    except Exception as e:
        print(f"   ⚠️ Gương kho B2 cũng lỗi: {str(e)[:70]}")
    # (khối KV đã tách thành `_kho_tu_kv()` và chạy TRƯỚC Firestore)

    except Exception as _e:
        print(f"   ⚠️ lớp cứu KV cũng hụt: {str(_e)[:70]}")
    print(f"   ⚠️ Đọc danh sách kho lỗi và chưa có đệm: {str(last)[:80]}")
    return []


def pool_accounts(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_config()
    out = []
    seen = set()
    # 1) tài khoản khai báo trong storage.yaml (env)
    for acc in cfg.get("pool", []):
        r = _resolve(acc)
        if r:
            out.append(r)
            seen.add(r["root"])
    # 2) tài khoản kết nối qua dashboard (Firestore) — không trùng
    for r in firestore_pool_accounts():
        if r["root"] not in seen:
            out.append(r)
            seen.add(r["root"])
    return out


def backup_account(cfg: dict | None = None) -> dict | None:
    cfg = cfg or load_config()
    b = cfg.get("backup", {})
    if not b.get("enabled"):
        return None
    return _resolve(b)


def account_drive(acc: dict) -> Drive:
    return Drive.from_oauth(acc["creds"])


def account_status(acc: dict) -> dict:
    """Dung lượng thực của 1 tài khoản (dùng cho dashboard/report)."""
    drv = account_drive(acc)
    u = drv.usage()
    cap = acc["cap_gb"] * GB
    used = u["used"]
    return {
        "name": acc["name"], "used": used, "limit": u["limit"], "cap": cap,
        "free_under_cap": max(0, cap - used), "pct": round(used / cap * 100, 1) if cap else 0,
    }


_STATUS_CACHE = {}     # root -> (thời điểm, free_under_cap)
_DEAD_ACCS = {}        # root -> lý do (token hỏng) — bỏ qua tới hết tiến trình
STATUS_TTL = 300       # giây


def _free_cached(acc):
    """Dung lượng còn trống của 1 kho, CÓ ĐỆM 5 PHÚT.

    ranked_accounts() gọi account_status() cho TỪNG kho, mà mỗi lần là 1 lượt gọi Google Drive
    (kèm refresh token). Với ~70 kho và hàm này nằm trong đường enqueue -> MỖI VIDEO tốn ~70 lượt
    gọi Drive; ở đỉnh 172 video/giờ là hơn 12.000 lượt/giờ, thừa sức chạm trần API của Google và
    làm mỗi lần đẩy video chậm hàng chục giây.

    Đệm 5 phút an toàn vì phần ĐANG BAY đã được trừ riêng qua _RESERVED/_shared_reservations —
    tức vẫn không thể nhét quá dung lượng kho dù số đọc được hơi cũ.

    Kho có token HỎNG (invalid_grant) bị nhớ lại và BỎ QUA hẳn: trước đây mỗi video lại thử lại
    kho chết đó một lần, vừa phí vừa rác log (đúng trường hợp kho ADISONDURHAM ngày 20-21/8)."""
    import time as _t
    root = acc["root"]
    if root in _DEAD_ACCS or root in _kho_chet_chung():
        return None
    hit = _STATUS_CACHE.get(root)
    if hit and (_t.time() - hit[0]) < STATUS_TTL:
        return hit[1]
    try:
        free = account_status(acc)["free_under_cap"]
    except Exception as e:
        msg = str(e)
        if "invalid_scope" in msg:
            # 23/8: lỗi CẤU HÌNH của chính mình (code đổi scope ≠ scope của token đang lưu) — KHÔNG
            # phải kho hỏng. Trước đây rơi vào nhánh "không đọc được" rồi im lặng bỏ kho -> cả 70 kho
            # biến mất, mọi video bị từ chối đẩy mà log chỉ ghi "không kho nào đủ chỗ".
            print(f"  🚨 SCOPE SAI ({acc.get('name')}): code đang xin scope KHÁC với token đã lưu -> "
                  f"MỌI kho sẽ chết. Sửa DRIVE_SCOPES/worker về đúng scope cũ rồi chạy lại. {msg[:90]}")
            return None
        if "invalid_grant" in msg or "expired or revoked" in msg or "unauthorized" in msg.lower():
            _DEAD_ACCS[root] = msg[:80]
            print(f"  ⛔ kho {acc.get('name')}: token hỏng -> BỎ QUA hẳn phiên này (cần kết nối lại)")
            _bao_kho_chet(root, acc.get("name", ""), msg)
        else:
            print(f"  ⚠️  Không đọc được dung lượng {acc.get('name')}: {msg[:70]}")
        return None
    _STATUS_CACHE[root] = (_t.time(), free)
    return free


# ── KHO TOKEN CHẾT: NHỚ CHUNG GIỮA MỌI TIẾN TRÌNH (25/8/2026) ────────────────────────────────
# Anh chỉ ra: `⚠️ kho ADISONDURHAM hụt: invalid_grant` rồi NGAY SAU đó `✅ đã cất ở kho ADISONDURHAM`
# — tức tài khoản VẪN SỐNG, chỉ là đang có HAI bản ghi cùng tên và một bản mang refresh_token cũ.
# `_DEAD_ACCS` chỉ nhớ trong MỘT tiến trình, nên mỗi lane/mỗi lượt publish lại đi tông vào bản chết
# một lần nữa: rác log, chậm, và mỗi lượt hỏng vẫn tính vào hạn mức Google.
# Ghi vào D1 qua đúng lệnh `key_nghi_ghi` đã có sẵn (không cần đổi bảng, không cần deploy Worker).
# Nghỉ 12 tiếng rồi tự thử lại — anh kết nối lại là nó tự sống, không phải nhớ xoá cờ.
_KHO_CHET_CACHE = {"at": 0.0, "val": set()}


def _hot():
    import importlib
    return importlib.import_module("hot_db")


def _bao_kho_chet(root: str, ten: str, msg: str) -> None:
    try:
        import datetime as _d
        den = (_d.datetime.now(_d.timezone.utc) + _d.timedelta(hours=12)).isoformat()
        _hot().key_nghi_ghi(f"kho:{root}", "token_chet", den)
        print(f"     📣 đã báo chung: bản ghi kho {ten} có token hỏng -> mọi tiến trình bỏ qua 12h "
              f"(kết nối lại là tự sống).")
    except Exception:
        pass          # chưa bật D1 -> giữ nguyên hành vi cũ (nhớ trong tiến trình)


def _kho_chet_chung() -> set:
    import time as _t
    if _t.time() - _KHO_CHET_CACHE["at"] < 300:
        return _KHO_CHET_CACHE["val"]
    ra = set()
    try:
        import datetime as _d
        gio = _d.datetime.now(_d.timezone.utc).isoformat()
        for r in (_hot().key_nghi_doc(gio) or []):
            k = str(r.get("kid") or "")
            if k.startswith("kho:"):
                ra.add(k[4:])
    except Exception:
        pass
    _KHO_CHET_CACHE["at"], _KHO_CHET_CACHE["val"] = _t.time(), ra
    return ra


def ranked_accounts(need_bytes: int = 0, cfg: dict | None = None,
                    owner: str | None = None, seed: str | None = None) -> list[tuple[dict, int]]:
    """
    Danh sách tài khoản pool ĐỦ CHỖ cho need_bytes, sắp theo free giảm dần.
    -> caller thử acc đầu; nếu upload lỗi/đầy thì nhảy acc kế (liền mạch, không kẹt).
    Đọc dung lượng THẬT mỗi acc (America 15GB free hay Google One đều đúng).
    owner != None: chỉ lấy acc của user đó (multi-tenant, tránh chồng chéo giữa user).
    seed != None (vd tên kênh): XOAY danh sách theo seed -> mỗi kênh bắt đầu ở 1 kho KHÁC nhau
      -> chạy SONG SONG không dồn 1 kho (đỡ spam API 1 kho, rải đều, giảm rủi ro, đăng sau không nghẽn).
    """
    shared = _shared_reservations()   # phần ĐANG BAY của các luồng khác -> trừ trước để không tràn
    scored = []
    for acc in pool_accounts(cfg):
        if owner and acc.get("owner") and acc["owner"] != owner:
            continue
        root = acc["root"]
        base = _free_cached(acc)          # có đệm 5' + bỏ hẳn kho token hỏng
        if base is None:
            continue
        held = max(_RESERVED.get(root, 0), shared.get(root, 0))   # local & chia-sẻ: lấy cái lớn hơn (tránh trễ Firestore)
        free = base - held
        scored.append((acc, max(0, free)))
    scored.sort(key=lambda x: -x[1])
    result = [(a, f) for (a, f) in scored if f >= max(0, need_bytes)]
    if seed and len(result) > 1:
        # 24/8 — XOAY THEO KÊNH **CỘNG** SỐ LẦN ĐẨY, không chỉ theo tên kênh.
        # Bản cũ băm mỗi tên kênh -> mỗi kênh LUÔN bắt đầu ở đúng MỘT vị trí, đời đời không đổi.
        # Đo thật: 55 kênh chỉ rơi vào 35 vị trí trong 72 -> **37 kho không kênh nào chạm tới**,
        # và kênh đẻ nhiều video thì mọi video dồn quanh cùng một chỗ. Số liệu khớp: kho nhiều nhất
        # 119 file, kho ít nhất (khác 0) 18 file — chênh 6,6 lần, 3 kho còn nguyên 0 file.
        # Thêm bộ đếm lượt đẩy của tiến trình: mỗi lần đẩy nhích một bước -> cùng một kênh cũng rải
        # ra nhiều kho. Vẫn giữ phần băm-theo-kênh để 18 luồng song song không cùng đâm vào một kho
        # tại cùng thời điểm (đó là lý do gốc của seed).
        import hashlib
        _RAI["n"] += 1
        k = (int(hashlib.md5(str(seed).encode()).hexdigest(), 16) + _RAI["n"]) % len(result)
        result = result[k:] + result[:k]
    return result


def pick_upload_account(min_free_bytes: int = 500 * 1_000_000,
                        cfg: dict | None = None,
                        owner: str | None = None) -> tuple[dict, Drive] | None:
    """Chọn tài khoản pool còn trống nhiều nhất (còn tương thích chỗ gọi cũ)."""
    ranked = ranked_accounts(min_free_bytes, cfg, owner)
    if not ranked:
        return None
    best = ranked[0][0]
    return best, account_drive(best)


def status_report(cfg: dict | None = None) -> list[dict]:
    """Trạng thái toàn hồ chứa (cho lệnh xem nhanh)."""
    rep = []
    for acc in pool_accounts(cfg):
        try:
            rep.append(account_status(acc))
        except Exception as e:
            rep.append({"name": acc["name"], "error": str(e)})
    return rep
