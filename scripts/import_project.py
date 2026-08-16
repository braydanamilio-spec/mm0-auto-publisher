"""
import_project.py — NHẬP video THÔNG MINH, ĐA CẤU TRÚC vào hệ đăng.

Nhận được NHIỀU kiểu sắp xếp (không bắt anh theo 1 chuẩn cứng):
  A) Mỗi video 1 thư mục + UPLOAD.md   (vd: 001-alarm-never-rang/ , shorts/f006-.../)
  B) Nhiều video phẳng trong 1 thư mục (mỗi video kèm <tên>.json / <tên>.txt / <tên>.md)
  C) Lồng nhiều tầng           (quét đệ quy toàn bộ, gặp .mp4 là nhận)

Tự nhận diện (không cần sắp phẳng):
  • VIDEO   : .mp4/.mov/.mkv/.webm (bỏ qua .md/.txt/.png/.srt...). Nhiều file lẫn vẫn tìm đúng.
  • LONG/SHORT (theo thứ tự ưu tiên):
      1. --type ép tay
      2. metadata ghi rõ type: short/long
      3. đường dẫn chứa 'short'/'reel'/'/shorts/'  -> short ; 'long' -> long
      4. tên kiểu fNNN-sN / -s1 / -short           -> short
      5. kích thước dọc (cao > rộng) hoặc thời lượng < 3' (đọc từ UPLOAD.md hoặc ffprobe) -> short
      6. mặc định long
  • METADATA: UPLOAD.md / publish.md / meta.md (mức thư mục) hoặc <tên>.json / <tên>.md / <tên>.txt (mức file)
  • TITLE/DESCRIPTION/HASHTAGS: đọc từ các mục ## Title / ## Description / ## Hashtags (hoặc JSON)
  • THUMBNAIL: thumbnail-choice.txt (a/b/c) -> *-thumb.png -> thumbnail/cover/thumb.png -> ảnh bất kỳ
  • PHỤ ĐỀ  : .srt (ưu tiên) / .vtt / .ass
  • BỎ QUA  : brand/ , Đã đăng/ , _POSTED/_sent/_dup , assets/raw , thư mục không có video
  • CHỐNG TRÙNG: dedup theo nội dung (chạy lại chỉ nhận video mới).

Chạy:
  python scripts/import_project.py --project "/.../black-start" --channel VN_B10_CUDJFJOGOFMC --dry-run
  python scripts/import_project.py --project "/.../black-start" --channel VN_B10_CUDJFJOGOFMC
  python scripts/import_project.py --project ... --channel ... --type short   # ép toàn bộ short
"""

from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from enqueue import enqueue

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
SUB_EXT = (".srt", ".vtt", ".ass")
META_FILES = ("upload.md", "publish.md", "meta.md", "metadata.md")
SKIP_DIRS = {"brand", "branding", "đã đăng", "da dang", "_posted", "_sent", "_dup",
             "assets", "raw", "src", "temp", "tmp", "cache", "node_modules"}


def _videos_in(folder: str) -> list[str]:
    return [os.path.join(folder, f) for f in sorted(os.listdir(folder))
            if f.lower().endswith(VIDEO_EXT) and not f.startswith(".")]


def _first(folder: str, exts: tuple[str, ...], base: str | None = None) -> str | None:
    for f in sorted(os.listdir(folder)):
        if f.startswith(".") or not f.lower().endswith(exts):
            continue
        if base and not f.lower().startswith(base.lower()):
            continue
        return os.path.join(folder, f)
    return None


def _fence(md: str, heading: str) -> str | None:
    m = re.search(rf"^#+\s*{re.escape(heading)}[^\n]*\n(.*?)```(.*?)```", md,
                  re.I | re.S | re.M)
    if m:
        return m.group(2).strip()
    m = re.search(rf"^#+\s*{re.escape(heading)}[^\n]*\n(.+?)(?=^#+\s|\Z)", md, re.I | re.S | re.M)
    return m.group(1).strip() if m else None


