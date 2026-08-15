# 🤝 HANDOFF — Bàn giao cho phiên Claude Code / người khác tiếp tục

> Đọc file này + `PROGRESS.md` + `SETUP.md` là nắm toàn bộ. Đây là "bản đồ" để sửa/nâng cấp mà không cần hỏi lại từ đầu.

---

## 🔗 Tài nguyên đã tạo (2026-08-15)

| Thứ | Giá trị |
|---|---|
| **GitHub repo** | https://github.com/braydanamilio-spec/mm0-auto-publisher (private, branch `main`) |
| **Firebase project ID** | `mm0-auto-publisher` |
| **Firebase console** | https://console.firebase.google.com/project/mm0-auto-publisher/overview |
| **Web App ID** | `1:377166959818:web:eabe7e170d02cc8eaa5033` (config đã nhúng sẵn trong `dashboard/index.html`) |
| **Tài khoản hạ tầng** | GitHub `braydanamilio-spec` · Firebase/Google `braydanamilio@gmail.com` |
| **Dashboard LIVE** | https://mm0-auto-publisher.web.app (user thật: `adisondurham@gmail.com`, đã cấp Editor+firebaseauth.admin) |
| **Connect Worker (Cloudflare)** | `https://mm0-connect.adisondurham-ef1.workers.dev` (tài khoản Cloudflare adisondurham) |

### 🔌 Worker endpoints (connect-worker/src/worker.js)
- `GET /auth/start?kind=youtube|drive&t=<idToken>` (channel để trống → tự lấy tên kênh thật) → OAuth.
- `GET /auth/callback` → đổi code lấy refresh_token, lưu Firestore `connections/{uid}__{label}__{kind}` (rules chặn client), `channels/{uid}__{label}` (có email), `storage_accounts/{uid}__{label}`.
- `GET/POST /api/branding` · `GET /api/comments` · `POST /api/comment-action` · `POST /api/disconnect` — đều verify Firebase ID token (JWKS RS256) → uid, có CORS. Worker tự đổi refresh_token→access_token gọi YouTube API (dashboard KHÔNG chạm token).
- Secrets Worker: `YT_CLIENT_ID/SECRET`, `SA_CLIENT_EMAIL`, `SA_PRIVATE_KEY`, `FIREBASE_PROJECT_ID`, `ALLOW_EMAIL`. Deploy: `cd connect-worker && npx wrangler deploy`.

### 🧭 Method mấu chốt (multi-tenant)
- Mọi doc có field `owner`=uid; doc id `{uid}__{channel}`; dashboard đọc `where owner==uid`, ghi `settings/overrides__{uid}`.
- Client ĐƯỢC sửa video: chỉ field `status`/`publish_at`/`reviewed_at` (rules) → dùng cho trang Đăng bài (Đăng ngay/Rải lịch).
- Chống trùng: `src/dedup.py` vân tay nội dung → `State.sig_exists(channel,sig)` tại `enqueue()`.
- Nhãn kênh tự sinh: `slugLabel(channel_title)` trong worker khi user để trống ô tên.

> ⚠️ Secrets (OAuth token, service account key, FB token) **KHÔNG** nằm trong repo — chúng nằm ở **GitHub → Settings → Secrets**. Code chỉ tham chiếu tên biến (xem `config/channels.yaml`).

---

## 🧭 Cách phiên Claude Code khác vào việc

```bash
# 1. Clone
gh repo clone braydanamilio-spec/mm0-auto-publisher
cd mm0-auto-publisher

# 2. Đọc theo thứ tự
#    PROGRESS.md      -> đang ở đâu, việc tiếp theo
#    HANDOFF.md       -> file này (tài nguyên + method)
#    README.md        -> tổng quan kiến trúc
#    SETUP.md         -> 8 bước đưa vào vận hành
#    docs/PRODUCTION-SPEC.md -> chuẩn file cho khâu làm video

# 3. Kiểm tra code còn chạy (không cần secrets)
python3 -m py_compile src/*.py scripts/*.py

# 4. Test logic không upload
#    (cần secrets thật để chạy đầy đủ; dry-run mô phỏng luồng)
python src/main.py --dry-run
```

