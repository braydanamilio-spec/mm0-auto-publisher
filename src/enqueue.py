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
            thumbnail: str | None = None, pool: bool = False,
            subtitle: str | None = None, subtitle_lang: str | None = None) -> dict:
    cfg = _load_channels()
    ch = cfg["channels"].get(channel)
    if not ch:
        raise SystemExit(f"❌ Không có kênh '{channel}' trong channels.yaml")

    # Dựng metadata chuẩn từ branding kênh
    raw = {"topic": topic, "type": vtype}
    for k, v in (("title", title), ("description", description),
                 ("hashtags", hashtags), ("tags", tags), ("platforms", platforms)):
        if v:
            raw[k] = v
    meta = M.build_metadata(raw, ch["branding"])
    warns = M.lint(meta)

    sidecar = {
        "channel": channel,          # để hệ thống định tuyến đúng kênh khi dùng pool
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
    vbase = os.path.basename(video).rsplit(".", 1)[0]
    if thumbnail and os.path.exists(thumbnail):
        ext = os.path.splitext(thumbnail)[1] or ".jpg"
        sidecar["thumbnail"] = f"{vbase}{ext}"
    if subtitle and os.path.exists(subtitle):
        sext = os.path.splitext(subtitle)[1] or ".srt"
        lang = subtitle_lang or ch["youtube"].get("default_language", "en")
        sidecar["captions"] = [{"file": f"{vbase}{sext}", "language": lang, "name": lang.upper()}]

    # Chọn đích: HỒ CHỨA (tự chọn acc còn trống) hoặc folder riêng của kênh
    if pool:
        import storage as ST
        picked = ST.pick_upload_account()
        if not picked:
            raise SystemExit("❌ Hồ chứa đã đầy hết — thêm tài khoản pool hoặc dọn dẹp.")
        acc, drive = picked
        root = acc["root"]
        where = f"pool:{acc['name']}"
    else:
        root = os.environ.get(ch["drive_folder_id_env"])
        if not root:
            raise SystemExit(f"❌ Chưa set biến {ch['drive_folder_id_env']} (Drive folder id).")
        drive = Drive()
        where = "kênh"

    created = drive.upload_to_queue(root, video, meta["type"], sidecar,
                                    thumbnail_path=thumbnail, subtitle_path=subtitle)

    print(f"✅ Đã đưa vào hàng đợi [{where}] kênh {channel} [{meta['type']}]: {meta['title']!r}")
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
    ap.add_argument("--pool", action="store_true", help="Đẩy vào HỒ CHỨA (tự chọn acc còn trống).")
    ap.add_argument("--subtitle", help="File phụ đề .srt/.vtt đi kèm (tự upload lên YouTube).")
    ap.add_argument("--subtitle-lang", dest="subtitle_lang", help="Mã ngôn ngữ phụ đề, vd en, vi.")
    a = ap.parse_args()

    enqueue(
        channel=a.channel, video=a.video, vtype=a.vtype, topic=a.topic,
        title=a.title, description=a.description,
        hashtags=a.hashtags.split() if a.hashtags else None,
        tags=a.tags.split(",") if a.tags else None,
        platforms=a.platforms.split(",") if a.platforms else None,
        publish_at=a.publish_at, thumbnail=a.thumbnail, pool=a.pool,
        subtitle=a.subtitle, subtitle_lang=a.subtitle_lang,
    )


if __name__ == "__main__":
    main()
