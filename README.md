# Bellhop

**Bellhop** is a declarative, stateful Python IPTV lineup pipeline (`bellhop`), Web TV Guide & single-tuner live player, and passthrough Xtream Codes server (`bellhop serve`).

It maps a raw **Xtream Codes** catalog (~16,600+ live streams) onto an official cable/fiber TV lineup (such as **Bell Fibe TV — Montréal, QC** or any North American provider via **TV Guide**, **Schedules Direct**, or **YAML**), validates **only** candidate streams, and serves both:

1. **Bellhop Web TV Guide & Live Browser Player** (`mpegts.js` + `hls.js`) with a single-tuner relay, 2D sticky Timeline EPG, stateful deep-link URLs, cookie-based login, and a live operational Settings drawer.
2. **Passthrough Xtream Codes & M3U Server** (`player_api.php`, `player.php`, `panel_api.php`, `get.php`, `xmltv.php`, `/live/...`) presenting curated lineup categories, channel numbers, official logos, and `302` stream/EPG passthroughs to any IPTV client (TiviMate, UHF, VLC, Kodi, Channels).

---

## Quickstart

```bash
# 1. Bootstrap or verify Python virtual environment:
./.agents/scripts/bootstrap.sh
source .venv/bin/activate

# 2. Configure host paths, credentials, and lineup settings:
cp credentials.example.yaml credentials.yaml
cp config.example.yaml config.yaml

# 3. Run the 6-phase pipeline (skip live stream probing if tuner is in use):
bellhop run --skip validate

# 4. Launch the unified Bellhop Web Guide + Passthrough Xtream API server (default 0.0.0.0:8080):
bellhop serve
```

---

## Architecture Overview

### 1. Six-Phase Stateful Pipeline (`bellhop run`)
Executed by `PipelineRunner` (`src/bellhop/runner.py`) and driven exclusively by `config.yaml`:

1. **`lineup` — Multi-Backend Lineup Retrieval** *(30-day TTL in `state/lineups/`)*
   - Pluggable `LineupProvider` backends (`src/bellhop/providers/`):
     - `tvguide` — Free US/Canadian headend discovery & channel/logo retrieval (`TVG-9100000607`).
     - `schedules_direct` — Official SD-JSON `20141201` client with cached SHA-1 token auth.
     - `yaml` — Curated YAML lineups ([data/lineups/bell_fibe_montreal.yaml](data/lineups/bell_fibe_montreal.yaml) + [data/official_logos.json](data/official_logos.json)).
2. **`catalog` — Upstream IPTV Catalog Fetch** *(24-hour TTL in `state/catalogs/`)*
   - Fetches `player_api.php`, snapshots daily state, and computes stream additions/removals/renames.
3. **`match` — Incremental Multi-Signal Channel Matcher**
   - Reconciles `state/channel_mapping.json` with `5.0`-point upgrade hysteresis, ranking regional feed affinity, callsigns, verified native `epg_channel_id`s, and quality (`4K` > `FHD/60fps` > `HD` > `SD`).
4. **`validate` — Candidate-Only Live Stream Validation**
   - Probes **only** matched candidate streams (`only_changed: true` by default), respecting single-tuner `max_connections: 1`, rejecting offline `/black.ts` placeholder redirects, and optionally verifying codecs via `ffprobe`.
5. **`m3u8` — Extended M3U8 Playlist & Diagnostics Report**
   - Writes `output/bellhop.m3u8` and `output/lineup_report.json`.
6. **`xtream` — Xtream JSON Bundle & Passthrough EPG Verification**
   - Verifies upstream `xmltv.php` reachability with a lightweight ~4 KB probe and exports `bellhop_xtream_playlist.json`, `get_live_streams.json`, and `get_live_categories.json` to `output/xtream/` (hot-reloaded automatically by running servers).

