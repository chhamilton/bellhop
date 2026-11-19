---
name: bellhop-pipeline-ops
description: >-
  Use this skill when modifying, debugging, or running the 6-phase Bellhop pipeline
  (`PipelineRunner`), channel matching (`ChannelMatcher`), stream validation
  (`StreamValidator`), passthrough EPG & Xtream exports, or lineup backends (`tvguide`,
  `schedules_direct`, `yaml`).
---

# Bellhop 6-Phase Pipeline Operations & Development

## Phase Flow (`src/bellhop/runner.py`)
`PipelineRunner(config).run(options)` executes canonical phases in order:
1. `lineup` (`src/bellhop/lineup_retriever.py` + `src/bellhop/providers/`) — `30d` TTL in `state/lineups/`
2. `catalog` (`src/bellhop/catalog_fetcher.py` + `src/bellhop/xtream.py`) — `24h` TTL in `state/catalogs/`
3. `match` (`src/bellhop/matcher.py` + `src/bellhop/mapping_state.py`) — Incremental reconciliation of `state/channel_mapping.json` (`upgrade_hysteresis_score = 5.0`)
4. `validate` (`src/bellhop/validator.py`) — Sequential candidate-only probing (`only_changed: true` by default; checkpoints every 20 slots)
5. `m3u8` (`src/bellhop/playlist.py`) — Extended M3U8 (`output/bellhop.m3u8`) + `output/lineup_report.json`
6. `xtream` (`src/bellhop/xtream_exporter.py` + `src/bellhop/epg_loader.py`) — Lightweight ~4 KB upstream `xmltv.php` passthrough verification (`verify_epg_endpoint`) + export of `bellhop_xtream_playlist.json`, `get_live_streams.json`, and `get_live_categories.json`

## Safe Verification Workflow
1. Always test logic and strict static types first:
   ```bash
   .venv/bin/pytest -v
   ```
2. Preview pipeline changes against real workspace state without mutating disk or opening stream connections:
   ```bash
   .venv/bin/bellhop run --dry-run
   ```
3. Run offline export regeneration (after lineup/logo/template edits):
   ```bash
   .venv/bin/bellhop run --phase export
   ```

## Adding a New Lineup Backend
1. Subclass `LineupProvider` in `src/bellhop/providers/base.py`.
2. Implement `supports(lineup_id, backend_hint)`, `discover_lineups(...)`, and `retrieve_lineup(...)`.
3. Convert raw channel rows to `RawProviderSlot` and pass through `normalize_provider_slots()` (`src/bellhop/providers/normalizer.py`) so SD/HD pairing, alias cleanup, category/language inference, and sibling exclusion rules (`RDS` vs `RDS 2`) apply automatically.
4. Register the provider in `build_default_providers()` (`src/bellhop/lineup_retriever.py`) and add MockTransport tests in `tests/test_lineup_providers.py`.

## Detailed Scoring & Provider Reference
Read [provider-and-matcher-quirks.md](./references/provider-and-matcher-quirks.md) when tuning candidate scoring weights, handling `cf.smoothroad.site` edge cases, or debugging TV Guide / Schedules Direct wire formats.
