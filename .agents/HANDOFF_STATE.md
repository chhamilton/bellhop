# Full Engineering Handoff & Workspace State (Bellhop)

Last updated: `2026-11-26`

## 1. Executive Summary
**Bellhop** provides a stateful, config-driven Python IPTV pipeline (`bellhop run`), a unified Web Guide + Passthrough Xtream Codes server (`bellhop serve`), and Alpine Docker packaging (`Dockerfile`, `docker-compose.example.yml`), with a bundled sample YAML lineup (`CAN-BELLFIBE-MTL-X`) and pluggable backends supporting any North American TV lineup.

### Current Verified State on Disk
- **Coding Style, Strict Typing, Domain Layering & Tests:**
  - **Unified Domain Package (`src/bellhop/`):** Organized into 6 layered subpackages (`common`, `config`, `epg`, `pipeline`, `xtream`, `web`) plus `serve.py` and `cli.py`, with mtime-aware `ConfigManager` (`config/manager.py`) and strict acyclic AST import boundaries enforced by `tests/test_export_bundle.py::test_bellhop_domain_layer_boundaries`.
  - **Python:** Google Python Style Guide (2-space indentation, spaces only / zero tabs, 80-char wrapping via `ruff format`, full `pydocstyle D` including `D205`), `mypy --strict` (`0` issues across all files, `0` `# type: ignore` comments).
  - **Frontend:** Strictly typed TypeScript (`src/bellhop/web/frontend/{api_contracts.d.ts,types.d.ts,prefs.ts,login.ts,guide_logic.ts,render.ts,player.ts,settings.ts,app.ts}`, `strict: true` + `noUncheckedIndexedAccess: true`) compiled to `src/bellhop/web/static/*.js` via `tsc`, with 7 Node unit test suites (`tests/frontend/{prefs,guide,player,settings,render,login,app}.test.mjs`).
  - **HTTP Stack (`aiohttp`):** Unified across both server (`aiohttp.web` in `common/http_utils.py`, `web/server.py`, `xtream/server.py`, `serve.py`) and client (`aiohttp.ClientSession` across `xtream/client.py`, `pipeline/validator.py`, `epg/xmltv.py`, `epg/refresh.py`, `epg/sd_client.py`, `pipeline/providers/tvguide.py`, `web/server.py`).
  - **Docker Packaging:** Multi-stage Alpine `Dockerfile` (`node:22-alpine` → `python:3.13-alpine`), `docker/entrypoint.sh` (`TZ`, `PUID`/`PGID`, auto-seeding `/config/config.yaml`), `docker/config.docker.yaml`, `.env.example`, `docker-compose.example.yml`, and `tests/test_docker_e2e.py`.
  - **Test Suite:** `pytest -v` enforces Python unit/integration/E2E tests (`>= 85%` branch+statement coverage), `mypy --strict`, `ruff check` (`D205` active) & `ruff format --check`, tab-free sources, `tsc --noEmit` + compiled asset sync, 7 Node frontend unit test suites, and Docker entrypoint/container E2E tests.
- **Bundled Sample Lineup (`data/lineups/bell_fibe_montreal.yaml`):**
  - Dual-band SD (`100–899`) and HD (`1000+`) dial positions with `collapse_sd_dupes: true` / `numbering_mode` support.
  - Mapped to HTTP-verified transparent PNG logos from `tv-logo/tv-logos` in `data/official_logos.json`.
- **Dynamic Lineup Backends (`src/bellhop/pipeline/providers/`):**
  - **`tvguide`** (`TvGuideLineupProvider`): Uses constant `https://backend.tvguide.com/tvschedules/tvguide` endpoint for US/Canadian discovery + channel/logo retrieval (e.g. `state/lineups/TVG-<id>.json`).
  - **`schedules_direct`** (`SchedulesDirectLineupProvider` backed by shared `SchedulesDirectSession` in `src/bellhop/epg/sd_client.py`): Uses constant `https://json.schedulesdirect.org/20141201` endpoint (`cache/sd_token.json`, auto re-auth on `{4001, 4003, 4004, 4007, 4009}`, headend discovery, channel + `SdStationInfo` parser).
  - **`yaml`** (`YamlLineupProvider`): Loads local curated YAML lineups.
