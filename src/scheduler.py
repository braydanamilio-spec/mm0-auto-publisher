"""
scheduler.py — Bộ lập lịch: rải "publish_at" cho video theo template 7/30/90 ngày
và lọc ra video "đến giờ đăng" theo giới hạn an toàn.

Nguyên tắc:
  - Video nào đã có publish_at (do sidecar chỉ định) -> tôn trọng.
  - Video chưa có -> tự rải vào các "slot giờ vàng" trống, đúng nhịp cadence.
  - Không đăng quá trần/ngày, giãn cách tối thiểu giữa 2 lần đăng.
"""

from __future__ import annotations
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def assign_slots(
    items: list[dict],
    template: dict,
    tz_name: str,
    now: datetime,
    used_slots: set[str] | None = None,
) -> list[dict]:
    """
    Gán publish_at (ISO string, tz-aware) cho từng item CHƯA có publish_at.
    - items: mỗi dict tối thiểu có {"type": "long"|"short"}.
    - used_slots: tập ISO string các slot đã bị chiếm (từ video đã lên lịch trước).
    Trả về items (đã bổ sung item["publish_at"]).
    """
    # Ưu tiên múi giờ KHÁN GIẢ của template (vd America/New_York) để canh giờ vàng USA;
    # nếu template không khai báo thì dùng múi giờ chung.
    tz = ZoneInfo(template.get("timezone", tz_name))
    used = set(used_slots or set())
    cadence = template["cadence"]
    best = template["best_times"]

    # Đếm quota mỗi loại / mỗi tuần để biết rải bao nhiêu
    short_per_day = cadence.get("short_per_day", 0)
    long_per_week = cadence.get("long_per_week", 0)

    # con trỏ ngày bắt đầu = hôm nay (giờ địa phương)
    start = now.astimezone(tz)
    day0 = start.date()

    # Tách theo loại, giữ thứ tự
    # Không lên lịch video đang CHỜ DUYỆT (needs_review) hoặc đã đăng
    def _schedulable(i):
        return not i.get("publish_at") and i.get("status") not in ("needs_review", "posted", "uploading")
    shorts = [i for i in items if i.get("type") == "short" and _schedulable(i)]
    longs = [i for i in items if i.get("type") == "long" and _schedulable(i)]

    def next_free_slot(day_offset_start, times_list, per_period, period_days):
        """generator sinh các slot trống lần lượt."""
        d = day_offset_start
        while True:
            # số slot loại này trong 'period_days'
            for k in range(period_days):
                the_day = day0 + timedelta(days=d + k)
                # số bài loại này trong period = per_period; rải đều
                daily = max(1, per_period) if period_days == 1 else 1
                slots_today = times_list[:daily] if period_days == 1 else times_list[:1]
                # với long_per_week: chỉ đăng vào (per_period) ngày đầu mỗi tuần
                if period_days == 7 and k >= per_period:
                    continue
                for t in slots_today:
                    dt = datetime.combine(the_day, _parse_hhmm(t), tzinfo=tz)
                    if dt <= start:  # đã qua giờ -> bỏ
                        continue
                    iso = dt.isoformat()
                    if iso in used:
                        continue
                    used.add(iso)
                    yield iso
            d += period_days

    if shorts and short_per_day > 0:
        gen = next_free_slot(0, best.get("short", ["20:00"]), short_per_day, 1)
        for it in shorts:
            it["publish_at"] = next(gen)

    if longs and long_per_week > 0:
        gen = next_free_slot(0, best.get("long", ["20:30"]), long_per_week, 7)
        for it in longs:
            it["publish_at"] = next(gen)

    return items


def due_items(items: list[dict], now: datetime) -> list[dict]:
    """Lọc video đến giờ (publish_at <= now) và trạng thái cần xử lý."""
    ready = []
    for it in items:
        pa = it.get("publish_at")
        if not pa:
            continue
        try:
            dt = datetime.fromisoformat(pa)
        except ValueError:
            continue
        status = it.get("status", "pending")
        if status in ("posted", "uploading", "needs_review"):
            continue
        if dt <= now:
            ready.append(it)
    ready.sort(key=lambda x: x.get("publish_at", ""))
    return ready


def apply_limits(
    ready: list[dict],
    safety: dict,
    posted_today_yt: int,
    posted_today_fb: int,
    last_upload_at: datetime | None,
    now: datetime,
) -> list[dict]:
    """
    Cắt danh sách theo trần/ngày + giãn cách tối thiểu.
    Phần dư tự động để lại lần chạy sau (rollover).
    """
    yt_cap = safety.get("youtube_max_uploads_per_day", 6)
    fb_cap = safety.get("facebook_max_uploads_per_day", 10)
    gap = timedelta(minutes=safety.get("min_minutes_between_uploads", 30))

    if last_upload_at and (now - last_upload_at) < gap:
        return []  # chưa đủ giãn cách -> chờ lần chạy sau

    out = []
    yt_used, fb_used = posted_today_yt, posted_today_fb
    for it in ready:
        plats = it.get("platforms", ["youtube", "facebook"])
        want_yt = "youtube" in plats
        want_fb = "facebook" in plats
        if want_yt and yt_used >= yt_cap:
            continue
        if want_fb and fb_used >= fb_cap and not want_yt:
            continue
        out.append(it)
        if want_yt:
            yt_used += 1
        if want_fb:
            fb_used += 1
        # mỗi lần chạy chỉ đăng 1 video để giữ giãn cách chắc chắn
        break
    return out
