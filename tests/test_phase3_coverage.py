"""Comprehensive unit and failure-path coverage tests for Phase 3."""

from __future__ import annotations

import asyncio
import io
import json
import os
import runpy
import stat
from collections.abc import Mapping
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from rich.console import Console

from bellhop import cli
from bellhop.common.accounts import hash_web_password
from bellhop.common.api_contracts import EpgStatusDict
from bellhop.common.file_utils import (
  chmod_private_file,
  ensure_private_dir,
  write_private_bytes,
)
from bellhop.common.http_utils import (
  WEB_CSP_POLICY,
  LoginRateLimiter,
  _parse_ip_addr,
  build_access_urls,
  constant_time_credential_match,
  parse_validated_host_header,
  resolve_bind_host,
  start_site,
)
from bellhop.config import (
  AppConfig,
  ConfigManager,
  LineupConfig,
  StateConfig,
  ValidationConfig,
  XtreamConfig,
  clear_config_cache,
  get_cached_config,
  get_config_manager,
  load_config,
)
from bellhop.config.load import _load_yaml_dict, _opt_str, find_project_root
from bellhop.epg.db import EpgDatabase
from bellhop.epg.models import ProgramItem
from bellhop.epg.refresh import EpgRefreshConfig, EpgRefreshService
from bellhop.epg.sd_client import (
  SchedulesDirectApiError,
  SchedulesDirectSession,
  normalize_sd_postal,
)
from bellhop.epg.sd_epg import (
  SchedulesDirectEpgClient,
  SchedulesDirectEpgConfig,
)
from bellhop.epg.xmltv import EpgVerificationResult
from bellhop.pipeline.catalog_fetcher import (
  CatalogDiff,
  fetch_daily_iptv_catalog,
)
from bellhop.pipeline.lineup_loader import CableLineup
from bellhop.pipeline.models import (
  LineupChannel,
  MappedChannel,
  MatchCandidate,
  ProbeResult,
  XtreamStream,
  _opt_float,
  _opt_int,
)
from bellhop.pipeline.providers.base import LineupBackendError
from bellhop.pipeline.providers.schedules_direct import (
  SchedulesDirectLineupProvider,
)
from bellhop.pipeline.providers.tvguide import (
  TvGuideLineupProvider,
  _parse_channel_number,
)
from bellhop.pipeline.providers.yaml_provider import YamlLineupProvider
from bellhop.pipeline.validator import (
  FFMPEG_PROTOCOL_WHITELIST,
  StreamValidator,
  _extract_first_hls_uri,
  _looks_like_error_payload,
  check_audio_language_compatibility,
  clone_probe_with_language_mismatch,
  has_mpegts_sync,
  normalize_audio_language_code,
  parse_blackdetect_duration,
  strip_slot_language_mismatch,
)
from bellhop.pipeline.xtream_exporter import XtreamExportResult
from bellhop.web.auth import WebAuthConfig, WebAuthManager, sanitize_next_url
from bellhop.web.server import WebGuideApp
from bellhop.web.store import LineupGuideStore
from bellhop.xtream.client import (
  XtreamError,
  _negotiate_xtream_handshake,
  fetch_xtream_account_info,
  fetch_xtream_catalog,
  load_catalog_from_data,
  load_catalog_from_json,
)
from bellhop.xtream.client import (
  _opt_int as _xt_opt_int,
)
from bellhop.xtream.server import XtreamExportStore, XtreamPassthroughServer
from tests.conftest import make_test_account, write_minimal_xtream_export
from tests.fake_xtream_server import make_valid_mpegts_chunk
from tests.http_mock import MockHttpServer, new_test_session


