"""
Launcher for the Vexo core.

main.py stays untouched. This file:
  1. swaps the two caches for working ones. TTLCache(maxsize=0) raises
     "value too large" on every write, which breaks search and extraction.
  2. sends yt-dlp, search scraping and the stream proxies through EGRESS_PROXY
     (the exit picked by scripts/egress.sh), so the IP that extracts a stream
     is the same IP that fetches it.
  3. fixes "HTTP Error 410: Gone" from sites. They answer 410 to anything that
     does not look like a real browser at the TLS level (yt-dlp issues #16729,
     #17642). Every outbound request now impersonates Chrome through curl_cffi,
     and extraction rotates impersonation targets when it still gets 410/403.
  4. returns MP4 fallbacks next to HLS, so the player can switch when a
     manifest gets blocked mid-play.
  5. adds /api/relay (which relay leg is serving) and /api/diag (can this exit
     actually reach each provider right now).

Run:  uvicorn server:app --host 0.0.0.0 --port 8000
Env:  EGRESS_PROXY        socks5://... or http://...   (set by scripts/relay.sh)
      IMPERSONATE         chrome (default) | safari | edge | off
      IMPERSONATE_ROTATE  chrome,safari,edge,chrome-131  targets tried on 410/403
      SEARCH_CACHE_TTL / EXTRACT_CACHE_TTL   seconds, 0 = no cache
"""
import asyncio
import logging
import os
import re
import threading
import time
from urllib.parse import quote, urljoin, urlparse

from cachetools import TTLCache

import main
from fastapi import HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from onlyfans_models import GENDERS, scrape_listing, scrape_profile

def _load_runtime_providers():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "providers.txt")
    provs = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("|")
                    if len(parts) >= 3:
                        provs[parts[0].strip()] = parts[2].rstrip("/") + "/"
        except Exception:
            pass
    return provs

_DYNAMIC_PROVIDERS = _load_runtime_providers()


log = logging.getLogger("vexo")

EGRESS = os.getenv("EGRESS_PROXY", "").strip()
IMPERSONATE = os.getenv("IMPERSONATE", "chrome").strip().lower()
IMP_ON = IMPERSONATE not in ("", "0", "off", "none", "false")
ROTATE = [t.strip() for t in os.getenv("IMPERSONATE_ROTATE", "chrome,safari,edge,chrome-131").split(",") if t.strip()]
BLOCK_CODES = ("HTTP Error 410", "HTTP Error 403", "HTTP Error 412", "HTTP Error 429")


# ---------------------------------------------------------------- 1. caches
class _NoCache(dict):
    """Cache that never stores anything (used when a TTL is set to 0)."""

    def __setitem__(self, key, value):
        return None


def _make_cache(ttl: int, size: int):
    if ttl > 0 and size > 0:
        return TTLCache(maxsize=size, ttl=ttl)
    return _NoCache()


main.search_cache = _make_cache(int(os.getenv("SEARCH_CACHE_TTL", "180")), 2048)
main.extraction_cache = _make_cache(int(os.getenv("EXTRACT_CACHE_TTL", "600")), 1024)


# ---------------------------------------------------------------- curl_cffi (browser TLS fingerprint)
try:  # optional: without it everything still works, just without impersonation
    from curl_cffi import requests as cffi_requests  # type: ignore
except Exception:  # pragma: no cover
    cffi_requests = None

CFFI = bool(cffi_requests) and IMP_ON
_PROXIES = {"http": EGRESS, "https": EGRESS} if EGRESS else None


@main.app.get("/api/models")
async def onlyfans_models(gender: str = "Female", sort: str = "popularity", page: int = 1, search: str = "", q: str = ""):
    """Return BabeWiki profiles from the selected popularity-sorted gender list."""
    if gender not in GENDERS:
        raise HTTPException(status_code=400, detail="gender must be Female, Male, or Female,Male")
    if sort != "popularity":
        raise HTTPException(status_code=400, detail="Only popularity sorting is supported")
    page = max(1, min(int(page or 1), 500))
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(
            main.thread_pool, scrape_listing, gender, page, main.requests.get, (search or q)
        )
        return data
    except Exception as exc:
        log.exception("BabeWiki listing scrape failed")
        raise HTTPException(status_code=502, detail=f"Could not load BabeWiki performers: {str(exc)[:180]}")


