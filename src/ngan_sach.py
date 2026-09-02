#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BỨC TƯỜNG HẠN MỨC FIRESTORE CHO REPO ĐĂNG BÀI — cùng sổ với dây chuyền render.  (2/9/2026)

Anh: *"firebase có hạn mức rồi đó, check fix triệt để để không để tình trạng firebase làm ảnh
hưởng hệ thống một lần nào nữa."*

── VÌ SAO HẠN MỨC CỨ CẠN DÙ ANH KHÔNG LÀM GÌ ───────────────────────────────────────────────
Đo được: repo có **sáu workflow chạy theo cron**, phần lớn mỗi giờ một lượt — publish YouTube ·
publish FB/IG · stats · cleanup · thumbnail · trend scout. Khoảng **144 lượt chạy mỗi ngày**, và
**không lượt nào** đi qua bức tường ngân sách, cũng không lượt nào ghi vào sổ.

Hậu quả kép, và vế thứ hai mới là vế độc:
  1. Việc phụ (thống kê, dọn dẹp, thumbnail) tiêu chung hạn mức với việc thiết yếu (đẩy video,
     đăng bài) mà không có ai nhường ai.
  2. Sổ ngân sách **không thấy** phần chúng tiêu. Nên `bao_ngan_sach()` in "0%" trong khi
     Firestore trả 429 — một cái đồng hồ báo bình xăng đầy trong lúc xe đã chết máy. Suốt hôm
     nay tôi đọc "TOÀN HỆ: ĐỌC 0/50.000 (0%)" ngay cạnh dòng `ResourceExhausted: 429`.

**Một cổng canh mà không đếm đúng thứ nó canh thì không phải cổng canh.**

── BA MỨC, GIỐNG HỆT `firestore_bridge.con_ngan_sach` ──────────────────────────────────────
Cố ý dùng chung con số và chung sổ D1, để hai repo không bao giờ nói hai câu khác nhau về cùng
một hạn mức.

    thiet_yeu   : không chạy = mất video đang làm / không đăng được  -> luôn cho chạy
    cuu_du_lieu : không chạy = mất video ĐÃ LÀM XONG                 -> cho tới 92%
    còn lại     : không chạy = thiếu vài con số trên màn hình        -> dừng ở 70%

── ĐI QUA `storage._hot_goi`, KHÔNG TỰ DỰNG LỐI HTTP ───────────────────────────────────────
Repo này đã có hai bản `_hot_goi` (storage, auto_enqueue). Viết bản thứ ba là mất luôn bài học
đã trả giá: thiếu `User-Agent` thì Cloudflare chặn ở cổng với mã 1010 và trả **403 y hệt sai
khoá** — tôi đã một lần báo nhầm "83 khoá Groq đã chết" vì đúng chuyện đó.
"""
import datetime
import os

TRAN_DOC_NGAY = 50_000
TRAN_GHI_NGAY = 20_000
MUC_PHU = 0.70
MUC_KHAN = 0.92


def _ngay() -> str:
    """Ngày theo mốc HỒI hạn mức, không theo ngày lịch địa phương.

    Firestore hồi lúc nửa đêm giờ Thái Bình Dương = 07:00 UTC. Lấy ngày UTC trần thì sổ sẽ sang
    trang lúc 00:00 UTC — bảy tiếng TRƯỚC lúc hạn mức thật sự hồi — và bảy tiếng ấy hệ tưởng
    mình còn nguyên ngân sách trong khi thực tế vẫn đang tiêu nốt của hôm trước.
    """
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=7)).strftime("%Y%m%d")


def _sol() -> dict:
    """Sổ hôm nay, hoặc {} nếu KHÔNG HỎI ĐƯỢC. Hai thứ đó phải khác nhau.

    Bản đầu viết `r = ... or {}` rồi `return {"doc": int(r.get("doc") or 0), ...}` — nên lệnh gọi
    hỏng cũng trả về một dict **hợp lệ** ghi `doc=0`, tức "không hỏi được" biến thành "chưa tiêu
    gì". Nhánh phòng thủ ngay dưới (`if not s: return False`) vì thế không bao giờ chạy, và bức
    tường mở toang đúng lúc nó cần đóng nhất.

    Đây đúng luật CLAUDE.md 15.2 — *mọi con số 0 phải đi kèm mẫu số* — mà tôi viết ra sáng nay
    rồi vi phạm ngay trong tệp tiếp theo. Nên: không có câu trả lời thì trả {} và nói thế.
    """
    try:
        import storage as _ST
        r = _ST._hot_goi("ngan_sach_doc", {"ngay": _ngay()})
    except Exception:
        return {}
    if not isinstance(r, dict) or ("doc" not in r and "ghi" not in r):
        return {}
    return {"doc": int(r.get("doc") or 0), "ghi": int(r.get("ghi") or 0)}


def cong(doc: int = 0, ghi: int = 0) -> None:
    """Ghi vào sổ. Hỏng thì im — ghi sổ không được phép làm hỏng việc chính."""
    if not (doc or ghi):
        return
    try:
        import storage as _ST
        _ST._hot_goi("ngan_sach_cong", {"ngay": _ngay(), "doc": int(doc), "ghi": int(ghi)})
    except Exception:
        pass


def con(loai: str = "doc", thiet_yeu: bool = False, cuu_du_lieu: bool = False) -> bool:
    """Còn được phép làm việc này không?"""
    if thiet_yeu:
        return True
    s = _sol()
    if not s:
        # ĐO KHÔNG ĐƯỢC THÌ GIẢ ĐỊNH CẠN, KHÔNG GIẢ ĐỊNH ĐẦY. Sổ im có hai nghĩa — "chưa tiêu gì"
        # và "không hỏi được" — và đoán sai theo hướng lạc quan là đúng cách hệ tự đập cho
        # Firestore chết sâu hơn. Việc thiết yếu đã thoát ở dòng trên nên chặn ở đây là an toàn.
        return False
    tran = TRAN_DOC_NGAY if loai == "doc" else TRAN_GHI_NGAY
    dung = s.get("doc" if loai == "doc" else "ghi", 0)
    return dung < tran * (MUC_KHAN if cuu_du_lieu else MUC_PHU)


def bao() -> str:
    s = _sol()
    if not s:
        return "🧱 sổ ngân sách: không hỏi được (coi như CẠN — phanh siết)"
    return (f"🧱 hôm nay: ĐỌC {s['doc']:,}/{TRAN_DOC_NGAY:,} "
            f"({s['doc']*100//TRAN_DOC_NGAY}%) · GHI {s['ghi']:,}/{TRAN_GHI_NGAY:,}")


def chan_neu_het(ten: str, loai: str = "doc", cuu_du_lieu: bool = False) -> bool:
    """Cổng dùng ở đầu một script VIỆC PHỤ. Trả True nghĩa là NÊN DỪNG.

    In ra lý do — một bước bị hoãn mà không nói vì sao thì lần sau người đọc log lại đi tìm bug
    ở chỗ không có bug.
    """
    if os.environ.get("BO_QUA_NGAN_SACH") == "1":
        return False
    if con(loai, cuu_du_lieu=cuu_du_lieu):
        return False
    print(f"⏹ {ten}: hoãn — ngân sách Firestore đã qua mức việc-phụ "
          f"({int(MUC_KHAN*100) if cuu_du_lieu else int(MUC_PHU*100)}%).")
    print(f"   {bao()}")
    print("   Việc này không mất dữ liệu khi hoãn; nó sẽ tự chạy ở lượt sau khi hạn mức hồi.")
    return True
