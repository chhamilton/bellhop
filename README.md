# Bellhop

**Bellhop** is a declarative, stateful Python IPTV lineup pipeline (`bellhop`), Web TV Guide & single-tuner live player, and passthrough Xtream Codes server (`bellhop serve`).

It maps a raw **Xtream Codes** live stream catalog onto an official cable/fiber TV lineup (using the bundled sample YAML lineup or any North American provider via **TV Guide**, **Schedules Direct**, or **YAML**), validates **only** candidate streams, and serves both:

1. **Bellhop Web TV Guide & Live Browser Player** (`mpegts.js` + `hls.js`) with per-account isolated single-tuner relays, 2D sticky Timeline EPG, stateful deep-link URLs, email-based scrypt login, and an Account & Player Setup drawer.
2. **Multi-Account Passthrough Xtream Codes & M3U8 Server** (`player_api.php`, `player.php`, `panel_api.php`, `get.php`, `xmltv.php`, `/live/...`) presenting curated lineup categories, channel numbers, official logos, dynamic per-user M3U8 playlists, and per-account `302` stream passthroughs to any IPTV client (TiviMate, UHF, VLC, Kodi, Channels).

---

## Quickstart

```bash
# 1. Bootstrap or verify Python virtual environment:
./.agents/scripts/bootstrap.sh
source .venv/bin/activate

# 2. Configure host paths, accounts/credentials, and lineup settings:
cp credentials.example.yaml credentials.yaml
cp config.example.yaml config.yaml

# 3. Run the 5-phase pipeline (skip live stream probing if tuner is in use):
bellhop run --skip validate

# 4. Launch the unified Bellhop Web Guide + Passthrough Xtream API server (default 0.0.0.0:8080):
bellhop serve
```

---

## Architecture Overview

### 1. Five-Phase Stateful Pipeline (`bellhop run`)
Executed by `PipelineRunner` (`src/bellhop/pipeline/runner.py`) and driven exclusively by `config.yaml`:

1. **`lineup` — Multi-Backend Lineup Retrieval & Grouping** *(30-day TTL in `state/lineups/`)*
   - Pluggable `LineupProvider` backends (`src/bellhop/pipeline/providers/`) using fixed official endpoints (plus conditional `iptv-org` enrichment and 2D `(Language x Genre)` MDL dial grouping):
     - `tvguide` — Free US/Canadian headend discovery & channel/logo retrieval (e.g. `TVG-9100000001`).
     - `schedules_direct` — Official SD-JSON `20141201` client with cached SHA-1 token auth (`src/bellhop/epg/sd_client.py`).
     - `yaml` — Curated YAML lineups ([data/lineups/bell_fibe_montreal.yaml](data/lineups/bell_fibe_montreal.yaml) + [data/official_logos.json](data/official_logos.json)).
2. **`catalog` — Upstream IPTV Catalog Fetch** *(24-hour TTL in `state/catalogs/`)*
   - Fetches `player_api.php`, snapshots daily state, and computes stream additions/removals/renames.
3. **`match` — Incremental Multi-Signal Channel Matcher**
   - Reconciles `state/channel_mapping.json` with `5.0`-point upgrade hysteresis (or `--reset-mapping` for a clean rebuild), ranking regional feed affinity, callsigns, verified native `epg_channel_id`s, and quality (`4K` > `FHD/60fps` > `HD` > `SD`).
4. **`validate` — Candidate-Only Live Stream Validation**
   - Probes **only** matched candidate streams (`only_changed: true` by default) via the configured `pipeline: true` account, respecting `max_connections`, rejecting offline `/black.ts` placeholder redirects, verifying Stage-2 codecs & audio language (`check_audio_language`) via `ffprobe`, and optionally detecting Stage-3 black feeds via `ffmpeg -vf blackdetect` (both pinned to `-protocol_whitelist http,https,tcp,tls,crypto,hls`).
5. **`export` — Diagnostics Report, Xtream JSON Bundle & SQLite EPG Hydration**
   - Writes `output/lineup_report.json`, verifies upstream `xmltv.php` reachability with a lightweight ~4 KB probe, exports credential-free `bellhop_xtream_playlist.json`, `get_live_streams.json`, and `get_live_categories.json` to `output/xtream/` (hot-reloaded automatically by running servers; playlists are served dynamically on `/get.php`), and hydrates the local SQLite EPG database.