def test_python_module_entrypoint_and_cli_error_branches(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  """Verify `python -m bellhop` entrypoint and CLI error/formatting helpers."""
  with monkeypatch.context() as mp:
    mp.setattr("bellhop.cli.main", lambda: 0)
    with pytest.raises(SystemExit) as exc_info:
      runpy.run_module("bellhop", run_name="__main__")
    assert exc_info.value.code == 0

  import bellhop.__main__  # noqa: F401 - exercise non-main import branch

  buf = io.StringIO()
  monkeypatch.setattr(
    cli, "console", Console(file=buf, force_terminal=False, width=120)
  )

  missing_cfg = tmp_path / "does_not_exist.yaml"
  assert cli.main(["run", "--config", str(missing_cfg), "--quiet"]) == 2
  assert (
    cli.main(
      [
        "run",
        "--config",
        str(tmp_path / "none.yaml"),
        "--phase",
        "invalid_phase_name",
      ]
    )
    == 2
  )

  def _raise_kb(coro: object) -> int:
    close_fn = getattr(coro, "close", None)
    if callable(close_fn):
      close_fn()
    raise KeyboardInterrupt

  monkeypatch.setattr("bellhop.cli.asyncio.run", _raise_kb)
  assert cli.main(["run", "--dry-run"]) == 130

  ch1 = LineupChannel(
    chno=1108, sd_chno=108, name="RDS", group="Sports", tvg_id="RDS.ca"
  )
  ch2 = LineupChannel(
    chno=1200, sd_chno=None, name="City", group="Local", tvg_id="City.ca"
  )
  lu = CableLineup(
    lineup_id="TEST",
    provider="Test Cable",
    market="MTL",
    postal_prefix="H3B",
    channels=[ch1, ch2],
  )
  c1 = MatchCandidate(stream=XtreamStream(stream_id=1, name="RDS"), score=90.0)
  mappings = [
    MappedChannel(channel=ch1, candidates=[c1], selected=c1),
    MappedChannel(channel=ch2, candidates=[], selected=None),
  ]
  for mode in ("sd", "both"):
    cfg = AppConfig(
      project_root=tmp_path,
      lineup=LineupConfig(collapse_sd_dupes=False, numbering_mode=mode),
    )
    cli._print_coverage_summary(
      lu,
      mappings,
      tmp_path / "report.json",
      cfg,
      dry_run=False,
    )
  out_text = buf.getvalue()
  assert "SD numbering" in out_text
  assert "full SD + HD dial" in out_text

  sync_ok: EpgStatusDict = {
    "enabled": True,
    "configured": True,
    "refreshing": False,
    "state": "ok",
    "last_updated": 100.0,
    "last_attempt": 100.0,
    "last_reason": "manual",
    "last_error": None,
    "program_count": 100,
    "active_program_count": 20,
    "channels_with_epg": 5,
    "max_stop_timestamp": 2000,
    "refresh_hours": 6.0,
  }
  sync_err: EpgStatusDict = {
    **sync_ok,
    "state": "error",
    "last_error": "SD down",
  }

  res_ok = XtreamExportResult(
    bundle_path=tmp_path / "b.json",
    live_streams_path=tmp_path / "s.json",
    live_categories_path=tmp_path / "c.json",
    emitted_streams=2,
    emitted_categories=1,
    channels_with_official_logo=1,
    channels_with_guide_id=1,
    xmltv_url_redacted="http://panel.example/xmltv.php",
    epg_verification=EpgVerificationResult(
      url="http://panel.example/xmltv.php",
      url_redacted="http://panel.example/xmltv.php",
      verified=True,
      status_code=200,
      format_detected="xmltv",
      latency_ms=15.0,
    ),
  )
  cli._print_xtream_panel(res_ok, epg_sync_status=sync_ok)

  res_err = XtreamExportResult(
    bundle_path=tmp_path / "b.json",
    live_streams_path=tmp_path / "s.json",
    live_categories_path=tmp_path / "c.json",
    emitted_streams=2,
    emitted_categories=1,
    channels_with_official_logo=1,
    channels_with_guide_id=0,
    xmltv_url_redacted="",
    epg_verification=EpgVerificationResult(
      url="http://panel.example/xmltv.php",
      verified=False,
      error="timeout password=secret",
    ),
  )
  cli._print_xtream_panel(res_err, epg_sync_status=sync_err)
  assert "Verified" in buf.getvalue()
  assert "Unverified" in buf.getvalue()
  assert "SD down" in buf.getvalue()


def test_file_http_and_config_hardening(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  """Verify symlink guards, CSP, constant-time creds, IPv6 `/64`, & `.env`."""
  real_dir = tmp_path / "real_dir"
  real_dir.mkdir()
  link_dir = tmp_path / "link_dir"
  link_dir.symlink_to(real_dir, target_is_directory=True)
  with pytest.raises(OSError, match="symlink"):
    ensure_private_dir(link_dir)

  real_file = tmp_path / "real_file"
  real_file.write_text("x", encoding="utf-8")
  real_file.chmod(0o644)
  link_file = tmp_path / "link_file"
  link_file.symlink_to(real_file)
  chmod_private_file(link_file)
  assert stat.S_IMODE(real_file.stat().st_mode) == 0o644
  with pytest.raises(OSError, match="symlink"):
    write_private_bytes(link_file, b"abc")

  assert "form-action 'self'" in WEB_CSP_POLICY
  assert constant_time_credential_match("alice", "pw", "alice", "pw") is True
  assert constant_time_credential_match("alice", "pw2", "alice", "pw") is False
  assert constant_time_credential_match("bob", "pw", "alice", "pw") is False
  assert constant_time_credential_match("", "pw", "alice", "pw") is False
  assert constant_time_credential_match("üser", "päss", "üser", "päss") is True

  now_box = [100.0]
  limiter = LoginRateLimiter(
    max_failures=2, window_sec=60.0, clock=lambda: now_box[0]
  )
  limiter.record_failure("2001:db8:abcd:1234::1")
  limiter.record_failure("2001:db8:abcd:1234:dead:beef::9")
  assert limiter.check("2001:db8:abcd:1234:9999::77") > 0
  assert limiter.check("2001:db8:abcd:1235::1") == 0
  limiter.record_failure("not-an-ip")
  limiter.record_failure("not-an-ip")
  assert limiter.check("not-an-ip") > 0
  limiter.record_success("not-an-ip")
  assert limiter.check("not-an-ip") == 0

  assert resolve_bind_host(None) == "0.0.0.0"
  assert resolve_bind_host("auto", ip_detector=lambda: "10.0.0.5") == "10.0.0.5"
  assert resolve_bind_host("127.0.0.1") == "127.0.0.1"
  assert build_access_urls("0.0.0.0", 8080, device_ip="192.168.1.9") == [
    "http://192.168.1.9:8080",
    "http://localhost:8080",
  ]
  assert build_access_urls("127.0.0.1", 8080) == ["http://localhost:8080"]
  assert build_access_urls("2001:db8::1", 8080) == ["http://[2001:db8::1]:8080"]
  assert str(_parse_ip_addr("::ffff:192.0.2.5")) == "192.0.2.5"
  assert _parse_ip_addr("") is None
  assert _parse_ip_addr("bogus") is None

  # Env vars & parent `.env` MUST NOT be interpolated or loaded into
  # config.yaml.
  monkeypatch.setenv("BELLHOP_PARENT_LEAK", "from_env")
  (tmp_path / ".env").write_text(
    "BELLHOP_PARENT_LEAK=from_dotenv\n", encoding="utf-8"
  )
  nested = tmp_path / "nested"
  nested.mkdir()
  nested_creds = nested / "credentials.yaml"
  nested_creds.write_text(
    f"""\
accounts:
  primary:
    email: "primary@example.com"
    web_password_hash: "{hash_web_password("nested_secret")}"
    xtream_username: "xt111111"
    xtream_password: "xt222222"
    upstream_username: "u"
    upstream_password: "p"
    pipeline: true
""",
    encoding="utf-8",
  )
  nested_cfg = nested / "config.yaml"
  nested_cfg.write_text(
    f'credentials_file: "{nested_creds}"\n'
    'upstream_xtream:\n  server_url: "http://panel.example/${BELLHOP_PARENT_LEAK}"\n',
    encoding="utf-8",
  )
  loaded_nested = load_config(nested_cfg)
  assert (
    loaded_nested.xtream.server_url
    == "http://panel.example/${BELLHOP_PARENT_LEAK}"
  )
  assert os.environ.get("BELLHOP_PARENT_LEAK") == "from_env"
  monkeypatch.delenv("BELLHOP_PARENT_LEAK", raising=False)

  assert _opt_str("  hello ") == "hello"
  assert _opt_str(" ") is None
  assert _opt_str(None) is None
  list_yaml = tmp_path / "list.yaml"
  list_yaml.write_text("- a\n- b\n", encoding="utf-8")
  with pytest.raises(ValueError, match="Expected a YAML mapping"):
    _load_yaml_dict(list_yaml)
  assert find_project_root(nested_cfg) == nested

  clear_config_cache()
  init_cfg = AppConfig(project_root=nested, config_file=nested_cfg)
  mgr_init = ConfigManager(initial_config=init_cfg)
  assert mgr_init.target_path == nested_cfg.resolve()
  assert mgr_init.get() is init_cfg
  mgr_no_cfg = ConfigManager(nested_cfg)
  assert mgr_no_cfg.target_path == nested_cfg
  monkeypatch.setenv("BELLHOP_CONFIG", str(nested_cfg))
  assert get_config_manager() is get_config_manager()
  cached = get_cached_config(force_reload=True)
  assert cached.config_file == nested_cfg.resolve()
  clear_config_cache()


@pytest.mark.asyncio
async def test_auth_revocations_grace_window_and_xtream_host_hardening(
  tmp_path: Path,
) -> None:
  """Verify `session_revoked.json`, 30s renewal grace, & Host validation."""
  now_box = [1_764_000_000.0]
  secret_file = tmp_path / "cache" / "session_secret.key"
  rev_file = tmp_path / "cache" / "session_revoked.json"
  acct1 = make_test_account(
    account_id="admin",
    email="admin@example.com",
    web_password_hash=hash_web_password("pw1"),
    xtream_username="xt",
    xtream_password="xp",
  )
  acct2 = make_test_account(
    account_id="admin",
    email="admin@example.com",
    web_password_hash=hash_web_password("pw2"),
    xtream_username="xt",
    xtream_password="xp",
  )
  auth_cfg = WebAuthConfig(
    accounts=(acct1,),
    session_ttl_hours=120.0 / 3600.0,
    secret_file=secret_file,
    revocation_file=rev_file,
  )
  mgr1 = WebAuthManager(auth_cfg, clock=lambda: now_box[0])
  tok, _info = mgr1.create_session_token(acct1, now_ts=int(now_box[0]))
  assert mgr1.verify_session_token(tok) is not None
  mgr1.revoke_token(tok, now_ts=int(now_box[0]))
  assert stat.S_IMODE(rev_file.stat().st_mode) == 0o600

  mgr2 = WebAuthManager(auth_cfg, clock=lambda: now_box[0])
  assert mgr2.verify_session_token(tok) is None

  rev_file.write_text("{corrupt", encoding="utf-8")
  mgr3 = WebAuthManager(auth_cfg, clock=lambda: now_box[0])
  tok2, _ = mgr3.create_session_token(acct1, now_ts=int(now_box[0]))
  assert mgr3.verify_session_token(tok2) is not None

  mgr_pw = WebAuthManager(
    WebAuthConfig(
      accounts=(acct2,),
      secret_file=secret_file,
      revocation_file=rev_file,
    ),
    clock=lambda: now_box[0],
  )
  assert mgr_pw.verify_session_token(tok2) is None
  assert mgr3.verify_session_token("malformed") is None
  assert mgr3.verify_session_token("a.b.c") is None

  xt_dir = write_minimal_xtream_export(tmp_path / "output" / "xtream")
  app = WebGuideApp(
    LineupGuideStore(xt_dir),
    auth=mgr3,
    clock=lambda: now_box[0],
  )
  await app.startup()
  runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
  base = f"http://127.0.0.1:{port}"
  try:
    async with new_test_session(base) as client:
      async with client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "pw1"},
      ) as r_login:
        assert r_login.status == 200
        raw_cookie = (
          r_login.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        )

      # Advance past half-TTL (70s of 120s) -> renewal issues new cookie and
      # grants 30s grace window to previous token.
      now_box[0] += 70.0
      async with client.get(
        "/api/lineup", headers={"Cookie": f"bellhop_session={raw_cookie}"}
      ) as r_renew:
        assert r_renew.status == 200
        assert "bellhop_session=" in r_renew.headers.get("Set-Cookie", "")

      # At +10s after renewal (within 30s grace), replay in a fresh session
      # with no CookieJar still works using the old cookie.
      now_box[0] += 10.0
      async with (
        new_test_session(base) as client_grace,
        client_grace.get(
          "/api/epg/99999",
          headers={"Cookie": f"bellhop_session={raw_cookie}"},
        ) as r_grace,
      ):
        assert r_grace.status == 404

      # Past 30s grace (+25s -> +35s since renewal), old cookie replay is 401.
      now_box[0] += 25.0
      async with (
        new_test_session(base) as client_expired,
        client_expired.get(
          "/api/lineup",
          headers={"Cookie": f"bellhop_session={raw_cookie}"},
        ) as r_expired_grace,
      ):
        assert r_expired_grace.status == 401

      fresh_tok, _ = mgr3.create_session_token(acct1, now_ts=int(now_box[0]))
      async with client.get(
        "/api/hls/99999/index.m3u8",
        headers={"Cookie": f"bellhop_session={fresh_tok}"},
      ) as r_hls_missing:
        assert r_hls_missing.status == 404
  finally:
    await runner.cleanup()
    await app.shutdown()

  for valid_host, expected in (
    ("192.168.1.10:8080", ("192.168.1.10", 8080)),
    ("guide.local", ("guide.local", None)),
    ("[::1]:8080", ("::1", 8080)),
    ("[2001:db8::1]", ("2001:db8::1", None)),
  ):
    assert parse_validated_host_header(valid_host) == expected

  for bad_host in (
    "",
    "evil.com/path",
    "user@host",
    "bad..host",
    "[::1%eth0]",
    "[not-ipv6]",
    "host:99999",
    "host:abc",
    "host\r\nX-Inject: 1",
    "[::1]:99999",
  ):
    assert parse_validated_host_header(bad_host) is None

  xt_srv = XtreamPassthroughServer(
    XtreamExportStore(xt_dir),
    accounts=(acct1,),
    host="127.0.0.1",
    port=0,
  )
  await xt_srv.start()
  try:
    x_url = f"http://127.0.0.1:{xt_srv.bound_port}"
    async with new_test_session(x_url) as xs:
      for _ in range(2):
        async with xs.get(
          "/player_api.php", params={"username": "xt", "password": "wrong"}
        ) as r_bad:
          assert r_bad.status == 200
          bad_payload = await r_bad.json()
          assert bad_payload["user_info"]["auth"] == 0
      assert xt_srv.login_limiter.check("127.0.0.1") == 0
      async with xs.get(
        "/player_api.php", params={"username": "xt", "password": "xp"}
      ) as r_ok:
        assert r_ok.status == 200
      assert len(xt_srv.login_limiter._failures.get("127.0.0.1", [])) == 2
  finally:
    await xt_srv.stop()


