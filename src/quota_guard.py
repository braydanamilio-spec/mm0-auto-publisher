#!/usr/bin/env python3
"""SỔ QUOTA + PHANH TỰ ĐỘNG cho 3 project Firestore (24/8/2026).

VẤN ĐỀ THẬT ĐÃ XẢY RA
---------------------
Sáng 24/8, `publish` chết 11/12 lượt liên tiếp: thoát ngay ở lệnh đọc ĐẦU TIÊN với
`429 Quota exceeded`. Video vẫn render bình thường nhưng KHÔNG có cái nào được đăng —
mất trắng nửa ngày sản lượng, mà log chỉ nói "hết hạn mức" chứ không nói project nào.

Gói Spark FREE cho mỗi project: **50.000 lượt đọc + 20.000 lượt ghi / ngày**.
Ba project của hệ (A dashboard/keys/connections · B render_jobs · C yt_queue/videos) có
ba trần RIÊNG. Cạn một cái là một mảng việc chết, các mảng khác vẫn chạy — nên càng dễ
tưởng "hệ vẫn ổn".

VÌ SAO CẦN PHANH, KHÔNG CHỈ CẦN TỐI ƯU
--------------------------------------
Tối ưu (đệm, truy vấn có cờ) hạ mức tiêu thụ, nhưng không có gì bảo đảm ngày mai thêm
20 kênh nữa thì không vỡ tiếp. Phanh giải quyết chuyện khác: khi đã dùng tới ngưỡng,
hệ **tự bỏ việc phụ để dành hạn mức cho việc thiết yếu**. Đăng video là thiết yếu;
quét vét, làm tươi thống kê, dựng lại thumbnail thì không.

=> Hỏng có thứ tự: mất vài con số trên dashboard, KHÔNG mất buổi đăng bài.

CÁCH DÙNG
---------
    import quota_guard as QG
    QG.dem("A", r=73)                       # đếm sau mỗi lượt đọc/ghi
    if QG.du_suc("B", 2200):                # xin phép trước việc ĐẮT
        quet_vet()
    QG.xa_so()                              # tự gọi lúc thoát (atexit)

Chi phí của chính sổ này: 1 lượt đọc + 1 lượt ghi mỗi project mỗi tiến trình
(~130 lượt cron/ngày ⇒ 0,26% trần). Rẻ hơn nhiều lần cái nó cứu.
"""
from __future__ import annotations

import atexit
import os
from datetime import datetime, timedelta, timezone

# Trần gói FREE (Spark). Để dạng biến để còn hạ xuống khi thử nghiệm.
TRAN_DOC = 50_000
TRAN_GHI = 20_000

# Dùng quá mức này thì DỪNG mọi việc phụ, chỉ giữ việc thiết yếu.
# 0.75 chứ không phải 0.95: phần đo được luôn THẤP hơn thực tế (dashboard, Worker, và các
# lối đọc chưa gắn đếm không vào sổ), nên phải chừa biên. Chạm 75% đo được ≈ đã đi khá sâu.
NGUONG_PHU = 0.75

_DEM: dict[str, dict[str, int]] = {}
_DA_DOC: dict[str, dict[str, int]] = {}     # số đã dùng đọc từ sổ (1 lần/tiến trình/project)
_TAT = os.environ.get("QUOTA_GUARD_OFF") == "1"


def _ngay() -> str:
    """Ngày theo mốc reset quota của Google (~00:00 giờ Thái Bình Dương ≈ 07:00-08:00 UTC).
    Lấy UTC-7 cho khớp, nếu không thì sổ sẽ sang trang lệch vài tiếng so với lúc Google reset."""
    return (datetime.now(timezone.utc) - timedelta(hours=7)).date().isoformat()


def _client(p: str):
    from firestore_state import client, client_publish, client_render_jobs
    return {"A": client, "B": client_render_jobs, "C": client_publish}[p]()


def dem(p: str, r: int = 0, w: int = 0) -> None:
    """Ghi nhận lượt đọc/ghi vừa dùng ở project p ('A'|'B'|'C'). Không bao giờ ném lỗi."""
    if _TAT or p not in ("A", "B", "C"):
        return
    o = _DEM.setdefault(p, {"r": 0, "w": 0})
    o["r"] += max(0, int(r or 0))
    o["w"] += max(0, int(w or 0))


def da_dung(p: str) -> dict[str, int]:
    """Số đã dùng HÔM NAY ở project p = sổ trên Firestore + phần tiến trình này vừa tiêu.

    Sổ chỉ đọc MỘT lần cho mỗi project trong một tiến trình; đọc hỏng thì coi như 0 (thà cho
    chạy còn hơn tự khoá mình vì không đọc nổi sổ)."""
    goc = _DA_DOC.get(p)
    if goc is None:
        goc = {"r": 0, "w": 0}
        if not _TAT:
            try:
                d = _client(p).collection("quota").document(f"__rw__{_ngay()}").get(timeout=10)
                x = (d.to_dict() or {}) if d.exists else {}
                goc = {"r": int(x.get("r", 0) or 0), "w": int(x.get("w", 0) or 0)}
                goc["r"] += 1                       # chính lượt đọc sổ này
            except Exception:
                pass
        _DA_DOC[p] = goc
    cua_ta = _DEM.get(p, {"r": 0, "w": 0})
    return {"r": goc["r"] + cua_ta["r"], "w": goc["w"] + cua_ta["w"]}


def con_lai(p: str) -> dict[str, int]:
    d = da_dung(p)
    return {"r": max(0, TRAN_DOC - d["r"]), "w": max(0, TRAN_GHI - d["w"])}


def du_suc(p: str, can_doc: int = 0, can_ghi: int = 0, thiet_yeu: bool = False) -> bool:
    """Có nên làm việc tốn `can_doc` lượt đọc / `can_ghi` lượt ghi ở project p không?

    thiet_yeu=True (đăng video, ghi kết quả job): chỉ chặn khi THẬT SỰ không còn chỗ.
    thiet_yeu=False (quét vét, thống kê, thumbnail): chặn sớm ở ngưỡng 75% để dành phần
    còn lại cho việc thiết yếu."""
    if _TAT:
        return True
    d = da_dung(p)
    tran_r = TRAN_DOC if thiet_yeu else int(TRAN_DOC * NGUONG_PHU)
    tran_w = TRAN_GHI if thiet_yeu else int(TRAN_GHI * NGUONG_PHU)
    return (d["r"] + can_doc) <= tran_r and (d["w"] + can_ghi) <= tran_w


def bao_cao(p: str) -> str:
    d = da_dung(p)
    return (f"project {p}: đọc {d['r']:,}/{TRAN_DOC:,} ({d['r']*100//TRAN_DOC}%) · "
            f"ghi {d['w']:,}/{TRAN_GHI:,} ({d['w']*100//TRAN_GHI}%)")


def xa_so() -> None:
    """Cộng phần tiến trình này vào sổ (1 lượt ghi/project). Tự chạy lúc thoát."""
    if _TAT:
        return
    from google.cloud.firestore_v1 import Increment
    for p, o in list(_DEM.items()):
        if not (o["r"] or o["w"]):
            continue
        try:
            _client(p).collection("quota").document(f"__rw__{_ngay()}").set(
                {"r": Increment(o["r"]), "w": Increment(o["w"] + 1), "at": _ngay()}, merge=True)
            o["r"] = o["w"] = 0
        except Exception:
            pass          # ghi sổ hỏng thì thôi — không được làm gãy việc chính vì cái sổ


atexit.register(xa_so)
