"""
watch_and_enqueue.py — Theo dõi folder OUTBOX local, thấy video mới là TỰ đẩy lên _QUEUE.

Tích hợp cực gọn cho dây chuyền render tự động qua đêm:
  Dây chuyền chỉ cần lưu video ra:
      OUTBOX/<CHANNEL>/<long|short>/<topic>.mp4
  (tùy chọn kèm <topic>.txt chứa 1 dòng tiêu đề, hoặc .json metadata đầy đủ)

  Script này quét mỗi 60 giây, đẩy lên Drive _QUEUE, rồi chuyển file local sang OUTBOX/_sent.

Chạy:
    export GOOGLE_APPLICATION_CREDENTIALS=/duong/dan/sa.json
    export BROKE_DRIVE_FOLDER_ID=...   # + các kênh khác
    python scripts/watch_and_enqueue.py --outbox ./OUTBOX
    # chạy 1 lần rồi thoát:
    python scripts/watch_and_enqueue.py --outbox ./OUTBOX --once
"""

from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from enqueue import enqueue

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm")


def scan_once(outbox: str):
    sent_dir = os.path.join(outbox, "_sent")
    os.makedirs(sent_dir, exist_ok=True)

    for channel in sorted(os.listdir(outbox)):
        ch_dir = os.path.join(outbox, channel)
        if not os.path.isdir(ch_dir) or channel.startswith("_"):
            continue
        for vtype in ("long", "short"):
            type_dir = os.path.join(ch_dir, vtype)
            if not os.path.isdir(type_dir):
                continue
            for fn in sorted(os.listdir(type_dir)):
                if not fn.lower().endswith(VIDEO_EXT):
                    continue
                path = os.path.join(type_dir, fn)
                base = fn.rsplit(".", 1)[0]

                # metadata kèm theo (nếu có)
                extra = {}
                jpath = os.path.join(type_dir, base + ".json")
                tpath = os.path.join(type_dir, base + ".txt")
                if os.path.exists(jpath):
                    with open(jpath, encoding="utf-8") as f:
                        extra = json.load(f)
                elif os.path.exists(tpath):
                    with open(tpath, encoding="utf-8") as f:
                        extra["title"] = f.readline().strip()

                # thumbnail đi kèm (nếu có): <base>.jpg / .png
                thumb = None
                for ext in (".jpg", ".jpeg", ".png"):
                    cand = os.path.join(type_dir, base + ext)
                    if os.path.exists(cand):
                        thumb = cand
                        break

                # phụ đề đi kèm (nếu có): <base>.srt / .vtt
                sub = None
                for ext in (".srt", ".vtt"):
                    cand = os.path.join(type_dir, base + ext)
                    if os.path.exists(cand):
                        sub = cand
                        break

                topic = extra.get("topic") or base.replace("_", " ").replace("-", " ").title()
                try:
                    enqueue(
                        channel=channel, video=path, vtype=vtype, topic=topic,
                        title=extra.get("title"), description=extra.get("description"),
                        hashtags=extra.get("hashtags"), tags=extra.get("tags"),
                        platforms=extra.get("platforms"), publish_at=extra.get("publish_at"),
                        thumbnail=thumb, subtitle=sub, subtitle_lang=extra.get("subtitle_lang"),
                    )
                    # chuyển file local đã gửi -> _sent (để không đẩy lại)
                    dest = os.path.join(sent_dir, channel + "__" + fn)
                    shutil.move(path, dest)
                    for side in (jpath, tpath, thumb, sub):
                        if side and os.path.exists(side):
                            shutil.move(side, os.path.join(sent_dir, channel + "__" + os.path.basename(side)))
                except Exception as e:
                    print(f"❌ Lỗi đẩy {fn}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outbox", default="./OUTBOX")
    ap.add_argument("--once", action="store_true", help="Quét 1 lần rồi thoát.")
    ap.add_argument("--interval", type=int, default=60, help="Giây giữa 2 lần quét.")
    a = ap.parse_args()

    print(f"👀 Theo dõi {a.outbox} ...")
    while True:
        scan_once(a.outbox)
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
