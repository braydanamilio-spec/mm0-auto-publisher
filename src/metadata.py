"""
metadata.py — Xây dựng & kiểm tra (lint) metadata chuẩn cho từng video.

Nhiệm vụ:
  - Ghép title / description / hashtags / tags theo branding của kênh.
  - Bảo đảm ĐÚNG GIỚI HẠN nền tảng (tránh lỗi upload & tránh bị coi spam).
  - Chèn CTA + disclaimer -> giảm rủi ro vi phạm chính sách.

Một "video item" là dict gộp từ:
  - filename trên Drive (fallback topic)
  - sidecar JSON (nếu có) — ghi đè mọi field
  - branding của kênh (defaults)
"""

from __future__ import annotations
import re

# Giới hạn cứng của nền tảng (2026)
YT_TITLE_MAX = 100
YT_DESC_MAX = 5000
YT_TAGS_TOTAL_MAX = 480          # tổng ký tự tags < 500, để đệm an toàn
FB_TITLE_MAX = 255
HASHTAG_MAX = 15                 # YouTube: >15 hashtag -> bỏ hết; giữ chung ngưỡng an toàn cho mọi nền tảng
IG_CAPTION_MAX = 2200           # Instagram caption tối đa
IG_HASHTAG_MAX = 30             # Instagram: >30 hashtag -> bài bị từ chối / dễ shadowban

# Từ ngữ dễ gây gắn cờ / mất kiếm tiền — cảnh báo (không chặn cứng).
RISKY_WORDS = [
    "guaranteed money", "get rich quick", "free money", "100% profit",
    "click here", "sub4sub", "subscribe for subscribe",
]


def slug_to_topic(filename: str) -> str:
    """`how-i-went-broke_short.mp4` -> `How I Went Broke`."""
    name = re.sub(r"\.(mp4|mov|mkv|webm)$", "", filename, flags=re.I)
    name = re.sub(r"[_\-]+(short|long|s|l)$", "", name, flags=re.I)
    name = name.replace("_", " ").replace("-", " ").strip()
    return " ".join(w.capitalize() if w.islower() else w for w in name.split())


def detect_type(filename: str, folder: str | None = None) -> str:
    """Suy ra long/short từ tên file hoặc folder chứa nó."""
    low = f"{folder or ''}/{filename}".lower()
    if "short" in low or low.endswith("_s.mp4") or "/short/" in low:
        return "short"
    return "long"


def _cap_hashtags(text: str, maxn: int) -> str:
    """Giữ tối đa `maxn` hashtag ĐẦU trong text, bỏ dấu # ở các tag thừa (giữ chữ, bỏ #).
    Vì YouTube >15 hashtag -> BỎ HẾT; ta chủ động cắt để hashtag còn lại vẫn có tác dụng."""
    seen = [0]
    def repl(m):
        seen[0] += 1
        return m.group(0) if seen[0] <= maxn else m.group(0)[1:]  # bỏ dấu #
    return re.sub(r"#(\w+)", repl, text)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rstrip()
    # Cắt ở ranh giới TỪ nếu khoảng trắng nằm gần cuối (giữ ≥60% để không quá cụt)
    sp = cut.rfind(" ")
    if sp >= int((limit - 1) * 0.6):
        cut = cut[:sp].rstrip()
    return cut + "…"


