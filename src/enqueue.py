"""
enqueue.py — CẦU NỐI giữa dây chuyền render và hệ thống đăng.

Sau khi render xong 1 video, gọi lệnh này => nó tự:
  1. Dựng title / description / hashtag / tags CHUẨN theo branding của kênh.
  2. Ghi sidecar .json.
  3. Đẩy video + sidecar lên đúng Drive `_QUEUE/long` hoặc `_QUEUE/short`.
=> Từ đó GitHub Actions tự lo phần đăng.

Ví dụ (gọi cuối pipeline render):
    export GOOGLE_APPLICATION_CREDENTIALS=/duong/dan/sa.json
    python src/enqueue.py \
        --channel BROKE --type short \
        --video out/broke_ep12.mp4 \
        --topic "How I Went From Broke To $10k/Month" \
        --hashtags "#money #sidehustle" \
        --publish-at 2026-08-16T20:00:00+07:00      # (tùy chọn; bỏ trống -> auto theo template)

Gọi từ Python:
    from enqueue import enqueue
    enqueue(channel="BROKE", video="out/x.mp4", vtype="short", topic="...")
"""

from __future__ import annotations
import argparse
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(__file__))
import metadata as M
from drive_client import Drive

CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "channels.yaml")


def _load_channels() -> dict:
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def enqueue(channel: str, video: str, vtype: str, topic: str,
            title: str | None = None, description: str | None = None,
            hashtags: list[str] | None = None, tags: list[str] | None = None,
            platforms: list[str] | None = None, publish_at: str | None = None,
            thumbnail: str | None = None, pool="auto",
            subtitle: str | None = None, subtitle_lang: str | None = None,
            playlist: str | None = None, dedup: bool = True,
            owner: str | None = None, script: str | None = None) -> dict:
    cfg = _load_channels()
    ch = cfg["channels"].get(channel)
    if not ch:
        raise SystemExit(f"❌ Không có kênh '{channel}' trong channels.yaml")

    # Multi-tenant: chưa biết owner -> tra từ kết nối YouTube của kênh (để dashboard hiện đúng user)
    if owner is None:
        try:
            from firestore_state import State as _S
            for c in _S().list_connections("youtube"):
                if c.get("channel") == channel and c.get("owner"):
                    owner = c["owner"]
                    break
        except Exception:
            pass

    # ---- CHỐNG TRÙNG: tra vân tay nội dung trong sổ cái Firestore ----
    # (chống kéo lại cả folder / trùng file / đổi máy). Lỗi Firestore -> bỏ qua kiểm tra.
    sig = None
    state = None
    if dedup:
        try:
            from dedup import content_signature
            from firestore_state import State
            sig = content_signature(video)
            state = State()
            existed = state.sig_exists(channel, sig, owner=owner)
            if existed:
                print(f"⏭  Trùng (đã có trong kênh {channel}) -> BỎ QUA: {os.path.basename(video)}")
                return {"duplicate": True, "id": existed, "sig": sig}
        except Exception as e:
            print(f"   ⚠️  Không tra được sổ chống trùng ({e}) — vẫn upload.")
            state = None

    # Dựng metadata chuẩn từ branding kênh
    raw = {"topic": topic, "type": vtype}
    for k, v in (("title", title), ("description", description),
                 ("hashtags", hashtags), ("tags", tags), ("platforms", platforms)):
        if v:
            raw[k] = v
    meta = M.build_metadata(raw, ch["branding"])
    warns = M.lint(meta)

    sidecar = {
        "channel": channel,          # để hệ thống định tuyến đúng kênh khi dùng pool
        "topic": topic,
        "type": meta["type"],
        "title": meta["title"],
        "description": meta["description"],
        "hashtags": meta["hashtags"],
        "tags": meta["tags"],
        "platforms": meta["platforms"],
    }
    if publish_at:
        sidecar["publish_at"] = publish_at
    if script:
        # KỊCH BẢN ĐI CÙNG VIDEO, KHÔNG CHỈ NẰM Ở FIRESTORE (25/8/2026).
        # Muốn render lại một video thì cần đúng kịch bản đã sinh ra nó. Trước đây kịch bản CHỈ nằm
        # trong `render_jobs` trên Firestore ⇒ hôm nào Firestore cạn hạn mức là mất luôn đường
        # resume, và hệ phải gọi AI viết lại một bài ĐÃ CÓ (xem luật 7.cp — dòng "♻️ Dùng lại kịch
        # bản đã lưu" chưa từng xuất hiện lần nào).
        # Drive thì luôn đọc được (có gương + lớp cứu KV), lại là nơi chính video đang nằm. Nhét vào
        # sidecar là kịch bản đi CÙNG video, không thể lạc nhau, và không tốn thêm một lượt ghi nào.
        sidecar["script"] = script[:400_000]
    if playlist:
        sidecar["playlist"] = playlist
    vbase = os.path.basename(video).rsplit(".", 1)[0]
    if thumbnail and os.path.exists(thumbnail):
        ext = os.path.splitext(thumbnail)[1] or ".jpg"
        sidecar["thumbnail"] = f"{vbase}{ext}"
    if subtitle and os.path.exists(subtitle):
        sext = os.path.splitext(subtitle)[1] or ".srt"
        lang = subtitle_lang or ch["youtube"].get("default_language", "en")
        sidecar["captions"] = [{"file": f"{vbase}{sext}", "language": lang, "name": lang.upper()}]

    # ---- Chọn đích + UPLOAD (kho pool: tự chia acc theo dung lượng; lỗi/đầy -> nhảy acc kế) ----
    import storage as ST
    use_pool = (pool is True) or (pool == "auto" and bool(ST.pool_accounts()))
    if use_pool:
        need = os.path.getsize(video) + 60 * 1024 * 1024   # +60MB đệm (sidecar/thumb/sub/overhead)
        ranked = ST.ranked_accounts(need, owner=owner, seed=channel)   # seed=kênh -> rải đều kho, song song không dồn 1 kho
        if not ranked:
            raise SystemExit(f"❌ Không tài khoản kho nào đủ chỗ (~{need/1e9:.2f} GB). "
                             f"Kết nối thêm Google Drive hoặc dọn dẹp.")
        targets = [(ST.account_drive(a), a["root"], f"kho:{a['name']}") for a, _ in ranked]
    else:
        root = os.environ.get(ch["drive_folder_id_env"])
        if not root:
            # PHÂN BIỆT "KHÔNG CÓ" VỚI "KHÔNG ĐỌC ĐƯỢC"  (1/9/2026)
            # Câu cũ nói thẳng "chưa kết nối tài khoản kho nào". Hôm nay nó SAI: có ~70 kho đã
            # kết nối, chỉ là Firestore cạn hạn mức nên `pool_accounts()` đọc rỗng. Hai tình
            # huống ấy dẫn tới hai hành động hoàn toàn khác nhau — một cái phải đi nối kho, một
            # cái phải chờ hạn mức — và câu sai làm mất nửa giờ đi kiểm cấu hình.
            _co_ho = False
            try:
                _co_ho = bool(ST.load_config().get("pool"))
            except Exception:
                pass
            _goi_y = ("Firestore/KV/D1 đều không trả về danh sách kho — nhiều khả năng CẠN HẠN MỨC "
                      "đọc, không phải chưa nối kho. Kiểm sổ ngân sách rồi chạy lại."
                      if not _co_ho else
                      f"chưa set {ch['drive_folder_id_env']}.")
            raise SystemExit(f"❌ Không lấy được danh sách kho Drive. {_goi_y}")
        targets = [(Drive(), root, "kênh")]

    created, where, last_err = None, None, None
    for drive, root, wh in targets:
        if use_pool:
            ST.reserve(root, need)   # GIỮ CHỖ TRƯỚC (ước lượng size thật + đệm) -> luồng khác thấy ngay, không cùng nhét 1 kho gần đầy
        try:
            created = drive.upload_to_queue(root, video, meta["type"], sidecar,
                                            thumbnail_path=thumbnail, subtitle_path=subtitle)
            where = wh
            if created is not None:
                created["account"] = wh[4:] if wh.startswith("kho:") else ""   # tên kho -> để Worker stream/preview đúng tài khoản
            break                                        # thành công -> GIỮ reservation (TTL 30' tự dọn khi usage() đã cập nhật)
        except Exception as e:
            if use_pool:
                ST.release(root, need)   # lỗi/đầy -> TRẢ CHỖ, nhảy kho kế
            last_err = e
            print(f"   ⚠️  Upload vào {wh} lỗi: {e}" + (" → thử tài khoản kế" if len(targets) > 1 else ""))
    if not created:
        raise SystemExit(f"❌ Upload thất bại toàn bộ tài khoản kho: {last_err}")

    # Ghi vân tay vào sổ cái NGAY -> lần kéo folder sau sẽ nhận ra & bỏ qua
    if sig and state:
        try:
            rec = {"channel": channel, "sig": sig, "drive_name": os.path.basename(video),
                   "type": meta["type"], "source_status": "queued"}
            if owner:
                rec["owner"] = owner
            state.upsert_video(created["id"], rec)
        except Exception as e:
            print(f"   ⚠️  Ghi sổ chống trùng lỗi ({e}).")

    # ── GHI BẢN GHI VÀO D1 — KHO MÀ DASHBOARD THẬT SỰ ĐỌC  (2/9/2026) ──────────────────────
    # Video lên Drive rồi mà màn hình vẫn hiện "Video trong kho: 0". Vì sổ ở trên ghi vào
    # **Firestore**, còn `apiHotStat` (thứ dashboard gọi) đếm bảng `render_job` bên **D1** —
    # hai kho song song, và mắt xích này chỉ chạm một. Đo thật: 18/18 luồng đẩy "2/2 video vào
    # hàng đợi đăng", `hot-jobs` trả 0 dòng, dashboard 0.
    #
    # Và Firestore là thứ hay cạn nhất: đúng hôm cạn thì cả sổ Firestore lẫn màn hình đều mù,
    # trong khi video vẫn nằm ngon trên Drive. `hot_db.ghi_job` đi qua Worker, KHÔNG đụng
    # Firestore, nên đường này sống cả khi kho kia chết.
    #
    # Không để trong cùng `try` với sổ Firestore: một cái hỏng không được kéo cái kia theo.
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(
            _os.path.dirname(_os.path.abspath(__file__)))), "render-pipeline"))
        import hot_db as _H
        if _H.bat_ghi():
            from datetime import datetime as _dt, timezone as _tz
            # GỌI THẲNG `goi("ghi_job")`, KHÔNG dùng `ghi_job()`.
            # `hot_db.ghi_job` GOM ĐỆM rồi xả theo lô — hợp lý cho tiến trình chạy dài, nhưng
            # `enqueue.py` là tiến trình con sống vài giây rồi thoát, nên đệm chưa xả là bản ghi
            # MẤT. Đo thật: luồng 18 đẩy "2/2 video vào hàng đợi", Drive nhận, mà `hot-jobs` vẫn
            # 0 dòng và không một dòng lỗi nào — vì không có lỗi, chỉ có đệm chưa xả.
            # `bao_chay.py` gọi thẳng và ô "Đang chạy" chạy đúng; làm y như vậy.
            # ── DÙNG `ghi_job()` RỒI `xa_het()`, KHÔNG TỰ GỌI LỆNH LẠ  (2/9/2026) ───────────
            # Bản trước gọi thẳng `goi("ghi_job", {...})` để né bộ đệm. Đo trên lượt 33631376874:
            # `⚠️ D1 hụt (1 lần): HTTP Error 500` — vì **`ghi_job` không có trong danh sách lệnh
            # mà `hot_db` dùng**; nó chỉ có `ghi_job_loat` (một LÔ, không phải một dòng). Tôi né
            # được bộ đệm nhưng đổi lấy một tên lệnh Worker không nhận, nên bản ghi mất sạch —
            # và đó chính là lý do màn hình vẫn hiện 0 dù video đã lên Drive thật.
            #
            # `hot_db` đã có sẵn đúng cặp cần dùng: `ghi_job()` xếp vào đệm, `xa_het()` xả ngay.
            # `xa_het` viết ra chính xác cho tình huống này — chú thích của nó: *"Gọi cuối luồng —
            # thiếu bước này là MẤT các lượt ghi cuối."* Đọc client cũ trước khi viết lối gọi mới.
            _H.ghi_job(owner=owner,
                       jid=f"gt-{channel.lower()}-{meta['type']}-{os.path.basename(video)}",
                       channel=channel.upper(), vtype=meta["type"], status="done",
                       step="đã lên kho", title=meta.get("title"),
                       drive_id=created["id"], queued=False,
                       at=_dt.now(_tz.utc).isoformat())
            _n = _H.xa_het()
            print(f"   🗂 ghi bản ghi D1: {'ok' if _n else 'HỤT — bản ghi không vào D1'}")
    except Exception as e:
        print(f"   ⚠️  Ghi bản ghi D1 lỗi ({str(e)[:80]}) — video vẫn ở Drive, chỉ số đếm chậm.")

    print(f"✅ Đã đưa vào hàng đợi [{where}] kênh {channel} [{meta['type']}]: {meta['title']!r}")
    print(f"   Drive file id: {created['id']}")
    if warns:
        print("   ⚠️  " + " | ".join(warns))
    return created


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--video", required=True, help="Đường dẫn file video local.")
    ap.add_argument("--type", dest="vtype", choices=["long", "short"], required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--title")
    ap.add_argument("--desc", dest="description")
    ap.add_argument("--hashtags", help='VD: "#money #finance"')
    ap.add_argument("--tags", help="VD: keyword1,keyword2")
    ap.add_argument("--platforms", help="VD: youtube,facebook")
    ap.add_argument("--publish-at", dest="publish_at", help="ISO. Bỏ trống = auto theo template.")
    ap.add_argument("--thumbnail", help="Đường dẫn ảnh thumbnail (long-form nên có).")
    ap.add_argument("--pool", dest="pool", action="store_const", const=True, default="auto",
                    help="Ép đẩy vào KHO pool (mặc định: auto — dùng kho nếu có kết nối).")
    ap.add_argument("--no-pool", dest="pool", action="store_const", const=False,
                    help="Ép dùng folder riêng của kênh (không dùng kho pool).")
    ap.add_argument("--subtitle", help="File phụ đề .srt/.vtt đi kèm (tự upload lên YouTube).")
    ap.add_argument("--subtitle-lang", dest="subtitle_lang", help="Mã ngôn ngữ phụ đề, vd en, vi.")
    ap.add_argument("--playlist", help="Tên playlist (tự tạo nếu chưa có).")
    a = ap.parse_args()

    enqueue(
        channel=a.channel, video=a.video, vtype=a.vtype, topic=a.topic,
        title=a.title, description=a.description,
        hashtags=a.hashtags.split() if a.hashtags else None,
        tags=a.tags.split(",") if a.tags else None,
        platforms=a.platforms.split(",") if a.platforms else None,
        publish_at=a.publish_at, thumbnail=a.thumbnail, pool=a.pool,
        subtitle=a.subtitle, subtitle_lang=a.subtitle_lang, playlist=a.playlist,
    )


if __name__ == "__main__":
    main()