@pytest.mark.asyncio
async def test_lineup_providers_and_pipeline_models_edge_cases(
  tmp_path: Path,
) -> None:
  """Verify YAML/TVGuide/SD provider error/discovery paths and models."""
  assert _opt_int(True) is None
  assert _opt_int("bad") is None
  assert _opt_int("42") == 42
  assert _opt_float(True) is None
  assert _opt_float("bad") is None
  assert _opt_float("2.5") == 2.5
  assert (
    ProbeResult(stream_id=1, url="u", ok=True, height=2160).resolution_label
    == "4K"
  )
  assert (
    ProbeResult(stream_id=1, url="u", ok=True, height=1080).resolution_label
    == "1080p"
  )
  assert (
    ProbeResult(stream_id=1, url="u", ok=True, height=720).resolution_label
    == "720p"
  )
  assert (
    ProbeResult(stream_id=1, url="u", ok=True, height=480).resolution_label
    == "480p"
  )
  assert ProbeResult(stream_id=1, url="u", ok=True).resolution_label == ""

  sample_yaml = Path("data/lineups/bell_fibe_montreal.yaml")
  yp = YamlLineupProvider(sample_yaml)
  assert yp.supports("CAN-BELLFIBE-MTL-X", backend_hint="yaml") is True
  assert yp.supports("YAML:custom", backend_hint="auto") is True
  assert yp.supports("ANY", backend_hint="tvguide") is False
  found = await yp.discover_lineups(country="CAN", query="bell")
  assert len(found) == 1
  retrieved_lu = await yp.retrieve_lineup(
    "CAN-BELLFIBE-MTL-X", excluded_channels={2}
  )
  assert retrieved_lu.channels
  assert await yp.discover_lineups(country="CAN", query="nonexistent") == []
  yp_missing = YamlLineupProvider(tmp_path / "missing.yaml")
  assert await yp_missing.discover_lineups() == []
  with pytest.raises(LineupBackendError):
    await yp_missing.retrieve_lineup("X")

  assert _parse_channel_number(None, 5) == 5
  assert _parse_channel_number("  ", 6) == 6
  assert _parse_channel_number("2.1", 1) == 2
  assert _parse_channel_number("17-2", 1) == 1702
  assert _parse_channel_number("ch99", 1) == 99
  assert _parse_channel_number("abc", 7) == 7

  def _tvg_err(req: web.Request) -> web.Response:
    if req.path == "/404":
      return web.Response(status=404)
    if req.path == "/list":
      return web.json_response(["not", "a", "dict"])
    if req.path == "/serviceproviders/zipcode/10001/web":
      return web.json_response(
        {
          "data": {
            "items": [
              {
                "id": 9100000001,
                "name": "Spectrum NYC",
                "city": "New York",
                "state": "NY",
                "type": "cable",
              }
            ]
          }
        }
      )
    if req.path == "/serviceprovider/9100000001/web":
      return web.json_response(
        {
          "data": {
            "item": {
              "id": 9100000001,
              "name": "Spectrum NYC",
              "city": "New York",
              "state": "NY",
            }
          }
        }
      )
    if req.path == "/serviceprovider/9100000001/sources/web":
      return web.json_response(
        {
          "data": {
            "items": [
              {
                "number": "2",
                "name": "CBS 2",
                "fullName": "WCBS-DT HD",
                "networkName": "CBS",
                "logo": "/2/wcbs.png",
              }
            ]
          }
        }
      )
    return web.Response(text="not json")

  async with MockHttpServer(_tvg_err) as mock, mock.new_session() as client:
    tvg = TvGuideLineupProvider(base_url=mock.base_url, client=client)
    assert tvg.supports("TVG-9100000001", backend_hint="tvg") is True
    assert (
      tvg.supports("TVG-9100000001", backend_hint="schedules_direct") is False
    )
    assert tvg.supports("TVG-9100000001", backend_hint="auto") is True
    for bad_path in ("/404", "/list", "/bad"):
      with pytest.raises(LineupBackendError):
        await tvg._get_json(bad_path)
    us_heads = await tvg.discover_lineups(country="US", postal_code="10001")
    assert len(us_heads) == 1
    tvg_lu = await tvg.retrieve_lineup(
      "TVG-9100000001", excluded_channels=set()
    )
    assert len(tvg_lu.channels) == 1

  sd_unconf = SchedulesDirectLineupProvider(
    token_cache_path=tmp_path / "t.json"
  )
  with pytest.raises(LineupBackendError, match="credentials"):
    await sd_unconf.discover_lineups(postal_code="H3B")

  subscribed = {"added": False}

  def _sd_handler(req: web.Request) -> web.Response:
    if req.path == "/token":
      return web.json_response({"code": 0, "token": "tok"})
    if req.path == "/lineups/CAN-0001-X" and req.method == "GET":
      if not subscribed["added"]:
        return web.json_response(
          {"code": 2100, "message": "Lineup not in account"}, status=400
        )
      return web.json_response(
        {
          "metadata": {"lineup": "CAN-0001-X", "transport": "Cable"},
          "map": [{"stationID": "10101", "channel": "1101"}],
          "stations": [
            {
              "stationID": "10101",
              "name": "CBFT HD",
              "callsign": "CBFTHD",
              "broadcastLanguage": ["fr"],
              "isHD": True,
            }
          ],
        }
      )
    if req.path == "/lineups/CAN-0001-X" and req.method == "PUT":
      subscribed["added"] = True
      return web.json_response({"code": 0})
    return web.json_response(
      {
        "lineups": [
          {
            "lineup": "CAN-0001-X",
            "name": "Bell Fibe TV",
            "location": "Montréal",
          }
        ]
      }
    )

  async with MockHttpServer(_sd_handler) as mock, mock.new_session() as client:
    sd = SchedulesDirectLineupProvider(
      username="u",
      password="p",
      base_url=mock.base_url,
      token_cache_path=tmp_path / "sd.json",
      auto_add_lineup=True,
      client=client,
    )
    with pytest.raises(
      LineupBackendError, match="pipeline.lineup.postal_code in config.yaml"
    ):
      await sd.discover_lineups(postal_code="")
    with pytest.raises(LineupBackendError, match="Invalid Schedules Direct"):
      await sd.retrieve_lineup("bad_id")
    sd_lu = await sd.retrieve_lineup("CAN-0001-X")
    assert sd_lu.provider == "Bell Fibe TV"
    assert sd_lu.market == "Montréal (Cable)"
    assert subscribed["added"] is True


