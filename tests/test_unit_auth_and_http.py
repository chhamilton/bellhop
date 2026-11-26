"""Focused unit & HTTP hardening tests for auth cookies, rate limits, and
XFF.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from yarl import URL

from bellhop.common.http_utils import (
  DEFAULT_CLIENT_MAX_SIZE,
  build_access_urls,
  parse_trusted_proxy_networks,
  resolve_bind_host,
  resolve_client_ip,
  start_site,
)
from bellhop.config import load_config
from bellhop.web.auth import (
  LoginRateLimiter,
  WebAuthConfig,
  WebAuthManager,
  render_login_page_html,
  sanitize_next_url,
)
from bellhop.web.server import WebGuideApp
from bellhop.web.store import LineupGuideStore
from tests.conftest import FakeClock
from tests.http_mock import new_test_session


def _write_minimal_bundle(xt_dir: Path) -> None:
  xt_dir.mkdir(parents=True, exist_ok=True)
  (xt_dir / "bellhop_xtream_playlist.json").write_text(
    json.dumps(
      {
        "lineup_info": {"lineup_id": "TEST", "provider": "Bellhop"},
        "server_info": {"timezone": "America/Montreal"},
        "live_categories": [{"category_id": "1", "category_name": "General"}],
        "live_streams": [
          {
            "num": 1101,
            "name": "ICI Radio-Canada",
            "stream_id": 101,
            "category_id": "1",
            "category_name": "General",
            "direct_source": "http://upstream.example/live/u/p/101.ts",
          }
        ],
      }
    ),
    encoding="utf-8",
  )


def test_sanitize_next_url_and_login_html_escaping() -> None:
  """Verify strict internal-only redirects and HTML single-quote escaping on
  `/login`.
  """
  assert sanitize_next_url(None) == "/"
  assert sanitize_next_url("/?cat=2&ch=1108") == "/?cat=2&ch=1108"
  assert sanitize_next_url("//evil.example/phish") == "/"
  assert sanitize_next_url("https://evil.example") == "/"
  assert sanitize_next_url("http:/evil.example") == "/"
  assert sanitize_next_url("/\\evil.example") == "/"
  assert sanitize_next_url("/\t/evil.example") == "/"
  assert sanitize_next_url("/\n/evil.example") == "/"
  assert sanitize_next_url("/%09/evil.example") == "/"
  assert sanitize_next_url("/%2f/evil.example") == "/"
  assert sanitize_next_url("/%5cevil.example") == "/"
  assert sanitize_next_url("/login?next=/") == "/"

  rendered = render_login_page_html(
    next_url="/?q=O'Connor", error_message="Can't sign in"
  ).decode("utf-8")
  assert "&#39;" in rendered
  assert "Can't" not in rendered
  assert "<script>" not in rendered
  assert "<style>" not in rendered


def test_web_auth_token_signing_persistence_and_rotation_with_fake_clock(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Verify HMAC session tokens, 0600 secret persistence, Unicode passwords, and
  mandatory login.
  """
  import stat

  # Unconfigured credentials MUST fail verification and MUST be rejected by
  # WebGuideApp
  unconfigured = WebAuthManager(WebAuthConfig())
  assert unconfigured.enabled is True
  assert unconfigured.has_credentials is False
  assert unconfigured.verify_credentials("", "") is False
  xt_empty = tmp_path / "empty_xtream"
  xt_empty.mkdir()
  with pytest.raises(ValueError, match="cannot be disabled"):
    WebGuideApp(LineupGuideStore(xt_empty), auth=unconfigured)

  secret_file = tmp_path / "cache" / "session_secret.key"
  auth1 = WebAuthManager(
    WebAuthConfig(
      username="admín",
      password="sëcret_web_password_🔐",
      session_ttl_hours=2.0,
      secret_file=secret_file,
    ),
    clock=fake_clock,
  )
  assert auth1.enabled is True
  assert auth1.verify_credentials("admín", "sëcret_web_password_🔐") is True
  assert auth1.verify_credentials("admín", "wrong_é") is False

  token, session = auth1.create_session_token("admín")
  assert secret_file.is_file()
  assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
  assert session.expires_at == int(fake_clock.now) + 7200

  # Before half-TTL: valid, no renewal needed
  fake_clock.advance(100.0)
  assert session.needs_renewal(now_ts=int(fake_clock.now)) is False
  assert auth1.verify_session_token(token) is not None
  assert auth1.verify_session_token(token + "x") is None

  # Past half-TTL: valid, sliding renewal triggered
  fake_clock.advance(3900.0)
  assert session.needs_renewal(now_ts=int(fake_clock.now)) is True

  # Restart with same secret_file keeps cookie valid
  auth2 = WebAuthManager(
    WebAuthConfig(
      username="admín",
      password="sëcret_web_password_🔐",
      session_ttl_hours=2.0,
      secret_file=secret_file,
    ),
    clock=fake_clock,
  )
  verified = auth2.verify_session_token(token)
  assert verified is not None and verified.username == "admín"

  # Password change invalidates existing cookies immediately
  auth_rotated = WebAuthManager(
    WebAuthConfig(
      username="admín",
      password="brand_new_password",
      session_ttl_hours=2.0,
      secret_file=secret_file,
    ),
    clock=fake_clock,
  )
  assert auth_rotated.verify_session_token(token) is None

  # Past full TTL (7201s total): original auth rejects expired token
  fake_clock.advance(3201.0)
  assert auth1.verify_session_token(token) is None


