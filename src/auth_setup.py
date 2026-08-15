"""
auth_setup.py — CHẠY 1 LẦN TRÊN MÁY BẠN để lấy REFRESH TOKEN cho mỗi kênh YouTube.

Cách dùng:
    python src/auth_setup.py --client-secret path/to/client_secret.json

Nó mở trình duyệt, bạn đăng nhập ĐÚNG tài khoản sở hữu kênh, bấm Cho phép.
Kết thúc, nó in ra:
    CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN
=> Bạn copy 3 giá trị này vào GitHub Secrets của kênh (xem SETUP.md).

Làm lại cho từng kênh (đăng nhập tài khoản tương ứng mỗi lần).
"""

from __future__ import annotations
import argparse
import json

from google_auth_oauthlib.flow import InstalledAppFlow

YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
             "https://www.googleapis.com/auth/youtube",
             "https://www.googleapis.com/auth/youtube.force-ssl"]  # captions/phụ đề
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-secret", required=True,
                    help="File client_secret_*.json tải từ Google Cloud (OAuth Desktop app).")
    ap.add_argument("--drive", action="store_true",
                    help="Lấy token DRIVE (cho tài khoản kho lưu trữ) thay vì YouTube.")
    args = ap.parse_args()

    scopes = DRIVE_SCOPES if args.drive else YT_SCOPES
    flow = InstalledAppFlow.from_client_secrets_file(args.client_secret, scopes)
    # run_local_server tự mở trình duyệt + nhận callback
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    with open(args.client_secret) as f:
        conf = json.load(f)
    key = "installed" if "installed" in conf else "web"
    client_id = conf[key]["client_id"]
    client_secret = conf[key]["client_secret"]

    print("\n" + "=" * 60)
    print("  ✅ LẤY TOKEN THÀNH CÔNG — COPY 3 DÒNG DƯỚI VÀO GITHUB SECRETS")
    print("=" * 60)
    print(f"CLIENT_ID     = {client_id}")
    print(f"CLIENT_SECRET = {client_secret}")
    print(f"REFRESH_TOKEN = {creds.refresh_token}")
    print("=" * 60)
    if not creds.refresh_token:
        print("⚠️  Không có refresh_token! Xóa quyền cũ tại "
              "https://myaccount.google.com/permissions rồi chạy lại.")


if __name__ == "__main__":
    main()