@pytest.mark.asyncio
async def test_xtream_client_catalog_fetcher_and_validator_whitelist(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  """Verify single-parse catalog handoff, `persist_cache=False`, & validator."""
  cat_list = load_catalog_from_data(
    [{"stream_id": 10, "name": "CH 10", "category_id": "1"}]
  )
  assert len(cat_list.streams) == 1
  cat_dict = load_catalog_from_data(
    {
      "account": {"username": "u", "max_connections": 2},
      "live_categories": {
        "live": [{"category_id": "1", "category_name": "Sports"}]
      },
      "live_streams": [{"stream_id": 10, "name": "RDS"}],
    }
  )
  assert cat_dict.categories == {"1": "Sports"}
  with pytest.raises(XtreamError):
    load_catalog_from_data(12345)

  def _xt_handler(req: web.Request) -> web.Response:
    act = req.query.get("action", "")
    if act == "get_live_categories":
      return web.json_response([{"category_id": "1", "category_name": "News"}])
    if act == "get_live_streams":
      return web.json_response(
        [{"stream_id": 1, "name": "RDI", "category_id": "1"}]
      )
    return web.json_response(
      {"user_info": {"auth": 1, "status": "Active"}, "server_info": {}}
    )

  async with MockHttpServer(_xt_handler) as mock:
    cache_dir = tmp_path / "xt_cache"
    state_dir = tmp_path / "state"
    cfg = AppConfig(
      project_root=tmp_path,
      accounts=(
        make_test_account(upstream_username="u", upstream_password="p"),
      ),
      xtream=XtreamConfig(
        server_url=mock.base_url,
        cache_dir=cache_dir,
      ),
      state=StateConfig(state_dir=state_dir),
    )
    cat = await fetch_xtream_catalog(cfg, persist_cache=False)
    assert len(cat.streams) == 1
    assert not cache_dir.exists()

    res = await fetch_daily_iptv_catalog(cfg)
    res.snapshot_path.write_text("{corrupt", encoding="utf-8")
    res2 = await fetch_daily_iptv_catalog(cfg, force_refresh=False)
    assert len(res2.catalog.streams) == 1
    assert CatalogDiff.from_dict(None).has_changes is False

  def _bad_auth(req: web.Request) -> web.Response:
    _ = req
    return web.json_response({"user_info": {"auth": 0}})

  async with MockHttpServer(_bad_auth) as mock, mock.new_session() as sess:
    xt_cfg = XtreamConfig(server_url=mock.base_url)
    with pytest.raises(XtreamError, match="authentication failed"):
      await _negotiate_xtream_handshake(
        sess,
        [mock.base_url],
        {"username": "u", "password": "p"},
        xt_cfg,
      )

  assert FFMPEG_PROTOCOL_WHITELIST == "http,https,tcp,tls,crypto,hls"
  assert normalize_audio_language_code("und") is None
  assert normalize_audio_language_code("fre") == "fr"
  assert check_audio_language_compatibility("fr", ["fr"]) == (True, True)
  p_raw = ProbeResult(
    stream_id=1, url="http://x/1.ts", ok=True, audio_languages=["es"]
  )
  p_demoted = clone_probe_with_language_mismatch(p_raw, "fr")
  assert p_demoted.audio_language_mismatch is True
  recovered = strip_slot_language_mismatch(p_demoted)
  assert recovered.ok is True and recovered.audio_language_mismatch is False
  assert parse_blackdetect_duration(
    "black_duration:1.25 black_duration:0.5"
  ) == pytest.approx(1.75)
  assert has_mpegts_sync(b"\x00" * 10) is False
  assert has_mpegts_sync(make_valid_mpegts_chunk(5)) is True

  captured_cmds: list[list[str]] = []

  class _FakeProc:
    def __init__(self, stdout_b: bytes, stderr_b: bytes, rc: int = 0) -> None:
      self._stdout = stdout_b
      self._stderr = stderr_b
      self.returncode = rc

    async def communicate(self) -> tuple[bytes, bytes]:
      return self._stdout, self._stderr

    def kill(self) -> None:
      return None

    async def wait(self) -> int:
      return self.returncode

  async def _fake_exec(*cmd: str, **_kw: object) -> _FakeProc:
    captured_cmds.append(list(cmd))
    if "ffprobe" in cmd[0]:
      return _FakeProc(
        json.dumps(
          {
            "streams": [
              {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
              },
              {
                "codec_type": "audio",
                "codec_name": "aac",
                "tags": {"language": "eng"},
              },
            ]
          }
        ).encode(),
        b"",
      )
    return _FakeProc(b"", b"black_duration:0.1")

  monkeypatch.setattr(
    "bellhop.pipeline.validator.shutil.which",
    lambda tool: f"/usr/bin/{tool}" if tool in {"ffprobe", "ffmpeg"} else None,
  )
  monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

  ts_bytes = make_valid_mpegts_chunk(6)

  def _val_handler(req: web.Request) -> web.Response:
    if req.path.endswith("/3.ts"):
      return web.Response(status=404)
    if req.path.endswith("/4.ts"):
      return web.Response(status=302, headers={"Location": "/black.ts"})
    if req.path == "/black.ts":
      return web.Response(status=200, body=ts_bytes, content_type="video/mp2t")
    if req.path.endswith("/5.ts"):
      return web.Response(
        status=200, body=b"<html>error</html>", content_type="text/html"
      )
    if req.path.endswith("/2.ts"):
      return web.Response(status=302, headers={"Location": "/master.m3u8"})
    if req.path == "/master.m3u8":
      return web.Response(
        status=200,
        body=b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n/media.m3u8\n",
        content_type="application/vnd.apple.mpegurl",
      )
    if req.path == "/media.m3u8":
      return web.Response(
        status=200,
        body=b"#EXTM3U\n#EXTINF:4.0,\n/seg0.ts\n",
        content_type="application/vnd.apple.mpegurl",
      )
    return web.Response(status=200, body=ts_bytes, content_type="video/mp2t")

  async with MockHttpServer(_val_handler) as mock, mock.new_session() as sess:
    val_cfg = AppConfig(
      project_root=tmp_path,
      accounts=(
        make_test_account(upstream_username="u", upstream_password="p"),
      ),
      xtream=XtreamConfig(server_url=mock.base_url),
      validation=ValidationConfig(
        use_ffprobe=True,
        detect_black_screen=True,
        min_bytes_to_read=564,
        inter_probe_delay_sec=0.0,
      ),
    )
    validator = StreamValidator(val_cfg)

    p_ok = await validator._execute_two_stage_probe(
      XtreamStream(stream_id=1, name="OK"), sess
    )
    assert p_ok.ok is True
    assert len(captured_cmds) == 2
    ffprobe_cmd, ffmpeg_cmd = captured_cmds[0], captured_cmds[1]
    for argv in (ffprobe_cmd, ffmpeg_cmd):
      wl_idx = argv.index("-protocol_whitelist")
      assert argv[wl_idx + 1] == FFMPEG_PROTOCOL_WHITELIST
    assert ffprobe_cmd[-2:] == ["-i", f"{mock.base_url}/live/u/p/1.ts"]

    p_hls = await validator._execute_two_stage_probe(
      XtreamStream(stream_id=2, name="HLS"), sess
    )
    assert p_hls.ok is True and p_hls.is_hls is True

    p_404 = await validator._execute_two_stage_probe(
      XtreamStream(stream_id=3, name="404"), sess
    )
    assert p_404.ok is False and p_404.http_status == 404

    p_black_redir = await validator._execute_two_stage_probe(
      XtreamStream(stream_id=4, name="BLACK"), sess
    )
    assert p_black_redir.ok is False

    p_html = await validator._execute_two_stage_probe(
      XtreamStream(stream_id=5, name="HTML"), sess
    )
    assert p_html.ok is False

    # Exercise validate_mappings deduplication + black screen + ffprobe errors.
    validator._probe_cache.clear()
    ch_shared_a = LineupChannel(chno=1, name="One", group="G")
    ch_shared_b = LineupChannel(chno=2, name="Two", group="G")
    cand_ok = MatchCandidate(
      stream=XtreamStream(stream_id=1, name="OK"), score=100.0
    )
    mapped_pair = [
      MappedChannel(channel=ch_shared_a, candidates=[cand_ok]),
      MappedChannel(channel=ch_shared_b, candidates=[cand_ok]),
    ]
    await validator.validate_mappings(mapped_pair)
    assert mapped_pair[0].selected is not None
    assert mapped_pair[1].selected is not None

    # Test pure-black rejection via ffmpeg blackdetect.
    async def _black_exec(*cmd: str, **_kw: object) -> _FakeProc:
      if "ffprobe" in cmd[0]:
        return _FakeProc(
          json.dumps(
            {
              "streams": [
                {
                  "codec_type": "video",
                  "codec_name": "h264",
                  "width": 1280,
                  "height": 720,
                }
              ]
            }
          ).encode(),
          b"",
        )
      return _FakeProc(b"", b"black_duration:1.45")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _black_exec)
    p_black_vid = await validator._execute_two_stage_probe(
      XtreamStream(stream_id=1, name="BLK"), sess
    )
    assert p_black_vid.ok is False and p_black_vid.black_screen is True

    # Test ffprobe non-zero exit + bad JSON
    async def _bad_json_exec(*cmd: str, **_kw: object) -> _FakeProc:
      _ = cmd
      return _FakeProc(b"{bad", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _bad_json_exec)
    p_bad_json = ProbeResult(
      stream_id=9, url=f"{mock.base_url}/live/u/p/1.ts", ok=True
    )
    await validator._stage2_ffprobe_check(p_bad_json)
    assert p_bad_json.ok is False and "invalid JSON" in (p_bad_json.error or "")

    async def _fail_rc_exec(*cmd: str, **_kw: object) -> _FakeProc:
      _ = cmd
      return _FakeProc(b"", b"boom password=p", rc=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail_rc_exec)
    p_rc = ProbeResult(
      stream_id=10, url=f"{mock.base_url}/live/u/p/1.ts", ok=True
    )
    await validator._stage2_ffprobe_check(p_rc)
    assert p_rc.ok is False and "p" not in (p_rc.error or "").replace(
      "ffprobe", ""
    ).replace("password=***", "")


@pytest.mark.asyncio
async def test_sd_client_and_epg_refresh_failure_branches(
  tmp_path: Path,
) -> None:
  """Verify SD corrupt token recovery, code 4007/4009 retry, & EpgRefresh."""
  token_calls = 0
  status_calls = 0

  def _sd_auth_handler(req: web.Request) -> web.Response:
    nonlocal token_calls, status_calls
    if req.path == "/token":
      token_calls += 1
      return web.json_response({"code": 0, "token": f"tok-{token_calls}"})
    if req.path == "/status":
      status_calls += 1
      if status_calls == 1:
        return web.json_response({"code": 4007, "message": "Token invalid"})
      if status_calls == 3:
        return web.json_response({"code": 4009, "message": "Token expired"})
      return web.json_response({"code": 0, "lineups": []})
    if req.path == "/fail":
      return web.Response(status=500, text="server error")
    return web.Response(status=404)

  tok_path = tmp_path / "cache" / "sd_token.json"
  ensure_private_dir(tok_path.parent)
  tok_path.write_text("{bad-json", encoding="utf-8")

  async with (
    MockHttpServer(_sd_auth_handler) as mock,
    mock.new_session() as sess,
  ):
    sd_sess = SchedulesDirectSession(
      username="u",
      password="p",
      base_url=mock.base_url,
      token_cache_path=tok_path,
    )
    status_res = await sd_sess.request_json(sess, "GET", "status")
    assert status_res["code"] == 0
    status_res2 = await sd_sess.request_json(sess, "GET", "status")
    assert status_res2["code"] == 0
    assert token_calls >= 3
    with pytest.raises(SchedulesDirectApiError):
      await sd_sess.request_json(sess, "GET", "fail")

  db = EpgDatabase(tmp_path / "cache" / "epg.sqlite3")
  xt_dir = write_minimal_xtream_export(tmp_path / "output" / "xtream")
  store = LineupGuideStore(xt_dir)
  try:
    sd_disabled = SchedulesDirectEpgClient(SchedulesDirectEpgConfig(), db=db)
    assert sd_disabled.enabled is False

    unconf = EpgRefreshService(
      store,
      EpgRefreshConfig(
        enabled=True,
        xmltv_url="",
        cache_file=tmp_path / "cache" / "epg.sqlite3",
      ),
    )
    try:
      unconf_status = await unconf.refresh_now(force=True, reason="test")
      assert unconf_status["state"] == "error"

      snap = store.get_snapshot()
      db.replace_guide_schedules(
        snap=snap,
        programs_by_id={
          "ICIRDI.ca": [
            ProgramItem(
              id="ext-1",
              channel_id="ICIRDI.ca",
              title="External Show",
              start_timestamp=1_764_000_000,
              stop_timestamp=1_764_003_600,
            )
          ]
        },
        fetched_at=1_764_000_000.0,
      )
      unconf.sync_external_if_needed(1_764_000_500)
      assert unconf.status_dict(now_ts=1_764_000_500)["program_count"] == 1
    finally:
      await unconf.stop()
  finally:
    db.close()