### 2. Unified Runtime Server (`bellhop serve`)
- **Co-Existing or Split `aiohttp.web` Listeners (`src/bellhop/serve.py`):**
  - Built on `aiohttp` (`aiohttp.web` server + `aiohttp.ClientSession` upstream client) and backed by mtime-cached `ConfigManager` (`config/manager.py`). By default, both the Bellhop Web Guide UI and the Passthrough Xtream Codes API share a single `aiohttp.web` application listener (`0.0.0.0:8080`) with clean route isolation, strict security headers (intentionally omitting wildcard CORS), and request body size limits. Setting `xtream_server.port` (or `host`) in `config.yaml` binds the Xtream server on its own dedicated listener.
- **Bellhop Web TV Guide & Single-Tuner Proxy (`src/bellhop/web/`):**
  - **Views & Filtering:** Category-grouped **Cards** and a 4-hour 2D sticky **Timeline** EPG grid (with continuous `NOW` indicator across category banners), live search, a **With Guide** toggle to hide channels without EPG data, category-relative counts with subtle color cues, and stateful URL query parameters (`?cat=&epg=1&q=&view=timeline&offset=&mode=hls&ch=`).
  - **Theming, Branding & Client Settings (`src/bellhop/web/frontend/`):** Strictly typed ES2022 TypeScript modules (`api_contracts.d.ts`, `types.d.ts`, `prefs.ts`, `login.ts`, `guide_logic.ts`, `render.ts`, `player.ts`, `settings.ts`, `app.ts` compiled to `src/bellhop/web/static/*.js`) featuring the Bellhop Play Bell SVG logo & multi-size favicon, complementary **Dark / Light Mode** with a Sun/Moon toggle on both `/` and `/login`, incremental 30s EPG progress updates, modal focus trapping & Timeline keyboard navigation, and an extensible JSON `localStorage` client settings store (`bellhop.settings` via `window.BellhopSettings`).
  - **Per-Account Single-Tuner Stream Relay (`TunerSessionManager`):** Proxies live MPEG-TS (`/api/stream/<chno>.ts` via `Transfer-Encoding: chunked`) and HLS (`/api/hls/<chno>/index.m3u8` with 60s edge-URL affinity and opaque per-account server-side segment tokens via `?u=...`), enforcing settle delays between channel switches, isolated per-user tuner handoffs, and automatic failover across `fallback_stream_ids` / `fallback_sources`. Upstream credentials never leave the server when viewing through the Web Guide.
  - **Embedded WAL-Mode SQLite EPG Store (`src/bellhop/epg/`):** Syncs Schedules Direct SD-JSON (`20141201` MD5 differential) and upstream `xmltv.php` backfill on demand without consuming the live-stream tuner, persisting indexed schedules in `cache/epg.sqlite3` with incremental auto-vacuum and 14-day retention.
  - **Email Auth (`scrypt`) & Account Setup Drawer:** Mandatory signed HMAC-SHA256 `v2` session cookies (`bellhop_session`, auto-generated secret in `cache/session_secret.key`—delete live to revoke all sessions immediately; persistent `jti` revocations in `cache/session_revoked.json`), scrypt password verification & in-place YAML auto-hashing, IPv6 `/64`-aware failed-login throttling (`429` + `Retry-After`), CSRF validation, strict `Content-Security-Policy` (`script-src 'self' blob:; form-action 'self'`), internal-only `/login?next=...` redirects, and the **Account & Player Setup** drawer (`/api/account*`) for password changes, regenerating 8-char Bellhop Xtream credentials, and copying player URLs.
