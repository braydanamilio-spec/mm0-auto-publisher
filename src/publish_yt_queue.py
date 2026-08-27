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
import affiliate as AFF
from firestore_state import State

# CHỐNG SPAM upload (đúng chính sách YouTube): trần/ngày theo OAuth client + trần mỗi lần cron chạy.
# Video còn dư tự để lại cron sau -> KHÔNG bắn hàng loạt 1 lượt (YouTube coi mass-upload nhanh là spam).
YT_CAP_PER_DAY = 6      # ~6 upload/ngày/1 project (quota 10k đơn vị)
MAX_PER_RUN = 2         # mỗi lần cron chỉ đăng tối đa 2 -> giãn cách tự nhiên theo tần suất cron


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
    uploaded_run: dict[str, int] = {}   # đếm upload trong LẦN CHẠY này theo client_id
    done_run = 0
    _aff_cache: dict[str, dict] = {}    # cache affiliate cfg theo owner (đỡ đọc lặp)

    for it in items:
        if done_run >= MAX_PER_RUN:
            print(f"  ⏸ đủ {MAX_PER_RUN} bài/lần chạy -> phần còn lại để cron sau (giãn cách chống spam).")
            break
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

        # Trần/ngày theo OAuth client (chống vượt quota + chống mass-upload spam) — giữ pending nếu đã đầy.
        _cid = conn.get("client_id")
        if _cid:
            try:
                used = state.client_uploads_today(_cid, now) + uploaded_run.get(_cid, 0)
            except Exception:
                used = uploaded_run.get(_cid, 0)
            if used >= YT_CAP_PER_DAY:
                print(f"  ⏸ client đủ {YT_CAP_PER_DAY} upload/ngày -> để mai: {it.get('drive_name') or fid}")
                continue

        owner_drives = [c for c in drive_conns if c.get("owner") == owner]
        # 27/8 — ĐỪNG IM LẶNG RƠI VỀ KHO ĐẦU DANH SÁCH.
        #
        # Bản cũ: không khớp `drive_account` thì lấy `owner_drives[0]` — MỘT TÀI KHOẢN KHÁC — rồi
        # tải file bằng khoá của nó. Trước nay còn chạy được là nhờ `upload_to_queue` mở file cho
        # "anyone with link", nên khoá nào cũng đọc được qua id.
        # Chuyện đó SẮP HẾT ĐÚNG: kho nối từ 27/8 dùng scope `drive.file`, mà `drive.file` chỉ thấy
        # file do CHÍNH app đó tạo — công khai hay không cũng vậy. Kho `drive.file` lọt vào vị trí
        # đầu danh sách là mọi job không khớp tên kho sẽ 404, với thông báo "không thấy Drive
        # account" hoặc lỗi tải chung chung — không chỉ được về đâu.
        # Nên: khớp đúng thì dùng đúng; KHÔNG khớp thì vẫn thử, nhưng NÓI RA là đang đoán, và thử
        # vài kho chứ không cắm đầu vào đúng một cái.
        _khop = [c for c in owner_drives if acct and (c.get("name") == acct or c.get("email") == acct)]
        if _khop:
            _ung = _khop[:1]
        else:
            _ung = owner_drives[:5]
            if _ung:
                print(f"     ⚠️ job không ghi kho nguồn khớp được (drive_account={acct!r}) — "
                      f"thử lần lượt {len(_ung)} kho đầu. Kho scope `drive.file` sẽ KHÔNG đọc được "
                      f"file của app khác, đây là lý do chính đáng để job này hỏng.")
        dc = _ung[0] if _ung else None
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
               "default_language": it.get("language") or "en"}

        # TIẾP THỊ LIÊN KẾT: chèn link vào mô tả + (tuỳ chọn) chuẩn bị bình luận đăng sau.
        if owner not in _aff_cache:
            _aff_cache[owner] = (state.get_doc("settings", "overrides__" + owner) or {}).get("affiliate") or {}
        aff_comment = AFF.apply(meta, _aff_cache[owner], "youtube", slug, fid)

        if dry_run:
            print(f"  (dry) yt_queue {fid} -> {slug} [{ytc['privacy']}]"); continue

        # KHOÁ chống đăng trùng: chỉ 1 tiến trình được xử lý item này
        if not state.claim_item("yt_queue", it["id"], now):
            print(f"  ⏭ bỏ qua (đang được tiến trình khác xử lý): {fid}"); continue

        print(f"  🚀 YT-queue: {it.get('drive_name') or fid} -> {slug}")
        creds = {"client_id": conn["client_id"], "client_secret": conn["client_secret"],
                 "refresh_token": conn["refresh_token"]}
        safe_name = "".join(c if (c.isalnum() or c in "._-") else "_" for c in (it.get("drive_name") or (fid + ".mp4")))
        tmp = os.path.join(tempfile.gettempdir(), safe_name)
        thumb_tmp = None
        caption_specs = []
        attempts = it.get("attempts", 0) + 1
        try:
            # Thử lần lượt các kho ứng viên: khớp tên thì danh sách chỉ có 1 (không tốn thêm gì);
            # không khớp thì mới thực sự dò. Lỗi của kho cuối cùng được ném lên để nhánh `except`
            # phía dưới xử đúng loại (quota / hỏng / thử lại).
            drv, _loi_tai = None, None
            for _c in _ung:
                try:
                    _d = ST.Drive.from_oauth({"client_id": _c["client_id"], "client_secret": _c["client_secret"],
                                              "refresh_token": _c["refresh_token"]})
                    _d.download(fid, tmp)
                    drv, dc = _d, _c
                    if len(_ung) > 1:
                        print(f"     ✔ đọc được file từ kho {_c.get('name')}")
                    break
                except Exception as _e:
                    _loi_tai = _e
                    if len(_ung) > 1:
                        print(f"     · kho {_c.get('name')} không đọc được: {str(_e)[:70]}")
            if drv is None:
                raise (_loi_tai or RuntimeError("không tải được file từ kho nào"))
            # THUMBNAIL có sẵn (import từ Drive) -> tải + đặt làm thumbnail YouTube
            if it.get("thumbnail_file_id"):
                try:
                    thumb_tmp = os.path.join(tempfile.gettempdir(), "thumb_" + safe_name + ".jpg")
                    drv.download(it["thumbnail_file_id"], thumb_tmp)
                except Exception as e:
                    print(f"     ⚠️ thumbnail: {e}"); thumb_tmp = None
            # PHỤ ĐỀ .srt có sẵn -> tải + up caption (khỏi để YouTube tự tạo)
            if it.get("caption_file_id"):
                try:
                    cpath = os.path.join(tempfile.gettempdir(), "cap_" + safe_name + ".srt")
                    drv.download(it["caption_file_id"], cpath)
                    caption_specs.append({"path": cpath, "language": it.get("caption_lang", "en"), "name": "English"})
                except Exception as e:
                    print(f"     ⚠️ caption: {e}")
            # publish_at native chỉ khi private + có giờ tương lai (YT tự công khai đúng giờ)
            sched = it.get("publish_at") if ytc["privacy"] == "private" else None
            # Playlist theo LOẠI video, tự tạo trong ĐÚNG kênh đang upload (creds riêng/kênh -> không thể lẫn playlist
            # sang kênh khác dù trùng tên "Long Videos"/"Shorts" — mỗi kênh có playlist CỦA RIÊNG nó).
            _pl_name = "Long Videos" if (it.get("type") or "long") == "long" else "Shorts"
            r = YT.upload(tmp, meta, ytc, creds, sched, thumbnail_path=thumb_tmp, captions=caption_specs, playlist=_pl_name)
            results["youtube"] = r
            if aff_comment and r.get("id"):
                try:
                    YT.post_comment(creds, r["id"], aff_comment)
                    print("     💬 Đã đăng bình luận link tiếp thị")
                except Exception:
                    pass
            state.update_yt_queue(it["id"], {"status": "posted", "results": results,
                                             "attempts": attempts, "posted_at": now.isoformat()})
            done_run += 1
            if conn.get("client_id"):
                uploaded_run[conn["client_id"]] = uploaded_run.get(conn["client_id"], 0) + 1
                try:
                    state.bump_client_uploads(conn["client_id"], now, owner=owner)
                except Exception:
                    pass
            print(f"     ✅ YouTube: {r.get('url')}")
        except YT.QuotaExceeded:
            state.update_yt_queue(it["id"], {"status": "pending", "error": "quota hết — để mai", "attempts": attempts})
            print("     ⏸ quota project đã hết -> để mai")
        except Exception as e:
            # QUAN TRỌNG: nếu upload YouTube ĐÃ THÀNH CÔNG (results["youtube"]["id"] có thật) và lỗi xảy ra
            # ở bước SAU đó (vd post_comment, hoặc chính write Firestore "posted" phía trên bị lỗi mạng/429
            # tạm thời) -> TUYỆT ĐỐI không được ghi đè mất "results" ở đây, nếu không lần cron sau coi như
            # CHƯA đăng (results rỗng) -> tải + up lại -> ĐĂNG TRÙNG video thật đã có trên YouTube.
            yt_id = (results.get("youtube") or {}).get("id")
            if yt_id:
                state.update_yt_queue(it["id"], {"status": "posted", "results": results,
                                                 "attempts": attempts, "posted_at": now.isoformat(),
                                                 "error": f"đã đăng nhưng lỗi bước sau: {e}"})
                print(f"     ⚠️ Đã đăng YouTube ({yt_id}) nhưng lỗi bước sau ({e}) — giữ nguyên kết quả, KHÔNG đăng lại.")
            else:
                status = "failed" if attempts >= 3 else "pending"
                state.update_yt_queue(it["id"], {"status": status, "error": str(e), "attempts": attempts})
                print(f"     ❌ YouTube lỗi: {e}")
        finally:
            for p in [tmp, thumb_tmp] + [c["path"] for c in caption_specs]:
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
    print("✔ yt_queue xong.")


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
