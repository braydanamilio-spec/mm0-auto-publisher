"""
import_legacy.py — NẠP KHO video làm sẵn TRƯỚC pipeline (3 kênh cũ) vào hệ thống đăng chuẩn.

TÁCH RIÊNG khỏi dây chuyền render — chỉ dùng 1 lần (chạy lại an toàn nhờ sổ chống trùng dedup).

Nguồn (folder trong ~/Documents):
  ch5_beyond    -> kênh BEYOND    (long: *-LONG.mp4 + *-LONG.json + _thumb.jpg + .srt; shorts/*.json)
  ch6_legacy    -> kênh LEGACY    (cùng format ch5)
  ch7_datarace  -> kênh RANKRUSH  (KÊNH MỚI, khác kênh DATARACE của hệ thống — user làm riêng máy cũ;
                                   metadata từ *-UPLOAD.txt, gồm videos/ + shorts/ toàn cục + compilations/;
                                   tự REBRAND "Data Race"/#datarace -> "Rank Rush"/#rankrush trong title+desc)

Mỗi video -> gọi src/enqueue.enqueue(): dựng sidecar chuẩn (title/desc/hashtags/tags/thumbnail/
captions) + đẩy lên Drive kho pool `_QUEUE/<long|short>` -> hệ thống tự nhận khi đăng.

Chạy:
  python3 scripts/import_legacy.py --dry-run                  # kiểm kê + validate metadata, KHÔNG cần credentials
  export GOOGLE_APPLICATION_CREDENTIALS=~/sa.json             # cần khi chạy thật (đọc kho pool + sổ dedup)
  python3 scripts/import_legacy.py                            # nạp tất cả
  python3 scripts/import_legacy.py --channels BEYOND,LEGACY --limit 5
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

OWNER_DEFAULT = "MW0vCcIkw9TNqsd8imuZZt0EdIc2"   # uid chủ (mrquyenbk@mm0user.app)
SRC_CANDIDATES = [
    os.path.expanduser("~/Documents"),
    "/Users/khieudinhquyen/Documents",
    "/Users/mrquyenbk/Documents",
]
FOLDERS = {"BEYOND": "ch5_beyond", "LEGACY": "ch6_legacy", "RANKRUSH": "ch7_datarace"}

# ch7 làm dưới brand cũ "DATA RACE" -> đổi sang brand mới RANK RUSH (không trùng kênh DATARACE của hệ thống)
REBRAND = [("DATA RACE", "RANK RUSH"), ("Data Race", "Rank Rush"), ("data race", "rank rush"),
           ("#datarace", "#rankrush"), ("#DataRace", "#rankrush"), ("@dataracehq", "@rankrush")]


def _rebrand(s):
    if not s:
        return s
    for old, new in REBRAND:
        s = s.replace(old, new)
    return s

# chỉ nhận hashtag chữ (#beyond, #barchartrace) — KHÔNG bắt "#1" trong tiêu đề/mô tả
TAG = r"#[A-Za-z_][\w']*"
HASHTAG_LINE = re.compile(rf"^\s*(?:{TAG}\s*)+$", re.M)
HASHTAG_TAIL = re.compile(rf"(?:\s+{TAG})+\s*$", re.M)


def _find_root() -> str:
    for base in SRC_CANDIDATES:
        if all(os.path.isdir(os.path.join(base, f)) for f in FOLDERS.values()):
            return base
    raise SystemExit("❌ Không tìm thấy 3 folder nguồn (ch5_beyond/ch6_legacy/ch7_datarace) trong ~/Documents")


def _split_hashtags(desc: str):
    """Tách dòng chỉ-toàn-hashtag khỏi mô tả -> (desc sạch, [hashtags]) để build_metadata tự chèn, không trùng đôi."""
    tags = []
    for m in HASHTAG_LINE.finditer(desc):
        tags += re.findall(TAG, m.group(0))
    clean = HASHTAG_LINE.sub("", desc)
    for m in HASHTAG_TAIL.finditer(clean):
        tags += re.findall(TAG, m.group(0))
    clean = HASHTAG_TAIL.sub("", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    seen, uniq = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    return clean, uniq


def _from_json_dir(channel: str, vdir: str):
    """ch5/ch6: 1 folder video = 1 long (-LONG.json) + shorts/*.json."""
    items = []
    for jf in glob.glob(os.path.join(vdir, "*-LONG.json")):
        meta = json.load(open(jf, encoding="utf-8"))
        base = jf[:-len(".json")]
        video = base + ".mp4"
        if not os.path.exists(video):
            continue
        desc, htags = _split_hashtags(meta.get("description", ""))
        thumb = os.path.join(vdir, meta.get("thumb", "")) if meta.get("thumb") else None
        srt = base + ".srt"
        items.append(dict(channel=channel, video=video, vtype="long",
                          topic=os.path.basename(vdir),
                          title=meta.get("title"), description=desc,
                          hashtags=(meta.get("hashtags") or htags) or None,
                          tags=meta.get("tags") or None,
                          thumbnail=thumb if thumb and os.path.exists(thumb) else None,
                          subtitle=srt if os.path.exists(srt) else None))
    for jf in sorted(glob.glob(os.path.join(vdir, "shorts", "*.json"))):
        meta = json.load(open(jf, encoding="utf-8"))
        video = jf[:-len(".json")] + ".mp4"
        if not os.path.exists(video):
            continue
        desc, htags = _split_hashtags(meta.get("description", ""))
        srt = jf[:-len(".json")] + ".srt"
        items.append(dict(channel=channel, video=video, vtype="short",
                          topic=os.path.basename(vdir),
                          title=meta.get("title"), description=desc,
                          hashtags=(meta.get("hashtags") or htags) or None,
                          tags=meta.get("tags") or None, thumbnail=None,
                          subtitle=srt if os.path.exists(srt) else None))
    return items


def _parse_upload_txt(path: str):
    """UPLOAD.txt ch7 -> {"long": {title, desc, tags}, "shorts": [{title, desc}, ...]}.
    TITLE có 2 kiểu: cùng dòng ("TITLE: X") hoặc dòng kế tiếp ("TITLE:\\nX")."""
    txt = open(path, encoding="utf-8", errors="replace").read()
    out = {"long": None, "shorts": []}
    # cắt theo section ════ LONG ════ / ── SHORT N ── / [1full] ...
    sections = re.split(r"═+\s*LONG[^═\n]*═+|─+\s*SHORT\s*\d+[^─\n]*─+", txt)
    heads = re.findall(r"═+\s*LONG[^═\n]*═+|─+\s*SHORT\s*\d+[^─\n]*─+", txt)
    for head, body in zip(heads, sections[1:]):
        m = re.search(r"TITLE:\s*(\S[^\n]*)", body) or re.search(r"TITLE:\s*\n\s*([^\n]+)", body)
        title = (m.group(1).strip() if m else None)
        dm = re.search(r"DESC(?:RIPTION)?:\s*\n?(.*?)(?=\nTAGS:|\nFILES:|\Z)", body, re.S)
        desc = dm.group(1).strip() if dm else ""
        tm = re.search(r"TAGS[^:\n]*:\s*([^\n]*)", body)
        tags = [t.strip() for t in tm.group(1).split(",") if t.strip()] if tm else []
        rec = {"title": title, "desc": desc, "tags": tags or None}
        if "LONG" in head:
            out["long"] = rec
        else:
            out["shorts"].append(rec)
    # format [1full]/[2recent]/[3top5] của shorts toàn cục
    for m in re.finditer(r"\[(\d)\w+\]\s*TITLE:\s*([^\n]+)\n\s*DESC:\s*(.*?)(?=\n\s*\[|\Z)", txt, re.S):
        out["shorts"].append({"title": m.group(2).strip(), "desc": m.group(3).strip(), "tags": None,
                              "n": int(m.group(1))})
    return out


def _from_datarace(root: str):
    """ch7 -> RANKRUSH: videos/*/ + compilations/*/ (long) + shorts/ toàn cục + videos/*/shorts/."""
    items = []
    ch7 = os.path.join(root, FOLDERS["RANKRUSH"])
    for vdir in sorted(glob.glob(os.path.join(ch7, "videos", "*")) + glob.glob(os.path.join(ch7, "compilations", "*"))):
        if not os.path.isdir(vdir):
            continue
        slug = os.path.basename(vdir)
        ups = glob.glob(os.path.join(vdir, "*-UPLOAD.txt"))
        meta = _parse_upload_txt(ups[0]) if ups else {"long": None, "shorts": []}
        longs = glob.glob(os.path.join(vdir, "*-LONG.mp4"))
        if longs:
            lm = meta["long"] or {}
            desc, htags = _split_hashtags(lm.get("desc") or "")
            thumbs = glob.glob(os.path.join(vdir, "*_thumb.jpg"))
            items.append(dict(channel="DATARACE", video=longs[0], vtype="long", topic=slug,
                              title=lm.get("title"), description=desc or None,
                              hashtags=htags or None, tags=lm.get("tags"),
                              thumbnail=thumbs[0] if thumbs else None, subtitle=None))
        for i, sv in enumerate(sorted(glob.glob(os.path.join(vdir, "shorts", "*-short-*.mp4")))):
            sm = meta["shorts"][i] if i < len(meta["shorts"]) else {}
            desc, htags = _split_hashtags(sm.get("desc") or "")
            items.append(dict(channel="DATARACE", video=sv, vtype="short", topic=slug,
                              title=sm.get("title"), description=desc or None,
                              hashtags=htags or None, tags=sm.get("tags"),
                              thumbnail=None, subtitle=None))
    # shorts toàn cục: <slug>-{1full,2recent,3top5}.mp4 + <slug>-shorts-UPLOAD.txt
    gdir = os.path.join(ch7, "shorts")
    for up in sorted(glob.glob(os.path.join(gdir, "*-shorts-UPLOAD.txt"))):
        slug = os.path.basename(up)[:-len("-shorts-UPLOAD.txt")]
        meta = _parse_upload_txt(up)
        vids = sorted(glob.glob(os.path.join(gdir, slug + "-[0-9]*.mp4")))
        for i, sv in enumerate(vids):
            sm = meta["shorts"][i] if i < len(meta["shorts"]) else {}
            desc, htags = _split_hashtags(sm.get("desc") or "")
            items.append(dict(channel="DATARACE", video=sv, vtype="short", topic=slug,
                              title=sm.get("title"), description=desc or None,
                              hashtags=htags or None, tags=None, thumbnail=None, subtitle=None))
    # REBRAND tập trung: mọi item ch7 mang key RANKRUSH + thay brand cũ trong chữ
    for it in items:
        it["channel"] = "RANKRUSH"
        it["title"] = _rebrand(it["title"])
        it["description"] = _rebrand(it["description"])
        it["hashtags"] = [_rebrand(h) for h in it["hashtags"]] if it["hashtags"] else None
        it["tags"] = [_rebrand(t) for t in it["tags"]] if it["tags"] else None
    return items


def collect(root: str, channels):
    items = []
    for ch in ("BEYOND", "LEGACY"):
        if ch in channels:
            base = os.path.join(root, FOLDERS[ch], "videos")
            for vdir in sorted(glob.glob(os.path.join(base, "*"))):
                if os.path.isdir(vdir):
                    items += _from_json_dir(ch, vdir)
    if "RANKRUSH" in channels:
        items += _from_datarace(root)
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Chỉ kiểm kê + validate metadata, không upload.")
    ap.add_argument("--channels", default="BEYOND,LEGACY,RANKRUSH")
    ap.add_argument("--limit", type=int, default=0, help="Giới hạn số video (0 = tất cả).")
    ap.add_argument("--start", type=int, default=0, help="Bỏ qua N video đầu (resume).")
    ap.add_argument("--owner", default=OWNER_DEFAULT)
    ap.add_argument("--src", default=None, help="Folder Documents chứa 3 kho nguồn.")
    a = ap.parse_args()

    root = a.src or _find_root()
    channels = [c.strip().upper() for c in a.channels.split(",") if c.strip()]
    items = collect(root, channels)
    if a.start:
        items = items[a.start:]
    if a.limit:
        items = items[:a.limit]

    from collections import Counter
    cnt = Counter((it["channel"], it["vtype"]) for it in items)
    print(f"📦 Kiểm kê từ {root}: {len(items)} video")
    for (ch, vt), n in sorted(cnt.items()):
        print(f"   {ch:9s} {vt:5s}: {n}")

    import metadata as M
    import yaml
    cfg = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "config", "channels.yaml")))
    bad = 0
    for it in items:
        ch = cfg["channels"].get(it["channel"])
        if not ch:
            print(f"❌ thiếu kênh {it['channel']} trong channels.yaml"); bad += 1; continue
        raw = {k: v for k, v in it.items() if v and k in
               ("topic", "title", "description", "hashtags", "tags")}
        raw["type"] = it["vtype"]
        meta = M.build_metadata(raw, ch["branding"])
        warns = M.lint(meta)
        if not it.get("title"):
            warns.append("thiếu title (dùng topic)")
        if it["vtype"] == "long" and not it.get("thumbnail"):
            warns.append("long thiếu thumbnail")
        if warns:
            print(f"⚠️  {os.path.basename(it['video'])}: " + " | ".join(warns))
    if a.dry_run:
        print(f"✅ DRY-RUN xong ({bad} lỗi cấu hình). Chạy thật: bỏ --dry-run (cần GOOGLE_APPLICATION_CREDENTIALS).")
        return

    from enqueue import enqueue
    platforms = {"BEYOND": ["youtube"], "LEGACY": ["youtube"], "RANKRUSH": ["youtube"]}
    ok = dup = err = 0
    for i, it in enumerate(items, 1):
        print(f"\n[{i}/{len(items)}] {it['channel']} {it['vtype']} — {os.path.basename(it['video'])}")
        try:
            r = enqueue(channel=it["channel"], video=it["video"], vtype=it["vtype"],
                        topic=it["topic"], title=it.get("title"), description=it.get("description"),
                        hashtags=it.get("hashtags"), tags=it.get("tags"),
                        platforms=platforms.get(it["channel"]),
                        thumbnail=it.get("thumbnail"), subtitle=it.get("subtitle"),
                        owner=a.owner)
            if r.get("duplicate"):
                dup += 1
            else:
                ok += 1
        except SystemExit as e:          # lỗi hạ tầng (hết kho/thiếu config) -> dừng, resume bằng --start
            print(f"⛔ Dừng tại video {i}: {e}\n   Resume: --start {a.start + i - 1}")
            break
        except Exception as e:
            err += 1
            print(f"   ⚠️  Lỗi, bỏ qua video này: {e}")
    print(f"\n🏁 Xong: {ok} upload · {dup} trùng (bỏ qua) · {err} lỗi.")


if __name__ == "__main__":
    main()
