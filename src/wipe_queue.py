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
import os          # 31/8 — thiếu dòng này nên bước đóng sổ chết bằng NameError, và nó chết
                   # ĐÚNG LÚC đã dọn xong: log ghi "TỔNG: 0 file" rồi mới báo "đóng sổ hụt".
                   # Tôi viết khối đóng sổ dùng os.environ mà không kiểm tệp đã import os chưa —
                   # cùng loại lỗi với `so` là biến toàn cục tình cờ: Python chỉ nói khi chạy tới.
import sys
from datetime import datetime, timezone

import storage


def _walk_files(drv, folder_id, depth=0, out=None):
    """Đệ quy lấy MỌI file (không phải thư mục) dưới 1 thư mục."""
    out = [] if out is None else out
    if depth > 4:
        return out
    # ── PHÂN TRANG  (4/9/2026) ─────────────────────────────────────────────────────────
    # Bản cũ gọi `files().list(pageSize=1000)` MỘT LẦN, không lặp `nextPageToken` — trong khi
    # `don_video_cu._quet` và `dung_lai_so._quet` cùng repo đều phân trang đúng.
    #
    # Thư mục quá 1000 tệp thì phần dư bị bỏ lại LẶNG LẼ, và hậu quả đi thành dây chuyền:
    # `main()` in "TỔNG: thấy 1000 · dọn 1000 · lỗi 0", trả 0 (xanh), rồi ghi
    # `kho_that = thấy − dọn = 0` lên D1 — **dashboard khẳng định "kho sạch" trong khi hàng
    # nghìn tệp còn nguyên**. Đúng §15.2: một con số 0 không có mẫu số thật.
    trang = None
    while True:
        r = drv.svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken,files(id,name,mimeType)", pageSize=1000,
            pageToken=trang, supportsAllDrives=True,
            includeItemsFromAllDrives=True).execute()
        for f in r.get("files", []):
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                _walk_files(drv, f["id"], depth + 1, out)
            else:
                out.append(f)
        trang = r.get("nextPageToken")
        if not trang:
            break
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


