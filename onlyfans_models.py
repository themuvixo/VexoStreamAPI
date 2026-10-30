"""Small, read-only BabeWiki performer directory/profile scraper."""

from __future__ import annotations

import re
import threading
import time
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

BASE_URL = "https://babewiki.com"
GENDERS = {"Female", "Male", "Female,Male"}
POPULARITY_SORT = "popularity"
_CACHE_TTL = 300
_cache = {}
_cache_lock = threading.Lock()

_PERSONAL_FIELDS = {
    "Age": "age",
    "Born": "birth_date",
    "Gender": "gender",
    "Career": "career",
    "Country": "country",
}
_PHYSICAL_FIELDS = {
    "Height": "height",
    "Hair Color": "hair_color",
    "Eye Color": "eye_color",
    "Ethnicity": "ethnicity",
    "Measurements": "measurements",
    "Breast Type": "breast_type",
    "Piercings": "piercings",
}


def _cached(key):
    with _cache_lock:
        row = _cache.get(key)
        if row and row[0] > time.monotonic():
            return row[1]
        if row:
            _cache.pop(key, None)
    return None


def _store(key, value):
    with _cache_lock:
        _cache[key] = (time.monotonic() + _CACHE_TTL, value)
    return value


def _fetch(url, http_get):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "babewiki.com":
        raise ValueError("Refusing to fetch a non-BabeWiki URL")
    response = http_get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; VexoStreamModels/1.0; "
                "+https://babewiki.com/)"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8",
        },
        timeout=12,
    )
    response.raise_for_status()
    return response.text


def _flag_to_country_code(flag):
    points = [ord(ch) for ch in (flag or "") if 0x1F1E6 <= ord(ch) <= 0x1F1FF]
    if len(points) == 2:
        return "".join(chr(point - 0x1F1E6 + ord("A")) for point in points)
    return ""


def _absolute_image(src):
    if not src:
        return ""
    absolute = urljoin(BASE_URL + "/", src.strip())
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    trusted_host = host == "babewiki.com" or host.endswith(".babewiki.com")
    return absolute if parsed.scheme == "https" and trusted_host else ""


def _srcset_image(srcset):
    """Pick the largest usable image candidate from a srcset attribute."""
    candidates = []
    for item in (srcset or "").split(","):
        parts = item.strip().split()
        if not parts:
            continue
        score = 1.0
        if len(parts) > 1:
            match = re.fullmatch(r"(\d+(?:\.\d+)?)(w|x)", parts[-1].lower())
            if match:
                score = float(match.group(1))
                if match.group(2) == "x":
                    score *= 10000
        candidates.append((score, parts[0]))
    for _, src in sorted(candidates, reverse=True):
        absolute = _absolute_image(src)
        if absolute:
            return absolute
    return ""


def _image_url(img):
    """Resolve an image across common lazy-loading and responsive-image markup."""
    if img is None:
        return ""
    for attr in ("data-original", "data-lazy-src", "data-src", "data-srcset", "srcset"):
        value = img.get(attr)
        image = _srcset_image(value) if attr.endswith("srcset") else _absolute_image(value)
        if image:
            return image
    picture = img.find_parent("picture")
    if picture:
        for source in picture.find_all("source"):
            image = _srcset_image(source.get("data-srcset") or source.get("srcset"))
            if image:
                return image
    return _absolute_image(img.get("src"))


_FLAG_RE = re.compile("[\U0001F1E6-\U0001F1FF]")


def _strip_flags(text):
    """Remove regional-indicator flag emoji so country text never shows the flag twice."""
    return re.sub(r"\s+", " ", _FLAG_RE.sub("", text or "")).strip()


def clean_search(search):
    search = re.sub(r"\s+", " ", (search or "")).strip()
    return search[:80]


def _page_url(gender, page, search=""):
    url = (
        f"{BASE_URL}/performers?gender={quote(gender, safe='')}"
        f"&sort={POPULARITY_SORT}&page={page}"
    )
    if search:
        url += f"&search={quote(search, safe='')}"
    return url


def _parse_listing(html, gender, page):
    soup = BeautifulSoup(html, "html.parser")
    models = []
    seen = set()
    for anchor in soup.select('a[href^="/performer/"]'):
        href = (anchor.get("href") or "").strip()
        match = re.fullmatch(r"/performer/([a-zA-Z0-9][a-zA-Z0-9-]{0,119})/?", href)
        if not match:
            continue
        slug = match.group(1)
        if slug in seen:
            continue
        image = anchor.find("img")
        spans = anchor.select("span.truncate")
        name = spans[0].get_text(" ", strip=True) if spans else ""
        if not name and image:
            name = re.sub(r"'s BabeWiki Profile$", "", image.get("alt", "")).strip()
        if not name:
            continue
        flag = ""
        flag_node = anchor.select_one("span.text-sm.leading-none")
        if flag_node:
            flag = flag_node.get_text("", strip=True)
        country_name = _strip_flags(spans[1].get_text(" ", strip=True)) if len(spans) > 1 else ""
        seen.add(slug)
        models.append(
            {
                "name": name,
                "slug": slug,
                "profile_url": f"{BASE_URL}/performer/{slug}",
                "image_url": _image_url(image),
                "country_code": _flag_to_country_code(flag),
                "country_flag": flag,
                "country_name": country_name,
                "gender": gender if gender != "Female,Male" else "",
            }
        )
    next_page = False
    for link in soup.select('a[href*="page="], button[aria-label*="Next"], button[title*="Next"]'):
        label = " ".join(
            [
                link.get_text(" ", strip=True),
                link.get("aria-label", ""),
                link.get("title", ""),
            ]
        ).lower()
        link_url = urljoin(BASE_URL, link.get("href", ""))
        if "next" in label or re.search(r"[?&]page=(\d+)", link_url):
            page_match = re.search(r"[?&]page=(\d+)", link_url)
            if "next" in label or (page_match and int(page_match.group(1)) > page):
                next_page = True
                break
    return models, next_page


