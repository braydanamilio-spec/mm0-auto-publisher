# 📐 QUY CHUẨN SẢN XUẤT — Contract giữa "Claude làm video" và hệ thống đăng

> File này là **hợp đồng dữ liệu**. Bất kỳ Claude Code / công cụ nào tạo video **PHẢI** xuất file đúng chuẩn dưới đây thì hệ thống đăng mới nhận và đăng chuẩn, không lỗi, đồng bộ tự động.

---

## 1. Nguyên tắc vàng

1 video = **1 file video** + **1 file `.json` (metadata)** + **1 file thumbnail** (long-form) + **phụ đề (tùy chọn)** → đặt cùng tên, cùng thư mục. Tất cả dùng **chung 1 "slug"** làm tên.

```
broke-ep012-broke-to-10k.mp4     ← video
broke-ep012-broke-to-10k.json    ← metadata (title/desc/hashtag/lịch)
broke-ep012-broke-to-10k.jpg     ← thumbnail
broke-ep012-broke-to-10k.srt     ← phụ đề (tùy chọn; .srt hoặc .vtt) → tự upload lên YouTube
```

Chỉ cần đặt đúng, mọi thứ còn lại **tự động**: hệ thống đọc `.json`, đăng đúng kênh, đúng giờ, gắn thumbnail, rồi tự phân loại đã/chưa đăng.

---

## 2. Cấu trúc thư mục xuất (OUTBOX)

Dây chuyền render lưu video ra **1 folder local duy nhất** theo cây sau. Script `watch_and_enqueue.py` sẽ tự đẩy lên Drive `_QUEUE`.

```
OUTBOX/
├── BROKE/                 ← ĐÚNG key kênh trong channels.yaml (BROKE | INSIDE_YOU | HUH)
│   ├── long/
│   │   ├── broke-ep012-broke-to-10k.mp4
│   │   ├── broke-ep012-broke-to-10k.json
│   │   └── broke-ep012-broke-to-10k.jpg
│   └── short/
│       ├── broke-ep013-1dollar-habit.mp4
│       └── broke-ep013-1dollar-habit.json
├── INSIDE_YOU/
│   ├── long/
│   └── short/
└── HUH/
    ├── long/
    └── short/
```

> ⚠️ Tên folder kênh **phải khớp key** trong `config/channels.yaml` (viết HOA, gạch dưới): `BROKE`, `INSIDE_YOU`, `HUH`. Sai key → không đẩy được.

---

## 3. Quy tắc đặt TÊN FILE (slug)

Định dạng: `<kênh>-ep<số 3 chữ số>-<chủ-đề-ngắn>`

| Quy tắc | Đúng ✅ | Sai ❌ |
|---|---|---|
| chữ thường, không dấu | `inside-you-ep004-3am-brain` | `Inside You 3AM Não` |
| dùng gạch nối `-` | `huh-ep021-ocean-facts` | `huh_ep021 ocean facts` |
| có số tập để sắp thứ tự | `broke-ep012-...` | `broke-final-v2-...` |
| ≤ 60 ký tự, chỉ a–z 0–9 `-` | `broke-ep012-broke-to-10k` | `broke-ep012-💰rich!!!` |
| **duy nhất** (không trùng) | mỗi video 1 slug | 2 video cùng tên |

---

## 4. Thông số VIDEO (kỹ thuật)

| | **LONG (dọc thư viện)** | **SHORT / Reels** |
|---|---|---|
| Tỷ lệ khung | **16:9 ngang** | **9:16 dọc** |
| Độ phân giải | 1920×1080 (tối thiểu 1280×720) | 1080×1920 |
| Thời lượng | **≥ 10 phút** | **< 3 phút** (tốt nhất 15–60 giây) |
| FPS | 24–30 | 24–30 |
| Codec video / audio | H.264 / AAC | H.264 / AAC |
| Container | `.mp4` | `.mp4` |
| Dung lượng | ≤ 2–3 GB (streaming lên OK) | ≤ 300 MB |
| Âm lượng | chuẩn hoá ~ −14 LUFS | ~ −14 LUFS |

