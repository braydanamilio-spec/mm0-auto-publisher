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
_A_CAN = {"den": 0.0}      # mốc thời gian NGỪNG hỏi project A (đặt khi A trả 429)


def _het_han_muc(e) -> bool:
    t = str(e).lower()
    return ("429" in t or "quota exceeded" in t or "resource_exhausted" in t
            or type(e).__name__ == "ResourceExhausted")
POOL_TTL = 1800    # giây (23/8: 10' -> 30'; danh sách kho gần như bất biến, kho mới nhận sau ≤30')


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
        _sd = _cl.collection("connections_mirror").document("__snap__").get()
        if not _sd.exists:
            return []
        _sx = _sd.to_dict() or {}
        return [{"name": c.get("channel", "drive"), "root": c["root"], "cap_gb": c.get("cap_gb", 14),
                 "owner": c.get("owner"), "email": c.get("email", ""),
                 "creds": {"client_id": c["client_id"], "client_secret": c["client_secret"],
                           "refresh_token": c["refresh_token"]}}
                for c in (_sx.get("accs") or [])
                if c.get("refresh_token") and c.get("root") and c.get("client_id")]

    def _b2_client():
        pid = os.environ.get("FIREBASE_PROJECT_ID_B2")
        sa = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_B")
        if not (pid and sa and os.path.exists(sa)):
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
                    continue
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
        _sd = client_render_jobs().collection("connections_mirror").document("__snap__").get()
        if _sd.exists:
            _sx = _sd.to_dict() or {}
            _out = [{"name": c.get("channel", "drive"), "root": c["root"], "cap_gb": c.get("cap_gb", 14),
                     "owner": c.get("owner"), "email": c.get("email", ""),
                     "creds": {"client_id": c["client_id"], "client_secret": c["client_secret"],
                               "refresh_token": c["refresh_token"]}}
                    for c in (_sx.get("accs") or [])
                    if c.get("refresh_token") and c.get("root") and c.get("client_id")]
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
                if c.get("refresh_token") and c.get("root"):
                    out.append({
                        "name": c.get("channel", "drive"),
                        "root": c["root"], "cap_gb": c.get("cap_gb", 14),
                        "owner": c.get("owner"), "email": c.get("email", ""),
                        "creds": {"client_id": c["client_id"], "client_secret": c["client_secret"],
                                  "refresh_token": c["refresh_token"]},
                    })
            _POOL_CACHE["at"], _POOL_CACHE["val"] = _t.time(), out
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
            if c.get("refresh_token") and c.get("root"):
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
            b2 = _fs.Client(project=os.environ.get("FIREBASE_PROJECT_ID_B2", "mm0-shard-b2"),
                            credentials=service_account.Credentials.from_service_account_file(key))
            rows = _mirror_rows(b2, "B2")
            if rows:
                return rows
    except Exception as e:
        print(f"   ⚠️ Gương kho B2 cũng lỗi: {str(e)[:70]}")
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
    if root in _DEAD_ACCS:
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
        else:
            print(f"  ⚠️  Không đọc được dung lượng {acc.get('name')}: {msg[:70]}")
        return None
    _STATUS_CACHE[root] = (_t.time(), free)
    return free


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
        import hashlib
        k = int(hashlib.md5(str(seed).encode()).hexdigest(), 16) % len(result)
        result = result[k:] + result[:k]   # xoay điểm bắt đầu theo kênh -> rải đều, song song không đụng nhau
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
