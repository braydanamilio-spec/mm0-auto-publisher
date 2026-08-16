"""
publish_social.py — Bộ đăng FACEBOOK + INSTAGRAM (TÁCH RIÊNG YouTube).

Vì sao tách riêng: cơ chế đăng FB/IG khác hẳn YouTube.
  - YouTube  (main.py)      : upload BYTES qua API (cron tải từ Drive rồi đẩy lên).
  - FB/IG    (file này)     : dùng LINK CÔNG KHAI tạm -> FB/IG TỰ KÉO video từ Drive.
                              => video 2-5GB vẫn nhẹ (không tải-lại GB qua cron), tối ưu.

Chạy trên GitHub Actions (workflow riêng publish_social.yml) — tắt máy vẫn tự đăng.

Định tuyến: mỗi kênh YouTube map tới 1 Facebook Page (overrides__<uid>.fb_map = {channel: pageSlug}).
  - Nếu user chỉ có 1 Page và chưa map -> tự dùng Page đó cho mọi kênh.
Instagram: chỉ đăng SHORT (Reels IG ngắn); long-form không hợp IG.

An toàn:
  - Idempotent: đã có results.facebook/instagram thì BỎ QUA (không đăng trùng).
  - Mỗi nền tảng độc lập: FB lỗi không ảnh hưởng IG và ngược lại.
  - Thu hồi link công khai NGAY sau khi đăng (finally).
  - Chỉ đăng video ĐÃ tới giờ (publish_at <= now) và đã có publish_at (do YouTube-flow gán).
"""

from __future__ import annotations
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import storage as ST
import facebook_uploader as FB
import instagram_uploader as IG
import metadata as M
from firestore_state import State
from scheduler import due_items


def _resolve_page(channel: str, pages: dict, fb_map: dict) -> dict | None:
    slug = fb_map.get(channel)
    if slug and slug in pages:
        return pages[slug]
    if not fb_map and len(pages) == 1:      # 1 Page duy nhất -> mặc định cho mọi kênh
        return next(iter(pages.values()))
    return None


def run(dry_run: bool = False):
    state = State()
    now = datetime.now(timezone.utc)

    fb_conns = state.list_connections("facebook")
    if not fb_conns:
        print("  ⚠️  Chưa kết nối Facebook Page nào -> bỏ qua đăng FB/IG.")
        return
    drive_conns = state.list_connections("drive")

    # gom theo user
    users: dict[str, dict] = {}
    for c in fb_conns:
        o = c.get("owner")
        if o and c.get("page_token") and c.get("page_id"):
            users.setdefault(o, {"pages": {}, "drive": []})["pages"][c.get("channel")] = c
    for c in drive_conns:
        o = c.get("owner")
        if o in users:
            users[o]["drive"].append(c)

    for uid, u in users.items():
        if not u["drive"]:
            continue
        ov = state.get_doc("settings", "overrides__" + uid) or {}
        fb_map = ov.get("fb_map") or {}
        do_fb = ov.get("post_facebook", True)
        do_ig = ov.get("post_instagram", True)

        pool = []
        for dc in u["drive"]:
            try:
                pool.append((ST.Drive.from_oauth({"client_id": dc["client_id"],
                             "client_secret": dc["client_secret"],
                             "refresh_token": dc["refresh_token"]}), dc["root"]))
            except Exception as e:
                print(f"  ⚠️ drive {uid[:8]}: {e}")

        for drv, root in pool:
            try:
                files = drv.list_queue(root)
            except Exception:
                continue
            # dựng item + lọc đến giờ
            items = []
            for f in files:
                doc = state.get_video(f["id"]) or {}
                if not doc.get("publish_at"):
                    continue
                items.append({"drive_file_id": f["id"], "publish_at": doc.get("publish_at"),
                              "status": doc.get("status"), "parent_id": f["parents"][0],
                              "name": f["name"], "results": doc.get("results") or {},
                              "type": doc.get("type") or f["type"], "_drv": drv})
            for it in due_items(items, now):
                _post_one(it, u["pages"], fb_map, do_fb, do_ig, state, now, dry_run)

    # HÀNG ĐỢI ĐỘC LẬP (đăng FB/IG không cần kênh YouTube) — chạy chung 1 cron
    run_queue(state, now, dry_run)
    print("✔ Đăng FB/IG xong.")