> Short **nên** có chữ `#shorts` trong title hoặc description để YouTube phân loại đúng (hệ thống tự thêm nếu thiếu).

---

## 5. Thông số THUMBNAIL

| | LONG | SHORT |
|---|---|---|
| Bắt buộc? | **Có** (thu hút click) | Không (YouTube tự lấy khung hình); có thì tốt |
| Kích thước | **1280×720** (16:9) | 1080×1920 nếu làm |
| Định dạng | `.jpg` hoặc `.png` | như trên |
| Dung lượng | **< 2 MB** (giới hạn YouTube) | < 2 MB |
| Nội dung | mặt biểu cảm + ≤ 4 chữ to, tương phản cao | — |

Đặt tên **trùng slug** với video: `broke-ep012-broke-to-10k.jpg`.

---

## 6. File METADATA `.json` — chuẩn quan trọng nhất

Đặt cạnh video, **cùng slug**, đuôi `.json`. Trường nào bỏ trống → hệ thống tự sinh theo branding kênh.

### Schema đầy đủ

```jsonc
{
  "topic":       "How I Went From Broke To $10k/Month",   // BẮT BUỘC — ý chính, dùng sinh title/tag
  "type":        "long",                                   // "long" | "short" (khớp folder)
  "title":       "How I Went From Broke To $10k/Month (True Story)",  // ≤100 ký tự, không < >
  "description": "Câu chuyện thật...\n\nTrong video bạn sẽ học:\n- ...",// ≤5000 ký tự
  "hashtags":    ["#money", "#sidehustle", "#finance"],    // 3–5 cái; YouTube chỉ hiện 3 cái đầu
  "tags":        ["make money online", "broke to rich"],   // keyword YouTube (khác hashtag)
  "platforms":   ["youtube", "facebook"],                  // đăng nền tảng nào
  "thumbnail":   "broke-ep012-broke-to-10k.jpg",           // tên file thumb (long-form)
  "captions":    [                                         // TÙY CHỌN — phụ đề, tự upload lên YouTube
    { "file": "broke-ep012-broke-to-10k.srt", "language": "en", "name": "English" }
  ],
  "publish_at":  "2026-08-16T20:30:00+07:00"               // TÙY CHỌN. Bỏ trống = auto theo template lịch
}
```

**Phụ đề (subtitle):** đặt file `.srt` hoặc `.vtt` cùng slug với video. Nếu dùng `watch_and_enqueue`/`OUTBOX`, hệ thống **tự nhận** `<slug>.srt`/`.vtt` — không cần khai báo. Nhiều ngôn ngữ thì thêm nhiều mục trong `captions`.

### Ví dụ tối giản cho SHORT (để hệ thống tự lo phần còn lại)

```json
{
  "topic": "The $1 Habit That Made Me Rich",
  "type": "short",
  "platforms": ["youtube", "facebook"]
}
```

### Giới hạn ký tự theo nền tảng (đã kiểm tra sẵn bằng `metadata.lint()`)

| Trường | YouTube | Facebook |
|---|---|---|
| Title | ≤ 100 ký tự, không chứa `< >` | ≤ 255 |
| Description | ≤ 5000 | ~ 2200 (Reels) |
| Hashtag | 3–5 (chỉ tính 3 đầu) | 3–5 |

---

## 7. Chuẩn nội dung TITLE / DESCRIPTION / HASHTAG (để "top 1")

- **Title**: hook trong 5 từ đầu; con số + kết quả cụ thể; không clickbait sai sự thật (dễ mất kiếm tiền). VD: `How I Turned $100 Into $10k (Real Story)`.
- **Description**: 2 câu đầu tóm tắt giá trị (hiện trên feed) → danh sách bullet nội dung → CTA subscribe → **disclaimer** (hệ thống tự chèn CTA + disclaimer theo kênh nếu bạn để trống).
- **Hashtag**: 3–5, sát niche. Tránh cụm rủi ro: `guaranteed money`, `get rich quick`, `free money`, `sub4sub`… (hệ thống sẽ cảnh báo).
- Ngôn ngữ: mặc định **English** (đúng khán giả US theo định hướng kênh).

