# 📊 TIẾN ĐỘ — MM0 Auto-Publisher

> File này là **bảng theo dõi tiến độ**. Mỗi phiên làm việc cập nhật lại phần "Trạng thái" và "Việc tiếp theo".

**Cập nhật lần cuối:** 2026-08-15 (phiên 8)
**Người/máy:** Claude Code — multi-tenant + branding/comment + đăng bài + quản lý kênh

---

## ✅ ĐÃ XONG (code hoàn chỉnh, đã test cú pháp)

- [x] Kiến trúc tổng thể (Drive → GitHub Actions → YouTube + Facebook, state Firestore, dashboard).
- [x] `src/` đầy đủ: main, drive_client, youtube_uploader, facebook_uploader, firestore_state, scheduler, metadata, auth_setup, enqueue.
- [x] Hỗ trợ **thumbnail** (upload + gắn vào YouTube + di chuyển kèm khi xong).
- [x] Cầu nối tự động: `enqueue.py` + `scripts/watch_and_enqueue.py` (thả file vào OUTBOX là tự đẩy).
- [x] Config: `channels.yaml` (BROKE / INSIDE_YOU / HUH), `posting_templates.yaml` (7/30/90 ngày).
- [x] GitHub Actions cron (`.github/workflows/publish.yml`).
- [x] Dashboard đẹp (`dashboard/index.html`) + `firebase.json` + `firestore.rules`.
- [x] Tài liệu: `README.md`, `SETUP.md`, `docs/PRODUCTION-SPEC.md`.
- [x] Repo GitHub được tạo + push (xem HANDOFF.md).
- [x] Firebase project được tạo (xem HANDOFF.md).

## ✅ ĐÃ XONG THÊM (phiên 2)

- [x] Xử lý quota YouTube thông minh (hết quota → giữ pending, thử lại ngày mai, không đánh failed).
- [x] Module thống kê `stats.py` + workflow `stats.yml` (view/like/comment + subs, read-only, nhẹ).
- [x] **Dashboard v2** chuẩn SaaS: sửa bug đè layout, biểu đồ (bar/donut canvas), KPI subs/views, trang Phân tích, bảng xếp hạng video, dark/light chuẩn 3 trạng thái. **Đã deploy LIVE:** https://mm0-auto-publisher.web.app
- [x] Firestore DB (asia-southeast1) đã tạo; APIs (Firestore/Drive/YouTube/IdentityToolkit) đã bật; rules + hosting đã deploy.
- [x] Test tự động `tests/test_core.py` + CI `ci.yml`. `.env.example`.

## ✅ ĐÃ XONG THÊM (phiên 5 — kho lưu trữ)

- [x] Pool nhiều tài khoản Drive free (OAuth mỗi acc → dùng đủ 15GB + xoá được): `src/storage.py`, `config/storage.yaml`.
- [x] `enqueue --pool` tự chọn acc còn trống; `main.process_pool` quét pool, định tuyến kênh theo sidecar.
- [x] Dọn dẹp 3 chế độ (keep/delete/archive) auto|manual: `src/cleanup.py` + `.github/workflows/cleanup.yml`.
- [x] Backup kho lạnh (archive → tài khoản Google One); sổ link đã đăng: `src/export_links.py` (CSV).
- [x] `auth_setup.py --drive` lấy token Drive cho acc kho. Tài liệu: `docs/STORAGE.md`.

## 🔲 CẦN NGƯỜI DÙNG LÀM (cần đăng nhập / thao tác thủ công — không tự động được)

- [ ] **BẬT 2SV cho adisondurham@gmail.com** (myaccount.google.com) → mở khoá Firebase Console → bật Auth provider → dashboard login được. (Blocker hiện tại; chỉ user làm được.)

- [ ] **BẬT Google Sign-in** (Firebase Console → Authentication → Sign-in method → Google → Enable). *(bước duy nhất còn kẹt vì cần client_id auto-tạo trên console)*
- [ ] **Bước 1–2 (SETUP.md):** Tạo OAuth consent + Desktop client; chạy `auth_setup.py` cho **mỗi kênh** → lấy refresh token.
- [ ] **Bước 4:** Tạo service account + key JSON; gán role `Cloud Datastore User`; tải JSON.
- [ ] **Bước 5:** Tạo folder Drive mỗi kênh, share cho email service account (Editor); chạy `init_drive_structure.py`.
- [ ] **Bước 6:** (Nếu đăng FB) Tạo FB app + lấy Page long-lived token.
- [ ] **Bước 7:** Nạp toàn bộ Secrets vào GitHub repo.
- [ ] Chạy thử `Run workflow` (dry-run) → rồi thật.

