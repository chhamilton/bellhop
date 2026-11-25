"""Unit tests for two-tier config loading, public config updates, and EPG
verification.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest
from aiohttp import web

from bellhop.config import (
  AppConfig,
  XtreamConfig,
  apply_public_config_update,
  export_public_config,
  load_config,
)
from bellhop.epg.xmltv import build_passthrough_xmltv_url, verify_epg_endpoint
from tests.http_mock import MockHttpServer


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
  assert cfg.serve.web.trust_proxy_headers is False
  assert cfg.serve.web.trusted_proxy_ips == ["127.0.0.1", "::1"]
  assert cfg.serve.xtream.username == "passthrough_client"
  assert cfg.serve.xtream.password == "passthrough_secret"
  assert cfg.serve.xtream.public_host == "tv.home.arpa"
  assert cfg.serve.xtream.server_protocol == "https"

  pub = export_public_config(cfg)
  assert set(pub.keys()) == {"upstream_xtream", "pipeline"}

  bad_proxy_cfg = tmp_path / "bad_proxy.yaml"
  bad_proxy_cfg.write_text(
    'web_server:\n  trust_proxy_headers: true\n  trusted_proxy_ips: ["not-an-ip"]\n',
    encoding="utf-8",
  )
  with pytest.raises(ValueError, match="trusted_proxy_ips"):
    load_config(bad_proxy_cfg)


def test_apply_public_config_update_and_validation_errors(
  tmp_path: Path,
) -> None:
  cfg_file = tmp_path / "config.yaml"
  cfg_file.write_text(
    """\
web_server:
  enabled: false
  host: "127.0.0.1"
  port: 8080
upstream_xtream:
  server_url: "http://provider.example"
  username: "up_u"
  password: "up_p"
  stream_format: "ts"
xtream_server:
  enabled: true
  username: "xt_u"
  password: "xt_p"
pipeline:
  lineup:
    backend: "yaml"
    lineup_id: "CAN-BELLFIBE-MTL-X"
  matching:
    upgrade_hysteresis_score: 5.0
  output:
    m3u8_enabled: true
    xtream_enabled: true
