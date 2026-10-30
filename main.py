import asyncio
from concurrent.futures import ThreadPoolExecutor
import html as html_parser
import logging
import os
import re
import time
from urllib.parse import quote, unquote, urlparse

from bs4 import BeautifulSoup
from cachetools import TTLCache
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
import requests
import yt_dlp

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def normalize_duration(raw) -> str:
    if raw is None or raw == "":
        return ""
    total = 0
    text = str(raw).strip()
    if isinstance(raw, (int, float)) or text.isdigit():
        total = int(float(raw))
    else:
        parts = text.split(":")
        if not 2 <= len(parts) <= 3 or not all(p.strip().isdigit() for p in parts):
            return text
        for p in parts:
            total = total * 60 + int(p)
    if total <= 0:
        return ""
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def normalize_video_item(item):
    if not isinstance(item, dict):
        return item
    out = dict(item)
    out["duration"] = normalize_duration(out.get("duration"))
    return out

extraction_cache = TTLCache(maxsize=0, ttl=0)
search_cache = TTLCache(maxsize=0, ttl=0)
thread_pool = ThreadPoolExecutor(max_workers=50)


def load_runtime_providers_config():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "providers.txt")
    cfg = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("|")
                        if len(parts) >= 3:
                            cfg[parts[0].strip()] = {
                                "id": parts[0].strip(),
                                "name": parts[1].strip(),
                                "domain": parts[2].strip().rstrip("/"),
                                "search": parts[3].strip() if len(parts) > 3 else "",
                                "page": parts[4].strip() if len(parts) > 4 else ""
                            }
        except Exception:
            pass
    return cfg

RUNTIME_PROVIDERS = load_runtime_providers_config()

app = FastAPI(title="Media Extraction Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_dynamic_headers(target: str, request_headers: dict = None) -> dict:
    target_lower = target.lower()
    if any(h in target_lower for h in ("pornhub", "phncdn", "phprcdn", "phcdn")):
        ref = "https://www.pornhub.com/"
    elif "xhamster" in target_lower or "xhcdn" in target_lower:
        ref = "https://xhamster.com/"
    elif "xnxx" in target_lower:
        ref = "https://www.xnxx.com/"
    elif "xvideos" in target_lower:
        ref = "https://www.xvideos.com/"
    elif "redtube" in target_lower:
        ref = "https://www.redtube.com/"
    elif "youporn" in target_lower:
        ref = "https://www.youporn.com/"
    else:
        ref = "https://www.xvideos.com/"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": ref,
        "Origin": ref.rstrip("/"),
        "Cookie": "has_accepted_cookie=1; age_verified=1; accessAgeDisclaimerPH=1; accessAgeDisclaimerUK=1; accessPH=1; platform=pc;",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if request_headers and "range" in request_headers:
        headers["Range"] = request_headers["range"]
    return headers


def clean_thumbnail_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    if "," in raw_url and ("http://" in raw_url or "https://" in raw_url):
        parts = re.split(r",\s+", raw_url)
        candidates = []
        for p in parts:
            url_part = p.strip().split(" ")[0]
            if url_part.startswith("http"):
                candidates.append(url_part)
        if candidates:
            raw_url = candidates[-1]

    raw_url = raw_url.strip()
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    return raw_url


def is_invalid_title(t: str) -> bool:
    if not t:
        return True
    t_clean = t.strip()
    if t_clean.isdigit() or len(t_clean) <= 2:
        return True
    if re.search(r"\d{1,2}:\d{2}", t_clean) and len(t_clean) <= 12:
        return True
    return False



def search_youporn_with_ytdlp(q: str, page: int) -> list:
    videos = []
    # yt-dlp has no "ypsearch" prefix; use the real search page (YouPornSearchIE) with paging
    search_term = f"https://www.youporn.com/search/?query={quote(q)}&page={max(1, int(page or 1))}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "nocheckcertificate": True,
        "age_limit": 21,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_term, download=False)
            entries = info.get("entries", []) if info else []
            for entry in entries:
                if not entry:
                    continue
                vkey = entry.get("id") or ""
                url = (
                    entry.get("url")
                    or f"https://www.youporn.com/watch/{vkey}"
                )
                title = html_parser.unescape(entry.get("title", "Unknown Video"))
                if any(
                    bad in url.lower() or bad in title.lower()
                    for bad in ["/join", "sponsor", "promo", "ad/"]
                ):
                    continue
                if is_invalid_title(title):
                    continue
                videos.append(
                    {
                        "vkey": vkey,
                        "title": title,
                        "thumbnail": clean_thumbnail_url(
                            entry.get("thumbnail", "")
                        ),
                        "url": url,
                        "provider": "youporn",
                    }
                )
    except Exception as e:
        logger.error(f"yt-dlp fallback search error for YouPorn: {e}")
    return videos


def search_xhamster_with_ytdlp(q: str, page: int) -> list:
    videos = []
    search_url = (
        f"https://xhamster.com/search/{quote(q)}"
        if page <= 1
        else f"https://xhamster.com/search/{quote(q)}/{page}"
    )
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "nocheckcertificate": True,
        "age_limit": 21,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
            entries = info.get("entries", []) if info else []
            for entry in entries:
                if not entry:
                    continue
                url = entry.get("url", "")
                vkey = entry.get("id", "")
                if not vkey and url:
                    parts = [p for p in url.split("/") if p]
                    vkey = parts[-1] if parts else ""
                if not vkey or "/videos/" not in url:
                    continue
                title = html_parser.unescape(
                    entry.get("title", f"Video {vkey}")
                )
                if any(
                    bad in url.lower() or bad in title.lower()
                    for bad in ["/join", "sponsor", "promo", "ad/"]
                ):
                    continue
                if is_invalid_title(title):
                    continue
                thumb = entry.get("thumbnail", "")
                if not thumb and entry.get("thumbnails"):
                    thumb = entry.get("thumbnails")[0].get("url", "")
                videos.append(
                    {
                        "vkey": vkey,
                        "title": title,
                        "thumbnail": clean_thumbnail_url(thumb),
                        "url": (
                            url
                            if url.startswith("http")
                            else f"https://xhamster.com/videos/{vkey}"
                        ),
                        "provider": "xhamster",
                    }
                )
    except Exception as e:
        logger.error(f"yt-dlp fallback search error for xHamster: {e}")
    return videos


