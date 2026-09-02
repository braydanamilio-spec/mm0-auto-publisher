"""
cleanup.py — DỌN DẸP file đã đăng trong hồ chứa (PER-USER / multi-tenant).

Mỗi user có chính sách riêng (settings/overrides__<uid>.cleanup):
  keep    -> không xoá gì (Google One giữ tất cả).
  delete  -> xoá file trong _POSTED cũ hơn keep_days (mặc định 14 ngày).
  archive -> (tạm) giữ lại — backup kho lạnh per-user sẽ bổ sung sau.

⚠️ RULE XOÁ (QUAN TRỌNG — _confirmed_posted):
  Video CHỈ được xoá (khi mode=delete + đủ keep_days) NẾU đã đăng >=2 NỀN TẢNG:
  YouTube VÀ (Facebook HOẶC Instagram). Nếu MỚI chỉ đăng YouTube -> GIỮ, KHÔNG xoá.
  (Mục đích: không bao giờ mất bản gốc khi chưa có đủ nơi lưu/đăng khác.)

Đọc tài khoản Drive của user từ connections (kết nối qua dashboard).

Chạy:
  python src/cleanup.py            # theo policy mỗi user
  python src/cleanup.py --now      # dọn ngay, bỏ qua keep_days
  python src/cleanup.py --dry-run  # chỉ xem
"""

from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import storage as ST
from firestore_state import client_render_jobs

IMG_EXT = (".jpg", ".jpeg", ".png")
CAP_EXT = (".srt", ".vtt")          # phụ đề đi kèm -> dọn luôn, tránh file mồ côi chiếm dung lượng
GB = 1_000_000_000
SCRIPT_SCAN_LIMIT = 500             # trần đọc/lần cho _manage_scripts (xem comment tại chỗ dùng)


def _age_days(f: dict, now: datetime, posted_at: str | None = None) -> float:
    # Ưu tiên tính tuổi từ THỜI ĐIỂM ĐĂNG (posted_at) — không phải modifiedTime
    # (video nằm _QUEUE lâu rồi mới đăng sẽ không bị xoá non ngay khi vào _POSTED).
    t = posted_at or f.get("modifiedTime") or f.get("createdTime")
    if not t:
        return 1e9
    try:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except Exception:
        return 1e9
    return (now - dt).total_seconds() / 86400


def _confirmed_posted(rec: dict | None) -> bool:
    """ĐỦ ĐIỀU KIỆN XOÁ chỉ khi ĐÃ đăng >=2 NỀN TẢNG: YouTube VÀ (Facebook HOẶC Instagram).
    Chỉ đăng YouTube -> KHÔNG xoá (giữ bản gốc). Tránh mất video khi chưa đủ nơi lưu."""
    if not rec:
        return False
    res = rec.get("results") or {}
    def _ok(p):
        return bool((res.get(p) or {}).get("id"))
    yt = _ok("youtube")
    social = _ok("facebook") or _ok("instagram")
    return yt and social


def _companions(drv, parent_id: str, base: str) -> list[str]:
    ids = []
    for name in (f"{base}.json", *[f"{base}{e}" for e in (*IMG_EXT, *CAP_EXT)]):
        cid = drv.find_file(parent_id, name)
        if cid:
            ids.append(cid)
    return ids


