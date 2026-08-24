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
import tempfile
import sys
import time
import random
from datetime import datetime, timezone

MAX_SOCIAL_PER_RUN = 3   # mỗi lần cron chỉ đăng tối đa 3 bài FB/IG -> giãn cách, tránh Meta gắn cờ spam

sys.path.insert(0, os.path.dirname(__file__))
import storage as ST
import facebook_uploader as FB
import instagram_uploader as IG
import metadata as M
import affiliate as AFF
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
        aff_cfg = ov.get("affiliate") or {}

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
                _post_one(it, u["pages"], fb_map, do_fb, do_ig, state, now, dry_run, aff_cfg)

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
    posted_run = 0
    _aff_cache: dict[str, dict] = {}   # affiliate cfg theo owner
    for it in items:
        if posted_run >= MAX_SOCIAL_PER_RUN:
            print(f"  ⏸ đủ {MAX_SOCIAL_PER_RUN} bài FB/IG/lần chạy -> phần còn lại để cron sau (chống spam).")
            break
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
        # TIẾP THỊ LIÊN KẾT: meta RIÊNG từng nền tảng (FB có link mô tả/bình luận; IG chỉ 'link ở bio').
        if owner not in _aff_cache:
            _aff_cache[owner] = (state.get_doc("settings", "overrides__" + owner) or {}).get("affiliate") or {}
        fb_meta, ig_meta = dict(meta), dict(meta)
        fb_comment = AFF.apply(fb_meta, _aff_cache[owner], "facebook", slug, fid)
        AFF.apply(ig_meta, _aff_cache[owner], "instagram", slug, fid)
        if dry_run:
            print(f"  (dry) queue {fid} -> Page {slug} FB={want_fb} IG={want_ig}"); continue
        # KHOÁ chống đăng trùng
        if not state.claim_item("social_queue", it["id"], now):
            print(f"  ⏭ bỏ qua (tiến trình khác đang xử lý): {fid}"); continue
        print(f"  🚀 Queue FB/IG: {it.get('drive_name') or fid} -> {conn.get('page_name')}")
        try:
            drv = ST.Drive.from_oauth({"client_id": dc["client_id"], "client_secret": dc["client_secret"],
                                       "refresh_token": dc["refresh_token"]})
        except Exception as e:
            state.update_queue(it["id"], {"status": "failed", "error": f"drive: {e}"}); continue
        # Giới hạn IG 25 bài/ngày/tài khoản -> nếu gần đầy thì để cron sau (giữ pending), tránh bị gắn cờ.
        if want_ig and not IG.publish_limit_ok(conn["ig_user_id"], conn["page_token"]):
            want_ig = False; errs_ig_skip = True
            print("     ⏸ IG đã gần trần 25 bài/ngày -> để cron sau.")
        else:
            errs_ig_skip = False
        public_url, errs, leaked = None, [], False
        errs_fb_skip = False        # FB hết nhịp -> HOÃN, không tính là lần thất bại (24/8)
        try:
            public_url = drv.make_public(fid)
            if want_fb:
                try:
                    r = FB.upload(None, fb_meta, conn["page_id"], conn["page_token"], video_url=public_url)
                    results["facebook"] = r; print(f"     ✅ Facebook: {r.get('url')}")
                    if fb_comment and r.get("id"):
                        FB.post_comment(r["id"], fb_comment, conn["page_token"])
                        print("     💬 Đã đăng bình luận link tiếp thị (FB)")
                except FB.HetNhip as e:
                    errs_fb_skip = True; print(f"     ⏸ {e} -> để cron sau (KHÔNG tính thất bại).")
                except Exception as e:
                    errs.append(f"FB: {e}"); print(f"     ❌ Facebook: {e}")
            if want_ig:
                try:
                    r = IG.upload(public_url, ig_meta, conn["ig_user_id"], conn["page_token"])
                    results["instagram"] = r; print(f"     ✅ Instagram: {r.get('url')}")
                except FB.HetNhip as e:
                    errs_ig_skip = True; print(f"     ⏸ IG {e} -> để cron sau.")
                except Exception as e:
                    errs.append(f"IG: {e}"); print(f"     ❌ Instagram: {e}")
            # QUAN TRỌNG: chờ FB KÉO XONG video (file_url tải ngầm) TRƯỚC khi thu hồi link,
            # nếu không video 2-5GB chưa kịp kéo -> hỏng. IG.upload đã block tới FINISHED nên an toàn sẵn.
            # Bọc riêng try/except: nếu hàm này throw (lỗi mạng/API) mà KHÔNG bắt ở đây, exception sẽ
            # văng thẳng ra ngoài, NHẢY QUA state.update_queue() ghi kết quả bên dưới -> FB đã đăng THẬT
            # nhưng results["facebook"] không kịp lưu -> cron sau coi như chưa đăng -> ĐĂNG TRÙNG.
            fb_id = (results.get("facebook") or {}).get("id")
            if fb_id:
                try:
                    if not FB.wait_video_ready(fb_id, conn["page_token"]):
                        print("     ⚠️ FB xử lý video lâu/không xong trước khi thu hồi link.")
                except Exception as e:
                    print(f"     ⚠️ wait_video_ready lỗi (bỏ qua, FB đã đăng {fb_id} rồi): {e}")
        finally:
            if public_url:
                if not drv.make_private(fid):
                    leaked = True   # thu hồi thất bại -> đánh cờ để sweeper/cron sau gỡ lại
        # Đã xong TẤT CẢ nền tảng được yêu cầu? (IG bị HOÃN do trần ngày -> CHƯA xong)
        fully_done = ((not want_fb) or results.get("facebook", {}).get("id")) and \
                     ((not want_ig) or results.get("instagram", {}).get("id")) and \
                     not errs_ig_skip and not errs_fb_skip
        attempts = it.get("attempts", 0) + 1
        # IG bị hoãn do trần ngày KHÔNG tính là lần thất bại (khỏi dead-letter oan)
        if fully_done:
            status = "posted"; posted_run += 1
        elif (errs_ig_skip or errs_fb_skip) and not errs:
            status = "pending"; attempts = it.get("attempts", 0)   # giữ nguyên, chờ quota IG
        elif attempts >= 3:          # bỏ cuộc sau 3 lần (dead-letter)
            status = "failed"
        else:
            status = "pending"       # lỗi TẠM -> giữ pending, cron sau retry (giữ kết quả 1 phần)
        state.update_queue(it["id"], {"status": status, "results": results,
                                      "error": " | ".join(errs), "attempts": attempts,
                                      "needs_revocation": leaked, "drive_file_id": fid,
                                      "posted_at": now.isoformat() if fully_done else None})
        if status == "posted":
            time.sleep(random.uniform(4, 12))   # giãn cách nhẹ giữa các bài -> tránh heuristic spam Meta