@pytest.mark.asyncio
async def test_remaining_validator_client_auth_and_postal_branches(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  """Cover postal normalization, SD discovery, auth helpers, cache, & probes."""
  # 1. `normalize_sd_postal` & SD discovery/slot parsing edge cases
  assert normalize_sd_postal("h3b 2y5") == ("H3B2Y5", "CAN")
  assert normalize_sd_postal("H3B") == ("H3B1A1", "CAN")
  assert normalize_sd_postal("10001-9999") == ("10001", "USA")
  assert normalize_sd_postal("  ") == ("", "USA")
  assert normalize_sd_postal("SW1A", explicit_country="UK") == ("SW1A", "GBR")

  def _sd_disc_handler(req: web.Request) -> web.Response:
    if req.path == "/token":
      return web.json_response({"code": 0, "token": "tok"})
    if req.path == "/headends":
      return web.json_response(
        [
          {
            "headend": "HE1",
            "transport": "Cable",
            "location": "Montréal",
            "lineups": [
              {"lineup": "CAN-0001-X", "name": "Bell Fibe TV"},
              {"lineup": "CAN-0002-X", "name": "Other Cable"},
            ],
          }
        ]
      )
    if req.path == "/lineups/CAN-0001-X":
      return web.json_response(
        {
          "metadata": {"lineup": "Bell Fibe TV", "location": "MTL"},
          "map": [{"stationID": "101", "channel": "2.1"}, {"stationID": "999"}],
          "stations": [
            {"stationID": "101", "name": "CBFT", "callsign": "CBFT"}
          ],
        }
      )
    return web.json_response({"code": 2100}, status=400)

  async with (
    MockHttpServer(_sd_disc_handler) as mock,
    mock.new_session() as sess,
  ):
    sd_p = SchedulesDirectLineupProvider(
      username="u",
      password="p",
      base_url=mock.base_url,
      token_cache_path=tmp_path / "sd_p.json",
      client=sess,
    )
    heads = await sd_p.discover_lineups(
      country="CAN", postal_code="H3B", query="bell"
    )
    assert len(heads) == 1 and heads[0].lineup_id == "CAN-0001-X"
    lu = await sd_p.retrieve_lineup("CAN-0001-X")
    assert any(ch.chno == 2 for ch in lu.channels)

    # `allow_error_codes` allows non-zero code without raising
    sd_sess = SchedulesDirectSession(
      username="u",
      password="p",
      base_url=mock.base_url,
      token_cache_path=tmp_path / "sd_s.json",
    )
    res_allowed = await sd_sess.request_json(
      sess, "GET", "other", allow_error_codes=frozenset({2100})
    )
    assert res_allowed["code"] == 2100

  # 2. `web/auth.py` helpers & legacy revocation shapes
  assert sanitize_next_url("//evil.com/path") == "/"
  assert sanitize_next_url("http://evil.com/") == "/"
  assert sanitize_next_url("/guide?a=1#top") == "/guide?a=1#top"
  rev_file = tmp_path / "rev_legacy.json"
  now_ts = 1_764_000_000
  rev_file.write_text(
    json.dumps(
      {
        "revoked": {
          "old_jti": now_ts - 10,
          "legacy_scalar": now_ts + 500,
          "bad_val": "ignored",
        }
      }
    ),
    encoding="utf-8",
  )
  acct_rev = make_test_account(
    account_id="admin",
    email="admin@example.com",
    web_password_hash=hash_web_password("pw"),
  )
  auth_mgr = WebAuthManager(
    WebAuthConfig(
      accounts=(acct_rev,),
      secret_file=tmp_path / "sec.key",
      revocation_file=rev_file,
    ),
    clock=lambda: float(now_ts),
  )
  t_hdr, _ = auth_mgr.create_session_token(acct_rev, now_ts=now_ts)
  auth_mgr.revoke_session_from_headers(
    {"Cookie": f"bellhop_session={t_hdr}"}, now_ts=now_ts
  )
  assert auth_mgr.verify_session_token(t_hdr) is None
  assert (
    auth_mgr.extract_session_from_headers({"cookie": "other=1"}, now_ts=now_ts)
    is None
  )
  assert "; Secure" in auth_mgr.build_clear_cookie_header(secure=True)

  # 3. `xtream/client.py`: warm/expired disk cache + `load_catalog_from_json`
  assert _xt_opt_int(True) is None
  assert _xt_opt_int("abc") is None
  raw_json_path = tmp_path / "seed.json"
  raw_json_path.write_text(
    json.dumps([{"stream_id": 5, "name": "Seed CH", "category_id": "1"}]),
    encoding="utf-8",
  )
  seed_cat = load_catalog_from_json(raw_json_path)
  assert len(seed_cat.streams) == 1

  api_calls = {"n": 0}

  def _xt_cache_handler(req: web.Request) -> web.Response:
    act = req.query.get("action", "")
    if act == "get_live_categories":
      return web.json_response([{"category_id": "1", "category_name": "Cat"}])
    if act == "get_live_streams":
      return web.json_response(
        [{"stream_id": 1, "name": "C1", "category_id": "1"}]
      )
    api_calls["n"] += 1
    return web.json_response(
      {"user_info": {"auth": 1, "status": "Active"}, "server_info": {}}
    )

  async with MockHttpServer(_xt_cache_handler) as mock:
    cache_cfg = AppConfig(
      project_root=tmp_path,
      accounts=(
        make_test_account(upstream_username="u", upstream_password="p"),
      ),
      xtream=XtreamConfig(
        server_url=mock.base_url,
        cache_dir=tmp_path / "xc",
        cache_ttl_hours=24.0,
      ),
    )
    c1 = await fetch_xtream_catalog(cache_cfg, persist_cache=True)
    c2 = await fetch_xtream_catalog(cache_cfg, persist_cache=True)
    assert len(c1.streams) == 1 and len(c2.streams) == 1
    assert api_calls["n"] == 1
    acct = await fetch_xtream_account_info(cache_cfg)
    assert acct.status == "Active"

  with pytest.raises(XtreamError):
    await fetch_xtream_account_info(AppConfig(project_root=tmp_path))
  with pytest.raises(XtreamError):
    await fetch_xtream_catalog(AppConfig(project_root=tmp_path))

  mode: list[str] = ["expired_dict"]

  def _xt_err_handler(req: web.Request) -> web.Response:
    act = req.query.get("action", "")
    cur = mode[0]
    if cur == "http500" and act == "get_live_streams":
      return web.Response(status=500, text="boom")
    if act == "get_live_categories":
      return web.json_response(
        ["not-a-dict", {"category_id": "1", "category_name": "Cat"}]
      )
    if act == "get_live_streams":
      if cur in {"expired_dict", "active_dict"}:
        return web.json_response({"error": "bad"})
      if cur == "expired_empty":
        return web.json_response([])
      return web.json_response(
        [{"stream_id": 1, "name": "C1", "category_id": "1"}]
      )
    status_val = "Expired" if cur.startswith("expired") else "Active"
    return web.json_response(
      {"user_info": {"auth": 1, "status": status_val}, "server_info": {}}
    )

  async with MockHttpServer(_xt_err_handler) as err_mock:
    err_cfg = AppConfig(
      project_root=tmp_path,
      accounts=(
        make_test_account(upstream_username="u", upstream_password="p"),
      ),
      xtream=XtreamConfig(
        server_url=err_mock.base_url,
      ),
    )
    for m_name in ("expired_dict", "expired_empty", "active_dict"):
      mode[0] = m_name
      with pytest.raises(XtreamError):
        await fetch_xtream_catalog(
          err_cfg, persist_cache=False, force_refresh=True
        )
    mode[0] = "http500"
    with pytest.raises(XtreamError):
      await fetch_xtream_catalog(
        err_cfg, persist_cache=False, force_refresh=True
      )

  mixed_cat = load_catalog_from_data(
    {
      "account": {"username": "u", "status": "Active", "max_connections": 1},
      "categories": {
        "live": [{"category_id": "1", "category_name": "News"}, "bad-cat"]
      },
      "streams": {
        "a": "bad-stream",
        "b": {"stream_id": 0, "name": "Zero"},
        "c": {"stream_id": 7, "name": "Valid", "category_id": "1"},
      },
    }
  )
  assert len(mixed_cat.streams) == 1 and mixed_cat.streams[0].stream_id == 7
  list_cat = load_catalog_from_data([{"stream_id": 8, "name": "ListItem"}])
  assert len(list_cat.streams) == 1
  with pytest.raises(XtreamError):
    load_catalog_from_data(123)

  # 4. `pipeline/validator.py`: error payloads, missing bins, & timeouts
  assert _looks_like_error_payload(b'{"error": "bad"}', "application/json")
  assert _extract_first_hls_uri(
    "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nsub.m3u8\n"
  ) == ("sub.m3u8", True)
  assert _extract_first_hls_uri("#EXTM3U\n#EXTINF:4.0,\n") == (None, False)

  monkeypatch.setattr(
    "bellhop.pipeline.validator.shutil.which", lambda _t: None
  )
  val_no_bins = StreamValidator(AppConfig(project_root=tmp_path))
  assert val_no_bins.ffprobe_path is None

  monkeypatch.setattr(
    "bellhop.pipeline.validator.shutil.which",
    lambda tool: f"/usr/bin/{tool}" if tool in {"ffprobe", "ffmpeg"} else None,
  )
  val = StreamValidator(
    AppConfig(
      project_root=tmp_path,
      validation=ValidationConfig(use_ffprobe=True, detect_black_screen=True),
    )
  )

  killed = {"count": 0}

  class _TimeoutProc:
    returncode = None

    async def communicate(self) -> tuple[bytes, bytes]:
      raise asyncio.TimeoutError

    def kill(self) -> None:
      killed["count"] += 1

    async def wait(self) -> int:
      return -9

  async def _timeout_exec(*_c: str, **_kw: object) -> _TimeoutProc:
    return _TimeoutProc()

  monkeypatch.setattr(asyncio, "create_subprocess_exec", _timeout_exec)
  p_to = ProbeResult(
    stream_id=1, url="http://panel.example/live/u/p/1.ts", ok=True
  )
  await val._stage2_ffprobe_check(p_to)
  assert p_to.ok is False and "timed out" in (p_to.error or "")
  assert killed["count"] == 1

  p_blk_to = ProbeResult(
    stream_id=2,
    url="http://panel.example/live/u/p/2.ts",
    ok=True,
    video_codec="h264",
  )
  await val._stage3_black_screen_check(p_blk_to)
  assert killed["count"] == 2 and p_blk_to.ok is True

  async def _oserror_exec(*_c: str, **_kw: object) -> _TimeoutProc:
    raise OSError("missing binary")

  monkeypatch.setattr(asyncio, "create_subprocess_exec", _oserror_exec)
  p_os = ProbeResult(
    stream_id=3, url="http://panel.example/live/u/p/3.ts", ok=True
  )
  await val._stage2_ffprobe_check(p_os)
  await val._stage3_black_screen_check(p_os)

  class _AudioOnlyProc:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
      return (
        json.dumps(
          {"streams": [{"codec_type": "audio", "codec_name": "aac"}]}
        ).encode(),
        b"",
      )

  async def _audio_exec(*_c: str, **_kw: object) -> _AudioOnlyProc:
    return _AudioOnlyProc()

  monkeypatch.setattr(asyncio, "create_subprocess_exec", _audio_exec)
  p_aud_rej = ProbeResult(
    stream_id=4, url="http://panel.example/live/u/p/4.ts", ok=True
  )
  await val._stage2_ffprobe_check(p_aud_rej, allow_audio_only=False)
  assert p_aud_rej.ok is False and "no decodable video" in (
    p_aud_rej.error or ""
  )
  p_aud_ok = ProbeResult(
    stream_id=5, url="http://panel.example/live/u/p/5.ts", ok=True
  )
  await val._stage2_ffprobe_check(p_aud_ok, allow_audio_only=True)
  assert p_aud_ok.ok is True


@pytest.mark.asyncio
async def test_sd_epg_full_discovery_and_web_server_extra_routes(
  tmp_path: Path,
) -> None:
  """Cover `SchedulesDirectEpgClient` discovery/sync, `EpgRefresh`, & Web UI."""
  now_ts = 1_764_000_000
  iso_start = "2025-11-24T16:00:00Z"
  target_day = "2025-11-24"

  # Write minimal export with Bell Fibe RDS + ConfigManager for WebGuideApp.
  xt_dir = write_minimal_xtream_export(tmp_path / "output" / "xtream")
  creds_path = tmp_path / "credentials.yaml"
  creds_path.write_text(
    f"""\
accounts:
  admin:
    email: "admin@example.com"
    web_password_hash: "{hash_web_password("pw")}"
    xtream_username: "xt111111"
    xtream_password: "xt222222"
    upstream_username: "u"
    upstream_password: "p"
    pipeline: true
""",
    encoding="utf-8",
  )
  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"""\
credentials_file: "{creds_path}"
paths:
  xtream_output_dir: "{xt_dir}"
upstream_xtream:
  server_url: "http://127.0.0.1:9"
pipeline:
  lineup:
    backend: "tvguide"
    lineup_id: "CAN-BELLFIBE-MTL-X"
    postal_code: "H3B"
""",
    encoding="utf-8",
  )

  # 1. `SchedulesDirectEpgClient`: empty lineups -> headends -> PUT -> schedule
  acct_lineups: list[dict[str, str]] = []

  def _sd_epg_handler(req: web.Request) -> web.Response:
    if req.path == "/token":
      return web.json_response({"code": 0, "token": "sd-tok"})
    if req.path == "/status":
      return web.json_response({"code": 0, "lineups": acct_lineups})
    if req.path == "/headends":
      return web.json_response(
        [
          "bad-he",
          {
            "location": "Montréal",
            "lineups": [
              "bad-lu",
              {"lineup": "CAN-FIBE-X", "name": "Bell Fibe TV"},
            ],
          },
        ]
      )
    if req.method == "PUT" and req.path == "/lineups/CAN-OTA-H3B":
      return web.json_response(
        {"code": 2100, "message": "Invalid lineup"}, status=400
      )
    if req.method == "PUT" and req.path == "/lineups/CAN-FIBE-X":
      acct_lineups.append(
        {
          "lineup": "CAN-FIBE-X",
          "name": "Sample Fiber TV",
          "location": "Montréal",
          "modified": "2025-11-24T00:00:00Z",
        }
      )
      return web.json_response({"code": 0})
    if req.path == "/lineups/CAN-FIBE-X":
      return web.json_response(
        {
          "map": [
            "bad-map",
            {"stationID": "10101", "channel": "1101"},
            {"stationID": "10202", "channel": "9999"},
          ],
          "stations": [
            "bad-st",
            {
              "stationID": "10101",
              "callsign": "ICIRDI",
              "name": "ICI RDI",
              "affiliate": "Radio-Canada",
            },
            {
              "stationID": "10202",
              "callsign": "RDS",
              "name": "RDS",
              "broadcastLanguage": ["fr"],
            },
          ],
        }
      )
    if req.path == "/schedules/md5":
      return web.json_response(
        {
          "10101": {
            "code": 0,
            target_day: {"code": 0, "md5": "d1"},
            "2025-11-25": {"code": 7010},
            "2025-11-26": {"code": 7010},
          },
          "10202": {"code": 7020},
        }
      )
    if req.path == "/schedules":
      return web.json_response(
        [
          {
            "stationID": "10101",
            "metadata": {"startDate": target_day, "md5": "d1"},
            "programs": [
              {
                "programID": "EP001",
                "md5": "p1",
                "airDateTime": iso_start,
                "duration": 3600,
                "liveTapeDelay": "Live",
                "new": True,
              },
              {
                "programID": "SP001",
                "md5": "p2",
                "airDateTime": "2025-11-24T17:00:00",
                "duration": 1800,
              },
              "bad-airing",
              {"programID": "", "airDateTime": "", "duration": 60},
              {
                "programID": "BADTS",
                "airDateTime": "not-an-iso",
                "duration": 60,
              },
            ],
          },
          {"stationID": "10101", "code": 7000},
          "bad-sched",
        ]
      )
    if req.path == "/programs":
      return web.json_response(
        [
          {
            "programID": "EP001",
            "md5": "p1",
            "titles": [{"title120": "Téléjournal"}],
            "episodeTitle150": "soir",
            "metadata": [{"Gracenote": {"season": 3, "episode": 4}}],
            "descriptions": {"description100": [{"description": "News"}]},
            "genres": ["News"],
            "movie": {"year": "2025"},
            "contentRating": [{"code": "G", "country": "FRA"}],
          },
          {
            "programID": "SP001",
            "md5": "p2",
            "titles": [{"title120": "Hockey Night"}],
            "eventDetails": {
              "teams": [{"name": "MTL"}, {"name": "TOR"}],
              "subType": "Hockey",
            },
            "descriptions": {"description1000": [{"description": "Live game"}]},
            "entityType": "Sports",
            "originalAirDate": "2025-11-24T00:00:00Z",
            "contentRating": [{"code": "TV-PG", "country": "USA"}],
          },
          {"programID": "ERR1", "code": 6000},
          "bad-prog",
        ]
      )
    return web.Response(status=404)

  async def _run_sd_and_refresh() -> None:
    sd_now = [float(now_ts)]
    db_file = tmp_path / "cache" / "epg_full.sqlite3"
    db = EpgDatabase(db_file)
    store = LineupGuideStore(xt_dir)
    try:
      async with MockHttpServer(_sd_epg_handler) as mock:

        def _sd_session_factory(
          *,
          timeout: aiohttp.ClientTimeout | None = None,
          headers: Mapping[str, str] | None = None,
        ) -> aiohttp.ClientSession:
          _ = timeout
          return mock.new_session(headers=headers)

        sd_cfg = SchedulesDirectEpgConfig(
          username="u",
          password="p",
          base_url=mock.base_url,
          postal_code="H3B",
          days=1,
          token_cache_file=tmp_path / "cache" / "sd_epg_tok.json",
        )
        sd_cli = SchedulesDirectEpgClient(
          sd_cfg,
          db=db,
          session_factory=_sd_session_factory,
          clock=lambda: sd_now[0],
        )
        snap = store.get_snapshot()
        summary = await sd_cli.fetch_for_snapshot(snap, now_ts=int(sd_now[0]))
        assert summary.matched_channels >= 1
        assert summary.fetched_programs == 2
        assert any(
          p.title == "Live: Téléjournal" and p.sub_title == "S03E04 · soir"
          for progs in summary.programs_by_chno.values()
          for p in progs
        )
        assert any(
          p.title == "Hockey Night" and p.sub_title == "MTL vs. TOR"
          for progs in summary.programs_by_chno.values()
          for p in progs
        )
        # Advance clock within the same UTC day with unchanged schedule MD5s
        sd_now[0] += 3600.0
        summary2 = await sd_cli.fetch_for_snapshot(snap, now_ts=int(sd_now[0]))
        assert summary2.fetched_schedule_days == 0
        assert summary2.fetched_programs == 0

        empty_sum = await SchedulesDirectEpgClient(
          SchedulesDirectEpgConfig(username="", password=""),
          db=db,
        ).fetch_for_snapshot(snap)
        assert empty_sum.matched_channels == 0

        sd_pref = SchedulesDirectEpgClient(
          SchedulesDirectEpgConfig(
            username="u",
            password="p",
            base_url=mock.base_url,
            postal_code="H3B",
            preferred_lineup_id="CAN-OTA-H3B",
            days=1,
            token_cache_file=tmp_path / "cache" / "sd_epg_tok.json",
          ),
          db=db,
          session_factory=_sd_session_factory,
          clock=lambda: sd_now[0],
        )
        sd_now[0] += 90_000.0
        await sd_pref.fetch_for_snapshot(snap, now_ts=int(sd_now[0]))
        sd_now[0] += 90_000.0
        summary3 = await sd_pref.fetch_for_snapshot(snap, now_ts=int(sd_now[0]))
        assert summary3.matched_channels == 2

      # Exercise EpgRefreshService with XMLTV file + concurrent refresh_now()
      ref_now = float(now_ts + 3600)
      xml_file = tmp_path / "epg.xml"
      xml_file.write_text(
        '<?xml version="1.0"?><tv>'
        '<programme start="20251124160000 +0000" stop="20251124170000 +0000" '
        'channel="ICIRDI.ca"><title>XML Show</title></programme></tv>',
        encoding="utf-8",
      )
      os.utime(xml_file, (now_ts, now_ts))
      bad_db = tmp_path / "cache" / "non_sqlite.db"
      bad_db.parent.mkdir(parents=True, exist_ok=True)
      bad_db.write_bytes(b"NOT SQLITE MAGIC BYTES")
      bad_ref = EpgRefreshService(
        store,
        EpgRefreshConfig(enabled=True, cache_file=bad_db, xmltv_file=xml_file),
        clock=lambda: float(now_ts),
      )
      try:
        assert bad_ref.hydrate_from_cache(now_ts=now_ts) == 0
      finally:
        await bad_ref.stop()

      ok_ref = EpgRefreshService(
        store,
        EpgRefreshConfig(
          enabled=True,
          cache_file=tmp_path / "cache" / "epg_xml_run.sqlite3",
          xmltv_file=xml_file,
        ),
        clock=lambda: ref_now,
      )
      try:
        r1, r2 = await asyncio.gather(
          ok_ref.refresh_now(force=True, reason="manual"),
          ok_ref.refresh_now(force=True, reason="joined"),
        )
        assert r1["state"] == "ok" and r2["state"] == "ok"
        assert ok_ref.hydrate_from_cache(now_ts=int(ref_now)) >= 1
        assert ok_ref.needs_refresh(now_ts=int(ref_now)) == (False, "fresh")
        xml_file.write_text(
          xml_file.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        os.utime(xml_file, (int(ref_now) + 50, int(ref_now) + 50))
        assert ok_ref.needs_refresh(now_ts=int(ref_now)) == (
          True,
          "xmltv_file_updated",
        )
        xml_file.unlink()
        assert ok_ref.needs_refresh(now_ts=int(ref_now)) == (
          False,
          "xmltv_file_missing",
        )
        await ok_ref.start()
        await asyncio.sleep(0.05)
      finally:
        await ok_ref.stop()
    finally:
      db.close()

  await _run_sd_and_refresh()

  # 2. `WebGuideApp`: `/api/account*`, removed `/api/config`, static, HLS, &
  #    `/api/stream`
  cfg_mgr = ConfigManager(cfg_path)
  auth = WebAuthManager(
    WebAuthConfig(
      accounts_provider=lambda: cfg_mgr.get().accounts,
      secret_file=tmp_path / "cache" / "w.key",
    )
  )

  web_app = WebGuideApp(
    LineupGuideStore(xt_dir),
    auth=auth,
    password_changer=cfg_mgr.change_account_web_password,
    stream_creds_regenerator=cfg_mgr.regenerate_account_stream_credentials,
    clock=lambda: float(now_ts),
  )
  await web_app.startup()
  runner, port = await start_site(web_app.build_app(), "127.0.0.1", 0)
  base = f"http://127.0.0.1:{port}"
  try:
    async with new_test_session(base) as client:
      async with client.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "pw"}
      ) as r_l:
        assert r_l.status == 200
      async with client.get(
        "/login?next=/guide", allow_redirects=False
      ) as r_redir:
        assert r_redir.status == 302
      async with client.get("/static/styles.css") as r_css:
        assert r_css.status == 200
      async with client.get("/favicon.ico") as r_fav:
        assert r_fav.status == 200
      async with client.get("/api/config") as r_cfg_gone:
        assert r_cfg_gone.status == 404
      async with client.get("/api/account") as r_acct:
        assert r_acct.status == 200
        assert (await r_acct.json())["xtream_username"] == "xt111111"
      async with client.post(
        "/api/account/password", data="{bad-json"
      ) as r_bad_ct:
        assert r_bad_ct.status == 415
      async with client.post(
        "/api/account/password",
        data="{bad-json",
        headers={"Content-Type": "application/json"},
      ) as r_bad_j:
        assert r_bad_j.status == 400
      async with client.post(
        "/api/account/password",
        json={"current_password": "wrong", "new_password": "new_password_2"},
      ) as r_bad_v:
        assert r_bad_v.status == 403
      async with client.post(
        "/api/account/password",
        json={"current_password": "pw", "new_password": "new_password_2"},
      ) as r_ok_c:
        assert r_ok_c.status == 200
      async with client.post(
        "/api/account/stream-credentials/regenerate", json={}
      ) as r_regen:
        assert r_regen.status == 200
        assert (await r_regen.json())["xtream_username"] != "xt111111"
      async with client.get("/api/hls/1101/asset") as r_no_u:
        assert r_no_u.status == 400
      async with client.get("/api/hls/1101/asset?u=unknown_tok") as r_bad_u:
        assert r_bad_u.status == 403
      async with client.post("/api/tuner/release?client=test-1") as r_rel:
        assert r_rel.status == 200
      async with client.head("/api/stream/1101.ts") as r_head:
        assert r_head.status == 200
      async with client.get("/api/stream/1101.ts") as r_ts_fail:
        # Upstream port 9 rejects TCP -> clean 502 error JSON (no crash)
        assert r_ts_fail.status == 502
  finally:
    await runner.cleanup()
    await web_app.shutdown()

  # 3. `WebGuideApp`: happy `/api/stream/*.ts`, HLS, & `501` unconfigured
  #    mutations
  def _hls_up_handler(req: web.Request) -> web.Response:
    if req.path == "/live/u/p/410101.ts":
      return web.Response(
        status=200,
        headers={"Content-Type": "video/mp2t"},
        body=make_valid_mpegts_chunk(4),
      )
    if req.path == "/live/u/p/410101.m3u8":
      return web.Response(
        status=200,
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
        text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n/sub.m3u8\n",
      )
    if req.path == "/sub.m3u8":
      return web.Response(
        status=200,
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
        text="#EXTM3U\n#EXTINF:4.0,\n/seg0.ts\n",
      )
    if req.path == "/seg0.ts":
      return web.Response(
        status=200,
        headers={"Content-Type": "video/mp2t"},
        body=make_valid_mpegts_chunk(2),
      )
    if req.path == "/bad.m3u8":
      return web.Response(status=200, content_type="text/plain", text="NOT_M3U")
    return web.Response(status=404)

  async with MockHttpServer(_hls_up_handler) as hls_mock:
    hls_xt_dir = write_minimal_xtream_export(
      tmp_path / "output" / "xtream_hls",
      streams=[
        {
          "num": 1101,
          "name": "ICI RDI",
          "stream_type": "live",
          "stream_id": 410101,
          "stream_icon": "https://logos.example/rdi.png",
          "epg_channel_id": "ICIRDI.ca",
          "category_id": "1",
          "direct_source": f"{hls_mock.base_url}/live/u/p/410101.ts",
        }
      ],
    )
    hls_acct = make_test_account(
      account_id="hls_user",
      email="hls@example.com",
      web_password_hash=hash_web_password("pw"),
    )
    hls_auth = WebAuthManager(
      WebAuthConfig(
        accounts=(hls_acct,),
        secret_file=tmp_path / "cache" / "hls.key",
      )
    )
    hls_web = WebGuideApp(
      LineupGuideStore(hls_xt_dir),
      auth=hls_auth,
      clock=lambda: float(now_ts),
    )
    await hls_web.startup()
    hls_runner, hls_port = await start_site(hls_web.build_app(), "127.0.0.1", 0)
    hls_base = f"http://127.0.0.1:{hls_port}"
    try:
      async with new_test_session(hls_base) as hclient:
        async with hclient.post(
          "/api/auth/login",
          json={"email": "hls@example.com", "password": "pw"},
        ) as r_hl:
          assert r_hl.status == 200
        async with hclient.post(
          "/api/account/password",
          json={"current_password": "pw", "new_password": "new_password_2"},
        ) as r_no_cfg:
          assert r_no_cfg.status == 501
        async with hclient.post("/api/epg/refresh") as r_ref:
          assert r_ref.status == 200
        async with hclient.get("/api/stream/1101.ts") as r_ts_ok:
          assert r_ts_ok.status == 200
          assert len(await r_ts_ok.read()) >= 188
        async with hclient.get("/api/hls/1101/index.m3u8") as r_m3u:
          assert r_m3u.status == 200
          m3u_txt = await r_m3u.text()
          assert "/api/hls/1101/asset?u=" in m3u_txt
        sub_tok = m3u_txt.split("asset?u=", 1)[1].splitlines()[0].strip()
        async with hclient.get(f"/api/hls/1101/asset?u={sub_tok}") as r_sub:
          assert r_sub.status == 200
          sub_txt = await r_sub.text()
          assert "/api/hls/1101/asset?u=" in sub_txt
        seg_tok = sub_txt.split("asset?u=", 1)[1].splitlines()[0].strip()
        async with hclient.get(f"/api/hls/1101/asset?u={seg_tok}") as r_seg:
          assert r_seg.status == 200
          assert len(await r_seg.read()) >= 188
        bad_tok = hls_web._register_hls_asset(
          hls_acct, 1101, f"{hls_mock.base_url}/bad.m3u8"
        )
        async with hclient.get(f"/api/hls/1101/asset?u={bad_tok}") as r_bad:
          assert r_bad.status == 200
          assert await r_bad.text() == "NOT_M3U"
        miss_tok = hls_web._register_hls_asset(
          hls_acct, 1101, f"{hls_mock.base_url}/missing.ts"
        )
        async with hclient.get(f"/api/hls/1101/asset?u={miss_tok}") as r_miss:
          assert r_miss.status == 502
    finally:
      await hls_runner.cleanup()
      await hls_web.shutdown()


