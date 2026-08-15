# 🔗 MM0 Connect Worker (Cloudflare — FREE)

Cho phép bấm **"Kết nối kênh"** ngay trên dashboard: đăng nhập Google của kênh → cấp quyền 1 lần → token tự lưu vào Firestore. Pipeline đọc token từ đó → đăng bình thường. **Không cần sửa GitHub Secrets nữa.**

100% miễn phí, **không cần thẻ tín dụng**.

---

## Chuẩn bị (1 lần)

### 1. Tài khoản Cloudflare + Wrangler
```bash
npm install -g wrangler
wrangler login          # đăng nhập Cloudflare (miễn phí)
```

### 2. OAuth client loại "Web application"
Vì Worker nhận OAuth qua web (khác Desktop app):
- Google Cloud Console → **APIs & Services → Credentials → Create OAuth client ID → Web application**.
- **Authorized redirect URIs:** thêm `https://mm0-connect.<subdomain>.workers.dev/auth/callback`
  (biết URL chính xác sau bước deploy đầu tiên — deploy trước, copy URL, rồi quay lại thêm).
- Lưu **Client ID** + **Client Secret**.
- ⭐ Nhớ **PUBLISH APP** (OAuth consent screen → In production) để refresh token **không hết hạn**.

### 3. Deploy Worker
```bash
cd connect-worker
wrangler deploy                       # lần đầu -> in ra URL: https://mm0-connect.<subdomain>.workers.dev
```
Copy URL đó → quay lại bước 2 thêm vào **Authorized redirect URIs**.

### 4. Nạp secrets cho Worker
```bash
wrangler secret put YT_CLIENT_ID        # Client ID (Web app)
wrangler secret put YT_CLIENT_SECRET
wrangler secret put FIREBASE_PROJECT_ID # mm0-auto-publisher
wrangler secret put SA_CLIENT_EMAIL     # email service account (…@…iam.gserviceaccount.com)
wrangler secret put SA_PRIVATE_KEY      # dán nguyên "private_key" từ file JSON service account
                                        #   (cả dòng -----BEGIN PRIVATE KEY----- ... -----END...-----)
wrangler secret put ALLOW_EMAIL         # (tuỳ chọn) chỉ email này mới được kết nối
```
> Service account phải có quyền ghi Firestore (role **Cloud Datastore User**) — chính là SA bạn đã tạo ở SETUP.md Bước 4.

### 5. Dán URL Worker vào dashboard
Sửa `dashboard/index.html`:
```js
const WORKER_URL = "https://mm0-connect.<subdomain>.workers.dev";
```
rồi `firebase deploy --only hosting`.

---

## Dùng hằng ngày

1. Mở dashboard → tab **Kết nối API**.
2. Gõ tên kênh (vd `BROKE`) → **🔗 Kết nối YouTube** → đăng nhập tài khoản kênh đó → Cho phép.
3. Xong! Kênh hiện trạng thái **Đã kết nối**, pipeline dùng token ngay.
4. Thêm kênh mới: chỉ cần khai báo kênh trong `config/channels.yaml` (branding) rồi bấm Kết nối — **không đụng Secrets**.

## Bảo mật

- Token nằm ở Firestore collection `connections/` — **rules chặn client đọc/ghi** hoàn toàn.
- Chỉ **service account** (pipeline) và **Worker** ghi/đọc (qua Admin/REST, bỏ qua rules).
- Dashboard **không bao giờ** thấy token (chỉ thấy trạng thái `yt_ok`).
- Đặt `ALLOW_EMAIL` để chỉ mình bạn kết nối được.

## Kiến trúc

```
Dashboard "Kết nối" ──▶ Worker /auth/start ──▶ Google Cho phép
                                                     │ code
Firestore connections/ ◀── Worker /auth/callback ◀───┘  (đổi code -> refresh_token)
        │
        ▼  (service account đọc)
   Pipeline main.py / stats.py ──▶ đăng / thống kê
```
