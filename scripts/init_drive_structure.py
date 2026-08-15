"""
init_drive_structure.py — Tạo sẵn cấu trúc folder cho MỘT kênh trên Google Drive.

Chạy 1 lần cho mỗi kênh (sau khi đã share folder gốc cho service account):
    GOOGLE_APPLICATION_CREDENTIALS=/tmp/sa.json \
    python scripts/init_drive_structure.py --root <DRIVE_FOLDER_ID>

Tạo ra:
    <root>/_QUEUE/long
    <root>/_QUEUE/short
    <root>/_POSTED
    <root>/_FAILED
    <root>/_DRAFT
"""

from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from drive_client import Drive


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="Drive folder id gốc của kênh.")
    args = ap.parse_args()

    d = Drive()
    queue = d.child_folder(args.root, "_QUEUE")
    d.child_folder(queue, "long")
    d.child_folder(queue, "short")
    d.child_folder(args.root, "_POSTED")
    d.child_folder(args.root, "_FAILED")
    d.child_folder(args.root, "_DRAFT")
    print("✅ Đã tạo cấu trúc folder trong root:", args.root)
    print("   _QUEUE/long, _QUEUE/short, _POSTED, _FAILED, _DRAFT")


if __name__ == "__main__":
    main()
