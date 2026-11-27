"""Unit and HTTP hardening tests for auth cookies, rate limits, & XFF."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from yarl import URL

from bellhop.common.accounts import (
  STREAM_CREDENTIAL_ALPHABET,
  STREAM_CREDENTIAL_LENGTH,
  hash_web_password,
)
from bellhop.common.http_utils import (
  DEFAULT_CLIENT_MAX_SIZE,
  build_access_urls,
  parse_trusted_proxy_networks,
  resolve_bind_host,
  resolve_client_ip,
  start_site,
)
from bellhop.config import ConfigManager, load_config
from bellhop.web.auth import (
  LoginRateLimiter,
  WebAuthConfig,
  WebAuthManager,
  render_login_page_html,
  sanitize_next_url,
)
from bellhop.web.server import WebGuideApp
from bellhop.web.store import LineupGuideStore
from tests.conftest import FakeClock, make_test_account
from tests.http_mock import new_test_session


def _write_minimal_bundle(xt_dir: Path) -> None:
  xt_dir.mkdir(parents=True, exist_ok=True)
  (xt_dir / "bellhop_xtream_playlist.json").write_text(
    json.dumps(
      {
        "lineup_info": {"lineup_id": "TEST", "provider": "Bellhop"},
        "server_info": {"timezone": "UTC"},
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
  """Verify internal-only redirects and quote escaping on `/login`."""
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
  """Verify HMAC sessions, `0600` secrets, Unicode passwords, & auth."""
  import stat

  # Unconfigured credentials MUST fail verification and MUST be rejected by
  # WebGuideApp
  unconfigured = WebAuthManager(WebAuthConfig())
  assert unconfigured.enabled is False
  assert unconfigured.has_credentials is False
  assert unconfigured.authenticate("", "") is None
  xt_empty = tmp_path / "empty_xtream"
  xt_empty.mkdir()
  with pytest.raises(ValueError, match="cannot be disabled"):
    WebGuideApp(LineupGuideStore(xt_empty), auth=unconfigured)

  secret_file = tmp_path / "cache" / "session_secret.key"
  acct_unicode = make_test_account(
    account_id="admin",
    email="admín@example.com",
    web_password_hash=hash_web_password("sëcret_web_password_🔐"),
  )
  auth1 = WebAuthManager(
    WebAuthConfig(
      accounts=(acct_unicode,),
      session_ttl_hours=2.0,
      secret_file=secret_file,
    ),
    clock=fake_clock,
  )
  assert auth1.enabled is True
  assert (
    auth1.authenticate("  Admín@Example.COM ", "sëcret_web_password_🔐")
    == acct_unicode
  )
  assert auth1.authenticate("admín@example.com", "wrong_é") is None

  token, session = auth1.create_session_token(acct_unicode)
  assert secret_file.is_file()
  assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
  assert session.expires_at == int(fake_clock.now) + 7200
  assert session.account_id == "admin"
  assert session.email == "admín@example.com"

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
      accounts=(acct_unicode,),
      session_ttl_hours=2.0,
      secret_file=secret_file,
    ),
    clock=fake_clock,
  )
  verified = auth2.verify_session_token(token)
  assert (
    verified is not None
    and verified.account_id == "admin"
    and verified.email == "admín@example.com"
  )

  # Deleting session_secret.key on disk immediately invalidates old tokens
  # and regenerates a fresh 0600 key on next session creation.
  secret_file.unlink()
  assert auth2.verify_session_token(token) is None
  rotated_tok, _ = auth2.create_session_token(acct_unicode)
  assert secret_file.is_file()
  assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
  assert auth2.verify_session_token(rotated_tok) is not None
  assert auth2.verify_session_token(token) is None

  # Password change invalidates existing cookies immediately
  acct_rotated = make_test_account(
    account_id="admin",
    email="admín@example.com",
    web_password_hash=hash_web_password("brand_new_password"),
  )
  auth_rotated = WebAuthManager(
    WebAuthConfig(
      accounts=(acct_rotated,),
      session_ttl_hours=2.0,
      secret_file=secret_file,
    ),
    clock=fake_clock,
  )
  assert auth_rotated.verify_session_token(rotated_tok) is None

  # Past full TTL (7201s total): original auth rejects expired token
  fake_clock.advance(3201.0)
  assert auth1.verify_session_token(token) is None
  assert auth2.verify_session_token(rotated_tok) is not None


@pytest.mark.asyncio
async def test_http_sliding_cookie_renewal_and_expiry_via_fake_clock(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Verify live HTTP sliding session renewal & expiry with zero sleeps."""
  xt_dir = tmp_path / "output" / "xtream"
  _write_minimal_bundle(xt_dir)
  acct = make_test_account(
    email="admin@example.com",
    web_password_hash=hash_web_password("secret"),
  )
  auth = WebAuthManager(
    WebAuthConfig(
      accounts=(acct,),
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
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "secret"},
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

  creds_path = tmp_path / "credentials.yaml"
  creds_path.write_text(
    f"""\
accounts:
  primary:
    email: "primary@example.com"
    web_password_hash: "{hash_web_password("secret_pass")}"
    xtream_username: "xt111111"
    xtream_password: "xt222222"
    upstream_username: "u1"
    upstream_password: "p1"
    pipeline: true
""",
    encoding="utf-8",
  )
  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"credentials_file: {creds_path}\nupstream_xtream:\n  server_url: http://example.invalid\n",
    encoding="utf-8",
  )
  assert load_config(cfg_path).serve.web_host == "0.0.0.0"


