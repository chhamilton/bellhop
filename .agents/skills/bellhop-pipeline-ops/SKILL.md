---
name: bellhop-pipeline-ops
description: >-
  Use this skill when modifying, debugging, or running the 5-phase Bellhop pipeline
  (`PipelineRunner`), channel matching (`ChannelMatcher`), stream validation
  (`StreamValidator`), passthrough EPG & Xtream exports, or lineup backends (`tvguide`,
  `schedules_direct`, `yaml`).
---

# Bellhop 5-Phase Pipeline Operations & Development

## Phase Flow (`src/bellhop/pipeline/runner.py`)
`PipelineRunner(config).run(options)` executes canonical phases in order:
1. `lineup` (`src/bellhop/pipeline/lineup_retriever.py` + `src/bellhop/pipeline/providers/`) — `30d` TTL in `state/lineups/`
2. `catalog` (`src/bellhop/pipeline/catalog_fetcher.py` + `src/bellhop/xtream/client.py`) — `24h` TTL in `state/catalogs/` (uses `config.pipeline_account`)
3. `match` (`src/bellhop/pipeline/matcher.py` + `src/bellhop/pipeline/matcher_vocab.py` + `src/bellhop/pipeline/mapping_state.py`) — Incremental reconciliation of `state/channel_mapping.json` (`upgrade_hysteresis_score = 5.0`)
4. `validate` (`src/bellhop/pipeline/validator.py`) — Sequential candidate-only probing via `config.pipeline_account` (`only_changed: true` by default; checkpoints every 20 slots)
5. `export` (`src/bellhop/pipeline/playlist.py` + `src/bellhop/pipeline/xtream_exporter.py` + `src/bellhop/epg/xmltv.py` + `src/bellhop/epg/sync.py`) — Writes `output/lineup_report.json`, runs lightweight ~4 KB upstream `xmltv.php` passthrough verification (`verify_epg_endpoint`), exports credential-free `bellhop_xtream_playlist.json`, `get_live_streams.json`, and `get_live_categories.json`, and hydrates SQLite EPG (M3U8 playlists are served dynamically by `bellhop serve`)

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
1. Subclass `LineupProvider` in `src/bellhop/pipeline/providers/base.py`.
2. Implement `supports(lineup_id, backend_hint)`, `discover_lineups(...)`, and `retrieve_lineup(...)`.
3. Convert raw channel rows to `RawProviderSlot` and pass through `normalize_provider_slots()` (`src/bellhop/pipeline/providers/normalizer.py`) so SD/HD pairing, alias cleanup, category/language inference, and sibling exclusion rules (`RDS` vs `RDS 2`) apply automatically.
4. Register the provider in `build_default_providers()` (`src/bellhop/pipeline/lineup_retriever.py`) and add `MockHttpServer` tests in `tests/test_lineup_providers.py`.

## Detailed Scoring & Provider Reference
Read [provider-and-matcher-quirks.md](./references/provider-and-matcher-quirks.md) when tuning candidate scoring weights, handling upstream IPTV naming edge cases, or debugging TV Guide / Schedules Direct wire formats.