- **Multi-Account Passthrough Xtream Codes API (`src/bellhop/xtream/server.py`):**
  - Supports `/player_api.php`, `/player.php`, `/panel_api.php`, on-demand `/get.php` (`m3u` & `m3u_plus`, `ts` & `hls`/`m3u8`, `category_id` filters), `/playlist.m3u`, `/playlist.m3u8`, local SQLite-backed `get_short_epg` / `get_simple_data_table` & chunked `/xmltv.php` (`302` upstream fallback only if SQLite is empty), and HTTP `302` upstream redirects for `/live/<user>/<pass>/<id>[.<ext>]` routed per-account to each user's own `upstream_username` / `upstream_password`.
  - **Security Note on Passthrough Redirects:** While `/player_api.php` and exported JSON strips `direct_source` credentials, `302` passthrough redirects on `/live/...` necessarily include that account's upstream provider URL and credentials in the HTTP `Location` header returned to authenticated Xtream clients so video streams directly from the provider without double-proxying. Use dedicated per-account `xtream_username`/`xtream_password` credentials on trusted LANs/VPNs, or use the Web TV Guide for fully server-proxied playback.
- **Domain Subpackage Layering (`src/bellhop/*`):**
  - `src/bellhop/` is structured into 6 acyclic domain layers (`common`, `config`, `epg`, `pipeline`, `xtream`, `web`) enforced on every test run by `test_bellhop_domain_layer_boundaries`.

---

## Configuration (`config.yaml` & `credentials.yaml`)

All settings live strictly in `config.yaml` and `credentials.yaml` (see [config.example.yaml](config.example.yaml) and [credentials.example.yaml](credentials.example.yaml)):

- **`config.yaml`:** `credentials_file`, `paths` (`cache_dir`, `state_dir`, `mapping_path`, `lineup_file`, `report_path`, `xtream_output_dir`, `seed_json`, `xmltv_file`), `web_server`, `xtream_server`, `upstream_xtream` (`server_url`, `stream_format`, timeouts/TTL), and `pipeline` (`lineup`, `matching`, `validation`, `epg`, `output`).
- **`credentials.yaml`:** `accounts:` (each entry defines `email`, `web_password` or auto-hashed `scrypt$` `web_password_hash`, `xtream_username`/`xtream_password`, `upstream_username`/`upstream_password`, and optional `pipeline: true`), plus optional `schedules_direct` (`username`, `password`, `auto_add_lineup`).

---

## Data & State Structure Layouts

All persistent state files, exported playlists, and HTTP API payloads are backed by `@dataclass(slots=True)` structs and `TypedDict` schemas (checked under `mypy --strict` with PEP 561 `py.typed` markers):

