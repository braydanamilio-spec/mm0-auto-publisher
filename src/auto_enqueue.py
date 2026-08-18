"""
auto_enqueue.py — TỰ ĐẨY video render xong vào yt_queue để ĐĂNG TỰ ĐỘNG.

AN TOÀN:
  - Mặc định TẮT. Chỉ chạy cho kênh có cờ auto_publish=true (settings/overrides.auto_publish[<kênh>]).
  - Kênh phải ĐÃ kết nối YouTube (connections/<owner>__<kênh>__youtube có refresh_token) -> chưa kết nối thì bỏ qua.
  - Dedup theo drive_file_id + cờ 'queued' trên render_jobs -> KHÔNG đăng trùng.
  - KHÔNG tự đặt giờ dồn: để publish_yt_queue lo trần (~6/ngày/kênh, 2/lần chạy) -> không spam.
Đọc render_jobs (video render xong, kèm drive_id/drive_account/type/title...) -> tạo item yt_queue 'pending'.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from firestore_state import State

SCAN_LIMIT = 40   # chỉ soi 40 job mới nhất/kênh mỗi lần -> chặn đọc phình (job cũ đã 'queued')


def _owner() -> str:
    return os.environ.get("OWNER_UID", "") or os.environ.get("OWNER", "")


def run(dry_run: bool = False):
    state = State(); db = state.db
    owner = _owner()
    if not owner:
        return
    # dashboard ghi vào settings/overrides__<uid> (per-user); fallback 'overrides' (single-tenant cũ)
    ov = state.get_doc("settings", "overrides__" + owner) or state.get_doc("settings", "overrides") or {}
    auto = ov.get("auto_publish") or {}                     # {"<TÊN kênh>": true/false}
    on_channels = {k for k, v in auto.items() if v}
    if not on_channels:
        return                                              # KHÔNG kênh nào bật -> im lặng, không làm gì

    def yt_ok(ch: str) -> bool:
        c = state.get_doc("connections", f"{owner}__{ch}__youtube")
        return bool(c and c.get("refresh_token"))
    ready = {ch for ch in on_channels if yt_ok(ch)}
    if not ready:
        print("  ⏸ auto-enqueue: có kênh bật nhưng CHƯA kết nối YouTube -> bỏ qua."); return

    # tập drive_file_id đã có trong yt_queue (mọi trạng thái) -> dedup
    queued_ids = set()
    try:
        for d in db.collection("yt_queue").where("owner", "==", owner).stream():
            fid = d.to_dict().get("drive_file_id")
            if fid:
                queued_ids.add(fid)
    except Exception as e:
        print(f"  ⚠️ auto-enqueue đọc yt_queue lỗi: {e}")

    now = datetime.now(timezone.utc)
    added = 0
    for ch in ready:
        try:
            q = (db.collection("render_jobs").where("owner", "==", owner)
                 .where("channel", "==", ch).where("status", "==", "done"))
            try:
                from google.cloud.firestore_v1 import Query
                docs = list(q.order_by("created_at", direction=Query.DESCENDING).limit(SCAN_LIMIT).stream())
            except Exception:
                docs = list(q.limit(SCAN_LIMIT).stream())    # thiếu index -> vẫn chạy (không sắp xếp)
        except Exception as e:
            print(f"  ⚠️ auto-enqueue đọc render_jobs {ch} lỗi: {e}"); continue

        for d in docs:
            j = d.to_dict()
            if j.get("queued"):
                continue
            fid = j.get("drive_id")
            if not fid or fid in queued_ids:
                if fid and not j.get("queued") and not dry_run:
                    db.collection("render_jobs").document(d.id).set({"queued": True}, merge=True)   # đã có trong queue -> đánh dấu
                continue
            item = {"owner": owner, "channel": ch, "drive_file_id": fid,
                    "drive_account": j.get("drive_account", ""), "type": j.get("type", "short"),
                    "title": j.get("title", ""),
                    "drive_name": j.get("drive_name") or ((j.get("title") or fid) + ".mp4"),
                    "description": j.get("description", ""), "hashtags": j.get("hashtags") or [],
                    "tags": j.get("tags") or [], "status": "pending", "attempts": 0,
                    "created_at": now.isoformat(), "publish_at": ""}
            if dry_run:
                print(f"  (dry) auto-enqueue {ch}: {(item['title'] or fid)[:44]}"); added += 1; continue
            db.collection("yt_queue").add(item)
            db.collection("render_jobs").document(d.id).set({"queued": True}, merge=True)
            queued_ids.add(fid); added += 1

    print(f"✔ auto-enqueue: thêm {added} video vào hàng đợi." if added else "✔ auto-enqueue: không có video mới.")


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