@main.app.get("/api/models/{slug}")
async def onlyfans_model_profile(slug: str):
    """Return the public profile details for one BabeWiki performer."""
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(
            main.thread_pool, scrape_profile, slug, main.requests.get
        )
        if not data:
            raise HTTPException(status_code=404, detail="Performer profile not found")
        return data
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid performer slug")
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("BabeWiki profile scrape failed for %s", slug)
        raise HTTPException(status_code=502, detail=f"Could not load BabeWiki profile: {str(exc)[:180]}")


def _no_ua(headers):
    """Impersonation sends a matching User-Agent; a foreign one breaks the fingerprint."""
    return {k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"}


def _cffi_target(name: str) -> str:
    """yt-dlp writes chrome-131, curl_cffi wants chrome131."""
    return (name or "chrome").replace("-", "")


# ---------------------------------------------------------------- 2+3. yt-dlp: egress + impersonation
_BaseYDL = main.yt_dlp.YoutubeDL
_tls = threading.local()          # per extraction thread: forced target + last info dict
_imp_ok = {}                      # target -> bool (available on this machine)
_imp_lock = threading.Lock()


def _imp_obj(name: str):
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget  # type: ignore
        return ImpersonateTarget.from_str(name)
    except Exception:
        return None


def _imp_available(name: str, params: dict) -> bool:
    with _imp_lock:
        if name in _imp_ok:
            return _imp_ok[name]
        ok = False
        tgt = _imp_obj(name)
        if tgt is not None:
            try:
                _BaseYDL({"quiet": True, "no_warnings": True, "impersonate": tgt})
                ok = True
            except Exception as e:
                log.warning("impersonate %s unavailable: %s", name, e)
        _imp_ok[name] = ok
        return ok


class VexoYDL(_BaseYDL):
    def __init__(self, params=None, *args, **kwargs):
        params = dict(params or {})
        if EGRESS:
            params.setdefault("proxy", EGRESS)
        target = getattr(_tls, "target", None) or (IMPERSONATE if IMP_ON else None)
        if target and target != "off" and "impersonate" not in params and _imp_available(target, params):
            params["impersonate"] = _imp_obj(target)
            if params.get("http_headers"):
                params["http_headers"] = _no_ua(params["http_headers"])
        super().__init__(params, *args, **kwargs)

    def extract_info(self, *args, **kwargs):
        info = super().extract_info(*args, **kwargs)
        _tls.info = info
        return info


main.yt_dlp.YoutubeDL = VexoYDL


# ---------------------------------------------------------------- search scraping (main.requests.get)
_real_requests = main.requests if hasattr(main, "requests") else None


class _RequestsShim:
    """main.py calls requests.get(url, headers=..., timeout=...) to scrape search pages.
    Same call, but with a Chrome TLS fingerprint and through the egress."""

    def __getattr__(self, name):
        return getattr(_real_requests, name)

    def get(self, url, headers=None, timeout=10, **kw):
        if CFFI:
            try:
                r = cffi_requests.get(
                    url, headers=_no_ua(headers), timeout=max(float(timeout or 10), 3.0),
                    impersonate=_cffi_target(IMPERSONATE), proxies=_PROXIES,
                    allow_redirects=True, verify=False,
                )
                if r.status_code < 400:
                    return r
                log.info("cffi %s -> %s, retrying plain", url[:80], r.status_code)
            except Exception as e:
                log.info("cffi get failed %s: %s", url[:80], e)
        return _real_requests.get(url, headers=headers, timeout=timeout, **kw)


if _real_requests is not None:
    main.requests = _RequestsShim()


# ---------------------------------------------------------------- 4. extraction with rotation + mp4 fallbacks
def _mp4_fallbacks(info) -> list:
    out, seen = [], set()
    for f in (info or {}).get("formats", []) or []:
        url = f.get("url") or ""
        proto = str(f.get("protocol", "")).lower()
        if not url or f.get("vcodec") == "none" or "m3u8" in proto or ".m3u8" in url:
            continue
        if not proto.startswith("http"):
            continue
        h = f.get("height")
        if not h:
            m = re.search(r"(\d{3,4})p?", f"{f.get('format_id', '')} {f.get('format_note', '')}")
            h = int(m.group(1)) if m else 0
        label = f"{h}p" if h else "MP4"
        if label in seen:
            continue
        seen.add(label)
        out.append({"quality": label, "url": url, "type": "mp4", "h": h})
    out.sort(key=lambda x: x["h"], reverse=True)
    for x in out:
        x.pop("h", None)
    return out


def extract(url: str) -> dict:
    targets = [None]
    if IMP_ON:
        targets += [t for t in ROTATE if t != IMPERSONATE and _imp_available(t, {})]
    res = {"status": "error", "error": "not started", "url": url}
    for i, tgt in enumerate(targets):
        _tls.target = tgt
        _tls.info = None
        try:
            res = main.extract_with_ytdlp(url)
        finally:
            _tls.target = None
        if res.get("status") == "success":
            fb = _mp4_fallbacks(getattr(_tls, "info", None))
            have = {q.get("url") for q in res.get("streams", {}).get("qualities", [])}
            res.setdefault("streams", {})["fallback"] = [f for f in fb if f["url"] not in have]
            res["impersonate"] = tgt or (IMPERSONATE if IMP_ON else "off")
            return res
        err = str(res.get("error", ""))
        if not any(c in err for c in BLOCK_CODES):
            break
        log.warning("extract blocked (%s) with %s, rotating", err[-60:], tgt or IMPERSONATE)
    err = str(res.get("error", ""))
    if "410" in err:
        res["hint"] = ("The provider refused this exit (410 Gone). The engine already retried with "
                       "browser impersonation. Set an UPSTREAM_PROXY / WG_CONFIG secret outside the US.")
    return res


# ---------------------------------------------------------------- HTTP fetch for the stream proxies
from fastapi import Request, Response  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

try:
    import httpx  # noqa: E402
except Exception:  # pragma: no cover
    httpx = None

try:
    from curl_cffi.requests import AsyncSession  # type: ignore
except Exception:  # pragma: no cover
    AsyncSession = None

app = main.app
_OVERRIDE = {"/api/extract", "/proxy-m3u8", "/proxy-video", "/proxy-image"}
try:
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) not in _OVERRIDE]
except Exception:  # test stand-ins
    pass


