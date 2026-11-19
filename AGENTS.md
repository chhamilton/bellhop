# AGENTS.md — Bellhop Workspace Guide

## Project Overview
This repository implements **Bellhop**, installed via a single `bellhop` CLI (`bellhop run` & `bellhop serve`) from `pyproject.toml` and organized into two clean Python packages:
1. **`bellhop` (`src/bellhop/`)** — A declarative, stateful 6-phase pipeline (`PipelineRunner`) that maps a raw Xtream Codes IPTV catalog (~16,600+ live streams) onto an official cable/fiber TV lineup (currently **Bell Fibe TV — Montréal, QC**, scalable to any US/Canadian provider via TV Guide or Schedules Direct), validates **only** matched candidate streams, exports Extended **M3U8** + **Xtream JSON** playlists with passthrough EPG metadata, and serves the unified Web Guide + Passthrough Xtream Codes API server via `bellhop serve` (`UnifiedServeServer`).
2. **`bellhop_web` (`src/bellhop_web/`)** — The decoupled async HTTP/1.1 web guide UI, TypeScript frontend (`frontend/` → `static/`), cookie auth (`bellhop_session`), dynamic passthrough EPG cache (`cache/bellhop_web/epg_cache.json`), and **single-tuner relay** (`TunerSessionManager`), hosted by `bellhop serve` without importing `bellhop.*`.

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
     1. **Infrastructure (`config.yaml` only, never exposed to Web UI):** `credentials_file`, `paths`, and `web_server`.
     2. **Operational (exposed in Web UI Settings excluding credentials):** `upstream_xtream`, `xtream_server`, and `pipeline` (`lineup`, `matching`, `validation`, `epg`, `output`).
   - Do **not** add configuration flags back to the `bellhop` CLI. CLI arguments are strictly reserved for ephemeral execution control (`--config`, `--phase`, `--skip`, `--dry-run`, `--force-refresh`, `--reprobe-all`, `--reset-mapping`, `--quiet`, `--verbose`).

3. **Credentials & Git Hygiene**
   - `credentials.yaml`, `config.yaml`, `.env`, `cache/`, `state/`, and `output/` are gitignored—never commit secrets or raw provider dumps.
   - Do **not** mask passwords with `***` inside `state/channel_mapping.json`, `output/bellhop.m3u8`, or `output/xtream/get_live_streams.json`: Xtream stream URLs require embedded credentials to play (masking caused `HTTP 513` upstream errors). Redaction belongs only in UI/API serialization (`AppConfig.to_dict(redact_secrets=True)` and `WebChannel.to_public_dict()`, which exposes `/api/stream/<chno>.ts`), while `/api/config` (`export_public_config`) omits credentials, `web_server`, and paths altogether.

4. **Package Boundary (`bellhop` vs `bellhop_web`) & Strict Typing**
   - Keep `src/bellhop_web/` cleanly decoupled from `bellhop.*` imports (shared low-level HTTP/1.1 & bind-address helpers live in `src/bellhop_web/http_utils.py`). The single runtime CLI entry point is `bellhop` (`bellhop run` / `bellhop serve`).
   - Maintain `mypy --strict` across `src/` and `tests/` with zero `# type: ignore` suppressions; prefer explicit `@dataclass(slots=True)` structs and `TypedDict`s over nested `dict[str, Any]`.

5. **Coding Style Preferences (Google Python Style & Strict TypeScript Frontend)**
   - **Python Style:** Follow the **Google Python Style Guide** — **no tabs, spaces only, 2-space indentation per depth**, 80-char line length, Google-style docstrings (`Args:`, `Returns:`, `Raises:`), and sorted imports, enforced via `[tool.ruff]` in `pyproject.toml` (`.venv/bin/ruff format src tests` and `.venv/bin/ruff check src tests`).
   - **Frontend TypeScript:** All browser JS is authored in strictly typed TypeScript under `src/bellhop_web/frontend/` (`types.d.ts`, `prefs.ts`, `app.ts`, 2-space spaces-only indent, `tsconfig.json` `strict: true` + `noUncheckedIndexedAccess: true`) and compiled into `src/bellhop_web/static/{prefs.js,app.js}` via `.venv/bin/node ./node_modules/typescript/bin/tsc`. Never edit the generated `static/prefs.js` or `static/app.js` files directly.
   - **Client-Side State (`localStorage["bellhop.settings"]`):** Store persistent browser UI preferences (theme, future view/favorite/player preferences) as structured JSON (nested dicts, arrays, strings, numbers, booleans) through `window.BellhopSettings` (`src/bellhop_web/frontend/prefs.ts`) rather than ad-hoc top-level `localStorage` keys.

