"""Small, dependency-light thumbnail URL normalization helpers."""

import html
import re
import threading
import time
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



# --------------------------------------------------------------------------
# Fast poster lookup.
# The watch page shows the video page's og:image. It sits in <head>, so the
# poster job only needs the first few KB of the page, not a full parse.
# --------------------------------------------------------------------------
_META_TAG = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_ATTR = re.compile(r"""([a-zA-Z_:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_IMAGE_URL_JSON = re.compile(r'"image_url"\s*:\s*"([^"]+)"')
_POSTER_KEYS = (
    ("og:image",),
    ("og:image:url", "og:image:secure_url"),
    ("twitter:image", "twitter:image:src"),
)


def poster_from_html(text, base_url=""):
    """Return the page's own poster, in the same order the watch page uses."""
    text = str(text or "")
    found = {}
    for tag in _META_TAG.finditer(text):
        attrs = {}
        for m in _META_ATTR.finditer(tag.group(0)):
            value = m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None else m.group(4)
            attrs[m.group(1).lower()] = value or ""
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        if key and key not in found and attrs.get("content"):
            found[key] = attrs["content"]
    for group in _POSTER_KEYS[:2]:
        for key in group:
            value = clean_thumbnail_url(found.get(key, ""), base_url=base_url)
            if value:
                return value
    json_hit = _IMAGE_URL_JSON.search(text)
    if json_hit:
        value = clean_thumbnail_url(json_hit.group(1).replace("\\/", "/"), base_url=base_url)
        if value:
            return value
    for key in _POSTER_KEYS[2]:
        value = clean_thumbnail_url(found.get(key, ""), base_url=base_url)
        if value:
            return value
    return ""


def read_page_head(chunks, limit=768 * 1024, deadline=None, base_url=""):
    """Collect streamed page bytes until a poster is found, </head>, the limit or the deadline.

    Errors while streaming (timeouts, resets) keep whatever already arrived:
    the provider logs show most timeouts happen after the <head> was received.
    Returns (poster, bytes_read).
    """
    buf = bytearray()
    next_check = 24 * 1024
    try:
        for chunk in chunks:
            if not chunk:
                continue
            buf += chunk
            tail = bytes(buf[-(len(chunk) + 8):]).lower()
            if b"</head" in tail or len(buf) >= limit:
                break
            if len(buf) >= next_check:
                next_check = len(buf) + 24 * 1024
                if poster_from_html(bytes(buf).decode("utf-8", "replace"), base_url=base_url):
                    break
            if deadline is not None and time.monotonic() >= deadline:
                break
    except Exception:
        pass
    return poster_from_html(bytes(buf).decode("utf-8", "replace"), base_url=base_url), len(buf)


class PosterBroker:
    """Non-blocking poster resolver shared by every /api/posters request.

    lookup() never touches the network: it answers from cache, marks misses as
    pending and hands them to a small background pool. Recent failures are
    remembered briefly so the site can stop waiting and use its fallback.
    """

    def __init__(self, fetch, submit, cache, fail_ttl=120.0, max_pending=128, clock=time.monotonic):
        self._fetch = fetch
        self._submit = submit
        self._cache = cache
        self._fail_ttl = float(fail_ttl)
        self._max_pending = int(max_pending)
        self._clock = clock
        self._failed = {}
        self._pending = set()
        self._lock = threading.Lock()

    def lookup(self, urls):
        wanted, seen = [], set()
        for u in urls or []:
            u = str(u or "").strip()
            if u and u not in seen and is_provider_page(u):
                seen.add(u)
                wanted.append(u)
            if len(wanted) >= MAX_POSTER_BATCH:
                break
        posters, pending, start = {}, [], []
        now = self._clock()
        with self._lock:
            for u in wanted:
                hit = self._cache.get(u) if hasattr(self._cache, "get") else None
                if hit:
                    posters[u] = hit
                    continue
                posters[u] = ""
                expires = self._failed.get(u)
                if expires is not None:
                    if expires > now:
                        continue
                    self._failed.pop(u, None)
                pending.append(u)
                if u in self._pending or len(self._pending) >= self._max_pending:
                    continue
                self._pending.add(u)
                start.append(u)
        for u in start:
            try:
                self._submit(self._run, u)
            except Exception:
                with self._lock:
                    self._pending.discard(u)
        return posters, pending

    def _run(self, url):
        poster = ""
        try:
            poster = clean_thumbnail_url(self._fetch(url, provider_of(url)) or "", base_url=url)
        except Exception:
            poster = ""
        with self._lock:
            self._pending.discard(url)
            if poster:
                try:
                    self._cache[url] = poster
                except Exception:
                    pass
                self._failed.pop(url, None)
            else:
                now = self._clock()
                self._failed[url] = now + self._fail_ttl
                if len(self._failed) > 4000:
                    for key in [k for k, exp in self._failed.items() if exp <= now][:2000]:
                        self._failed.pop(key, None)
        return poster