class _Up:
    """One upstream response, whichever client produced it."""

    def __init__(self, status, headers, aiter, close):
        self.status, self.headers, self.aiter, self.close = status, headers, aiter, close


async def _open(url: str, headers: dict, timeout: float, stream: bool) -> _Up:
    """curl_cffi (Chrome fingerprint) first, httpx second. Returns the first 2xx, else the last answer."""
    last = None
    if AsyncSession is not None and IMP_ON:
        s = None
        try:
            s = AsyncSession(impersonate=_cffi_target(IMPERSONATE), proxies=_PROXIES, timeout=timeout, verify=False)
            r = await s.get(url, headers=_no_ua(headers), stream=stream, allow_redirects=True)

            async def close(r=r, s=s):
                try:
                    if stream:
                        await r.aclose()
                finally:
                    await s.close()

            if 200 <= r.status_code < 300:
                if stream:
                    return _Up(r.status_code, dict(r.headers), r.aiter_content(), close)
                body = r.content
                await close()

                async def once(b=body):
                    yield b

                return _Up(r.status_code, dict(r.headers), once(), _noop)
            last = r.status_code
            await close()
        except Exception as e:
            log.info("cffi open failed %s: %s", url[:80], e)
            if s is not None:
                try:
                    await s.close()
                except Exception:
                    pass
    if httpx is not None:
        client = (httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxy=EGRESS) if EGRESS
                  else httpx.AsyncClient(timeout=timeout, follow_redirects=True))  # same exit IP as yt-dlp
        try:
            resp = await client.send(client.build_request("GET", url, headers=headers), stream=True)

            async def close(resp=resp, client=client):
                try:
                    await resp.aclose()
                finally:
                    await client.aclose()

            if 200 <= resp.status_code < 300:
                return _Up(resp.status_code, dict(resp.headers), resp.aiter_bytes(65536), close)
            last = resp.status_code
            await close()
        except Exception as e:
            log.info("httpx open failed %s: %s", url[:80], e)
            await client.aclose()
    return _Up(last or 502, {}, None, _noop)