def _post_one(it, pages, fb_map, do_fb, do_ig, state, now, dry_run, aff_cfg=None):
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

    # CHỐNG ĐĂNG TRÙNG (không đụng field `status` của luồng YouTube): đọc LẠI results mới nhất
    # ngay trước khi đăng -> nếu cron khác vừa đăng xong thì bỏ qua nền tảng đó.
    fresh = (state.get_video(fid) or {}).get("results") or {}
    if fresh.get("facebook", {}).get("id"):
        results["facebook"] = fresh["facebook"]; want_fb = False
    if fresh.get("instagram", {}).get("id"):
        results["instagram"] = fresh["instagram"]; want_ig = False
    if not (want_fb or want_ig):
        return
    # Trần IG 25 bài/ngày -> hoãn IG nếu gần đầy (không tính là lỗi)
    if want_ig and not IG.publish_limit_ok(page["ig_user_id"], page["page_token"]):
        want_ig = False
        print("     ⏸ IG gần trần 25 bài/ngày -> để cron sau.")

    # TIẾP THỊ LIÊN KẾT: meta riêng từng nền tảng + comment link (FB Reels).
    fb_meta, ig_meta = dict(meta), dict(meta)
    _slug = page.get("name") or channel or ""
    fb_comment = AFF.apply(fb_meta, aff_cfg, "facebook", _slug, fid)
    AFF.apply(ig_meta, aff_cfg, "instagram", _slug, fid)

    # ẢNH BÌA: dùng ĐÚNG tấm thumbnail của chính video này (cùng tấm YouTube dùng) -> bài FB và
    # YouTube nhìn đồng bộ, thay vì để FB tự bốc một khung bất kỳ. Tìm theo TÊN FILE ghi trong
    # sidecar, trong ĐÚNG thư mục chứa video -> không bao giờ lấy nhầm ảnh của video khác.
    fb_thumb = None
    if sidecar.get("thumbnail"):
        try:
            _tid = drv.find_file(it["parent_id"], sidecar["thumbnail"])
            if _tid:
                fb_thumb = os.path.join(tempfile.gettempdir(), f"fbthumb_{fid}_{sidecar['thumbnail']}")
                drv.download(_tid, fb_thumb)
        except Exception as e:
            fb_thumb = None      # không có ảnh -> FB tự lấy khung như cũ, KHÔNG chặn việc đăng
            print(f"     ⚠️ không tải được ảnh bìa ({str(e)[:70]}) -> để FB tự chọn khung")

    print(f"  🚀 FB/IG: {it['name']} -> {page.get('page_name')}")
    public_url = None
    try:
        public_url = drv.make_public(fid)   # FB/IG tự kéo từ link này
        if want_fb:
            try:
                r = FB.upload(None, fb_meta, page["page_id"], page["page_token"],
                              video_url=public_url, thumb_path=fb_thumb)
                results["facebook"] = r
                state.upsert_video(fid, {"results": results})
                print(f"     ✅ Facebook: {r.get('url')}")
                if fb_comment and r.get("id"):
                    FB.post_comment(r["id"], fb_comment, page["page_token"])
                    print("     💬 Đã đăng bình luận link tiếp thị (FB)")
            except FB.HetNhip as e:
                # Trần theo APP (mã 4) dùng chung cho cả IG -> thử IG nữa cũng chặn, chỉ tốn lượt gọi.
                want_ig = False
                print(f"     ⏸ {e} -> giữ pending, cron sau đăng lại.")
            except Exception as e:
                print(f"     ❌ Facebook lỗi: {e}")
        if want_ig:
            try:
                r = IG.upload(public_url, ig_meta, page["ig_user_id"], page["page_token"])
                results["instagram"] = r
                state.upsert_video(fid, {"results": results})
                print(f"     ✅ Instagram: {r.get('url')}")
            except Exception as e:
                print(f"     ❌ Instagram lỗi: {e}")
        # Chờ FB kéo xong video TRƯỚC khi thu hồi link (video nặng tải ngầm nhiều phút).
        # results["facebook"] đã lưu Firestore NGAY phía trên (an toàn nếu bước này lỗi) — vẫn bọc
        # try/except để không văng exception làm dở dang state.upsert_video "social_status" cuối hàm.
        fb_id = (results.get("facebook") or {}).get("id")
        if fb_id:
            try:
                if not FB.wait_video_ready(fb_id, page["page_token"]):
                    print("     ⚠️ FB xử lý video lâu/không xong trước khi thu hồi link.")
            except Exception as e:
                print(f"     ⚠️ wait_video_ready lỗi (bỏ qua, FB đã đăng {fb_id} rồi): {e}")
    finally:
        if public_url:
            if not drv.make_private(fid):   # THU HỒI link công khai (retry+verify bên trong)
                state.upsert_video(fid, {"needs_revocation": True})
        if fb_thumb:                        # dọn ảnh bìa tạm (runner CI dung lượng có hạn)
            try:
                os.remove(fb_thumb)
            except OSError:
                pass
    # Nhả khoá: đăng xong -> posted; còn thiếu -> pending để cron sau retry (giữ kết quả 1 phần)
    fully = ((not want_fb) or results.get("facebook", {}).get("id")) and \
            ((not want_ig) or results.get("instagram", {}).get("id"))
    state.upsert_video(fid, {"results": results,
                             "social_status": "posted" if fully else "pending"})


