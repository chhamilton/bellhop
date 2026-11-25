# AGENTS.md — Bellhop Workspace Guide

## Project Overview
This repository implements **Bellhop**, installed via a single `bellhop` CLI (`bellhop run` & `bellhop serve`) from `pyproject.toml` and organized into layered domain subpackages under `src/bellhop/`:
- **`bellhop.common` (`src/bellhop/common/`)** — Foundational owner-only file permissions (`file_utils.py`), shared HTTP `TypedDict` contracts & TypeScript codegen (`api_contracts.py`), `aiohttp` factories/middleware/rate-limiting (`http_utils.py`), and cached `ExportBundleReader` (`export_bundle.py`).
- **`bellhop.config` (`src/bellhop/config/`)** — Two-tier YAML/env configuration (`model.py`, `load.py`, `public.py`).
- **`bellhop.epg` (`src/bellhop/epg/`)** — Embedded WAL-mode SQLite 3 EPG & Schedules Direct backing store (`db.py`, `cache/epg.sqlite3`), shared `SchedulesDirectSession` (`sd_client.py`), SD-JSON MD5 differential fetcher (`sd_epg.py`), XMLTV parser/verifier (`xmltv.py`), background refresher (`refresh.py`), and pipeline sync adapter (`sync.py`).
- **`bellhop.pipeline` (`src/bellhop/pipeline/`)** — Declarative, stateful 6-phase pipeline (`runner.py`) mapping raw Xtream catalogs onto official cable/fiber TV lineups (`providers/`: `tvguide`, `schedules_direct`, `yaml`), multi-signal `ChannelMatcher` (`matcher.py` + `matcher_vocab.py`), candidate-only `StreamValidator` (`validator.py`), and Extended **M3U8** + **Xtream JSON** exporters (`playlist.py`, `xtream_exporter.py`).
- **`bellhop.xtream` (`src/bellhop/xtream/`)** — Upstream `player_api.php` client (`client.py`) and local `XtreamPassthroughServer` (`server.py`, serving SQLite-backed XMLTV/short-EPG and `302` stream passthroughs).
- **`bellhop.web` (`src/bellhop/web/`)** — Decoupled `aiohttp.web` TV Guide UI (`server.py`, `store.py`, `auth.py`), strict ES2022 TypeScript frontend (`frontend/` → `static/`), and **single-tuner relay** (`TunerSessionManager`), co-hosted with `bellhop.xtream` by `UnifiedServeServer` (`src/bellhop/serve.py`).

Long-term direction: evolve `PipelineRunner` + `bellhop serve` into a **persistent service with internal scheduling**, explicit sync controls, **multi-lineup / multi-playlist support**, and optional proxied stream relay.

---

## Critical Operational & Architectural Rules

1. **Single-Tuner Limit (`max_connections: 1`) — Do Not Interrupt Active Viewing**
   - The configured Xtream account allows **at most 1 concurrent live stream connection**.
   - Metadata/EPG endpoints (`player_api.php`, `xmltv.php`) do **not** consume the tuner slot, but probing `/live/<user>/<pass>/<id>.ts` or `.m3u8` **does**.
   - During development, always default to `bellhop run --dry-run` or `bellhop run --skip validate` unless the user confirms the tuner slot is free.
   - Never bypass `effective_concurrency()` or `TunerSessionManager`'s settle delay (`180ms`–`350ms` between closing one stream and opening the next).

2. **Two-Tier Declarative Configuration in `config.yaml` Only (No CLI Configuration Bloat)**
   - **All** persistent settings belong in `config.yaml` (with secrets in `credentials.yaml` or `.env`), organized into two tiers:
     1. **Infrastructure (`config.yaml` only, never exposed to Web UI):** `credentials_file`, `paths`, `web_server` (login is mandatory and cannot be disabled), and `xtream_server`.
     2. **Operational (exposed in Web UI Settings to authenticated users only, excluding credentials):** `upstream_xtream` and `pipeline` (`lineup`, `matching`, `validation`, `epg`, `output`).
   - Do **not** add configuration flags back to the `bellhop` CLI. CLI arguments are strictly reserved for ephemeral execution control (`--config`, `--phase`, `--skip`, `--dry-run`, `--force-refresh`, `--reprobe-all`, `--reset-mapping`, `--quiet`, `--verbose`).