def search_redtube_with_ytdlp(q: str, page: int) -> list:
    videos = []
    search_url = (
        f"https://www.redtube.com/?search={quote(q)}"
        if page <= 1
        else f"https://www.redtube.com/?search={quote(q)}&page={page}"
    )
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "nocheckcertificate": True,
        "age_limit": 21,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
            entries = info.get("entries", []) if info else []
            for entry in entries:
                if not entry:
                    continue
                url = entry.get("url", "")
                vkey = entry.get("id", "")
                if not vkey and url:
                    parts = [p for p in url.split("/") if p]
                    vkey = parts[-1] if parts else ""
                if not vkey or not vkey.isdigit():
                    continue
                title = html_parser.unescape(
                    entry.get("title", f"Video {vkey}")
                )
                if any(
                    bad in url.lower() or bad in title.lower()
                    for bad in [
                        "/join",
                        "sponsor",
                        "promo",
                        "ad/",
                        "/channels/",
                        "/pornstar/",
                        "/amateur/",
                                                "adtng.com",
                        "trafficjunky.net",
                    ]
                ):
                    continue
                if is_invalid_title(title) or "information/" in url:
                    continue
                thumb = entry.get("thumbnail", "")
                videos.append(
                    {
                        "vkey": vkey,
                        "title": title,
                        "thumbnail": clean_thumbnail_url(thumb),
                        "url": (
                            url
                            if url.startswith("http")
                            else f"https://www.redtube.com/{vkey}"
                        ),
                        "provider": "redtube",
                    }
                )
    except Exception as e:
        logger.error(f"yt-dlp fallback search error for RedTube: {e}")
    return videos


