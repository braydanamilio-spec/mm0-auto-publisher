-- ══ BẢNG "NÓNG" CHUYỂN SANG D1 (24/8/2026) ══════════════════════════════════════════════════
-- Chỉ chuyển thứ ĐỌC/GHI NHIỀU mà KHÔNG cần realtime và KHÔNG cần rules phía trình duyệt.
-- Dashboard vẫn ở Firestore vì nó sống nhờ hai thứ đó.
--
-- MỖI BẢNG CÓ ĐÚNG MỘT CHỦ GHI — luật rút ra từ sự cố B/B2 đêm nay:
--   render_job   : pipeline ghi
--   dem_ngay     : pipeline ghi (cộng dồn)
--   key_nghi     : pipeline ghi (sổ nghỉ key dùng chung 18 luồng)
--   hang_cho     : plan ghi khi xếp, luồng ghi khi lấy (giao dịch nguyên tử của SQLite)
-- Không nơi nào khác được ghi vào các bảng này.
--
-- D1 đếm SỐ DÒNG ĐỌC, nên mọi truy vấn trong hệ đều phải trúng index bên dưới.

CREATE TABLE IF NOT EXISTS render_job (
  id          TEXT PRIMARY KEY,
  owner       TEXT NOT NULL,
  channel     TEXT NOT NULL,
  vtype       TEXT NOT NULL,            -- long | short
  status      TEXT NOT NULL,            -- queued | running | done | failed ...
  step        TEXT,
  title       TEXT,
  drive_id    TEXT,
  queued      INTEGER DEFAULT 0,        -- 0 = chưa xếp lịch đăng
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
-- đếm "kênh này đã có bao nhiêu video xong" — truy vấn nóng nhất, gọi ~110 lần mỗi phiên plan
CREATE INDEX IF NOT EXISTS ix_job_dem   ON render_job(owner, channel, vtype, status);
-- auto_enqueue tìm video mới chưa xếp lịch
CREATE INDEX IF NOT EXISTS ix_job_queue ON render_job(owner, status, queued);
CREATE INDEX IF NOT EXISTS ix_job_drive ON render_job(drive_id);

-- sổ đếm theo ngày: 1 dòng/ngày/kênh, cộng dồn bằng UPSERT
CREATE TABLE IF NOT EXISTS dem_ngay (
  owner   TEXT NOT NULL,
  ngay    TEXT NOT NULL,               -- YYYYMMDD theo giờ Thái Bình Dương
  channel TEXT NOT NULL,               -- '' = tổng của cả ngày
  so_long INTEGER DEFAULT 0,
  so_short INTEGER DEFAULT 0,
  PRIMARY KEY (owner, ngay, channel)
);

-- sổ nghỉ key dùng chung cho 18 máy (thay doc __cool__ trên Firestore)
CREATE TABLE IF NOT EXISTS key_nghi (
  kid     TEXT NOT NULL,               -- SHA1 12 ký tự, KHÔNG lưu key trần
  loai    TEXT NOT NULL,               -- viet | vis | ve
  den     TEXT NOT NULL,               -- ISO, hết giờ nghỉ
  PRIMARY KEY (kid, loai)
);
CREATE INDEX IF NOT EXISTS ix_nghi_den ON key_nghi(den);

-- hàng chờ kênh: luồng nào xong trước lấy dòng kế (UPDATE ... RETURNING = nguyên tử)
CREATE TABLE IF NOT EXISTS hang_cho (
  owner    TEXT NOT NULL,
  channel  TEXT NOT NULL,
  phien    TEXT NOT NULL,              -- id phiên, để dọn hàng cũ
  lay_boi  TEXT,                       -- NULL = chưa ai lấy
  lay_luc  TEXT,
  PRIMARY KEY (owner, channel, phien)
);
CREATE INDEX IF NOT EXISTS ix_cho ON hang_cho(owner, phien, lay_boi);

-- sổ ngân sách đọc/ghi theo ngày, để bức tường quota nhìn được TỔNG của cả hệ
CREATE TABLE IF NOT EXISTS ngan_sach (
  ngay TEXT PRIMARY KEY,
  doc  INTEGER DEFAULT 0,
  ghi  INTEGER DEFAULT 0
);