| Artifact / Endpoint | Typed Model / Schema | Key Structure Layout |
|---|---|---|
| `config.yaml` + `credentials.yaml` | `AppConfig`, `UserAccount`, `AccountInfoResponseDict` ([config/](src/bellhop/config/), [accounts.py](src/bellhop/common/accounts.py), [api_contracts.py](src/bellhop/common/api_contracts.py)) | File-backed config + multi-account `accounts:` (`email`, `web_password_hash`, `xtream_*`, `upstream_*`, `pipeline`). `/api/account*` exposes only the caller's Xtream credentials, player URLs, and password change. |
| `state/lineups/<id>.json` | `LineupRetrievalResult`, `CableLineup`, `LineupChannelDict` ([lineup_retriever.py](src/bellhop/pipeline/lineup_retriever.py), [models.py](src/bellhop/pipeline/models.py)) | `{saved_timestamp, lineup_id, backend, last_diff, lineup: {provider, market, channels: [LineupChannelDict]}}`. |
| `state/catalogs/iptv_catalog_*.json` | `CatalogFetchResult`, `CatalogDiff`, `XtreamStream` ([catalog_fetcher.py](src/bellhop/pipeline/catalog_fetcher.py)) | `{saved_timestamp, account, categories, stream_count, last_diff, streams: [...]}`. |
| `state/channel_mapping.json` | `MappingDocument`, `ChannelMappingSlot`, `CandidateRecord`, `ProbeResultDict` ([mapping_state.py](src/bellhop/pipeline/mapping_state.py)) | `{schema_version: 1, lineup_id, last_summary, slots: {"<chno>": {preferred_stream_id, preferred_source, change_status, candidates: [...]}}}`. |
| `output/lineup_report.json` | `LineupDiagnosticsReport`, `ReportSummaryStats`, `GroupCoverageStats`, `ReportChannelRecord` ([playlist.py](src/bellhop/pipeline/playlist.py)) | `{generated_at, lineup_id, summary, groups: {"<cat>": {total, active, native_epg, ...}}, channels: [...]}`. |
| `output/xtream/*.json` | `XtreamExportBundle`, `ExportedCategory`, `ExportedLiveStream`, `ExportedStreamGuideMeta`, `ExportedChannelGuideBinding` ([xtream_exporter.py](src/bellhop/pipeline/xtream_exporter.py)) | `bellhop_xtream_playlist.json` (`{user_info, server_info: {xmltv_url}, lineup_info, live_categories, live_streams, epg_guide}`), `get_live_streams.json`, `get_live_categories.json`. |
| `cache/epg.sqlite3` | `EpgDatabase`, `EpgRefreshStatus` ([db.py](src/bellhop/epg/db.py), [refresh.py](src/bellhop/epg/refresh.py)) | Embedded WAL-mode SQLite 3 schema (`epg_meta`, `sd_lineups`, `sd_stations`, `sd_channel_map`, `sd_programs`, `sd_schedule_days`, `sd_airings`, `guide_airings`) with indexed time-range queries and chunked XMLTV streaming. |
| `GET /api/lineup` & `/api/epg/<chno>` | `GuideCategory`, `WebChannel`, `PublicChannelDict`, `ProgramItemDict` ([store.py](src/bellhop/web/store.py)) | `{server_timestamp, lineup, epg_status, categories: [GuideCategoryDict], channels: [PublicChannelDict]}` (`direct_source` stripped; local `/api/stream/<chno>.ts` URLs). |
| `localStorage["bellhop.settings"]` | `BellhopClientSettings`, `BellhopSettingsApi` ([types.d.ts](src/bellhop/web/frontend/types.d.ts), [prefs.ts](src/bellhop/web/frontend/prefs.ts)) | Versioned structured JSON object (`{version: 1, ui: {theme: "dark" \| "light"}, ...}`) supporting arbitrary nested dicts, arrays, strings, numbers, and booleans synced across tabs. |
| Xtream Passthrough API | `PassthroughCategory`, `PassthroughStream`, `PlayerApiStreamDict` ([server.py](src/bellhop/xtream/server.py)) | `/player_api.php`, `/panel_api.php`, `/get.php` (`m3u` / `m3u_plus`), SQLite `/xmltv.php`, and `302` upstream redirects on `/live/<u/>/<p>/<id>.<ext>`. |

---

## CLI Reference

```bash
# Run all enabled pipeline phases:
bellhop run

# Preview lineup/catalog/matching changes without writing files or probing streams:
bellhop run --dry-run

# Run pipeline without probing live streams (safe while watching TV):
bellhop run --skip validate

# Run specific phase(s) (`lineup`, `catalog`, `match`, `validate`, `export` / `epg`):
bellhop run --phase lineup,catalog,match
bellhop run --phase validate --reprobe-all
bellhop run --phase export

# Bypass cache TTLs (`lineup`, `catalog`, `epg`, or all) or rebuild mapping state:
bellhop run --force-refresh catalog,epg
bellhop run --reset-mapping --skip validate

# Start unified Bellhop Web Guide + Passthrough Xtream Codes API server:
bellhop serve
```

---

## Docker & Docker Compose (Alpine Linux)

Bellhop ships with a multi-stage Alpine Linux [Dockerfile](Dockerfile) (`node:22-alpine` TypeScript builder → `python:3.13-alpine` runtime with `tzdata`, `su-exec`, `ca-certificates`, and `ffmpeg`/`ffprobe`), an example [.env.example](.env.example), and [docker-compose.example.yml](docker-compose.example.yml).

```bash
# 1. Copy example env & compose files, plus config.yaml and credentials.yaml into ./docker-data/config/:
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
mkdir -p docker-data/config && cp config.example.yaml docker-data/config/config.yaml && cp credentials.example.yaml docker-data/config/credentials.yaml

# 2. Build and start Bellhop (`bellhop serve` on port 8080):
docker compose up -d --build

# 3. Run the 5-phase pipeline inside the container (skip validate if tuner is in use):
docker compose exec bellhop bellhop run --skip validate
```

### Container Mount Points & Environment Variables

