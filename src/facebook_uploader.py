"""
facebook_uploader.py — Đăng video lên Facebook Page bằng Graph API.

Cần:
  - PAGE_ID của Page.
  - PAGE ACCESS TOKEN (long-lived, không hết hạn nếu lấy đúng cách — xem SETUP.md).

Hai loại:
  - Video dài  -> POST /{page_id}/videos       (multipart, gọn nhẹ)
  - Reels(short)-> Reels API 3 pha (start -> upload -> finish)

Lưu ý: token Page long-lived KHÔNG hết hạn khi lấy từ System User (Business),
hoặc hết ~60 ngày nếu lấy từ user token — SETUP.md hướng dẫn cách lấy loại bền.
"""

from __future__ import annotations
import os
import time
import requests

GRAPH = "https://graph.facebook.com/v21.0"

# ── HẾT NHỊP FACEBOOK (24/8/2026, anh hỏi "đăng facebook có ảnh hưởng quota ko") ──────────────
# Facebook KHÔNG dùng chung hạn mức với YouTube (10.000 đơn vị/ngày/project Google) và cũng không
# đụng gì tới Firestore. Nhưng nó có TRẦN RIÊNG, và trước bản vá này hệ KHÔNG hề nhận ra:
#   • mã 4  — app request limit;  17 — user request limit;  32 — page request limit
#   • mã 613 — "calls to this api have exceeded the rate limit"
#   • Reels có trần bài/ngày riêng cho mỗi Page
# Hai hậu quả thật:
#   1. `upload()` thấy Reels lỗi liền QUAY VỀ đăng video thường -> gọi thêm một lượt nữa vào đúng
#      cái Page đang bị chặn, làm nặng thêm.
#   2. `publish_social` đếm 3 lần lỗi là dán nhãn `failed` -> **video bị bỏ luôn**, đúng cái bẫy đã
#      vá cho Instagram (`errs_ig_skip`) mà Facebook thì chưa.
# Nên: nhận diện hết-nhịp thành một loại lỗi RIÊNG để tầng trên hoãn sang lượt sau, không tính là
# thất bại, và không thử nền tảng thay thế.
MA_HET_NHIP = {4, 17, 32, 613, 80001, 80002, 80003, 80004}


class HetNhip(Exception):
    """FB/IG chặn vì chạm trần nhịp gọi hoặc trần bài/ngày — HOÃN, không phải hỏng."""


def _soi(r) -> None:
    """Đọc phản hồi Graph: hết nhịp thì ném HetNhip, lỗi khác thì để raise_for_status lo.

    Cũng in `X-App-Usage` khi đã dùng >80% để còn thấy trước lúc bị chặn (Graph trả tỉ lệ phần
    trăm trong header, không phải đợi lỗi mới biết)."""
    try:
        dung = r.headers.get("X-App-Usage") or r.headers.get("X-Business-Use-Case-Usage") or ""
        if dung:
            import json as _j
            x = _j.loads(dung)
            muc = x if isinstance(x, dict) else {}
            if isinstance(x, dict) and x and not any(k in x for k in ("call_count", "total_time")):
                muc = (list(x.values())[0] or [{}])[0]     # dạng theo từng business id
            cao = max(int(muc.get(k, 0) or 0) for k in ("call_count", "total_cputime", "total_time"))
            if cao >= 80:
                print(f"     ⚠️ FB đã dùng {cao}% trần nhịp — sắp bị chặn.")
    except Exception:
        pass
    if r.status_code < 400:
        return
    try:
        e = (r.json() or {}).get("error") or {}
    except Exception:
        return
    ma = int(e.get("code", 0) or 0)
    msg = str(e.get("message") or "")
    if ma in MA_HET_NHIP or "rate limit" in msg.lower() or "limit reached" in msg.lower():
        raise HetNhip(f"FB hết nhịp (mã {ma}): {msg[:120]}")