### 2. Unified Runtime Server (`bellhop serve`)
- **Co-Existing or Split `aiohttp.web` Listeners (`src/bellhop/serve.py`):**
  - Built on `aiohttp` (`aiohttp.web` server + `aiohttp.ClientSession` upstream client). By default, both the Bellhop Web Guide UI and the Passthrough Xtream Codes API share a single `aiohttp.web` application listener (`0.0.0.0:8080`) with clean route isolation, CORS preflight handling, and request body size limits. Setting `xtream_server.port` (or `host`) in `config.yaml` binds the Xtream server on its own dedicated listener.
- **Bellhop Web TV Guide & Single-Tuner Proxy (`src/bellhop_web/`):**
  - **Views & Filtering:** Category-grouped **Cards** and a 4-hour 2D sticky **Timeline** EPG grid (with continuous `NOW` indicator across category banners), live search, a **With Guide** toggle to hide channels without EPG data, category-relative counts with subtle color cues, and stateful URL query parameters (`?cat=&epg=1&q=&view=timeline&offset=&mode=hls&ch=`).
  - **Theming, Branding & Client Settings (`src/bellhop_web/frontend/`):** Strictly typed ES2022 TypeScript modules (`api_contracts.d.ts`, `types.d.ts`, `prefs.ts`, `login.ts`, `guide_logic.ts`, `render.ts`, `player.ts`, `settings.ts`, `app.ts` compiled to `src/bellhop_web/static/*.js`) featuring the Bellhop Play Bell SVG logo & multi-size favicon, complementary **Dark / Light Mode** with a Sun/Moon toggle on both `/` and `/login`, incremental 30s EPG progress updates, modal focus trapping & Timeline keyboard navigation, and an extensible JSON `localStorage` client settings store (`bellhop.settings` via `window.BellhopSettings`).
  - **Single-Tuner Stream Relay (`TunerSessionManager`):** Proxies live MPEG-TS (`/api/stream/<chno>.ts` via `Transfer-Encoding: chunked`) and HLS (`/api/hls/<chno>/index.m3u8` with 60s edge-URL affinity), enforcing settle delays between channel switches and automatic failover across `fallback_sources`. Upstream credentials never leave the server when viewing through the Web Guide.
  - **Dynamic Passthrough EPG Cache (`EpgRefreshService`):** Fetches upstream `xmltv.php` on demand (without consuming the live-stream tuner), parses mapped channels in a worker thread, and persists a compact cache in `cache/bellhop_web/epg_cache.json`.
  - **Authentication & Scoped Settings UI:** Signed HMAC-SHA256 session cookies (`bellhop_session`), per-IP failed-login throttling (`429` + `Retry-After`), strict `Content-Security-Policy` (`script-src 'self' blob:`), centered Bellhop splash on `/login?next=...`, and a **Settings** drawer (`GET/PUT /api/config`) preserving YAML comments and `${ENV}` placeholders via `ruamel.yaml` while keeping `web_server`, filesystem paths, and credentials strictly server-side.
- **Passthrough Xtream Codes API (`src/bellhop/xtream_server.py`):**
  - Supports `/player_api.php`, `/player.php`, `/panel_api.php`, `/get.php` (`m3u` & `m3u_plus`, `ts` & `hls`/`m3u8`, `category_id` filters), `/playlist.m3u`, `/playlist.m3u8`, and HTTP `302` upstream redirects for `/xmltv.php`, `/epg.xml`, and `/live/<user>/<pass>/<id>[.<ext>]`.
  - **Security Note on Passthrough Redirects:** While `/player_api.php` strips `direct_source` from JSON listings, the `302` passthrough redirects on `/live/...` and `/xmltv.php` necessarily include the upstream provider URL and credentials in the HTTP `Location` header returned to authenticated Xtream clients so video and XMLTV streams flow directly from the IPTV provider without double-proxying. Use dedicated `xtream_server` client credentials on trusted LANs/VPNs, or use the Web TV Guide for fully server-proxied playback.
- **One-Way Package Boundary (`bellhop_web` ← `bellhop`):**
  - `src/bellhop_web/` is kept strictly decoupled from `bellhop.*` imports (`test_bellhop_web_package_never_imports_bellhop`), isolating the HTTP/UI presentation, auth, contract-codegen, and single-tuner relay layer from pipeline/CLI internals.

---

