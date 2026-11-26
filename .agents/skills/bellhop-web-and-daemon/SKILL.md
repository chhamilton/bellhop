---
name: bellhop-web-and-daemon
description: >-
  Use this skill when modifying `bellhop serve` (`src/bellhop/serve.py`, `src/bellhop/xtream/`,
  `src/bellhop/web/`, `src/bellhop/epg/`), debugging browser live video playback (`mpegts.js` /
  `hls.js` / single-tuner relay), updating the TypeScript Cards, 2D Timeline, Login, or Settings UI,
  updating Alpine Docker packaging, or building the persistent internal scheduler.
---

# Bellhop Web Guide, Passthrough Xtream Server & Single-Tuner Proxy Guide

## Runtime Architecture (`src/bellhop/{serve.py,common,epg,xtream,web}`)
- **`http_utils.py` (`src/bellhop/common/http_utils.py`):** Shared `aiohttp.web` application factory (`create_web_app` with strict security headers—including `form-action 'self'`, intentionally omitting wildcard CORS—JSON error middleware, and `256 KiB` body limit), `LoginRateLimiter` (with IPv6 `/64` normalization), `constant_time_credential_match`, response helpers (`json_response`, `bytes_response`, `redirect_response`, `start_site`), `ClientSessionFactory` / `upstream_url`, and bind/LAN URL helpers (`detect_device_ip`, `resolve_bind_host`, `build_access_urls`).
- **`web/store.py` & `epg/` (`src/bellhop/web/store.py`, `src/bellhop/epg/`):**
  - `LineupGuideStore` shares a cached `ExportBundleReader` (`src/bellhop/common/export_bundle.py`) with `XtreamExportStore`, hot-reloading `output/xtream/` on file `mtime` changes and redacting `direct_source` from public API payloads without retaining bulk EPG listings in Python RAM.
  - `EpgDatabase` (`src/bellhop/epg/db.py`) manages the embedded WAL-mode SQLite 3 database (`cache/epg.sqlite3`), serving indexed range queries for `/api/lineup`, `/api/epg/<chno>`, and Xtream `get_short_epg` / `get_simple_data_table`, plus keyset-paginated `64 KB` cursor streaming for `/xmltv.php`.
  - `EpgRefreshService` (`src/bellhop/epg/refresh.py`) & `SchedulesDirectEpgClient` (`src/bellhop/epg/sd_epg.py` via shared `SchedulesDirectSession` in `sd_client.py`) dynamically sync Schedules Direct (`20141201` MD5 differential) + passthrough XMLTV backfill in the background and commit reconciled schedules into `epg.sqlite3`.
- **`auth.py` (`src/bellhop/web/auth.py`):** Mandatory HMAC-SHA256 signed sliding session cookies (`bellhop_session`, signing key persisted in `cache/session_secret.key` and explicit/grace-window revocations persisted in `cache/session_revoked.json`, 30s grace on sliding renewal), and internal-only `/login?next=...` redirect handling.
- **`server.py` (`src/bellhop/web/server.py` — `WebGuideApp` & `TunerSessionManager`):**
  - `GET /api/lineup` & `GET /api/epg/<chno>` — Categories, channels, live `progress_pct`, and upcoming schedules.
  - `GET /api/stream/<chno>.ts` — Single-tuner live MPEG-TS relay (`Transfer-Encoding: chunked`) with candidate failover (`fallback_sources`, skipping `/black.ts` placeholders).
  - `GET /api/hls/<chno>/index.m3u8` & `/api/hls/<chno>/asset?u=...` — HLS manifest rewriter + opaque token-bound segment relay with 60s edge-URL session affinity (`_hls_edge_urls`).
  - `POST /api/tuner/release?client=<id>` — Scoped client teardown so `pagehide` beacons from reloading tabs never kill the new tab's stream.
  - `GET/PUT /api/config` — Scoped Tier-2 operational settings editor via `ConfigManager` (`upstream_xtream`, `pipeline` only; never exposes `web_server`, `xtream_server`, paths, or credentials).
- **`frontend/` & `static/` (`src/bellhop/web/frontend/`, `src/bellhop/web/static/`):**
  - Strictly typed TypeScript sources (`api_contracts.d.ts`, `types.d.ts`, `prefs.ts`, `login.ts`, `guide_logic.ts`, `render.ts`, `player.ts`, `settings.ts`, `app.ts`) compiled via `tsc` into `static/*.js` (never edit compiled `.js` or `api_contracts.d.ts` directly).
  - `prefs.ts` implements `window.BellhopSettings`, a versioned JSON `localStorage["bellhop.settings"]` store backing Dark/Light mode (`ui.theme`) and future client preferences, loaded synchronously in `<head>` on both `/` and `/login`.
  - Public pre-auth routes in `server.py` serve `/static/bellhop-logo.svg`, `/favicon.svg`, `/favicon.ico`, `/static/favicon.ico`, `/static/styles.css`, `/static/prefs.js`, and `/static/login.js` so `/login` renders the splash logo, favicon, and Sun/Moon theme toggle before login.
- **`serve.py` & `xtream/server.py` (`src/bellhop/`):**
  - `UnifiedServeServer` co-hosts `WebGuideApp` and `XtreamPassthroughServer` on a shared port (`0.0.0.0:8080`) or optional separate ports, fulfilling `/player_api.php`, `/player.php`, `/panel_api.php`, `/get.php`, `/playlist.m3u*`, SQLite-backed `/xmltv.php` (`302` upstream fallback only if SQLite empty), and `/live/<u/>/<p>/<id>[.<ext>]` `302` passthroughs.

## Critical Browser & Proxy Invariants
1. **Keep `[hidden] { display: none !important; }`** in `styles.css` (including `.settings-overlay[hidden]`, `.settings-pane[hidden]`) so flex/grid rules never override `element.hidden = true`.
2. **Always pass absolute URLs to `mpegts.createPlayer`** (`new URL(..., window.location.origin).href`) so `mpegts.js`'s `blob:` Web Worker can `fetch()` the stream.
3. **Preserve `Transfer-Encoding: chunked`** on streaming endpoints in `server.py` so reverse proxies and browser `ReadableStream`s never buffer waiting for EOF.
4. **Coordinate Tuner Access:** When adding background scheduled validation, always check `TunerSessionManager.active_info` first so scheduled probes yield immediately whenever a user is actively watching a channel!

## Next Milestone: Persistent Scheduler, Explicit Sync, Multi-Lineup & Proxied Relay
Read [persistent-daemon-blueprint.md](./references/persistent-daemon-blueprint.md) when implementing internal scheduling, explicit sync controls, and run history/progress SSE.