async def _noop():
    return None


async def _read(up: _Up) -> bytes:
    chunks = []
    try:
        async for c in up.aiter:
            chunks.append(c)
    finally:
        await up.close()
    return b"".join(chunks)


def _public_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme or "https").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{proto}://{host}"


def _wrap(base: str, abs_uri: str) -> str:
    ep = "/proxy-m3u8" if ".m3u8" in abs_uri.split("?")[0] else "/proxy-video"
    return f"{base}{ep}?url={quote(abs_uri, safe='')}"


def _rewrite_m3u8(text: str, target: str, base: str) -> str:
    query = urlparse(target).query
    out = []

    def absolute(u):
        a = urljoin(target, u)
        if query and "?" not in a:
            a += "?" + query
        return a

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                line = line.replace(m.group(0), f'URI="{_wrap(base, absolute(m.group(1)))}"')
            out.append(line)
        else:
            out.append(_wrap(base, absolute(line)))
    return "\n".join(out) + "\n"


CORS = {"Access-Control-Allow-Origin": "*"}

import ipaddress  # noqa: E402


def _safe_url(u: str) -> bool:
    """Block SSRF: only public http(s) hosts."""
    try:
        p = urlparse(u if not u.startswith("//") else "https:" + u)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        h = p.hostname.lower()
        if h == "localhost" or h.endswith((".local", ".internal", ".localhost")):
            return False
        try:
            ip = ipaddress.ip_address(h)
            return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
        except ValueError:
            return True
    except Exception:
        return False


def _bad_url():
    return JSONResponse({"status": "error", "error": "URL not allowed"}, status_code=400, headers=CORS)


