#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DỰNG LẠI SỔ TỪ DRIVE — video có thật mà không bản ghi nào biết.  (2/9/2026)

Anh: *"a vẫn chưa thấy videos nào được hiển thị web để a check và coi nó."*

── TÌNH HUỐNG ──────────────────────────────────────────────────────────────────────────────
36 video của 18 kênh nằm thật trên Drive (đếm từ log: mỗi luồng in `→ kho X · <mã tệp>`), ô
"Video trong kho" đọc kho thật cũng ra 142. Nhưng **thư viện trống** và ô "Hôm nay" bằng 0, vì
cả hai đọc **bản ghi job** — mà bản ghi rỗng: lượt render 12:41 chạy bằng mã cũ, gọi lệnh
`ghi_job` mà Worker không nhận (HTTP 500).

Bản vá đã đẩy, nhưng nó chỉ cứu các lượt SAU. Video đã làm rồi thì không lượt nào quay lại ghi
sổ hộ chúng — chúng sẽ vô hình mãi mãi. Nên cần một lượt dựng lại sổ, chạy một lần.

── VÌ SAO KHÔNG DÙNG `heal_unpushed` ───────────────────────────────────────────────────────
Hàm ấy chữa ca NGƯỢC LẠI: có bản ghi mà thiếu `drive_id` (render xong nhưng đẩy hụt). Ở đây có
tệp mà thiếu bản ghi. Hai chiều khác nhau, không dùng chung được.

── BẰNG CHỨNG DUY NHẤT ĐƯỢC CHẤP NHẬN ──────────────────────────────────────────────────────
Sidecar `.json` mà `enqueue.py` ghi kèm mỗi video: nó giữ `channel`, `type`, `title`. Không có
sidecar thì BỎ QUA, không đoán theo tên tệp — cùng luật với `don_drive_kenh`: không biết thì
đừng ghi bừa, vì một bản ghi sai kênh còn tệ hơn không có bản ghi.

Ghi theo ĐÚNG id ổn định mà `enqueue` dùng (`gt-<kênh>-<loại>-<tên tệp>`) nên chạy lại nhiều
lần cũng chỉ ghi đè chính nó, không đẻ dòng rác.

    python src/dung_lai_so.py           # chỉ đếm
    python src/dung_lai_so.py --that    # ghi thật