def parse_metadata_fallback(url: str, provider: str) -> dict:
    base_domain = "https://www.xvideos.com"
    if "pornhub.com" in url:
        base_domain = "https://www.pornhub.com"
    elif "xhamster.com" in url:
        base_domain = "https://xhamster.com"
    elif "xnxx.com" in url:
        base_domain = "https://www.xnxx.com"
    elif "xvideos.com" in url:
        base_domain = "https://www.xvideos.com"
    elif "redtube.com" in url:
        base_domain = "https://www.redtube.com"
    elif "youporn.com" in url:
        base_domain = "https://www.youporn.com"

    url = re.sub(r"https?://[a-zA-Z0-9-]+\." + provider + r"\.com", base_domain, url)
    headers = get_dynamic_headers(url)
    try:
        resp = requests.get(url, headers=headers, timeout=3.5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            poster_url = ""
            scraped_title = ""

            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                scraped_title = (
                    og_title.get("content").replace("&amp;", "&").strip()
                )

            if not scraped_title:
                title_tag = soup.find("title")
                if title_tag:
                    scraped_title = (
                        title_tag.get_text()
                        .replace("&amp;", "&")
                        .split("- RedTube")[0]
                        .split("- YouPorn")[0]
                        .split("- xHamster")[0]
                        .strip()
                    )

            og_img = soup.find("meta", property="og:image")
            if og_img and og_img.get("content"):
                poster_url = clean_thumbnail_url(
                    og_img.get("content").replace("&amp;", "&")
                )

            if not poster_url:
                img_json = re.search(r'"image_url"\s*:\s*"([^"]+)"', resp.text)
                if img_json:
                    poster_url = clean_thumbnail_url(
                        img_json.group(1).replace("\\/", "/")
                    )


            return {
                "thumbnail": poster_url,
                "title": scraped_title,
            }
    except Exception:
        pass
    return {"thumbnail": "", "title": ""}


# ---------------------------------------------------------------------------
# Pornhub pager (v7)
# Page 2+ used to come back identical to page 1: the HTML search answers some
# queries (channel / pornstar names such as "Brazzers") with a redirect that
# drops &page=N, and the page then also carries "recommended" rails. New order:
#   1. Webmasters JSON API  /webmasters/search?search=&page=   (stable paging)
#   2. HTML search with page verification (re-applies paging after a redirect,
#      checks the pagination widget, drops side rails)
# Every page is fingerprinted; a page identical to an earlier page of the same
# query is treated as a miss so Next never repeats results.
# ---------------------------------------------------------------------------
PH_BASE = "https://www.pornhub.com"
_PH_SIGS = TTLCache(maxsize=4096, ttl=1800)
_PH_RAILS = (
    "#recommendedVideos", "#hotVideosSection", "#relatedVideosCenter", ".underplayer",
    "#vpContentContainer", ".recommendedContainer", "#mostRecentVideosSection_rec",
    ".sectionWrapper.related", "#trendingVideos", ".pornstarsBox",
)


def _ph_sig(videos):
    return tuple(v.get("vkey") or v.get("url") for v in videos[:10])


def _ph_is_repeat(q, page, videos) -> bool:
    if page <= 1 or not videos:
        return False
    sig = _ph_sig(videos)
    key = q.strip().lower()
    for p in range(max(1, page - 3), page):
        prev = _PH_SIGS.get((key, p))
        if prev and (prev == sig or len(set(prev) & set(sig)) >= max(3, len(sig) // 2)):
            return True
    return False


def _ph_remember(q, page, videos):
    if videos:
        _PH_SIGS[(q.strip().lower(), page)] = _ph_sig(videos)


def _ph_headers(accept="text/html,application/xhtml+xml"):
    h = get_dynamic_headers(PH_BASE + "/")
    h["Accept"] = accept
    return h


def _ph_vkey_from(url: str) -> str:
    m = re.search(r"viewkey=([A-Za-z0-9]+)", url or "")
    return m.group(1) if m else ""


def _ph_webmasters(q: str, page: int) -> list:
    params = f"search={quote(q)}&page={page}&thumbsize=large_hd"
    r = requests.get(f"{PH_BASE}/webmasters/search?{params}", headers=_ph_headers("application/json"), timeout=10)
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    out, seen = [], set()
    for v in (data or {}).get("videos") or []:
        if not isinstance(v, dict):
            continue
        vkey = str(v.get("video_id") or "") or _ph_vkey_from(v.get("url", ""))
        if not vkey or vkey in seen:
            continue
        seen.add(vkey)
        thumb = v.get("default_thumb") or v.get("thumb") or ""
        if not thumb:
            thumbs = v.get("thumbs") or []
            if thumbs and isinstance(thumbs[-1], dict):
                thumb = thumbs[-1].get("src", "")
        title = html_parser.unescape(str(v.get("title") or "")).strip()
        out.append({
            "vkey": vkey,
            "title": title if title and not is_invalid_title(title) else f"Pornhub Video {vkey}",
            "thumbnail": clean_thumbnail_url(thumb),
            "url": f"{PH_BASE}/view_video.php?viewkey={vkey}",
            "provider": "pornhub",
            "duration": v.get("duration") or "",
        })
    return out


def _ph_parse_items(soup, seen=None) -> list:
    seen = seen if seen is not None else set()
    for sel in _PH_RAILS:
        for el in soup.select(sel):
            el.decompose()
    items = soup.select("ul#videoSearchResult li.pcVideoListItem, ul#videoSearchResult li[data-video-vkey]")
    if not items:
        items = soup.select(
            "ul#showAllChanelVideos li.pcVideoListItem, ul#channelsBody li.pcVideoListItem, "
            "ul#mostRecentVideosSection li.pcVideoListItem, ul#pornstarsVideoSection li.pcVideoListItem, "
            "ul.search-video-thumbs li.pcVideoListItem, ul.videos.row-5-thumbs li.pcVideoListItem"
        )
    if not items:
        items = soup.select("li.pcVideoListItem[data-video-vkey]")
    videos = []
    for item in items:
        try:
            cls = " ".join(item.get("class", [])).lower()
            if "promo" in cls or "sponsor" in cls or "ad" in cls.split():
                continue
            a_tag = item.select_one("a[href*='viewkey='], .title a, a.linkVideoThumb")
            vkey = item.get("data-video-vkey") or item.get("_vkey") or _ph_vkey_from(a_tag.get("href", "") if a_tag else "")
            if not vkey or vkey in seen:
                continue
            seen.add(vkey)
            title = (a_tag.get("title", "") if a_tag else "") or (a_tag.get_text(strip=True) if a_tag else "")
            if not title:
                t_el = item.select_one(".title, .videoTitle, span.title")
                title = t_el.get_text(strip=True) if t_el else ""
            if not title or is_invalid_title(title):
                title = f"Pornhub Video {vkey}"
            thumb = ""
            img_tag = item.select_one("img")
            if img_tag:
                for attr in ("data-thumb_url", "data-mediumthumb", "data-src", "data-image", "src"):
                    val = (img_tag.get(attr) or "").strip()
                    if val and not val.startswith("data:") and "blank" not in val:
                        thumb = clean_thumbnail_url(val)
                        break
            dur_el = item.select_one(".duration, var.duration")
            videos.append({
                "vkey": vkey,
                "title": html_parser.unescape(title),
                "thumbnail": thumb,
                "url": f"{PH_BASE}/view_video.php?viewkey={vkey}",
                "provider": "pornhub",
                "duration": dur_el.get_text(strip=True) if dur_el else "",
            })
            if len(videos) >= 48:
                break
        except Exception:
            continue
    return videos


def _ph_page_ok(soup, page: int) -> bool:
    """False when the pagination widget says we are on a different page."""
    cur = soup.select_one("li.page_current, .pagination3 li.page_current, .pagination3 .active")
    if not cur:
        return page <= 1 or bool(soup.select_one("ul#videoSearchResult"))
    m = re.search(r"\d+", cur.get_text(" ", strip=True))
    return (int(m.group(0)) if m else 1) == page


def _with_page(url: str, page: int) -> str:
    url = re.sub(r"([?&])page=\d+&?", r"\1", url).rstrip("?&")
    return url + ("&" if "?" in url else "?") + f"page={page}"


def _ph_html(q: str, page: int) -> list:
    url = f"{PH_BASE}/video/search?search={quote(q)}"
    if page > 1:
        url += f"&page={page}"
    r = requests.get(url, headers=_ph_headers(), timeout=10)
    if r.status_code != 200:
        return []
    final = str(getattr(r, "url", "") or url)
    if page > 1 and f"page={page}" not in final:
        # Redirected to a channel / pornstar page: paging was dropped, put it back.
        r = requests.get(_with_page(final, page), headers=_ph_headers(), timeout=10)
        if r.status_code != 200:
            return []
    soup = BeautifulSoup(r.text, "html.parser")
    if not _ph_page_ok(soup, page):
        logger.info("pornhub html: page %s not honoured for %r", page, q)
        return []
    return _ph_parse_items(soup)


def search_pornhub(q: str, page: int) -> list:
    page = max(1, int(page or 1))
    for name, method in (("webmasters", _ph_webmasters), ("html", _ph_html)):
        for attempt in range(2):
            try:
                videos = method(q, page)
            except Exception as e:
                logger.info("pornhub %s attempt %s failed: %s", name, attempt + 1, e)
                videos = []
            if videos and _ph_is_repeat(q, page, videos):
                logger.info("pornhub %s returned a repeated page %s for %r, trying next method", name, page, q)
                videos = []
                break
            if videos:
                _ph_remember(q, page, videos)
                return videos
            time.sleep(0.4)
    return []


def search_provider_robust(provider: str, q: str, page: int):
    cache_key = f"{provider}:{q}:{page}"
    if cache_key in search_cache:
        return search_cache[cache_key]

    videos = []
    if provider == "pornhub":
        # Dedicated pager (webmasters JSON API -> verified HTML). The generic
        # loop below is only used if both of those come back empty.
        videos = search_pornhub(q, page)
        if videos:
            search_cache[cache_key] = videos
        # never fall back to the unverified generic scraper: it is what repeated page 1
        return videos
    dummy_url = (
        f"https://www.{provider}.com/"
        if provider != "xhamster"
        else "https://xhamster.com/"
    )
    headers = get_dynamic_headers(dummy_url)

    if provider == "pornhub":
        search_url = (
            f"https://www.pornhub.com/video/search?search={quote(q)}"
            if page <= 1
            else f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"
        )
    elif provider == "youporn":
        search_url = (
            f"https://www.youporn.com/search/?query={quote(q)}&page={page}"
        )
    elif provider == "xhamster":
        search_url = (
            f"https://xhamster.com/search/{quote(q)}"
            if page <= 1
            else f"https://xhamster.com/search/{quote(q)}/{page}"
        )
    elif provider == "redtube":
        search_url = (
            f"https://www.redtube.com/?search={quote(q)}"
            if page <= 1
            else f"https://www.redtube.com/?search={quote(q)}&page={page}"
        )
    elif provider == "xnxx":
        search_url = (
            f"https://www.xnxx.com/search/{quote(q)}"
            if page <= 1
            else f"https://www.xnxx.com/search/{quote(q)}/{page}"
        )
    elif provider == "xvideos":
        p_val = page - 1 if page > 1 else 0
        search_url = (
            f"https://www.xvideos.com/?k={quote(q)}"
            if p_val == 0
            else f"https://www.xvideos.com/?k={quote(q)}&p={p_val}"
        )
    else:
        search_url = f"https://www.xvideos.com/?k={quote(q)}"

    for attempt in range(3):
        try:
            resp = requests.get(search_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                seen = set()

                if provider == "pornhub":
                    videos = _ph_parse_items(soup, seen)

                elif provider == "youporn":
                    items = soup.select(
                        "div.video-box, div.pb-card, div.list-item, "
                        "li.video-tile, div.videoBox, div.videoListItem, "
                        'div[class*="video"], div.video-tile, a[href*="/watch/"]'
                    )
                    for item in items:
                        full_url, title, thumb = "", "", ""
                        if item.name == "a":
                            href = item.get("href", "")
                            if not href or "/watch/" not in href:
                                continue
                            full_url = (
                                href
                                if href.startswith("http")
                                else f"https://www.youporn.com{href}"
                            )
                            title = (
                                item.get("title")
                                or item.get("alt")
                                or item.get_text(strip=True)
                            )
                            img_tag = item.select_one("img")
                            if img_tag:
                                if is_invalid_title(title):
                                    title = img_tag.get("alt") or title
                                thumb = clean_thumbnail_url(
                                    img_tag.get("data-src")
                                    or img_tag.get("src")
                                    or img_tag.get("data-lazy-src")
                                    or img_tag.get("data-image")
                                    or img_tag.get("data-thumb")
                                    or img_tag.get("data-poster")
                                    or ""
                                )
                        else:
                            a_tag = item.select_one('a[href*="/watch/"]')
                            if not a_tag:
                                continue
                            href = a_tag.get("href", "")
                            full_url = (
                                href
                                if href.startswith("http")
                                else f"https://www.youporn.com{href}"
                            )
                            title_tag = item.select_one(
                                'a[href*="/watch/"] [title], p.title, '
                                "span.title, a, div.title, h3, h4"
                            )
                            title = (
                                title_tag.get("title")
                                or title_tag.get_text(strip=True)
                                if title_tag
                                else a_tag.get("title", "")
                            )
                            img_tag = item.select_one("img")
                            if img_tag:
                                if is_invalid_title(title):
                                    title = img_tag.get("alt") or title
                                thumb = clean_thumbnail_url(
                                    img_tag.get("data-src")
                                    or img_tag.get("src")
                                    or img_tag.get("data-lazy-src")
                                    or img_tag.get("data-image")
                                    or img_tag.get("data-thumb")
                                    or img_tag.get("data-poster")
                                    or ""
                                )

                        full_url = full_url.split("?")[0].rstrip("/")
                        if not full_url or full_url in seen:
                            continue
                        if any(
                            bad in full_url.lower() or bad in title.lower()
                            for bad in ["/join", "sponsor", "promo", "ad/"]
                        ):
                            continue
                        if is_invalid_title(title):
                            continue
                        seen.add(full_url)

                        match_id = re.search(r"/watch/(\d+)", full_url)
                        if not match_id:
                            continue
                        vkey = match_id.group(1)

                        videos.append(
                            {
                                "vkey": vkey,
                                "title": html_parser.unescape(title),
                                "thumbnail": thumb,
                                "url": full_url,
                                "provider": "youporn",
                            }
                        )
                        if len(videos) >= 48:
                            break

                
                elif provider == "xhamster":
                    items = soup.select(
                        "div.thumb-list__item, article.video-thumb, "
                        "div.video-container, div[class*=\"video-thumb\"], "
                        "div.video-box, div[class*=\"video-card\"]"
                    )
                    if not items:
                        items = soup.select('a[href*="/videos/"]')
                    for item in items:
                        a_tag = (
                            item
                            if item.name == "a"
                            else item.select_one('a[href*="/videos/"]')
                        )
                        if not a_tag:
                            continue
                        href = a_tag.get("href", "")
                        if not href or "/videos/" not in href:
                            continue
                        full_url = (
                            href
                            if href.startswith("http")
                            else f"https://xhamster.com{href}"
                        )
                        full_url = full_url.split("?")[0].rstrip("/")

                        if any(
                            bad in full_url.lower()
                            for bad in [
                                "/pornstars/",
                                "/channels/",
                                "/creators/",
                                "/categories/",
                                "/tags/",
                                "/join",
                                "sponsor",
                                "promo",
                            ]
                        ):
                            continue
                        if full_url in seen:
                            continue

                        vid_parts = [p for p in full_url.split("/") if p]
                        vid_id = vid_parts[-1] if vid_parts else "unknown"

                        title_tag = (
                            item.select_one(
                                "a.video-thumb__title, "
                                "span.video-thumb__title-text, [title], h4, p"
                            )
                            if item.name != "a"
                            else item
                        )
                        title = ""
                        if title_tag:
                            title = title_tag.get("title") or title_tag.get_text(
                                strip=True
                            )
                        if (
                            not title
                            or title == vid_id
                            or "xhamster" in title.lower()
                        ):
                            title = a_tag.get("title") or vid_id.replace(
                                "-", " "
                            ).title()

                        if any(
                            bad in title.lower()
                            for bad in ["sponsor", "promo", "ad/"]
                        ):
                            continue
                        if is_invalid_title(title):
                            continue
                        seen.add(full_url)

                        thumb = ""
                        container = (
                            item
                            if item.name != "a"
                            else (
                                item.parent.parent
                                if item.parent
                                else item
                            )
                        )

                        for noscript in container.select("noscript"):
                            ns_match = re.search(
                                r"https?://[^\s<>\"']+\.(?:xhcdn|phncdn)\.com"
                                r"[^\s<>\"']+(?:\.jpg|\.jpeg|\.png|\.webp)",
                                noscript.text,
                            )
                            if ns_match:
                                thumb = clean_thumbnail_url(ns_match.group(0))
                                break

                        if not thumb:
                            for tag in container.select("img, source"):
                                for attr in [
                                    "data-srcset",
                                    "srcset",
                                    "data-src",
                                    "data-lazy-src",
                                    "data-original",
                                    "data-thumb",
                                    "data-image",
                                    "src",
                                ]:
                                    val = tag.get(attr, "")
                                    if val and (
                                        "xhcdn" in val or "phncdn" in val
                                    ):
                                        cleaned = clean_thumbnail_url(val)
                                        if cleaned.startswith("http"):
                                            thumb = cleaned
                                            break
                                if thumb:
                                    break

                        if not thumb:
                            for attr in [
                                "data-thumb",
                                "data-preview",
                                "data-poster",
                                "data-image",
                                "data-src",
                            ]:
                                val = container.get(attr, "")
                                if val and val.startswith("http"):
                                    thumb = clean_thumbnail_url(val)
                                    break

                        if not thumb:
                            container_html = str(container)
                            matches = re.findall(
                                r"https?://[^\s<>\"']+(?:xhcdn|phncdn)\.com[^\s<>\"']+",
                                container_html,
                            )
                            for candidate in matches:
                                if any(
                                    ext in candidate.lower()
                                    for ext in [
                                        ".jpg",
                                        ".jpeg",
                                        ".png",
                                        ".webp",
                                    ]
                                ) and not any(
                                    bad in candidate.lower()
                                    for bad in [
                                        "logo",
                                        "svg",
                                        "avatar",
                                        "pixel",
                                        "icon",
                                    ]
                                ):
                                    thumb = (
                                        candidate.split('"')[0]
                                        .split("'")[0]
                                        .split(" ")[0]
                                    )
                                    break

                        videos.append(
                            {
                                "vkey": vid_id,
                                "title": html_parser.unescape(title),
                                "thumbnail": thumb,
                                "url": full_url,
                                "provider": "xhamster",
                            }
                        )
                        if len(videos) >= 48:
                            break

                    missing_thumbs = [
                        v for v in videos if not v.get("thumbnail")
                    ]
                    if missing_thumbs:

                        def resolve_thumb(v):
                            try:
                                meta = parse_metadata_fallback(
                                    v["url"], "xhamster"
                                )
                                if meta.get("thumbnail"):
                                    v["thumbnail"] = meta["thumbnail"]
                                if meta.get("title") and not is_invalid_title(
                                    meta["title"]
                                ):
                                    v["title"] = meta["title"]
                            except Exception:
                                pass

                        with ThreadPoolExecutor(
                            max_workers=min(len(missing_thumbs), 30)
                        ) as pool:
                            list(pool.map(resolve_thumb, missing_thumbs))

                    videos = [
                        v for v in videos if not is_invalid_title(v["title"])
                    ]

                elif provider == "redtube":
                    items = soup.select(
                        "div.videoBox, li.videoblock, div.video-item, "
                        "div.pb-card, div.video-tile, "
                        'div[class*="video"], a[href*="/"]'
                    )
                    for item in items:
                        full_url, title, thumb = "", "", ""
                        if item.name == "a":
                            href = item.get("href", "")
                            if not href or any(
                                b in href
                                for b in [
                                    "search=",
                                    "/hot",
                                    "/join",
                                    "/channels/",
                                    "/pornstar/",
                                    "/amateur/",
                                    "/information/",
                                                                        "adtng.com",
                                    "trafficjunky.net",
                                ]
                            ):
                                continue
                            parts = [p for p in href.split("/") if p]
                            if not parts or not parts[-1].isdigit():
                                continue
                            full_url = (
                                href
                                if href.startswith("http")
                                else f"https://www.redtube.com{href}"
                            )
                            title = (
                                item.get("title")
                                or item.get("alt")
                                or item.get_text(strip=True)
                            )
                            img_tag = item.select_one("img")
                            if img_tag:
                                if is_invalid_title(title):
                                    title = img_tag.get("alt") or title
                                thumb = clean_thumbnail_url(
                                    img_tag.get("data-src")
                                    or img_tag.get("src")
                                    or img_tag.get("data-lazy-src")
                                    or img_tag.get("data-image")
                                    or img_tag.get("data-thumb")
                                    or ""
                                )
                        else:
                            a_tag = item.select_one("a[href]")
                            if not a_tag:
                                continue
                            href = a_tag.get("href", "")
                            if not href or any(
                                b in href
                                for b in [
                                    "search=",
                                    "/hot",
                                    "/join",
                                    "/channels/",
                                    "/pornstar/",
                                    "/amateur/",
                                    "/information/",
                                                                        "adtng.com",
                                    "trafficjunky.net",
                                ]
                            ):
                                continue
                            parts = [p for p in href.split("/") if p]
                            if not parts or not parts[-1].isdigit():
                                continue
                            full_url = (
                                href
                                if href.startswith("http")
                                else f"https://www.redtube.com{href}"
                            )

                            title_tag = item.select_one(
                                ".video-title-text, a[title], span.title, "
                                "a, p, h3, h4"
                            )
                            if title_tag:
                                title = title_tag.get(
                                    "title"
                                ) or title_tag.get_text(strip=True)
                            if is_invalid_title(title):
                                title = a_tag.get("title", "")

                            img_tag = item.select_one("img")
                            if img_tag:
                                if is_invalid_title(title):
                                    title = img_tag.get("alt") or title
                                thumb = clean_thumbnail_url(
                                    img_tag.get("data-src")
                                    or img_tag.get("src")
                                    or img_tag.get("data-lazy-src")
                                    or img_tag.get("data-image")
                                    or img_tag.get("data-thumb")
                                    or ""
                                )

                        full_url = full_url.split("?")[0].rstrip("/")
                        if not full_url or full_url in seen:
                            continue
                        seen.add(full_url)

                        vid_parts = [p for p in full_url.split("/") if p]
                        vid_id = vid_parts[-1] if vid_parts else "unknown"

                        if not vid_id.isdigit():
                            continue
                        if any(
                            bad in full_url.lower() or bad in title.lower()
                            for bad in [
                                "/join",
                                "sponsor",
                                "promo",
                                "ad/",
                                "adtng",
                                "trafficjunky",
                            ]
                        ):
                            continue

                        videos.append(
                            {
                                "vkey": vid_id,
                                "title": html_parser.unescape(title),
                                "thumbnail": thumb,
                                "url": full_url,
                                "provider": "redtube",
                            }
                        )
                        if len(videos) >= 48:
                            break

                    def resolve_redtube(v):
                        try:
                            meta = parse_metadata_fallback(v["url"], "redtube")
                            if meta.get("thumbnail") and (
                                not v["thumbnail"]
                                or "rdtcdn.com" not in v["thumbnail"]
                            ):
                                v["thumbnail"] = meta["thumbnail"]
                            if meta.get("title") and not is_invalid_title(
                                meta["title"]
                            ):
                                v["title"] = meta["title"]
                        except Exception:
                            pass

                    missing_rdt = [
                        v
                        for v in videos
                        if not v.get("thumbnail")
                        or is_invalid_title(v["title"])
                    ]
                    if missing_rdt:
                        with ThreadPoolExecutor(
                            max_workers=min(len(missing_rdt), 30)
                        ) as pool:
                            list(pool.map(resolve_redtube, missing_rdt))

                    videos = [
                        v for v in videos if not is_invalid_title(v["title"])
                    ]

                elif provider == "xnxx":
                    items = soup.select("div.mozaique div.thumb-block")
                    for item in items:
                        a_tag = item.select_one(
                            'a[href*="/video-"], a[href*="/video."]'
                        )
                        if not a_tag:
                            continue
                        href = a_tag.get("href", "")
                        full_url = (
                            f"https://www.xnxx.com{href}"
                            if href.startswith("/")
                            else href
                        )

                        vid_id = (
                            href.split("/")[1]
                            if len(href.split("/")) > 1
                            else href
                        )
                        title_tag = item.select_one(
                            "div.thumb-under a[title], div.thumb-under a"
                        )
                        title = (
                            title_tag.get("title")
                            or title_tag.get_text(strip=True)
                            if title_tag
                            else "Unknown Video"
                        )

                        if any(
                            bad in full_url.lower() or bad in title.lower()
                            for bad in ["/join", "sponsor", "promo", "ad/"]
                        ):
                            continue
                        if is_invalid_title(title):
                            continue
                        if full_url in seen:
                            continue
                        seen.add(full_url)

                        img_tag = item.select_one("img")
                        thumb = (
                            clean_thumbnail_url(
                                img_tag.get("data-src")
                                or img_tag.get("src")
                                or ""
                            )
                            if img_tag
                            else ""
                        )

                        videos.append(
                            {
                                "vkey": vid_id,
                                "title": html_parser.unescape(title),
                                "thumbnail": thumb,
                                "url": full_url,
                                "provider": "xnxx",
                            }
                        )
                        if len(videos) >= 48:
                            break

                elif provider == "xvideos":
                    items = soup.select("div.mozaique div.thumb-block")
                    for item in items:
                        a_tag = item.select_one(
                            'p.title a, a[href*="/video."]'
                        )
                        if not a_tag:
                            continue
                        href = a_tag.get("href", "")
                        vid_id = (
                            href.split("/")[1]
                            if len(href.split("/")) > 1
                            else href
                        )
                        clean_href = href.rstrip("/")
                        if (
                            clean_href.endswith("_")
                            or clean_href.endswith("/_")
                            or len(clean_href.split("/")) < 3
                        ):
                            clean_href = f"/{vid_id}/video_stream"
                        full_url = (
                            f"https://www.xvideos.com{clean_href}"
                            if clean_href.startswith("/")
                            else clean_href
                        )

                        title_tag = item.select_one("p.title a")
                        title = (
                            title_tag.get("title")
                            or title_tag.get_text(strip=True)
                            if title_tag
                            else "Unknown Video"
                        )

                        if any(
                            bad in full_url.lower() or bad in title.lower()
                            for bad in ["/join", "sponsor", "promo", "ad/"]
                        ):
                            continue
                        if is_invalid_title(title):
                            continue
                        if full_url in seen:
                            continue
                        seen.add(full_url)

                        img_tag = item.select_one("img")
                        thumb = (
                            clean_thumbnail_url(
                                img_tag.get("data-src")
                                or img_tag.get("src")
                                or ""
                            )
                            if img_tag
                            else ""
                        )

                        videos.append(
                            {
                                "vkey": vid_id,
                                "title": html_parser.unescape(title),
                                "thumbnail": thumb,
                                "url": full_url,
                                "provider": "xvideos",
                            }
                        )
                        if len(videos) >= 48:
                            break

                if len(videos) > 0:
                    search_cache[cache_key] = videos
                    return videos
        except Exception as e:
            logger.error(
                f"Search provider {provider} attempt {attempt+1} error: {e}"
            )
            time.sleep(1.0)

    if provider == "youporn":
        videos = search_youporn_with_ytdlp(q, page)
    elif provider == "xhamster":
        videos = search_xhamster_with_ytdlp(q, page)
    elif provider == "redtube":
        videos = search_redtube_with_ytdlp(q, page)

    search_cache[cache_key] = videos
    return videos


def collect_video_qualities(formats) -> list:
    """Return one stable, playable stream per reported video height, highest first.

    HLS and direct HTTP formats are both eligible. When a provider exposes the
    same height through both, prefer HLS; otherwise retain the direct format.
    This avoids silently dropping MP4-only resolutions when any HLS format is
    present, and makes the player's first/default option deterministic.
    """
    best_by_height = {}
    for item in formats or []:
        if not isinstance(item, dict):
            continue
        media_url = str(item.get("url") or "").strip()
        if not media_url or item.get("vcodec") == "none":
            continue
        protocol = str(item.get("protocol") or "").lower()
        ext = str(item.get("ext") or "").lower()
        format_id = str(item.get("format_id") or "").lower()
        format_note = str(item.get("format_note") or "").lower()
        resolution = str(item.get("resolution") or "").lower()
        is_hls = "m3u8" in protocol or ext == "m3u8" or ".m3u8" in media_url.lower() or "hls" in format_id
        if not (is_hls or protocol.startswith("http") or ext == "mp4"):
            continue

        height = item.get("height")
        try:
            height = int(height) if height else 0
        except (TypeError, ValueError):
            height = 0
        if height <= 0:
            # Resolution values often look like 1920x1080, where the second
            # dimension is the video's height, not the first.
            dimensions = re.search(r"\b\d{3,5}\s*[x×]\s*(\d{3,5})\b", resolution)
            if dimensions:
                height = int(dimensions.group(1))
            else:
                # Prefer explicit resolution fields, then common format labels.
                for candidate in (resolution, format_note, format_id):
                    match = re.search(r"(?<!\d)(\d{3,5})\s*[pP]?", candidate)
                    if match:
                        parsed = int(match.group(1))
                        if 120 <= parsed <= 10000:
                            height = parsed
                            break
        if height <= 0:
            continue

        label = f"{height}p"
        candidate = {
            "quality": label,
            "url": media_url,
            "type": "hls" if is_hls else "mp4",
            "height": height,
        }
        prior = best_by_height.get(height)
        # Prefer HLS where available for adaptive playlist support. Keep the
        # first stream of a given type so yt-dlp's provider ordering is stable.
        if prior is None or (is_hls and prior["type"] != "hls"):
            best_by_height[height] = candidate

    return [
        {k: v for k, v in item.items() if k != "height"}
        for _, item in sorted(best_by_height.items(), reverse=True)
    ]


def extract_with_ytdlp(url: str) -> dict:
    if url in extraction_cache:
        return extraction_cache[url]

    provider = "xvideos"
    if "pornhub.com" in url:
        provider = "pornhub"
    elif "xhamster.com" in url:
        provider = "xhamster"
    elif "xnxx.com" in url:
        provider = "xnxx"
    elif "xvideos.com" in url:
        provider = "xvideos"
    elif "redtube.com" in url:
        provider = "redtube"
    elif "youporn.com" in url:
        provider = "youporn"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "format": "bestvideo+bestaudio/best",
        "nocheckcertificate": True,
        "age_limit": 21,
        "http_headers": get_dynamic_headers(url),
    }

    max_retries = 3
    last_error = "Unknown Error"

    for attempt in range(max_retries):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            title = html_parser.unescape(info.get("title", ""))
            duration = info.get("duration", 0)

            extra_meta = parse_metadata_fallback(url, provider)

            if (
                not title
                or is_invalid_title(title)
                or (len(title) <= 8 and title.isalnum())
            ):
                if extra_meta.get("title") and not is_invalid_title(
                    extra_meta.get("title")
                ):
                    title = extra_meta.get("title")
            if not title or is_invalid_title(title):
                title = "Unknown Video"

            all_thumbs = []
            safe_thumb = extra_meta.get("thumbnail", "")
            if safe_thumb and not safe_thumb.startswith("data:image"):
                all_thumbs.append(clean_thumbnail_url(safe_thumb))

            if info.get("thumbnail") and info.get("thumbnail") not in all_thumbs:
                all_thumbs.append(clean_thumbnail_url(info.get("thumbnail")))

            for t in info.get("thumbnails", []):
                t_url = clean_thumbnail_url(t.get("url", ""))
                if t_url and t_url not in all_thumbs:
                    all_thumbs.append(t_url)

            clean_thumbs = [
                t
                for t in all_thumbs
                if "hash=" not in t
                and "validto=" not in t
                and "hdnea=" not in t
                and "svg" not in t
                and "logo.jpg" not in t
            ]

            if clean_thumbs:
                thumbnail = clean_thumbs[0]
                thumbnails = clean_thumbs
            else:
                thumbnail = all_thumbs[0] if all_thumbs else ""
                thumbnails = all_thumbs

            qualities = collect_video_qualities(info.get("formats", []))

            if not qualities:
                last_error = "No valid streams found"
                if attempt < max_retries - 1:
                    time.sleep(1.0)
                    continue
                return {"status": "error", "error": last_error, "url": url}

            result = {
                "status": "success",
                "title": title,
                "thumbnail": thumbnail,
                "thumbnails": thumbnails,
                "duration": duration,
                "streams": {"qualities": qualities},
                "url": url,
                "provider": provider,
            }

            extraction_cache[url] = result
            return result

        except Exception as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                time.sleep(1.0)
                continue

    return {
        "status": "error",
        "error": f"Failed after {max_retries} retries: {last_error}",
        "url": url,
    }


def disabled_providers() -> set:
    """Optional server-side lock list (DISABLED_PROVIDERS=pornhub,xnxx). The Worker admin panel is the main switch."""
    return {p.strip().lower() for p in os.getenv("DISABLED_PROVIDERS", "").split(",") if p.strip()}


@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1, provider: str = "xvideos"):
    q = (q or "trending").strip()[:100] or "trending"
    page = max(1, min(int(page or 1), 500))
    provider = (provider or "xvideos").strip().lower()
    if provider not in RUNTIME_PROVIDERS and provider not in ("pornhub", "xvideos", "xnxx", "xhamster", "redtube", "youporn"):
        provider = "xvideos"
    if provider in disabled_providers():
        return JSONResponse({"status": "error", "error": f"{provider} is locked", "locked": True}, status_code=423)
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        thread_pool, search_provider_robust, provider, q, page
    )
    if isinstance(res, list):
        res = [normalize_video_item(v) for v in res]
    return JSONResponse(res)


@app.get("/api/extract")
async def extract_endpoint(url: str):
    if not url:
        return JSONResponse({"status": "error", "error": "Missing URL"})
    target_url = url.strip()
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        thread_pool, extract_with_ytdlp, target_url
    )
    return JSONResponse(res)


