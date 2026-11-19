"""Unit tests for 2-tier & legacy config loading, public config updates, and EPG verification."""

from __future__ import annotations

import gzip
from pathlib import Path

import httpx
import pytest

from bellhop.config import (
  AppConfig,
  XtreamConfig,
  apply_public_config_update,
  export_public_config,
  load_config,
)
from bellhop.epg_loader import build_passthrough_xmltv_url, verify_epg_endpoint


def test_two_tier_config_with_credentials_yaml_and_env_interpolation(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setenv("TEST_XT_PASS", "env_xtream_secret")
  monkeypatch.setenv("TEST_WEB_PASS", "env_web_secret")

  creds_path = tmp_path / "credentials.yaml"
  creds_path.write_text(
    """\
upstream_xtream:
  server_url: "http://provider.example:8080"
  username: "xt_account"
  password: "${TEST_XT_PASS}"

schedules_direct:
  username: "sd_account"
  password: "sd_secret_password"

web_server:
  username: "guide_admin"
  password: "${TEST_WEB_PASS}"

xtream_server:
  username: "passthrough_client"
  password: "passthrough_secret"
""",
    encoding="utf-8",
  )

  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"""\
credentials_file: "credentials.yaml"

paths:
  cache_dir: "{tmp_path / "custom_cache"}"
  state_dir: "{tmp_path / "custom_state"}"
  xtream_output_dir: "{tmp_path / "custom_xtream"}"

web_server:
  enabled: true
  host: "0.0.0.0"
  port: 8181
  settle_ms: 220
  session_ttl_hours: 72.0

upstream_xtream:
  stream_format: "m3u8"
  request_timeout_sec: 45.0
  refresh_hours: 12.0

xtream_server:
  enabled: true
  host: null
  port: null
  public_host: "tv.home.arpa"
  server_protocol: "https"

pipeline:
  lineup:
    backend: "tvguide"
    lineup_id: "TVG-9100000607"
    postal_code: "H2Y1C6"
    collapse_sd_dupes: true
    exclude_audio: true
    pinned_streams:
      1108: 410108
    excluded_channels: [1107]
  matching:
    upgrade_hysteresis_score: 6.5
  validation:
    enabled: false
    max_candidates_per_channel: 7
  epg:
    verify: true
    refresh_hours: 4.0
  output:
    m3u8_enabled: false
    xtream_enabled: true
    include_unmatched_comment: true
""",
    encoding="utf-8",
  )

  cfg = load_config(cfg_path)
  assert cfg.xtream.server_url == "http://provider.example:8080"
  assert cfg.xtream.username == "xt_account"
  assert cfg.xtream.password == "env_xtream_secret"
  assert cfg.xtream.stream_format == "m3u8"
  assert cfg.xtream.cache_ttl_hours == 12.0
  assert cfg.xtream.cache_dir == tmp_path / "custom_cache"

  assert cfg.lineup.backend == "tvguide"
  assert cfg.lineup.lineup_id == "TVG-9100000607"
  assert cfg.lineup.numbering_mode == "hd"
  assert cfg.lineup.pinned_streams == {1108: 410108}
  assert cfg.lineup.excluded_channels == {1107}
  assert cfg.lineup.schedules_direct.username == "sd_account"
  assert cfg.lineup.schedules_direct.password == "sd_secret_password"

  assert cfg.state.state_dir == tmp_path / "custom_state"
  assert (
    cfg.state.mapping_path == tmp_path / "custom_state" / "channel_mapping.json"
  )
  assert cfg.state.upgrade_hysteresis_score == 6.5

  assert cfg.validation.enabled is False
  assert cfg.validation.max_candidates_per_channel == 7
  assert cfg.epg.verify is True
  assert cfg.epg.refresh_hours == 4.0
  assert cfg.output.m3u8_enabled is False
  assert cfg.output.xtream_dir == tmp_path / "custom_xtream"

  assert cfg.serve.web_host == "0.0.0.0"
  assert cfg.serve.web_port == 8181
  assert cfg.serve.xtream_host == "0.0.0.0"
  assert cfg.serve.xtream_port == 8181
  assert cfg.serve.web.username == "guide_admin"
  assert cfg.serve.web.password == "env_web_secret"
  assert cfg.serve.xtream.username == "passthrough_client"
  assert cfg.serve.xtream.password == "passthrough_secret"
  assert cfg.serve.xtream.public_host == "tv.home.arpa"
  assert cfg.serve.xtream.server_protocol == "https"

  pub = export_public_config(cfg)
  assert set(pub.keys()) == {"upstream_xtream", "xtream_server", "pipeline"}


