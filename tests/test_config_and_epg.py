"""Tests for file-backed config/credentials normalization, manager, and EPG."""

from __future__ import annotations

import gzip
import re
import stat
from pathlib import Path

import pytest
from aiohttp import web

from bellhop.common.accounts import (
  STREAM_CREDENTIAL_ALPHABET,
  STREAM_CREDENTIAL_LENGTH,
  verify_web_password,
)
from bellhop.config import (
  DEFAULT_IPTV_ORG_API_URL,
  DEFAULT_SD_BASE_URL,
  DEFAULT_TVGUIDE_BASE_URL,
  AppConfig,
  ConfigManager,
  IptvOrgConfig,
  LineupConfig,
  SchedulesDirectConfig,
  XtreamConfig,
  clear_config_cache,
  load_config,
)
from bellhop.epg.xmltv import build_passthrough_xmltv_url, verify_epg_endpoint
from tests.conftest import TEST_PASSWORD_HASH, make_test_account
from tests.http_mock import MockHttpServer


def test_file_backed_config_and_credential_auto_normalization(
  tmp_path: Path,
) -> None:
  creds_path = tmp_path / "credentials.yaml"
  creds_path.write_text(
    """\
# Top-level credential comment should survive
accounts:
  admin:
    # Keep per-account comments intact
    email: "Admin@Example.COM"
    web_password: "plain_web_secret_password"
    # Omit xtream_* so 8-char credentials are auto-generated
    upstream_username: "up_admin"
    upstream_password: "up_admin_secret"
  family:
    email: "family@example.com"
    web_password: "family_web_secret_password"
    # Accept legacy/custom non-8-char Xtream credentials without rewriting
    xtream_username: "LegacyFamily_Player"
    xtream_password: "LegacyPassword!123"
    upstream_username: "up_family"
    upstream_password: "up_family_secret"
    pipeline: true

schedules_direct:
  username: "sd_account"
  password: "sd_secret_password"
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
  report_path: "{tmp_path / "custom_report.json"}"
  xtream_output_dir: "{tmp_path / "custom_xtream"}"

web_server:
  enabled: true
  host: "0.0.0.0"
  port: 8181
  settle_ms: 220
  session_ttl_hours: 72.0

upstream_xtream:
  server_url: "http://provider.example:8080"
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
    lineup_id: "TVG-9100000001"
    postal_code: "H3B1A1"
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
    include_unmatched_comment: true
""",
    encoding="utf-8",
  )

  cfg = load_config(cfg_path)
  assert cfg.xtream.server_url == "http://provider.example:8080"
  assert cfg.xtream.stream_format == "m3u8"
  assert cfg.xtream.cache_ttl_hours == 12.0
  assert cfg.xtream.cache_dir == tmp_path / "custom_cache"

  assert cfg.lineup.backend == "tvguide"
  assert cfg.lineup.lineup_id == "TVG-9100000001"
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
  assert cfg.output.report_path == tmp_path / "custom_report.json"
  assert cfg.output.xtream_dir == tmp_path / "custom_xtream"

  assert cfg.serve.web_host == "0.0.0.0"
  assert cfg.serve.web_port == 8181
  assert cfg.serve.xtream_host == "0.0.0.0"
  assert cfg.serve.xtream_port == 8181
  assert cfg.serve.web.trust_proxy_headers is False
  assert cfg.serve.web.trusted_proxy_ips == ["127.0.0.1", "::1"]
  assert cfg.serve.xtream.public_host == "tv.home.arpa"
  assert cfg.serve.xtream.server_protocol == "https"

  assert len(cfg.accounts) == 2
  admin, family = cfg.accounts
  assert admin.account_id == "admin"
  assert admin.email == "admin@example.com"
  assert admin.web_password_hash.startswith("scrypt$")
  assert verify_web_password(
    "plain_web_secret_password", admin.web_password_hash
  )
  assert len(admin.xtream_username) == STREAM_CREDENTIAL_LENGTH
  assert len(admin.xtream_password) == STREAM_CREDENTIAL_LENGTH
  assert all(c in STREAM_CREDENTIAL_ALPHABET for c in admin.xtream_username)
  assert all(c in STREAM_CREDENTIAL_ALPHABET for c in admin.xtream_password)

  # Legacy custom Xtream credentials preserved verbatim
  assert family.xtream_username == "LegacyFamily_Player"
  assert family.xtream_password == "LegacyPassword!123"
  assert cfg.pipeline_account == family
  assert cfg.pipeline_accounts == (family,)

  # On-disk credentials.yaml replaced plaintext web_password with scrypt hash &
  # preserved comments.
  assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600
  saved_creds_text = creds_path.read_text(encoding="utf-8")
  assert "# Top-level credential comment should survive" in saved_creds_text
  assert "# Keep per-account comments intact" in saved_creds_text
  assert "plain_web_secret_password" not in saved_creds_text
  assert "family_web_secret_password" not in saved_creds_text
  assert not re.search(r"^\s*web_password:", saved_creds_text, flags=re.M)

  # ConfigManager redaction hides secrets
  redacted = cfg.to_dict(redact_secrets=True)
  assert redacted["accounts"]["admin"]["web_password_hash"] == "***"
  assert redacted["accounts"]["admin"]["xtream_password"] == "***"
  assert redacted["accounts"]["admin"]["upstream_password"] == "***"

  bad_proxy_cfg = tmp_path / "bad_proxy.yaml"
  bad_proxy_cfg.write_text(
    f'credentials_file: "{creds_path}"\nweb_server:\n  trust_proxy_headers: true\n  trusted_proxy_ips: ["not-an-ip"]\n',
    encoding="utf-8",
  )
  with pytest.raises(ValueError, match="trusted_proxy_ips"):
    load_config(bad_proxy_cfg)


