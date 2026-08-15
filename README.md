# 📺 MM0 Auto-Publisher

Hệ thống **tự động đăng video từ Google Drive lên YouTube + Facebook**, chạy trên **GitHub Actions** — **không cần bật máy**, đúng giờ, đúng số lượng, multi-channel, có **dashboard Firebase** quản lý realtime. 100% chạy trên hạ tầng **miễn phí**.

## Nó hoạt động thế nào (1 hình)

```
    BẠN LÀM VIDEO                    CLOUD TỰ ĐỘNG (24/7)                 BẠN THEO DÕI
 ┌──────────────────┐   share    ┌──────────────────────────┐        ┌──────────────┐
 │  Google Drive     │──────────▶ │  GitHub Actions (cron)    │        │  Dashboard    │
 │  MM0-PUBLISH/     │            │   mỗi 30 phút:            │        │  Firebase     │
 │   BROKE/          │            │   1. quét _QUEUE          │───────▶│  Hosting      │
 │    _QUEUE/long    │            │   2. video đến giờ?       │  state │  (realtime)   │
 │    _QUEUE/short   │            │   3. upload YT + FB       │        └──────────────┘
 │    _POSTED/       │◀───────────│   4. chuyển sang _POSTED  │
 │    _FAILED/       │   move     │   5. ghi Firestore        │
 └──────────────────┘            └──────────────────────────┘
```

- **Cấp quyền YouTube 1 lần** (refresh token) → chạy mãi mãi, không đăng nhập lại.
- **Bỏ video vào `_QUEUE`** → hệ thống tự đặt tiêu đề/mô tả/hashtag chuẩn, tự lên lịch theo template, tự đăng, tự dọn vào `_POSTED`.
- **Phân loại rõ ràng:** `_QUEUE` (chưa đăng) · `_POSTED` (đã đăng) · `_FAILED` (lỗi cần xem) · `_DRAFT` (đang làm).
- **Template lịch:** gói **7 / 30 / 90 ngày** phát triển kênh (xem `config/posting_templates.yaml`).

## Cấu trúc dự án

```
MM0-AutoPublisher/
├── config/
│   ├── channels.yaml            # khai báo các kênh (multi-channel)
│   ├── posting_templates.yaml   # lịch đăng 7/30/90 ngày + trần an toàn
│   └── sidecar.example.json     # metadata tùy chọn cho từng video
├── src/
│   ├── main.py                  # bộ điều phối (Actions gọi file này)
│   ├── drive_client.py          # Google Drive (service account)
│   ├── youtube_uploader.py      # upload YouTube (OAuth refresh token)
│   ├── facebook_uploader.py     # đăng Facebook Page / Reels
│   ├── firestore_state.py       # trạng thái + đồng bộ dashboard
│   ├── scheduler.py             # rải publish_at theo template
│   ├── metadata.py              # dựng title/desc/hashtag + lint chính sách
│   └── auth_setup.py            # CHẠY 1 LẦN lấy refresh token
├── scripts/
│   └── init_drive_structure.py  # tạo folder _QUEUE/_POSTED... trên Drive
├── dashboard/                   # web quản lý (Firebase Hosting)
├── .github/workflows/publish.yml# cron chạy trên cloud
└── SETUP.md                     # ⭐ HƯỚNG DẪN TỪ ZERO — đọc file này trước
```

## Bắt đầu

👉 Mở **[SETUP.md](SETUP.md)** và làm theo 8 bước. Khoảng 45–60 phút cho lần đầu.

## Giới hạn cần biết (quan trọng)

| Hạ tầng | Miễn phí | Trần thực tế |
|---|---|---|
| YouTube Data API | ✅ 10.000 quota/ngày | **~6 upload/ngày/kênh** (mỗi upload ~1.600). Muốn nhiều hơn → xin tăng quota hoặc thêm kênh. |
| GitHub Actions | ✅ (repo public: không giới hạn phút) | job tối đa 6h — thừa sức upload video dài. |
| Firestore | ✅ 50k đọc/20k ghi mỗi ngày | quá đủ cho dashboard. |
| Firebase Hosting | ✅ 10GB băng thông/tháng | quá đủ. |
| Facebook Graph API | ✅ | tuân thủ chính sách Page. |

> Hệ thống tự động **giữ trần** và **giãn cách** giữa các lần đăng để không bị gắn cờ spam.
