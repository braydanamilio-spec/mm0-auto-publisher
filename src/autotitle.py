"""
autotitle.py — PHƯƠNG ÁN 2 (dự phòng): tự sinh TIÊU ĐỀ + emoji + MÔ TẢ + HASHTAG + TAG
tổ hợp (hàng TRIỆU biến thể), hook cao, chung chung, hợp thị trường USA.

DÙNG KHI: video KHÔNG có file metadata (không tiêu đề/mô tả riêng) — chỉ khi user BẬT
(overrides.fallback_meta.enabled). Mặc định TẮT. Video có metadata thật -> KHÔNG đụng.

Deterministic: seed theo drive_file_id -> mỗi video 1 tiêu đề CỐ ĐỊNH (retry không đổi),
nhưng khác nhau giữa các video (đa dạng).

Số tổ hợp: HOOKS(24)×POWER(14)×SUBJECT(12)×EMO_LEAD(14)×EMO_TAIL(10)×FORMAT(7)
  ≈ 4.9 triệu tiêu đề, × biến thể mô tả × tổ hợp hashtag -> hàng CHỤC TRIỆU.
"""
from __future__ import annotations
import random

# --- Thành phần tiêu đề (hook CTR cao, chung chung, USA) ---
HOOKS = [
    "You won't believe", "Nobody talks about", "The truth about", "This changed everything about",
    "Wait for it —", "POV:", "Watch this before you scroll", "The secret behind",
    "Here's why", "Stop scrolling —", "This is why", "What they don't tell you about",
    "I can't believe", "The real reason", "Everyone's obsessed with", "This broke the internet:",
    "Caught on camera:", "You need to see", "How to actually", "The moment",
    "Little-known fact:", "Plot twist:", "They said it couldn't be done:", "Nobody expected",
]
POWER = ["insane", "unbelievable", "genius", "wild", "shocking", "next-level", "jaw-dropping",
         "mind-blowing", "epic", "crazy", "unreal", "legendary", "priceless", "flawless"]
SUBJECTS = ["this", "this trick", "this moment", "the results", "what happens next", "this hack",
            "this story", "this secret", "the ending", "this reaction", "the details", "this setup"]
EMO_LEAD = ["🔥", "😱", "🤯", "🚨", "👀", "💥", "⚡", "💯", "🥶", "❤️‍🔥", "😳", "🤔", "✨", "🙌"]
EMO_TAIL = ["🔥🔥", "😱", "🤯", "💯", "👇", "‼️", "🙌", "👀", "🚀", "✅"]

DESC_LINES = [
    "This one caught everyone off guard.",
    "Watch till the end — it's worth it.",
    "You'll want to see this twice.",
    "This is the kind of thing you can't unsee.",
    "Small moment, big impact.",
    "Some things just have to be seen to be believed.",
    "This hit different.",
    "Save this before it disappears.",
]
CTA_LINES = [
    "Drop a ❤️ if you agree!", "Follow for more 🔥", "Share this with someone who needs it!",
    "Comment your thoughts below 👇", "Hit follow so you don't miss the next one 🚀",
    "Which part surprised you most?", "Tag a friend who has to see this 👀",
]

# --- Pools hashtag / tag (viral USA, chung chung) ---
HASHTAGS_CORE = ["#fyp", "#foryou", "#foryoupage", "#viral", "#trending", "#trend", "#usa",
                 "#explore", "#explorepage", "#mustwatch", "#wow", "#relatable", "#satisfying",
                 "#storytime", "#viralvideo", "#trendingnow", "#reelsviral", "#instagood"]
TAGS_POOL = ["viral", "trending", "fyp", "for you", "usa", "explore", "must watch", "satisfying",
             "relatable", "story", "viral video", "trending now", "wow", "amazing", "best moments"]


def _title(r: random.Random) -> str:
    hook = r.choice(HOOKS); power = r.choice(POWER); subj = r.choice(SUBJECTS)
    lead = r.choice(EMO_LEAD); tail = r.choice(EMO_TAIL)
    fmts = [
        f"{lead} {hook} {subj} {tail}",
        f"{hook} {power} {subj} {tail}",
        f"{lead} {power.capitalize()}! {subj} {tail}",
        f"{hook} {subj}… {tail}",
        f"{lead} This is {power} {tail}",
        f"{hook} {subj} (100% {power}) {tail}",
        f"{lead} {hook} {subj}",
    ]
    return r.choice(fmts).strip()[:100]


def generate(seed: str, vtype: str = "long") -> dict:
    """Trả {title, description, hashtags, tags} tổ hợp — cố định theo seed."""
    r = random.Random(str(seed))
    title = _title(r)
    body = r.choice(DESC_LINES)
    cta = r.choice(CTA_LINES)
    description = f"{title}\n\n{body}\n{cta}"
    n_tag = r.randint(6, 10)
    hashtags = r.sample(HASHTAGS_CORE, min(n_tag, len(HASHTAGS_CORE)))
    if vtype == "short" and "#shorts" not in hashtags:
        hashtags = ["#shorts"] + hashtags[:n_tag - 1]
    tags = r.sample(TAGS_POOL, min(r.randint(6, 10), len(TAGS_POOL)))
    return {"title": title, "description": description, "hashtags": hashtags, "tags": tags}


def apply_fallback(raw: dict, cfg: dict, seed: str) -> dict:
    """Nếu BẬT phương án 2 VÀ video CHƯA có metadata thật (không title & không description)
    -> điền tiêu đề/mô tả/hashtag/tag tổ hợp. Ngược lại giữ nguyên."""
    if not cfg or not cfg.get("enabled"):
        return raw
    if (raw.get("title") or "").strip() or (raw.get("description") or "").strip():
        return raw   # đã có metadata thật -> KHÔNG đụng
    g = generate(seed, raw.get("type") or "long")
    raw["title"] = g["title"]
    raw["description"] = g["description"]
    if not raw.get("hashtags"):
        raw["hashtags"] = g["hashtags"]
    if not raw.get("tags"):
        raw["tags"] = g["tags"]
    raw["_autofilled"] = True
    return raw
