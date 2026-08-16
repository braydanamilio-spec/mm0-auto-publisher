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
from urllib.parse import quote


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


def link_for(cfg: dict, slug: str) -> str:
    """Link riêng của kênh/Page (nếu có), nếu không lấy link mặc định."""
    links = (cfg.get("links") or {}) if cfg else {}
    return str(links.get(slug) or (cfg or {}).get("default_url") or "").strip()


def enabled_for(cfg: dict, platform: str) -> bool:
    if not cfg or not cfg.get("enabled"):
        return False
    plats = cfg.get("platforms") or {}
    return bool(plats.get(platform, True))   # mặc định bật nếu không cấu hình riêng


def _block(cfg: dict, url: str) -> str:
    """Khối văn bản: CTA + link + disclosure."""
    cta = str((cfg or {}).get("cta_text") or "🛒 Link:").strip()
    disc = str((cfg or {}).get("disclosure") or "").strip()
    out = f"{cta} {url}".strip()
    if disc:
        out += "\n" + disc
    return out


def apply(meta: dict, cfg: dict, platform: str, slug: str, content_id: str) -> str | None:
    """Chèn link ĐÚNG CHỖ cho `platform`, mutate meta['description'].
    Trả về TEXT bình luận cần đăng sau (hoặc None nếu không cần comment).
    `meta` nên là BẢN SAO riêng cho từng nền tảng (FB/IG khác nhau)."""
    if not enabled_for(cfg, platform):
        return None
    url0 = link_for(cfg, slug)
    if not url0:
        return None
    vtype = meta.get("type", "long")
    url = add_utm(url0, platform, slug, content_id, bool(cfg.get("utm", True)))
    disc = str(cfg.get("disclosure") or "").strip()
    desc = str(meta.get("description") or "")

    if platform == "instagram":
        # Caption/comment IG KHÔNG bấm link được -> chỉ CTA bio (không nhét link thật -> tránh phí + spam).
        add = "👉 Link ở phần Bio"
        if disc:
            add += "\n" + disc
        meta["description"] = (desc + "\n\n" + add).strip()[:2200]
        return None

    if platform == "facebook":
        if vtype == "short":
            # Reels: caption không click -> để link ở BÌNH LUẬN.
            meta["description"] = (desc + "\n\n👇 Link ở bình luận ghim").strip()[:2200]
            return _block(cfg, url)
        # Video thường: mô tả bấm được.
        meta["description"] = (desc + "\n\n" + _block(cfg, url)).strip()
        return _block(cfg, url) if cfg.get("auto_comment") else None

    if platform == "youtube":
        meta["description"] = (desc + "\n\n" + _block(cfg, url)).strip()[:5000]
        return _block(cfg, url) if cfg.get("auto_comment") else None

    return None