3. **Credentials & Git Hygiene**
   - `credentials.yaml`, `config.yaml`, `.env`, `cache/`, `state/`, and `output/` are gitignored—never commit secrets or raw provider dumps.
   - Do **not** mask passwords with `***` inside `state/channel_mapping.json`, `output/bellhop.m3u8`, or `output/xtream/get_live_streams.json`: Xtream stream URLs require embedded credentials to play (masking caused `HTTP 513` upstream errors). Redaction belongs only in UI/API serialization (`AppConfig.to_dict(redact_secrets=True)` and `WebChannel.to_public_dict()`, which exposes `/api/stream/<chno>.ts`), while `/api/config` (`export_public_config`) omits credentials, `web_server`, and paths altogether.
   - **Passthrough & SQLite EPG Model:** By design, `XtreamPassthroughServer` (`302` redirects on `/live/<user>/<pass>/<id>`) returns the upstream IPTV stream URL (including upstream credentials) in the `Location` header to authenticated Xtream clients so video streams directly from the provider without double-proxying, while `/xmltv.php` and `get_short_epg` serve lineup-filtered EPG locally from `epg.sqlite3` (`302` redirecting `/xmltv.php` to upstream only as a fallback if SQLite is empty). Browser viewers using the Web TV Guide stream via the server-side `/api/stream/<chno>.ts` and `/api/hls/<chno>/index.m3u8` relays, which never expose upstream credentials.

4. **Domain Subpackage Layering (`src/bellhop/*`) & Strict Typing**
   - Preserve the acyclic domain import DAG enforced by `tests/test_export_bundle.py::test_bellhop_domain_layer_boundaries`: `common` → `{}`; `config` → `{common}`; `epg` → `{common, config}`; `web` → `{common, epg}`; `xtream` → `{common, config, epg}`; `pipeline` → `{common, config, epg, xtream.client}`; `serve` → `{common, config, epg, web, xtream}`.
   - Maintain `mypy --strict` across `src/` and `tests/` with zero `# type: ignore` suppressions; prefer explicit `@dataclass(slots=True)` structs and `TypedDict`s (`src/bellhop/common/api_contracts.py` -> auto-generated `src/bellhop/web/frontend/api_contracts.d.ts`) over nested `dict[str, Any]`.

