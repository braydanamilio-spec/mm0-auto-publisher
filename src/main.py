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


def resolve_channel_env(ch: dict, state=None, key=None) -> dict:
    """
    Lấy credentials cho kênh. ƯU TIÊN token đã 'Kết nối' qua dashboard (Firestore
    connections/), nếu chưa có thì dùng GitHub Secrets (*_env). Nhờ vậy kênh mới
    chỉ cần bấm Kết nối là chạy, khỏi sửa Secrets.
    """
    out = {"drive_folder_id": env(ch["drive_folder_id_env"])}
    yt = ch.get("youtube", {})
    if yt.get("enabled"):
        conn = state.get_connection(key, "youtube") if (state and key) else None
        if conn and conn.get("refresh_token"):
            out["yt_creds"] = {"client_id": conn["client_id"],
                               "client_secret": conn["client_secret"],
                               "refresh_token": conn["refresh_token"]}
        else:
            out["yt_creds"] = {
                "client_id": env(yt["client_id_env"]),
                "client_secret": env(yt["client_secret_env"]),
                "refresh_token": env(yt["refresh_token_env"]),
            }
    fb = ch.get("facebook", {})
    if fb.get("enabled"):
        conn = state.get_connection(key, "facebook") if (state and key) else None
        if conn and conn.get("page_token"):
            out["fb"] = {"page_id": conn.get("page_id"), "page_token": conn["page_token"]}
        else:
            out["fb"] = {"page_id": env(fb["page_id_env"]), "page_token": env(fb["page_token_env"])}
    return out


def process_channel(key, ch, templates, safety, tz, dry_run, drive, state, now):
    print(f"\n=== KÊNH: {ch['display_name']} ({key}) ===")
    resolved = resolve_channel_env(ch, state, key)
    root = resolved["drive_folder_id"]
    if not root:
        print("  ⚠️  Thiếu Drive folder id (secret chưa set) -> bỏ qua.")
        return

    tmpl_name = ch.get("active_template", os.environ.get("POSTING_TEMPLATE", "balanced_1long_3short"))
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
            # Phục hồi: file còn trong _QUEUE mà trạng thái "uploading" => job trước chết dở
            # (concurrency đảm bảo không có run khác đang chạy) => cho đăng lại an toàn.
            "status": "pending" if doc.get("status") == "uploading" else doc.get("status", "pending"),
            "attempts": doc.get("attempts", 0),
            "warnings": warns,
            "thumbnail": sidecar.get("thumbnail"),
            "captions": sidecar.get("captions"),
            "results": doc.get("results") or {},   # để bỏ qua nền tảng đã đăng (chống trùng)
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
    caption_tmps = []
    results = dict(it.get("results") or {})   # bắt đầu từ kết quả đã có (idempotent)
    yt_ok = fb_ok = False   # 'newly' — chỉ đếm nền tảng ĐĂNG MỚI lần này
    plats = meta["platforms"]
    need_yt = "youtube" in plats and resolved.get("yt_creds") and not results.get("youtube", {}).get("id")
    need_fb = "facebook" in plats and resolved.get("fb") and not results.get("facebook", {}).get("id")
    try:
        # Chỉ tải video khi thực sự cần đăng (tiết kiệm băng thông khi retry)
        if need_yt or need_fb:
            drive.download(it["drive_file_id"], tmp)

        # Tải thumbnail + phụ đề (chỉ khi cần đăng YouTube)
        caption_specs = []
        if need_yt:
            if it.get("thumbnail"):
                tid = drive.find_file(it["parent_id"], it["thumbnail"])
                if tid:
                    thumb_tmp = os.path.join(tempfile.gettempdir(), it["thumbnail"])
                    drive.download(tid, thumb_tmp)
            for cap in it.get("captions") or []:
                cidf = drive.find_file(it["parent_id"], cap["file"])
                if cidf:
                    cpath = os.path.join(tempfile.gettempdir(), cap["file"])
                    drive.download(cidf, cpath)
                    caption_tmps.append(cpath)
                    caption_specs.append({"path": cpath, "language": cap.get("language", "en"),
                                          "name": cap.get("name", "")})

        if results.get("youtube", {}).get("id"):
            print("     ↺ YouTube đã đăng trước đó — bỏ qua (chống trùng).")
        elif need_yt:
            r = YT.upload(tmp, meta, ch["youtube"], resolved["yt_creds"],
                          it.get("publish_at"), thumbnail_path=thumb_tmp, captions=caption_specs)
            results["youtube"] = r
            yt_ok = True
            state.upsert_video(it["drive_file_id"], {"results": results})  # LƯU NGAY -> không đăng lại
            print(f"     ✅ YouTube: {r['url']}")

        if results.get("facebook", {}).get("id"):
            print("     ↺ Facebook đã đăng trước đó — bỏ qua (chống trùng).")
        elif need_fb:
            r = FB.upload(tmp, meta, resolved["fb"]["page_id"], resolved["fb"]["page_token"])
            results["facebook"] = r
            fb_ok = True
            state.upsert_video(it["drive_file_id"], {"results": results})
            print(f"     ✅ Facebook: {r['url']}")

        # Chỉ coi là xong khi ĐÃ đăng ít nhất 1 nền tảng (tránh mất file khi thiếu creds)
        posted_any = bool(results.get("youtube", {}).get("id") or results.get("facebook", {}).get("id"))
        if not posted_any:
            print("     ⚠️  Chưa đăng được nền tảng nào (thiếu creds/nền tảng?) — giữ lại hàng đợi.")
            state.upsert_video(it["drive_file_id"], {"status": "pending"})
            return

        # Thành công -> chuyển video + sidecar + thumbnail + phụ đề sang _POSTED
        drive.move(it["drive_file_id"], root, "_POSTED")
        base = name.rsplit(".", 1)[0]
        companions = [f"{base}.json", it.get("thumbnail")]
        companions += [c["file"] for c in (it.get("captions") or [])]
        for companion in companions:
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
        for p in [tmp, thumb_tmp, *caption_tmps]:
            if p and os.path.exists(p):
                os.remove(p)