- **Live-Validated Channel Mapping (`state/channel_mapping.json`):**
  - Provider: `provider.example` (`max_connections: 1`, credentials in gitignored `credentials.yaml`).
  - Candidate pool narrowed from full raw provider catalog → per-slot ranked candidates (high candidate-reduction ratio).
  - Multi-stage validation enforces `-protocol_whitelist http,https,tcp,tls,crypto,hls` on `ffprobe`/`ffmpeg`, Stage-2 audio language checking, and Stage-3 `ffmpeg -vf blackdetect`; `preferred_source: "validated"` preserves verified native EPG bindings and quality tags across incremental runs.
- **Generated Outputs & Runtime Caches:**
  - `output/bellhop.m3u8` & `output/lineup_report.json` (Phase 5 `m3u8`).
  - `output/xtream/` (Phase 6 `xtream`): `bellhop_xtream_playlist.json`, `get_live_streams.json`, `get_live_categories.json` with embedded upstream passthrough `xmltv_url`.
  - `cache/epg.sqlite3`: Embedded WAL-mode SQLite 3 backing store (`sd_lineups`, `sd_stations`, `sd_channel_map`, `sd_programs`, `sd_schedule_days`, `sd_airings`, `guide_airings`) prioritizing Schedules Direct SD-JSON with upstream XMLTV backfill (`~47 MiB` total server RSS, incremental auto-vacuum + 14-day SD program retention).
  - Owner-only permissions (`0o600` files / `0o700` dirs via symlink-guarded `src/bellhop/common/file_utils.py`) across `config.yaml`, `credentials.yaml`, `session_secret.key`, `session_revoked.json`, `sd_token.json`, `epg.sqlite3`, `cache/`, `state/`, and `output/`.
- **Unified Bellhop Server (`bellhop serve` / `UnifiedServeServer`):**
  - Co-hosts `WebGuideApp` (`/`, `/login`, `/static/*`, `/api/*`) and `XtreamPassthroughServer` (`/player_api.php`, `/player.php`, `/panel_api.php`, `/get.php`, `/playlist.m3u*`, `/xmltv.php`, `/epg.xml`, `/live/<u/>/<p>/<id>[.<ext>]`) on `0.0.0.0:8080` (or separate ports when `xtream_server.port` is overridden), loading settings via mtime-cached `ConfigManager` without walking parent `.env` files.
  - Mandatory Web Guide login (`web_server.username` & `password` required when `web_server.enabled: true`), constant-time SHA-256 credential matching, separate credentials + IPv6 `/64`-aware `LoginRateLimiter` (`429`) for Xtream API clients (`xtream_server`), HMAC-SHA256 sliding session cookies (`bellhop_session`, key in `cache/session_secret.key`, revocations persisted in `cache/session_revoked.json` with a 30s grace window on sliding renewal), strict CSP (`form-action 'self'`, omitting wildcard CORS), internal-only `/login?next=...` redirect validation, CSRF protection (`Sec-Fetch-Site` / `Origin`), and opaque server-side HLS asset tokens (`/api/hls/<chno>/asset?u=...`).
  - Two-tier `config.yaml` & `/api/config` Settings drawer: Tier-1 server/path properties (`credentials_file`, `paths`, `web_server`, `xtream_server`) are `config.yaml`-only; `/api/config` exposes only authenticated user/pipeline settings (`upstream_xtream`, `pipeline`).

---

## 2. Next Planned Milestones
Per user direction:
1. **Internal Scheduling + Explicit Sync Control** in `bellhop serve` (tuner-aware off-hours validation + Web UI sync controls & progress).
2. **Multi-Lineup & Multi-IPTV-Playlist Support** (lifting single-lineup / single-upstream assumptions cleanly).
3. **Optional Proxied Stream Relay** for Xtream clients (cross-device single-tuner preemption, `fallback_sources` failover, and same-channel fan-out).
