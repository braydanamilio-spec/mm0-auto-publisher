# ⭐ SETUP — Hướng dẫn từ ZERO (làm 1 lần, ~45–60 phút)

Làm tuần tự 8 bước. Mỗi bước có phần **"copy cái gì"** để bạn không lạc.
Bạn sẽ thu thập dần các giá trị bí mật (secrets) rồi dán vào GitHub ở **Bước 7**.

> 📝 Mở 1 file Notes tạm để dán các giá trị vào khi lấy được. Gợi ý các dòng cần điền:
> ```
> GCP_SA_KEY = (nội dung file JSON)
> FIREBASE_PROJECT_ID =
> BROKE_DRIVE_FOLDER_ID =
> BROKE_YT_CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN =
> BROKE_FB_PAGE_ID / PAGE_TOKEN =
> (lặp lại cho INSIDE_YOU, HUH...)
> ```

---

## Bước 1 — Tạo Google Cloud Project + bật API

1. Vào https://console.cloud.google.com → **Create Project** → đặt tên `mm0-publisher`.
2. Menu **APIs & Services → Library**, bật (Enable) 2 API:
   - **YouTube Data API v3**
   - **Google Drive API**

**Copy:** ghi nhớ **Project ID** (dạng `mm0-publisher-xxxxx`).

---

## Bước 2 — OAuth để lấy REFRESH TOKEN cho từng kênh YouTube

YouTube bắt buộc dùng OAuth của chính chủ kênh. Làm 1 lần → token chạy mãi.

1. **APIs & Services → OAuth consent screen**:
   - User type: **External** → Create.
   - Điền tên app, email hỗ trợ (email bạn), lưu.
   - **Scopes:** thêm `.../auth/youtube.upload` và `.../auth/youtube`.
   - **Test users:** thêm **email của TỪNG kênh** bạn sẽ đăng nhập. (App ở chế độ Testing là đủ, refresh token của Desktop app **không hết hạn** khi bạn là test user.)
2. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Desktop app** → Create → **Download JSON** (đặt tên `client_secret.json`).
3. Trên **máy bạn**, cài Python rồi chạy (làm lại cho **mỗi kênh**, mỗi lần đăng nhập đúng tài khoản kênh đó):

   ```bash
   cd MM0-AutoPublisher
   pip install -r requirements.txt
   python src/auth_setup.py --client-secret /duong/dan/client_secret.json
   ```

   Trình duyệt mở ra → đăng nhập tài khoản kênh → **Cho phép**. Terminal in ra:
   ```
   CLIENT_ID     = ...
   CLIENT_SECRET = ...
   REFRESH_TOKEN = ...
   ```

**Copy:** 3 giá trị trên cho **mỗi kênh** → dán vào Notes theo tên `BROKE_YT_...`, `INSIDE_YOU_YT_...`, `HUH_YT_...`.

> ⚠️ Nếu `REFRESH_TOKEN` rỗng: vào https://myaccount.google.com/permissions xoá quyền app cũ rồi chạy lại.

---

## Bước 3 — Firebase (Firestore + Auth + Hosting)

1. Vào https://console.firebase.google.com → **Add project** → **chọn đúng project** đã tạo ở Bước 1 (dùng chung, không tạo mới).
2. **Build → Firestore Database → Create database** → chọn **Production mode** → region gần (asia-southeast1).
3. **Build → Authentication → Get started → Sign-in method → bật Google**.
4. **Project settings (bánh răng) → General → Your apps → Web (</>)** → đăng ký app tên `dashboard` → **copy đoạn `firebaseConfig`** hiện ra.

**Copy:**
- `firebaseConfig` (apiKey, authDomain, projectId, appId) → để dán vào `dashboard/index.html` ở Bước 8.
- **FIREBASE_PROJECT_ID** = chính Project ID.

---

## Bước 4 — Service Account (dùng cho Drive + Firestore)