5. **Coding Style Preferences (Google Python Style & Strict TypeScript Frontend)**
   - **Python Style:** Follow the **Google Python Style Guide** — **no tabs, spaces only, 2-space indentation per depth**, 80-column code wrapping via `ruff format` (`E501` ignored so long URLs/strings aren't forcibly split), Google-style docstrings (`D` pydocstyle with `convention = "google"` enforced on modules, packages, public classes, and public functions; `D102`/`D105`/`D107` exempted on self-explanatory methods/`__init__`), and sorted imports via `[tool.ruff]` in `pyproject.toml`.
   - **Frontend TypeScript:** All browser JS is authored in strictly typed ES2022 TypeScript modules under `src/bellhop/web/frontend/` (`api_contracts.d.ts`, `types.d.ts`, `prefs.ts`, `login.ts`, `guide_logic.ts`, `render.ts`, `player.ts`, `settings.ts`, `app.ts`; 2-space indent, `strict: true` + `noUncheckedIndexedAccess: true`) and compiled into `src/bellhop/web/static/*.js` via `.venv/bin/node ./node_modules/typescript/bin/tsc`. Compiled `static/*.js` and generated `api_contracts.d.ts` are intentionally committed so Python-only installs work without Node at runtime, guarded against drift by `tests/test_typecheck.py`. Never hand-edit `static/*.js` or `frontend/api_contracts.d.ts`.
   - **Client-Side State (`localStorage["bellhop.settings"]`):** Store persistent browser UI preferences (theme, future view/favorite/player preferences) as structured JSON (nested dicts, arrays, strings, numbers, booleans) through `window.BellhopSettings` (`src/bellhop/web/frontend/prefs.ts`) rather than ad-hoc top-level `localStorage` keys.

---

## Repository Map

```text
.
├── config.example.yaml          # Two-tier documented template for config.yaml
├── credentials.example.yaml     # Documented template for credentials.yaml
├── requirements.lock            # Pinned runtime dependency lockfile for reproducible builds
├── data/
│   ├── lineups/bell_fibe_montreal.yaml  # Curated 355-network / 698-slot Bell Fibe MTL lineup
│   └── official_logos.json              # 339 verified tv-logo/tv-logos transparent PNG URLs
├── src/
│   └── bellhop/
│       ├── cli.py               # `bellhop run` & `bellhop serve` CLI
│       ├── serve.py             # `UnifiedServeServer` co-hosting Web Guide + Xtream API (shared or split ports)
│       ├── common/              # Tier 0: `file_utils`, `api_contracts`, `http_utils`, cached `export_bundle`
│       ├── config/              # Tier 1: Two-tier config package (`model.py`, `load.py`, `public.py`)
│       ├── epg/                 # Tier 2: `constants`, WAL SQLite `db`, `models`, `sd_client`, `sd_epg`, `xmltv`, `refresh`, `sync`
│       ├── pipeline/            # Tier 3a: `models`, `lineup_loader`, `dial_grouping`, `iptv_org`, `lineup_retriever`, `providers/`, `catalog_fetcher`, `matcher`, `matcher_vocab`, `mapping_state`, `validator`, `playlist`, `xtream_exporter`, `runner`
│       ├── xtream/              # Tier 3b: Upstream `client.py` + `XtreamPassthroughServer` (`server.py`)
│       └── web/                 # Tier 3c: `auth`, `store`, `server` (`WebGuideApp` + `TunerSessionManager`), `frontend/`, `static/`
├── Dockerfile                   # Multi-stage Alpine container (`node:22-alpine` -> `python:3.13-alpine`)
├── docker/                      # `entrypoint.sh`, `bellhop-shim.sh` (`PUID`/`PGID`), & `config.docker.yaml`
├── docker-compose.example.yml   # Example Docker Compose stack with `/config`, `/data/{state,cache,output}`
├── .env.example                 # Example environment variables (container runtime + surfaced credentials)
├── tsconfig.json                # Strict ES2022 TypeScript compiler config (`frontend/*.ts` -> `static/*.js`)
├── package.json                 # Dev-only TypeScript toolchain manifest (`npm ci` locked via `package-lock.json`)
└── tests/                       # Unit, contract, Node & Docker E2E suite (`pytest`, `pytest-cov >= 85%`)
```

---

## Common Commands

```bash
# Bootstrap or verify environment (Python .venv + Ruff + Node/TypeScript):
./.agents/scripts/bootstrap.sh

# Format & lint Python (Google style, 2-space indent, zero tabs):
.venv/bin/ruff format src tests
.venv/bin/ruff check src tests

# Typecheck & compile TypeScript frontend -> src/bellhop/web/static/*.js:
.venv/bin/node ./node_modules/typescript/bin/tsc

# Run strict static type checking across src/ and tests/:
.venv/bin/mypy

# Run full test suite (Python tests, mypy --strict, ruff check/format, tsc sync, Node & container E2E):
.venv/bin/pytest -v

# Preview pipeline changes without writing files or probing live streams:
.venv/bin/bellhop run --dry-run

# Run pipeline skipping live stream validation:
.venv/bin/bellhop run --skip validate

# Re-export M3U8 + Xtream JSON from existing state:
.venv/bin/bellhop run --phase export

# Launch unified Bellhop Web Guide + Passthrough Xtream API server (0.0.0.0:8080):
.venv/bin/bellhop serve
```

For full historical context, verified metrics, upstream provider quirks, and the persistent-daemon roadmap, read [.agents/HANDOFF_STATE.md](./.agents/HANDOFF_STATE.md).
