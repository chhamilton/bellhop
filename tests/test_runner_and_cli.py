"""Tests for the config-driven `PipelineRunner` and single-command `bellhop run` CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bellhop.cli import main as cli_main
from bellhop.config import load_config
from bellhop.pipeline.runner import (
  PipelineConfigError,
  PipelineRunner,
  PipelineRunOptions,
  parse_phase_tokens,
  parse_refresh_targets,
  resolve_active_phases,
)


def test_phase_and_refresh_parsing() -> None:
  assert parse_phase_tokens(["export"]) == ["m3u8", "xtream"]
  assert parse_phase_tokens(["epg"]) == ["xtream"]
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

  cache_dir = tmp_path / "cache"
  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"""\
credentials_file: "{tmp_path / "none.yaml"}"
paths:
  cache_dir: "{cache_dir}"
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

  # 3b. CLI `--phase epg` populates `cache/epg.sqlite3` directly
  xml_local = tmp_path / "local.xml"
  xml_local.write_text(
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<tv>\n"
    '  <programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="RDS.ca">\n'
    "    <title>Hockey Canadiens</title>\n"
    "  </programme>\n"
    "</tv>\n",
    encoding="utf-8",
  )
  cfg_with_xml = cfg_path.read_text(encoding="utf-8").replace(
    "xmltv_file: null", f'xmltv_file: "{xml_local}"'
  )
  if "xmltv_file:" not in cfg_with_xml:
    cfg_with_xml = cfg_with_xml.replace(
      "paths:\n", f'paths:\n  xmltv_file: "{xml_local}"\n'
    )
  cfg_path.write_text(cfg_with_xml, encoding="utf-8")
  rc_epg = cli_main(["run", "-c", str(cfg_path), "--phase", "epg", "--quiet"])
  assert rc_epg == 0
  assert (tmp_path / "cache" / "epg.sqlite3").is_file()

  # 4. Bare `bellhop -c ... --dry-run` defaults to `run`; invalid phase / missing config return exit code 2
  assert cli_main(["-c", str(cfg_path), "--dry-run", "--quiet"]) == 0
  assert (
    cli_main(["run", "-c", str(cfg_path), "--phase", "bogus_phase", "--quiet"])
    == 2
  )
  assert cli_main(["run", "-c", str(tmp_path / "missing.yaml"), "--quiet"]) == 2