## Two-Tier Configuration (`config.yaml` & `credentials.yaml`)

All persistent settings belong in `config.yaml` (see [config.example.yaml](config.example.yaml) and [credentials.example.yaml](credentials.example.yaml)):

1. **Infrastructure & Host Settings (config-file only, never exposed to Web UI):**
   - `credentials_file` — Path to `credentials.yaml`
   - `paths` — `cache_dir`, `state_dir`, `mapping_path`, `lineup_file`, `m3u8_playlist_path`, `m3u8_report_path`, `xtream_output_dir`, `seed_json`, `xmltv_file`
   - `web_server` — Bind `host`/`port`, `settle_ms`, `session_ttl_hours`, Web Guide login credentials, and reverse-proxy `X-Forwarded-For` trust (`trust_proxy_headers` + `trusted_proxy_ips` allowlist)
2. **Operational Settings (inspectable & editable via the Web UI Settings drawer, excluding credentials):**
   - `upstream_xtream` — Upstream server URL, `stream_format` (`ts`/`m3u8`), timeout, catalog `refresh_hours`
   - `xtream_server` — Passthrough Xtream API `enabled`, optional `host`/`port` override, `public_host`/`public_port`, `server_protocol`
   - `pipeline` — `lineup` (backend, lineup ID, postal code, `collapse_sd_dupes`, `exclude_audio`, `pinned_streams`, `excluded_channels`), `matching` (`upgrade_hysteresis_score`), `validation`, `epg`, and `output` toggles

---

## Data & State Structure Layouts

All persistent state files, exported playlists, and HTTP API payloads are backed by `@dataclass(slots=True)` structs and `TypedDict` schemas (checked under `mypy --strict` with PEP 561 `py.typed` markers):

| Artifact / Endpoint | Typed Model / Schema | Key Structure Layout |
|---|---|---|
| `config.yaml` + `/api/config` | `AppConfig`, `PublicConfigPayload` ([config/](src/bellhop/config/), [api_contracts.py](src/bellhop_web/api_contracts.py)) | **Tier 1 (file-only):** `credentials_file`, `paths`, `web_server`. **Tier 2 (`/api/config`):** `{upstream_xtream, xtream_server, pipeline: {lineup, matching, validation, epg, output}}`. |
| `state/lineups/<id>.json` | `LineupRetrievalResult`, `CableLineup`, `LineupChannelDict` ([lineup_retriever.py](src/bellhop/lineup_retriever.py), [models.py](src/bellhop/models.py)) | `{saved_timestamp, lineup_id, backend, last_diff, lineup: {provider, market, channels: [LineupChannelDict]}}`. |
| `state/catalogs/iptv_catalog_*.json` | `CatalogFetchResult`, `CatalogDiff`, `XtreamStream` ([catalog_fetcher.py](src/bellhop/catalog_fetcher.py)) | `{saved_timestamp, account, categories, stream_count, last_diff, streams: [...]}`. |
| `state/channel_mapping.json` | `MappingDocument`, `ChannelMappingSlot`, `CandidateRecord`, `ProbeResultDict` ([mapping_state.py](src/bellhop/mapping_state.py)) | `{schema_version: 1, lineup_id, last_summary, slots: {"<chno>": {preferred_stream_id, preferred_source, change_status, candidates: [...]}}}`. |
| `output/lineup_report.json` | `LineupDiagnosticsReport`, `ReportSummaryStats`, `GroupCoverageStats`, `ReportChannelRecord` ([playlist.py](src/bellhop/playlist.py)) | `{generated_at, lineup_id, summary, groups: {"<cat>": {total, active, native_epg, ...}}, channels: [...]}`. |
| `output/xtream/*.json` | `XtreamExportBundle`, `ExportedCategory`, `ExportedLiveStream`, `ExportedStreamGuideMeta`, `ExportedChannelGuideBinding` ([xtream_exporter.py](src/bellhop/xtream_exporter.py)) | `bellhop_xtream_playlist.json` (`{user_info, server_info: {xmltv_url}, lineup_info, live_categories, live_streams, epg_guide}`), `get_live_streams.json`, `get_live_categories.json`. |
| `cache/bellhop_web/epg_cache.json` | `EpgCacheDocumentDict`, `CachedProgramDict`, `EpgRefreshStatus` ([epg_refresh.py](src/bellhop_web/epg_refresh.py)) | `{version: 1, fetched_at, channel_count, program_count, channels: {"<epg_id>": [CachedProgramDict]}}`. |
| `GET /api/lineup` & `/api/epg/<chno>` | `GuideCategory`, `WebChannel`, `PublicChannelDict`, `ProgramItemDict` ([store.py](src/bellhop_web/store.py)) | `{server_timestamp, lineup, epg_status, categories: [GuideCategoryDict], channels: [PublicChannelDict]}` (`direct_source` stripped; local `/api/stream/<chno>.ts` URLs). |
| `localStorage["bellhop.settings"]` | `BellhopClientSettings`, `BellhopSettingsApi` ([types.d.ts](src/bellhop_web/frontend/types.d.ts), [prefs.ts](src/bellhop_web/frontend/prefs.ts)) | Versioned structured JSON object (`{version: 1, ui: {theme: "dark" \| "light"}, ...}`) supporting arbitrary nested dicts, arrays, strings, numbers, and booleans synced across tabs. |
| Xtream Passthrough API | `PassthroughCategory`, `PassthroughStream`, `PlayerApiStreamDict` ([xtream_server.py](src/bellhop/xtream_server.py)) | `/player_api.php`, `/panel_api.php`, `/get.php` (`m3u` / `m3u_plus`), and `302` upstream redirects on `/xmltv.php` & `/live/<u/>/<p>/<id>.<ext>`. |

