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

_CHUNK = 2 * 1024 * 1024  # 2MB đầu + 2MB cuối


def content_signature(path: str) -> str:
    """Trả về vân tay ngắn, ổn định theo nội dung file."""
    size = os.path.getsize(path)
    h = hashlib.sha1()
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(_CHUNK))          # 2MB đầu
        if size > _CHUNK:
            f.seek(max(0, size - _CHUNK))
            h.update(f.read(_CHUNK))        # 2MB cuối
    return "sig1_" + h.hexdigest()[:24]
