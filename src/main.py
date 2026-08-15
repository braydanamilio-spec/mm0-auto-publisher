"""
main.py — BỘ ĐIỀU PHỐI CHÍNH. GitHub Actions gọi file này mỗi lần cron chạy.

Luồng cho mỗi kênh:
  1. Quét _QUEUE trên Drive -> danh sách video hiện có.
  2. Với video MỚI: đọc sidecar, dựng metadata, gán publish_at theo template, lưu Firestore.
  3. Lọc video ĐẾN GIỜ + trong trần an toàn -> chọn 1 video để đăng lần này.
  4. Tải về -> upload YouTube (+ Facebook) -> chuyển file sang _POSTED -> cập nhật state.
  5. In log để Actions hiển thị.

Chạy thử tại máy (dry-run, không upload):
    python src/main.py --dry-run
"""

from __future__ import annotations
import argparse
import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone

import yaml

sys.path.insert(0, os.path.dirname(__file__))
import metadata as M
import scheduler as S
from drive_client import Drive
from firestore_state import State
import youtube_uploader as YT
import facebook_uploader as FB

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")


def load_yaml(name: str) -> dict:
    with open(os.path.join(CONFIG_DIR, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name_or_key: str) -> str | None:
    return os.environ.get(name_or_key)


def resolve_channel_env(ch: dict) -> dict:
    """Đổi các *_env trong config thành giá trị thật từ biến môi trường."""
    out = {"drive_folder_id": env(ch["drive_folder_id_env"])}
    yt = ch.get("youtube", {})
    if yt.get("enabled"):
        out["yt_creds"] = {
            "client_id": env(yt["client_id_env"]),
            "client_secret": env(yt["client_secret_env"]),
            "refresh_token": env(yt["refresh_token_env"]),
        }
    fb = ch.get("facebook", {})
    if fb.get("enabled"):
        out["fb"] = {"page_id": env(fb["page_id_env"]), "page_token": env(fb["page_token_env"])}
    return out


def process_channel(key, ch, templates, safety, tz, dry_run, drive, state, now):
    print(f"\n=== KÊNH: {ch['display_name']} ({key}) ===")
    resolved = resolve_channel_env(ch)
    root = resolved["drive_folder_id"]
    if not root:
        print("  ⚠️  Thiếu Drive folder id (secret chưa set) -> bỏ qua.")
        return

    tmpl_name = ch.get("active_template", os.environ.get("POSTING_TEMPLATE", "growth_30d"))
    template = templates["templates"][tmpl_name]

    # 1) Quét Drive
    files = drive.list_queue(root)
    print(f"  📂 _QUEUE có {len(files)} video.")

    # 2) Ghép với state + gán publish_at cho video mới
    items, used_slots = [], set()
    for f in files:
        doc = state.get_video(f["id"]) or {}
        sidecar = drive.read_sidecar(f["parents"][0], f["name"])
        raw = {
            "topic": sidecar.get("topic") or M.slug_to_topic(f["name"]),
            "type": sidecar.get("type") or f["type"],
            **{k: sidecar[k] for k in ("title", "description", "hashtags", "tags",
                                       "platforms", "publish_at") if k in sidecar},
        }
        meta = M.build_metadata(raw, ch["branding"])
        warns = M.lint(meta)
        item = {
            "drive_file_id": f["id"],
            "drive_name": f["name"],
            "parent_id": f["parents"][0],
            "channel": key,
            "type": meta["type"],
            "meta": meta,
            "publish_at": doc.get("publish_at") or raw.get("publish_at"),
            "status": doc.get("status", "pending"),
            "attempts": doc.get("attempts", 0),
            "warnings": warns,
            "thumbnail": sidecar.get("thumbnail"),
        }
        if item["publish_at"]:
            used_slots.add(item["publish_at"])
        items.append(item)

    S.assign_slots(items, template, tz, now, used_slots)

    # Lưu/đồng bộ mọi item về Firestore (để dashboard thấy)
    for it in items:
        state.upsert_video(it["drive_file_id"], {
            "channel": key,
            "drive_name": it["drive_name"],
            "type": it["type"],
            "title": it["meta"]["title"],
            "publish_at": it.get("publish_at"),
            "status": it["status"],
            "attempts": it["attempts"],
            "warnings": it["warnings"],
            "template": tmpl_name,
        })

    # 3) Lọc đến giờ + trần an toàn
    ready = S.due_items(items, now)
    counters = state.get_counters(key, now)
    last = state.last_upload_at(key, now)
    todo = S.apply_limits(ready, safety, counters.get("yt", 0), counters.get("fb", 0), last, now)

    print(f"  ⏰ {len(ready)} video đến giờ | chọn đăng lần này: {len(todo)} "
          f"(đã đăng hôm nay YT={counters.get('yt',0)} FB={counters.get('fb',0)})")

    if not todo:
        return

    # 4) Đăng
    for it in todo:
        try:
            publish_one(it, ch, resolved, drive, state, root, dry_run, now)
        except YT.QuotaExceeded:
            print(f"  ⏸ Kênh {key}: hết quota — dừng đăng hôm nay, phần còn lại để ngày mai.")
            break


def publish_one(it, ch, resolved, drive, state, root, dry_run, now):
    name = it["drive_name"]
    meta = it["meta"]
    print(f"  🚀 Đăng: {name} -> {meta['title']!r} [{it['type']}]")
    if it["warnings"]:
        print("     ⚠️  " + " | ".join(it["warnings"]))

    if dry_run:
        print("     (dry-run: KHÔNG upload)")
        return

    state.upsert_video(it["drive_file_id"], {"status": "uploading"})
    tmp = os.path.join(tempfile.gettempdir(), name)
    thumb_tmp = None
    results = {}
    yt_ok = fb_ok = False
    try:
        drive.download(it["drive_file_id"], tmp)

        # Tải thumbnail tùy chỉnh (nếu sidecar khai báo)
        if it.get("thumbnail"):
            tid = drive.find_file(it["parent_id"], it["thumbnail"])
            if tid:
                thumb_tmp = os.path.join(tempfile.gettempdir(), it["thumbnail"])
                drive.download(tid, thumb_tmp)

        if "youtube" in meta["platforms"] and resolved.get("yt_creds"):
            r = YT.upload(tmp, meta, ch["youtube"], resolved["yt_creds"],
                          it.get("publish_at"), thumbnail_path=thumb_tmp)
            results["youtube"] = r
            yt_ok = True
            print(f"     ✅ YouTube: {r['url']}")

        if "facebook" in meta["platforms"] and resolved.get("fb"):
            r = FB.upload(tmp, meta, resolved["fb"]["page_id"], resolved["fb"]["page_token"])
            results["facebook"] = r
            fb_ok = True
            print(f"     ✅ Facebook: {r['url']}")

        # Thành công (ít nhất 1 nền tảng) -> chuyển video + sidecar + thumbnail sang _POSTED
        drive.move(it["drive_file_id"], root, "_POSTED")
        base = name.rsplit(".", 1)[0]
        for companion in (f"{base}.json", it.get("thumbnail")):
            if not companion:
                continue
            cid = drive.find_file(it["parent_id"], companion)
            if cid:
                drive.move(cid, root, "_POSTED")
        state.upsert_video(it["drive_file_id"], {
            "status": "posted", "results": results,
            "posted_at": now.isoformat(),
        })
        state.bump_counters(it["channel"], now, yt=int(yt_ok), fb=int(fb_ok))
        state.set_channel_health(it["channel"], {
            "last_publish_at": now.isoformat(),
            **({"yt_ok": True} if yt_ok else {}),
            **({"fb_ok": True} if fb_ok else {}),
        })
        print("     📦 Đã chuyển sang _POSTED.")

    except YT.QuotaExceeded:
        print("     ⏸ Hết quota YouTube hôm nay — giữ video ở 'pending', tự thử lại ngày mai.")
        state.upsert_video(it["drive_file_id"], {"status": "pending", "note": "quota_wait"})
        raise  # báo process_channel dừng đăng kênh này
    except Exception as e:
        attempts = it["attempts"] + 1
        print(f"     ❌ LỖI: {e}")
        traceback.print_exc()
        if any(k in str(e).lower() for k in ("invalid_grant", "unauthorized", "invalid credentials")):
            state.set_channel_health(it["channel"], {"yt_ok": False, "yt_error": str(e)[:200]})
        target_status = "failed"
        state.upsert_video(it["drive_file_id"], {
            "status": target_status, "attempts": attempts,
            "error": str(e), "partial_results": results,
        })
        # Lỗi >= 3 lần -> chuyển _FAILED để bạn kiểm tra thủ công
        if attempts >= 3:
            try:
                drive.move(it["drive_file_id"], root, "_FAILED")
                print("     🗂️  Lỗi >=3 lần -> chuyển _FAILED.")
            except Exception:
                pass
    finally:
        for p in (tmp, thumb_tmp):
            if p and os.path.exists(p):
                os.remove(p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Không upload, chỉ mô phỏng.")
    ap.add_argument("--only", help="Chỉ chạy 1 kênh (key trong channels.yaml).")
    args = ap.parse_args()

    channels = load_yaml("channels.yaml")
    templates = load_yaml("posting_templates.yaml")
    safety = templates.get("safety_limits", {})
    tz = channels.get("timezone", "Asia/Ho_Chi_Minh")
    now = datetime.now(timezone.utc)

    drive = Drive()
    state = State()

    for key, ch in channels["channels"].items():
        if not ch.get("enabled"):
            continue
        if args.only and key != args.only:
            continue
        try:
            process_channel(key, ch, templates, safety, tz, args.dry_run, drive, state, now)
        except Exception as e:
            print(f"  ❌ Kênh {key} lỗi tổng: {e}")
            traceback.print_exc()

    print("\n✔ Hoàn tất lần chạy.")


if __name__ == "__main__":
    main()
