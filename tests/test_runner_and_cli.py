"""Tests for the config-driven `PipelineRunner` and single-command `bellhop run` CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bellhop.cli import main as cli_main
from bellhop.config import load_config
from bellhop.runner import (
  PipelineConfigError,
  PipelineRunner,
  PipelineRunOptions,
  parse_phase_tokens,
  parse_refresh_targets,
  resolve_active_phases,
)


def test_phase_and_refresh_parsing() -> None:
  assert parse_phase_tokens(["export"]) == ["m3u8", "xtream"]
  assert parse_phase_tokens(["3,1", "6"]) == ["lineup", "match", "xtream"]
  assert parse_refresh_targets([]) == {"lineup", "catalog", "epg"}
  assert parse_refresh_targets(["lineup,epg"]) == {"lineup", "epg"}

  with pytest.raises(PipelineConfigError):
    parse_phase_tokens(["nonexistent_phase"])


def test_dry_run_writes_nothing_and_selective_phases_compose(
  tmp_path: Path,
) -> None:
  import asyncio

  seed_catalog = tmp_path / "seed_streams.json"
  seed_catalog.write_text(
    json.dumps(
      [
        {
          "stream_id": 101,
          "name": "CA FR: RDS FHD",
          "category_name": "CA",
          "epg_channel_id": "RDS.ca",
        }
      ]
    ),
    encoding="utf-8",
  )
  mini_lineup = tmp_path / "lineup.yaml"
  mini_lineup.write_text(
    """\
lineup_id: CAN-BELLFIBE-MTL-X
provider: Bell Fibe TV
market: Montréal, QC
postal_prefix: H2Y
channels:
  - chno: 1108
    sd_chno: 108
    name: RDS
    callsign: RDS
    tvg_id: RDS.ca
    group: Sports
    language: fr
    regions: [qc, ca]
    aliases: [RDS]
""",
    encoding="utf-8",
  )

  state_dir = tmp_path / "state"
  out_m3u8 = tmp_path / "output" / "lineup.m3u8"
  out_report = tmp_path / "output" / "report.json"
  out_xtream = tmp_path / "output" / "xtream"

  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"""\
paths:
  state_dir: "{state_dir}"
  lineup_file: "{mini_lineup}"
  seed_json: "{seed_catalog}"
  m3u8_playlist_path: "{out_m3u8}"
  m3u8_report_path: "{out_report}"
  xtream_output_dir: "{out_xtream}"
upstream_xtream:
  server_url: "http://panel.example"
  username: "alice"
  password: "secret_password"
pipeline:
  lineup:
    lineup_id: "CAN-BELLFIBE-MTL-X"
    collapse_sd_dupes: true
  validation:
    enabled: true
    only_changed: true
  epg:
    verify: false
  output:
    m3u8_enabled: true
    xtream_enabled: true
""",
    encoding="utf-8",
  )

  cfg = load_config(cfg_path)
  redacted = cfg.to_dict(redact_secrets=True)
  assert redacted["xtream"]["password"] == "***"
  active, skipped = resolve_active_phases(cfg, skip_phases=["validate"])
  assert active == ["lineup", "catalog", "match", "m3u8", "xtream"]
  assert skipped == ["validate"]

  # 1. Dry-run computes match & validation queue without creating state/ or output/ files
  runner = PipelineRunner(cfg)
  dry_res = asyncio.run(runner.run(PipelineRunOptions(dry_run=True)))
  assert dry_res.dry_run is True
  assert dry_res.mapping_saved is False
  assert dry_res.validation_would_probe_slots == 1
  assert dry_res.validation_probed_streams == 0
  assert not (state_dir / "channel_mapping.json").exists()
  assert not out_m3u8.exists()
  assert not (out_xtream / "get_live_streams.json").exists()

  # 2. CLI `--phase lineup,catalog,match` persists mapping state only (no outputs yet)
  rc1 = cli_main(
    ["run", "-c", str(cfg_path), "--phase", "lineup,catalog,match", "--quiet"]
  )
  assert rc1 == 0
  assert (state_dir / "channel_mapping.json").is_file()
  assert not out_m3u8.exists()
  assert not (out_xtream / "get_live_streams.json").exists()

  # 3. CLI `--phase export` hydrates existing state and emits both M3U8 and Xtream JSON
  rc2 = cli_main(["run", "-c", str(cfg_path), "--phase", "export", "--quiet"])
  assert rc2 == 0
  assert out_m3u8.is_file()
  assert out_report.is_file()
  assert (out_xtream / "get_live_streams.json").is_file()

  # 4. Bare `bellhop -c ... --dry-run` defaults to `run`; invalid phase / missing config return exit code 2
  assert cli_main(["-c", str(cfg_path), "--dry-run", "--quiet"]) == 0
  assert (
    cli_main(["run", "-c", str(cfg_path), "--phase", "bogus_phase", "--quiet"])
    == 2
  )
  assert cli_main(["run", "-c", str(tmp_path / "missing.yaml"), "--quiet"]) == 2