---

## CLI Reference

```bash
# Run all enabled pipeline phases:
bellhop run

# Preview lineup/catalog/matching changes without writing files or probing streams:
bellhop run --dry-run

# Run pipeline without probing live streams (safe while watching TV):
bellhop run --skip validate

# Run specific phase(s) (`export` is shorthand for `m3u8,xtream`):
bellhop run --phase lineup,catalog,match
bellhop run --phase validate --reprobe-all
bellhop run --phase export

# Bypass cache TTLs (`lineup`, `catalog`, `epg`, or all):
bellhop run --force-refresh catalog,epg

# Start unified Bellhop Web Guide + Passthrough Xtream Codes API server:
bellhop serve
```

---

## Docker & Docker Compose (Alpine Linux)

Bellhop ships with a multi-stage Alpine Linux [Dockerfile](Dockerfile) (`node:22-alpine` TypeScript builder → `python:3.13-alpine` runtime with `tzdata`, `su-exec`, `ca-certificates`, and `ffmpeg`/`ffprobe`), an example [.env.example](.env.example), and [docker-compose.example.yml](docker-compose.example.yml).

```bash
# 1. Copy example env & compose files and fill in your credentials:
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml

# 2. Build and start Bellhop (`bellhop serve` on port 8080):
docker compose up -d --build

# 3. Run the 6-phase pipeline inside the container (skip validate if tuner is in use):
docker compose exec bellhop bellhop run --skip validate
```

### Container Mount Points & Environment Variables

| Mount Point | Purpose |
|---|---|
| `/config` | `config.yaml` (auto-seeded from [docker/config.docker.yaml](docker/config.docker.yaml) on first boot) and optional `credentials.yaml` |
| `/data/state` | Monthly lineup snapshots, daily catalog snapshots, `channel_mapping.json`, and `sd_token.json` |
| `/data/cache` | Dynamic Web Guide EPG cache (`bellhop_web/epg_cache.json`) and persistent session signing key (`session_secret.key`) |
| `/data/output` | Exported `bellhop.m3u8`, `lineup_report.json`, and `xtream/*.json` bundle |

