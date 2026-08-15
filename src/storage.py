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


def reserve(root: str, nbytes: int) -> None:
    _RESERVED[root] = _RESERVED.get(root, 0) + int(nbytes)


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
                    owner: str | None = None) -> list[tuple[dict, int]]:
    """
    Danh sách tài khoản pool ĐỦ CHỖ cho need_bytes, sắp theo free giảm dần.
    -> caller thử acc đầu; nếu upload lỗi/đầy thì nhảy acc kế (liền mạch, không kẹt).
    Đọc dung lượng THẬT mỗi acc (America 15GB free hay Google One đều đúng).
    owner != None: chỉ lấy acc của user đó (multi-tenant, tránh chồng chéo giữa user).
    """
    scored = []
    for acc in pool_accounts(cfg):
        if owner and acc.get("owner") and acc["owner"] != owner:
            continue
        try:
            free = account_status(acc)["free_under_cap"] - _RESERVED.get(acc["root"], 0)
        except Exception as e:
            print(f"  ⚠️  Không đọc được dung lượng {acc['name']}: {e}")
            continue
        scored.append((acc, max(0, free)))
    scored.sort(key=lambda x: -x[1])
    return [(a, f) for (a, f) in scored if f >= max(0, need_bytes)]


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
