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
import affiliate as AFF
import autotitle as AT
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
        # Đưa client_id lên TOP-LEVEL để publish_one đếm/khoá quota project đúng (trần ~6 upload/ngày).
        out["client_id"] = out["yt_creds"]["client_id"]
    fb = ch.get("facebook", {})
    if fb.get("enabled"):
        conn = state.get_connection(key, "facebook") if (state and key) else None
        if conn and conn.get("page_token"):
            out["fb"] = {"page_id": conn.get("page_id"), "page_token": conn["page_token"]}
        else:
            out["fb"] = {"page_id": env(fb["page_id_env"]), "page_token": env(fb["page_token_env"])}
    return out


def _initial_status(prev, review_mode):
    """Trạng thái khởi tạo cho video: phục hồi 'uploading'->pending; video mới ->
    'needs_review' nếu bật chế độ duyệt, ngược lại 'pending'."""
    if prev == "uploading":
        return "pending"
    if prev:
        return prev
    return "needs_review" if review_mode else "pending"


import re as _re
def _natkey(s: str):
    """Sắp xếp TỰ NHIÊN theo số (01<02<...<10) -> đăng lần lượt, không lộn xộn."""
    return [int(t) if t.isdigit() else t.lower() for t in _re.split(r"(\d+)", str(s or ""))]


def _checks(sidecar: dict, meta: dict) -> dict:
    """Cờ ĐỦ/THIẾU của 1 video (để dashboard cảnh báo trước khi đăng — KHÔNG chặn đăng)."""
    return {
        "desc": bool((sidecar.get("description") or "").strip()),   # có mô tả riêng (không phải chỉ chủ đề)
        "thumb": bool(sidecar.get("thumbnail")),                     # có ảnh thumbnail đi kèm
        "hashtags": bool(meta.get("hashtags")),
        "tags": bool(meta.get("tags")),
        "captions": bool(sidecar.get("captions")),
    }


def process_channel(key, ch, templates, safety, tz, dry_run, drive, state, now, overrides=None, review_mode=False):
    # ĐƯỜNG CŨ (folder-secret theo kênh) đã bị thay bằng kho POOL + hàng đợi yt_queue (pool-aware).
    # Kênh không set folder-secret -> bỏ qua LẶNG LẼ (không phải lỗi), tránh spam log + "failure" giả.
    resolved = resolve_channel_env(ch, state, key)
    root = resolved["drive_folder_id"]
    if not root:
        return                                   # dùng kho pool + yt_queue thay thế -> không cần folder-secret
    print(f"\n=== KÊNH: {ch['display_name']} ({key}) ===")

    # Ưu tiên template chọn trên dashboard (Firestore overrides) -> active_template -> env default
    tmpl_name = (overrides or {}).get(key) or ch.get("active_template",
                os.environ.get("POSTING_TEMPLATE", "balanced_1long_3short"))
    template = templates["templates"].get(tmpl_name) or templates["templates"]["balanced_1long_3short"]

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
            "status": _initial_status(doc.get("status"), review_mode),
            "attempts": doc.get("attempts", 0),
            "warnings": warns,
            "thumbnail": sidecar.get("thumbnail"),
            "captions": sidecar.get("captions"),
            "playlist": sidecar.get("playlist"),
            "results": doc.get("results") or {},   # để bỏ qua nền tảng đã đăng (chống trùng)
            "checks": _checks(sidecar, meta),
        }
        if item["publish_at"]:
            used_slots.add(item["publish_at"])
        items.append(item)

    items.sort(key=lambda it: _natkey(it["drive_name"]))   # 01,02,03... nhận slot sớm trước
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
            "checks": it.get("checks"),
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


