-- ══ HẠN MỨC ĐĂNG YOUTUBE (24/8/2026) ════════════════════════════════════════════════════════
-- YouTube cho 10.000 đơn vị/ngày mỗi DỰ ÁN Google Cloud; một lần đăng tốn 1.600 -> ~6 video/ngày.
-- Trần này CỨNG, không nới bằng code. Cách duy nhất tăng: thêm dự án + cặp OAuth client.
-- Bảng này để thêm dự án là CẮM VÀO CHẠY, không phải sửa code ở 5 chỗ.

CREATE TABLE IF NOT EXISTS yt_project (
  client_id TEXT PRIMARY KEY,
  ten       TEXT,                       -- tên gợi nhớ, vd "mm0-yt-02"
  tran_ngay INTEGER DEFAULT 6,          -- số video/ngày (10.000 ÷ 1.600)
  bat       INTEGER DEFAULT 1,          -- 0 = tạm tắt dự án này
  ghi_chu   TEXT
);

-- đếm đã dùng theo NGÀY + dự án. Reset bằng cách sang dòng ngày mới, không cần dọn.
CREATE TABLE IF NOT EXISTS yt_dung (
  ngay      TEXT NOT NULL,              -- YYYY-MM-DD giờ Thái Bình Dương (mốc reset của Google)
  client_id TEXT NOT NULL,
  da_dung   INTEGER DEFAULT 0,
  PRIMARY KEY (ngay, client_id)
);

-- SỔ ĐĂNG: một dòng mỗi lần đăng thành công -> chia đều giữa kênh, và biết kênh nào đói
CREATE TABLE IF NOT EXISTS yt_da_dang (
  drive_id  TEXT PRIMARY KEY,
  owner     TEXT NOT NULL,
  channel   TEXT NOT NULL,
  vtype     TEXT,
  client_id TEXT,
  luc       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dang_kenh ON yt_da_dang(owner, channel, luc);
