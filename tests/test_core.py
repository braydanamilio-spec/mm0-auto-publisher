"""
test_core.py — Kiểm thử logic THUẦN (không cần mạng / secrets).
Chạy: python tests/test_core.py   (thoát code != 0 nếu có test fail)
"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import metadata as M
import scheduler as S

fails = []
def check(name, cond):
    print(("  ✅ " if cond else "  ❌ ") + name)
    if not cond:
        fails.append(name)

BRANDING = {
    "hashtags": ["#money", "#finance"],
    "long_title_template": "{topic}",
    "short_title_template": "{topic} #shorts",
    "cta": "Subscribe!",
    "disclaimer": "Not financial advice.",
}

print("== metadata ==")
check("slug_to_topic", M.slug_to_topic("broke-ep012-broke-to-10k.mp4") == "Broke Ep012 Broke To 10k")
check("detect_type short qua folder", M.detect_type("x.mp4", "OUTBOX/BROKE/short") == "short")
check("detect_type long mặc định", M.detect_type("x.mp4", "OUTBOX/BROKE/long") == "long")

meta = M.build_metadata({"topic": "T" * 200, "type": "long"}, BRANDING)
check("title bị cắt <=100", len(meta["title"]) <= 100)
check("description có disclaimer", "financial advice" in meta["description"].lower())
check("tags tổng < 480 ký tự", sum(len(t) + 1 for t in meta["tags"]) <= 480)

short_meta = M.build_metadata({"topic": "Quick tip", "type": "short"}, BRANDING)
check("short title có #shorts", "#shorts" in short_meta["title"])

bad = M.build_metadata({"topic": "get rich quick now", "type": "long"}, BRANDING)
check("lint bắt cụm rủi ro", any("get rich quick" in w for w in M.lint(bad)))
check("lint bắt ký tự < >", any("<" in w or ">" in w for w in M.lint({**meta, "title": "a<b"})))

print("== scheduler ==")
now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
items = [
    {"type": "short", "publish_at": (now - timedelta(hours=1)).isoformat(), "status": "pending"},
    {"type": "short", "publish_at": (now + timedelta(hours=1)).isoformat(), "status": "pending"},
    {"type": "long", "publish_at": (now - timedelta(hours=2)).isoformat(), "status": "posted"},
]
due = S.due_items(items, now)
check("due_items chỉ lấy quá giờ & chưa posted", len(due) == 1)

safety = {"youtube_max_uploads_per_day": 6, "facebook_max_uploads_per_day": 10, "min_minutes_between_uploads": 30}
ready = [{"type": "short", "platforms": ["youtube"], "publish_at": now.isoformat(), "status": "pending"}]
sel = S.apply_limits(ready, safety, 0, 0, None, now)
check("apply_limits chọn tối đa 1/lần", len(sel) == 1)
capped = S.apply_limits(ready, safety, 6, 0, None, now)
check("apply_limits chặn khi đủ trần YT", len(capped) == 0)
gap = S.apply_limits(ready, safety, 0, 0, now - timedelta(minutes=5), now)
check("apply_limits chặn khi chưa đủ giãn cách", len(gap) == 0)

print()
if fails:
    print(f"❌ {len(fails)} test FAIL: {fails}")
    sys.exit(1)
print("✅ Tất cả test PASS")