def scrape_listing(gender, page, http_get, search=""):
    if gender not in GENDERS:
        raise ValueError("gender must be Female, Male, or Female,Male")
    page = max(1, min(int(page or 1), 500))
    search = clean_search(search)
    key = ("listing", gender, page, search.lower())
    cached = _cached(key)
    if cached is not None:
        return cached
    html = _fetch(_page_url(gender, page, search), http_get)
    models, has_more = _parse_listing(html, gender, page)
    return _store(
        key,
        {
            "models": models,
            "gender": gender,
            "sort": POPULARITY_SORT,
            "search": search,
            "page": page,
            "has_more": has_more or len(models) >= 40,
        },
    )


def _clean_lines(soup):
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in soup.get_text("\n", strip=True).splitlines()
        if re.sub(r"\s+", " ", line).strip()
    ]


def _field_values(lines, fields):
    labels = {label.lower(): key for label, key in fields.items()}
    result = {}
    for index, line in enumerate(lines):
        clean = line.strip().rstrip(":").strip()
        key = labels.get(clean.lower())
        if not key:
            continue
        value = ""
        if ":" in line and line.split(":", 1)[0].strip().lower() in labels:
            value = line.split(":", 1)[1].strip()
        elif index + 1 < len(lines):
            value = lines[index + 1].strip()
        if value:
            result[key] = value
    age = result.get("age", "")
    born_match = re.search(r"\bBorn:\s*(.+)$", age, re.IGNORECASE)
    if born_match:
        result["birth_date"] = born_match.group(1).strip()
        result["age"] = age[: born_match.start()].strip()
    return result


def _profile_sections(soup):
    lines = _clean_lines(soup)
    personal_start = next(
        (i for i, line in enumerate(lines) if line.lower() == "personal details"),
        -1,
    )
    physical_start = next(
        (i for i, line in enumerate(lines) if line.lower() == "physical attributes"),
        -1,
    )
    if personal_start >= 0:
        personal_lines = lines[personal_start + 1 : physical_start if physical_start > personal_start else len(lines)]
    else:
        personal_lines = lines
    physical_lines = lines[physical_start + 1 :] if physical_start >= 0 else lines

    personal = _field_values(personal_lines, _PERSONAL_FIELDS)
    physical = _field_values(physical_lines, _PHYSICAL_FIELDS)

    # Some profile layouts split "Born" out of the Age value into its own row.
    if "birth_date" not in personal:
        for line in personal_lines:
            born_match = re.match(r"Born:\s*(.+)$", line, re.IGNORECASE)
            if born_match:
                personal["birth_date"] = born_match.group(1).strip()
                break
    return personal, physical


def _gallery_images(soup, slug, main_image=""):
    """Every photo in the profile carousel, in photo order, without loop clones."""
    numbered = {}
    for img in soup.find_all("img"):
        alt = img.get("alt") or ""
        match = re.search(r"'s photo (\d+)\s*$", alt, re.IGNORECASE)
        if not match:
            continue
        src = _image_url(img)
        if src:
            numbered.setdefault(int(match.group(1)), src)
    images = [numbered[n] for n in sorted(numbered)]
    if not images:
        # Fallback: any image file named after this performer.
        prefix = "/images/" + slug.lower().replace("-", "_") + "_"
        for img in soup.find_all("img"):
            src = _image_url(img)
            if src and prefix in urlparse(src).path.lower():
                images.append(src)
    if main_image:
        images.insert(0, main_image)
    out, seen = [], set()
    for src in images:
        if src not in seen:
            seen.add(src)
            out.append(src)
    return out


def scrape_profile(slug, http_get):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,119}", slug or ""):
        raise ValueError("Invalid performer slug")
    key = ("profile", slug.lower())
    cached = _cached(key)
    if cached is not None:
        return cached
    html = _fetch(f"{BASE_URL}/performer/{slug}", http_get)
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    name = heading.get_text(" ", strip=True) if heading else ""
    if not name:
        title = soup.find("title")
        name = title.get_text(" ", strip=True).split(" Wiki", 1)[0] if title else ""
    if not name:
        return None
    image = soup.select_one('img[alt$="\'s photo 1"]') or soup.select_one('img[alt*="BabeWiki Profile"], img[src*="/images/"]')
    flag_node = soup.select_one("span.text-sm.leading-none")
    flag = flag_node.get_text("", strip=True) if flag_node else ""
    personal, physical = _profile_sections(soup)
    if flag and not personal.get("country"):
        # The country text is usually the next truncated span beside the flag.
        country_node = flag_node.find_next("span", class_="truncate")
        if country_node:
            personal["country"] = country_node.get_text(" ", strip=True)
    personal = {k: _strip_flags(v) for k, v in personal.items() if _strip_flags(v)}
    physical = {k: _strip_flags(v) for k, v in physical.items() if _strip_flags(v)}
    main_image = _image_url(image)
    if not main_image:
        for meta in soup.select('meta[property="og:image"], meta[name="twitter:image"]'):
            main_image = _absolute_image(meta.get("content"))
            if main_image:
                break
    images = _gallery_images(soup, slug, main_image)
    result = {
        "name": name,
        "slug": slug,
        "profile_url": f"{BASE_URL}/performer/{slug}",
        "image_url": images[0] if images else main_image,
        "images": images,
        "country_flag": flag,
        "country_code": _flag_to_country_code(flag),
        "country_name": personal.get("country", ""),
        "personal_details": personal,
        "physical_attributes": physical,
    }
    return _store(key, result)