def process_pool(channels_cfg, templates, safety, tz, dry_run, state, now):
    """Quét HỒ CHỨA (nhiều tài khoản Drive), định tuyến kênh theo sidecar['channel']."""
    try:
        import storage as ST
        accounts = ST.pool_accounts()
    except Exception as e:
        print(f"  (pool) bỏ qua: {e}")
        return
    if not accounts:
        return

    print("\n=== HỒ CHỨA (pool đa tài khoản) ===")
    groups: dict[str, list] = {}
    for acc in accounts:
        drv = ST.account_drive(acc)
        for f in drv.list_queue(acc["root"]):
            sidecar = drv.read_sidecar(f["parents"][0], f["name"])
            channel = sidecar.get("channel")
            if not channel or channel not in channels_cfg:
                print(f"  ⚠️  {f['name']}: sidecar thiếu 'channel' hợp lệ -> bỏ qua.")
                continue
            ch = channels_cfg[channel]
            doc = state.get_video(f["id"]) or {}
            raw = {
                "topic": sidecar.get("topic") or M.slug_to_topic(f["name"]),
                "type": sidecar.get("type") or f["type"],
                **{k: sidecar[k] for k in ("title", "description", "hashtags", "tags",
                                           "platforms", "publish_at") if k in sidecar},
            }
            meta = M.build_metadata(raw, ch["branding"])
            groups.setdefault(channel, []).append({
                "drive_file_id": f["id"], "drive_name": f["name"], "parent_id": f["parents"][0],
                "channel": channel, "type": meta["type"], "meta": meta,
                "publish_at": doc.get("publish_at") or raw.get("publish_at"),
                "status": "pending" if doc.get("status") == "uploading" else doc.get("status", "pending"),
                "attempts": doc.get("attempts", 0),
                "warnings": M.lint(meta), "thumbnail": sidecar.get("thumbnail"),
                "captions": sidecar.get("captions"), "results": doc.get("results") or {},
                "_drive": drv, "_root": acc["root"],
            })

    for channel, items in groups.items():
        ch = channels_cfg[channel]
        tmpl_name = ch.get("active_template", os.environ.get("POSTING_TEMPLATE", "balanced_1long_3short"))
        template = templates["templates"][tmpl_name]
        used = {it["publish_at"] for it in items if it.get("publish_at")}
        S.assign_slots(items, template, tz, now, used)
        for it in items:
            state.upsert_video(it["drive_file_id"], {
                "channel": channel, "drive_name": it["drive_name"], "type": it["type"],
                "title": it["meta"]["title"], "publish_at": it.get("publish_at"),
                "status": it["status"], "warnings": it["warnings"], "storage": "pool",
            })
        ready = S.due_items(items, now)
        counters = state.get_counters(channel, now)
        last = state.last_upload_at(channel, now)
        todo = S.apply_limits(ready, safety, counters.get("yt", 0), counters.get("fb", 0), last, now)
        print(f"  [{channel}] pool: {len(ready)} đến giờ, đăng {len(todo)}")
        resolved = resolve_channel_env(ch, state, channel)
        for it in todo:
            try:
                publish_one(it, ch, resolved, it["_drive"], state, it["_root"], dry_run, now)
            except YT.QuotaExceeded:
                print(f"  ⏸ {channel}: hết quota, dừng.")
                break


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

    # Quét hồ chứa pool (nếu có cấu hình) — định tuyến kênh theo sidecar
    if not args.only:
        try:
            process_pool(channels["channels"], templates, safety, tz, args.dry_run, state, now)
        except Exception as e:
            print(f"  ❌ Pool lỗi tổng: {e}")
            traceback.print_exc()

    print("\n✔ Hoàn tất lần chạy.")


if __name__ == "__main__":
    main()
