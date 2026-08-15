"""
enqueue.py — CẦU NỐI giữa dây chuyền render và hệ thống đăng.

Sau khi render xong 1 video, gọi lệnh này => nó tự:
  1. Dựng title / description / hashtag / tags CHUẨN theo branding của kênh.
  2. Ghi sidecar .json.
  3. Đẩy video + sidecar lên đúng Drive `_QUEUE/long` hoặc `_QUEUE/short`.
=> Từ đó GitHub Actions tự lo phần đăng.

Ví dụ (gọi cuối pipeline render):
    export GOOGLE_APPLICATION_CREDENTIALS=/duong/dan/sa.json
    python src/enqueue.py \
        --channel BROKE --type short \
        --video out/broke_ep12.mp4 \
        --topic "How I Went From Broke To $10k/Month" \
        --hashtags "#money #sidehustle" \
        --publish-at 2026-08-16T20:00:00+07:00      # (tùy chọn; bỏ trống -> auto theo template)

Gọi từ Python:
    from enqueue import enqueue
    enqueue(channel="BROKE", video="out/x.mp4", vtype="short", topic="...")
"""

from __future__ import annotations
import argparse
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(__file__))
import metadata as M
from drive_client import Drive

CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "channels.yaml")


def _load_channels() -> dict:
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def enqueue(channel: str, video: str, vtype: str, topic: str,
            title: str | None = None, description: str | None = None,
            hashtags: list[str] | None = None, tags: list[str] | None = None,
            platforms: list[str] | None = None, publish_at: str | None = None,
            thumbnail: str | None = None) -> dict:
    cfg = _load_channels()
    ch = cfg["channels"].get(channel)
    if not ch:
        raise SystemExit(f"❌ Không có kênh '{channel}' trong channels.yaml")

    folder_id = os.environ.get(ch["drive_folder_id_env"])
    if not folder_id:
        raise SystemExit(f"❌ Chưa set biến {ch['drive_folder_id_env']} (Drive folder id).")

    # Dựng metadata chuẩn từ branding kênh
    raw = {"topic": topic, "type": vtype}
    for k, v in (("title", title), ("description", description),
                 ("hashtags", hashtags), ("tags", tags), ("platforms", platforms)):
        if v:
            raw[k] = v
    meta = M.build_metadata(raw, ch["branding"])
    warns = M.lint(meta)

    sidecar = {
        "topic": topic,
        "type": meta["type"],
        "title": meta["title"],
        "description": meta["description"],
        "hashtags": meta["hashtags"],
        "tags": meta["tags"],
        "platforms": meta["platforms"],
    }
    if publish_at:
        sidecar["publish_at"] = publish_at
    if thumbnail and os.path.exists(thumbnail):
        base = os.path.basename(video).rsplit(".", 1)[0]
        ext = os.path.splitext(thumbnail)[1] or ".jpg"
        sidecar["thumbnail"] = f"{base}{ext}"

    drive = Drive()
    created = drive.upload_to_queue(folder_id, video, meta["type"], sidecar, thumbnail_path=thumbnail)

    print(f"✅ Đã đưa vào hàng đợi kênh {channel} [{meta['type']}]: {meta['title']!r}")
    print(f"   Drive file id: {created['id']}")
    if warns:
        print("   ⚠️  " + " | ".join(warns))
    return created


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--video", required=True, help="Đường dẫn file video local.")
    ap.add_argument("--type", dest="vtype", choices=["long", "short"], required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--title")
    ap.add_argument("--desc", dest="description")
    ap.add_argument("--hashtags", help='VD: "#money #finance"')
    ap.add_argument("--tags", help="VD: keyword1,keyword2")
    ap.add_argument("--platforms", help="VD: youtube,facebook")
    ap.add_argument("--publish-at", dest="publish_at", help="ISO. Bỏ trống = auto theo template.")
    ap.add_argument("--thumbnail", help="Đường dẫn ảnh thumbnail (long-form nên có).")
    a = ap.parse_args()

    enqueue(
        channel=a.channel, video=a.video, vtype=a.vtype, topic=a.topic,
        title=a.title, description=a.description,
        hashtags=a.hashtags.split() if a.hashtags else None,
        tags=a.tags.split(",") if a.tags else None,
        platforms=a.platforms.split(",") if a.platforms else None,
        publish_at=a.publish_at, thumbnail=a.thumbnail,
    )


if __name__ == "__main__":
    main()