def test_accounts_and_config_load_edge_branches(tmp_path: Path) -> None:
  """Cover validation error branches in `accounts` and `config.load`."""
  from bellhop.common.accounts import (
    UserAccount,
    build_upstream_stream_url,
    normalize_upstream_base_url,
    password_hash_version,
    validate_accounts_collection,
    validate_email,
    validate_stream_credential,
    verify_web_password,
  )
  from bellhop.config.load import (
    resolve_config_targets,
    resolve_credentials_path,
  )

  # 1. `common/accounts.py` validation & normalization error branches
  for bad_email in ("", "ctrl\x07@example.com", "not-an-email"):
    with pytest.raises(ValueError):
      validate_email(bad_email, account_id="acct")

  for bad_cred in ("", "bad/cred", "bad?cred", "bad cred"):
    with pytest.raises(ValueError):
      validate_stream_credential(bad_cred, account_id="acct")

  for blank_pw in ("", "   "):
    with pytest.raises(ValueError):
      hash_web_password(blank_pw)

  assert verify_web_password("", "scrypt$16384$8$1$a$b") is False
  assert verify_web_password("pw", "bcrypt$bad") is False
  assert (
    verify_web_password("pw", "scrypt$16384$8$1$not_b64!$not_b64!") is False
  )
  assert (
    verify_web_password(
      "pw", "scrypt$3$8$1$AAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAA=="
    )
    is False
  )
  assert (
    verify_web_password(
      "pw", "scrypt$1$8$1$AAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAA=="
    )
    is False
  )
  assert normalize_upstream_base_url("") == ""
  assert normalize_upstream_base_url("panel.example") == "http://panel.example"
  assert build_upstream_stream_url("", "u", "p", 123) == ""
  valid_hash = hash_web_password("good_pw_123")
  assert len(password_hash_version(valid_hash)) == 12

  base_acct = UserAccount(
    account_id="primary",
    email="primary@example.com",
    web_password_hash=valid_hash,
    xtream_username="xt111111",
    xtream_password="xt222222",
    upstream_username="up1",
    upstream_password="upp",
    pipeline=True,
  )
  with pytest.raises(ValueError, match="At least one account"):
    validate_accounts_collection([], require_non_empty=True)
  with pytest.raises(ValueError, match="cannot be empty"):
    validate_accounts_collection(
      [
        UserAccount(
          account_id="   ",
          email=base_acct.email,
          web_password_hash=valid_hash,
          xtream_username="xt1",
          xtream_password="xp1",
          upstream_username="u1",
          upstream_password="p1",
        )
      ]
    )
  with pytest.raises(ValueError, match="Duplicate account"):
    validate_accounts_collection(
      [
        base_acct,
        UserAccount(
          account_id="PRIMARY",
          email="other@example.com",
          web_password_hash=valid_hash,
          xtream_username="xt2",
          xtream_password="xp2",
          upstream_username="u2",
          upstream_password="p2",
        ),
      ]
    )
  with pytest.raises(ValueError, match="web_password_hash"):
    validate_accounts_collection(
      [
        UserAccount(
          account_id="badhash",
          email="bh@example.com",
          web_password_hash="invalid",
          xtream_username="xt3",
          xtream_password="xp3",
          upstream_username="u3",
          upstream_password="p3",
        )
      ]
    )
  with pytest.raises(ValueError, match="upstream_username"):
    validate_accounts_collection(
      [
        UserAccount(
          account_id="noup",
          email="nu@example.com",
          web_password_hash=valid_hash,
          xtream_username="xt4",
          xtream_password="xp4",
          upstream_username="",
          upstream_password="",
        )
      ]
    )

  # 2. `config/load.py` path resolution, malformed creds, & string proxy list
  with pytest.raises(FileNotFoundError):
    resolve_config_targets(tmp_path / "does_not_exist.yaml")

  root_dir = tmp_path / "proj"
  sub_dir = root_dir / "sub"
  sub_dir.mkdir(parents=True)
  (root_dir / "pyproject.toml").write_text("[project]\nname='x'\n")
  (root_dir / "credentials.yaml").write_text("accounts: {}\n")
  assert (
    resolve_credentials_path(
      {"credentials_file": "credentials.yaml"},
      root=root_dir,
      base_dir=sub_dir,
    )
    == root_dir / "credentials.yaml"
  )

  # Blank / non-mapping / bad account entries in credentials.yaml
  cfg_p = tmp_path / "edge_cfg.yaml"
  creds_p = tmp_path / "edge_creds.yaml"
  cfg_p.write_text(
    f"""\
credentials_file: "{creds_p}"
upstream_xtream:
  server_url: "http://panel.example"
web_server:
  trusted_proxy_ips: "127.0.0.1, ::1"
""",
    encoding="utf-8",
  )

  creds_p.write_text("", encoding="utf-8")
  with pytest.raises(ValueError, match="accounts:"):
    load_config(cfg_p)

  creds_p.write_text("- not-a-mapping\n", encoding="utf-8")
  with pytest.raises(ValueError, match="YAML mapping"):
    load_config(cfg_p)

  creds_p.write_text("accounts:\n  bad: 'string'\n", encoding="utf-8")
  with pytest.raises(ValueError, match="YAML mapping"):
    load_config(cfg_p)

  creds_p.write_text(
    'accounts:\n  "":\n    email: "a@b.com"\n', encoding="utf-8"
  )
  with pytest.raises(ValueError, match="Account key"):
    load_config(cfg_p)

  # Legacy `password:` alias normalization
  creds_p.write_text(
    """\
accounts:
  legacy_user:
    email: "legacy@example.com"
    password: "legacy_web_password"
    upstream_username: "up_leg"
    upstream_password: "up_leg_pw"
""",
    encoding="utf-8",
  )
  loaded_edge = load_config(cfg_p)
  assert loaded_edge.serve.web.trusted_proxy_ips == ["127.0.0.1", "::1"]
  assert loaded_edge.accounts[0].web_password_hash.startswith("scrypt$")
  assert "legacy_web_password" not in creds_p.read_text(encoding="utf-8")

  # Blank plaintext web_password/password keys stripped alongside existing hash
  creds_p.write_text(
    f"""\
accounts:
  legacy_user:
    email: "legacy@example.com"
    web_password: ""
    password: ""
    web_password_hash: "{valid_hash}"
    upstream_username: "up_leg"
    upstream_password: "up_leg_pw"
""",
    encoding="utf-8",
  )
  loaded_blank_pw = load_config(cfg_p)
  assert loaded_blank_pw.accounts[0].web_password_hash == valid_hash
  creds_disk = creds_p.read_text(encoding="utf-8")
  assert "web_password:" not in creds_disk
  assert "\n    password:" not in creds_disk

  # Invalid trusted_proxy_ips type -> ValueError
  cfg_p.write_text(
    f"""\
credentials_file: "{creds_p}"
upstream_xtream:
  server_url: "http://panel.example"
web_server:
  trusted_proxy_ips: 123
""",
    encoding="utf-8",
  )
  with pytest.raises(ValueError, match="trusted_proxy_ips"):
    load_config(cfg_p)