def publish_one(it, ch, resolved, drive, state, root, dry_run, now, owner=None, aff_cfg=None):
    name = it["drive_name"]
    meta = it["meta"]
    # TIẾP THỊ LIÊN KẾT: chèn link vào mô tả YouTube + chuẩn bị bình luận (nếu bật auto_comment).
    aff_comment = AFF.apply(meta, aff_cfg, "youtube", it["channel"], it["drive_file_id"]) if aff_cfg else None
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
    # Trần theo PROJECT (OAuth client): nhiều channel chung 1 client sẽ chung 10.000/ngày (~6 upload)
    _cid = resolved.get("client_id")
    _proj_cap = int(os.environ.get("YT_UPLOADS_PER_PROJECT", "6"))
    if need_yt and _cid and state.client_uploads_today(_cid, now) >= _proj_cap:
        print(f"     ⏸ Project (client …{_cid[-10:]}) đã đủ {_proj_cap} upload hôm nay -> để mai (rollover).")
        need_yt = False
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

        errs = []
        # ---- YOUTUBE: đếm quota NGAY khi thành công. Lỗi Facebook sau đó KHÔNG xoá thành quả này ----
        if results.get("youtube", {}).get("id"):
            print("     ↺ YouTube đã đăng trước đó — bỏ qua (chống trùng).")
        elif need_yt:
            try:
                # Ghép cấu hình YouTube của kênh + override SỬA TAY từng video (quyền riêng tư/kid/ngôn ngữ/danh mục/địa điểm)
                ytc = dict(ch["youtube"])
                ov = it.get("yt_over") or {}
                if ov.get("privacy"): ytc["privacy"] = ov["privacy"]
                if "made_for_kids" in ov: ytc["made_for_kids"] = bool(ov["made_for_kids"])
                if ov.get("language"): ytc["default_language"] = ov["language"]
                if ov.get("category"): ytc["category_id"] = ov["category"]
                if ov.get("location"): ytc["location"] = ov["location"]
                r = YT.upload(tmp, meta, ytc, resolved["yt_creds"],
                              it.get("publish_at"), thumbnail_path=thumb_tmp,
                              captions=caption_specs, playlist=it.get("playlist"))
                results["youtube"] = r
                yt_ok = True
                if aff_comment and r.get("id"):
                    YT.post_comment(resolved["yt_creds"], r["id"], aff_comment)
                state.upsert_video(it["drive_file_id"], {"results": results})   # LƯU NGAY
                state.bump_counters(it["channel"], now, yt=1, owner=owner)       # ĐẾM NGAY -> cap không lệch
                if _cid:
                    state.bump_client_uploads(_cid, now, owner=owner)           # đếm quota theo project
                print(f"     ✅ YouTube: {r['url']}")
            except YT.QuotaExceeded:
                raise
            except Exception as e:
                errs.append(f"YouTube: {e}")
                print(f"     ❌ YouTube lỗi: {e}")

        # ---- FACEBOOK: độc lập; lỗi ở đây không làm hỏng thành quả YouTube ----
        if results.get("facebook", {}).get("id"):
            print("     ↺ Facebook đã đăng trước đó — bỏ qua (chống trùng).")
        elif need_fb:
            try:
                r = FB.upload(tmp, meta, resolved["fb"]["page_id"], resolved["fb"]["page_token"])
                results["facebook"] = r
                fb_ok = True
                state.upsert_video(it["drive_file_id"], {"results": results})
                state.bump_counters(it["channel"], now, fb=1, owner=owner)
                print(f"     ✅ Facebook: {r['url']}")
            except Exception as e:
                errs.append(f"Facebook: {e}")
                print(f"     ❌ Facebook lỗi: {e}")

        posted_any = bool(results.get("youtube", {}).get("id") or results.get("facebook", {}).get("id"))
        if not posted_any:
            # Không nền tảng nào đăng: do LỖI -> tính attempts; do thiếu creds -> giữ pending
            if errs:
                attempts = it["attempts"] + 1
                blob = " | ".join(errs)
                state.upsert_video(it["drive_file_id"], {"status": "failed", "attempts": attempts,
                                                         "error": blob, "partial_results": results})
                if any(k in blob.lower() for k in ("invalid_grant", "unauthorized", "invalid credentials")):
                    state.set_channel_health(it["channel"], {"yt_ok": False, "yt_error": blob[:200]}, owner=owner)
                if attempts >= 3:
                    try:
                        drive.move(it["drive_file_id"], root, "_FAILED")
                        print("     🗂️  Lỗi >=3 lần -> chuyển _FAILED.")
                    except Exception:
                        pass
            else:
                print("     ⚠️  Chưa đăng được nền tảng nào (thiếu creds?) — giữ hàng đợi.")
                state.upsert_video(it["drive_file_id"], {"status": "pending"})
            return

        # ĐÃ đăng ít nhất 1 nền tảng -> chuyển _POSTED (nền tảng kia lỗi thì ghi nhận là posted-partial)
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
            "status": "posted", "results": results, "posted_at": now.isoformat(),
            **({"partial_error": " | ".join(errs)} if errs else {}),
        })
        state.set_channel_health(it["channel"], {
            "last_publish_at": now.isoformat(),
            **({"yt_ok": True} if yt_ok else {}),
            **({"fb_ok": True} if fb_ok else {}),
        }, owner=owner)
        print("     📦 Đã chuyển sang _POSTED." + (f"  (⚠ {'; '.join(errs)})" if errs else ""))

    except YT.QuotaExceeded:
        print("     ⏸ Hết quota YouTube hôm nay — giữ video ở 'pending', tự thử lại ngày mai.")
        state.upsert_video(it["drive_file_id"], {"status": "pending", "note": "quota_wait"})
        raise  # báo process_channel dừng đăng kênh này
    except Exception as e:
        # Lỗi NGOÀI upload nền tảng (download/move…). Nếu đã đăng được thì KHÔNG đánh 'failed'.
        posted_any = bool(results.get("youtube", {}).get("id") or results.get("facebook", {}).get("id"))
        attempts = it["attempts"] + 1
        print(f"     ❌ LỖI: {e}")
        traceback.print_exc()
        state.upsert_video(it["drive_file_id"], {
            "status": "posted" if posted_any else "failed",
            "attempts": attempts, "error": str(e), "partial_results": results,
        })
        if not posted_any and attempts >= 3:
            try:
                drive.move(it["drive_file_id"], root, "_FAILED")
                print("     🗂️  Lỗi >=3 lần -> chuyển _FAILED.")
            except Exception:
                pass
    finally:
        for p in [tmp, thumb_tmp, *caption_tmps]:
            if p and os.path.exists(p):
                os.remove(p)


