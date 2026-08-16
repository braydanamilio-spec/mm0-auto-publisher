"""
affiliate.py — Chèn LINK TIẾP THỊ LIÊN KẾT thông minh theo từng nền tảng.

Sự thật khả năng link (đã kiểm tra kỹ):
  - YouTube (long+short) MÔ TẢ        : link BẤM ĐƯỢC  -> chèn vào description.
  - Facebook video thường (long) MÔ TẢ: link BẤM ĐƯỢC  -> chèn vào description.
  - Facebook Reels (short) CAPTION    : link KHÔNG bấm  -> đăng BÌNH LUẬN đầu (comment).
  - Instagram Reels caption/comment   : link KHÔNG bấm + API không cho tự comment
                                        -> chỉ ghi 'Link ở bio' (user tự đặt link bio).

Chống spam + đúng chính sách:
  - UTM mỗi bài (utm_source/campaign/content) -> link KHÁC nhau (không phải spam link giống hệt)
    + đo được hiệu suất từng nền tảng/kênh/video.
  - Tự thêm disclosure (#ad / 'Tiếp thị liên kết') -> tuân thủ YouTube paid-promotion & Meta branded content.
  - 1 link/bài. Không nhồi nhiều link.

cfg = settings/overrides__<uid>.affiliate:
  { enabled, default_url, cta_text, disclosure, utm, auto_comment,
    platforms:{youtube,facebook,instagram}, links:{<slug>: url} }
"""
from __future__ import annotations
import random
import re
from urllib.parse import quote

# Giới hạn độ dài caption/description mỗi nền tảng (để chèn link KHÔNG vỡ + link luôn còn).
LIMITS = {
    ("youtube", "long"): 5000, ("youtube", "short"): 5000,
    ("facebook", "long"): 8000, ("facebook", "short"): 2200,   # Reels caption ~2200
    ("instagram", "short"): 2200, ("instagram", "long"): 2200,
}

# THƯ VIỆN CTA chuẩn thị trường USA — random mỗi bài để KHÔNG giống hệt (chống spam, tự nhiên hơn).
CTA_TEMPLATES = [
    "🛒 Shop it here 👉", "🔥 Grab yours now 👉", "✅ Get it here 👉",
    "💯 Best price here 👉", "⚡ Limited stock — shop now 👉", "🎁 Check the deal 👉",
    "🛍️ Buy now 👉", "👇 Tap the link 👇", "🤑 Save on this 👉",
    "💥 Don't miss out 👉", "⭐ Fan favorite 👉", "🚀 Grab the deal 👉",
]
# CTA cho chỗ "link ở bình luận / bio" (không kèm URL ngay dòng đó)
CTA_ELSEWHERE = {
    "comment": ["👇 Link in the comments", "🔗 Link pinned in comments 👇", "👇 Tap comments for the link"],
    "bio": ["🔗 Link in bio 👆", "👆 Grab it — link in bio", "🛒 Shop via link in bio 👆"],
}


def pick_cta(cfg: dict, content_id: str, where: str = "url") -> str:
    """CTA cố định (nếu user đặt) HOẶC random từ thư viện — seed theo content_id để ổn định khi retry."""
    custom = str((cfg or {}).get("cta_text") or "").strip()
    if custom and where == "url":
        return custom
    rnd = random.Random(str(content_id))
    if where == "url":
        return rnd.choice(CTA_TEMPLATES)
    return rnd.choice(CTA_ELSEWHERE.get(where, CTA_TEMPLATES))


def _clip_utm(s: str) -> str:
    return quote(str(s or "").strip())[:60]


def add_utm(url: str, source: str, campaign: str, content: str, on: bool = True) -> str:
    """Gắn UTM để mỗi bài 1 link khác nhau (chống spam) + đo hiệu suất."""
    if not on or not url:
        return url
    if "utm_source=" in url:      # đã có UTM -> giữ nguyên
        return url
    sep = "&" if "?" in url else "?"
    q = (f"utm_source={_clip_utm(source)}&utm_medium=affiliate"
         f"&utm_campaign={_clip_utm(campaign)}&utm_content={_clip_utm(content)}")
    return url + sep + q


def _as_pool(v) -> list:
    """Chuẩn hoá về DANH SÁCH url — nhận list, hoặc chuỗi nhiều link (xuống dòng/dấu phẩy)."""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [p.strip() for p in re.split(r"[\n,]+", str(v or "")) if p.strip()]