def read_meta(folder: str, video: str) -> tuple[dict, str]:
    """Trả (meta, md_text). Ưu tiên metadata theo TÊN FILE, rồi tới metadata mức thư mục."""
    base = os.path.basename(video).rsplit(".", 1)[0]
    # 1) JSON theo tên
    jp = os.path.join(folder, base + ".json")
    if os.path.exists(jp):
        try:
            return (json.load(open(jp, encoding="utf-8")) or {}), ""
        except Exception:
            pass
    # 2) .md/.txt theo tên, hoặc UPLOAD.md mức thư mục (khi 1 video/thư mục)
    md_path = None
    for cand in (base + ".md", base + "-upload.md"):
        if os.path.exists(os.path.join(folder, cand)):
            md_path = os.path.join(folder, cand)
            break
    if not md_path and len(_videos_in(folder)) <= 1:
        for mf in os.listdir(folder):
            if mf.lower() in META_FILES:
                md_path = os.path.join(folder, mf)
                break
    out = {}
    md = ""
    if md_path:
        md = open(md_path, encoding="utf-8", errors="ignore").read()
        t = _fence(md, "Title")
        if t:
            out["title"] = t.splitlines()[0].strip()
        d = _fence(md, "Description")
        if d:
            out["description"] = d.strip()
        h = _fence(md, "Hashtags") or _fence(md, "Tags")
        if h:
            out["hashtags"] = [w for w in re.split(r"[\s,]+", h) if w]
    else:
        tp = os.path.join(folder, base + ".txt")
        if os.path.exists(tp):
            out["title"] = open(tp, encoding="utf-8", errors="ignore").readline().strip()
    return out, md


def _dims_duration(video: str, md: str):
    """(portrait?, seconds) — đọc từ UPLOAD.md trước; nếu không có thì thử ffprobe."""
    portrait = None
    secs = None
    m = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", md)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        portrait = h > w
    m = re.search(r"(\d+(?:\.\d+)?)\s*s\b", md)
    if m:
        secs = float(m.group(1))
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", md)
    if m and secs is None:
        secs = int(m.group(1)) * 60 + int(m.group(2))
    if portrait is None or secs is None:
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                  "-show_entries", "stream=width,height:format=duration",
                                  "-of", "json", video], capture_output=True, text=True, timeout=30)
            j = json.loads(out.stdout or "{}")
            st = (j.get("streams") or [{}])[0]
            if portrait is None and st.get("width") and st.get("height"):
                portrait = int(st["height"]) > int(st["width"])
            if secs is None and j.get("format", {}).get("duration"):
                secs = float(j["format"]["duration"])
        except Exception:
            pass
    return portrait, secs


def detect_type(video: str, folder: str, meta: dict, md: str, force: str | None) -> str:
    if force:
        return force
    if str(meta.get("type", "")).lower() in ("short", "long"):
        return meta["type"].lower()
    path = (folder + "/" + os.path.basename(video)).lower()
    if re.search(r"(/shorts?/|reel|f\d+-s\d+|[-_]s\d\b|[-_]short\b)", path):
        return "short"
    if re.search(r"([-_/]long\b|/long/)", path):
        return "long"
    portrait, secs = _dims_duration(video, md)
    if portrait is True:
        return "short"
    if secs is not None and secs < 180:
        return "short"
    return "long"


