# Sổ tay kết nối MM0

Hướng dẫn đúng-đủ-bước cho **kho Drive** và **kênh YouTube**. Cập nhật 23/8/2026 — sau khi hệ đổi quyền Drive sang `drive.file` (token vĩnh viễn, không giới hạn số kho).

---

## PHẦN 1 · KẾT NỐI KHO DRIVE (mỗi mail 15GB free)

**Chuẩn bị:** mail Google muốn làm kho (mới hay cũ đều được). Không cần cài gì, không cần tạo project, không cần key.

1. Mở dashboard 👉 https://mm0-auto-publisher.web.app → tab **💾 Hồ chứa**.
2. Điền **tên kho** (vd `store21` — chỉ chữ thường + số, đặt theo thứ tự cho dễ nhớ) → bấm **🔗 Connect Google Drive**.
3. Màn hình Google hiện ra → **chọn đúng mail của kho** (nếu chưa có trong danh sách → *Use another account* → đăng nhập mail đó).
4. Nếu hiện cảnh báo *"Google hasn't verified this app"* → bấm **Advanced** (góc trái dưới) → **Go to mm0 (unsafe)** → tiếp tục. (Cảnh báo chuẩn cho app cá nhân — an toàn vì app là của chính mình.)
5. Màn hình xin quyền → **Continue/Allow**. Từ 23/8 quyền xin là `drive.file` (chỉ đụng file do app tạo) → **token vĩnh viễn, không bao giờ phải làm lại**.
6. Quay về dashboard thấy kho mới trong danh sách Hồ chứa (+15GB) là xong.

**Kết nối lại kho cũ (token đời 7-ngày đã chết):** banner đỏ ở Hồ chứa réo tên kho nào thì làm đúng 6 bước trên cho mail kho đó — 1 lần cuối cùng trong đời kho.

**Lưu ý:**
- Không giới hạn số kho (100 suất user-cap KHÔNG áp cho quyền drive.file). Thêm 300–500 kho thoải mái.
- Vượt ~150 kho thì báo Claude nén danh sách kho thành snapshot 1-doc (chống tốn quota đọc).

---

## PHẦN 2 · KẾT NỐI KÊNH YOUTUBE (khi bắt đầu đăng bài)

**Chuẩn bị:** kênh YouTube đã tạo bằng mail nào thì cầm sẵn mail đó.

1. Dashboard → tab **📺 Kênh** (Channels Hub) → bấm **Kết nối** ở kênh tương ứng (hoặc thêm kênh mới rồi Kết nối).
2. Chọn đúng **mail sở hữu kênh** → nếu mail có nhiều kênh, chọn đúng **kênh** (YouTube hỏi riêng).
3. Cảnh báo unverified → **Advanced → Continue** (như Drive).
4. Màn hình quyền sẽ xin: đăng video, quản lý kênh, phụ đề/thumbnail, đọc thống kê + **doanh thu** → **Allow tất cả** (đã gộp xin 1 lần, sau này không phải xin lại).
5. Xong — kênh hiện trạng thái Đã kết nối, video trong _QUEUE của kênh sẽ được cron tự đăng.

**Mỗi kênh YouTube tốn 1 suất user-cap (còn ~16/100).** Gần chạm 95 → báo Claude tách thêm OAuth client project mới (worker hỗ trợ sẵn `YT_CLIENTS`).

---

## PHẦN 3 · 2 VIỆC MỘT-LẦN cho YouTube API (làm khi bắt đầu đăng thật)

### 3a. Form audit YouTube API (BẮT BUỘC — không làm thì video đăng bằng API bị YouTube tự khoá Private)
1. Vào 👉 https://support.google.com/youtube/contact/yt_api_form (đăng nhập braydanamilio@gmail.com).
2. Khai: project `mm0-auto-publisher`; mục đích *"tự động đăng video do chính chúng tôi sản xuất lên các kênh YouTube thuộc sở hữu của chúng tôi"*; không thu thập dữ liệu người dùng khác; kèm link dashboard làm demo.
3. Trong form tick luôn mục **xin nới quota** (mặc định 10.000 units/ngày ≈ 6 video — xin 50.000–100.000).
4. Chờ mail duyệt ~1–2 tuần. Trong lúc chờ vẫn đăng được (video có thể bị private — chỉ đăng thử nghiệm).

### 3b. Nhân project khi cần đăng nhiều (mỗi project = 10K units/ngày riêng)
Mỗi project mới (mail nào cũng được, ~10 phút):
1. https://console.cloud.google.com → New Project (vd `mm0-yt-2`).
2. **APIs & Services → Library** → bật **YouTube Data API v3** (+ YouTube Analytics API).
3. **Google Auth Platform → Audience**: User type External → tạo xong bấm **PUBLISH APP** ngay (⚠️ bài học xương máu: publish TRƯỚC khi cấp bất kỳ token nào).
4. **Clients → Create Client** → loại **Web application** → Authorized redirect URI: `https://mm0-connect.adisondurham-ef1.workers.dev/oauth/callback` (xem lại URI chính xác trong worker nếu đổi domain).
5. Copy Client ID + Secret → đưa Claude cắm vào secret `YT_CLIENTS` của worker (KHÔNG dán secret vào chat công khai — dùng lệnh `wrangler secret put` Claude hướng dẫn).
6. Điền form audit (3a) cho project mới.

---

## PHẦN 4 · CHECKLIST NHANH KHI GẶP LỖI

| Triệu chứng | Nguyên nhân | Xử |
|---|---|---|
| Kho báo `invalid_grant` | Token đời cũ (trước 23/8) hết hạn 7 ngày | Kết nối lại 1 lần (Phần 1) → vĩnh viễn |
| "App not verified" chặn cứng không có nút Advanced | Đăng nhập sai mail / app đổi trạng thái | Kiểm mail; xem Audience của project |
| Video đăng bị Private | Project chưa qua audit YouTube | Điền form 3a |
| Đăng lỗi `quotaExceeded` | Hết 10K units/ngày của project | Chờ reset (14h VN) hoặc nhân project (3b) |
| Kết nối báo user cap | Hết 100 suất (chỉ YouTube ăn suất) | Tách project client mới (3b) |