---

## 8. Dữ liệu ĐỒNG BỘ như thế nào (để bạn hình dung)

```
[Claude làm video]                         [Auto-Publisher]
render xong -> lưu vào                      GitHub Actions (mỗi 30')
OUTBOX/<KÊNH>/<type>/<slug>.{mp4,json,jpg}      │
        │                                       ▼
        ▼   watch_and_enqueue.py           đọc _QUEUE + .json
Drive: <KÊNH>/_QUEUE/<type>/  ───────────▶ đăng YT+FB đúng giờ
        (video + json + thumb)                  │
                                                ▼
                                    chuyển sang _POSTED + ghi Firestore
                                                │
                                                ▼
                                        Dashboard hiện realtime
```

Mọi trạng thái nằm ở Firestore (1 nguồn sự thật) → **không trùng, không sót**. Đã đăng thì file rời `_QUEUE`, không đăng lại.

---

## 9. ✂️ COPY khối này giao cho "Claude Code làm video"

> Dán nguyên văn phần dưới vào yêu cầu khi nhờ Claude Code khác tạo video:

```
QUY CHUẨN XUẤT FILE (bắt buộc tuân thủ tuyệt đối):

1. Mỗi video xuất ra 3 file cùng tên (slug), lưu vào:
   OUTBOX/<KÊNH>/<long|short>/
   trong đó <KÊNH> ∈ {BROKE, INSIDE_YOU, HUH} (viết HOA, đúng như vậy).

2. Slug đặt tên: "<kênh_thường>-ep<3 số>-<chủ-đề-ngắn>", chữ thường không dấu,
   dùng gạch nối, chỉ a-z 0-9 -, tối đa 60 ký tự, duy nhất.
   VD: broke-ep012-broke-to-10k

3. Ba file:
   - <slug>.mp4   : LONG = 1920x1080 (16:9), ≥10 phút, H.264/AAC.
                    SHORT = 1080x1920 (9:16), <60s lý tưởng, H.264/AAC.
   - <slug>.json  : metadata theo schema bên dưới.
   - <slug>.jpg   : thumbnail 1280x720 (<2MB) cho LONG (SHORT có thể bỏ).
   - <slug>.srt   : (TÙY CHỌN) phụ đề .srt/.vtt cùng slug -> hệ thống tự upload lên YouTube.

4. Nội dung <slug>.json (English):
   {
     "topic": "<ý chính 1 câu>",
     "type": "long" | "short",
     "title": "<hook ≤100 ký tự, không dùng < >>",
     "description": "<2 câu tóm tắt + bullet + để trống CTA/disclaimer cho hệ thống tự chèn>",
     "hashtags": ["#..", "#..", "#.."],
     "tags": ["keyword", "keyword"],
     "platforms": ["youtube", "facebook"],
     "thumbnail": "<slug>.jpg",
     "publish_at": ""   // để trống -> hệ thống tự lên lịch theo template
   }

5. KHÔNG dùng cụm rủi ro chính sách: guaranteed money, get rich quick,
   free money, sub4sub. Title phải đúng sự thật (tránh mất kiếm tiền).

6. Xuất xong để yên trong OUTBOX — KHÔNG tự upload. Hệ thống đăng sẽ tự lấy.
```

---

## 10. ✅ Checklist QA trước khi để video vào OUTBOX

- [ ] Đúng folder `OUTBOX/<KÊNH>/<long|short>/`, key kênh viết HOA đúng.
- [ ] 3 file cùng slug; slug hợp lệ (a–z 0–9 `-`, ≤60).
- [ ] LONG: 16:9, ≥10'; SHORT: 9:16, <3'. Codec H.264/AAC, `.mp4`.
- [ ] Thumbnail 1280×720, < 2MB (long-form).
- [ ] `.json` có `topic` + `type` đúng; title ≤100 ký tự không `< >`.
- [ ] Hashtag 3–5, không cụm rủi ro.
- [ ] `platforms` đúng kênh cần đăng.

> Đạt hết checklist = hệ thống đăng chuẩn "top 1", không cần bạn đụng tay.