def test_apply_public_config_update_legacy_layout_and_validation_errors(
  tmp_path: Path,
) -> None:
  legacy_cfg = tmp_path / "legacy.yaml"
  legacy_cfg.write_text(
    """\
xtream:
  server_url: "http://legacy.example"
  username: "legacy_u"
  password: "legacy_p"
  stream_format: "ts"
lineup:
  backend: "yaml"
  lineup_id: "CAN-BELLFIBE-MTL-X"
state:
  upgrade_hysteresis_score: 5.0
output:
  m3u8:
    enabled: true
  xtream:
    enabled: true
serve:
  host: "127.0.0.1"
  port: 8080
  web:
    enabled: false
  xtream:
    enabled: true
    username: "xt_u"
    password: "xt_p"
""",
    encoding="utf-8",
  )

  updated, restart_req = apply_public_config_update(
    legacy_cfg,
    {
      "upstream_xtream": {"stream_format": "m3u8", "request_timeout_sec": 25.0},
      "xtream_server": {"public_host": "lan.local"},
      "pipeline": {
        "matching": {"upgrade_hysteresis_score": 8.0},
        "output": {"m3u8_enabled": False},
      },
    },
  )
  assert restart_req is False
  assert updated.xtream.stream_format == "m3u8"
  assert updated.xtream.username == "legacy_u"
  assert updated.xtream.password == "legacy_p"
  assert updated.state.upgrade_hysteresis_score == 8.0
  assert updated.output.m3u8_enabled is False
  assert updated.serve.xtream.public_host == "lan.local"
  assert updated.serve.xtream.password == "xt_p"

  # Validation failures leave existing file untouched
  invalid_payloads: list[dict[str, object]] = [
    {"upstream_xtream": {"stream_format": "mp4"}},
    {"xtream_server": {"server_protocol": "ftp"}},
    {"pipeline": {"lineup": {"backend": "unknown_backend"}}},
    {"pipeline": {"lineup": {"lineup_id": "   "}}},
    {"pipeline": {"lineup": {"numbering_mode": "invalid"}}},
    {"pipeline": {"lineup": {"pinned_streams": "not-a-dict"}}},
    {"pipeline": {"lineup": {"excluded_channels": "not-a-list"}}},
    # Disabling xtream_server when web.enabled is already false must fail
    {"xtream_server": {"enabled": False}},
  ]
  for bad in invalid_payloads:
    with pytest.raises(ValueError):
      apply_public_config_update(legacy_cfg, bad)


@pytest.mark.asyncio
async def test_verify_epg_endpoint_plain_gzip_local_and_errors(
  tmp_path: Path,
) -> None:
  # 1. Unconfigured credentials -> skipped=True
  empty_cfg = AppConfig(project_root=tmp_path)
  assert build_passthrough_xmltv_url(empty_cfg) == ""
  skipped_res = await verify_epg_endpoint(empty_cfg)
  assert skipped_res.skipped is True
  assert skipped_res.verified is False

  # 2. Local xmltv_file: missing vs valid gzipped XMLTV
  missing_cfg = AppConfig(project_root=tmp_path)
  missing_cfg.epg.xmltv_file = Path("missing.xml")
  missing_res = await verify_epg_endpoint(missing_cfg)
  assert missing_res.verified is False
  assert "not found" in (missing_res.error or "")

  gz_xml = tmp_path / "guide.xml.gz"
  gz_xml.write_bytes(gzip.compress(b'<?xml version="1.0"?><tv></tv>'))
  gz_cfg = AppConfig(project_root=tmp_path)
  gz_cfg.epg.xmltv_file = gz_xml
  gz_res = await verify_epg_endpoint(gz_cfg)
  assert gz_res.verified is True
  assert gz_res.format_detected == "xmltv_gzip"

  # 3. Remote streaming probe: gzipped XMLTV, non-XML HTML body, and HTTP 503
  mode = {"kind": "gzip"}

  def mock_epg(req: httpx.Request) -> httpx.Response:
    if mode["kind"] == "gzip":
      return httpx.Response(
        200,
        headers={"content-type": "application/gzip"},
        content=gzip.compress(
          b'<?xml version="1.0"?><tv generator-info-name="test"></tv>'
        ),
      )
    if mode["kind"] == "html":
      return httpx.Response(
        200, headers={"content-type": "text/html"}, text="<html>Denied</html>"
      )
    return httpx.Response(503, text="Service Unavailable")

  remote_cfg = AppConfig(
    project_root=tmp_path,
    xtream=XtreamConfig(
      server_url="http://epg.example", username="u", password="secret"
    ),
  )
  assert build_passthrough_xmltv_url(remote_cfg, redact_secret=True) == (
    "http://epg.example/xmltv.php?username=u&password=***"
  )

  async with httpx.AsyncClient(
    transport=httpx.MockTransport(mock_epg)
  ) as client:
    ok_gz = await verify_epg_endpoint(remote_cfg, client=client)
    assert ok_gz.verified is True
    assert ok_gz.format_detected == "xmltv_gzip"
    assert "***" in ok_gz.to_dict()["url"]

    mode["kind"] = "html"
    bad_body = await verify_epg_endpoint(remote_cfg, client=client)
    assert bad_body.verified is False
    assert "not valid XMLTV" in (bad_body.error or "")

    mode["kind"] = "503"
    bad_status = await verify_epg_endpoint(remote_cfg, client=client)
    assert bad_status.verified is False
    assert bad_status.status_code == 503