def _manage_scripts(state, rj, uid: str, accts: list, dry_run: bool, archive_days: int = 30) -> tuple[int, int]:
    """Giữ Firestore (free 1GiB) KHÔNG phình theo số video, dù render hàng chục nghìn video:
      - Video ĐÃ ĐĂNG YouTube -> script hết tác dụng (YouTube đã giữ bản chính) -> xoá field.
      - Video CHƯA đăng nhưng render đã lâu (>=archive_days, ví dụ auto-đăng chưa bật/kênh bị bỏ quên)
        -> ĐẨY SANG DRIVE (_SCRIPTS_ARCHIVE, kho free lớn hơn Firestore nhiều) rồi mới xoá khỏi Firestore
        -> KHÔNG MẤT DỮ LIỆU, hoàn toàn tự động, không cần bấm gì.
      - Video chưa đăng + còn mới (< archive_days) -> giữ nguyên trong Firestore (còn cần, đọc nhanh).
    `rj` = client Project B đã tạo SẴN 1 LẦN ở run() (KHÔNG tự tạo lại ở đây mỗi user -> tránh lặp lại
    request kết nối + ping kiểm tra database của _sa_client() cho từng user, dù cả app dùng CHUNG 1 cặp
    secret GOOGLE_APPLICATION_CREDENTIALS_B/FIREBASE_PROJECT_ID_B — thừa 1 read Firestore mỗi user/lần chạy).
    Trả (số đã xoá vì đã đăng, số đã đẩy Drive vì cũ)."""
    import json, tempfile
    n_posted = n_archived = 0
    try:
        # LIMIT phòng vệ (không đổi hành vi bình thường — script tự dọn dần mỗi ngày nên set thường nhỏ):
        # tránh .stream() không giới hạn quét TOÀN BỘ collection nếu việc dọn script từng bị treo nhiều
        # ngày liền (bug khác / lỗi mạng) khiến backlog phình to -> 1 lần chạy vẫn chỉ đọc tối đa
        # SCRIPT_SCAN_LIMIT doc, phần dư để cron cleanup ngày mai xử lý tiếp (không mất dữ liệu).
        docs = list(rj.collection("render_jobs").where("owner", "==", uid)
                    .where("script", "!=", "").limit(SCRIPT_SCAN_LIMIT).stream())
    except Exception as e:
        print(f"  ⚠️ manage_scripts đọc render_jobs lỗi ({e}) — bỏ qua, thử lại ngày sau"); return 0, 0
    if not docs:
        return 0, 0
    now = datetime.now(timezone.utc)
    drv = accts[0][1] if accts else None   # dùng tài khoản Drive đầu tiên của user -> script vài KB, acc nào cũng đủ chỗ
    archive_folder = None
    for d in docs:
        job = d.to_dict() or {}
        did = job.get("drive_id")
        rec = None
        if did:
            try: rec = state.get_video(did)
            except Exception: rec = None
        posted_yt = bool(((rec or {}).get("results") or {}).get("youtube", {}).get("id"))
        if posted_yt:
            if dry_run:
                print(f"     • [prune script — đã đăng] {job.get('title', did)[:40]}"); n_posted += 1; continue
            try:
                d.reference.set({"script": ""}, merge=True); n_posted += 1
            except Exception as e:
                print(f"     ❌ prune script {did}: {e}")
            continue
        # chưa đăng -> còn mới thì giữ nguyên, cũ quá thì đẩy Drive backup
        try:
            dt = datetime.fromisoformat(str(job.get("created_at")).replace("Z", "+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            age_days = (now - dt).total_seconds() / 86400
        except Exception:
            age_days = 0
        if age_days < archive_days:
            continue
        if dry_run:
            print(f"     • [archive->Drive] {job.get('title', d.id)[:40]} ({age_days:.0f} ngày, chưa đăng)"); n_archived += 1; continue
        if not drv:
            continue   # không có Drive nào -> AN TOÀN: giữ nguyên trong Firestore, không xoá khi chưa backup được
        try:
            if archive_folder is None:
                archive_folder = drv.child_folder(accts[0][2], "_SCRIPTS_ARCHIVE", create=True)
            tmp = os.path.join(tempfile.gettempdir(), f"script_{d.id}.json")
            try:
                script_obj = json.loads(job.get("script") or "null")
            except Exception:
                script_obj = job.get("script")   # không parse được (bị cắt do quá dài) -> lưu nguyên chuỗi, vẫn đọc được
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"job_id": d.id, "channel": job.get("channel"), "type": job.get("type"),
                          "title": job.get("title"), "drive_id": did, "created_at": job.get("created_at"),
                          "script": script_obj}, f, ensure_ascii=False)
            drv.upload_file(archive_folder, tmp, name=f"{job.get('channel', 'ch')}_{d.id}.json")
            os.remove(tmp)
            d.reference.set({"script": ""}, merge=True)
            n_archived += 1
        except Exception as e:
            print(f"     ❌ archive script {d.id}: {e}")   # lỗi -> KHÔNG xoá field (an toàn), thử lại ngày sau
    return n_posted, n_archived


