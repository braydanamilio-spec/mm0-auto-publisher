"""
import_project.py — NHẬP video từ cấu trúc "folder-per-video + UPLOAD.md" (dây chuyền làm video)
vào hệ thống đăng (tự upload lên kho pool + đặt lịch).

Cấu trúc THẬT được hỗ trợ (khớp output của anh):
  <project>/
    001-alarm-never-rang/           <- video LONG (thư mục ở gốc)
        001-alarm-never-rang.mp4
        001-alarm-never-rang.srt        (phụ đề, nếu có)
        001-alarm-never-rang-thumb.png  (thumbnail chính)
        001-...-thumb-a/b/c.png         (lựa chọn thumbnail)
        thumbnail-choice.txt            (chọn thumbnail nào)
        UPLOAD.md                       (Title / Description / Hashtags)
    shorts/
        f006-...-the-fuel/          <- video SHORT (thư mục dưới shorts/)
            *.mp4 + *-thumb.png + UPLOAD.md
        Đã đăng/                    <- BỎ QUA (đã đăng)
    brand/                          <- BỎ QUA (branding, không phải video)

Nhận diện:
  - Tự tìm file .mp4 trong thư mục (bỏ qua .md/.txt/.png/.srt/.vtt).
  - LONG nếu thư mục ở gốc; SHORT nếu nằm dưới 'shorts/'. (Ép tay: --type long|short)
  - Title/Description/Hashtags đọc từ UPLOAD.md.
  - Thumbnail: theo thumbnail-choice.txt, else *-thumb.png.
  - Phụ đề: .srt (ưu tiên) hoặc .vtt.
  - Bỏ qua: brand/, Đã đăng/, thư mục không có .mp4.
  - Chống trùng: dedup theo nội dung (không upload lại video đã có).

Chạy:
  export GOOGLE_APPLICATION_CREDENTIALS=/duong/dan/sa.json
  export FIREBASE_PROJECT_ID=...
  python scripts/import_project.py --project "/duong/dan/black-start" --channel VN_B10_CUDJFJOGOFMC
  python scripts/import_project.py --project ... --channel ... --dry-run      # chỉ xem
  python scripts/import_project.py --project ... --channel ... --type short   # ép tất cả là short
"""

from __future__ import annotations
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from enqueue import enqueue

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm")
SKIP_DIRS = {"brand", "đã đăng", "da dang", "_posted", "_sent", "_dup", "assets", "raw"}


def _find(folder: str, exts: tuple[str, ...]) -> str | None:
    for fn in sorted(os.listdir(folder)):
        if fn.lower().endswith(exts) and not fn.startswith("."):
            return os.path.join(folder, fn)
    return None