---

## Repository Map

```text
.
├── config.example.yaml          # Two-tier documented template for config.yaml
├── credentials.example.yaml     # Documented template for credentials.yaml
├── data/
│   ├── lineups/bell_fibe_montreal.yaml  # Curated 355-network / 698-slot Bell Fibe MTL lineup
│   └── official_logos.json              # 339 verified tv-logo/tv-logos transparent PNG URLs
├── src/
│   ├── bellhop/
│   │   ├── cli.py               # `bellhop run` & `bellhop serve` CLI
│   │   ├── config.py            # 2-tier + legacy AppConfig loader & scoped `/api/config` serializer
│   │   ├── runner.py            # Reusable 6-phase `PipelineRunner` engine
│   │   ├── lineup_retriever.py  # Phase (1) `lineup`: 30d snapshot + diff + backend dispatch
│   │   ├── providers/           # `LineupProvider` ABC + `tvguide`, `schedules_direct`, `yaml`, `normalizer`
│   │   ├── catalog_fetcher.py   # Phase (2) `catalog`: 24h Xtream JSON fetch + diff
│   │   ├── xtream.py            # Low-level Xtream `player_api.php` client
│   │   ├── matcher.py           # Multi-signal `ChannelMatcher` (identity, EPG, region, quality)
│   │   ├── mapping_state.py     # Phase (3) `match`: incremental state + 5.0-pt hysteresis
│   │   ├── validator.py         # Phase (4) `validate`: HTTP/MPEG-TS/HLS + optional ffprobe
│   │   ├── playlist.py          # Phase (5) `m3u8`: Extended M3U8 + JSON report
│   │   ├── epg_loader.py        # Passthrough `xmltv.php` URL builder & ~4 KB streaming verifier
│   │   ├── xtream_exporter.py   # Phase (6) `xtream`: Official Xtream JSON + passthrough EPG exporter
│   │   ├── xtream_server.py     # Passthrough Xtream Codes & M3U HTTP API server (`302` upstream)
│   │   └── serve.py             # `UnifiedServeServer` co-hosting Web Guide + Xtream API (shared or split ports)
│   └── bellhop_web/
│       ├── http_utils.py        # Shared async HTTP/1.1 parser, response writers & LAN bind helpers
│       ├── auth.py              # HMAC-SHA256 `bellhop_session` cookie auth & `/login?next=...` page
│       ├── epg_refresh.py       # Dynamic passthrough XMLTV fetcher & `cache/bellhop_web/epg_cache.json`
│       ├── store.py             # Hot-reloading reader for `output/xtream/` (with M3U8/XMLTV fallbacks)
│       ├── server.py            # `WebGuideApp` + `TunerSessionManager` chunked MPEG-TS & HLS proxy
│       ├── frontend/            # Strict TypeScript sources (`types.d.ts`, `prefs.ts`, `app.ts`)
│       └── static/              # HTML/CSS, SVG logo & ICO favicon, compiled `prefs.js`/`app.js`, vendor libs
├── Dockerfile                   # Multi-stage Alpine container (`node:22-alpine` -> `python:3.13-alpine`)
├── docker/                      # `entrypoint.sh` (`TZ`/`PUID`/`PGID`/mount seeding) & `config.docker.yaml`
├── docker-compose.example.yml   # Example Docker Compose stack with `/config`, `/data/{state,cache,output}`
├── .env.example                 # Example environment variables (container runtime + surfaced credentials)
├── tsconfig.json                # Strict TypeScript compiler config (`frontend/*.ts` -> `static/*.js`)
├── package.json                 # Dev-only TypeScript toolchain manifest
└── tests/                       # Offline Python, Node & Docker entrypoint/container E2E tests (`pytest`)
```

---

## Common Commands

```bash
# Bootstrap or verify environment (Python .venv + Ruff + Node/TypeScript):
./.agents/scripts/bootstrap.sh

# Format & lint Python (Google style, 2-space indent, zero tabs):
.venv/bin/ruff format src tests
.venv/bin/ruff check src tests

# Typecheck & compile TypeScript frontend -> src/bellhop_web/static/*.js:
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