def _run_guarded(fn):
    """Hết hạn mức đọc/ghi Firestore là chuyện TẠM THỜI, tự hồi khi quota reset (~08:00 UTC) —
    không phải lỗi cấu hình cần người sửa. Trước đây nó ném ResourceExhausted lên tận cùng ->
    workflow đỏ, và mỗi lần cron chạy trong khung cạn quota lại đỏ thêm một lần (sự cố 21/8:
    publish + publish_social đỏ liên tục 00:00-07:10Z). Bắt riêng ca này, báo rõ rồi thoát 0:
    lần cron kế tiếp sẽ tự làm lại. Mọi lỗi KHÁC vẫn ném lên như cũ để còn thấy mà sửa."""
    try:
        fn()
    except Exception as e:
        if type(e).__name__ == "ResourceExhausted" or "429 Quota exceeded" in str(e):
            print("\n⏳ Firestore hết hạn mức hôm nay -> bỏ lượt này, cron sau tự chạy lại.")
            try:
                from firestore_state import chan_doan_429
                print(chan_doan_429())     # 24/8: nói RÕ project nào cạn, khỏi đoán
            except Exception:
                pass
            print(f"   ({str(e)[:110]})")
            raise SystemExit(0)
        raise

if __name__ == "__main__":
    _run_guarded(lambda: run(dry_run="--dry-run" in sys.argv))