def don_ban_ghi(ten_kho: set, dry: bool) -> int:
    """Xoá bản ghi `render_jobs` của những kho vừa bị dọn sạch tệp.

    ── VÌ SAO CẦN  (4/9/2026) ──────────────────────────────────────────────────────────────
    Anh: *"sao vẫn hiện 90 videos chưa dọn sạch"* — trong khi ô hồ chứa cùng màn hình hiện
    **0 B / 15 GB**. Hai con số nói ngược nhau, và cả hai đều "đúng":

        tệp trên Drive : đã xoá  (wipe_queue làm việc này)
        bản ghi render_jobs : còn nguyên  (KHÔNG ai xoá)

    Dashboard đếm BẢN GHI chứ không đếm TỆP (§12.9 đã ghi đúng bẫy này từ 1/9 và vá bằng cách
    ĐỔI CHỖ ĐẾM — nguồn mới vẫn là bản ghi). Nên xoá sạch kho mà con số không đổi một đơn vị.

    ── VÌ SAO CHỈ XOÁ KHI CHỨNG MINH ĐƯỢC  (§15.6) ─────────────────────────────────────────
    Chỉ xoá bản ghi của những kho mà lượt này VỪA dọn sạch, và chỉ khi lượt ấy chạy thật. Kho
    không dọn được (token hỏng) thì tệp còn nguyên, xoá bản ghi của nó là làm mất dấu một video
    ĐANG SỐNG. `ten_kho` vì thế là danh sách kho ĐÃ dọn xong, không phải danh sách kho định dọn.

    Và như mọi lệnh dọn: **danh sách rỗng thì DỪNG**, vì "không kho nào vừa dọn" đọc ra y hệt
    "dọn tất".
    """
    if not ten_kho:
        print("   ⏸ không kho nào dọn xong — KHÔNG đụng bản ghi (rỗng ≠ tất cả)")
        return 0
    try:
        from firestore_state import client_render_jobs
        db = client_render_jobs()
    except Exception as e:
        print(f"   ⚠ không mở được Firestore ({type(e).__name__}) — bỏ qua dọn bản ghi")
        return 0
    owner = os.environ.get("OWNER_UID") or os.environ.get("MM0_OWNER") or ""
    if not owner:
        print("   ⚠ thiếu OWNER_UID — bỏ qua dọn bản ghi (không dám xoá toàn bảng)")
        return 0
    n = 0
    # TRẦN: mỗi lượt tối đa 800 bản ghi. Không trần thì một bảng lớn nuốt trọn hạn mức xoá,
    # và lượt sau không còn gì để chạy tiếp — thà dọn nhiều lượt còn hơn cạn giữa chừng (§13.7).
    q = (db.collection("render_jobs").where("owner", "==", owner)
           .where("status", "==", "done").limit(800))
    lo = db.batch() if not dry else None
    for d in q.stream():
        j = d.to_dict() or {}
        if (j.get("drive_account") or "") not in ten_kho:
            continue
        n += 1
        if not dry:
            lo.delete(d.reference)
            if n % 400 == 0:
                lo.commit()
                lo = db.batch()
    if not dry and n:
        lo.commit()
    print(f"   🧾 bản ghi render_jobs của {len(ten_kho)} kho vừa dọn: "
          f"{'sẽ xoá' if dry else 'đã xoá'} {n}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="chỉ đếm, không đụng file")
    ap.add_argument("--don-ban-ghi", action="store_true",
                    help="xoá luôn BẢN GHI render_jobs của những kho vừa dọn "
                         "(mặc định KHÔNG — xem `don_ban_ghi`)")
    ap.add_argument("--scope", choices=("store", "queue"), default="store",
                    help="store = mọi file dưới MM0-STORE (mặc định) · queue = chỉ _QUEUE")
    a = ap.parse_args()

    accs = storage.pool_accounts()
    # 23/8: kho khai trong storage.yaml (env) dùng refresh_token cũ, có cái đã bị thu hồi
    # (ADISONDURHAM -> invalid_grant) và vì trùng root nên bản Firestore MỚI bị loại ở bước dedupe
    # -> kho đó không dọn được. Giữ sẵn bản Firestore theo root để thử lại khi bản env chết.
    alt = {}
    try:
        for r in storage.firestore_pool_accounts():
            alt.setdefault(r["root"], r)
    except Exception as e:
        print(f"⚠️ không đọc được danh sách kho dự phòng: {str(e)[:70]}")
    print(f"🧹 Dọn [{a.scope}] trên {len(accs)} kho {'(CHẠY THỬ)' if a.dry_run else ''}", flush=True)
    tot_seen = tot_done = 0
    loi = []
    xong = set()          # kho DỌN XONG (không lỗi) — chỉ những kho này mới được xoá bản ghi
    for i, acc in enumerate(accs, 1):
        seen, done, err = _wipe_account(acc, a.dry_run, a.scope)
        if err and alt.get(acc.get("root")) and alt[acc["root"]].get("creds") != acc.get("creds"):
            print(f"      ↻ {acc.get('name')}: thử lại bằng thẻ kết nối mới từ Firestore")
            seen, done, err = _wipe_account(alt[acc["root"]], a.dry_run, a.scope)
        tot_seen += seen
        tot_done += done
        if err:
            loi.append(f"{acc.get('name')}: {err}")
        else:
            # DỌN XONG mới ghi vào danh sách. Kho lỗi (token hỏng) còn nguyên tệp, xoá bản ghi
            # của nó là làm mất dấu một video ĐANG SỐNG — §15.6: chỉ xoá khi chứng minh được.
            xong.add(acc.get("name") or "")
        if seen or err:
            print(f"  [{i}/{len(accs)}] {acc.get('name'):<22} thấy {seen:>4} · dọn {done:>4}"
                  + (f" · ⚠️ {err}" if err else ""), flush=True)
    print(f"\n📊 TỔNG: thấy {tot_seen} file · đã bỏ thùng rác {tot_done} · lỗi {len(loi)}")

    # ── XOÁ TỆP RỒI PHẢI XOÁ CẢ BẢN GHI  (4/9/2026) ────────────────────────────────────────
    # Anh: *"sao vẫn hiện 90 videos chưa dọn sạch"* trong khi ô hồ chứa cùng màn hình hiện
    # 0 B / 15 GB. Cả hai đều đúng: tệp đã xoá, bản ghi `render_jobs` còn nguyên — và dashboard
    # đếm BẢN GHI (§12.9). Nên xoá sạch kho mà con số không đổi một đơn vị.
    # Mặc định TẮT: lệnh này vốn chỉ hứa dọn tệp, và xoá bản ghi là mất lịch sử. `lam_lai.yml`
    # bật nó vì mục đích của luồng ấy đúng là làm lại từ đầu.
    if getattr(a, "don_ban_ghi", False):
        don_ban_ghi(xong - {""}, a.dry_run)

    # ══ ĐÓNG SỔ NGAY TRONG CÙNG THAO TÁC ═══════════════════════════════════════════════════
    # 31/8 — Anh báo "vẫn chưa dọn, kho còn 2067" trong khi kho đã sạch từ trước. Con số ấy là
    # sổ `kho_that`, do bộ lập kế hoạch ghi MỘT LẦN MỖI NGÀY sau khi đi hết các kho Drive. Dọn
    # xong giữa hai lần ghi thì sổ vẫn giữ số cũ — và dashboard dán nhãn "✓ kho thật" cho nó,
    # tức khẳng định con số vừa được đếm từ Drive. Nhãn ấy làm chuyện tệ hơn hẳn một số cũ:
    # không ai nghĩ tới việc nghi ngờ.
    # Luật: mọi thao tác làm THAY ĐỔI kho phải đóng sổ ngay trong chính thao tác ấy. Để lượt
    # chạy hôm sau ghi hộ là để hệ có hai sự thật, và cái sai lại là cái được hiển thị.
    if not a.dry_run and not loi:
        _con = max(0, tot_seen - tot_done)
        try:
            import json as _j, urllib.request as _u
            _k = os.environ.get("HOT_KEY", "")
            _o = os.environ.get("OWNER_UID", "")
            if _k and _o:
                _url = (os.environ.get("HOT_URL")
                        or "https://mm0-connect.adisondurham-ef1.workers.dev/api/hot")
                # Worker nhận {lenh, tham:{...}}, không phải {op, ...} — gửi sai khuôn thì
                # nó trả 400 mà không nói thiếu gì.
                _b = _j.dumps({"lenh": "kho_that_ghi",
                               "tham": {"owner": _o, "tong": _con,
                                        "luc": datetime.now(timezone.utc).isoformat()}}).encode()
                _r = _u.Request(_url, method="POST", data=_b,
                                headers={"content-type": "application/json", "x-hot-key": _k,
                                         # thiếu User-Agent thì Cloudflare chặn 1010, trả 403
                                         # y như sai khoá — mất cả buổi mới lần ra
                                         "user-agent": "MM0-Pipeline/1.0"})
                with _u.urlopen(_r, timeout=20) as _rr:
                    _rr.read()
                print(f"   📕 đã đóng sổ kho_that = {_con} (dashboard hết hiện số trước lúc dọn)")
            else:
                print("   ⚠️ thiếu HOT_KEY hoặc OWNER_UID — KHÔNG đóng được sổ. Dashboard sẽ "
                      "còn hiện số cũ tới lượt đếm sau; đó là hiển thị sai, không phải kho bẩn.")
        except Exception as e:
            print(f"   ⚠️ đóng sổ hụt ({type(e).__name__}) — dashboard còn hiện số cũ tới lượt "
                  f"đếm sau. Kho đã dọn xong, chỉ con số là chưa khớp.")
    for l in loi[:15]:
        print("   ⚠️", l)
    # Lỗi nào cũng phải hiện thành mã thoát khác 0 — KHÔNG được im lặng báo "sạch" như bản chạy
    # từ trình duyệt hôm 23/8 (429 bị đọc nhầm thành thư mục rỗng).
    return 1 if loi else 0


if __name__ == "__main__":
    sys.exit(main())
