#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""XOÁ FILE DRIVE CỦA CÁC KÊNH ĐÃ NGHỈ — và CHỈ khi chứng minh được.  (2/9/2026)

Anh: *"rác channel cũ vẫn còn chưa xoá dứt điểm"*, và chọn mức **xoá cả file Drive**.

── VÌ SAO KHÔNG DÙNG `wipe_queue.py` ───────────────────────────────────────────────────────
`wipe_queue.py` dọn MỌI file dưới kho — nó viết cho lúc muốn làm sạch trắng. Ở đây 18 kênh đang
dùng có video nằm chung kho với video kênh cũ, nên "dọn cả kho" là xoá luôn hàng đang chạy.

── LUẬT AN TOÀN: KHÔNG CHỨNG MINH ĐƯỢC THÌ KHÔNG ĐỤNG ──────────────────────────────────────
Mỗi video có một sidecar `<tên>.json` do `enqueue.py` ghi, trong đó có trường `channel`. Đó là
BẰNG CHỨNG duy nhất được chấp nhận. Cụ thể:

    sidecar đọc được · channel KHÔNG có trong channels.yaml   -> xoá
    sidecar đọc được · channel CÓ trong channels.yaml         -> giữ
    không có sidecar / đọc hỏng / thiếu trường `channel`      -> GIỮ, và báo ra

Dòng thứ ba là dòng quan trọng nhất: **không biết ≠ đã nghỉ**. Đoán theo tiền tố tên tệp
(`v3_`, `v5_`, `v9_`) nghe rất hợp lý và sẽ sai đúng một lần — lần ấy là xoá vĩnh viễn một video
đang chạy. Cùng bài học với "danh sách ngoại lệ là danh sách vô hạn" (CLAUDE.md 13.9): đừng liệt
kê dấu hiệu, hãy đọc nguồn sự thật.

Và như mọi lệnh dọn trong repo: **danh sách giữ lại rỗng thì DỪNG**, không xoá gì. Rỗng nghĩa là
đọc `channels.yaml` hỏng, mà lúc ấy "không kênh nào được giữ" đọc ra y hệt "xoá sạch".

    python src/don_drive_kenh.py               # chỉ đếm và liệt kê, không đụng gì
    python src/don_drive_kenh.py --that        # xoá thật (vĩnh viễn, thu hồi dung lượng)
    python src/don_drive_kenh.py --that --rac  # bỏ vào thùng rác thay vì xoá hẳn (cứu được 30 ngày)
