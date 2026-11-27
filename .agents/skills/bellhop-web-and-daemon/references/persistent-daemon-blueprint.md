# Blueprint: Persistent Internal Scheduler & Web Configuration/Monitoring App

This document captures the architectural plan for evolving `PipelineRunner` (`src/bellhop/pipeline/runner.py`) + `WebGuideApp` (`src/bellhop/web/server.py`) into a single self-scheduling, web-managed daemon.

## 1. Unified Tuner Arbitration (Viewer Always Wins)
Because `max_connections == 1`, live viewing and Phase (4) stream validation share one upstream tuner:
- Introduce a shared `TunerCoordinator`:
  - **Interactive Viewer Priority:** When a user opens `/api/stream/<chno>.ts` or `/api/hls/<chno>/index.m3u8`, any in-progress background Phase (4) validation task immediately pauses/cancels its current probe (checkpointing already-probed slots to `state/channel_mapping.json`) and yields the tuner within `<200ms`.
  - **Idle-Window Validation:** Scheduled `validate` runs execute during configured off-hours (e.g. `04:00` in configured timezone) and only while `active_viewer == None`.

## 2. Internal Async Scheduler (`SchedulerService`)
Add a `schedule:` block to `config.yaml`:
```yaml
schedule:
  enabled: true
  timezone: "UTC"
  # Daily catalog + match + EPG + export check (respects 30d lineup & 24h catalog TTLs)
  sync_cron_hour: 4
  sync_cron_minute: 0
  # Optional periodic lightweight EPG-only refresh (does not touch tuner)
  epg_refresh_hours: 12.0
```
- Runs as an `asyncio.Task` inside the server lifecycle (`startup()` / `shutdown()`).
- Invokes `PipelineRunner(config).run(PipelineRunOptions(...))` and appends each `PipelineRunResult.to_dict()` to `state/run_history.jsonl` (capped to e.g. last 100 runs), triggering `LineupGuideStore.get_snapshot(force_reload=True)` upon completion.

## 3. Control-Plane REST / SSE Endpoints
Extend `WebGuideApp` with admin endpoints:
- `GET /api/admin/status` — Active tuner state, scheduler next-run timestamp, current running phase/progress, and latest `PipelineRunResult`.
- `GET /api/admin/history` — Recent `PipelineRunResult` entries from `state/run_history.jsonl`.
- `POST /api/admin/run` — Trigger an immediate background run (`{phases, skip_phases, dry_run, force_refresh, reprobe_all}`), streaming live phase/slot progress via `GET /api/admin/events` (`text/event-stream` SSE).
- `GET /api/admin/config` — Future lineup-customization endpoint backed by `ConfigManager` returning strictly non-secret lineup/pipeline settings without exposing `web_server`, `xtream_server`, filesystem `paths`, or `credentials.yaml` secrets.
- `PUT /api/admin/config` — Validates and atomically persists future lineup-customization updates to `config.yaml` via `ConfigManager`, preserving YAML comments while leaving infrastructure settings and `credentials.yaml` secrets read-only from this endpoint.
- `GET /api/admin/discover-lineups` — Wraps `discover_provider_lineups()` so users can search TV Guide or Schedules Direct lineups by ZIP/postal code right from a dropdown in the web UI!

## 4. Web UI Additions (`Admin / Settings` Drawer or Tab)
Add a clean **Settings & Sync Status** view alongside `Guide` and `Timeline`:
- **Dashboard:** Last sync timestamp, channel coverage metrics, EPG item count, active tuner badge, and one-click buttons (`Sync Now (Skip Validation)`, `Dry-Run Preview`, `Full Off-Hours Validate`).
- **Lineup Picker:** Search by postal/ZIP code (`tvguide` or `schedules_direct`), pick a provider lineup, toggle `collapse_sd_dupes` / `exclude_audio`, and pin/exclude specific channel slots interactively.