"""
import argparse
import json
import re as _re
import concurrent.futures as _tp
import os
import sys

GOC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, GOC)
sys.path.insert(0, os.path.join(GOC, "..", "..", "render-pipeline"))

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
    ap.add_argument("--that", action="store_true")
    a = ap.parse_args()
    owner = os.environ.get("OWNER_UID", "")
    if not owner:
        print("❌ thiếu OWNER_UID")
        return 2

    import hot_db as H
    ho = ST.pool_accounts() or []
    if not ho:
        print("❌ không đọc được danh sách kho — DỪNG (không coi 'không thấy' là 'không có').")
        return 1
    print(f"→ soi {len(ho)} kho Drive")

    thay = ghi = bo = 0
    kho_doc = 0
    for i, acc in enumerate(ho, 1):
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
        goc = {}
        for f in tep:
            goc.setdefault(f["name"].rsplit(".", 1)[0], []).append(f)
        can = []
        for base, nhom in goc.items():
            mp4 = next((x for x in nhom if x["name"].lower().endswith(".mp4")), None)
            js = next((x for x in nhom if x["name"].lower().endswith(".json")), None)
            if not mp4:
                continue
            thay += 1
            can.append((base, mp4, js))
        # ── TẢI SIDECAR SONG SONG  (3/9/2026) ─────────────────────────────────────────────
        # Bản đầu tải `.json` của từng video TUẦN TỰ trong vòng lặp. Với hơn 1.300 video đó là
        # 1.300 vòng mạng nối đuôi nhau — đo được 18 phút, mà job `don_kho.yml` chỉ có
        # `timeout-minutes: 20`, nên nó bị huỷ TRƯỚC bước đối chiếu cuối. Nhìn từ dashboard cả
        # lượt hiện ra "cancelled", che mất việc phần dọn Firestore đã chạy xong sạch ở bước đầu.
        #
        # Đã thử bỏ hẳn sidecar và đọc kênh/loại từ tên `v9_<kênh>_NNNN[_long]`. Nhanh tuyệt
        # đối, nhưng TIÊU ĐỀ chỉ có trong sidecar — 1.300 bản ghi sẽ mang tên tệp làm tiêu đề
        # và thư viện trên web đọc ra "v9_howloud_0000_long". Đổi một lỗi lấy một lỗi.
        #
        # Nên giữ sidecar và làm nó song song. Tám luồng vì đây là việc CHỜ MẠNG, không phải
        # việc tính; cao hơn thì Drive bắt đầu trả 403 rateLimitExceeded.
        def _doc(bo3):
            base3, mp43, js3 = bo3
            d3 = {}
            if js3:
                try:
                    d3 = json.loads(drv.svc.files().get_media(
                        fileId=js3["id"]).execute().decode("utf-8", "ignore"))
                except Exception:
                    d3 = {}
            k3 = str(d3.get("channel") or "").upper().replace(" ", "")
            l3 = "long" if str(d3.get("type") or "") == "long" else "short"
            # Sidecar thiếu hoặc tải hỏng thì tên tệp vẫn đủ để biết kênh và loại — mất tiêu
            # đề còn hơn mất cả bản ghi, vì bản ghi mất là video ấy biến khỏi kho trên web.
            if not k3:
                m3 = _re.match(r"^v9_([a-z0-9]+)_\d+(_long)?$", base3, _re.I)
                if m3:
                    k3, l3 = m3.group(1).upper(), ("long" if m3.group(2) else "short")
            return base3, mp43, d3, k3, l3

        with _tp.ThreadPoolExecutor(max_workers=8) as ex:
            ket = list(ex.map(_doc, can))
        for base, mp4, d, kenh, loai in ket:
            if not kenh:
                bo += 1
                continue
            if not a.that:
                ghi += 1
                continue
            H.ghi_job(owner=owner,
                      jid=f"gt-{kenh.lower()}-{loai}-{mp4['name']}",
                      channel=kenh, vtype=loai, status="done",
                      step="đã lên kho (dựng lại sổ)",
                      title=d.get("title") or base,
                      drive_id=mp4["id"], queued=False,
                      at=mp4.get("createdTime") or "")
            ghi += 1
    # ── ĐẾM THỨ ĐÃ GIAO, KHÔNG ĐẾM THỨ ĐÃ XẾP HÀNG  (4/9/2026) ───────────────────────────
    # `ghi` cộng lên ngay sau `H.ghi_job(...)`, nhưng `ghi_job` chỉ NHÉT VÀO BỘ ĐỆM — và
    # còn `return` sớm không báo gì khi `bat_ghi()` tắt. Thứ thật sự đẩy lên D1 là
    # `xa_het()`, mà giá trị trả về của nó bị vứt đi; `_xa_buf` hụt thì nó `break` lặng lẽ
    # và bỏ phần còn lại trong đệm.
    #
    # Nên bản cũ in "ĐÃ GHI 1.200 bản ghi" được cả khi D1 nhận 0 — đúng §15.3: mã thoát
    # trả lời "có nổ không", không trả lời "có giao được hàng không". Nay in cả ba con số
    # và cho lượt chạy HỎNG khi xếp hàng mà không giao được gì.
    # Tín hiệu giao hàng KHÔNG phải giá trị trả về của `xa_het()`: `ghi_job` tự xả mỗi khi
    # đệm đầy `BUF_MAX`, nên ở một lượt LÀNH `xa_het()` chỉ đẩy phần đuôi và trả về một số
    # rất nhỏ. Lấy nó làm cổng là chế ra một cỗ máy bắt oan (§13.8).
    # Thứ trả lời đúng câu hỏi "có sót bản ghi nào không" là ĐỆM SAU KHI XẢ: `xa_het()`
    # lặp tới khi đệm rỗng và `break` khi một lượt đẩy hụt — còn mục trong đệm nghĩa là
    # có bản ghi KHÔNG lên được D1.
    if a.that:
        H.xa_het()
    con = len(getattr(H, "_DEM_BUF", []))
    print(f"\n📊 soi {thay} video trong {kho_doc}/{len(ho)} kho đọc được")
    if a.that:
        print(f"   xếp hàng {ghi} bản ghi · còn kẹt trong đệm {con} · "
              f"bỏ qua {bo} (không có sidecar đọc được)")
        print("   " + H.bao_cao())
        if con:
            print(f"   ❌ {con}/{ghi} bản ghi KHÔNG lên được D1 — sổ dựng lại còn thiếu. "
                  f"Lượt này HỎNG để mốc cron sau tự thử lại.")
            return 1
    else:
        print(f"   SẼ GHI {ghi} bản ghi · bỏ qua {bo} (không có sidecar đọc được)")
    if not thay:
        print("   ⚠ KHÔNG soi được video nào — đây KHÔNG phải bằng chứng kho rỗng. Kiểm quyền khoá dịch vụ.")
        return 1
    if not a.that:
        print("\n⚠ CHẠY THỬ — chưa ghi gì. Thêm `--that` để ghi thật.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