"""
import argparse
import io
import json
import os
import sys
from collections import Counter

GOC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, GOC)

import storage as ST                       # noqa: E402

YAML = os.path.join(GOC, "..", "config", "channels.yaml")


def giu_lai() -> set:
    """Kênh GIỮ LẠI, đọc thẳng channels.yaml. Rỗng = dừng cả lệnh."""
    if not os.path.exists(YAML):
        return set()
    try:
        import yaml
        d = yaml.safe_load(io.open(YAML, encoding="utf-8")) or {}
        return {str(k).upper() for k in (d.get("channels") or {})}
    except Exception:
        import re
        s = io.open(YAML, encoding="utf-8").read()
        return {m.upper() for m in re.findall(r"^\s{2}([A-Z0-9_]+):", s, re.M)}


def _quet(drv, thu_muc: str, sau: int = 0, ra=None) -> list:
    """Mọi file (không phải thư mục) dưới một thư mục, đệ quy."""
    ra = [] if ra is None else ra
    if sau > 4:
        return ra
    trang = None
    while True:
        r = drv.svc.files().list(
            q=f"'{thu_muc}' in parents and trashed=false",
            fields="nextPageToken,files(id,name,mimeType,size)", pageSize=1000,
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


def _kenh_cua(drv, ten_json: dict) -> str:
    """Đọc `channel` trong sidecar. Không đọc được thì trả "" — và "" nghĩa là ĐỪNG ĐỤNG."""
    try:
        dl = drv.svc.files().get_media(fileId=ten_json["id"]).execute()
        d = json.loads(dl.decode("utf-8", "ignore"))
        return str(d.get("channel") or "").upper().replace(" ", "")
    except Exception:
        return ""


def don_mot_kho(acc, giu: set, that: bool, rac: bool) -> tuple:
    """Trả (số file xoá, số byte thu hồi, số file không xác định được kênh, đếm theo kênh)."""
    # Mở kho bằng ĐÚNG đường mà `wipe_queue.py` dùng. `Drive(acc)` trông hợp lý và sai:
    # `Drive.__init__(self, service=None)` không nhận hồ sơ kho, nên `Drive(acc)` sẽ coi cái dict
    # ấy là `service` và chết ở lệnh gọi đầu tiên — sau khi đã quét xong, tức đúng lúc tệ nhất.
    # (CLAUDE.md 13.15: bài kiểm phải gọi bằng đúng đường mà mã thật gọi.)
    drv = ST.account_drive(acc)
    root = acc.get("root") or ""
    if not root:
        return 0, 0, 0, Counter()
    tep = _quet(drv, root)
    theo_goc = {}
    for f in tep:
        goc = f["name"].rsplit(".", 1)[0]
        theo_goc.setdefault(goc, []).append(f)

    xoa = byte = mo = 0
    dem = Counter()
    for goc, nhom in theo_goc.items():
        js = next((x for x in nhom if x["name"].lower().endswith(".json")), None)
        if not js:
            mo += len([x for x in nhom if not x["name"].lower().endswith(".json")])
            continue
        k = _kenh_cua(drv, js)
        if not k:
            mo += len(nhom)
            continue
        if k in giu:
            continue
        dem[k] += 1
        for f in nhom:
            byte += int(f.get("size") or 0)
            if that:
                try:
                    drv.trash(f["id"]) if rac else drv.delete(f["id"])
                except Exception as e:
                    print(f"      ⚠ không xoá được {f['name'][:40]}: {str(e)[:60]}")
                    continue
            xoa += 1
    return xoa, byte, mo, dem


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--that", action="store_true", help="xoá thật (mặc định chỉ liệt kê)")
    ap.add_argument("--rac", action="store_true", help="bỏ vào thùng rác thay vì xoá vĩnh viễn")
    a = ap.parse_args()

    giu = giu_lai()
    if not giu:
        print("❌ DỪNG: danh sách giữ lại RỖNG (đọc channels.yaml hỏng).")
        print("   Rỗng nghĩa là 'không kênh nào được giữ' — đọc ra y hệt 'xoá sạch'.")
        return 2
    print(f"→ giữ lại {len(giu)} kênh: {', '.join(sorted(giu))}")

    ho = ST.pool_accounts() or []
    if not ho:
        print("❌ không đọc được danh sách kho — DỪNG (không dám coi 'kho rỗng' là 'không có gì').")
        return 1
    print(f"→ {len(ho)} kho Drive · chế độ: "
          f"{'XOÁ THẬT' if a.that and not a.rac else ('BỎ THÙNG RÁC' if a.that else 'chạy thử')}")

    t_xoa = t_byte = t_mo = 0
    tong = Counter()
    for i, acc in enumerate(ho, 1):
        ten = acc.get("email") or acc.get("name") or f"kho{i}"
        try:
            x, b, m, d = don_mot_kho(acc, giu, a.that, a.rac)
        except Exception as e:
            print(f"   ⚠ {ten[:28]:28s} lỗi: {str(e)[:70]}")
            continue
        t_xoa += x; t_byte += b; t_mo += m; tong.update(d)
        if x or m:
            print(f"   {ten[:28]:28s} {x:4d} file kênh cũ · {m} file không rõ kênh (giữ)")

    print(f"\n📊 {'ĐÃ XOÁ' if a.that else 'SẼ XOÁ'} {t_xoa} file · {t_byte/1e9:.2f} GB")
    if tong:
        print("   theo kênh: " + ", ".join(f"{k}×{v}" for k, v in tong.most_common(12)))
    if t_mo:
        print(f"   ⚠ {t_mo} file KHÔNG có sidecar đọc được — giữ nguyên. Không biết ≠ đã nghỉ.")
    if not a.that:
        print("\n⚠ CHẠY THỬ — chưa đụng gì. Thêm `--that` để xoá thật.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
