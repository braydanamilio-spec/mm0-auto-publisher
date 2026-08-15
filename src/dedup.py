"""
dedup.py — VÂN TAY NỘI DUNG để chống upload trùng video.

Vấn đề: kéo lại cả folder (100 video cũ + 50 mới) -> không được đăng lại 100 cái cũ.
Giải: mỗi video có 1 "vân tay" ổn định theo NỘI DUNG (không phụ thuộc tên/đường dẫn).
      Trước khi upload, tra sổ cái Firestore; nếu vân tay đã có -> BỎ QUA.

Vân tay = sha1( size + 2MB đầu + 2MB cuối ).
  - Nhanh (không đọc hết file GB), đủ chống trùng cho video thực tế.
  - Đổi tên file vẫn nhận ra trùng (vì dựa trên byte nội dung).
  - Sao chép y hệt -> cùng vân tay -> bị chặn.
"""

from __future__ import annotations
import hashlib
import os

_CHUNK = 1 * 1024 * 1024  # 1MB mỗi điểm lấy mẫu


def content_signature(path: str) -> str:
    """
    Vân tay ổn định theo NỘI DUNG file.

    Lấy mẫu ở NHIỀU vị trí (đầu, 25%, 50%, 75%, cuối) + kích thước chính xác.
    Video cùng bộ (intro/outro giống nhau) vẫn KHÁC vân tay nhờ mẫu ở phần giữa
    -> tránh chặn nhầm video mới. Vẫn nhanh (đọc ~5MB, không đọc cả file GB).
    """
    size = os.path.getsize(path)
    h = hashlib.sha1()
    h.update(str(size).encode())            # size chính xác vào vân tay
    with open(path, "rb") as f:
        if size <= 5 * _CHUNK:
            h.update(f.read())              # file nhỏ: hash toàn bộ (chắc chắn nhất)
        else:
            for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
                off = min(max(0, int(size * frac) - _CHUNK // 2), max(0, size - _CHUNK))
                f.seek(off)
                h.update(f.read(_CHUNK))
    return "sig2_" + h.hexdigest()[:24]