@pytest.mark.asyncio
async def test_http_hardening_413_403_cors_and_reverse_proxy_throttling(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Verify HTTP middleware limits (413/403/204/404) & XFF isolation."""
  xt_dir = tmp_path / "output" / "xtream"
  _write_minimal_bundle(xt_dir)
  auth = WebAuthManager(
    WebAuthConfig(
      accounts=(
        make_test_account(
          email="admin@example.com",
          web_password_hash=hash_web_password("correct_password"),
        ),
      ),
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
          json={"email": "admin@example.com", "password": "wrong"},
          headers={"X-Forwarded-For": f"{fake_prefix}, 203.0.113.10"},
        ) as bad_r:
          assert bad_r.status == 401

      async with client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "correct_password"},
        headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.10"},
      ) as blocked_r:
        assert blocked_r.status == 429
        assert int(blocked_r.headers["Retry-After"]) == 60

      # Separate client IP behind trusted proxy is unaffected
      async with client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "correct_password"},
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
        json={"email": "admin@example.com", "password": "correct_password"},
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
          json={"email": "admin@example.com", "password": "wrong"},
          headers={"X-Forwarded-For": spoof_ip},
        ) as bad_r2:
          assert bad_r2.status == 401
      async with client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "correct_password"},
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
  """Verify Web Guide CSRF blocks, forged HLS tokens, and CORS omission."""
  import base64

  xt_dir = tmp_path / "output" / "xtream"
  _write_minimal_bundle(xt_dir)
  acct = make_test_account(
    email="admin@example.com",
    web_password_hash=hash_web_password("secret"),
  )
  auth = WebAuthManager(
    WebAuthConfig(
      accounts=(acct,),
      secret_file=tmp_path / "session_secret.key",
    ),
    clock=fake_clock,
  )
  app = WebGuideApp(
    LineupGuideStore(xt_dir),
    auth=auth,
    password_changer=lambda _aid, _cur, _new: acct,
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
        json={"email": "admin@example.com", "password": "secret"},
        headers={"Origin": "https://attacker.example"},
      ) as csrf_login:
        assert csrf_login.status == 403

      # Legitimate login succeeds and never emits wildcard CORS
      async with client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "secret"},
      ) as ok_login:
        assert ok_login.status == 200
        assert "Access-Control-Allow-Origin" not in ok_login.headers

      # Removed /api/config endpoint returns 404
      async with client.get("/api/config") as old_cfg_r:
        assert old_cfg_r.status == 404

      async with client.get("/api/lineup") as lu_r:
        assert lu_r.status == 200
        assert "Access-Control-Allow-Origin" not in lu_r.headers

      # State-changing GET routes are disabled (rejected with 404/405, zero
      # state change)
      for get_mut in (
        "/api/auth/logout",
        "/api/epg/refresh",
        "/api/tuner/release",
        "/api/account/password",
        "/api/account/stream-credentials/regenerate",
      ):
        async with client.get(get_mut) as gm_r:
          assert gm_r.status in {404, 405}

      # Cross-site POST mutations blocked via Sec-Fetch-Site and Origin
      async with client.post(
        "/api/account/password",
        json={},
        headers={"Sec-Fetch-Site": "cross-site"},
      ) as csrf_post:
        assert csrf_post.status == 403

      # Non-JSON Content-Type on POST /api/account/password rejected with 415
      async with client.post(
        "/api/account/password",
        data="{}",
        headers={"Content-Type": "text/plain"},
      ) as plain_post:
        assert plain_post.status == 415

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
async def test_account_self_service_routes_and_removed_config_route(
  tmp_path: Path,
) -> None:
  """Verify `/api/account*` self-service flows, validation, and 503 mode."""
  xt_dir = tmp_path / "output" / "xtream"
  _write_minimal_bundle(xt_dir)
  creds_path = tmp_path / "credentials.yaml"
  creds_path.write_text(
    """\
accounts:
  primary:
    email: "primary@example.com"
    web_password: "initial_secret_pw"
    xtream_username: "initxt01"
    xtream_password: "initxtpw"
    upstream_username: "up_user"
    upstream_password: "up_secret_password"
    pipeline: true
""",
    encoding="utf-8",
  )
  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"""\
credentials_file: "{creds_path}"
paths:
  cache_dir: "{tmp_path / "cache"}"
  state_dir: "{tmp_path / "state"}"
  xtream_output_dir: "{xt_dir}"
web_server:
  port: 9090
xtream_server:
  public_host: "bellhop.lan"
upstream_xtream:
  server_url: "http://upstream.example"
""",
    encoding="utf-8",
  )
  mgr = ConfigManager(cfg_path)
  cfg = mgr.get()
  auth = WebAuthManager(
    WebAuthConfig(
      accounts_provider=lambda: mgr.get().accounts,
      secret_file=tmp_path / "cache" / "session_secret.key",
    )
  )

  # 1. Without handlers attached, account mutations fail cleanly with 501
  app_ro = WebGuideApp(LineupGuideStore(xt_dir), auth=auth, settle_sec=0.0)
  await app_ro.startup()
  runner_ro, port_ro = await start_site(app_ro.build_app(), "127.0.0.1", 0)
  try:
    async with new_test_session(f"http://127.0.0.1:{port_ro}") as cli_ro:
      async with cli_ro.post(
        "/api/auth/login",
        json={"email": "primary@example.com", "password": "initial_secret_pw"},
      ) as login_ro:
        assert login_ro.status == 200
      async with cli_ro.post(
        "/api/account/password",
        json={
          "current_password": "initial_secret_pw",
          "new_password": "new_strong_password_2",
        },
      ) as pw_501:
        assert pw_501.status == 501
      async with cli_ro.post(
        "/api/account/stream-credentials/regenerate",
        json={},
      ) as reg_501:
        assert reg_501.status == 501
  finally:
    await runner_ro.cleanup()
    await app_ro.shutdown()

  # 2. Full ConfigManager-backed self-service account lifecycle
  app = WebGuideApp(
    LineupGuideStore(xt_dir),
    auth=auth,
    upstream_server_url="http://upstream.example",
    player_base_url_provider=lambda _hdrs: (
      f"http://{cfg.serve.public_host or 'bellhop.lan'}:{cfg.serve.web_port}"
    ),
    password_changer=mgr.change_account_web_password,
    stream_creds_regenerator=mgr.regenerate_account_stream_credentials,
    settle_sec=0.0,
  )
  await app.startup()
  runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
  try:
    async with new_test_session(f"http://127.0.0.1:{port}") as client:
      async with client.post(
        "/api/auth/login",
        json={"email": "primary@example.com", "password": "initial_secret_pw"},
      ) as login_r:
        assert login_r.status == 200

      async with client.get("/api/config") as old_cfg:
        assert old_cfg.status == 404

      async with client.get("/api/account") as acct_r:
        assert acct_r.status == 200
        info = await acct_r.json()
        assert info["account_id"] == "primary"
        assert info["email"] == "primary@example.com"
        assert info["xtream_username"] == "initxt01"
        assert info["xtream_password"] == "initxtpw"
        assert "up_secret_password" not in json.dumps(info)
        assert (
          info["player_urls"]["xtream_server_url"] == "http://bellhop.lan:9090"
        )
        assert "initxt01" in info["player_urls"]["m3u_url"]
        assert "initxtpw" in info["player_urls"]["xmltv_url"]

      # Wrong current password -> 403
      async with client.post(
        "/api/account/password",
        json={
          "current_password": "wrong_current_pw",
          "new_password": "new_strong_password_2",
        },
      ) as wrong_pw:
        assert wrong_pw.status == 403

      # Too-short new password -> 400
      async with client.post(
        "/api/account/password",
        json={
          "current_password": "initial_secret_pw",
          "new_password": "short",
        },
      ) as short_pw:
        assert short_pw.status == 400

      # Valid password change rotates session cookie and persists scrypt hash
      async with client.post(
        "/api/account/password",
        json={
          "current_password": "initial_secret_pw",
          "new_password": "new_strong_password_2",
        },
      ) as ok_pw:
        assert ok_pw.status == 200
        pw_body = await ok_pw.json()
        assert pw_body["ok"] is True
        assert pw_body["email"] == "primary@example.com"
        assert "bellhop_session=" in ok_pw.headers.get("Set-Cookie", "")

      # Regenerate Bellhop Xtream credentials (8-char [a-z0-9])
      async with client.post(
        "/api/account/stream-credentials/regenerate",
        json={},
      ) as reg_r:
        assert reg_r.status == 200
        reg_body = await reg_r.json()
        new_u = reg_body["xtream_username"]
        new_p = reg_body["xtream_password"]
        assert new_u != "initxt01"
        assert new_p != "initxtpw"
        assert len(new_u) == STREAM_CREDENTIAL_LENGTH
        assert len(new_p) == STREAM_CREDENTIAL_LENGTH
        assert all(ch in STREAM_CREDENTIAL_ALPHABET for ch in new_u + new_p)
        assert new_u in reg_body["player_urls"]["m3u_url"]

    # Verify old password rejected and new password accepted on fresh client
    async with new_test_session(f"http://127.0.0.1:{port}") as fresh:
      async with fresh.post(
        "/api/auth/login",
        json={"email": "primary@example.com", "password": "initial_secret_pw"},
      ) as old_login:
        assert old_login.status == 401
      async with fresh.post(
        "/api/auth/login",
        json={
          "email": "primary@example.com",
          "password": "new_strong_password_2",
        },
      ) as new_login:
        assert new_login.status == 200
  finally:
    await runner.cleanup()
    await app.shutdown()


@pytest.mark.asyncio
async def test_session_revocation_on_logout_https_cookie_flag_and_url_redaction(
  tmp_path: Path,
) -> None:
  """Verify logout revocation, HTTPS `Secure` flag, & URL redaction."""
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
      accounts=(
        make_test_account(
          email="admin@example.com",
          web_password_hash=hash_web_password("secret"),
        ),
      ),
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
        json={"email": "admin@example.com", "password": "secret"},
      ) as r_http,
    ):
      assert r_http.status == 200
      set_cookie_http = r_http.headers.get("Set-Cookie", "")
      assert "Secure" not in set_cookie_http

    async with new_test_session(base_url) as client_https:
      async with client_https.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "secret"},
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

    # Verify IPv4-mapped IPv6 loopback (`::ffff:127.0.0.1`), SameSite/Referer,
    # and empty-Host CSRF hardening on `WebGuideApp` + `is_trusted_proxy_peer`
    from types import SimpleNamespace
    from typing import cast

    from aiohttp import web

    from bellhop.common.http_utils import is_trusted_proxy_peer

    assert is_trusted_proxy_peer(
      "::ffff:127.0.0.1",
      trust_proxy_headers=True,
      trusted_networks=parse_trusted_proxy_networks(["127.0.0.1"]),
    )
    mapped_req = cast(
      web.Request,
      SimpleNamespace(
        secure=False,
        remote="::ffff:127.0.0.1",
        headers={"X-Forwarded-Proto": "https"},
      ),
    )
    assert app._is_https_request(mapped_req) is True

    # Block same-site `Sec-Fetch-Site`, mismatched `Referer`, & empty `Host`
    async with new_test_session(base_url) as csrf_cli:
      async with csrf_cli.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "secret"},
        headers={"Sec-Fetch-Site": "same-site"},
      ) as r_samesite:
        assert r_samesite.status == 403
      async with csrf_cli.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "secret"},
        headers={"Referer": "https://evil.example/phish"},
      ) as r_referer:
        assert r_referer.status == 403
    no_host_req = cast(
      web.Request,
      SimpleNamespace(
        headers={"Origin": f"http://127.0.0.1:{port}", "Host": ""}
      ),
    )
    assert WebGuideApp._is_cross_site_mutation(no_host_req) is True
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