def link_pool(cfg: dict, slug: str) -> list:
    """DANH SÁCH link cho kênh/Page: pool RIÊNG của kênh (nếu có) > pool MẶC ĐỊNH (toàn cục).
    Hỗ trợ cả kiểu cũ (1 link chuỗi) lẫn mới (nhiều link)."""
    cfg = cfg or {}
    links = cfg.get("links") or {}
    own = _as_pool(links.get(slug))
    if own:
        return own
    return _as_pool(cfg.get("default_urls")) or _as_pool(cfg.get("default_url"))


def pick_url(pool: list, content_id: str) -> str:
    """XOAY VÒNG: chọn 1 link trong pool theo seed content_id -> mỗi video 1 link cố định,
    nhưng RẢI ĐỀU khác nhau giữa các video (đa dạng, chống spam link giống hệt)."""
    if not pool:
        return ""
    if len(pool) == 1:
        return pool[0]
    return random.Random("url:" + str(content_id)).choice(pool)


def link_for(cfg: dict, slug: str) -> str:
    """(giữ tương thích) — 1 link đầu của pool."""
    pool = link_pool(cfg, slug)
    return pool[0] if pool else ""


def enabled_for(cfg: dict, platform: str) -> bool:
    if not cfg or not cfg.get("enabled"):
        return False
    plats = cfg.get("platforms") or {}
    return bool(plats.get(platform, True))   # mặc định bật nếu không cấu hình riêng


def _block(cfg: dict, url: str, cta: str) -> str:
    """Khối văn bản: CTA + link + disclosure."""
    disc = str((cfg or {}).get("disclosure") or "").strip()
    out = f"{cta} {url}".strip()
    if disc:
        out += "\n" + disc
    return out


def _fit(desc: str, block: str, limit: int, top: bool = False) -> str:
    """Ghép desc + block sao cho TỔNG <= limit, ƯU TIÊN GIỮ block (link).
    top=True -> block Ở ĐẦU mô tả; ngược lại ở cuối. Tràn -> cắt DESC GỐC (không cắt link) ở ranh giới từ."""
    desc = (desc or "").strip()
    block = (block or "").strip()
    sep = "\n\n"
    room = limit - len(block) - len(sep)
    if room < 0:                     # block dài hơn cả limit (hiếm) -> chỉ giữ block cắt gọn
        return block[:limit].rstrip()
    if len(desc) > room:
        cut = desc[:room].rstrip()
        sp = cut.rfind(" ")
        if sp >= int(room * 0.6):
            cut = cut[:sp].rstrip()
        desc = cut + "…"
    if not desc:
        return block
    return (block + sep + desc) if top else (desc + sep + block)


def apply(meta: dict, cfg: dict, platform: str, slug: str, content_id: str) -> str | None:
    """Chèn link ĐÚNG CHỖ cho `platform`, mutate meta['description'] (quản lý độ dài, link luôn còn).
    Trả về TEXT bình luận cần đăng sau (hoặc None). `meta` nên là BẢN SAO riêng từng nền tảng."""
    if not enabled_for(cfg, platform):
        return None
    url0 = pick_url(link_pool(cfg, slug), content_id)   # XOAY VÒNG link theo video
    if not url0:
        return None
    vtype = meta.get("type", "long")
    limit = LIMITS.get((platform, vtype), 2200)
    top = str(cfg.get("placement") or "bottom") == "top"   # vị trí link: đầu / cuối mô tả
    url = add_utm(url0, platform, slug, content_id, bool(cfg.get("utm", True)))
    disc = str(cfg.get("disclosure") or "").strip()
    desc = str(meta.get("description") or "")

    if platform == "instagram":
        # Caption/comment IG KHÔNG bấm link -> chỉ CTA bio (không nhét link thật).
        cta = pick_cta(cfg, content_id, "bio")
        add = cta + ("\n" + disc if disc else "")
        meta["description"] = _fit(desc, add, limit, top)
        return None

    if platform == "facebook":
        cta = pick_cta(cfg, content_id, "url")
        block = _block(cfg, url, cta)
        if vtype == "short":
            # Reels: caption không click -> link ở BÌNH LUẬN. Caption chỉ CTA "link ở comment".
            note = pick_cta(cfg, content_id, "comment")
            meta["description"] = _fit(desc, note, limit, top)
            return block
        meta["description"] = _fit(desc, block, limit, top)
        return block if cfg.get("auto_comment") else None

    if platform == "youtube":
        cta = pick_cta(cfg, content_id, "url")
        block = _block(cfg, url, cta)
        meta["description"] = _fit(desc, block, limit, top)
        return block if cfg.get("auto_comment") else None

    return None
