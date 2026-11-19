# Full Engineering Handoff & Workspace State (Bellhop)

Last updated: `2026-11-19`

## 1. Executive Summary
**Bellhop** provides a stateful, config-driven Python IPTV pipeline (`bellhop run`), a unified Web Guide + Passthrough Xtream Codes server (`bellhop serve`), and Alpine Docker packaging (`Dockerfile`, `docker-compose.example.yml`), tailored initially for **Bell Fibe TV — Montréal, QC** (`CAN-BELLFIBE-MTL-X` / `TVG-9100000607`) and generalized to support **any** North American TV lineup via pluggable backends.

### Current Verified State on Disk
- **Coding Style, Strict Typing & Tests:**
  - **Python:** Google Python Style Guide (2-space indentation, spaces only / zero tabs, 80-char wrapping via `ruff`), `mypy --strict` (`0` issues across all files, `0` `# type: ignore` comments).
  - **Frontend:** Strictly typed TypeScript (`src/bellhop_web/frontend/{types.d.ts,prefs.ts,app.ts}`, `strict: true` + `noUncheckedIndexedAccess: true`) compiled to `src/bellhop_web/static/{prefs.js,app.js}` via `tsc`, with Node unit tests (`tests/frontend/prefs.test.mjs`).
  - **HTTP Stack (`aiohttp`):** Unified across both server (`aiohttp.web` in `http_utils.py`, `server.py`, `xtream_server.py`, `serve.py`) and client (`aiohttp.ClientSession` across `xtream.py`, `validator.py`, `epg_loader.py`, `epg_refresh.py`, `tvguide.py`, `schedules_direct.py`, `server.py`), replacing both `httpx` and the earlier custom `asyncio.StreamReader` HTTP parser.
  - **Docker Packaging:** Multi-stage Alpine `Dockerfile` (`node:22-alpine` → `python:3.13-alpine`), `docker/entrypoint.sh` (`TZ`, `PUID`/`PGID`, auto-seeding `/config/config.yaml`), `docker/config.docker.yaml`, `.env.example`, `docker-compose.example.yml`, and `tests/test_docker_e2e.py`.
  - **Test Suite:** `pytest -v` enforces Python unit/integration/E2E tests, `mypy --strict`, `ruff check/format`, tab-free sources, `tsc --noEmit` + compiled asset sync, Node frontend unit tests, and Docker entrypoint/container E2E tests.
- **Curated Bell Fibe Montréal Lineup (`data/lineups/bell_fibe_montreal.yaml`):**
  - **355 unique networks** across **19 Bell Fibe categories** (**698 total dial positions** across `100–899` SD and `1000+` HD; `355` slots when `collapse_sd_dupes: true`).
  - **339 / 355** networks mapped to HTTP-verified transparent PNG logos from `tv-logo/tv-logos` in `data/official_logos.json`.
- **Dynamic Lineup Backends (`src/bellhop/providers/`):**
  - **`tvguide`** (`TvGuideLineupProvider`): Free US/Canadian discovery + channel/logo retrieval (`TVG-9100000607` saved in `state/lineups/TVG-9100000607.json`).
  - **`schedules_direct`** (`SchedulesDirectLineupProvider`): Full SD-JSON `20141201` client (`state/sd_token.json`, auto re-auth, headend discovery, channel + `SdStationInfo` parser).
  - **`yaml`** (`YamlLineupProvider`): Loads local curated YAML lineups.
- **Live-Validated Channel Mapping (`state/channel_mapping.json`):**
  - Provider: `provider.example` (`max_connections: 1`, credentials in gitignored `credentials.yaml`).
  - Candidate pool narrowed from **16,622** raw streams → **940 unique candidate streams** (`94.3%` reduction).
  - **349 / 355 (`98.3%`)** networks are **live-validated (`preferred_source: "validated"`)** (`235` with verified native provider EPG; `17` 4K, `58` FHD/60fps, `238` HD, `34` untagged live, `2` SD, `0` failed validation).
- **Generated Outputs & Runtime Caches:**
  - `output/bellhop.m3u8` & `output/lineup_report.json` (Phase 5 `m3u8`).
  - `output/xtream/` (Phase 6 `xtream`): `bellhop_xtream_playlist.json`, `get_live_streams.json`, `get_live_categories.json` with embedded upstream passthrough `xmltv_url`.
  - `cache/bellhop_web/epg_cache.json`: Compact persistent JSON EPG cache dynamically refreshed from upstream `xmltv.php` by `EpgRefreshService`.
- **Unified Bellhop Server (`bellhop serve` / `UnifiedServeServer`):**
  - Co-hosts `WebGuideApp` (`/`, `/login`, `/static/*`, `/api/*`) and `XtreamPassthroughServer` (`/player_api.php`, `/player.php`, `/panel_api.php`, `/get.php`, `/playlist.m3u*`, `/xmltv.php`, `/epg.xml`, `/live/<u/>/<p>/<id>[.<ext>]`) on `0.0.0.0:8080` (or separate ports when `xtream_server.port` is overridden).
  - Separate credentials for Web Guide login (`web_server`) vs Xtream API clients (`xtream_server`), with HMAC-SHA256 sliding session cookies (`bellhop_session`, key in `cache/bellhop_web/session_secret.key`) and deep-link `/login?next=...` preservation.
  - Two-tier `config.yaml` & `/api/config` Settings drawer exposing **only** operational settings (`upstream_xtream`, `xtream_server`, `pipeline`).

---

## 2. Next Planned Milestones
Per user direction:
1. **Internal Scheduling + Explicit Sync Control** in `bellhop serve` (tuner-aware off-hours validation + Web UI sync controls & progress).
2. **Multi-Lineup & Multi-IPTV-Playlist Support** (lifting single-lineup / single-upstream assumptions cleanly).
3. **Optional Proxied Stream Relay** for Xtream clients (cross-device single-tuner preemption, `fallback_sources` failover, and same-channel fan-out).
