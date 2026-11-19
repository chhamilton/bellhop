---
name: bellhop-web-and-daemon
description: >-
  Use this skill when modifying `bellhop serve` (`src/bellhop/serve.py`, `src/bellhop/xtream_server.py`,
  `src/bellhop_web/`), debugging browser live video playback (`mpegts.js` / `hls.js` /
  single-tuner relay), updating the TypeScript Cards, 2D Timeline, Login, or Settings UI,
  updating Alpine Docker packaging, or building the persistent internal scheduler.
---

# Bellhop Web Guide, Passthrough Xtream Server & Single-Tuner Proxy Guide

## Runtime Architecture (`src/bellhop/serve.py`, `src/bellhop/xtream_server.py`, `src/bellhop_web/`)
- **`http_utils.py` (`src/bellhop_web/http_utils.py`):** Shared `aiohttp.web` application factory (`create_web_app` with CORS, `OPTIONS` preflight, JSON error middleware, and `256 KiB` body limit), response helpers (`json_response`, `bytes_response`, `redirect_response`, `start_site`), `ClientSessionFactory` / `upstream_url`, and bind/LAN URL helpers (`detect_device_ip`, `resolve_bind_host`, `build_access_urls`).
- **`store.py` & `epg_refresh.py` (`src/bellhop_web/`):**
  - `LineupGuideStore` hot-reloads `output/xtream/` on file `mtime` changes (with M3U8 & `.xml`/`.xml.gz` fallbacks) and redacts `direct_source` from public API payloads.
  - `EpgRefreshService` dynamically fetches the passthrough `xmltv_url` in the background, parses mapped channels via `asyncio.to_thread`, evicts stale listings, and persists `cache/bellhop_web/epg_cache.json`.
- **`auth.py` (`src/bellhop_web/auth.py`):** HMAC-SHA256 signed sliding session cookies (`bellhop_session`, key persisted in `cache/bellhop_web/session_secret.key`) and `/login?next=...` redirect handling.
- **`server.py` (`WebGuideApp` & `TunerSessionManager`):**
  - `GET /api/lineup` & `GET /api/epg/<chno>` — Categories, channels, live `progress_pct`, and upcoming schedules.
  - `GET /api/stream/<chno>.ts` — Single-tuner live MPEG-TS relay (`Transfer-Encoding: chunked`) with candidate failover (`fallback_sources`, skipping `/black.ts` placeholders).
  - `GET /api/hls/<chno>/index.m3u8` & `/api/hls/<chno>/asset?u=...` — HLS manifest rewriter + segment relay with 60s edge-URL session affinity (`_hls_edge_urls`).
  - `POST /api/tuner/release?client=<id>` — Scoped client teardown so `pagehide` beacons from reloading tabs never kill the new tab's stream.
  - `GET/PUT /api/config` — Scoped operational settings editor (`upstream_xtream`, `xtream_server`, `pipeline` only; never exposes `web_server`, paths, or credentials).
- **`frontend/` & `static/` (`src/bellhop_web/frontend/`, `src/bellhop_web/static/`):**
  - Strictly typed TypeScript sources (`types.d.ts`, `prefs.ts`, `app.ts`) compiled via `tsc` into `static/prefs.js` and `static/app.js` (never edit compiled `.js` directly).
  - `prefs.ts` implements `window.BellhopSettings`, a versioned JSON `localStorage["bellhop.settings"]` store backing Dark/Light mode (`ui.theme`) and future client preferences, loaded synchronously in `<head>` on both `/` and `/login`.
  - Public pre-auth routes in `server.py` serve `/static/bellhop-logo.svg`, `/favicon.svg`, `/favicon.ico`, `/static/favicon.ico`, and `/static/prefs.js` so `/login` renders the splash logo, favicon, and Sun/Moon theme toggle before login.
- **`serve.py` & `xtream_server.py` (`src/bellhop/`):**
  - `UnifiedServeServer` co-hosts `WebGuideApp` and `XtreamPassthroughServer` on a shared port (`0.0.0.0:8080`) or optional separate ports, fulfilling `/player_api.php`, `/player.php`, `/panel_api.php`, `/get.php`, `/playlist.m3u*`, `/xmltv.php`, and `/live/<u/>/<p>/<id>[.<ext>]` `302` passthroughs.

## Critical Browser & Proxy Invariants
1. **Keep `[hidden] { display: none !important; }`** in `styles.css` (including `.settings-overlay[hidden]`, `.settings-pane[hidden]`) so flex/grid rules never override `element.hidden = true`.
2. **Always pass absolute URLs to `mpegts.createPlayer`** (`new URL(..., window.location.origin).href`) so `mpegts.js`'s `blob:` Web Worker can `fetch()` the stream.
3. **Preserve `Transfer-Encoding: chunked`** on streaming endpoints in `server.py` so reverse proxies and browser `ReadableStream`s never buffer waiting for EOF.
4. **Coordinate Tuner Access:** When adding background scheduled validation, always check `TunerSessionManager.active_info` first so scheduled probes yield immediately whenever a user is actively watching a channel!

## Next Milestone: Persistent Scheduler, Explicit Sync, Multi-Lineup & Proxied Relay
Read [persistent-daemon-blueprint.md](./references/persistent-daemon-blueprint.md) when implementing internal scheduling, explicit sync controls, and run history/progress SSE.