def post_comment(object_id: str, message: str, page_token: str) -> str | None:
    """Đăng 1 BÌNH LUẬN (chứa link tiếp thị) lên video/bài — dùng cho Reels vì caption không bấm link được.
    An toàn: lỗi -> trả None (không chặn luồng đăng)."""
    try:
        r = requests.post(f"{GRAPH}/{object_id}/comments",
                          data={"message": message, "access_token": page_token}, timeout=60)
        r.raise_for_status()
        return r.json().get("id")
    except Exception as e:
        print(f"     ⚠️ FB post_comment lỗi: {e}")
        return None


def wait_video_ready(video_id: str, page_token: str,
                     poll_seconds: int = 15, max_polls: int = 80) -> bool:
    """Chờ FB KÉO + XỬ LÝ xong video (khi đăng bằng file_url, FB tải ngầm).
    Trả True nếu 'ready'. Dùng để KHÔNG thu hồi link Drive trước khi FB kéo xong
    (video 2-5GB có thể mất nhiều phút). max_polls*poll_seconds ~ 20 phút.
    An toàn: lỗi mạng khi poll -> bỏ qua vòng đó, thử lại; hết giờ -> trả False."""
    for _ in range(max_polls):
        try:
            st = requests.get(f"{GRAPH}/{video_id}",
                              params={"fields": "status", "access_token": page_token},
                              timeout=60).json()
            vs = ((st.get("status") or {}).get("video_status")) or ""
            if vs == "ready":
                return True
            if vs == "error":
                return False
        except Exception:
            pass
        time.sleep(poll_seconds)
    return False


def set_thumbnail(video_id: str, thumb_path: str, page_token: str) -> bool:
    """Đặt ảnh bìa cho video ĐÃ đăng (Graph: POST /{video-id}/thumbnails, is_preferred=true).

    Dùng cho Reels — quy trình 3 pha của Reels không nhận ảnh bìa ngay lúc đăng, phải đặt sau.
    BEST-EFFORT: hỏng thì chỉ mất ảnh bìa (FB tự lấy 1 khung), TUYỆT ĐỐI không được ném lỗi ra
    ngoài — lúc gọi thì video ĐÃ đăng rồi, ném ra là caller tưởng thất bại và đăng lại lần nữa
    (đúng lỗi trùng bài đã sửa ở youtube_uploader)."""
    if not (video_id and thumb_path and os.path.exists(thumb_path)):
        return False
    try:
        with open(thumb_path, "rb") as t:
            r = requests.post(f"{GRAPH}/{video_id}/thumbnails",
                              data={"is_preferred": "true", "access_token": page_token},
                              files={"source": t}, timeout=120)
        if r.status_code >= 400:
            print(f"     ⚠️ FB không nhận ảnh bìa: {r.text[:120]}")
            return False
        return True
    except Exception as e:
        print(f"     ⚠️ FB đặt ảnh bìa lỗi: {str(e)[:100]}")
        return False


def upload_video(file_path: str, meta: dict, page_id: str, page_token: str,
                 video_url: str | None = None, thumb_path: str | None = None) -> dict:
    """Đăng video thường lên Page.
    - video_url có sẵn -> FB TỰ KÉO từ link (không tải-lại qua cron -> tối ưu cho video 2-5GB).
    - không -> upload bytes (stream, không nạp hết vào RAM)."""
    desc = f"{meta['title']}\n\n{meta['description']}"
    common = {"title": meta["title"][:255], "description": desc, "access_token": page_token}
    # 'thumb' gửi kèm ngay lệnh đăng -> 1 lần gọi, ảnh bìa có hiệu lực từ giây đầu (không như Reels
    # phải đặt sau). Mở file trong ngữ cảnh có điều kiện nên dùng ExitStack cho gọn.
    import contextlib
    with contextlib.ExitStack() as stack:
        extra = {}
        if thumb_path and os.path.exists(thumb_path):
            extra["thumb"] = stack.enter_context(open(thumb_path, "rb"))
        if video_url:
            r = requests.post(f"{GRAPH}/{page_id}/videos", data={**common, "file_url": video_url},
                              files=(extra or None), timeout=600)
        else:
            f = stack.enter_context(open(file_path, "rb"))
            r = requests.post(f"{GRAPH}/{page_id}/videos", data=common,
                              files={"source": f, **extra}, timeout=1800)
    _soi(r)
    r.raise_for_status()
    data = r.json()
    return {"id": data.get("id"), "url": f"https://facebook.com/{data.get('id')}"}