def test_account_collision_checks_fail_fast_without_mutating_credentials(
  tmp_path: Path,
) -> None:
  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    'credentials_file: "credentials.yaml"\n', encoding="utf-8"
  )
  creds_path = tmp_path / "credentials.yaml"

  for case_name, bad_creds_yaml, err_pattern in (
    (
      "email",
      """\
accounts:
  one:
    email: "shared@example.com"
    web_password: "secret_one_password"
    xtream_username: "user0001"
    xtream_password: "pass0001"
    upstream_username: "up_one"
    upstream_password: "up_p1"
  two:
    email: "SHARED@example.com"
    web_password: "secret_two_password"
    xtream_username: "user0002"
    xtream_password: "pass0002"
    upstream_username: "up_two"
    upstream_password: "up_p2"
""",
      "email collision",
    ),
    (
      "xtream_username",
      """\
accounts:
  one:
    email: "one@example.com"
    web_password: "secret_one_password"
    xtream_username: "same_xt"
    xtream_password: "pass0001"
    upstream_username: "up_one"
    upstream_password: "up_p1"
  two:
    email: "two@example.com"
    web_password: "secret_two_password"
    xtream_username: "same_xt"
    xtream_password: "pass0002"
    upstream_username: "up_two"
    upstream_password: "up_p2"
""",
      "xtream_username",
    ),
    (
      "upstream_username",
      """\
accounts:
  one:
    email: "one@example.com"
    web_password: "secret_one_password"
    xtream_username: "user0001"
    xtream_password: "pass0001"
    upstream_username: "shared_up"
    upstream_password: "up_p1"
  two:
    email: "two@example.com"
    web_password: "secret_two_password"
    xtream_username: "user0002"
    xtream_password: "pass0002"
    upstream_username: "shared_up"
    upstream_password: "up_p2"
""",
      "upstream_username",
    ),
  ):
    creds_path.write_text(bad_creds_yaml, encoding="utf-8")
    with pytest.raises(ValueError, match=err_pattern):
      load_config(cfg_path)
    # Fail-fast validation must NOT mutate credentials.yaml before erroring
    assert creds_path.read_text(encoding="utf-8") == bad_creds_yaml, case_name

  # Default pipeline selection falls back to first account if none marked
  creds_path.write_text(
    f"""\
accounts:
  first:
    email: "first@example.com"
    web_password_hash: "{TEST_PASSWORD_HASH}"
    xtream_username: "firstxt1"
    xtream_password: "firstpw1"
    upstream_username: "up_first"
    upstream_password: "up_p1"
  second:
    email: "second@example.com"
    web_password_hash: "{TEST_PASSWORD_HASH}"
    xtream_username: "secndxt2"
    xtream_password: "secndpw2"
    upstream_username: "up_second"
    upstream_password: "up_p2"
""",
    encoding="utf-8",
  )
  default_pipe_cfg = load_config(cfg_path)
  assert default_pipe_cfg.pipeline_account is not None
  assert default_pipe_cfg.pipeline_account.account_id == "first"


def test_missing_config_or_credentials_raises_file_not_found(
  tmp_path: Path,
) -> None:
  with pytest.raises(FileNotFoundError):
    load_config(tmp_path / "missing_config.yaml")

  cfg_only = tmp_path / "config.yaml"
  cfg_only.write_text(
    'credentials_file: "missing_creds.yaml"\n', encoding="utf-8"
  )
  with pytest.raises(FileNotFoundError):
    load_config(cfg_only)


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
    accounts=(
      make_test_account(upstream_username="u", upstream_password="secret"),
    ),
    xtream=XtreamConfig(server_url="http://epg.example"),
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


