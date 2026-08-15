# 📊 TIẾN ĐỘ — MM0 Auto-Publisher

> File này là **bảng theo dõi tiến độ**. Mỗi phiên làm việc cập nhật lại phần "Trạng thái" và "Việc tiếp theo".

**Cập nhật lần cuối:** 2026-08-15
**Người/máy:** Claude Code (session build ban đầu)

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

## 🔲 CẦN NGƯỜI DÙNG LÀM (cần đăng nhập / thao tác thủ công — không tự động được)

- [ ] **Bước 1–2 (SETUP.md):** Bật YouTube Data API + Drive API; tạo OAuth consent + Desktop client; chạy `auth_setup.py` cho **mỗi kênh** → lấy refresh token.
- [ ] **Bước 3:** Bật Firestore (Native mode) + Authentication (Google) + Hosting trong Firebase Console.
- [ ] **Bước 4:** Tạo service account + key JSON; gán role `Cloud Datastore User`.
- [ ] **Bước 5:** Tạo folder Drive mỗi kênh, share cho email service account (Editor); chạy `init_drive_structure.py`.
- [ ] **Bước 6:** (Nếu đăng FB) Tạo FB app + lấy Page long-lived token.
- [ ] **Bước 7:** Nạp toàn bộ Secrets vào GitHub repo.
- [ ] **Bước 8:** Dán `firebaseConfig` vào `dashboard/index.html`; sửa email trong `firestore.rules`; `firebase deploy`.
- [ ] Chạy thử `Run workflow` (dry-run) → rồi thật.

## 💡 NÂNG CẤP TƯƠNG LAI (ý tưởng, chưa làm)

- [ ] Thêm kênh mới (ch7 DATA RACE, ch8 SAY THIS) vào `channels.yaml` + block env trong workflow.
- [ ] Tách mỗi kênh 1 Google Cloud project để vượt trần 6 upload/ngày.
- [ ] Native scheduling YouTube (đặt `use_native_schedule: true`).
- [ ] Retry thông minh cho lỗi quota (đợi sang ngày thay vì đánh failed).
- [ ] Nút "đăng ngay" / "hoãn" trên dashboard (cần ghi Firestore từ client + Cloud Function nhỏ).
- [ ] Thống kê hiệu suất (views/CTR) kéo về dashboard qua YouTube Analytics API.

---

## Ghi chú trạng thái gần nhất
(Phiên sau ghi tiếp vào đây — mới nhất lên trên)

- **2026-08-15**: Dựng xong toàn bộ code + docs; tạo repo + Firebase project; đẩy code lên. Chờ người dùng chạy SETUP.md để đưa vào vận hành.