def process_pool(channels_cfg, templates, safety, tz, dry_run, state, now, overrides=None, review_mode=False):
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
                "status": _initial_status(doc.get("status"), review_mode),
                "attempts": doc.get("attempts", 0),
                "warnings": M.lint(meta), "thumbnail": sidecar.get("thumbnail"),
                "captions": sidecar.get("captions"), "playlist": sidecar.get("playlist"),
                "results": doc.get("results") or {},
                "_drive": drv, "_root": acc["root"],
            })

    for channel, items in groups.items():
        ch = channels_cfg[channel]
        tmpl_name = (overrides or {}).get(channel) or ch.get("active_template",
                    os.environ.get("POSTING_TEMPLATE", "balanced_1long_3short"))
        template = templates["templates"].get(tmpl_name) or templates["templates"]["balanced_1long_3short"]
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


def process_users(channels_cfg, templates, safety, tz, dry_run, state, now):
    """PHASE 2 multi-tenant: chạy pipeline PER-USER dựa trên connections (Firestore).
    Mỗi user: kênh YouTube (token) + kho Drive (pool) đã kết nối -> đăng, stamp owner=uid."""
    try:
        import storage as ST
        yt_conns = state.list_connections("youtube")
        drive_conns = state.list_connections("drive")
    except Exception as e:
        print(f"  (per-user) bỏ qua: {e}")
        return
    if not yt_conns:
        return

    users: dict[str, dict] = {}
    for c in yt_conns:
        o, ch = c.get("owner"), c.get("channel")
        if o and ch:
            users.setdefault(o, {"yt": {}, "drive": []})["yt"][ch] = c
    for c in drive_conns:
        o = c.get("owner")
        if o and o in users:
            users[o]["drive"].append(c)

    print(f"\n=== PER-USER (multi-tenant): {len(users)} user ===")
    for uid, u in users.items():
        if not u["drive"]:
            print(f"  user {uid[:8]}: chưa kết nối Drive kho -> bỏ qua.")
            continue
        ov = state.get_doc("settings", "overrides__" + uid) or {}
        review_mode = bool(ov.get("review_mode"))
        ov_ch = ov.get("channels", {})
        fb_meta_cfg = ov.get("fallback_meta") or {}   # PHƯƠNG ÁN 2: metadata tổ hợp dự phòng (mặc định tắt)

        pool = []
        for dc in u["drive"]:
            try:
                pool.append((ST.Drive.from_oauth({"client_id": dc["client_id"],
                             "client_secret": dc["client_secret"],
                             "refresh_token": dc["refresh_token"]}), dc["root"]))
            except Exception as e:
                print(f"  ⚠️ drive {uid[:8]}: {e}")

        groups: dict[str, list] = {}
        for drv, root in pool:
            try:
                files = drv.list_queue(root)
            except Exception as e:
                print(f"  ⚠️ list_queue {uid[:8]}: {e}")
                continue
            for f in files:
                sidecar = drv.read_sidecar(f["parents"][0], f["name"])
                channel = sidecar.get("channel")
                if not channel or channel not in u["yt"]:
                    continue
                doc = state.get_video(f["id"]) or {}
                branding = (channels_cfg.get(channel) or {}).get("branding") or {"hashtags": []}
                raw = {"topic": sidecar.get("topic") or M.slug_to_topic(f["name"]),
                       "type": sidecar.get("type") or f["type"],
                       **{k: sidecar[k] for k in ("title", "description", "hashtags", "tags",
                                                  "platforms", "publish_at") if k in sidecar}}
                # Ưu tiên bản người dùng SỬA TAY trên dashboard (edited=True)
                if doc.get("edited"):
                    for k in ("title", "description", "tags", "hashtags"):
                        if doc.get(k) not in (None, "", []):
                            raw[k] = doc[k]
                # PHƯƠNG ÁN 2: nếu BẬT và video KHÔNG có metadata thật -> điền tổ hợp dự phòng.
                AT.apply_fallback(raw, fb_meta_cfg, f["id"])
                meta = M.build_metadata(raw, branding)
                groups.setdefault(channel, []).append({
                    "drive_file_id": f["id"], "drive_name": f["name"], "parent_id": f["parents"][0],
                    "channel": channel, "type": meta["type"], "meta": meta,
                    "publish_at": doc.get("publish_at") or raw.get("publish_at"),
                    "status": _initial_status(doc.get("status"), review_mode),
                    "attempts": doc.get("attempts", 0), "warnings": M.lint(meta),
                    "thumbnail": sidecar.get("thumbnail"), "captions": sidecar.get("captions"),
                    "playlist": sidecar.get("playlist"), "results": doc.get("results") or {},
                    "checks": _checks(sidecar, meta),
                    "yt_over": ({k: doc[k] for k in ("privacy", "made_for_kids", "language", "category", "location")
                                 if doc.get(k) not in (None, "")} if doc.get("edited") else {}),
                    "_drive": drv, "_root": root,
                })

        for channel, items in groups.items():
            conn = u["yt"][channel]
            ch_cfg = channels_cfg.get(channel) or {
                "display_name": channel,
                "youtube": {"category_id": "27", "default_language": "en",
                            "privacy": "public", "made_for_kids": False},
                "branding": {"hashtags": []}}
            tmpl_name = ov_ch.get(channel) or ch_cfg.get("active_template") \
                or os.environ.get("POSTING_TEMPLATE", "balanced_1long_3short")
            template = templates["templates"].get(tmpl_name) or templates["templates"]["balanced_1long_3short"]
            items.sort(key=lambda it: _natkey(it["drive_name"]))   # 01,02,03... trước
            used = {it["publish_at"] for it in items if it.get("publish_at")}
            S.assign_slots(items, template, tz, now, used)
            for it in items:
                state.upsert_video(it["drive_file_id"], {
                    "owner": uid, "channel": channel, "drive_name": it["drive_name"],
                    "type": it["type"], "title": it["meta"]["title"],
                    "description": it["meta"]["description"], "tags": it["meta"]["tags"],
                    "hashtags": it["meta"]["hashtags"],
                    "publish_at": it.get("publish_at"), "status": it["status"],
                    "warnings": it["warnings"], "checks": it.get("checks"), "storage": "pool",
                })
            ready = S.due_items(items, now)
            counters = state.get_counters(channel, now, owner=uid)
            last = state.last_upload_at(channel, now, owner=uid)
            todo = S.apply_limits(ready, safety, counters.get("yt", 0), counters.get("fb", 0), last, now)
            resolved = {"yt_creds": {"client_id": conn["client_id"],
                                     "client_secret": conn["client_secret"],
                                     "refresh_token": conn["refresh_token"]},
                        "client_id": conn["client_id"]}
            print(f"  [{uid[:8]}/{channel}] {len(ready)} đến giờ, đăng {len(todo)}")
            for it in todo:
                try:
                    publish_one(it, ch_cfg, resolved, it["_drive"], state, it["_root"],
                                dry_run, now, owner=uid, aff_cfg=ov.get("affiliate") or {})
                except YT.QuotaExceeded:
                    print(f"  ⏸ {uid[:8]}/{channel}: hết quota.")
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
    # Cấu hình chọn trên dashboard: template per-kênh + chế độ duyệt
    ov_doc = state.get_doc("settings", "overrides") or {}
    overrides = ov_doc.get("channels", {})
    review_mode = bool(ov_doc.get("review_mode"))
    if review_mode:
        print("  🔎 Chế độ DUYỆT đang bật — video mới sẽ chờ anh duyệt trên dashboard.")

    for key, ch in channels["channels"].items():
        if not ch.get("enabled"):
            continue
        if args.only and key != args.only:
            continue
        try:
            process_channel(key, ch, templates, safety, tz, args.dry_run, drive, state, now, overrides, review_mode)
        except Exception as e:
            print(f"  ❌ Kênh {key} lỗi tổng: {e}")
            traceback.print_exc()

    # Multi-tenant: chạy pipeline per-user dựa trên connections (kết nối qua dashboard)
    if not args.only:
        try:
            process_users(channels["channels"], templates, safety, tz, args.dry_run, state, now)
        except Exception as e:
            print(f"  ❌ Per-user lỗi tổng: {e}")
            traceback.print_exc()

    # AUTO-ENQUEUE: tự đẩy video render xong vào hàng đợi (CHỈ kênh bật auto_publish + đã kết nối YouTube).
    if not args.only:
        try:
            import auto_enqueue
            auto_enqueue.run(dry_run=args.dry_run)
        except Exception as e:
            print(f"  ❌ auto-enqueue lỗi tổng: {e}")
            traceback.print_exc()

    # CONTENT HUB: đăng YouTube từ hàng đợi cloud (user chọn video Drive trên dashboard HOẶC auto-enqueue ở trên)
    if not args.only:
        try:
            import publish_yt_queue
            publish_yt_queue.run(dry_run=args.dry_run)
        except Exception as e:
            print(f"  ❌ yt_queue lỗi tổng: {e}")
            traceback.print_exc()

    # Ghi cấu hình cho dashboard (trang Cài đặt)
    try:
        default_tmpl = os.environ.get("POSTING_TEMPLATE", "balanced_1long_3short")
        ch_tmpl = {k: c.get("active_template", default_tmpl)
                   for k, c in channels["channels"].items() if c.get("enabled")}
        cleanup = {}
        try:
            with open(os.path.join(CONFIG_DIR, "storage.yaml"), encoding="utf-8") as f:
                cleanup = (yaml.safe_load(f) or {}).get("cleanup", {})
        except FileNotFoundError:
            pass
        state.set_doc("settings", "config", {"default_template": default_tmpl,
                                             "channels": ch_tmpl, "cleanup": cleanup, "safety": safety})
    except Exception as e:
        print(f"  (settings write skip: {e})")

    print("\n✔ Hoàn tất lần chạy.")


if __name__ == "__main__":
    main()
