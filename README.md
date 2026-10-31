# VexoStream API

Clean, headless backend API for media extraction and streaming proxy.
All Cloudflare Workers, GitHub Actions relays, decoy pages, and frontend web UIs have been completely removed.

---

## Endpoints

- `GET /` — API health check (`{"status": "Online", "engine": "Fast Edge Extraction Engine"}`)
- `GET /api/explore?q=...&page=...&provider=...` — Search & explore provider catalogue
- `GET /api/models?gender=Female&sort=popularity&page=1&search=abella` — Popular BabeWiki performer listings; `gender` accepts `Female`, `Male`, or `Female,Male`; `search` is optional
- `GET /api/models/{slug}` — Performer profile details plus every gallery photo (`images` array, in order)
- `GET /api/extract?url=...` — Video stream extraction via yt-dlp & TLS rotation
- `GET /api/providers` — List supported content providers
- `GET /proxy-m3u8?url=...` — Rewritten HLS stream proxy
- `GET /proxy-video?url=...` — MP4 direct chunk proxy
- `GET /proxy-image?url=...` — Image/thumbnail proxy
- `GET /api/diag` — Provider connectivity diagnostic report

---

## 🚀 Deployment

### 1. Railway (Netherlands / Europe-West4)
Preset via `railway.json` for region `europe-west4` (Netherlands). Push to GitHub or deploy via Railway CLI:
```bash
railway up
```

### 2. VPS / Docker / Docker Compose
```bash
docker compose up -d --build
```

Or standalone Docker:
```bash
docker build -t vexostream-api .
docker run -d -p 8000:8000 --name vexostream-api vexostream-api
```

Or directly with Python:
```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

### 3. PaaS (Render / Koyeb / Fly.io / Heroku)
Ready-to-use `Procfile` and `render.yaml` are included.

---

### Pterodactyl / Python panels

This project now supports direct execution, so set the panel startup file to `server.py` (or `PY_FILE=server.py` if the panel uses that variable). Set `REQUIREMENTS_FILE=requirements.txt`. The app binds to `0.0.0.0` and reads `PORT`, then `SERVER_PORT`, then defaults to `8000`. Make sure the port matches the panel allocation. The server logs should show Uvicorn listening on that port; if the panel still says offline, check that its allocation and `PORT`/`SERVER_PORT` agree.

### VPS with systemd

For a Linux VPS, install Python 3.12 and FFmpeg, upload the project to `/opt/vexostream`, then create a virtual environment and install dependencies:

```bash
cd /opt/vexostream
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

Keep it running in production with a systemd service (example below). Set `WorkingDirectory` and `ExecStart` to your actual project path, then run `sudo systemctl enable --now vexostream`. View logs with `sudo journalctl -u vexostream -f`.

```ini
[Unit]
Description=VexoStream API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/vexostream
Environment=PORT=8000
ExecStart=/opt/vexostream/.venv/bin/python /opt/vexostream/server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

For Docker/VPS, `docker compose up -d --build` remains the easiest option. Generic Python hosts can use `python -u server.py`; hosts that expect a Procfile are configured to do the same. Provider-specific panels may still require their own port/allocation setting.

## ⚙️ Configuration (Environment Variables)

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8000` | Port for the Uvicorn server |
| `EGRESS_PROXY` | *(empty)* | Optional upstream proxy (`socks5://...` or `http://...`) |
| `IMPERSONATE` | `chrome` | Browser TLS fingerprint impersonation (`chrome`, `safari`, `edge`, `off`) |
| `IMPERSONATE_ROTATE` | `chrome,safari,edge,chrome-131` | Fingerprint fallback targets on 403/410 errors |
| `SEARCH_CACHE_TTL` | `180` | Cache time in seconds for search queries |
| `EXTRACT_CACHE_TTL` | `600` | Cache time in seconds for stream extraction |
| `DISABLED_PROVIDERS` | *(empty)* | Optional hard lock, e.g. `pornhub,xnxx`. Explore/extract answer `423`. The Worker admin panel is the main switch. |

---

## v6 changes
- SSRF guard on `/api/extract` and every proxy (no localhost / private / metadata IPs)
- httpx fallback now uses `EGRESS_PROXY` too (same exit IP as yt-dlp)
- Search shim no longer forces an 8s minimum timeout (metadata lookups were slowing every extract)
- YouPorn yt-dlp fallback used a non-existent `ypsearch` prefix and ignored paging: fixed
- `/api/explore` validates `q`, `page`, `provider`
- Cache defaults now match this README (180s / 600s)
- Docker: non-root user, healthcheck, proxy headers; `.dockerignore`; Render/Railway health checks
- `yt-dlp` pin relaxed to a version that exists on PyPI

---

## v7 changes
- **Pornhub paging fixed.** Page 2+ returned page 1 again: the HTML search redirects some queries (channel / pornstar names like "Brazzers") to a page without `&page=N`, and side rails leaked into results. New pager:
  1. Webmasters JSON API (`/webmasters/search?search=&page=`), stable paging
  2. HTML search with verification: re-applies `page` after a redirect, checks the pagination widget, strips recommended/hot rails
  3. Every page is fingerprinted; a page that repeats an earlier page of the same query is rejected instead of shown
- The old unverified Pornhub scraper is no longer used as a fallback.
- `DISABLED_PROVIDERS` env var (optional server-side lock); `/api/providers` now reports `enabled`.
