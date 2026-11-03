"""Small, dependency-light thumbnail URL normalization helpers."""

import html
import re
from urllib.parse import urljoin, urlparse


def clean_thumbnail_url(raw_url, base_url=""):
    """Normalize provider poster values, including lazy/srcset/relative values."""
    if isinstance(raw_url, dict):
        raw_url = raw_url.get("url") or raw_url.get("src") or raw_url.get("thumbnail") or ""
    if isinstance(raw_url, (list, tuple)):
        raw_url = next((value for value in reversed(raw_url) if value), "")
    value = html.unescape(str(raw_url or "")).strip()
    if not value:
        return ""
    css_url = re.fullmatch(r"url\((.*?)\)", value, re.IGNORECASE)
    if css_url:
        value = css_url.group(1).strip().strip("\"'")

    # srcset candidates may be ordered small-to-large or large-to-small.
    # Select the highest declared width/density, not merely the last entry.
    if "," in value:
        candidates = []
        for index, part in enumerate(value.split(",")):
            match = re.match(r"\s*(\S+)(?:\s+(\d+(?:\.\d+)?)(w|x))?", part.strip(), re.IGNORECASE)
            if not match:
                continue
            score = float(match.group(2) or 0)
            if match.group(3) and match.group(3).lower() == "x":
                score *= 10000
            candidates.append((score, index, match.group(1)))
        if candidates and any(score for score, _, _ in candidates):
            value = max(candidates)[2]

    value = value.strip().strip("\"'")
    if value.startswith("//"):
        value = "https:" + value
    elif base_url:
        value = urljoin(base_url, value)
    if value.startswith("http://"):
        value = "https://" + value[7:]
    try:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return ""
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
            return ""
        low = value.lower()
        if low.startswith(("data:", "blob:", "javascript:")) or any(
            marker in low for marker in ("blank.gif", "placeholder", "transparent.gif", "pixel.gif")
        ):
            return ""
        if ".svg" in parsed.path.lower():
            return ""
        return value
    except Exception:
        return ""


def thumbnail_from_img(img, base_url=""):
    """Resolve a listing card's poster from an <img>.

    Provider "poster" attributes win (data-thumb_url / data-mediumthumb are the
    thumbnail the uploader picked). srcset candidates come last because several
    providers fill them with timeline frames or sprite sheets.
    """
    if img is None:
        return ""
    candidates = [
        img.get(attr)
        for attr in (
            "data-thumb_url",
            "data-mediumthumb",
            "data-poster",
            "data-original",
            "data-src",
            "data-lazy-src",
            "data-image",
            "data-thumb",
            "src",
            "data-srcset",
            "srcset",
        )
    ]
    picture = img.find_parent("picture")
    if picture:
        for source in picture.find_all("source"):
            candidates.extend([source.get("data-srcset"), source.get("srcset")])
    for candidate in candidates:
        value = clean_thumbnail_url(candidate, base_url=base_url)
        if value:
            return value
    return ""


# --------------------------------------------------------------------------
# Real posters for listing cards.
# Search pages often show a timeline frame; the video page's og:image is the
# poster the player (and the watch page) uses. /api/posters resolves those in
# one batched, cached call so cards can swap to the same image.
# --------------------------------------------------------------------------
POSTER_DOMAINS = ("pornhub.com", "xvideos.com", "xnxx.com", "redtube.com", "youporn.com", "xhamster.com")
MAX_POSTER_BATCH = 24


def is_provider_page(url):
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
        return False
    return any(host == d or host.endswith("." + d) for d in POSTER_DOMAINS)


def provider_of(url):
    host = (urlparse(str(url or "")).hostname or "").lower()
    for d in POSTER_DOMAINS:
        if host == d or host.endswith("." + d):
            return d.split(".")[0]
    return ""


def resolve_posters(urls, fetch_meta, cache, pool_map=map):
    """Return {page_url: poster_url or ""} for up to MAX_POSTER_BATCH provider pages.

    fetch_meta(url, provider) -> {"thumbnail": ...}; cache is dict-like.
    Only positive poster results are cached. A temporary provider timeout must
    not pin a missing poster for hours or prevent a later retry.
    """
    wanted, seen = [], set()
    for u in urls or []:
        u = str(u or "").strip()
        if u and u not in seen and is_provider_page(u):
            seen.add(u)
            wanted.append(u)
        if len(wanted) >= MAX_POSTER_BATCH:
            break
    out, missing = {}, []
    for u in wanted:
        cached = cache.get(u) if hasattr(cache, "get") else None
        if cached:
            out[u] = cached
        else:
            missing.append(u)

    def one(u):
        try:
            meta = fetch_meta(u, provider_of(u)) or {}
            return u, clean_thumbnail_url(meta.get("thumbnail") or "", base_url=u)
        except Exception:
            return u, ""

    for u, poster in pool_map(one, missing):
        if poster:
            try:
                cache[u] = poster
            except Exception:
                pass
        out[u] = poster
    return out