def run_queue(state, now, dry_run=False):
    """Đăng FB/IG cho các item trong social_queue đã tới giờ (độc lập YouTube)."""
    try:
        items = state.list_social_queue()
    except Exception as e:
        print(f"  ⚠️ social_queue: {e}")
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
                    continue   # chưa tới giờ -> để lần cron sau (catch-up)
            except Exception:
                pass
        owner, slug, plat = it.get("owner"), it.get("page"), it.get("platform", "fb")
        fid, acct = it.get("drive_file_id"), it.get("drive_account")
        if not (owner and slug and fid):
            state.update_queue(it["id"], {"status": "failed", "error": "thiếu dữ liệu"}); continue
        conn = state.get_doc("connections", f"{owner}__{slug}__facebook")
        if not conn or not conn.get("page_token"):
            state.update_queue(it["id"], {"status": "failed", "error": "Page chưa kết nối"}); continue
        owner_drives = [c for c in drive_conns if c.get("owner") == owner]
        dc = next((c for c in owner_drives if acct and (c.get("name") == acct or c.get("email") == acct)), None)
        if not dc and owner_drives:      # không khớp tên -> dùng Drive đầu tiên của owner (thường chỉ 1)
            dc = owner_drives[0]
        if not dc:
            state.update_queue(it["id"], {"status": "failed", "error": "không thấy Drive account"}); continue
        results = dict(it.get("results") or {})
        want_fb = plat in ("fb", "both") and not results.get("facebook", {}).get("id")
        want_ig = plat in ("ig", "both") and bool(conn.get("ig_user_id")) and not results.get("instagram", {}).get("id")
        if not (want_fb or want_ig):
            state.update_queue(it["id"], {"status": "posted", "results": results}); continue
        meta = M.build_metadata({"topic": it.get("title") or it.get("drive_name") or "post",
                                 "type": it.get("type") or "short",
                                 "title": it.get("title") or "", "description": it.get("description") or "",
                                 "hashtags": it.get("hashtags") or []},
                                {"hashtags": it.get("hashtags") or []})
        if dry_run:
            print(f"  (dry) queue {fid} -> Page {slug} FB={want_fb} IG={want_ig}"); continue
        print(f"  🚀 Queue FB/IG: {it.get('drive_name') or fid} -> {conn.get('page_name')}")
        try:
            drv = ST.Drive.from_oauth({"client_id": dc["client_id"], "client_secret": dc["client_secret"],
                                       "refresh_token": dc["refresh_token"]})
        except Exception as e:
            state.update_queue(it["id"], {"status": "failed", "error": f"drive: {e}"}); continue
        public_url, errs = None, []
        try:
            public_url = drv.make_public(fid)
            if want_fb:
                try:
                    r = FB.upload_video(None, meta, conn["page_id"], conn["page_token"], video_url=public_url)
                    results["facebook"] = r; print(f"     ✅ Facebook: {r.get('url')}")
                except Exception as e:
                    errs.append(f"FB: {e}"); print(f"     ❌ Facebook: {e}")
            if want_ig:
                try:
                    r = IG.upload(public_url, meta, conn["ig_user_id"], conn["page_token"])
                    results["instagram"] = r; print(f"     ✅ Instagram: {r.get('url')}")
                except Exception as e:
                    errs.append(f"IG: {e}"); print(f"     ❌ Instagram: {e}")
        finally:
            if public_url:
                try:
                    drv.make_private(fid)
                except Exception:
                    pass
        posted = bool(results.get("facebook", {}).get("id") or results.get("instagram", {}).get("id"))
        state.update_queue(it["id"], {"status": "posted" if posted else "failed", "results": results,
                                      "error": " | ".join(errs), "attempts": it.get("attempts", 0) + 1,
                                      "posted_at": now.isoformat() if posted else None})


def _post_one(it, pages, fb_map, do_fb, do_ig, state, now, dry_run):
    drv = it["_drv"]
    sidecar = drv.read_sidecar(it["parent_id"], it["name"]) or {}
    channel = sidecar.get("channel")
    page = _resolve_page(channel, pages, fb_map)
    if not page:
        return  # kênh chưa gắn Facebook Page -> bỏ qua (không phải lỗi)

    branding = {"hashtags": sidecar.get("hashtags") or []}
    meta = M.build_metadata({"topic": sidecar.get("topic") or M.slug_to_topic(it["name"]),
                             "type": it["type"],
                             **{k: sidecar[k] for k in ("title", "description", "hashtags", "tags") if k in sidecar}},
                            branding)
    results = dict(it["results"])
    fid = it["drive_file_id"]

    want_fb = do_fb and "facebook" in (sidecar.get("platforms") or ["youtube", "facebook", "instagram"]) \
        and not results.get("facebook", {}).get("id")
    want_ig = do_ig and bool(page.get("ig_user_id")) and it["type"] == "short" \
        and "instagram" in (sidecar.get("platforms") or ["youtube", "facebook", "instagram"]) \
        and not results.get("instagram", {}).get("id")
    if not (want_fb or want_ig):
        return
    if dry_run:
        print(f"  (dry) {it['name']} -> Page {page.get('page_name')} FB={want_fb} IG={want_ig}")
        return

    print(f"  🚀 FB/IG: {it['name']} -> {page.get('page_name')}")
    public_url = None
    try:
        public_url = drv.make_public(fid)   # FB/IG tự kéo từ link này
        if want_fb:
            try:
                r = FB.upload_video(None, meta, page["page_id"], page["page_token"], video_url=public_url)
                results["facebook"] = r
                state.upsert_video(fid, {"results": results})
                print(f"     ✅ Facebook: {r.get('url')}")
            except Exception as e:
                print(f"     ❌ Facebook lỗi: {e}")
        if want_ig:
            try:
                r = IG.upload(public_url, meta, page["ig_user_id"], page["page_token"])
                results["instagram"] = r
                state.upsert_video(fid, {"results": results})
                print(f"     ✅ Instagram: {r.get('url')}")
            except Exception as e:
                print(f"     ❌ Instagram lỗi: {e}")
    finally:
        if public_url:
            drv.make_private(fid)   # THU HỒI link công khai ngay (bảo mật)


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
