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


def _prune_scripts(state, uid: str, dry_run: bool) -> int:
    """Xoá field 'script' (kịch bản chi tiết, lưu ở render_jobs/Project B) khỏi các video ĐÃ ĐĂNG YouTube.
    Lý do: script chỉ để RENDER LẠI khi Drive gặp sự cố TRƯỚC LÚC ĐĂNG -> đăng rồi thì YouTube đã giữ bản
    chính thức, script hết tác dụng -> xoá để Firestore (free 1GiB) KHÔNG phình theo thời gian dù render
    hàng chục nghìn video. Video CHƯA đăng -> giữ nguyên script (còn cần để lỡ có sự cố)."""
    n = 0
    try:
        rj = client_render_jobs()
        q = rj.collection("render_jobs").where("owner", "==", uid).where("script", "!=", "")
        docs = list(q.stream())
    except Exception as e:
        print(f"  ⚠️ prune_scripts đọc render_jobs lỗi ({e}) — bỏ qua, thử lại ngày sau"); return 0
    for d in docs:
        job = d.to_dict() or {}
        did = job.get("drive_id")
        if not did:
            continue
        try:
            rec = state.get_video(did)
        except Exception:
            rec = None
        posted_yt = bool(((rec or {}).get("results") or {}).get("youtube", {}).get("id"))
        if not posted_yt:
            continue   # chưa đăng -> còn cần script, giữ nguyên
        if dry_run:
            print(f"     • [prune script] {job.get('title', did)[:40]}"); n += 1; continue
        try:
            d.reference.set({"script": ""}, merge=True); n += 1
        except Exception as e:
            print(f"     ❌ prune script {did}: {e}")
    return n


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

        # Dọn field 'script' (kịch bản) của video ĐÃ ĐĂNG -> chạy LUÔN, không phụ thuộc mode Drive
        # (mode chỉ quyết định có xoá FILE video hay không; script ở Firestore B là mối lo riêng, luôn dọn).
        try:
            n_pruned = _prune_scripts(state, uid, dry_run)
            if n_pruned:
                print(f"  📄 script đã đăng -> dọn {n_pruned} bản (Firestore B)")
        except Exception as e:
            print(f"  ⚠️ prune_scripts {uid[:8]}: {e}")

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
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", action="store_true", help="Dọn ngay, bỏ qua keep_days.")
    a = ap.parse_args()
    run(dry_run=a.dry_run, force_now=a.now)


if __name__ == "__main__":
    main()
