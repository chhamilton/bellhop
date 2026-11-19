# GEMINI.md — Antigravity Environment Context

Welcome to the **Bellhop** workspace.

## Immediate Context & Onboarding
1. **Core Rules & Architecture:** Loaded automatically from [AGENTS.md](./AGENTS.md).
2. **Full Project State & Handoff Dump:** Read [.agents/HANDOFF_STATE.md](./.agents/HANDOFF_STATE.md) at the start of a new task for complete context on current channel coverage (`349/355` validated), state file layout, upstream Xtream/TVGuide/Schedules Direct quirks, and the next-phase **internal scheduler + explicit sync + multi-lineup/multi-playlist** roadmap.
3. **Workspace Skills (`.agents/skills/`):**
   - `bellhop-pipeline-ops` — Activate when modifying or debugging the 6-phase `PipelineRunner`, `ChannelMatcher`, `StreamValidator`, passthrough EPG verifier, or `LineupProvider` backends (`tvguide`, `schedules_direct`, `yaml`).
   - `bellhop-web-and-daemon` — Activate when working on `bellhop serve` (`WebGuideApp`, `TunerSessionManager`, `mpegts.js`/`hls.js` streaming, TypeScript Guide/Timeline/Login/Settings UI, Docker packaging) or building the persistent internal scheduler.

## Fresh Machine Setup
If opening this workspace in a new Antigravity container or machine where `.venv` is missing or broken (e.g. Python path changed):
```bash
./.agents/scripts/bootstrap.sh
```
Note: `credentials.yaml`, `config.yaml`, `state/`, `cache/`, and `output/` are gitignored. If transferring via Git only (rather than copying the full directory), copy `credentials.yaml` and `config.yaml` (and optionally `state/` + `output/`) alongside the repository, or regenerate `state/` and `output/` via `.venv/bin/bellhop run --skip validate`.
