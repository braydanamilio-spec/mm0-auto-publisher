#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DỌN VIDEO DỰNG BẰNG ENGINE CŨ — theo MỐC THỜI GIAN, không xoá sạch.  (3/9/2026)

Anh: *"kho videos render cũ lỗi thì dọn cho anh chứ, xoá sạch rồi render lại cho a chứ hay sao."*

── VÌ SAO KHÔNG DÙNG `lam_lai.yml` (xoá sạch) ──────────────────────────────────────────────
Vì lượt render đang chạy **đã đẩy video MỚI lên cùng kho**. Xoá sạch là giết luôn thứ vừa làm ra
bằng engine đã vá — rồi lại phải dựng lại từ đầu, đúng cái anh dặn tránh: *"đừng làm đi làm lại
tốn tài nguyên."*

── MỐC PHÂN BIỆT ───────────────────────────────────────────────────────────────────────────
Không có cờ nào trong tệp nói "dựng bằng engine nào". Thứ phân biệt được là **giờ tạo trên
Drive**: mọi bản vá hình ảnh (phụ đề karaoke vàng · bỏ dải đen · prompt nền sáng · thẻ chương
chữ to · trần ảnh) đều đẩy xong **trước 07:00 UTC ngày 3/9**. Video tạo trước mốc ấy chắc chắn
mang engine cũ; sau mốc thì chắc chắn mang engine mới.

Mốc thời gian là **bằng chứng gián tiếp**, nên mặc định chỉ BỎ THÙNG RÁC (30 ngày cứu được),
không xoá vĩnh viễn. Nếu mốc chọn sai thì còn kéo lại được — sai một lần mà cứu được thì khác
hẳn sai một lần rồi mất.

    python src/don_video_cu.py --truoc 2026-09-03T07:00:00Z            # đếm thử
    python src/don_video_cu.py --truoc 2026-09-03T07:00:00Z --that     # bỏ thùng rác
"""
import argparse
import os
import sys

GOC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, GOC)

import storage as ST                       # noqa: E402


def _quet(drv, thu_muc, sau=0, ra=None):
    ra = [] if ra is None else ra
    if sau > 4:
        return ra
    trang = None
    while True:
        r = drv.svc.files().list(
            q=f"'{thu_muc}' in parents and trashed=false",
            fields="nextPageToken,files(id,name,mimeType,size,createdTime)", pageSize=1000,
            pageToken=trang, supportsAllDrives=True,
            includeItemsFromAllDrives=True).execute()
        for f in r.get("files", []):
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                _quet(drv, f["id"], sau + 1, ra)
            else:
                ra.append(f)
        trang = r.get("nextPageToken")
        if not trang:
            break
    return ra


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--truoc", required=True, help="mốc ISO, ví dụ 2026-09-03T07:00:00Z")
    ap.add_argument("--that", action="store_true")
    ap.add_argument("--xoa-han", action="store_true", help="xoá vĩnh viễn thay vì bỏ thùng rác")
    a = ap.parse_args()

    ho = ST.pool_accounts() or []
    if not ho:
        print("❌ không đọc được danh sách kho — DỪNG (không coi 'không thấy' là 'không có').")
        return 1
    print(f"→ {len(ho)} kho · dọn tệp tạo TRƯỚC {a.truoc} · "
          f"{'XOÁ HẲN' if a.xoa_han else 'BỎ THÙNG RÁC'} · {'THẬT' if a.that else 'chạy thử'}")

    thay = cu = moi = xong = 0
    kho_doc = 0
    for acc in ho:
        root = acc.get("root") or ""
        if not root:
            continue
        try:
            drv = ST.account_drive(acc)
            tep = _quet(drv, root)
        except Exception:
            continue
        if tep:
            kho_doc += 1
        # ── GOM THÀNH LÔ, KHÔNG GỌI TỪNG TỆP  (4/9/2026) ─────────────────────────────────
        # Lượt chạy đầu bị HUỶ vì quá 45 phút ngay ở bước này: mỗi tệp một vòng mạng, kho
        # ~5.000 tệp là 15–20 phút chỉ để bỏ thùng rác, cộng thời gian quét 100 kho thì vượt
        # trần. Và nó vượt MỖI LẦN — mỗi lượt quét lại từ đầu rồi bị cắt giữa chừng, nên việc
        # dọn không bao giờ tới đích. Xem `Drive.trash_lo`.
        can_xoa = []
        for f in tep:
            thay += 1
            t = str(f.get("createdTime") or "")
            if not t:
                continue                      # KHÔNG biết giờ tạo thì KHÔNG đụng
            if t >= a.truoc:
                moi += 1
                continue
            cu += 1
            if a.that:
                can_xoa.append(f["id"])
        if can_xoa:
            if a.xoa_han:
                for fid in can_xoa:
                    try:
                        drv.delete(fid); xong += 1
                    except Exception as e:
                        print(f"      ⚠ {fid}: {str(e)[:60]}")
            else:
                _ok, _loi = drv.trash_lo(can_xoa)
                xong += _ok
                if _loi:
                    print(f"      ⚠ {acc.get('name')}: {_loi} tệp không bỏ được vào thùng rác")

    print(f"\n📊 soi {thay:,} tệp trong {kho_doc}/{len(ho)} kho đọc được")
    print(f"   cũ (trước mốc): {cu:,}   ·   mới (giữ lại): {moi:,}")
    if a.that:
        print(f"   ✅ đã {'xoá' if a.xoa_han else 'bỏ thùng rác'} {xong:,} tệp")
    else:
        print("\n⚠ CHẠY THỬ — chưa đụng gì. Thêm `--that` để làm thật.")
    if not thay:
        print("   ⚠ KHÔNG soi được tệp nào — đây KHÔNG phải bằng chứng kho rỗng.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