def test_config_manager_caching_and_self_service_account_mutations(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """Verify ConfigManager mtime cache, password updates, & cred rotation."""
  from bellhop.config.load import _load_yaml_dict

  assert DEFAULT_SD_BASE_URL.startswith("https://")
  assert DEFAULT_TVGUIDE_BASE_URL.startswith("https://")
  assert DEFAULT_IPTV_ORG_API_URL.startswith("https://")
  assert not hasattr(SchedulesDirectConfig(), "base_url")
  assert not hasattr(IptvOrgConfig(), "base_url")
  assert not hasattr(LineupConfig(), "tvguide")

  creds_path = tmp_path / "credentials.yaml"
  creds_path.write_text(
    f"""\
# Preserve account comments across password changes & rotations
accounts:
  alice:
    email: "alice@example.com"
    web_password_hash: "{TEST_PASSWORD_HASH}"
    xtream_username: "alicex01"
    xtream_password: "alicep01"
    upstream_username: "up_alice"
    upstream_password: "up_p1"
  bob:
    email: "bob@example.com"
    web_password_hash: "{TEST_PASSWORD_HASH}"
    xtream_username: "bobxt002"
    xtream_password: "bobpw002"
    upstream_username: "up_bob"
    upstream_password: "up_p2"
""",
    encoding="utf-8",
  )

  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    """\
credentials_file: "credentials.yaml"
upstream_xtream:
  server_url: "http://panel.example:8080"
pipeline:
  lineup:
    backend: "schedules_direct"
    refresh_days: 30
""",
    encoding="utf-8",
  )

  parse_calls = {"n": 0}
  orig_loader = _load_yaml_dict

  def _counting_loader(path: Path) -> dict[str, object]:
    parse_calls["n"] += 1
    return orig_loader(path)

  monkeypatch.setattr(
    "bellhop.config.manager._load_yaml_dict", _counting_loader
  )
  clear_config_cache()
  mgr = ConfigManager(cfg_path)

  first_cfg = mgr.get()
  second_cfg = mgr.get()
  assert first_cfg is second_cfg
  assert parse_calls["n"] == 1

  # Modifying config.yaml invalidates cache once.
  cfg_path.write_text(
    cfg_path.read_text(encoding="utf-8").replace(
      "refresh_days: 30", "refresh_days: 14"
    )
    + "# cache bump\n",
    encoding="utf-8",
  )
  reloaded_cfg = mgr.get()
  assert reloaded_cfg.lineup.refresh_days == 14.0
  assert parse_calls["n"] == 2

  # Password change checks current password & length cleanly.
  with pytest.raises(PermissionError):
    mgr.change_account_web_password(
      "alice", "wrong_current_password", "brand_new_pass_1"
    )
  with pytest.raises(ValueError):
    mgr.change_account_web_password("alice", "test_web_password", "short")
  with pytest.raises(KeyError):
    mgr.change_account_web_password(
      "unknown", "test_web_password", "brand_new_pass_1"
    )

  updated_pw_acct = mgr.change_account_web_password(
    "alice", "test_web_password", "brand_new_pass_1"
  )
  assert verify_web_password(
    "brand_new_pass_1", updated_pw_acct.web_password_hash
  )
  assert not verify_web_password(
    "test_web_password", updated_pw_acct.web_password_hash
  )

  # Regenerate rotates BOTH xtream_username and xtream_password to
  # 8-char [a-z0-9].
  rotated = mgr.regenerate_account_stream_credentials("alice")
  assert rotated.xtream_username != "alicex01"
  assert rotated.xtream_password != "alicep01"
  assert rotated.xtream_username != "bobxt002"
  assert len(rotated.xtream_username) == STREAM_CREDENTIAL_LENGTH
  assert len(rotated.xtream_password) == STREAM_CREDENTIAL_LENGTH
  assert all(c in STREAM_CREDENTIAL_ALPHABET for c in rotated.xtream_username)
  assert all(c in STREAM_CREDENTIAL_ALPHABET for c in rotated.xtream_password)

  # Reloaded disk file retains comments and updated credentials/hashes.
  creds_disk = creds_path.read_text(encoding="utf-8")
  assert "# Preserve account comments across password changes" in creds_disk
  reloaded_after = load_config(cfg_path, persist_credential_updates=False)
  assert reloaded_after.account_by_id("alice") == rotated
  assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600


def test_docker_template_and_numbering_mode(tmp_path: Path) -> None:
  """Verify `docker/` templates load cleanly & preserve `numbering_mode`."""
  repo_root = Path(__file__).resolve().parents[1]
  docker_cfg_src = (repo_root / "docker" / "config.docker.yaml").read_text(
    encoding="utf-8"
  )
  docker_creds_src = (
    repo_root / "docker" / "credentials.docker.yaml"
  ).read_text(encoding="utf-8")

  cfg_copy = tmp_path / "config.yaml"
  creds_copy = tmp_path / "credentials.yaml"
  creds_copy.write_text(docker_creds_src, encoding="utf-8")
  cfg_copy.write_text(
    docker_cfg_src.replace(
      'credentials_file: "/config/credentials.yaml"',
      f'credentials_file: "{creds_copy}"',
    ),
    encoding="utf-8",
  )
  docker_cfg = load_config(cfg_copy)
  assert docker_cfg.xtream.stream_format == "ts"
  assert len(docker_cfg.accounts) == 1
  assert docker_cfg.accounts[0].email == "admin@example.com"

  cfg_path = tmp_path / "num_mode.yaml"
  cfg_path.write_text(
    f"""\
credentials_file: "{creds_copy}"
pipeline:
  lineup:
    collapse_sd_dupes: true
    numbering_mode: "both"
""",
    encoding="utf-8",
  )
  parsed = load_config(cfg_path)
  assert parsed.lineup.collapse_sd_dupes is True
  assert parsed.lineup.numbering_mode == "both"