1. **Google Cloud Console → IAM & Admin → Service Accounts → Create service account**:
   - Tên: `mm0-bot`. Create.
   - **Grant roles:** thêm **`Cloud Datastore User`** (để ghi Firestore). Done.
2. Mở service account vừa tạo → tab **Keys → Add key → Create new key → JSON** → tải file về.
3. Mở email service account (dạng `mm0-bot@...iam.gserviceaccount.com`) — **copy email này**.

**Copy:**
- **GCP_SA_KEY** = toàn bộ **nội dung** file JSON vừa tải (mở bằng text editor, copy hết).
- Email service account (dùng ở Bước 5).

---

## Bước 5 — Google Drive: tạo & chia sẻ folder cho mỗi kênh

1. Trên Google Drive tạo cây folder:
   ```
   MM0-PUBLISH/
     BROKE/
     INSIDE-YOU/
     HUH/
   ```
2. Với **mỗi folder kênh** (VD `BROKE`): chuột phải → **Share** → dán **email service account** (Bước 4) → quyền **Editor** → Send.
3. Mở folder kênh, nhìn URL: `https://drive.google.com/drive/folders/XXXXXXXX` → phần `XXXXXXXX` là **folder id**.

**Copy:** `BROKE_DRIVE_FOLDER_ID`, `INSIDE_YOU_DRIVE_FOLDER_ID`, `HUH_DRIVE_FOLDER_ID`.

4. Tạo cấu trúc con tự động (`_QUEUE/long`, `_QUEUE/short`, `_POSTED`, `_FAILED`, `_DRAFT`) cho mỗi kênh:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=/duong/dan/sa.json
   python scripts/init_drive_structure.py --root <BROKE_DRIVE_FOLDER_ID>
   python scripts/init_drive_structure.py --root <INSIDE_YOU_DRIVE_FOLDER_ID>
   python scripts/init_drive_structure.py --root <HUH_DRIVE_FOLDER_ID>
   ```

---

## Bước 6 — Facebook Page token (nếu đăng FB)

1. Vào https://developers.facebook.com → **Create App** → loại **Business**.
2. Thêm sản phẩm **Facebook Login** + xin quyền: `pages_manage_posts`, `pages_read_engagement`, `pages_show_list`.
3. Dùng **Graph API Explorer** (developers.facebook.com/tools/explorer):
   - Chọn app → **Get User Access Token** → tick các quyền trên.
   - Gọi `GET /me/accounts` → lấy **Page ID** + **access_token của Page**.
4. **Đổi sang token dài hạn** (long-lived) để khỏi hết hạn:
   ```
   GET /oauth/access_token?grant_type=fb_exchange_token
       &client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN
   ```
   rồi gọi lại `/me/accounts` bằng token dài hạn để lấy **Page token dài hạn**.

**Copy:** `BROKE_FB_PAGE_ID`, `BROKE_FB_PAGE_TOKEN` (và cho các kênh khác).

> Chưa muốn dùng Facebook? Trong `config/channels.yaml` đặt `facebook: enabled: false` cho kênh đó, và trong sidecar để `"platforms": ["youtube"]`.

---

## Bước 7 — Đẩy code lên GitHub + nạp Secrets

1. Tạo repo mới trên GitHub. **Nên để PUBLIC** (Actions không giới hạn phút; secrets vẫn được mã hoá an toàn, code không chứa bí mật).
2. Đẩy thư mục này lên:
   ```bash
   cd MM0-AutoPublisher
   git init && git add . && git commit -m "MM0 auto-publisher"
   git branch -M main
   git remote add origin https://github.com/<bạn>/<repo>.git
   git push -u origin main
   ```
3. Trên GitHub: **Settings → Secrets and variables → Actions → New repository secret**. Thêm **tất cả** giá trị trong Notes:
   - `GCP_SA_KEY` (dán nguyên nội dung JSON), `FIREBASE_PROJECT_ID`
   - Mỗi kênh: `<KÊNH>_DRIVE_FOLDER_ID`, `<KÊNH>_YT_CLIENT_ID`, `<KÊNH>_YT_CLIENT_SECRET`, `<KÊNH>_YT_REFRESH_TOKEN`, `<KÊNH>_FB_PAGE_ID`, `<KÊNH>_FB_PAGE_TOKEN`.
   - (Tùy chọn) Tab **Variables** → thêm `POSTING_TEMPLATE` = `sprint_7d` | `growth_30d` | `scale_90d`.
4. Tab **Actions** → bật workflows nếu được hỏi.

> Tên secret phải **khớp** với tên trong `.github/workflows/publish.yml`. Thêm kênh mới → thêm block env tương ứng trong file đó.

---

## Bước 8 — Deploy dashboard + chạy thử

1. Sửa `dashboard/index.html`: thay `firebaseConfig` bằng config ở Bước 3.
2. Sửa `dashboard/firestore.rules`: đổi email thành email Google bạn đăng nhập dashboard.
3. Deploy:
   ```bash
   npm install -g firebase-tools
   firebase login
   cd dashboard
   firebase use <FIREBASE_PROJECT_ID>
   firebase deploy --only hosting,firestore:rules
   ```
   → nhận URL dạng `https://<project>.web.app` — đây là dashboard của bạn.