@pytest.mark.asyncio
async def test_http_sliding_cookie_renewal_and_expiry_via_fake_clock(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Verify live HTTP sliding session renewal & expiry with zero sleeps."""
  xt_dir = tmp_path / "output" / "xtream"
  _write_minimal_bundle(xt_dir)
  auth = WebAuthManager(
    WebAuthConfig(
      username="admin",
      password="secret",
      session_ttl_hours=2.0,  # 7200s TTL -> renews at >= 3600s
      secret_file=tmp_path / "session_secret.key",
    ),
    clock=fake_clock,
  )
  app = WebGuideApp(
    LineupGuideStore(xt_dir), auth=auth, settle_sec=0.0, clock=fake_clock
  )
  await app.startup()
  runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
  try:
    async with new_test_session(f"http://127.0.0.1:{port}") as client:
      async with client.post(
        "/api/auth/login", json={"username": "admin", "password": "secret"}
      ) as login_r:
        assert login_r.status == 200
        assert "bellhop_session=" in login_r.headers.get("Set-Cookie", "")

      # t + 600s (< 3600s): authenticated, no Set-Cookie renewal header
      fake_clock.advance(600.0)
      async with client.get("/api/health") as h1:
        assert h1.status == 200
        assert "Set-Cookie" not in h1.headers

      # t + 3700s (>= 3600s): authenticated AND issues renewed Set-Cookie!
      fake_clock.advance(3100.0)
      async with client.get("/api/health") as h2:
        assert h2.status == 200
        assert "bellhop_session=" in h2.headers.get("Set-Cookie", "")

      # Advance 8000s past the renewed cookie's 7200s TTL -> 401 + clear cookie!
      fake_clock.advance(8000.0)
      async with client.get("/api/health") as h_exp:
        assert h_exp.status == 401
        assert "Max-Age=0" in h_exp.headers.get("Set-Cookie", "")
  finally:
    await runner.cleanup()
    await app.shutdown()


def test_login_rate_limiter_unit_with_fake_clock(
  fake_clock: FakeClock,
) -> None:
  """Verify per-IP sliding window, Retry-After math, and max-IP eviction."""
  limiter = LoginRateLimiter(
    max_failures=3, window_sec=30.0, max_tracked_ips=2, clock=fake_clock
  )
  assert limiter.check("10.0.0.5") == 0
  limiter.record_failure("10.0.0.5")
  fake_clock.advance(5.0)
  limiter.record_failure("10.0.0.5")
  assert limiter.check("10.0.0.5") == 0
  fake_clock.advance(5.0)
  limiter.record_failure("10.0.0.5")
  assert limiter.check("10.0.0.5") == 20
  assert limiter.check("10.0.0.6") == 0

  # Bounded map eviction when tracking more than max_tracked_ips
  limiter.record_failure("10.0.0.6")
  limiter.record_failure("10.0.0.7")
  assert len(limiter._failures) <= 2

  # Advancing past window unblocks
  fake_clock.advance(31.0)
  assert limiter.check("10.0.0.6") == 0
  limiter.record_failure("10.0.0.6")
  limiter.record_success("10.0.0.6")
  assert limiter.check("10.0.0.6") == 0


def test_resolve_client_ip_and_bind_host_helpers(tmp_path: Path) -> None:
  """Verify rightmost-untrusted XFF parsing and LAN bind URL helpers."""
  nets = parse_trusted_proxy_networks(["127.0.0.1", "::1", "172.16.0.0/12"])
  assert (
    resolve_client_ip(
      "127.0.0.1",
      xff_headers=["203.0.113.5"],
      trust_proxy_headers=False,
      trusted_networks=nets,
    )
    == "127.0.0.1"
  )
  assert (
    resolve_client_ip(
      "198.51.100.9",
      xff_headers=["203.0.113.5"],
      trust_proxy_headers=True,
      trusted_networks=nets,
    )
    == "198.51.100.9"
  )
  assert (
    resolve_client_ip(
      "::ffff:127.0.0.1",
      xff_headers=["1.1.1.1", "203.0.113.42, 172.18.0.2"],
      trust_proxy_headers=True,
      trusted_networks=nets,
    )
    == "203.0.113.42"
  )
  assert (
    resolve_client_ip(
      "127.0.0.1",
      real_ip_header="203.0.113.88",
      trust_proxy_headers=True,
      trusted_networks=nets,
    )
    == "203.0.113.88"
  )

  assert resolve_bind_host(None) == "0.0.0.0"
  assert resolve_bind_host("") == "0.0.0.0"
  assert resolve_bind_host("127.0.0.1") == "127.0.0.1"
  assert (
    resolve_bind_host("auto", ip_detector=lambda: "192.168.1.42")
    == "192.168.1.42"
  )
  assert (
    resolve_bind_host("lan", ip_detector=lambda: "192.168.1.42")
    == "192.168.1.42"
  )
  assert build_access_urls(
    "0.0.0.0", 8080, ip_detector=lambda: "192.168.1.42"
  ) == [
    "http://192.168.1.42:8080",
    "http://localhost:8080",
  ]
  assert resolve_bind_host("auto", ip_detector=lambda: None) == "0.0.0.0"
  assert build_access_urls("0.0.0.0", 8080, ip_detector=lambda: None) == [
    "http://localhost:8080"
  ]

  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    "upstream_xtream:\n  server_url: http://example.invalid\n", encoding="utf-8"
  )
  assert load_config(cfg_path).serve.web_host == "0.0.0.0"


@pytest.mark.asyncio
async def test_http_hardening_413_403_cors_and_reverse_proxy_throttling(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Verify HTTP middleware limits (413/403/204/404) & XFF rate-limit
  isolation.
  """
  xt_dir = tmp_path / "output" / "xtream"
  _write_minimal_bundle(xt_dir)
  auth = WebAuthManager(
    WebAuthConfig(
      username="admin",
      password="correct_password",
      secret_file=tmp_path / "session_secret.key",
    ),
    clock=fake_clock,
  )

  # 1. Trusted proxy (`127.0.0.1` allowlisted) isolates rate limit per XFF
  #    client
  #    and recovers deterministically when `fake_clock` advances past window!
  app_trusted = WebGuideApp(
    LineupGuideStore(xt_dir),
    auth=auth,
    login_limiter=LoginRateLimiter(
      max_failures=2, window_sec=60.0, clock=fake_clock
    ),
    trust_proxy_headers=True,
    trusted_proxy_ips=["127.0.0.1"],
    settle_sec=0.0,
    clock=fake_clock,
  )
  await app_trusted.startup()
  runner_a, port_a = await start_site(app_trusted.build_app(), "127.0.0.1", 0)
  base_a = f"http://127.0.0.1:{port_a}"
  try:
    async with new_test_session(base_a) as client:
      async with client.options("/api/lineup") as opt_r:
        assert opt_r.status == 401
        assert "Access-Control-Allow-Origin" not in opt_r.headers

      oversized = b"x" * (DEFAULT_CLIENT_MAX_SIZE + 512)
      async with client.post(
        "/api/auth/login",
        data=oversized,
        headers={"Content-Type": "application/json"},
      ) as big_r:
        assert big_r.status == 413

      for fake_prefix in ("8.8.8.8", "9.9.9.9"):
        async with client.post(
          "/api/auth/login",
          json={"username": "admin", "password": "wrong"},
          headers={"X-Forwarded-For": f"{fake_prefix}, 203.0.113.10"},
        ) as bad_r:
          assert bad_r.status == 401

      async with client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "correct_password"},
        headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.10"},
      ) as blocked_r:
        assert blocked_r.status == 429
        assert int(blocked_r.headers["Retry-After"]) == 60

      # Separate client IP behind trusted proxy is unaffected
      async with client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "correct_password"},
        headers={"X-Forwarded-For": "203.0.113.20"},
      ) as legit_r:
        assert legit_r.status == 200

      # Authenticated static traversal returns 403 & unknown /api/* returns 404
      async with client.get(
        URL(f"{base_a}/static/%2e%2e/pyproject.toml", encoded=True)
      ) as trav_r:
        assert trav_r.status == 403
      async with client.get("/api/unknown-endpoint") as nf_r:
        assert nf_r.status == 404

      # Advance fake clock 61s -> 203.0.113.10 is unblocked with zero sleeps!
      fake_clock.advance(61.0)
      async with client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "correct_password"},
        headers={"X-Forwarded-For": "203.0.113.10"},
      ) as unblocked_r:
        assert unblocked_r.status == 200
  finally:
    await runner_a.cleanup()
    await app_trusted.shutdown()

  # 2. Unlisted peer (`127.0.0.1` not in `["10.99.0.1"]`) ignores spoofed XFF
  app_unlisted = WebGuideApp(
    LineupGuideStore(xt_dir),
    auth=auth,
    login_limiter=LoginRateLimiter(
      max_failures=2, window_sec=60.0, clock=fake_clock
    ),
    trust_proxy_headers=True,
    trusted_proxy_ips=["10.99.0.1"],
    settle_sec=0.0,
    clock=fake_clock,
  )
  await app_unlisted.startup()
  runner_b, port_b = await start_site(app_unlisted.build_app(), "127.0.0.1", 0)
  try:
    async with new_test_session(f"http://127.0.0.1:{port_b}") as client:
      for spoof_ip in ("203.0.113.1", "203.0.113.2"):
        async with client.post(
          "/api/auth/login",
          json={"username": "admin", "password": "wrong"},
          headers={"X-Forwarded-For": spoof_ip},
        ) as bad_r2:
          assert bad_r2.status == 401
      async with client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "correct_password"},
        headers={"X-Forwarded-For": "203.0.113.99"},
      ) as blocked_r2:
        assert blocked_r2.status == 429
  finally:
    await runner_b.cleanup()
    await app_unlisted.shutdown()


