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


def firestore_pool_accounts() -> list[dict]:
    """Tài khoản Drive đã 'Kết nối' qua dashboard (Firestore) — token do Worker ghi."""
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
        return out
    except Exception:
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
        try:
            root = acc["root"]
            held = max(_RESERVED.get(root, 0), shared.get(root, 0))   # local & chia-sẻ: lấy cái lớn hơn (tránh trễ Firestore)
            free = account_status(acc)["free_under_cap"] - held
        except Exception as e:
            print(f"  ⚠️  Không đọc được dung lượng {acc['name']}: {e}")
            continue
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