4. **Chạy thử toàn hệ thống:**
   - Bỏ 1 video test vào `MM0-PUBLISH/BROKE/_QUEUE/short/` (đặt tên gợi nhớ, VD `test-hook_short.mp4`).
   - Trên GitHub → **Actions → MM0 Auto-Publisher → Run workflow** → tick **dry-run = true** → xem log (không upload thật, chỉ kiểm tra nối kết).
   - Nếu log OK → chạy lại **dry-run = false** để đăng thật.
5. Xong! Từ giờ cron tự chạy **mỗi 30 phút**. Bạn chỉ cần **bỏ video vào `_QUEUE`**, hệ thống lo phần còn lại.

---

## Vận hành hằng ngày

| Muốn gì | Làm gì |
|---|---|
| Đăng thêm video | Bỏ file vào `_QUEUE/long` hoặc `_QUEUE/short`. Hệ thống tự lên lịch. |
| Đặt tiêu đề/mô tả riêng | Thêm file `.json` cùng tên (xem `config/sidecar.example.json`). |
| Chọn giờ đăng cụ thể | Đặt `"publish_at"` trong sidecar (ISO, VD `2026-08-16T20:30:00+07:00`). |
| Đổi nhịp đăng | Đổi Variable `POSTING_TEMPLATE`, hoặc thêm `active_template` cho kênh trong `channels.yaml`. |
| Xem đã/chưa đăng | Mở dashboard, hoặc nhìn folder `_POSTED` vs `_QUEUE`. |
| Video lỗi | Nằm trong `_FAILED` + dashboard hiện trạng thái "Lỗi" kèm lý do. |

## Tối ưu "top 1" & lách giới hạn free

- **Vượt trần 6 upload/ngày:** tách mỗi kênh 1 Google Cloud project riêng → mỗi project 10.000 quota. Hoặc điền form xin tăng quota YouTube (miễn phí, cần mô tả app).
- **Đăng đúng giờ vàng:** template đã canh giờ US tối (giờ VN sáng/trưa/tối) — chỉnh trong `posting_templates.yaml`.
- **Không bao giờ trùng/sót:** Firestore là "sổ cái" — mỗi video 1 trạng thái; đã `posted` thì không đăng lại; file tự rời `_QUEUE`.
- **An toàn chính sách:** `metadata.lint()` cảnh báo cụm từ rủi ro; tự chèn disclaimer; giữ giãn cách ≥30 phút chống spam.
- **Lên lịch native YouTube:** đặt `use_native_schedule: true` trong block `youtube` của kênh → video upload dạng private và YouTube tự công khai đúng `publish_at` (mượt hơn nữa).