def run(dry_run=False, force_now=False):
    try:
        from firestore_state import State
        state = State()
        conns = state.list_connections("drive")
    except Exception as e:
        print(f"  ❌ Firestore: {e}")
        return

    users: dict[str, list] = {}
    for c in conns:
        o = c.get("owner")
        if o and c.get("root") and c.get("refresh_token"):
            users.setdefault(o, []).append(c)
    if not users:
        print("  ⚠️  Chưa có tài khoản Drive kết nối nào. Bỏ qua.")
        return

    # Tạo client Project B (render_jobs) 1 LẦN DUY NHẤT cho cả lần chạy (mọi user dùng chung 1 cặp
    # secret B) -> tránh _manage_scripts() tự tạo lại (kèm ping kiểm tra DB) mỗi user trong vòng lặp dưới.
    rj = client_render_jobs()

    now = datetime.now(timezone.utc)
    for uid, dconns in users.items():
        pol = (state.get_doc("settings", "overrides__" + uid) or {}).get("cleanup") or {}
        mode = pol.get("mode", "keep")
        keep_days = pol.get("keep_days", 14)

        accts, report = [], []
        for dc in dconns:
            try:
                drv = ST.Drive.from_oauth({"client_id": dc["client_id"],
                        "client_secret": dc["client_secret"],
                        "refresh_token": dc["refresh_token"]})
                accts.append((dc.get("channel", "store"), drv, dc["root"]))
                try:
                    u = drv.usage()
                    report.append({"name": dc.get("channel", "store"),
                                   "email": dc.get("email", ""), "used": u["used"],
                                   "cap": int(dc.get("cap_gb", 14)) * GB})
                except Exception:
                    pass
            except Exception as e:
                print(f"  ⚠️ {uid[:8]} drive: {e}")

        # Báo cáo dung lượng -> dashboard (storage/<uid>)
        if report:
            try:
                state.set_doc("storage", uid, {"owner": uid, "accounts": report,
                                               "cleanup_mode": mode, "keep_days": keep_days})
            except Exception:
                pass

        print(f"🧹 user {uid[:8]}: mode={mode} keep_days={keep_days}"
              f"{' | DRY' if dry_run else ''}{' | NGAY' if force_now else ''}")

        # Dọn field 'script' (kịch bản) -> chạy LUÔN, không phụ thuộc mode Drive (đó là chính sách riêng
        # cho FILE video; script ở Firestore B là mối lo dung lượng riêng, luôn dọn tự động).
        try:
            n_posted, n_archived = _manage_scripts(state, rj, uid, accts, dry_run,
                                                    archive_days=pol.get("script_archive_days", 30))
            if n_posted:
                print(f"  📄 script đã đăng -> xoá {n_posted} bản (Firestore B)")
            if n_archived:
                print(f"  💾 script cũ chưa đăng -> đã đẩy Drive (_SCRIPTS_ARCHIVE) + xoá khỏi Firestore: {n_archived} bản")
        except Exception as e:
            print(f"  ⚠️ manage_scripts {uid[:8]}: {e}")

        if mode == "keep":
            continue
        if mode == "archive":
            print("  (archive per-user chưa có backup riêng -> giữ lại, không xoá)")
            continue

        # mode == delete
        for name, drv, root in accts:
            try:
                posted = drv.list_folder_videos(root, "_POSTED")
            except Exception:
                continue
            for f in posted:
                # AN TOÀN CHỐNG MẤT DỮ LIỆU: chỉ xoá khi Firestore xác nhận đã đăng thật (có link/id).
                rec = None
                try:
                    rec = state.get_video(f["id"])
                except Exception:
                    rec = None
                if not _confirmed_posted(rec):
                    print(f"     ⏭ giữ lại (chưa xác nhận đã đăng): {f['name']}")
                    continue
                age = _age_days(f, now, (rec or {}).get("posted_at"))
                if not force_now and age < keep_days:
                    continue
                base = f["name"].rsplit(".", 1)[0]
                parent = (f.get("parents") or [None])[0]
                comp = _companions(drv, parent, base) if parent else []
                if dry_run:
                    print(f"     • [delete] {f['name']} ({age:.0f} ngày)")
                    continue
                try:
                    drv.delete(f["id"])
                    for c in comp:
                        drv.delete(c)
                    state.upsert_video(f["id"], {"source_status": "deleted"})
                    print(f"     🗑️  {f['name']} (+{len(comp)} phụ)")
                except Exception as e:
                    print(f"     ❌ {f['name']}: {e}")

    print("\n✔ Cleanup xong.")


def main():
    # ── TƯỜNG HẠN MỨC: VIỆC PHỤ NHƯỜNG VIỆC THIẾT YẾU  (2/9/2026) ────────────────────────
    # Anh: *"đừng để Firestore làm ảnh hưởng hệ thống một lần nào nữa."* Sáu workflow cron chạy
    # mỗi giờ (~144 lượt/ngày) cùng đọc Firestore mà không cái nào nhường cái nào. Bước này
    # hoãn được: không chạy thì chỉ thiếu vài con số trên màn hình, còn hạn mức bị nó tiêu thì
    # đường ĐẨY VIDEO mất chỗ — và mất chỗ ở đó là mất cả lượt render đã dựng xong.
    # Bắt riêng phần nạp: cổng canh không được phép là thứ gây hỏng việc nó đi canh.
    try:
        import ngan_sach as _NS
    except Exception:
        _NS = None
    if _NS is not None and _NS.chan_neu_het(
            (__doc__ or "bước này").splitlines()[0][:40]):
        return 0

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", action="store_true", help="Dọn ngay, bỏ qua keep_days.")
    a = ap.parse_args()
    run(dry_run=a.dry_run, force_now=a.now)


def _run_guarded(fn):
    """Hết hạn mức Firestore là chuyện TẠM THỜI (tự hồi khi reset) — bắt riêng 429 để thoát 0
    thay vì crash đỏ mỗi lần cron chạy trong khung cạn quota. Lỗi KHÁC vẫn ném lên. (Cùng khuôn
    main.py/publish_social.py — 21/8 stats còn sót nên đỏ oan lúc 19:05Z.)"""
    try:
        fn()
    except Exception as e:
        if type(e).__name__ == "ResourceExhausted" or "429 Quota exceeded" in str(e):
            print("\n⏳ Firestore hết hạn mức -> bỏ lượt này, cron sau tự chạy lại.")
            raise SystemExit(0)
        raise

if __name__ == "__main__":
    _run_guarded(lambda: main())