async def test_expired_account_auto_skips_validate_and_completes_matching_and_exports(
  tmp_path: Path,
) -> None:
  """Verify expired Xtream accounts implicitly skip `validate` and finish matching + exports."""
  from aiohttp import web

  status_box = {"status": "Active"}
  stream_probes: list[str] = []

  async def handle_api(request: web.Request) -> web.Response:
    action = request.query.get("action", "")
    if not action:
      return web.json_response(
        {
          "user_info": {
            "username": "alice",
            "status": status_box["status"],
            "auth": 1,
            "active_cons": "0",
            "max_connections": "1",
          },
          "server_info": {"server_protocol": "http"},
        }
      )
    if action == "get_live_categories":
      return web.json_response([{"category_id": "1", "category_name": "CA"}])
    return web.json_response(
      [
        {
          "stream_id": 501,
          "name": "CA: YTV",
          "category_id": "1",
          "epg_channel_id": "YTV.ca",
        }
      ]
    )

  async def handle_stream(request: web.Request) -> web.Response:
    stream_probes.append(request.path)
    return web.Response(status=503, text="Expired placeholder")

  app = web.Application()
  app.router.add_get("/player_api.php", handle_api)
  app.router.add_get("/live/{user}/{pw}/{sid}", handle_stream)
  runner_srv = web.AppRunner(app)
  await runner_srv.setup()
  site = web.TCPSite(runner_srv, "127.0.0.1", 0)
  await site.start()
  try:
    port = int(runner_srv.addresses[0][1])

    mini_lineup = tmp_path / "lineup.yaml"
    mini_lineup.write_text(
      """\
lineup_id: CAN-BELLFIBE-MTL-X
provider: Bell Fibe TV
market: Montréal, QC
postal_prefix: H2Y
channels:
  - chno: 552
    name: YTV
    callsign: YTV
    tvg_id: YTV.ca
    group: Kids
    language: en
    regions: [ca]
    aliases: [YTV]
""",
      encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    out_m3u8 = tmp_path / "output" / "bellhop.m3u8"
    out_xtream = tmp_path / "output" / "xtream" / "get_live_streams.json"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
      f"""\
paths:
  state_dir: "{state_dir}"
  cache_dir: "{tmp_path / "cache"}"
  lineup_file: "{mini_lineup}"
  m3u8_playlist_path: "{out_m3u8}"
  m3u8_report_path: "{tmp_path / "output" / "report.json"}"
  xtream_output_dir: "{tmp_path / "output" / "xtream"}"
upstream_xtream:
  server_url: "http://127.0.0.1:{port}"
  username: "alice"
  password: "secret"
pipeline:
  lineup:
    lineup_id: "CAN-BELLFIBE-MTL-X"
  validation:
    enabled: true
  epg:
    verify: false
  output:
    m3u8_enabled: true
    xtream_enabled: true
""",
      encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    # Run 1: fetch active catalog + match (explicitly skip validate)
    await PipelineRunner(cfg).run(PipelineRunOptions(skip_phases=["validate"]))

    # Account expires while daily catalog snapshot is still fresh (<24h TTL)
    status_box["status"] = "Expired"
    res_cached = await PipelineRunner(cfg).run()
    assert res_cached.validation_skipped_reason == (
      "Xtream account status is 'Expired'"
    )
    assert "validate" in res_cached.skipped_phases
    assert "validate" not in res_cached.executed_phases
    assert "m3u8" in res_cached.executed_phases
    assert "xtream" in res_cached.executed_phases
    assert out_m3u8.is_file() and "YTV" in out_m3u8.read_text(encoding="utf-8")
    assert out_xtream.is_file()

    # Even with `--force-refresh catalog` while Expired, matching + exports succeed
    res_fresh = await PipelineRunner(cfg).run(
      PipelineRunOptions(force_refresh={"catalog"})
    )
    assert res_fresh.validation_skipped_reason == (
      "Xtream account status is 'Expired'"
    )
    assert stream_probes == []
    saved = json.loads((state_dir / "channel_mapping.json").read_text())
    assert (
      saved["slots"]["552"]["candidates"][0]["validation_status"]
      == "unvalidated"
    )
    assert saved["slots"]["552"]["preferred_stream_id"] == 501
  finally:
    await runner_srv.cleanup()


def test_cli_non_quiet_panels_dry_run_export_and_empty_phases(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  """Verify non-quiet Rich panels render for `--dry-run`, `--phase export`, and empty phase sets."""
  import io

  from rich.console import Console

  from bellhop import cli

  buf = io.StringIO()
  monkeypatch.setattr(
    cli,
    "console",
    Console(file=buf, width=120, force_terminal=False, color_system=None),
  )

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
  xml_local = tmp_path / "guide.xml"
  xml_local.write_text(
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<tv>\n"
    '  <programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="RDS.ca">\n'
    "    <title>Hockey Canadiens</title>\n"
    "  </programme>\n"
    "</tv>\n",
    encoding="utf-8",
  )
  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"""\
credentials_file: "{tmp_path / "none.yaml"}"
paths:
  cache_dir: "{tmp_path / "cache"}"
  state_dir: "{tmp_path / "state"}"
  lineup_file: "{mini_lineup}"
  seed_json: "{seed_catalog}"
  m3u8_playlist_path: "{tmp_path / "output" / "bellhop.m3u8"}"
  m3u8_report_path: "{tmp_path / "output" / "report.json"}"
  xtream_output_dir: "{tmp_path / "output" / "xtream"}"
  xmltv_file: "{xml_local}"
upstream_xtream:
  server_url: "http://panel.example"
  username: "alice"
  password: "secret_password"
pipeline:
  lineup:
    lineup_id: "CAN-BELLFIBE-MTL-X"
    collapse_sd_dupes: false
    numbering_mode: "both"
  validation:
    enabled: true
  epg:
    verify: false
  output:
    m3u8_enabled: true
    xtream_enabled: true
""",
    encoding="utf-8",
  )

  # 1. Non-quiet `--dry-run` renders Phase 1–5 panels without writing files
  assert cli_main(["run", "-c", str(cfg_path), "--dry-run"]) == 0
  dry_out = buf.getvalue()
  assert "Phase (1) — Lineup Retrieval" in dry_out
  assert "Phase (2) — IPTV Catalog Fetch" in dry_out
  assert "Phase (3) — Rough Channel Matcher" in dry_out
  assert "Dry-Run Mode" in dry_out
  assert "Bell Fibe TV (Montréal, QC)" in dry_out

  # 2. Non-quiet `--skip validate --force-refresh epg` emits full SD+HD M3U8 + Xtream + EPG sync panels
  buf.truncate(0)
  buf.seek(0)
  assert (
    cli_main(
      [
        "run",
        "-c",
        str(cfg_path),
        "--skip",
        "validate",
        "--force-refresh",
        "epg",
      ]
    )
    == 0
  )
  export_out = buf.getvalue()
  assert "Phase (5) — M3U8 & Diagnostics Generation" in export_out
  assert "full SD + HD dial" in export_out
  assert "Phase (6) — Official Xtream JSON & EPG Export" in export_out
  assert "EPG Cache Sync:" in export_out
  assert "OK" in export_out

  # 3. Non-quiet empty phase set prints warning and exits 0
  buf.truncate(0)
  buf.seek(0)
  assert (
    cli_main(
      ["run", "-c", str(cfg_path), "--phase", "lineup", "--skip", "lineup"]
    )
    == 0
  )
  assert "No active phases selected" in buf.getvalue()


@pytest.mark.asyncio
async def test_cli_serve_shared_and_split_banners_and_clean_cancellation(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  """Verify `_execute_cli_serve` renders shared & split/Xtream-only banners and stops cleanly."""
  import asyncio
  import io

  from rich.console import Console

  from bellhop import cli
  from tests.conftest import await_until, write_minimal_xtream_export

  buf = io.StringIO()
  monkeypatch.setattr(
    cli,
    "console",
    Console(file=buf, width=120, force_terminal=False, color_system=None),
  )

  xt_dir = write_minimal_xtream_export(tmp_path / "output" / "xtream")

  # Case A: Shared Web Guide + Xtream listener on ephemeral port 0
  cfg_shared = tmp_path / "config_shared.yaml"
  cfg_shared.write_text(
    f"""\
paths:
  cache_dir: "{tmp_path / "cache"}"
  state_dir: "{tmp_path / "state"}"
  xtream_output_dir: "{xt_dir}"
web_server:
  enabled: true
  host: "127.0.0.1"
  port: 0
  username: "web_admin"
  password: "web_secret"
xtream_server:
  enabled: true
  username: "xt_user"
  password: "xt_secret"
pipeline:
  epg:
    verify: false
    refresh_hours: 6.0
""",
    encoding="utf-8",
  )

  args_shared = cli.build_parser().parse_args(
    ["serve", "-c", str(cfg_shared), "-v"]
  )
  serve_task = asyncio.create_task(cli._execute_cli_serve(args_shared))
  await await_until(
    lambda: "Unified Web Guide & Xtream API Server" in buf.getvalue(),
    description="shared bellhop serve banner",
  )
  serve_task.cancel()
  assert await serve_task == 0
  shared_banner = buf.getvalue()
  assert "shared Web Guide + Xtream API" in shared_banner
  assert "web_admin" in shared_banner
  assert "xt_user" in shared_banner

  # Case B: Split / Web-disabled Xtream-only listener (`web_server.enabled: false`)
  buf.truncate(0)
  buf.seek(0)
  cfg_split = tmp_path / "config_split.yaml"
  cfg_split.write_text(
    f"""\
paths:
  cache_dir: "{tmp_path / "cache"}"
  state_dir: "{tmp_path / "state"}"
  xtream_output_dir: "{xt_dir}"
web_server:
  enabled: false
  host: "127.0.0.1"
  port: 0
xtream_server:
  enabled: true
  host: "127.0.0.1"
  port: 0
pipeline:
  epg:
    verify: false
""",
    encoding="utf-8",
  )
  args_split = cli.build_parser().parse_args(["serve", "-c", str(cfg_split)])
  split_task = asyncio.create_task(cli._execute_cli_serve(args_split))
  await await_until(
    lambda: "Xtream API URL:" in buf.getvalue(),
    description="split Xtream-only bellhop serve banner",
  )
  split_task.cancel()
  assert await split_task == 0
  split_banner = buf.getvalue()
  assert "Disabled (`web_server.enabled: false`)" in split_banner
  assert "Auth Disabled" in split_banner
