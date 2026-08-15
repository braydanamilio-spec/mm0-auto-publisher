"""
stats.py — Kéo THỐNG KÊ view/like/comment mỗi video + subs mỗi kênh về Firestore.

CỰC NHẸ & AN TOÀN:
  - Chỉ ĐỌC (read-only) qua API chính thức. Không đăng gì -> không thể bị coi spam.
  - channels.list = 1 quota/kênh; videos.list = 1 quota / mỗi 50 video.
    => refresh vài lần/ngày tốn < 10 quota. Không đụng tới trần upload.

Chạy (thường do workflow stats.yml gọi vài lần/ngày):
    python src/stats.py
"""

from __future__ import annotations
import os
import sys
from datetime import datetime, timezone

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from firestore_state import State
from youtube_uploader import _client

CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "channels.yaml")


def _resolve_creds(ch: dict, state=None, key=None) -> dict | None:
    yt = ch.get("youtube", {})
    if not yt.get("enabled"):
        return None
    # Ưu tiên token đã 'Kết nối' qua dashboard (Firestore), sau đó tới GitHub Secrets
    conn = state.get_connection(key, "youtube") if (state and key) else None
    if conn and conn.get("refresh_token"):
        return {"client_id": conn["client_id"], "client_secret": conn["client_secret"],
                "refresh_token": conn["refresh_token"]}
    cid = os.environ.get(yt["client_id_env"])
    csec = os.environ.get(yt["client_secret_env"])
    ref = os.environ.get(yt["refresh_token_env"])
    if not (cid and csec and ref):
        return None
    return {"client_id": cid, "client_secret": csec, "refresh_token": ref}


def refresh_conn(uid: str, channel: str, creds: dict, state: State):
    """Refresh thống kê cho 1 kênh của 1 user (multi-tenant), stamp owner=uid."""
    svc = _client(creds["client_id"], creds["client_secret"], creds["refresh_token"])

    # 1) Thống kê KÊNH (subs, tổng view, số video)
    resp = svc.channels().list(part="statistics,snippet", mine=True).execute()
    items = resp.get("items", [])
    if items:
        s = items[0]["statistics"]
        snip = items[0]["snippet"]
        state.set_channel_stats(channel, {
            "title": snip.get("title"),
            "channel_title": snip.get("title"),
            "subscribers": int(s.get("subscriberCount", 0)),
            "total_views": int(s.get("viewCount", 0)),
            "video_count": int(s.get("videoCount", 0)),
            "yt_ok": True,
            "yt_checked_at": datetime.now(timezone.utc).isoformat(),
        }, owner=uid)
        print(f"  ✔ {uid[:8]}/{channel}: {s.get('subscriberCount','?')} subs, {s.get('viewCount','?')} views")

    # 2) Thống kê từng VIDEO đã đăng (batch 50) — chỉ video của user này
    pairs = state.posted_youtube(channel, owner=uid)
    by_yid = {yid: doc for doc, yid in pairs}
    ids = list(by_yid.keys())
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        vresp = svc.videos().list(part="statistics", id=",".join(chunk)).execute()
        for v in vresp.get("items", []):
            st = v.get("statistics", {})
            doc = by_yid.get(v["id"])
            if doc:
                state.set_video_stats(doc, {
                    "views": int(st.get("viewCount", 0)),
                    "likes": int(st.get("likeCount", 0)),
                    "comments": int(st.get("commentCount", 0)),
                })
    if ids:
        print(f"     cập nhật {len(ids)} video.")


def main():
    state = State()
    print("📈 Refresh thống kê (per-user)...")
    for c in state.list_connections("youtube"):
        uid, channel = c.get("owner"), c.get("channel")
        if not (uid and channel and c.get("refresh_token")):
            continue
        try:
            refresh_conn(uid, channel, c, state)
        except Exception as e:
            print(f"  ❌ {channel}: {e}")
    print("✔ Xong.")


if __name__ == "__main__":
    main()