def _locked_for(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for pid in main.disabled_providers():
        if pid in host:
            return pid
    return ""


@app.get("/api/extract")
async def extract_endpoint(url: str = ""):
    if not url:
        return JSONResponse({"status": "error", "error": "Missing URL"})
    target = url.strip()
    if not _safe_url(target):
        return _bad_url()
    locked = _locked_for(target)
    if locked:
        return JSONResponse({"status": "error", "error": f"{locked} is locked", "locked": True}, status_code=423)
    loop = asyncio.get_running_loop()
    try:
        return JSONResponse(await loop.run_in_executor(main.thread_pool, extract, target))
    except Exception as e:  # never leak a 500 with a stack trace
        log.exception("extract crashed")
        return JSONResponse({"status": "error", "error": str(e)[:300], "url": target})


@app.get("/proxy-image")
async def proxy_image(url: str):
    target = url.strip()
    if target.startswith("//"):
        target = "https:" + target
    if not _safe_url(target):
        return _bad_url()
    headers = main.get_dynamic_headers(target)
    if "babewiki" in (urlparse(target).hostname or "").lower():
        headers["Referer"] = "https://babewiki.com/"
        headers["Origin"] = "https://babewiki.com"
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    up = await _open(target, headers, 12.0, stream=False)
    if up.aiter is None:
        return Response(status_code=404, headers=CORS)
    return Response(content=await _read(up), headers={
        **CORS, "Cache-Control": "public, max-age=86400",
        "Content-Type": _h(up.headers, "content-type", "image/jpeg")})


def _h(headers: dict, name: str, default=""):
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return v
    return default


@app.get("/proxy-m3u8")
async def proxy_m3u8(request: Request, url: str):
    target = url.strip()
    if not _safe_url(target):
        return _bad_url()
    up = None
    for attempt in range(3):
        up = await _open(target, main.get_dynamic_headers(target), 15.0, stream=False)
        if up.aiter is not None:
            text = (await _read(up)).decode("utf-8", "replace")
            if "#EXTM3U" in text:
                return Response(content=_rewrite_m3u8(text, target, _public_base(request)),
                                media_type="application/vnd.apple.mpegurl",
                                headers={**CORS, "Cache-Control": "no-cache, no-store"})
        await asyncio.sleep(0.4 * (attempt + 1))
    return Response(status_code=up.status if up.status >= 400 else 502,
                    content=f"Upstream answered {up.status}", headers=CORS)


@app.get("/proxy-video")
async def proxy_video(request: Request, url: str):
    target = url.strip()
    if not _safe_url(target):
        return _bad_url()
    headers = main.get_dynamic_headers(target, dict(request.headers))
    up = None
    for attempt in range(3):
        up = await _open(target, headers, 60.0, stream=True)
        if up.aiter is not None:
            break
        await asyncio.sleep(0.4 * (attempt + 1))
    if up is None or up.aiter is None:
        return Response(status_code=(up.status if up and up.status >= 400 else 502), headers=CORS)
    out = {**CORS, "Accept-Ranges": "bytes", "Content-Type": "video/mp4",
           "Access-Control-Expose-Headers": "Content-Range, Accept-Ranges, Content-Length, Content-Type"}
    for k in ("content-type", "content-length", "content-range", "last-modified", "etag"):
        v = _h(up.headers, k)
        if v:
            out["-".join(p.capitalize() for p in k.split("-"))] = v

    async def body():
        try:
            async for chunk in up.aiter:
                yield chunk
        finally:
            await up.close()

    return StreamingResponse(body(), status_code=up.status, headers=out)


# ---------------------------------------------------------------- 5. relay + diagnostics
@app.get("/api/relay")
def relay_info():
    return {
        "relay": int(os.getenv("RELAY", "0") or 0),
        "started_at": os.getenv("RELAY_STARTED_AT", ""),
        "expires_at": os.getenv("RELAY_EXPIRES_AT", ""),
        "warp": os.getenv("WARP_STATE", "off"),
        "egress": os.getenv("EGRESS_STATE", "direct"),
        "run_url": os.getenv("RUN_URL", ""),
        "impersonate": IMPERSONATE if CFFI else "off",
    }


_DIAG = {"at": 0.0, "data": None}
_DIAG_SITES = _DYNAMIC_PROVIDERS if _DYNAMIC_PROVIDERS else {"default": "https://www.google.com/"}


def _diag_sync():
    res = {}
    for name, u in _DIAG_SITES.items():
        try:
            r = main.requests.get(u, headers=main.get_dynamic_headers(u), timeout=10)
            res[name] = r.status_code
        except Exception:
            res[name] = 0
    return res


@app.get("/api/diag")
async def diag():
    if not _DIAG["data"] or time.time() - _DIAG["at"] > 300:
        loop = asyncio.get_running_loop()
        _DIAG["data"] = await loop.run_in_executor(main.thread_pool, _diag_sync)
        _DIAG["at"] = time.time()
    return {"egress": os.getenv("EGRESS_STATE", "direct"), "impersonate": IMPERSONATE if CFFI else "off",
            "providers": _DIAG["data"], "checked_at": int(_DIAG["at"])}

@app.get("/api/providers")
def get_providers_endpoint():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "providers.txt")
    items = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split("|")
                    if len(parts) >= 3:
                        pid = parts[0].strip()
                        items.append({"id": pid, "name": parts[1].strip(), "domain": parts[2].strip(),
                                      "enabled": pid not in main.disabled_providers()})
    return {"providers": items}