def build_metadata(item: dict, branding: dict) -> dict:
    """
    Trả về dict metadata sẵn sàng upload:
      { title, description, tags[], hashtags[], type, platforms[] }
    `item` = dữ liệu thô (topic, sidecar overrides...).
    """
    vtype = item.get("type") or "long"
    topic = item.get("topic") or "Untitled"
    hashtags = item.get("hashtags") or branding.get("hashtags") or []
    hashtags = hashtags[:5]  # YouTube chỉ hiển thị 3 hashtag đầu; giữ tối đa 5

    # ---- TITLE ----
    if item.get("title"):
        title = item["title"]
    else:
        tmpl = branding.get(
            "short_title_template" if vtype == "short" else "long_title_template",
            "{topic}",
        )
        title = tmpl.format(topic=topic)
    title = title.replace("<", "").replace(">", "")   # YouTube từ chối cứng < >
    title = _clip(title, YT_TITLE_MAX)

    # ---- DESCRIPTION ----
    parts = []
    if item.get("description"):
        parts.append(item["description"].strip())
    else:
        parts.append(topic)
    if branding.get("cta"):
        parts.append("\n" + branding["cta"].strip())
    # Đếm hashtag NGƯỜI DÙNG đã có sẵn -> chỉ chèn thêm sao cho TỔNG ≤ 15
    # (YouTube: >15 hashtag -> BỎ TẤT CẢ; Instagram: khuyến nghị ≤30). Chống spam/shadowban.
    existing_tags = len(re.findall(r"#\w+", " ".join(parts)))
    room = max(0, HASHTAG_MAX - existing_tags)
    if hashtags and room:
        parts.append(" ".join(hashtags[:room]))
    if branding.get("disclaimer"):
        parts.append("\n" + branding["disclaimer"].strip())
    description = "\n".join(p for p in parts if p)
    description = description.replace("<", "").replace(">", "")   # YouTube từ chối cứng < >
    description = _cap_hashtags(description, HASHTAG_MAX)          # tổng hashtag ≤15 (đúng chính sách)
    description = _clip(description, YT_DESC_MAX)

    # ---- TAGS (keyword YouTube, khác hashtag) ----
    tags = item.get("tags") or []
    if not tags:
        # tự sinh tag từ topic + hashtag (bỏ dấu #)
        tags = [w for w in topic.split() if len(w) > 3][:8]
        tags += [h.lstrip("#") for h in hashtags]
    tags = _trim_tags(tags)

    return {
        "type": vtype,
        "title": title,
        "description": description,
        "tags": tags,
        "hashtags": hashtags,
        "platforms": item.get("platforms") or ["youtube", "facebook"],
    }


def _trim_tags(tags: list[str]) -> list[str]:
    out, total = [], 0
    for t in tags:
        t = t.strip()
        if not t:
            continue
        if total + len(t) + 1 > YT_TAGS_TOTAL_MAX:
            break
        out.append(t)
        total += len(t) + 1
    return out


def lint(meta: dict) -> list[str]:
    """Trả về danh sách CẢNH BÁO (rỗng = sạch). Không chặn upload, chỉ nhắc."""
    warns = []
    if not meta["title"]:
        warns.append("Thiếu title.")
    if len(meta["title"]) > YT_TITLE_MAX:
        warns.append(f"Title > {YT_TITLE_MAX} ký tự.")
    if any(c in meta["title"] for c in "<>"):
        warns.append("Title chứa ký tự < hoặc > (YouTube sẽ từ chối).")
    if len(meta["description"]) > YT_DESC_MAX:
        warns.append(f"Description > {YT_DESC_MAX} ký tự.")
    blob = (meta["title"] + " " + meta["description"]).lower()
    for w in RISKY_WORDS:
        if w in blob:
            warns.append(f"Cụm từ rủi ro chính sách: '{w}'.")
    if meta["type"] == "short" and "#shorts" not in blob:
        warns.append("Short nên có #shorts để YouTube phân loại đúng.")
    ntags = len(re.findall(r"#\w+", meta["description"]))
    if ntags > HASHTAG_MAX:
        warns.append(f"{ntags} hashtag (>{HASHTAG_MAX}) — YouTube sẽ BỎ HẾT hashtag.")
    return warns


def lint_ig(meta: dict) -> list[str]:
    """Lint riêng cho Instagram/Facebook (caption 2200, hashtag ≤30)."""
    warns = []
    caption = f"{meta.get('title','')}\n\n{meta.get('description','')}"
    if len(caption) > IG_CAPTION_MAX:
        warns.append(f"Caption IG > {IG_CAPTION_MAX} ký tự (sẽ bị cắt).")
    ntags = len(re.findall(r"#\w+", caption))
    if ntags > IG_HASHTAG_MAX:
        warns.append(f"{ntags} hashtag (>{IG_HASHTAG_MAX}) — IG dễ từ chối / shadowban.")
    return warns