@pytest.mark.asyncio
async def test_csrf_cors_and_hls_ssrf_protection(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Verify Web Guide omits wildcard CORS, blocks CSRF/GET mutations, and
  rejects forged HLS tokens.
  """
  import base64

  xt_dir = tmp_path / "output" / "xtream"
  _write_minimal_bundle(xt_dir)
  auth = WebAuthManager(
    WebAuthConfig(
      username="admin",
      password="secret",
      secret_file=tmp_path / "session_secret.key",
    ),
    clock=fake_clock,
  )
  app = WebGuideApp(
    LineupGuideStore(xt_dir),
    auth=auth,
    config_reader=lambda: {
      "available": True,
      "config_file": "config.yaml",
      "config": {},
    },
    config_writer=lambda _u: {
      "saved": True,
      "restart_required": False,
      "config_file": "config.yaml",
      "config": {},
    },
    settle_sec=0.0,
    clock=fake_clock,
  )
  await app.startup()
  runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
  try:
    async with new_test_session(f"http://127.0.0.1:{port}") as client:
      # Cross-site login blocked with 403
      async with client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
        headers={"Origin": "https://attacker.example"},
      ) as csrf_login:
        assert csrf_login.status == 403

      # Legitimate login succeeds and never emits wildcard CORS
      async with client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
      ) as ok_login:
        assert ok_login.status == 200
        assert "Access-Control-Allow-Origin" not in ok_login.headers

      async with client.get("/api/lineup") as lu_r:
        assert lu_r.status == 200
        assert "Access-Control-Allow-Origin" not in lu_r.headers

      # State-changing GET routes are disabled (rejected with 404/405, zero
      # state change)
      for get_mut in (
        "/api/auth/logout",
        "/api/epg/refresh",
        "/api/tuner/release",
      ):
        async with client.get(get_mut) as gm_r:
          assert gm_r.status in {404, 405}

      # Cross-site POST/PUT mutations blocked via Sec-Fetch-Site and Origin
      async with client.put(
        "/api/config",
        json={},
        headers={"Sec-Fetch-Site": "cross-site"},
      ) as csrf_put:
        assert csrf_put.status == 403

      # Non-JSON Content-Type on PUT /api/config rejected with 415
      async with client.put(
        "/api/config",
        data="{}",
        headers={"Content-Type": "text/plain"},
      ) as plain_put:
        assert plain_put.status == 415

      # Forged base64 URL in `/api/hls/<chno>/asset?u=` rejected with 403 (zero
      # SSRF)
      forged = (
        base64.urlsafe_b64encode(b"http://127.0.0.1:22/secret")
        .decode("ascii")
        .rstrip("=")
      )
      async with client.get(f"/api/hls/1101/asset?u={forged}") as ssrf_r:
        assert ssrf_r.status == 403
  finally:
    await runner.cleanup()
    await app.shutdown()


@pytest.mark.asyncio
async def test_session_revocation_on_logout_https_cookie_flag_and_url_redaction(
  tmp_path: Path,
) -> None:
  """Verify logout revokes session replay, `X-Forwarded-Proto` sets `Secure`,
  and URLs redact.
  """
  from bellhop.common.http_utils import redact_url_secrets
  from tests.conftest import write_minimal_xtream_export

  redacted = redact_url_secrets(
    "Failed GET http://u:p1@panel.example/live/alice/sec_pw/101.ts?password=q_pw&token=t_tok"
  )
  assert "p1" not in redacted
  assert "sec_pw" not in redacted
  assert "q_pw" not in redacted
  assert "t_tok" not in redacted
  assert "http://u:***@panel.example/live/alice/***/101.ts" in redacted

  xt_dir = write_minimal_xtream_export(tmp_path / "output" / "xtream")
  auth = WebAuthManager(
    WebAuthConfig(
      username="admin",
      password="secret",
      secret_file=tmp_path / "session_secret.key",
    )
  )
  app = WebGuideApp(
    LineupGuideStore(xt_dir),
    auth=auth,
    trust_proxy_headers=True,
    trusted_proxy_ips=("127.0.0.1", "::1"),
  )
  await app.startup()
  runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
  base_url = f"http://127.0.0.1:{port}"
  try:
    # Plain HTTP login omits `Secure`; trusted `X-Forwarded-Proto: https` adds
    # `Secure`
    async with (
      new_test_session(base_url) as client_http,
      client_http.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
      ) as r_http,
    ):
      assert r_http.status == 200
      set_cookie_http = r_http.headers.get("Set-Cookie", "")
      assert "Secure" not in set_cookie_http

    async with new_test_session(base_url) as client_https:
      async with client_https.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
        headers={"X-Forwarded-Proto": "https"},
      ) as r_https:
        assert r_https.status == 200
        set_cookie_https = r_https.headers.get("Set-Cookie", "")
        assert "; Secure" in set_cookie_https
        raw_token = set_cookie_https.split(";", 1)[0].split("=", 1)[1].strip()

      # Logout clears cookie AND revokes token server-side
      async with client_https.post(
        "/api/auth/logout",
        headers={
          "X-Forwarded-Proto": "https",
          "Cookie": f"bellhop_session={raw_token}",
        },
      ) as r_out:
        assert r_out.status == 200
        assert "; Secure" in r_out.headers.get("Set-Cookie", "")

    # Replaying the captured pre-logout cookie in a fresh client is rejected
    # with 401!
    async with (
      new_test_session(base_url) as replay_client,
      replay_client.get(
        "/api/lineup",
        headers={"Cookie": f"bellhop_session={raw_token}"},
      ) as r_replay,
    ):
      assert r_replay.status == 401
  finally:
    await runner.cleanup()
    await app.shutdown()


@pytest.mark.asyncio
async def test_url_redaction_and_ssrf_protection_controls() -> None:
  """Verify shorthand redaction, URL/DNS SSRF filtering, & redirect blocking."""
  import socket

  import aiohttp.abc
  from aiohttp import web

  from bellhop.common.http_utils import (
    SafeUpstreamResolver,
    UpstreamTargetBlockedError,
    extract_explicit_private_origins,
    open_upstream_url,
    redact_url_secrets,
    validate_upstream_url,
  )
  from tests.http_mock import MockHttpServer

  # 1. `redact_url_secrets` handles shorthand URLs and explicit secret literals.
  shorthand = redact_url_secrets(
    "HTTP error fetching http://panel.example:8080/alice/s3cr3t_pw/410101.ts "
    "token=tok99 secret=custom_pw"
  )
  assert "s3cr3t_pw" not in shorthand
  assert "tok99" not in shorthand
  assert "http://panel.example:8080/alice/***/410101.ts" in shorthand
  literal = redact_url_secrets(
    "ffmpeg failed: bad key custom_pw on stream", secret="custom_pw"
  )
  assert "custom_pw" not in literal
  assert "***" in literal

  # 2. `extract_explicit_private_origins` & `validate_upstream_url`
  explicit = extract_explicit_private_origins(
    [
      "http://127.0.0.1:54321/live/u/p/1.ts",
      "http://host.docker.internal:54321/live/u/p/2.ts",
      "http://169.254.169.254/latest/meta-data",
      "http://0.0.0.0:8080/x",
      "http://panel.example:8080/live/u/p/3.ts",
    ]
  )
  assert ("127.0.0.1", 54321) in explicit
  assert ("host.docker.internal", 54321) in explicit
  assert ("panel.example", 8080) not in explicit
  assert all(host not in ("169.254.169.254", "0.0.0.0") for host, _ in explicit)

  assert str(
    validate_upstream_url(
      "http://127.0.0.1:54321/live/u/p/1.ts",
      allowed_private_origins=explicit,
    )
  ).startswith("http://127.0.0.1:54321/")

  for blocked in (
    "http://127.0.0.1:8080/api/config",
    "http://127.1/secret",
    "http://2130706433/secret",
    "http://0x7f000001/secret",
    "http://[::1]:8080/api/config",
    "http://[::ffff:127.0.0.1]:8080/api/config",
    "http://localhost/secret",
    "http://localhost./secret",
    "http://169.254.169.254/latest/meta-data",
    "http://192.168.1.1/admin",
    "file:///etc/passwd",
  ):
    with pytest.raises(UpstreamTargetBlockedError):
      validate_upstream_url(blocked, allowed_private_origins=explicit)

  # 3. `SafeUpstreamResolver` blocks DNS rebinding to non-public IPs.
  class _StubResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, target_ip: str) -> None:
      self.target_ip = target_ip

    async def resolve(
      self,
      host: str,
      port: int = 0,
      family: socket.AddressFamily = socket.AF_INET,
    ) -> list[aiohttp.abc.ResolveResult]:
      _ = family
      return [
        {
          "hostname": host,
          "host": self.target_ip,
          "port": port,
          "family": socket.AF_INET,
          "proto": socket.IPPROTO_TCP,
          "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
        }
      ]

    async def close(self) -> None:
      return None

  for rebind_ip in ("127.0.0.1", "169.254.169.254", "10.0.0.8"):
    resolver = SafeUpstreamResolver(_StubResolver(rebind_ip))
    with pytest.raises(UpstreamTargetBlockedError):
      await resolver.resolve("rebind.attacker.example", 80)

  ok_resolver = SafeUpstreamResolver(_StubResolver("93.184.216.34"))
  res = await ok_resolver.resolve("cdn.example.com", 443)
  assert res[0]["host"] == "93.184.216.34"
  await ok_resolver.close()

  # 4. `open_upstream_url` rejects `302` redirects pointing at loopback/IMDS.
  redirect_target = "http://127.0.0.1:8080/api/config"

  def _mock_redirect(_req: web.Request) -> web.Response:
    return web.Response(status=302, headers={"Location": redirect_target})

  async with MockHttpServer(_mock_redirect) as mock, mock.new_session() as cli:
    for target in (
      "http://127.0.0.1:8080/api/config",
      "http://169.254.169.254/latest/meta-data",
    ):
      redirect_target = target
      with pytest.raises(UpstreamTargetBlockedError):
        async with open_upstream_url(
          cli, "http://upstream.example/live/u/p/1.ts"
        ) as _resp:
          pytest.fail("Expected SSRF redirect to be blocked")