- **`TZ`** (e.g. `America/Montreal`): Configures container timezone via Alpine `tzdata`.
- **`PUID` / `PGID`** (default `1000` / `1000`): Maps non-root process execution and volume ownership via `su-exec` (both at container boot and automatically on `docker compose exec bellhop bellhop ...` commands).
- **Surfaced Credentials (`.env` or `credentials.yaml`):** `BELLHOP_WEB_USERNAME`, `BELLHOP_WEB_PASSWORD`, `BELLHOP_WEB_SESSION_SECRET`, `BELLHOP_SERVE_USERNAME`, `BELLHOP_SERVE_PASSWORD`, `XTREAM_SERVER_URL`, `XTREAM_USERNAME`, `XTREAM_PASSWORD`, and optional `SD_USERNAME` / `SD_PASSWORD`.

---

## Dependencies & Local Bootstrap

Run `./.agents/scripts/bootstrap.sh` to create `.venv`, install all Python runtime + dev dependencies, install the TypeScript compiler, build frontend JS, and run the full test suite:

- **Python Runtime (`pyproject.toml` & pinned `requirements.lock`):** Python `>=3.10`, `aiohttp`, `pyyaml`, `ruamel.yaml`, `python-dotenv`, `rich` (plus optional system `ffmpeg`/`ffprobe` for Phase 4 codec probing; skippable in Docker via `--build-arg INSTALL_FFMPEG=0`).
- **Python Dev (`pip install -e ".[dev]"`):** `mypy`, `types-PyYAML`, `ruff`, `pytest`, `pytest-asyncio`, `pytest-cov`, `nodejs-wheel` (provides self-contained `node` + `npm` inside `.venv`).
- **Frontend Dev (`package.json` & `package-lock.json`):** `typescript` (compiles `src/bellhop_web/frontend/*.ts` → `src/bellhop_web/static/*.js`; vendored player versions & SHA-256 digests are recorded in [VERSIONS.md](src/bellhop_web/static/vendor/VERSIONS.md)).

---

## Coding Style, Type Checking & Tests

- **Python Style (Google Python Style Guide):** Spaces only (zero tabs), **2-space indentation**, 80-column soft wrapping via `ruff format` (`E501` exempted for long URLs/strings), Google-style docstrings enforced via `ruff` `pydocstyle` (`D` with `convention = "google"` across modules, packages, public classes, and public functions; `D102`/`D105`/`D107` exempted on self-explanatory methods/`__init__`), and strict `mypy --strict` typing (zero `# type: ignore`) configured in [pyproject.toml](pyproject.toml).
- **Frontend TypeScript (`src/bellhop_web/frontend/`):** Edit `.ts` ES2022 module sources (2-space indent, `strict: true`, `noUncheckedIndexedAccess: true` in [tsconfig.json](tsconfig.json)) and compile to `src/bellhop_web/static/*.js` via `tsc`. Both `static/*.js` and generated `frontend/api_contracts.d.ts` are intentionally committed so `pip install .` requires no Node toolchain at runtime; `test_typescript_strict_and_compiled_assets_in_sync` enforces exact parity on every test run.
- **Automated Verification (`pytest`):** Enforces `>= 85%` statement coverage (`pytest-cov`), `--strict-markers` (`docker`, `slow`, `real_outputs`), Python unit/contract/`FakeXtreamServer` & Alpine Docker E2E tests, `mypy --strict`, `ruff check` & `ruff format --check`, zero-tab checks, `tsc --noEmit` + asset sync, and Node unit tests (`tests/frontend/{prefs,guide}.test.mjs`):

```bash
# Format & lint Python (Google style, 2-space indent, pydocstyle D):
.venv/bin/ruff format src tests
.venv/bin/ruff check src tests

# Typecheck & compile TypeScript frontend -> src/bellhop_web/static/*.js:
.venv/bin/node ./node_modules/typescript/bin/tsc

# Run strict Python typecheck and complete test suite (with coverage & Docker E2E):
.venv/bin/mypy
.venv/bin/pytest -v

# Fast hermetic non-Docker subset / opt-in local output check:
.venv/bin/pytest -m "not docker"
.venv/bin/pytest -m real_outputs --run-real-outputs
```

---

## License

Licensed under the [Apache License, Version 2.0](LICENSE). Third-party browser media player attributions are listed in [NOTICE](NOTICE) and [VERSIONS.md](src/bellhop_web/static/vendor/VERSIONS.md).
