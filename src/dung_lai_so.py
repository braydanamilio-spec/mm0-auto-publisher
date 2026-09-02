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
        for base, nhom in goc.items():
            mp4 = next((x for x in nhom if x["name"].lower().endswith(".mp4")), None)
            js = next((x for x in nhom if x["name"].lower().endswith(".json")), None)
            if not mp4:
                continue
            thay += 1
            if not js:
                bo += 1
                continue
            try:
                d = json.loads(drv.svc.files().get_media(
                    fileId=js["id"]).execute().decode("utf-8", "ignore"))
            except Exception:
                bo += 1
                continue
            kenh = str(d.get("channel") or "").upper().replace(" ", "")
            loai = "long" if str(d.get("type") or "") == "long" else "short"
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
    if a.that:
        H.xa_het()
    print(f"\n📊 soi {thay} video trong {kho_doc}/{len(ho)} kho đọc được")
    print(f"   {'ĐÃ GHI' if a.that else 'SẼ GHI'} {ghi} bản ghi · bỏ qua {bo} (không có sidecar đọc được)")
    if not thay:
        print("   ⚠ KHÔNG soi được video nào — đây KHÔNG phải bằng chứng kho rỗng. Kiểm quyền khoá dịch vụ.")
        return 1
    if not a.that:
        print("\n⚠ CHẠY THỬ — chưa ghi gì. Thêm `--that` để ghi thật.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
