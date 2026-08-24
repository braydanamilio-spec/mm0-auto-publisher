"""
auto_enqueue.py — TỰ ĐẨY video render xong vào yt_queue để ĐĂNG TỰ ĐỘNG, RẢI LỊCH THEO TEMPLATE.

AN TOÀN:
  - Mặc định TẮT. Chỉ chạy kênh có cờ auto_publish=true (settings/overrides__<uid>.auto_publish[<TÊN kênh>]).
  - Kênh phải ĐÃ kết nối YouTube (connections/<owner>__<kênh>__youtube có refresh_token) -> chưa kết nối thì bỏ qua.
  - Dedup theo drive_file_id + cờ 'queued' trên render_jobs -> KHÔNG đăng trùng.

LỊCH (chống chồng chéo ngày):
  - Mỗi kênh gắn 1 TEMPLATE (posting_templates.yaml) quy định MIX/ngày: short_per_day + long (~long_per_week).
  - Rải video vào ngày TRỐNG: ngày nào đã đủ mix template thì NHẢY sang ngày sau. Không dồn quá mix.
  - Đăng đúng GIỜ VÀNG US (best_times theo timezone template -> quy đổi UTC).
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from firestore_state import State, client_render_jobs
import quota_guard as _QG

try:
    import yaml
except Exception:
    yaml = None
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

SCAN_LIMIT = 40
DEFAULT_TMPL = os.environ.get("POSTING_TEMPLATE", "balanced_1long_3short")


def _owner() -> str:
    return os.environ.get("OWNER_UID", "") or os.environ.get("OWNER", "")


def _load_templates() -> dict:
    if not yaml:
        return {}
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "posting_templates.yaml")
    try:
        return (yaml.safe_load(open(p)) or {}).get("templates", {}) or {}
    except Exception:
        return {}


def _tz(name: str):
    if ZoneInfo:
        try:
            return ZoneInfo(name or "America/New_York")
        except Exception:
            pass
    return timezone(timedelta(hours=-4))   # fallback ~EDT nếu thiếu tzdata


def run(dry_run: bool = False):
    state = State()
    q_db = state.pub              # yt_queue -> Project C (OWNED)
    rj = client_render_jobs()     # render_jobs -> Project B (render)
    owner = _owner()
    if not owner:
        return
    ov = state.get_doc("settings", "overrides__" + owner) or state.get_doc("settings", "overrides") or {}
    auto = ov.get("auto_publish") or {}
    on_channels = {k for k, v in auto.items() if v}
    if not on_channels:
        return

    def yt_ok(ch: str) -> bool:
        c = state.get_doc("connections", f"{owner}__{ch}__youtube")
        return bool(c and c.get("refresh_token"))
    ready = {ch for ch in on_channels if yt_ok(ch)}
    if not ready:
        print("  ⏸ auto-enqueue: có kênh bật nhưng CHƯA kết nối YouTube -> bỏ qua."); return

    templates = _load_templates()
    ov_ch = ov.get("channels") or {}                        # {kênh: tên_template}

    def tmpl_of(ch: str) -> dict:
        name = ov_ch.get(ch) or DEFAULT_TMPL
        t = templates.get(name) or templates.get("balanced_1long_3short") or {}
        cad = t.get("cadence", {}) or {}; bt = t.get("best_times", {}) or {}
        return {"tz": _tz(t.get("timezone", "America/New_York")),
                "short_pd": int(cad.get("short_per_day", 3) or 3),
                "long_pw": int(cad.get("long_per_week", 7) or 7),
                "t_short": [str(x) for x in (bt.get("short") or ["12:00", "17:00", "20:00"])],
                "t_long": [str(x) for x in (bt.get("long") or ["15:00"])]}

    tcache = {ch: tmpl_of(ch) for ch in ready}

    # 1 lần đọc yt_queue: dedup + dựng LỊCH đã có (theo NGÀY địa phương template + theo LOẠI + tuần cho long)
    queued_ids = set()
    day_cnt: dict[str, dict[str, dict[str, int]]] = {}      # {kênh: {ngày-ET: {"short":n,"long":n}}}
    wk_long: dict[str, dict[str, int]] = {}                 # {kênh: {tuần-ISO: số long}}
    try:
        for d in q_db.collection("yt_queue").where("owner", "==", owner).stream():
            data = d.to_dict()
            fid = data.get("drive_file_id")
            if fid:
                queued_ids.add(fid)
            if data.get("status") in ("pending", "processing", "scheduled", "posted"):
                pa, ch2, ty = data.get("publish_at"), data.get("channel"), (data.get("type") or "short")
                if pa and ch2 in tcache:
                    try:
                        loc = datetime.fromisoformat(str(pa).replace("Z", "+00:00")).astimezone(tcache[ch2]["tz"])
                        diso = loc.date().isoformat()
                        dd = day_cnt.setdefault(ch2, {}).setdefault(diso, {"short": 0, "long": 0})
                        dd[ty] = dd.get(ty, 0) + 1
                        if ty == "long":
                            wiso = f"{loc.isocalendar().year}-W{loc.isocalendar().week:02d}"
                            wk_long.setdefault(ch2, {})[wiso] = wk_long.setdefault(ch2, {}).get(wiso, 0) + 1
                    except Exception:
                        pass
    except Exception as e:
        print(f"  ⚠️ auto-enqueue đọc yt_queue lỗi: {e}")

    now = datetime.now(timezone.utc)

    def next_slot(ch: str, vtype: str) -> str:
        """Ngày TRỐNG kế tiếp theo MIX template: short<short_pd/ngày; long<=1/ngày & <=long_pw/tuần. Giờ = best_times US."""
        t = tcache[ch]; tz = t["tz"]
        times = t["t_short"] if vtype == "short" else t["t_long"]
        cap_day = t["short_pd"] if vtype == "short" else 1
        day = now.astimezone(tz).date()
        for _ in range(400):
            diso = day.isoformat()
            dd = day_cnt.setdefault(ch, {}).setdefault(diso, {"short": 0, "long": 0})
            ok = dd.get(vtype, 0) < cap_day
            wiso = None
            if vtype == "long":
                ic = day.isocalendar(); wiso = f"{ic[0]}-W{ic[1]:02d}"
                if wk_long.setdefault(ch, {}).get(wiso, 0) >= t["long_pw"]:
                    ok = False                              # đủ long/tuần -> sang tuần sau
            if ok:
                idx = dd.get(vtype, 0)
                hhmm = times[idx % len(times)]
                try:
                    hh, mm = [int(x) for x in hhmm.split(":")[:2]]
                except Exception:
                    hh, mm = (15, 0) if vtype == "long" else (17, 0)
                loc = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
                utc = loc.astimezone(timezone.utc)
                if utc <= now:
                    utc = now + timedelta(minutes=5)
                dd[vtype] = idx + 1
                if vtype == "long" and wiso:
                    wk_long[ch][wiso] = wk_long[ch].get(wiso, 0) + 1
                return utc.isoformat()
            day = day + timedelta(days=1)
        return (now + timedelta(minutes=5)).isoformat()

    added = 0

    # ================== 24/8: TÌM VIDEO MỚI BẰNG CỜ, KHÔNG QUÉT MÙ ==================
    # Vì sao đổi: vòng lặp cũ quét SCAN_LIMIT(40) doc 'done' mới nhất của TỪNG kênh, MỖI lượt cron.
    #   55 kênh × 40 doc × 48 lượt/ngày ≈ 105.000 lượt đọc project B — một mình nó gấp đôi trần
    #   50K/ngày, và đó là lý do B cạn hạn mức. Mà 39/40 doc quét được đều đã xếp lịch từ lâu:
    #   ta trả tiền để đọc đi đọc lại đúng những doc không dùng tới.
    # Nay: firestore_bridge.update_job đóng `queued=False` ngay khi job sang 'done' -> ở đây chỉ cần
    #   MỘT truy vấn `status==done AND queued==False`, trả về ĐÚNG số video mới (thường 0-20).
    #   Truy vấn toàn bằng-nhau nên Firestore ghép được từ chỉ mục đơn — KHÔNG cần tạo composite index.
    # Doc 'done' tạo TRƯỚC hôm nay chưa có trường `queued` nên truy vấn nhanh không thấy -> giữ lối
    #   quét cũ làm QUÉT VÉT, chạy tối đa 6 giờ/lần. Vòng lặp bên dưới luôn đóng dấu queued=True cho
    #   mọi doc nó chạm tới, nên mỗi doc cũ chỉ phải quét đúng MỘT lần trong đời rồi vào lối nhanh.
    FAST_LIMIT = 300
    SWEEP_EVERY_H = 6
    docs_all, fast_ok = [], False
    try:
        docs_all = list(rj.collection("render_jobs").where("owner", "==", owner)
                        .where("status", "==", "done").where("queued", "==", False)
                        .limit(FAST_LIMIT).stream())
        fast_ok = True
        _QG.dem("B", r=max(1, len(docs_all)))
        print(f"  ⚡ auto-enqueue lối nhanh: {len(docs_all)} video mới (bấy nhiêu lượt đọc, thay vì ~{len(ready)*SCAN_LIMIT})")
    except Exception as e:
        print(f"  ⚠️ lối nhanh hỏng ({str(e)[:70]}) -> quét kiểu cũ lượt này")

    need_sweep = not fast_ok
    _da_tung_quet = False
    sweep_ref = q_db.collection("counters").document("__enqueue_sweep__")
    if fast_ok:
        try:
            sd = sweep_ref.get()
            last = (sd.to_dict() or {}).get("at", "") if sd.exists else ""
            _da_tung_quet = bool(last)
            if not last:
                need_sweep = True
            else:
                gio = (now - datetime.fromisoformat(str(last).replace("Z", "+00:00"))).total_seconds() / 3600
                need_sweep = gio >= SWEEP_EVERY_H
        except Exception:
            need_sweep = True          # không đọc được mốc -> quét vét cho chắc, thà tốn còn hơn sót

    # PHANH QUOTA: quét vét là việc PHỤ (chỉ bắt doc cũ trước ngày có cờ). Hạn mức B đã đi sâu thì
    # bỏ lượt quét này — thà chậm bắt vài doc cũ còn hơn ăn nốt phần hạn mức mà việc ĐĂNG BÀI cần.
    #
    # NGOẠI TRỪ LẦN QUÉT ĐẦU TIÊN (24/8 — lỗi tiềm ẩn của chính bản vá này):
    # mọi video 'done' render TRƯỚC hôm nay đều KHÔNG có trường `queued`, nên lối nhanh không thấy
    # chúng; chỉ quét vét mới bắt được. Nếu lần quét đầu bị phanh chặn, mốc `at` không bao giờ được
    # ghi -> lượt sau lại "chưa từng quét" -> lại bị chặn -> **số video cũ đó KHÔNG BAO GIỜ ĐƯỢC ĐĂNG**.
    # Phanh sinh ra để hoãn việc phụ, không phải để bỏ rơi video. Lần đầu tính là THIẾT YẾU.
    _lan_dau = need_sweep and fast_ok and not _da_tung_quet
    if _lan_dau:
        print("  🔑 quét vét LẦN ĐẦU (bắt video cũ chưa có cờ queued) — tính là việc thiết yếu, không hoãn.")
    if need_sweep and fast_ok and not _lan_dau and not _QG.du_suc("B", can_doc=len(ready) * SCAN_LIMIT):
        print(f"  ⏸ hoãn quét vét — {_QG.bao_cao('B')} (để dành hạn mức cho việc đăng)")
        need_sweep = False

    if need_sweep:
        print(f"  🧹 quét vét (6h/lần, bắt doc cũ chưa có cờ queued): {len(ready)} kênh")
        if not dry_run:
            try:
                sweep_ref.set({"at": now.isoformat()}, merge=True)
            except Exception:
                pass
        docs_all = docs_all + _quet_vet(rj, owner, ready)

    by_ch: dict[str, list] = {}
    for d in docs_all:
        c = (d.to_dict() or {}).get("channel") or ""
        if c in ready:
            by_ch.setdefault(c, []).append(d)

    for ch, docs in by_ch.items():
        for d in docs:
            j = d.to_dict()
            if j.get("queued"):
                continue
            fid = j.get("drive_id")
            if not fid or fid in queued_ids:
                if fid and not dry_run:
                    rj.collection("render_jobs").document(d.id).set({"queued": True}, merge=True)
                continue
            vtype = "long" if (j.get("type") == "long") else "short"
            when = next_slot(ch, vtype)
            item = {"owner": owner, "channel": ch, "drive_file_id": fid,
                    "drive_account": j.get("drive_account", ""), "type": vtype,
                    "title": j.get("title", ""),
                    "drive_name": j.get("drive_name") or ((j.get("title") or fid) + ".mp4"),
                    "description": j.get("description", ""), "hashtags": j.get("hashtags") or [],
                    "tags": j.get("tags") or [], "status": "pending", "attempts": 0,
                    "created_at": now.isoformat(), "publish_at": when}
            if dry_run:
                print(f"  (dry) {ch} [{vtype}] -> {when[:16]} · {(item['title'] or fid)[:38]}"); added += 1; continue
            q_db.collection("yt_queue").add(item)
            rj.collection("render_jobs").document(d.id).set({"queued": True}, merge=True)
            queued_ids.add(fid); added += 1

    print(f"✔ auto-enqueue: xếp lịch {added} video (theo template)." if added else "✔ auto-enqueue: không có video mới.")
    return added


def _quet_vet(rj, owner, ready):
    """LỐI CŨ giữ lại làm quét vét: 40 doc 'done' mới nhất mỗi kênh. Đắt (≈55×40 lượt đọc) nên chỉ
    chạy 6 giờ/lần, đủ để bắt các doc tạo trước ngày có cờ `queued`."""
    out = []
    for ch in ready:
        try:
            q = (rj.collection("render_jobs").where("owner", "==", owner)
                 .where("channel", "==", ch).where("status", "==", "done"))
            try:
                from google.cloud.firestore_v1 import Query
                # 20/8: DESCENDING (mới nhất trước), KHÔNG phải ASCENDING như trước.
                # render_jobs 'done' KHÔNG BAO GIỜ bị xoá (chỉ được đánh dấu queued=True) -> mỗi kênh
                # tích luỹ NGÀY CÀNG NHIỀU doc 'done'. Với ASCENDING + limit(SCAN_LIMIT), query này
                # LUÔN trả về CÙNG SCAN_LIMIT DOC CŨ NHẤT, không đổi, mỗi lần chạy — một khi kênh đã có
                # > SCAN_LIMIT doc 'done' (rất nhanh với render factory 10 luồng/10 phút), toàn bộ
                # SCAN_LIMIT doc trả về đều đã queued=True từ lâu -> vòng lặp bên dưới skip hết, video
                # MỚI render xong (luôn nằm ở cuối, ngoài giới hạn SCAN_LIMIT) KHÔNG BAO GIỜ được xét tới
                # -> auto-enqueue coi như CHẾT LẶNG LẼ cho kênh đó (không lỗi, không log rõ ràng).
                docs = list(q.order_by("created_at", direction=Query.DESCENDING).limit(SCAN_LIMIT).stream())
            except Exception:
                docs = list(q.limit(SCAN_LIMIT).stream())
            out.extend(docs)
            _QG.dem("B", r=max(1, len(docs)))
        except Exception as e:
            print(f"  ⚠️ auto-enqueue đọc render_jobs {ch} lỗi: {e}"); continue
    return out



def _run_guarded(fn):
    """Hết hạn mức Firestore là chuyện TẠM THỜI (tự hồi khi reset) — bắt riêng 429 để thoát 0
    thay vì crash đỏ mỗi lần cron chạy trong khung cạn quota. Lỗi KHÁC vẫn ném lên. (Cùng khuôn
    main.py/publish_social.py — 21/8 stats còn sót nên đỏ oan lúc 19:05Z.)"""
    try:
        fn()
    except Exception as e:
        if type(e).__name__ == "ResourceExhausted" or "429 Quota exceeded" in str(e):
            print("\n⏳ Firestore hết hạn mức -> bỏ lượt này, cron sau tự chạy lại.")
            try:
                from firestore_state import chan_doan_429
                print(chan_doan_429())     # 24/8: nói RÕ project nào cạn, khỏi đoán
            except Exception:
                pass
            raise SystemExit(0)
        raise

if __name__ == "__main__":
    _run_guarded(lambda: run(dry_run="--dry-run" in sys.argv))