def pick_thumbnail(folder: str, base: str) -> str | None:
    choice = os.path.join(folder, "thumbnail-choice.txt")
    if os.path.exists(choice):
        txt = open(choice, encoding="utf-8", errors="ignore").read().strip().lower()
        mf = re.search(r"([^\s/]+\.(?:png|jpg|jpeg|webp))", txt)
        if mf:
            c = os.path.join(folder, os.path.basename(mf.group(1)))
            if os.path.exists(c):
                return c
        ml = re.search(r"\b([abc])\b", txt)
        if ml:
            for e in IMG_EXT:
                c = os.path.join(folder, f"{base}-thumb-{ml.group(1)}{e}")
                if os.path.exists(c):
                    return c
    for name in (f"{base}-thumb", f"{base}", "thumbnail", "cover", "thumb"):
        for e in IMG_EXT:
            c = os.path.join(folder, name + e)
            if os.path.exists(c):
                return c
    # ảnh bất kỳ không phải biến thể a/b/c
    for f in sorted(os.listdir(folder)):
        if f.lower().endswith(IMG_EXT) and not re.search(r"-thumb-[abc]\.", f.lower()):
            return os.path.join(folder, f)
    return None


def import_video(video: str, channel: str, force_type: str | None, dry_run: bool) -> str:
    folder = os.path.dirname(video)
    base = os.path.basename(video).rsplit(".", 1)[0]
    meta, md = read_meta(folder, video)
    vtype = detect_type(video, folder, meta, md, force_type)
    topic = meta.get("title") or base.replace("-", " ").replace("_", " ").title()
    thumb = pick_thumbnail(folder, base)
    sub = _first(folder, SUB_EXT, base) or (_first(folder, SUB_EXT) if len(_videos_in(folder)) <= 1 else None)
    tag = f"[{vtype}] {os.path.basename(video)}" + (" +thumb" if thumb else "") + (" +sub" if sub else "")
    if dry_run:
        return "(dry) " + tag
    res = enqueue(channel=channel, video=video, vtype=vtype, topic=topic,
                  title=meta.get("title"), description=meta.get("description"),
                  hashtags=meta.get("hashtags"), thumbnail=thumb, subtitle=sub)
    return "trùng -> bỏ qua" if res.get("duplicate") else "OK " + tag


def _natkey(s: str):
    """Khoá sắp xếp TỰ NHIÊN: 001 < 002 < ... < 010 (số so theo giá trị, không theo chữ)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def iter_video_folders(project: str):
    """Đệ quy: gặp .mp4 ở đâu cũng nhận, bỏ qua thư mục cấm."""
    found = []
    for root, dirs, _files in os.walk(project):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIRS]
        found.extend(_videos_in(root))
    # SẮP XẾP TĂNG DẦN theo số thứ tự (01,02,...) -> đăng lần lượt, không lộn xộn
    found.sort(key=lambda v: _natkey(os.path.relpath(v, project)))
    return found


def run(project: str, channel: str, force_type: str | None, dry_run: bool):
    # Trỏ thẳng 1 FILE video cũng được (nhận đúng file đó + tìm metadata/thumb/sub cạnh nó)
    if os.path.isfile(project) and project.lower().endswith(VIDEO_EXT):
        print(f"📄 {os.path.basename(project)}: {import_video(project, channel, force_type, dry_run)}")
        return
    if not os.path.isdir(project):
        raise SystemExit(f"❌ Không thấy: {project}")
    n = {"long": 0, "short": 0, "dup": 0}
    seen = set()
    for video in iter_video_folders(project):
        if video in seen:
            continue
        seen.add(video)
        rel = os.path.relpath(video, project)
        try:
            r = import_video(video, channel, force_type, dry_run)
        except SystemExit as e:
            r = f"❌ {e}"
        except Exception as e:
            r = f"❌ {e}"
        print(f"📄 {rel}: {r}")
        if "[short]" in r:
            n["short"] += 1
        elif "[long]" in r:
            n["long"] += 1
        if "trùng" in r:
            n["dup"] += 1
    print(f"\n✔ Xong. Long: {n['long']} · Short: {n['short']} · Trùng bỏ qua: {n['dup']}"
          f"{' (DRY-RUN)' if dry_run else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--channel", required=True, help="Nhãn kênh (vd VN_B10_CUDJFJOGOFMC).")
    ap.add_argument("--type", choices=["long", "short"], help="Ép tất cả long/short.")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(a.project, a.channel, a.type, a.dry_run)


if __name__ == "__main__":
    main()
