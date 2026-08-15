"""
cleanup.py — DỌN DẸP file đã đăng trong hồ chứa, theo chế độ anh tích chọn (storage.yaml).

Chế độ (cleanup.mode):
  keep    -> không xoá gì (Google One giữ tất cả).
  delete  -> xoá hẳn file trong _POSTED cũ hơn keep_days (YouTube là backup; link đã lưu Firestore).
  archive -> tải file về rồi đẩy sang tài khoản BACKUP (kho lạnh) rồi xoá bản gốc để giải phóng chỗ.

Chạy:
  python src/cleanup.py                # theo policy + keep_days
  python src/cleanup.py --now          # dọn ngay, bỏ qua keep_days
  python src/cleanup.py --dry-run      # chỉ xem, không xoá/di chuyển
"""

from __future__ import annotations
import argparse
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
import storage as ST

IMG_EXT = (".jpg", ".jpeg", ".png")


def _age_days(f: dict, now: datetime) -> float:
    t = f.get("modifiedTime") or f.get("createdTime")
    if not t:
        return 1e9
    dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    return (now - dt).total_seconds() / 86400


def _companions(drv, parent_id: str, base: str) -> list[str]:
    ids = []
    for name in (f"{base}.json", *[f"{base}{e}" for e in IMG_EXT]):
        cid = drv.find_file(parent_id, name)
        if cid:
            ids.append(cid)
    return ids


def _mark_firestore(file_id: str, status: str):
    """Đánh dấu source_status trên Firestore (nếu có) để dashboard biết nguồn đã dọn."""
    try:
        from firestore_state import State
        State().upsert_video(file_id, {"source_status": status})
    except Exception:
        pass


def run(dry_run=False, force_now=False):
    cfg = ST.load_config()
    policy = cfg.get("cleanup", {})
    mode = policy.get("mode", "keep")
    keep_days = policy.get("keep_days", 14)

    # Báo cáo dung lượng pool -> dashboard (trang Kho lưu trữ)
    try:
        accts = ST.pool_accounts(cfg)
        if accts:
            from firestore_state import State
            report = []
            for acc in accts:
                try:
                    st = ST.account_status(acc)
                    report.append({"name": st["name"], "used": st["used"], "cap": st["cap"]})
                except Exception:
                    pass
            if report:
                State().set_doc("storage", "pool", {
                    "accounts": report, "cleanup_mode": mode,
                    "keep_days": keep_days, "trigger": policy.get("trigger", "auto")})
                print(f"  📊 Đã cập nhật dung lượng {len(report)} tài khoản lên dashboard.")
    except Exception as e:
        print(f"  (storage report skip: {e})")

    print(f"🧹 Cleanup mode = {mode} | keep_days = {keep_days}"
          f"{' | DRY-RUN' if dry_run else ''}{' | NGAY' if force_now else ''}")
    if mode == "keep":
        print("  → Giữ tất cả, không dọn gì. (Hợp với Google One)")
        return

    now = datetime.now(timezone.utc)
    accounts = ST.pool_accounts(cfg)
    if not accounts:
        print("  ⚠️  Chưa cấu hình tài khoản pool nào (thiếu secret). Bỏ qua.")
        return

    backup = ST.backup_account(cfg) if mode == "archive" else None
    backup_drv = ST.account_drive(backup) if backup else None
    backup_folder = None
    if backup_drv:
        backup_folder = backup_drv.child_folder(backup["root"], "_ARCHIVE")

    total_freed = 0
    for acc in accounts:
        drv = ST.account_drive(acc)
        posted = drv.list_folder_videos(acc["root"], "_POSTED")
        if not posted:
            continue
        print(f"\n  📦 {acc['name']}: {len(posted)} file trong _POSTED")
        for f in posted:
            age = _age_days(f, now)
            if not force_now and age < keep_days:
                continue
            base = f["name"].rsplit(".", 1)[0]
            parent = f["parents"][0]
            size = int(f.get("size", 0))
            comp = _companions(drv, parent, base)

            if dry_run:
                print(f"     • [{mode}] {f['name']} ({age:.0f} ngày, {size/1e6:.0f}MB)")
                continue

            if mode == "delete":
                drv.delete(f["id"])
                for c in comp:
                    drv.delete(c)
                _mark_firestore(f["id"], "deleted")
                total_freed += size
                print(f"     🗑️  Xoá {f['name']} (+{len(comp)} phụ)")

            elif mode == "archive" and backup_drv:
                tmp = os.path.join(tempfile.gettempdir(), f["name"])
                try:
                    drv.download(f["id"], tmp)
                    backup_drv.upload_file(backup_folder, tmp, f["name"])
                    drv.delete(f["id"])
                    for c in comp:
                        drv.delete(c)
                    _mark_firestore(f["id"], "archived")
                    total_freed += size
                    print(f"     📥 Archive {f['name']} → backup, đã xoá bản gốc")
                except Exception as e:
                    print(f"     ❌ Lỗi archive {f['name']}: {e}")
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)

    print(f"\n✔ Xong. Giải phóng ~{total_freed/1e9:.2f} GB.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", action="store_true", help="Dọn ngay, bỏ qua keep_days.")
    a = ap.parse_args()
    run(dry_run=a.dry_run, force_now=a.now)


if __name__ == "__main__":
    main()
