"""
export_links.py — Xuất SỔ LINK video đã đăng ra CSV (để quản lý & backup link).

Kể cả khi file nguồn đã bị dọn dẹp, link YouTube/Facebook vẫn còn trong Firestore
-> đây là "sổ cái" để anh tra cứu, tải lại (từ backup) hoặc đăng lại khi cần.

Chạy:
  python src/export_links.py                 # in ra màn hình + lưu posted_links.csv
  python src/export_links.py --out /path.csv
"""

from __future__ import annotations
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from firestore_state import State

FIELDS = ["channel", "type", "title", "posted_at", "publish_at",
          "youtube_url", "youtube_id", "facebook_url", "source_status", "drive_name"]


def rows():
    st = State()
    out = []
    for v in st.list_videos():
        if v.get("status") != "posted":
            continue
        yt = (v.get("results") or {}).get("youtube") or {}
        fb = (v.get("results") or {}).get("facebook") or {}
        out.append({
            "channel": v.get("channel", ""),
            "type": v.get("type", ""),
            "title": v.get("title", ""),
            "posted_at": v.get("posted_at", ""),
            "publish_at": v.get("publish_at", ""),
            "youtube_url": yt.get("url", ""),
            "youtube_id": yt.get("id", ""),
            "facebook_url": fb.get("url", ""),
            "source_status": v.get("source_status", "live"),  # live | deleted | archived
            "drive_name": v.get("drive_name", ""),
        })
    out.sort(key=lambda r: r.get("posted_at", ""), reverse=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="posted_links.csv")
    a = ap.parse_args()

    data = rows()
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(data)

    print(f"✔ {len(data)} link đã đăng → {a.out}")
    for r in data[:20]:
        print(f"  [{r['channel']}] {r['title'][:50]:50} {r['youtube_url']}  ({r['source_status']})")
    if len(data) > 20:
        print(f"  … và {len(data)-20} video nữa trong file CSV.")


if __name__ == "__main__":
    main()