def _fence_after(md: str, heading: str) -> str | None:
    """Lấy nội dung trong ``` ... ``` NGAY SAU dòng '## <heading>'."""
    m = re.search(rf"^#+\s*{re.escape(heading)}[^\n]*\n(.*?)```(.*?)```", md,
                  re.IGNORECASE | re.DOTALL | re.MULTILINE)
    if m:
        return m.group(2).strip()
    # fallback: đoạn text sau heading tới heading kế
    m = re.search(rf"^#+\s*{re.escape(heading)}[^\n]*\n(.+?)(?=^#+\s|\Z)", md,
                  re.IGNORECASE | re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else None


def parse_upload_md(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    md = open(path, encoding="utf-8", errors="ignore").read()
    out = {}
    t = _fence_after(md, "Title")
    if t:
        out["title"] = t.splitlines()[0].strip()
    d = _fence_after(md, "Description")
    if d:
        out["description"] = d.strip()
    h = _fence_after(md, "Hashtags") or _fence_after(md, "Tags")
    if h:
        out["hashtags"] = [w for w in re.split(r"[\s,]+", h) if w.startswith("#") or w]
    return out


def pick_thumbnail(folder: str, base: str) -> str | None:
    # 1) thumbnail-choice.txt -> tên file .png hoặc chữ a/b/c
    choice = os.path.join(folder, "thumbnail-choice.txt")
    if os.path.exists(choice):
        txt = open(choice, encoding="utf-8", errors="ignore").read().strip().lower()
        mfile = re.search(r"([^\s/]+\.png)", txt)
        if mfile:
            cand = os.path.join(folder, os.path.basename(mfile.group(1)))
            if os.path.exists(cand):
                return cand
        mlet = re.search(r"\b([abc])\b", txt)
        if mlet:
            cand = os.path.join(folder, f"{base}-thumb-{mlet.group(1)}.png")
            if os.path.exists(cand):
                return cand
    # 2) thumbnail chính
    for name in (f"{base}-thumb.png", f"{base}.png"):
        cand = os.path.join(folder, name)
        if os.path.exists(cand):
            return cand
    # 3) bất kỳ *-thumb.png
    for fn in sorted(os.listdir(folder)):
        if fn.lower().endswith("-thumb.png"):
            return os.path.join(folder, fn)
    return None


def import_folder(folder: str, channel: str, vtype: str, dry_run: bool) -> str:
    video = _find(folder, VIDEO_EXT)
    if not video:
        return "skip (không có video .mp4)"
    base = os.path.basename(video).rsplit(".", 1)[0]
    meta = parse_upload_md(os.path.join(folder, "UPLOAD.md"))
    topic = meta.get("title") or base.replace("-", " ").replace("_", " ").title()
    thumb = pick_thumbnail(folder, base)
    sub = _find(folder, (".srt",)) or _find(folder, (".vtt",))
    res = enqueue(
        channel=channel, video=video, vtype=vtype, topic=topic,
        title=meta.get("title"), description=meta.get("description"),
        hashtags=meta.get("hashtags"), thumbnail=thumb, subtitle=sub,
    )
    if res.get("duplicate"):
        return "trùng -> bỏ qua"
    return f"OK [{vtype}] {os.path.basename(video)}" + (f" +thumb" if thumb else "") + (f" +sub" if sub else "")


def run(project: str, channel: str, force_type: str | None, dry_run: bool):
    if not os.path.isdir(project):
        raise SystemExit(f"❌ Không thấy thư mục: {project}")
    n_long = n_short = 0

    def _skip(name: str) -> bool:
        return name.startswith(".") or name.lower() in SKIP_DIRS

    # LONG: thư mục ở gốc (trừ shorts/ và các thư mục bỏ qua)
    for name in sorted(os.listdir(project)):
        p = os.path.join(project, name)
        if not os.path.isdir(p) or _skip(name) or name.lower() == "shorts":
            continue
        vtype = force_type or "long"
        if _find(p, VIDEO_EXT):
            print(f"📁 {name}: ", end="")
            print(import_folder(p, channel, vtype, dry_run) if not dry_run
                  else f"(dry) [{vtype}] {os.path.basename(_find(p, VIDEO_EXT))}")
            n_long += 1

    # SHORT: thư mục dưới shorts/
    sdir = os.path.join(project, "shorts")
    if os.path.isdir(sdir):
        for name in sorted(os.listdir(sdir)):
            p = os.path.join(sdir, name)
            if not os.path.isdir(p) or _skip(name):
                continue
            vtype = force_type or "short"
            if _find(p, VIDEO_EXT):
                print(f"📁 shorts/{name}: ", end="")
                print(import_folder(p, channel, vtype, dry_run) if not dry_run
                      else f"(dry) [{vtype}] {os.path.basename(_find(p, VIDEO_EXT))}")
                n_short += 1

    print(f"\n✔ Xong. Long: {n_long} · Short: {n_short}"
          f"{' (DRY-RUN, chưa upload)' if dry_run else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="Thư mục dự án (vd .../black-start).")
    ap.add_argument("--channel", required=True, help="Nhãn kênh định tuyến (vd VN_B10_CUDJFJOGOFMC).")
    ap.add_argument("--type", choices=["long", "short"], help="Ép tất cả là long/short (bỏ qua tự nhận).")
    ap.add_argument("--dry-run", action="store_true", help="Chỉ xem, không upload.")
    a = ap.parse_args()
    run(a.project, a.channel, a.type, a.dry_run)


if __name__ == "__main__":
    main()
