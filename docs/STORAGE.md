# 💾 KHO LƯU TRỮ THÔNG MINH — Pool nhiều tài khoản + Dọn dẹp + Backup

Gộp nhiều tài khoản Google free (15GB mỗi cái) thành 1 hồ chứa lớn, tự rải video theo chỗ trống, tự (hoặc thủ công) dọn dẹp, và giữ **sổ link** video đã đăng.

---

## 1. Ba chế độ dọn dẹp — anh TÍCH CHỌN trong `config/storage.yaml`

```yaml
cleanup:
  mode: archive      # keep | delete | archive
  keep_days: 14
  trigger: auto      # auto (theo lịch) | manual (chỉ khi anh bấm/gõ lệnh)
```

| mode | Ý nghĩa | Hợp với |
|---|---|---|
| **keep** | Không xoá gì | **Google One** dung lượng lớn — giữ tất cả |
| **delete** | Xoá hẳn file đã đăng > `keep_days` ngày | Pool free — YouTube là backup, chỉ cần link |
| **archive** | Tải file → đẩy sang tài khoản **backup** (kho lạnh) → xoá bản gốc để giải phóng chỗ | Muốn giữ file gốc để **đăng lại** nếu video bị gỡ |

> Link YouTube/Facebook **luôn được lưu** trong Firestore + sổ CSV → dù xoá file, anh vẫn tra được và (nếu archive) tải bản gốc ra đăng lại.

## 2. Hồ chứa nhiều tài khoản (15GB × N → 150GB / 1TB)

Mỗi tài khoản pool cấp quyền Drive **1 lần** → lấy refresh token:

```bash
# tạo OAuth Desktop client (dùng chung được), rồi với TỪNG tài khoản:
python src/auth_setup.py --drive --client-secret client_secret.json
```

Đăng nhập đúng tài khoản đó → copy `CLIENT_ID/SECRET/REFRESH_TOKEN` vào GitHub Secrets theo tên trong `storage.yaml` (`STORE1_*`, `STORE2_*`, …). Mỗi tài khoản tạo 1 folder `MM0-STORE` → lấy folder id → `STOREx_FOLDER_ID`.

Thêm bao nhiêu tài khoản tuỳ ý (copy block trong `storage.yaml`) → dung lượng cộng dồn.

**Xem dung lượng hồ chứa bất cứ lúc nào:**
```bash
python -c "import sys;sys.path.insert(0,'src');import storage,json;print(json.dumps(storage.status_report(),indent=2,ensure_ascii=False))"
```

## 3. Thêm video vào hồ chứa (tự chọn tài khoản còn trống)

```bash
python src/enqueue.py --pool --channel BROKE --type short \
  --video out/ep12.mp4 --topic "..." --thumbnail out/ep12.jpg
```

`--pool` → hệ thống tự chọn tài khoản **còn nhiều chỗ nhất** (dưới `cap_gb`) để đẩy vào. Sidecar tự ghi `channel` để lúc đăng định tuyến đúng kênh.

> Dùng dây chuyền tự động qua đêm: `watch_and_enqueue.py` vẫn hoạt động; muốn dùng pool thì gọi `enqueue --pool` trong pipeline.

## 4. Dọn dẹp

- **Tự động:** để `trigger: auto` → workflow `cleanup.yml` chạy mỗi ngày 03:00 UTC, dọn theo policy.
- **Thủ công:**
  ```bash
  python src/cleanup.py --dry-run     # xem trước sẽ dọn gì
  python src/cleanup.py               # dọn theo keep_days
  python src/cleanup.py --now         # dọn ngay, bỏ qua keep_days
  ```
  hoặc trên GitHub: **Actions → MM0 Storage Cleanup → Run workflow**.

## 5. Sổ link video đã đăng (backup link)

```bash
python src/export_links.py            # -> posted_links.csv
```

CSV gồm: kênh, tiêu đề, ngày đăng, **link YouTube/Facebook**, `source_status` (live/deleted/archived). Đây là sổ cái để quản lý + đăng lại khi cần.

## 6. Chọn nhanh theo nhu cầu

| Anh muốn | Cấu hình |
|---|---|
| Free tối đa, không tốn tiền | Pool nhiều acc 15GB + `mode: delete` |
| Giữ bản gốc để đăng lại nếu bị gỡ | Pool + `mode: archive` + 1 acc backup (Google One) |
| Đơn giản, giữ tất cả | 1 Google One + `mode: keep` |

## 7. Lưu ý chất lượng

Không nơi lưu nào làm giảm chất lượng — miễn upload **file gốc**. YouTube tự re-encode phía họ. Backup/archive chỉ chép nguyên bytes → lossless 100%.
