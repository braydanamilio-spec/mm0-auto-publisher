"""
import_project.py — NHẬP video THÔNG MINH, nhận diện NHIỀU KIỂU ĐẶT TÊN / cấu trúc.

Hỗ trợ (tự nhận, không bắt theo 1 chuẩn cứng):
  FORMAT A — mỗi video 1 thư mục + UPLOAD.md (## Title / ## Description / ## Hashtags trong ``` ```).
  FORMAT B — nhiều video phẳng, mỗi video kèm <tên>.json / <tên>.txt / <tên>.md.
  FORMAT C — 1 file "*-UPLOAD.txt" mô tả CẢ long + shorts, chia mục:
                ━━━ LONG ━━━ / ━━━ SHORT 1 ━━━ ...
                TITLE:  / DESCRIPTION: / TAGS (...): / #hashtags
                FILES: video=... · thumb=... · captions=...
             shorts nằm trong subfolder shorts/ + .json per-video.
  + per-video .json với key linh hoạt (title/description/tags/hashtags/keywords/type).

Nhận diện chung:
  • VIDEO .mp4/.mov/.mkv/.webm (bỏ .md/.txt/.png/.jpg/.srt). Lẫn nhiều file vẫn tìm đúng.
  • LONG/SHORT: --type ép tay > metadata.type > đường dẫn (shorts/reel/fNNN-sN/_short) > dọc/ngắn(<3') > long.
  • THUMBNAIL: FILES:thumb= > thumbnail-choice.txt(a/b/c) > *-thumb.* > thumbnail/cover > ảnh bất kỳ.
  • PHỤ ĐỀ: FILES:captions= > .srt > .vtt > .ass.
  • Đăng THEO THỨ TỰ TĂNG DẦN (01,02,...,10). Bỏ qua brand/Đã đăng/_POSTED. Dedup chống trùng.

Chạy:
  python scripts/import_project.py --project "/.../beyond" --channel VN_B10_CUDJFJOGOFMC --dry-run
  python scripts/import_project.py --project "/.../beyond" --channel VN_B10_CUDJFJOGOFMC
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
SKIP_DIRS = {"brand", "branding", "đã đăng", "da dang", "_posted", "_sent", "_dup",
             "assets", "raw", "temp", "tmp", "cache", "node_modules"}
_SECTION_HDR = re.compile(r"(?im)^[\s━=–—*_\-]*(LONG|SHORTS?)\s*(\d*)[\s━=–—*_\-]*$")


def _natkey(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s or ""))]


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


# ---------- FORMAT C: file *-UPLOAD.txt chia mục LONG/SHORT ----------
def _parse_block(body: str) -> dict:
    out, mode, buf = {}, None, []

    def flush():
        if mode == "title" and buf:
            out["title"] = " ".join(x.strip() for x in buf if x.strip())[:200]
        elif mode == "desc" and buf:
            out["description"] = "\n".join(buf).strip()

    for ln in body.splitlines():
        s = ln.strip()
        low = s.lower()
        if low.startswith("title:"):
            flush(); mode, buf = "title", ([s[6:].strip()] if s[6:].strip() else [])
        elif low.startswith("description:"):
            flush(); mode, buf = "desc", ([s[12:].strip()] if s[12:].strip() else [])
        elif low.startswith("tags"):
            flush(); mode = None
            after = s.split(":", 1)[1] if ":" in s else ""
            out.setdefault("tags", []).extend([t.strip() for t in after.split(",") if t.strip()])
            mode = "tags"
        elif low.startswith("files:"):
            flush(); mode = None
            for k, v in re.findall(r"(\w+)\s*=\s*([^\s·|]+)", s):
                out[k.lower()] = v
        elif s.startswith("#"):
            out.setdefault("hashtags", []).extend([w for w in s.split() if w.startswith("#")])
        elif low.startswith(("— credits", "- credits", "credits", "footage:", "music:", "sound effects:")):
            flush(); mode = None
        else:
            if mode == "title" and s:
                buf.append(s)
            elif mode == "desc":
                buf.append(ln)
            elif mode == "tags" and s:
                out["tags"].extend([t.strip() for t in s.split(",") if t.strip()])
    flush()
    return out


def _parse_sections(text: str) -> list[dict]:
    hdrs = list(_SECTION_HDR.finditer(text))
    if not hdrs:
        return []
    secs = []
    for i, h in enumerate(hdrs):
        start = h.end()
        end = hdrs[i + 1].start() if i + 1 < len(hdrs) else len(text)
        secs.append(_parse_block(text[start:end]))
    return secs


def _find_upload_txt(folder: str, project: str) -> str | None:
    """Tìm file *upload*.txt/.md ở thư mục này hoặc các thư mục cha (tới gốc project)."""
    cur = folder
    root = os.path.abspath(project)
    for _ in range(6):
        try:
            for f in sorted(os.listdir(cur)):
                lf = f.lower()
                if ("upload" in lf) and lf.endswith((".txt", ".md")):
                    return os.path.join(cur, f)
        except Exception:
            pass
        if os.path.abspath(cur) == root:
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


# ---------- FORMAT A: UPLOAD.md dạng ## Title trong ``` ``` ----------
def _fence(md: str, heading: str) -> str | None:
    m = re.search(rf"^#+\s*{re.escape(heading)}[^\n]*\n(.*?)```(.*?)```", md, re.I | re.S | re.M)
    if m:
        return m.group(2).strip()
    m = re.search(rf"^#+\s*{re.escape(heading)}[^\n]*\n(.+?)(?=^#+\s|\Z)", md, re.I | re.S | re.M)
    return m.group(1).strip() if m else None


def _norm_json(d: dict) -> dict:
    out = {}
    if d.get("title") or d.get("topic"):
        out["title"] = d.get("title") or d.get("topic")
    if d.get("description") or d.get("desc"):
        out["description"] = d.get("description") or d.get("desc")
    h = d.get("hashtags") or d.get("tags_hash")
    if h:
        out["hashtags"] = h if isinstance(h, list) else str(h).split()
    tg = d.get("tags") or d.get("keywords")
    if tg:
        out["tags"] = tg if isinstance(tg, list) else [t.strip() for t in str(tg).split(",")]
    if d.get("type"):
        out["type"] = d["type"]
    return out


def read_meta(folder: str, video: str, project: str) -> tuple[dict, dict]:
    """Trả (meta, extra). extra có thể chứa thumb/sub (đường dẫn tuyệt đối) từ FILES:."""
    base = os.path.basename(video).rsplit(".", 1)[0]
    vb = os.path.basename(video).lower()

    # 1) JSON theo tên (key linh hoạt)
    for jn in (base + ".json", base + "-upload.json"):
        jp = os.path.join(folder, jn)
        if os.path.exists(jp):
            try:
                return _norm_json(json.load(open(jp, encoding="utf-8")) or {}), {}
            except Exception:
                pass

    # 2) FORMAT C — *-UPLOAD.txt chia mục, map theo FILES: video=
    up = _find_upload_txt(folder, project)
    if up and up.lower().endswith(".txt"):
        text = open(up, encoding="utf-8", errors="ignore").read()
        for sec in _parse_sections(text):
            sv = sec.get("video")
            if sv and os.path.basename(sv).lower() == vb:
                meta, extra = {}, {}
                for k in ("title", "description", "hashtags", "tags", "type"):
                    if sec.get(k):
                        meta[k] = sec[k]
                updir = os.path.dirname(up)
                if sec.get("thumb"):
                    tp = os.path.join(updir, sec["thumb"])
                    if os.path.exists(tp):
                        extra["thumb"] = tp
                if sec.get("captions"):
                    cp = os.path.join(updir, sec["captions"])
                    if os.path.exists(cp):
                        extra["sub"] = cp
                return meta, extra

    # 3) FORMAT A — UPLOAD.md dạng ## Title (khi 1 video/thư mục)
    if up and up.lower().endswith(".md"):
        md = open(up, encoding="utf-8", errors="ignore").read()
        out = {}
        t = _fence(md, "Title")
        if t:
            out["title"] = t.splitlines()[0].strip()
        d = _fence(md, "Description")
        if d:
            out["description"] = d.strip()
        h = _fence(md, "Hashtags") or _fence(md, "Tags")
        if h:
            out["hashtags"] = [w for w in re.split(r"[\s,]+", h) if w]
        if out:
            return out, {}

    # 4) <base>.txt (dòng đầu = tiêu đề)
    tp = os.path.join(folder, base + ".txt")
    if os.path.exists(tp):
        return {"title": open(tp, encoding="utf-8", errors="ignore").readline().strip()}, {}
    return {}, {}


def _dims_duration(video: str, md: str):
    portrait, secs = None, None
    m = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", md)
    if m:
        portrait = int(m.group(2)) > int(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*s\b", md)
    if m:
        secs = float(m.group(1))
    if portrait is None or secs is None:
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                  "-show_entries", "stream=width,height:format=duration",
                                  "-of", "json", video], capture_output=True, text=True, timeout=30)
            j = json.loads(out.stdout or "{}")
            st = (j.get("streams") or [{}])[0]
            if portrait is None and st.get("width"):
                portrait = int(st["height"]) > int(st["width"])
            if secs is None and j.get("format", {}).get("duration"):
                secs = float(j["format"]["duration"])
        except Exception:
            pass
    return portrait, secs


def detect_type(video: str, folder: str, meta: dict, force: str | None) -> str:
    if force:
        return force
    if str(meta.get("type", "")).lower() in ("short", "long"):
        return meta["type"].lower()
    path = (folder + "/" + os.path.basename(video)).lower()
    if re.search(r"(/shorts?/|reel|f\d+-s\d+|[-_]s\d\b|[-_]short\b|_short-\d)", path):
        return "short"
    if re.search(r"([-_/]long\b|/long/|-long\.)", path):
        return "long"
    portrait, secs = _dims_duration(video, "")
    if portrait is True or (secs is not None and secs < 180):
        return "short"
    return "long"


def pick_thumbnail(folder: str, base: str) -> str | None:
    choice = os.path.join(folder, "thumbnail-choice.txt")
    if os.path.exists(choice):
        txt = open(choice, encoding="utf-8", errors="ignore").read().strip().lower()
        mf = re.search(r"([^\s/]+\.(?:png|jpg|jpeg|webp))", txt)
        if mf and os.path.exists(os.path.join(folder, os.path.basename(mf.group(1)))):
            return os.path.join(folder, os.path.basename(mf.group(1)))
        ml = re.search(r"\b([abc])\b", txt)
        if ml:
            for e in IMG_EXT:
                c = os.path.join(folder, f"{base}-thumb-{ml.group(1)}{e}")
                if os.path.exists(c):
                    return c
    for name in (f"{base}-thumb", f"{base}_thumb", base, "thumbnail", "cover", "thumb"):
        for e in IMG_EXT:
            c = os.path.join(folder, name + e)
            if os.path.exists(c):
                return c
    for f in sorted(os.listdir(folder)):
        if f.lower().endswith(IMG_EXT) and not re.search(r"-thumb-[abc]\.", f.lower()):
            return os.path.join(folder, f)
    return None


def import_video(video: str, channel: str, force_type: str | None, dry_run: bool, project: str) -> str:
    folder = os.path.dirname(video)
    base = os.path.basename(video).rsplit(".", 1)[0]
    meta, extra = read_meta(folder, video, project)
    vtype = detect_type(video, folder, meta, force_type)
    topic = meta.get("title") or base.replace("-", " ").replace("_", " ").title()
    thumb = extra.get("thumb") or pick_thumbnail(folder, base)
    sub = extra.get("sub") or _first(folder, SUB_EXT, base) or \
        (_first(folder, SUB_EXT) if len(_videos_in(folder)) <= 1 else None)
    tag = f"[{vtype}] {os.path.basename(video)}" + (" +thumb" if thumb else "") + (" +sub" if sub else "")
    if dry_run:
        return "(dry) " + tag + (f"  «{meta['title'][:40]}»" if meta.get("title") else "")
    res = enqueue(channel=channel, video=video, vtype=vtype, topic=topic,
                  title=meta.get("title"), description=meta.get("description"),
                  hashtags=meta.get("hashtags"), tags=meta.get("tags"),
                  thumbnail=thumb, subtitle=sub)
    return "trùng -> bỏ qua" if res.get("duplicate") else "OK " + tag


def collect_videos(project: str) -> list[str]:
    found = []
    for root, dirs, _ in os.walk(project):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIRS]
        found.extend(_videos_in(root))
    found = sorted(set(found), key=lambda v: _natkey(os.path.relpath(v, project)))
    return found


def run(project: str, channel: str, force_type: str | None, dry_run: bool):
    if os.path.isfile(project) and project.lower().endswith(VIDEO_EXT):
        print(f"📄 {os.path.basename(project)}: "
              f"{import_video(project, channel, force_type, dry_run, os.path.dirname(project))}")
        return
    if not os.path.isdir(project):
        raise SystemExit(f"❌ Không thấy: {project}")
    n = {"long": 0, "short": 0, "dup": 0}
    for video in collect_videos(project):
        rel = os.path.relpath(video, project)
        try:
            r = import_video(video, channel, force_type, dry_run, project)
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
    ap.add_argument("--type", choices=["long", "short"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(a.project, a.channel, a.type, a.dry_run)


if __name__ == "__main__":
    main()