## 💡 NÂNG CẤP TƯƠNG LAI (ý tưởng, chưa làm)

- [ ] Thêm kênh mới (ch7 DATA RACE, ch8 SAY THIS) vào `channels.yaml` + block env trong workflow.
- [ ] Tách mỗi kênh 1 Google Cloud project để vượt trần 6 upload/ngày.
- [ ] Native scheduling YouTube (đặt `use_native_schedule: true`).
- [ ] Retry thông minh cho lỗi quota (đợi sang ngày thay vì đánh failed).
- [x] ~~Nút "đăng ngay" / "hoãn" trên dashboard~~ → ĐÃ LÀM (trang Đăng bài: Đăng ngay + Rải lịch hàng loạt; client ghi status/publish_at qua rules, không cần Cloud Function).
- [ ] Thống kê hiệu suất (views/CTR) kéo về dashboard qua YouTube Analytics API.

---

## Ghi chú trạng thái gần nhất
(Phiên sau ghi tiếp vào đây — mới nhất lên trên)

- **2026-08-15 (phiên 8 — multi-tenant + quản lý kênh + đăng bài)**: Đã build & deploy LIVE:
  - **Multi-tenant** (mỗi user dữ liệu riêng): mọi doc có field `owner`=uid; doc id `{uid}__{channel}`; Worker xác thực Firebase ID token (JWKS RS256) → uid; rules owner-based. `main.process_users`/stats/cleanup chạy per-user.
  - **(3) Branding + (4) Comment/Like** qua Worker API JSON có CORS (kèm ID token): `GET/POST /api/branding`, `GET /api/comments`, `POST /api/comment-action`, `POST /api/disconnect`. Dashboard 2 tab Branding/Comments. Avatar/banner KHÔNG đổi được qua API (chỉ Studio).
  - **Chống trùng upload** `src/dedup.py` (vân tay nội dung sha1 size+2MB đầu+cuối) tại `enqueue()` → tra `State.sig_exists` trong Firestore; kéo lại cả folder chỉ đăng video mới. File trùng → `OUTBOX/_dup`.
  - **Auto-name**: kết nối để trống ô tên → Worker tự lấy tên kênh thật làm nhãn (`slugLabel`). Trang Kết nối **gom nhóm theo Gmail** (field `email` trong channels doc; mỗi mail hiện số kênh + tổng subs).
  - **Gỡ kênh/Gmail/kho**: `/api/disconnect` (revoke token + xoá doc), nút 🗑 trên card/nhóm/kho.
  - **Trang "Kênh của tôi"** (v-channels): thẻ hub mỗi kênh, hiện cả khi chưa có video (`allChannels()` gộp connections vào chFilter/Tiến độ/Settings), chọn gói template per-kênh, nút nhảy Branding/Comments/Video.
  - **Trang "Đăng bài"** (v-publish): Đăng ngay (status=pending+publish_at=now → cron ~30' đăng), Rải lịch hàng loạt (video/ngày + khung giờ + ngày bắt đầu), Đăng tất cả ngay. Ghi video docs từ client qua `window.__updateVideo/__bulkUpdateVideos` (rules cho sửa status/publish_at/reviewed_at).
  - **metadata.py**: tự cắt title 100 / desc 5000 / tags 480 ở **ranh giới từ** + bỏ ký tự `< >` (YouTube từ chối) → không lỗi/không cảnh báo.
  - Tích hợp **cả Drive free (15GB) lẫn Google One**: Worker đọc `storageQuota` thật khi connect → `cap_gb`/`used` đúng.

- **2026-08-15 (phiên 6+)**: Đã thêm & deploy: **Cloudflare Worker** kết nối OAuth (nút Kết nối YouTube/Drive trên dashboard → token lưu Firestore); **chế độ Duyệt** trước khi đăng; **giao diện EN/VI** (mặc định EN); **playlist** tự tạo; **chọn template per-kênh** trên dashboard; **dọn dẹp linh động** chỉnh trên dashboard (mode/keep_days → Firestore, cleanup đọc); **thống kê Long/Short** theo kênh; logo/favicon SVG. Secret pipeline GitHub đã nạp. CÒN LẠI: branding kênh qua API + quản lý comment/like (đang xếp hàng). Chi tiết Worker: connect-worker/README.md.

- **2026-08-15**: Dựng xong toàn bộ code + docs; tạo repo + Firebase project; đẩy code lên. Chờ người dùng chạy SETUP.md để đưa vào vận hành.