| Mount Point | Purpose |
|---|---|
| `/config` | `config.yaml` and `credentials.yaml` (auto-seeded from [docker/](docker/) templates on first boot) |
| `/data/state` | Monthly lineup snapshots, daily catalog snapshots, and `channel_mapping.json` |
| `/data/cache` | Embedded WAL SQLite EPG store (`epg.sqlite3`), SD token (`sd_token.json`), `iptv_org/` metadata cache, persistent session signing key (`session_secret.key`), and revocations (`session_revoked.json`) |
| `/data/output` | Diagnostics report (`lineup_report.json`) and exported `xtream/*.json` bundle |

- **`TZ`** (default `UTC`, e.g. `America/New_York` or `America/Toronto`): Configures container timezone via Alpine `tzdata`.
- **`PUID` / `PGID`** (default `1000` / `1000`): Maps non-root process execution and volume ownership via `su-exec` (both at container boot and automatically on `docker compose exec bellhop bellhop ...` commands).

---

## Dependencies & Local Bootstrap

Run `./.agents/scripts/bootstrap.sh` to create `.venv`, install all Python runtime + dev dependencies, install the TypeScript compiler, build frontend JS, and run the full test suite:

- **Python Runtime (`pyproject.toml` & pinned `requirements.lock`):** Python `>=3.10`, `aiohttp`, `pyyaml`, `ruamel.yaml`, `rich` (plus optional system `ffmpeg`/`ffprobe` for Phase 4 codec probing; skippable in Docker via `--build-arg INSTALL_FFMPEG=0`).
- **Python Dev (`pip install -e ".[dev]"`):** `mypy`, `types-PyYAML`, `ruff`, `pytest`, `pytest-asyncio`, `pytest-cov`, `nodejs-wheel` (provides self-contained `node` + `npm` inside `.venv`).
- **Frontend Dev (`package.json` & `package-lock.json`):** `typescript` (compiles `src/bellhop/web/frontend/*.ts` → `src/bellhop/web/static/*.js`; vendored player versions & SHA-256 digests are recorded in [VERSIONS.md](src/bellhop/web/static/vendor/VERSIONS.md)).

---

## Coding Style, Type Checking & Tests

- **Python Style (Google Python Style Guide):** Spaces only (zero tabs), **2-space indentation**, 80-column soft wrapping via `ruff format` (`E501` exempted for long URLs/strings), Google-style docstrings enforced via `ruff` `pydocstyle` (`D` with `convention = "google"` across modules, packages, public classes, and public functions; `D102`/`D105`/`D107` exempted on self-explanatory methods/`__init__`), and strict `mypy --strict` typing (zero `# type: ignore`) configured in [pyproject.toml](pyproject.toml).
- **Frontend TypeScript (`src/bellhop/web/frontend/`):** Edit `.ts` ES2022 module sources (2-space indent, `strict: true`, `noUncheckedIndexedAccess: true` in [tsconfig.json](tsconfig.json)) and compile to `src/bellhop/web/static/*.js` via `tsc`. Both `static/*.js` and generated `frontend/api_contracts.d.ts` are intentionally committed so `pip install .` requires no Node toolchain at runtime; `test_typescript_strict_and_compiled_assets_in_sync` enforces exact parity on every test run.
- **Automated Verification (`pytest`):** Enforces `>= 85%` branch+statement coverage (`pytest-cov`), `--strict-markers` (`docker`, `slow`, `real_outputs`), Python unit/contract/`FakeXtreamServer` & Alpine Docker E2E tests, `mypy --strict`, `ruff check` & `ruff format --check`, zero-tab checks, `tsc --noEmit` + asset sync, and 7 Node unit test suites (`tests/frontend/{prefs,guide,player,settings,render,login,app}.test.mjs`):

```bash
# Format & lint Python (Google style, 2-space indent, pydocstyle D):
.venv/bin/ruff format src tests
.venv/bin/ruff check src tests

# Typecheck & compile TypeScript frontend -> src/bellhop/web/static/*.js:
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

Licensed under the [Apache License, Version 2.0](LICENSE). Third-party browser media player attributions are listed in [NOTICE](NOTICE) and [VERSIONS.md](src/bellhop/web/static/vendor/VERSIONS.md).