@app.get("/proxy-image")
async def fallback_proxy_image(url: str):
    target = url.strip()
    if target.startswith("//"):
        target = "https:" + target

    headers = get_dynamic_headers(target)

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(
                timeout=10.0, follow_redirects=True
            ) as client:
                req = await client.get(target, headers=headers)
                if req.status_code == 200:
                    return StreamingResponse(
                        (chunk async for chunk in req.aiter_bytes()),
                        status_code=req.status_code,
                        headers={
                            "Content-Type": req.headers.get(
                                "Content-Type", "image/jpeg"
                            ),
                            "Access-Control-Allow-Origin": "*",
                            "Cache-Control": "public, max-age=86400",
                        },
                    )
        except Exception:
            await asyncio.sleep(0.5)
    return Response(status_code=404)


@app.get("/proxy-m3u8")
async def proxy_m3u8(
    request: Request,
    url: str,
    sig: str = "",
    exp: str = "",
    request_host: str = "",
):
    target = url.strip()
    headers = get_dynamic_headers(target)

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(
                timeout=10.0, follow_redirects=True
            ) as client:
                resp = await client.get(target, headers=headers)
                if resp.status_code == 200:
                    parsed_target = urlparse(target)
                    base_path = target.split("?")[0].rsplit("/", 1)[0] + "/"
                    target_query = parsed_target.query

                    proto = request.headers.get("x-forwarded-proto", "https")
                    if not request_host:
                        request_host = request.headers.get("host", "")

                    lines = resp.text.split("\n")
                    rewritten = []
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("#"):
                            if "URI=" in line:
                                match = re.search(r'URI="([^"]+)"', line)
                                if match:
                                    uri = match.group(1)
                                    abs_uri = (
                                        uri
                                        if uri.startswith("http")
                                        else base_path + uri
                                    )
                                    if target_query and "?" not in abs_uri:
                                        abs_uri += "?" + target_query
                                    next_endpoint = (
                                        "/proxy-m3u8"
                                        if ".m3u8" in abs_uri
                                        else "/proxy-video"
                                    )
                                    new_uri = (
                                        f"{proto}://{request_host}{next_endpoint}"
                                        f"?url={quote(abs_uri)}&sig={sig}&exp={exp}"
                                        f"&request_host={request_host}"
                                    )
                                    line = line.replace(
                                        f'URI="{uri}"', f'URI="{new_uri}"'
                                    )
                            rewritten.append(line)
                        else:
                            abs_uri = (
                                line
                                if line.startswith("http")
                                else base_path + line
                            )
                            if target_query and "?" not in abs_uri:
                                abs_uri += "?" + target_query
                            next_endpoint = (
                                "/proxy-m3u8"
                                if ".m3u8" in abs_uri
                                else "/proxy-video"
                            )
                            new_uri = (
                                f"{proto}://{request_host}{next_endpoint}"
                                f"?url={quote(abs_uri)}&sig={sig}&exp={exp}"
                                f"&request_host={request_host}"
                            )
                            rewritten.append(new_uri)

                    return Response(
                        content="\n".join(rewritten),
                        media_type="application/vnd.apple.mpegurl",
                        headers={
                            "Access-Control-Allow-Origin": "*",
                            "Cache-Control": "no-cache, no-store",
                        },
                    )
        except Exception:
            await asyncio.sleep(0.5)
    return Response(status_code=502, content="Backend Proxy Error")


@app.get("/proxy-video")
async def proxy_video(
    request: Request,
    url: str,
    sig: str = "",
    exp: str = "",
    request_host: str = "",
):
    target = url.strip()
    headers = get_dynamic_headers(target, request.headers)

    for attempt in range(3):
        try:
            client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
            req = client.build_request("GET", target, headers=headers)
            resp = await client.send(req, stream=True)
            if resp.status_code in [200, 206]:
                resp_headers = {
                    "Access-Control-Allow-Origin": "*",
                    "Accept-Ranges": "bytes",
                    "Content-Type": "video/mp4",
                }
                for k in ["Content-Type", "Content-Length", "Content-Range"]:
                    if k in resp.headers and resp.headers[k]:
                        resp_headers[k] = resp.headers[k]

                async def stream_generator():
                    try:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            yield chunk
                    finally:
                        await resp.aclose()
                        await client.aclose()

                return StreamingResponse(
                    stream_generator(),
                    status_code=resp.status_code,
                    headers=resp_headers,
                )
        except Exception:
            await asyncio.sleep(0.5)
    return Response(status_code=502)


@app.get("/")
def health():
    return {"status": "Online", "engine": "Fast Edge Extraction Engine"}