""",
    encoding="utf-8",
  )

  import stat

  updated, restart_req = apply_public_config_update(
    cfg_file,
    {
      "upstream_xtream": {"stream_format": "m3u8", "request_timeout_sec": 25.0},
      "xtream_server": {"public_host": "hacked.local", "enabled": False},
      "pipeline": {
        "matching": {"upgrade_hysteresis_score": 8.0},
        "output": {"m3u8_enabled": False},
      },
    },
  )
  assert restart_req is False
  assert stat.S_IMODE(cfg_file.stat().st_mode) == 0o600
  assert updated.xtream.stream_format == "m3u8"
  assert updated.xtream.username == "up_u"
  assert updated.xtream.password == "up_p"
  assert updated.state.upgrade_hysteresis_score == 8.0
  assert updated.output.m3u8_enabled is False
  # Server properties (`xtream_server` / `web_server`) are config-file-only and
  # ignored!
  assert updated.serve.xtream.public_host is None
  assert updated.serve.xtream.enabled is True
  assert updated.serve.xtream.password == "xt_p"

  # Validation failures leave existing file untouched
  invalid_payloads: list[dict[str, object]] = [
    {"upstream_xtream": {"stream_format": "mp4"}},
    {"pipeline": {"lineup": {"backend": "unknown_backend"}}},
    {"pipeline": {"lineup": {"lineup_id": "   "}}},
    {"pipeline": {"lineup": {"numbering_mode": "invalid"}}},
    {"pipeline": {"lineup": {"pinned_streams": "not-a-dict"}}},
    {"pipeline": {"lineup": {"excluded_channels": "not-a-list"}}},
  ]
  for bad in invalid_payloads:
    with pytest.raises(ValueError):
      apply_public_config_update(cfg_file, bad)


def test_apply_public_config_update_preserves_comments_and_env_placeholders(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  repo_root = Path(__file__).resolve().parents[1]

  # 1. Full `config.example.yaml` round-trip: every comment line survives save.
  example_text = (repo_root / "config.example.yaml").read_text(encoding="utf-8")
  example_comments = [
    line.strip()
    for line in example_text.splitlines()
    if line.strip().startswith("#")
  ]
  assert len(example_comments) >= 40

  cfg_example_copy = tmp_path / "config.yaml"
  cfg_example_copy.write_text(example_text, encoding="utf-8")

  loaded = load_config(cfg_example_copy)
  ui_payload: dict[str, object] = dict(export_public_config(loaded))
  ui_payload["upstream_xtream"] = {
    **dict(export_public_config(loaded)["upstream_xtream"]),
    "stream_format": "m3u8",
  }
  updated_cfg, _ = apply_public_config_update(cfg_example_copy, ui_payload)
  assert updated_cfg.xtream.stream_format == "m3u8"

  saved_example_text = cfg_example_copy.read_text(encoding="utf-8")
  saved_comments = [
    line.strip()
    for line in saved_example_text.splitlines()
    if line.strip().startswith("#")
  ]
  assert saved_comments == example_comments
  # Double-quoted scalar style, `null` representation, and comments survive:
  assert 'stream_format: "m3u8"' in saved_example_text
  assert (
    '# Stream output format: "ts" (MPEG-TS, lowest latency) or "m3u8" (HLS)'
    in saved_example_text
  )
  assert "host: null" in saved_example_text

  # 2. Docker config with `${ENV}` placeholders on both secrets AND
  #    `server_url`:
  # saving unrelated Settings in the Web UI keeps `${XTREAM_SERVER_URL}` intact!
  monkeypatch.setenv("XTREAM_SERVER_URL", "http://env-provider.example:8080")
  monkeypatch.setenv("XTREAM_USERNAME", "env_xt_user")
  monkeypatch.setenv("XTREAM_PASSWORD", "env_xt_pass")
  monkeypatch.setenv("BELLHOP_WEB_USERNAME", "env_web_admin")
  monkeypatch.setenv("BELLHOP_WEB_PASSWORD", "env_web_pass")
  monkeypatch.setenv("BELLHOP_SERVE_USERNAME", "env_srv_user")
  monkeypatch.setenv("BELLHOP_SERVE_PASSWORD", "env_srv_pass")

  docker_template = (repo_root / "docker" / "config.docker.yaml").read_text(
    encoding="utf-8"
  )
  docker_cfg_path = tmp_path / "docker_config.yaml"
  docker_cfg_path.write_text(docker_template, encoding="utf-8")

  before_docker = load_config(docker_cfg_path)
  assert before_docker.xtream.server_url == "http://env-provider.example:8080"

  # Browser receives resolved `server_url` from GET /api/config and sends it
  # back unchanged
  # alongside an edited pipeline knob (`upgrade_hysteresis_score: 7.5`).
  public_doc = export_public_config(before_docker)
  save_body: dict[str, object] = {
    "upstream_xtream": dict(public_doc["upstream_xtream"]),
    "pipeline": {
      **dict(public_doc["pipeline"]),
      "matching": {"upgrade_hysteresis_score": 7.5},
    },
  }
  after_docker, _ = apply_public_config_update(docker_cfg_path, save_body)
  assert after_docker.state.upgrade_hysteresis_score == 7.5

  raw_disk = docker_cfg_path.read_text(encoding="utf-8")
  for placeholder in (
    "${XTREAM_SERVER_URL}",
    "${XTREAM_USERNAME}",
    "${XTREAM_PASSWORD}",
    "${BELLHOP_WEB_USERNAME}",
    "${BELLHOP_WEB_PASSWORD}",
    "${BELLHOP_SERVE_USERNAME}",
    "${BELLHOP_SERVE_PASSWORD}",
    "${SD_USERNAME}",
    "${SD_PASSWORD}",
  ):
    assert placeholder in raw_disk
  assert "http://env-provider.example:8080" not in raw_disk
  assert "# 1. HOST & INFRASTRUCTURE" in raw_disk
  assert "# 2. OPERATIONAL SETTINGS" in raw_disk

  # Explicitly changing `server_url` to a new URL DOES replace
  # `${XTREAM_SERVER_URL}`.
  changed_url_cfg, _ = apply_public_config_update(
    docker_cfg_path,
    {"upstream_xtream": {"server_url": "http://explicit-new.example:8080"}},
  )
  assert changed_url_cfg.xtream.server_url == "http://explicit-new.example:8080"
  assert (
    'server_url: "http://explicit-new.example:8080"'
    in docker_cfg_path.read_text(encoding="utf-8")
  )


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

  def mock_epg(_req: web.Request) -> web.Response:
    if mode["kind"] == "gzip":
      return web.Response(
        status=200,
        headers={"Content-Type": "application/gzip"},
        body=gzip.compress(
          b'<?xml version="1.0"?><tv generator-info-name="test"></tv>'
        ),
      )
    if mode["kind"] == "html":
      return web.Response(
        status=200,
        content_type="text/html",
        text="<html>Denied</html>",
      )
    return web.Response(status=503, text="Service Unavailable")

  remote_cfg = AppConfig(
    project_root=tmp_path,
    xtream=XtreamConfig(
      server_url="http://epg.example", username="u", password="secret"
    ),
  )
  assert build_passthrough_xmltv_url(remote_cfg, redact_secret=True) == (
    "http://epg.example/xmltv.php?username=u&password=***"
  )

  async with MockHttpServer(mock_epg) as mock, mock.new_session() as client:
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