def upload_reel(file_path: str | None, meta: dict, page_id: str, page_token: str,
                video_url: str | None = None, thumb_path: str | None = None) -> dict:
    """
    Đăng Reels (video dọc/short) theo quy trình 3 pha của Graph API.
    - video_url có sẵn -> PHA 2 gửi header `file_url` để FB tự kéo (không tải-lại bytes).
    - không -> upload nhị phân từ file_path.
    """
    # PHA 1: start -> lấy video_id + upload_url
    start = requests.post(
        f"{GRAPH}/{page_id}/video_reels",
        data={"upload_phase": "start", "access_token": page_token},
        timeout=120,
    )
    _soi(start)
    start.raise_for_status()
    s = start.json()
    video_id = s["video_id"]
    upload_url = s["upload_url"]

    # PHA 2: upload — ưu tiên hosted file_url (FB tự kéo), fallback upload nhị phân
    if video_url:
        up = requests.post(
            upload_url,
            headers={"Authorization": f"OAuth {page_token}", "file_url": video_url},
            timeout=300,
        )
    else:
        size = os.path.getsize(file_path)
        with open(file_path, "rb") as f:
            up = requests.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {page_token}",
                    "offset": "0",
                    "file_size": str(size),
                },
                data=f,
                timeout=1800,
            )
    _soi(up)
    up.raise_for_status()

    # PHA 3: finish + publish
    desc = f"{meta['title']}\n\n{meta['description']}"
    finish = requests.post(
        f"{GRAPH}/{page_id}/video_reels",
        params={
            "access_token": page_token,
            "video_id": video_id,
            "upload_phase": "finish",
            "video_state": "PUBLISHED",
            "description": desc[:2200],
        },
        timeout=120,
    )
    _soi(finish)
    finish.raise_for_status()
    # Ảnh bìa: Reels KHÔNG nhận thumb trong 3 pha -> đặt sau khi đã publish. Best-effort, không
    # bao giờ ném lỗi ra ngoài (video đã đăng rồi; ném ra là caller tưởng hỏng và đăng lại).
    if thumb_path:
        set_thumbnail(video_id, thumb_path, page_token)
    return {"id": video_id, "url": f"https://facebook.com/reel/{video_id}"}


def upload(file_path: str | None, meta: dict, page_id: str, page_token: str,
           video_url: str | None = None, thumb_path: str | None = None) -> dict:
    """thumb_path: ảnh bìa DÙNG CHUNG với YouTube (cùng tấm thumbnail của chính video đó) -> bài
    trên FB và YouTube nhìn đồng bộ. Thiếu ảnh thì FB tự lấy 1 khung như trước, không lỗi."""
    # Short -> ưu tiên Reels (đúng định dạng, phân phối tốt hơn); long -> video Page thường.
    if meta.get("type") == "short":
        try:
            return upload_reel(file_path, meta, page_id, page_token, video_url=video_url,
                               thumb_path=thumb_path)
        except HetNhip:
            raise            # đang bị chặn: đăng dạng khác cũng chặn, mà còn tốn thêm 1 lượt gọi
        except Exception as e:
            # An toàn: nếu Reels API lỗi (vd không hỗ trợ hosted url) -> quay về video thường (vẫn đăng được).
            print(f"     ⚠️ Reels lỗi ({e}) -> đăng dạng video thường.")
            return upload_video(file_path, meta, page_id, page_token, video_url=video_url,
                                thumb_path=thumb_path)
    return upload_video(file_path, meta, page_id, page_token, video_url=video_url,
                        thumb_path=thumb_path)
