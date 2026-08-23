#!/usr/bin/env python3
"""DỌN SẠCH _QUEUE của MỌI kho (23/8/2026).

Vì sao cần: bản dọn chạy từ trình duyệt đi qua Cloudflare Worker -> Worker đọc refresh_token ở
Firestore project A. Hôm A cạn quota đọc, Worker trả 429 và mọi lệnh liệt kê/xoá đều chết (tệ hơn:
liệt kê trả rỗng, dễ tưởng nhầm là ĐÃ SẠCH). Script này đi đường storage.py -> danh sách kho lấy từ
BẢN GƯƠNG ở project B, hoàn toàn không đụng A.

Mặc định: ĐƯA VÀO THÙNG RÁC (trashed=true), KHÔNG xoá vĩnh viễn — còn cứu lại được trong 30 ngày.

    python src/wipe_queue.py --dry-run     # chỉ đếm, không đụng gì
    python src/wipe_queue.py               # bỏ vào thùng rác
"""
import argparse
import sys

import storage


def _walk_files(drv, folder_id, depth=0, out=None):
    """Đệ quy lấy MỌI file (không phải thư mục) dưới 1 thư mục."""
    out = [] if out is None else out
    if depth > 4:
        return out
    res = drv.svc.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,mimeType)", pageSize=1000,
        supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get("files", [])
    for f in res:
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            _walk_files(drv, f["id"], depth + 1, out)
        else:
            out.append(f)
    return out


def _wipe_account(acc, dry: bool, scope: str) -> tuple[int, int, str]:
    """Trả (số file thấy, số file đã bỏ thùng rác, lỗi).

    scope="store": dọn MỌI file dưới MM0-STORE (_QUEUE + _POSTED + thumbnail + mọi thư mục con)
    scope="queue": chỉ _QUEUE/long|short
    """
    try:
        drv = storage.account_drive(acc)
    except Exception as e:
        return 0, 0, f"không mở được kho: {str(e)[:70]}"
    root = acc.get("root")
    try:
        start = root if scope == "store" else drv.child_folder(root, "_QUEUE", create=False)
    except Exception as e:
        return 0, 0, f"không mở được thư mục: {str(e)[:70]}"
    if not start:
        return 0, 0, ""
    try:
        files = _walk_files(drv, start)
    except Exception as e:
        return 0, 0, f"liệt kê: {str(e)[:70]}"
    seen = len(files)
    done = 0
    err = ""
    if dry:
        return seen, 0, ""
    for f in files:
        try:
            drv.svc.files().update(fileId=f["id"], body={"trashed": True},
                                   supportsAllDrives=True).execute()
            done += 1
        except Exception as e:
            err = f"xoá {f.get('name', '')[:24]}: {str(e)[:50]}"
    return seen, done, err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="chỉ đếm, không đụng file")
    ap.add_argument("--scope", choices=("store", "queue"), default="store",
                    help="store = mọi file dưới MM0-STORE (mặc định) · queue = chỉ _QUEUE")
    a = ap.parse_args()

    accs = storage.pool_accounts()
    print(f"🧹 Dọn [{a.scope}] trên {len(accs)} kho {'(CHẠY THỬ)' if a.dry_run else ''}", flush=True)
    tot_seen = tot_done = 0
    loi = []
    for i, acc in enumerate(accs, 1):
        seen, done, err = _wipe_account(acc, a.dry_run, a.scope)
        tot_seen += seen
        tot_done += done
        if err:
            loi.append(f"{acc.get('name')}: {err}")
        if seen or err:
            print(f"  [{i}/{len(accs)}] {acc.get('name'):<22} thấy {seen:>4} · dọn {done:>4}"
                  + (f" · ⚠️ {err}" if err else ""), flush=True)
    print(f"\n📊 TỔNG: thấy {tot_seen} file · đã bỏ thùng rác {tot_done} · lỗi {len(loi)}")
    for l in loi[:15]:
        print("   ⚠️", l)
    # Lỗi nào cũng phải hiện thành mã thoát khác 0 — KHÔNG được im lặng báo "sạch" như bản chạy
    # từ trình duyệt hôm 23/8 (429 bị đọc nhầm thành thư mục rỗng).
    return 1 if loi else 0


if __name__ == "__main__":
    sys.exit(main())
