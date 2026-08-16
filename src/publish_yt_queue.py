"""
publish_yt_queue.py — Đăng YouTube từ HÀNG ĐỢI CLOUD (Content Hub).

User duyệt Drive trên dashboard -> chọn video -> yt_queue (Firestore).
Cron này (gọi trong main.py) TẢI video từ Drive + ĐĂNG lên YouTube — KHÔNG cần script local.

An toàn:
  - Chỉ đăng item ĐÃ tới giờ (publish_at <= now); catch-up nếu cron trễ.
  - Idempotent: đã có results.youtube.id -> bỏ qua (chống trùng).
  - Lỗi TẠM -> giữ pending, cron sau retry (tối đa 3 lần) rồi mới failed.
  - Quota hết -> để mai (giữ pending).
  - Xoá file tạm sau khi đăng.
"""
from __future__ import annotations
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import storage as ST
import youtube_uploader as YT
import metadata as M
from firestore_state import State


def run(dry_run: bool = False):
    state = State()
    now = datetime.now(timezone.utc)
    try:
        items = state.list_yt_queue()
    except Exception as e:
        print(f"  ⚠️ yt_queue: {e}")
        return
    if not items:
        return
    drive_conns = state.list_connections("drive")

    for it in items:
        pa = it.get("publish_at")
        if pa:
            try:
                dt = datetime.fromisoformat(str(pa).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt > now:
                    continue   # chưa tới giờ
            except Exception:
                pass
        owner, slug = it.get("owner"), it.get("channel")
        fid, acct = it.get("drive_file_id"), it.get("drive_account")
        if not (owner and slug and fid):
            state.update_yt_queue(it["id"], {"status": "failed", "error": "thiếu dữ liệu"}); continue

        conn = state.get_doc("connections", f"{owner}__{slug}__youtube")
        if not conn or not conn.get("refresh_token"):
            state.update_yt_queue(it["id"], {"status": "failed", "error": "Kênh chưa kết nối YouTube"}); continue

        results = dict(it.get("results") or {})
        if results.get("youtube", {}).get("id"):
            state.update_yt_queue(it["id"], {"status": "posted", "results": results}); continue

        owner_drives = [c for c in drive_conns if c.get("owner") == owner]
        dc = next((c for c in owner_drives if acct and (c.get("name") == acct or c.get("email") == acct)), None) \
            or (owner_drives[0] if owner_drives else None)
        if not dc:
            state.update_yt_queue(it["id"], {"status": "failed", "error": "không thấy Drive account"}); continue

        meta = M.build_metadata({"topic": it.get("title") or it.get("drive_name") or "video",
                                 "type": it.get("type") or "long",
                                 "title": it.get("title") or "", "description": it.get("description") or "",
                                 "hashtags": it.get("hashtags") or [], "tags": it.get("tags") or []},
                                {"hashtags": it.get("hashtags") or []})
        ytc = {"privacy": it.get("privacy") or "public",
               "made_for_kids": bool(it.get("made_for_kids")),
               "category_id": it.get("category") or "22",
               "default_language": it.get("language") or None}

        if dry_run:
            print(f"  (dry) yt_queue {fid} -> {slug} [{ytc['privacy']}]"); continue

        print(f"  🚀 YT-queue: {it.get('drive_name') or fid} -> {slug}")
        creds = {"client_id": conn["client_id"], "client_secret": conn["client_secret"],
                 "refresh_token": conn["refresh_token"]}
        safe_name = "".join(c if (c.isalnum() or c in "._-") else "_" for c in (it.get("drive_name") or (fid + ".mp4")))
        tmp = os.path.join(tempfile.gettempdir(), safe_name)
        attempts = it.get("attempts", 0) + 1
        try:
            drv = ST.Drive.from_oauth({"client_id": dc["client_id"], "client_secret": dc["client_secret"],
                                       "refresh_token": dc["refresh_token"]})
            drv.download(fid, tmp)
            # publish_at native chỉ khi private + có giờ tương lai (YT tự công khai đúng giờ)
            sched = it.get("publish_at") if ytc["privacy"] == "private" else None
            r = YT.upload(tmp, meta, ytc, creds, sched)
            results["youtube"] = r
            state.update_yt_queue(it["id"], {"status": "posted", "results": results,
                                             "attempts": attempts, "posted_at": now.isoformat()})
            if conn.get("client_id"):
                try:
                    state.bump_client_uploads(conn["client_id"], now, owner=owner)
                except Exception:
                    pass
            print(f"     ✅ YouTube: {r.get('url')}")
        except YT.QuotaExceeded:
            state.update_yt_queue(it["id"], {"status": "pending", "error": "quota hết — để mai", "attempts": attempts})
            print("     ⏸ quota project đã hết -> để mai")
        except Exception as e:
            status = "failed" if attempts >= 3 else "pending"
            state.update_yt_queue(it["id"], {"status": status, "error": str(e), "attempts": attempts})
            print(f"     ❌ YouTube lỗi: {e}")
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
    print("✔ yt_queue xong.")


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