**Quy ước khi sửa:**
- Sửa xong luôn `py_compile` lại + cập nhật `PROGRESS.md` (ghi dòng mới nhất lên đầu mục "Ghi chú trạng thái").
- Commit message tiếng Việt ngắn gọn; push lên `main`.
- KHÔNG hardcode secrets vào code. Thêm secret mới → khai báo tên `*_env` trong `channels.yaml` + map trong `.github/workflows/publish.yml`.

---

## 🗂️ Bản đồ code (sửa gì ở đâu)

| Muốn thay đổi | Sửa file |
|---|---|
| Thêm/bớt kênh, đổi branding/hashtag | `config/channels.yaml` (+ block env trong `.github/workflows/publish.yml`) |
| Đổi nhịp đăng, giờ vàng, trần an toàn | `config/posting_templates.yaml` |
| Logic chọn giờ / lọc "đến giờ" | `src/scheduler.py` |
| Cách dựng title/desc/hashtag + lint | `src/metadata.py` |
| Luồng đăng chính | `src/main.py` |
| Upload YouTube (+ thumbnail, native schedule) | `src/youtube_uploader.py` |
| Đăng Facebook (video/Reels) | `src/facebook_uploader.py` |
| Đọc/ghi Drive | `src/drive_client.py` |
| State + counters (Firestore) | `src/firestore_state.py` |
| Cầu nối render→hàng đợi | `src/enqueue.py`, `scripts/watch_and_enqueue.py` |
| Giao diện dashboard | `dashboard/index.html` |
| Bảo mật Firestore | `dashboard/firestore.rules` |
| Lịch cron chạy | `.github/workflows/publish.yml` |

---

## ⏳ Việc hạ tầng CÒN LẠI (cần đăng nhập tài khoản — Claude không tự làm hết được)

1. **Bật các API trong project `mm0-auto-publisher`** (bấm Enable, chờ ~2 phút):
   - Firestore: https://console.developers.google.com/apis/api/firestore.googleapis.com/overview?project=mm0-auto-publisher
   - Google Drive: https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project=mm0-auto-publisher
   - YouTube Data API v3: https://console.developers.google.com/apis/api/youtube.googleapis.com/overview?project=mm0-auto-publisher
2. **Tạo Firestore DB** (sau khi bật API):
   ```bash
   cd dashboard
   firebase firestore:databases:create "(default)" --location asia-southeast1 --project mm0-auto-publisher
   ```
3. **Bật Authentication → Google** (Firebase Console → Authentication → Sign-in method → Google → Enable). *(bước này chỉ có trên console)*
4. **Sửa email chủ** trong `dashboard/firestore.rules` (`YOUR_EMAIL@gmail.com` → email bạn đăng nhập dashboard).
5. **Deploy dashboard + rules:**
   ```bash
   cd dashboard
   firebase deploy --only hosting,firestore:rules --project mm0-auto-publisher
   ```
6. Làm tiếp **Bước 1–7 của SETUP.md** (OAuth per-channel, service account, share Drive, GitHub Secrets).

---

## 🧩 Method tóm tắt (triết lý hệ thống)

- **1 nguồn sự thật = Firestore.** Không đăng trùng, không sót.
- **API-only** (không proxy, không trình duyệt giả lập). Auth bằng token, không phụ thuộc IP.
- **Free-first:** GitHub Actions (cron) + Firestore/Hosting (Spark) + Drive.
- **Tách bạch bí mật:** code public-safe; secrets ở GitHub Secrets.
- **Idempotent:** mỗi lần cron chạy độc lập, an toàn nếu chạy lại.
