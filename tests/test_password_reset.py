"""End-to-end & unit tests for the Bellhop email password-reset flow."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web

from bellhop.common.accounts import UserAccount, verify_web_password
from bellhop.common.api_contracts import (
  ForgotPasswordResponseDict,
  ResetPasswordResponseDict,
  validate_typed_dict_payload,
)
from bellhop.common.http_utils import (
  create_web_app,
  match_public_base_url,
  normalize_public_base_url,
  normalize_public_base_urls,
  parse_origin_identity,
  start_site,
)
from bellhop.common.mailer import SentEmailResult
from bellhop.config import ConfigManager, load_config
from bellhop.serve import UnifiedServeServer
from bellhop.web import server as web_server_mod
from bellhop.web.auth import (
  GENERIC_PASSWORD_RESET_MESSAGE,
  PASSWORD_RESET_TTL_SEC,
  SESSION_COOKIE_NAME,
  WebAuthConfig,
  WebAuthManager,
  sanitize_next_url,
)
from bellhop.web.server import (
  RESET_ACCOUNT_COOLDOWN_SEC,
  RESET_ACCOUNT_WINDOW_MAX,
  RESET_ACCOUNT_WINDOW_SEC,
  RESET_REQUEST_IP_MAX,
  RESET_REQUEST_IP_WINDOW_SEC,
  WebGuideApp,
)
from bellhop.web.store import LineupGuideStore
from tests.conftest import (
  TEST_PASSWORD_HASH,
  TEST_WEB_PASSWORD,
  FakeClock,
  write_minimal_xtream_export,
)
from tests.http_mock import TestHttpClient

_RESET_LINK_RE = re.compile(
  r"https?://[^\s<>\"']+/reset-password\?token=[^\s<>\"']+"
)


@dataclass(slots=True, frozen=True)
class CapturedEmail:
  """Outbound message recorded by `DummyEmailSender`."""

  to_address: str
  subject: str
  text_body: str
  html_body: str | None


@dataclass(slots=True)
class DummyEmailSender:
  """In-memory fake `EmailSenderProtocol` used across password-reset tests."""

  sent: list[CapturedEmail] = field(default_factory=list)
  fail_with: Exception | None = None

  async def send(
    self,
    *,
    to_address: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
  ) -> SentEmailResult:
    if self.fail_with is not None:
      raise self.fail_with
    self.sent.append(
      CapturedEmail(
        to_address=to_address,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
      )
    )
    return SentEmailResult(
      message_id=f"<msg-{len(self.sent)}@dummy.invalid>",
      from_header="Bellhop <bellhop@example.com>",
      to_address=to_address,
      subject=subject,
      smtp_host="smtp.dummy.invalid",
      smtp_port=587,
      tls_mode="starttls",
    )


def _extract_reset_url_and_token(msg: CapturedEmail) -> tuple[str, str]:
  m = _RESET_LINK_RE.search(msg.text_body)
  assert m is not None, f"Reset URL missing in email body: {msg.text_body!r}"
  reset_url = m.group(0)
  parsed = urlsplit(reset_url)
  token_vals = parse_qs(parsed.query).get("token") or []
  assert len(token_vals) == 1 and token_vals[0]
  return reset_url, token_vals[0]


def _write_test_files(
  tmp_path: Path,
  *,
  email_enabled: bool = True,
  public_base_urls: Sequence[str] = (),
  trust_proxy_headers: bool = False,
  include_bob: bool = False,
) -> tuple[Path, Path, Path]:
  xt_dir = write_minimal_xtream_export(tmp_path / "xtream")
  creds_path = tmp_path / "credentials.yaml"
  bob_block = (
    f"""\
  bob:
    email: "bob@example.com"
    web_password_hash: "{TEST_PASSWORD_HASH}"
    xtream_username: "bobxt001"
    xtream_password: "bobpass1"
    upstream_username: "up_bob"
    upstream_password: "up_bob_pass"
"""
    if include_bob
    else ""
  )
  creds_path.write_text(
    f"""\
accounts:
  alice:
    email: "alice@example.com"
    web_password_hash: "{TEST_PASSWORD_HASH}"
    xtream_username: "alicext1"
    xtream_password: "alicep11"
    upstream_username: "up_alice"
    upstream_password: "up_alice_pass"
{bob_block}email:
  username: "bellhop@example.com"
  password: "app-password-secret"
""",
    encoding="utf-8",
  )
  cfg_path = tmp_path / "config.yaml"
  if public_base_urls:
    pub_block = "  public_base_urls:\n" + "\n".join(
      f'    - "{u}"' for u in public_base_urls
    )
  else:
    pub_block = "  public_base_urls: []"
  cfg_path.write_text(
    f"""\
credentials_file: "{creds_path}"
paths:
  cache_dir: "{tmp_path / "cache"}"
  xtream_output_dir: "{xt_dir}"
web_server:
  enabled: true
  host: "127.0.0.1"
  port: 0
{pub_block}
  trust_proxy_headers: {"true" if trust_proxy_headers else "false"}
  trusted_proxy_ips:
    - "127.0.0.1"
    - "::1"
upstream_xtream:
  server_url: "http://upstream.example"
  stream_format: "ts"
xtream_server:
  enabled: false
email:
  enabled: {"true" if email_enabled else "false"}
  smtp_host: "smtp.gmail.com"
  smtp_port: 587
  tls_mode: "starttls"
  from_address: "bellhop@example.com"
""",
    encoding="utf-8",
  )
  return cfg_path, creds_path, xt_dir


@asynccontextmanager
async def _run_reset_app(
  tmp_path: Path,
  *,
  clock: FakeClock,
  email_enabled: bool = True,
  sender: DummyEmailSender | None = None,
  public_base_urls: Sequence[str] = (),
  trust_proxy_headers: bool = False,
  include_bob: bool = False,
  password_reset_ttl_sec: int = PASSWORD_RESET_TTL_SEC,
  resetter_wrapper: Callable[
    [Callable[[str, str], UserAccount]], Callable[[str, str], UserAccount]
  ]
  | None = None,
) -> AsyncIterator[
  tuple[str, WebGuideApp, ConfigManager, DummyEmailSender, Path, Path]
]:
  cfg_path, creds_path, xt_dir = _write_test_files(
    tmp_path,
    email_enabled=email_enabled,
    public_base_urls=public_base_urls,
    trust_proxy_headers=trust_proxy_headers,
    include_bob=include_bob,
  )
  mgr = ConfigManager(cfg_path)
  cfg = mgr.get()
  cache_dir = cfg.resolve_path(cfg.cache_dir)
  auth = WebAuthManager(
    WebAuthConfig(
      accounts=cfg.accounts,
      accounts_provider=lambda: mgr.get().accounts,
      secret_file=cache_dir / "session_secret.key",
    ),
    clock=clock,
  )
  dummy = sender or DummyEmailSender()
  resetter: Callable[[str, str], UserAccount] = mgr.set_account_web_password
  if resetter_wrapper is not None:
    resetter = resetter_wrapper(resetter)

  web_app = WebGuideApp(
    LineupGuideStore(xt_dir),
    auth=auth,
    trust_proxy_headers=cfg.serve.web.trust_proxy_headers,
    trusted_proxy_ips=cfg.serve.web.trusted_proxy_ips,
    password_changer=mgr.change_account_web_password,
    password_resetter=resetter,
    email_sender=dummy if email_enabled else None,
    public_base_urls=cfg.serve.web.public_base_urls,
    public_base_urls_provider=lambda: mgr.get().serve.web.public_base_urls,
    server_protocol=cfg.serve.xtream.server_protocol,
    password_reset_ttl_sec=password_reset_ttl_sec,
    clock=clock,
  )
  app = create_web_app()
  web_app.add_routes(app)
  await web_app.startup()
  runner, port = await start_site(app, "127.0.0.1", 0)
  try:
    yield (
      f"http://127.0.0.1:{port}",
      web_app,
      mgr,
      dummy,
      cfg_path,
      cache_dir,
    )
  finally:
    await runner.cleanup()
    await web_app.shutdown()


def test_normalize_public_base_url_validation(tmp_path: Path) -> None:
  """Validate single/list origins, deduplication, and config schema rules."""
  assert (
    normalize_public_base_url("HTTPS://TV.Example.COM/")
    == "https://tv.example.com"
  )
  assert (
    normalize_public_base_url("http://192.168.1.50:8080")
    == "http://192.168.1.50:8080"
  )
  assert (
    normalize_public_base_url("https://[::1]:8443/") == "https://[::1]:8443"
  )
  assert normalize_public_base_urls(None) == ()
  assert normalize_public_base_urls(
    [
      "HTTPS://TV.Example.COM/",
      "http://192.168.1.50:8080",
      "https://tv.example.com",
    ]
  ) == ("https://tv.example.com", "http://192.168.1.50:8080")

  for bad in (
    "",
    "   ",
    "https://tv.example.com\n",
    "https://tv.example.com /path",
    "ftp://tv.example.com",
    "tv.example.com",
    "https://user:pass@tv.example.com",
    "https:///just-a-path",
    "https://tv.example.com/bellhop",
    "https://tv.example.com?foo=bar",
    "https://tv.example.com#frag",
  ):
    with pytest.raises(ValueError):
      normalize_public_base_url(bad)

  # Direct helper checks for IPv6 & scheme-insensitive reverse proxies.
  assert (
    match_public_base_url(
      ["https://tv.example.com", "http://[2001:db8::1]:8080"],
      host_header="[2001:DB8::1]:8080",
      is_https=False,
    )
    == "http://[2001:db8::1]:8080"
  )
  assert (
    match_public_base_url(
      ["https://tv.example.com"],
      host_header="tv.example.com",
      is_https=False,
    )
    == "https://tv.example.com"
  )
  assert match_public_base_url([], host_header="tv.example.com") is None
  assert parse_origin_identity("https://tv.example.com:99999") is None
  assert parse_origin_identity("https://[::1]:8443/reset") == (
    "https",
    "::1",
    8443,
  )

  # Config loader rejects singular key or malformed `public_base_urls` types.
  creds = tmp_path / "credentials.yaml"
  creds.write_text(
    f'accounts:\n  alice:\n    email: "alice@example.com"\n'
    f'    web_password_hash: "{TEST_PASSWORD_HASH}"\n'
    '    upstream_username: "up_alice"\n'
    '    upstream_password: "up_alice_pass"\n',
    encoding="utf-8",
  )
  for idx, bad_web in enumerate(
    (
      '  public_base_url: "https://tv.example.com"',
      '  public_base_urls: "https://tv.example.com"',
      "  public_base_urls:\n    - 123",
    )
  ):
    bad_cfg = tmp_path / f"bad_{idx}.yaml"
    bad_cfg.write_text(
      f'credentials_file: "{creds}"\nweb_server:\n{bad_web}\n',
      encoding="utf-8",
    )
    with pytest.raises(ValueError, match="public_base_urls"):
      load_config(bad_cfg)


def test_sanitize_next_url_blocks_reset_password_loops() -> None:
  """Redirecting `next` back to `/reset-password` normalizes to `/`."""
  assert sanitize_next_url("/reset-password?token=abc") == "/"
  assert sanitize_next_url("/reset-password/extra") == "/"


@pytest.mark.asyncio
async def test_password_reset_disabled_and_serve_wiring(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Hide Forgot Password UI & reject APIs when email is disabled or off."""
  async with (
    _run_reset_app(tmp_path, clock=fake_clock, email_enabled=False) as (
      base_url,
      _web_app,
      _mgr,
      dummy,
      _cfg_path,
      _cache,
    ),
    TestHttpClient(base_url=base_url) as client,
  ):
    login_page = await client.get("/login")
    assert login_page.status_code == 200
    assert "forgot-password-link" not in login_page.text
    assert "forgot-password-form" not in login_page.text

    forgot_r = await client.post(
      "/api/auth/forgot-password", json={"email": "alice@example.com"}
    )
    assert forgot_r.status_code == 404

    reset_r = await client.post(
      "/api/auth/reset-password",
      json={"token": "fake", "new_password": "new_valid_password"},
    )
    assert reset_r.status_code == 404

    page_r = await client.get("/reset-password?token=fake")
    assert page_r.status_code == 400
    assert "invalid or has expired" in page_r.text
    assert dummy.sent == []

  # Verify `UnifiedServeServer.from_config` hot-reloads email enabled state
  # and validates explicit `public_base_urls` wiring without restarting.
  cfg_dir = tmp_path / "serve_wired"
  cfg_path, _creds_path, _xt_dir = _write_test_files(
    cfg_dir,
    email_enabled=False,
    public_base_urls=("https://tv.example.com", "http://192.168.1.50:8080"),
  )
  cfg = load_config(cfg_path)
  mgr = ConfigManager(cfg_path, initial_config=cfg)

  def _is_reset_enabled(app: WebGuideApp) -> bool:
    return app.password_reset_enabled

  srv = UnifiedServeServer.from_config(cfg, config_manager=mgr)
  assert srv.web_app is not None
  web_app_wired = srv.web_app
  assert _is_reset_enabled(web_app_wired) is False

  # Enable `email` in config.yaml and bump mtime past fs resolution.
  text = cfg_path.read_text(encoding="utf-8").replace(
    "enabled: false\n  smtp_host:", "enabled: true\n  smtp_host:"
  )
  cfg_path.write_text(text, encoding="utf-8")
  future = time.time() + 2.0
  os.utime(cfg_path, (future, future))
  assert web_app_wired._active_email_sender() is not None
  assert _is_reset_enabled(web_app_wired) is True
  assert web_app_wired._public_base_urls_provider is not None
  assert web_app_wired._public_base_urls_provider() == (
    "https://tv.example.com",
    "http://192.168.1.50:8080",
  )


@pytest.mark.asyncio
async def test_forgot_password_zero_enumeration_and_mailer_error_isolation(
  tmp_path: Path,
  fake_clock: FakeClock,
  caplog: pytest.LogCaptureFixture,
) -> None:
  """Return identical `200` JSON across valid, unknown, & failing sends."""
  dummy = DummyEmailSender()
  async with _run_reset_app(
    tmp_path,
    clock=fake_clock,
    sender=dummy,
    include_bob=True,
    trust_proxy_headers=True,
  ) as (base_url, web_app, _mgr, _dummy, _cfg_path, _cache):
    client_ctx = TestHttpClient(base_url=base_url)
    async with client_ctx as client:
      # Verify HTML login page exposes Forgot Password affordance + form.
      login_page = await client.get("/login")
      assert login_page.status_code == 200
      assert "forgot-password-link" in login_page.text
      assert "forgot-password-form" in login_page.text

      r_valid = await client.post(
        "/api/auth/forgot-password",
        json={"email": "  ALICE@EXAMPLE.COM  "},
        headers={"X-Forwarded-For": "10.10.0.1"},
      )
      await web_app.wait_background_tasks()
      assert len(dummy.sent) == 1

      r_unknown = await client.post(
        "/api/auth/forgot-password",
        json={"email": "nobody@example.com"},
        headers={"X-Forwarded-For": "10.10.0.2"},
      )
      r_bad_email = await client.post(
        "/api/auth/forgot-password",
        json={"email": "not-an-email"},
        headers={"X-Forwarded-For": "10.10.0.3"},
      )
      r_empty = await client.post(
        "/api/auth/forgot-password",
        json={"email": ""},
        headers={"X-Forwarded-For": "10.10.0.4"},
      )
      # Cooldown-suppressed second request for Alice.
      r_cooldown = await client.post(
        "/api/auth/forgot-password",
        json={"email": "alice@example.com"},
        headers={"X-Forwarded-For": "10.10.0.5"},
      )

      # Simulated SMTP transport failure containing an embedded secret URL.
      dummy.fail_with = RuntimeError(
        "SMTP down https://user:topsecretpass@smtp.example/send"
      )
      with caplog.at_level(logging.WARNING, logger="bellhop.web"):
        r_mailer_fail = await client.post(
          "/api/auth/forgot-password",
          json={"email": "bob@example.com"},
          headers={"X-Forwarded-For": "10.10.0.6"},
        )
        await web_app.wait_background_tasks()

      for resp in (
        r_valid,
        r_unknown,
        r_bad_email,
        r_empty,
        r_cooldown,
        r_mailer_fail,
      ):
        assert resp.status_code == 200
        assert "Set-Cookie" not in resp.headers
        assert resp.headers.get("Cache-Control") == "no-store"
        body = resp.json()
        validate_typed_dict_payload(body, ForgotPasswordResponseDict)
        assert body == {
          "ok": True,
          "message": GENERIC_PASSWORD_RESET_MESSAGE,
        }
        assert resp.text == r_valid.text

      assert len(dummy.sent) == 1
      joined_logs = "\n".join(rec.getMessage() for rec in caplog.records)
      assert "topsecretpass" not in joined_logs
      assert "bob@example.com" not in joined_logs


@pytest.mark.asyncio
async def test_host_header_and_xfh_poisoning_immunity(
  tmp_path: Path, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
  """Ignore attacker `Host` & `X-Forwarded-Host` when crafting reset links."""
  # Case A: Empty `public_base_urls` -> strictly uses local bound socket.
  dir_a = tmp_path / "socket_fallback"
  async with _run_reset_app(dir_a, clock=fake_clock, public_base_urls=()) as (
    base_url,
    web_app,
    _mgr,
    dummy,
    _cfg,
    _cache,
  ):
    port = urlsplit(base_url).port
    async with TestHttpClient(base_url=base_url) as client:
      resp = await client.post(
        "/api/auth/forgot-password",
        json={"email": "alice@example.com"},
        headers={
          "Host": "attacker.evil.example:8443",
          "X-Forwarded-Host": "phishing.evil.example",
          "Forwarded": "host=phishing.evil.example",
        },
      )
      assert resp.status_code == 200
      await web_app.wait_background_tasks()
    assert len(dummy.sent) == 1
    reset_url, _tok = _extract_reset_url_and_token(dummy.sent[0])
    assert reset_url.startswith(f"http://127.0.0.1:{port}/reset-password?")
    assert "attacker.evil.example" not in reset_url
    assert "phishing.evil.example" not in reset_url

    # Unit check `0.0.0.0` wildcard socket fallback -> `detect_device_ip()`.
    class _FakeTransport:
      def get_extra_info(self, key: str) -> object:
        return ("0.0.0.0", 8080) if key == "sockname" else None

    class _FakeReq:
      transport = _FakeTransport()

    monkeypatch.setattr(
      web_server_mod, "detect_device_ip", lambda: "192.168.50.10"
    )
    fallback_origin = web_app._resolve_reset_base_url(
      cast(web.Request, _FakeReq())
    )
    assert fallback_origin == "http://192.168.50.10:8080"


@pytest.mark.asyncio
async def test_multi_host_allowlist_selects_matching_origin_and_blocks_poisoning(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Select matching multi-host URL & reject spoofed/unlisted origins."""
  allowed_urls = [
    "https://tv.yourdomain.com",
    "https://tv.internal.lan:8443",
    "http://192.168.1.50:8080",
  ]
  async with (
    _run_reset_app(
      tmp_path,
      clock=fake_clock,
      public_base_urls=allowed_urls,
      trust_proxy_headers=True,
    ) as (base_url, web_app, _mgr, dummy, _cfg, _cache),
    TestHttpClient(base_url=base_url) as client,
  ):
    port = urlsplit(base_url).port

    # 1. Internal LAN request selects `http://192.168.1.50:8080`.
    r_lan = await client.post(
      "/api/auth/forgot-password",
      json={"email": "alice@example.com"},
      headers={
        "Host": "192.168.1.50:8080",
        "Origin": "http://192.168.1.50:8080",
        "X-Forwarded-For": "10.1.0.1",
      },
    )
    assert r_lan.status_code == 200
    await web_app.wait_background_tasks()
    url_lan, _ = _extract_reset_url_and_token(dummy.sent[-1])
    assert url_lan.startswith("http://192.168.1.50:8080/reset-password?")

    # 2. Internal split-DNS HTTPS on custom port selects `:8443`.
    fake_clock.advance(RESET_ACCOUNT_COOLDOWN_SEC + 1.0)
    r_int = await client.post(
      "/api/auth/forgot-password",
      json={"email": "alice@example.com"},
      headers={
        "Host": "tv.internal.lan:8443",
        "Origin": "https://tv.internal.lan:8443",
        "X-Forwarded-For": "10.1.0.2",
      },
    )
    assert r_int.status_code == 200
    await web_app.wait_background_tasks()
    url_int, _ = _extract_reset_url_and_token(dummy.sent[-1])
    assert url_int.startswith("https://tv.internal.lan:8443/reset-password?")

    # 3. Reverse proxy rewriting `Host` to backend loopback while passing
    # allowlisted `Origin` passes CSRF and selects `https://tv.yourdomain.com`.
    fake_clock.advance(RESET_ACCOUNT_COOLDOWN_SEC + 1.0)
    r_proxy_origin = await client.post(
      "/api/auth/forgot-password",
      json={"email": "alice@example.com"},
      headers={
        "Host": f"127.0.0.1:{port}",
        "Origin": "https://tv.yourdomain.com",
        "X-Forwarded-For": "10.1.0.3",
      },
    )
    assert r_proxy_origin.status_code == 200
    await web_app.wait_background_tasks()
    url_ext, _ = _extract_reset_url_and_token(dummy.sent[-1])
    assert url_ext.startswith("https://tv.yourdomain.com/reset-password?")

    # Reset 15m per-account send budget before testing remaining paths.
    fake_clock.advance(RESET_ACCOUNT_WINDOW_SEC + 1.0)

    # 4. Trusted proxy forwarding `X-Forwarded-Host` (with Referer allowlist).
    r_xfh = await client.post(
      "/api/auth/forgot-password",
      json={"email": "alice@example.com"},
      headers={
        "Host": f"127.0.0.1:{port}",
        "Referer": "https://tv.yourdomain.com/login",
        "X-Forwarded-Host": "tv.yourdomain.com, proxy.internal",
        "X-Forwarded-For": "10.1.0.4",
      },
    )
    assert r_xfh.status_code == 200
    await web_app.wait_background_tasks()
    url_xfh, _ = _extract_reset_url_and_token(dummy.sent[-1])
    assert url_xfh.startswith("https://tv.yourdomain.com/reset-password?")

    # 5. Spoofed attacker Host/XFH falls back cleanly to primary entry `[0]`.
    fake_clock.advance(RESET_ACCOUNT_COOLDOWN_SEC + 1.0)
    r_spoof = await client.post(
      "/api/auth/forgot-password",
      json={"email": "alice@example.com"},
      headers={
        "Host": "attacker.evil.example",
        "X-Forwarded-Host": "phishing.evil.example",
        "X-Forwarded-For": "10.1.0.5",
      },
    )
    assert r_spoof.status_code == 200
    await web_app.wait_background_tasks()
    url_spoof, _ = _extract_reset_url_and_token(dummy.sent[-1])
    assert url_spoof.startswith("https://tv.yourdomain.com/reset-password?")
    assert "attacker.evil.example" not in url_spoof

    # 6. Known hostname with unlisted port falls back to primary `[0]`.
    fake_clock.advance(RESET_ACCOUNT_COOLDOWN_SEC + 1.0)
    r_bad_port = await client.post(
      "/api/auth/forgot-password",
      json={"email": "alice@example.com"},
      headers={
        "Host": "tv.yourdomain.com:9999",
        "X-Forwarded-For": "10.1.0.6",
      },
    )
    assert r_bad_port.status_code == 200
    await web_app.wait_background_tasks()
    url_bad_port, _ = _extract_reset_url_and_token(dummy.sent[-1])
    assert url_bad_port.startswith("https://tv.yourdomain.com/reset-password?")
    assert dummy.sent[-1].html_body is not None
    assert "https://tv.yourdomain.com/reset-password?token=" in (
      dummy.sent[-1].html_body
    )

    # 7. Spoofed cross-site Origin/Referer still blocked with 403.
    r_evil_origin = await client.post(
      "/api/auth/forgot-password",
      json={"email": "alice@example.com"},
      headers={"Origin": "https://attacker.evil.example"},
    )
    assert r_evil_origin.status_code == 403
    r_evil_ref = await client.post(
      "/api/auth/forgot-password",
      json={"email": "alice@example.com"},
      headers={"Referer": "https://attacker.evil.example/phish"},
    )
    assert r_evil_ref.status_code == 403


@pytest.mark.asyncio
async def test_click_only_initiation_and_scanner_safe_get(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Email request and link-preview GETs leave password intact & token live."""
  async with (
    _run_reset_app(tmp_path, clock=fake_clock) as (
      base_url,
      web_app,
      _mgr,
      dummy,
      _cfg,
      _cache,
    ),
    TestHttpClient(base_url=base_url) as client,
  ):
    r = await client.post(
      "/api/auth/forgot-password", json={"email": "alice@example.com"}
    )
    assert r.status_code == 200
    await web_app.wait_background_tasks()
    assert len(dummy.sent) == 1
    _url, token = _extract_reset_url_and_token(dummy.sent[0])

    # Email security scanners prefetching `GET /reset-password` multiple times
    # do NOT consume the token or lock out the existing password.
    for _ in range(3):
      get_r = await client.get("/reset-password", params={"token": token})
      assert get_r.status_code == 200
      assert get_r.headers.get("Referrer-Policy") == "no-referrer"
      assert 'id="reset-password-form"' in get_r.text
      assert 'id="reset-username"' in get_r.text
      assert "alice@example.com" in get_r.text

    # Existing password still works before submitting the reset form.
    login_r = await client.post(
      "/api/auth/login",
      json={"email": "alice@example.com", "password": TEST_WEB_PASSWORD},
    )
    assert login_r.status_code == 200

    # Token still works to complete password reset.
    async with TestHttpClient(base_url=base_url) as reset_client:
      reset_r = await reset_client.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": "new_alice_pass_99"},
      )
      assert reset_r.status_code == 200
      validate_typed_dict_payload(reset_r.json(), ResetPasswordResponseDict)


@pytest.mark.asyncio
async def test_invalid_new_password_retry_does_not_burn_token(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Weak/oversized password returns 400 without consuming reset token."""
  async with (
    _run_reset_app(tmp_path, clock=fake_clock) as (
      base_url,
      web_app,
      _mgr,
      dummy,
      _cfg,
      _cache,
    ),
    TestHttpClient(base_url=base_url) as client,
  ):
    await client.post(
      "/api/auth/forgot-password", json={"email": "alice@example.com"}
    )
    await web_app.wait_background_tasks()
    _url, token = _extract_reset_url_and_token(dummy.sent[0])

    for bad_pw in ("short", "        ", "x" * 257):
      bad_r = await client.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": bad_pw},
      )
      assert bad_r.status_code == 400

    # Same token remains valid and succeeds on valid password.
    ok_r = await client.post(
      "/api/auth/reset-password",
      json={"token": token, "new_password": "valid_new_password"},
    )
    assert ok_r.status_code == 200


@pytest.mark.asyncio
async def test_reset_invalidates_sibling_tokens_replay_and_other_sessions(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Resetting consumes token, kills sibling tokens & sessions, auto-logs in."""
  async with (
    _run_reset_app(tmp_path, clock=fake_clock) as (
      base_url,
      web_app,
      mgr,
      dummy,
      _cfg_path,
      cache_dir,
    ),
    TestHttpClient(base_url=base_url, follow_redirects=False) as browser_a,
    TestHttpClient(base_url=base_url, follow_redirects=False) as browser_b,
    TestHttpClient(base_url=base_url) as verifier,
  ):
    # Browser A establishes an active session.
    login_a = await browser_a.post(
      "/api/auth/login",
      json={"email": "alice@example.com", "password": TEST_WEB_PASSWORD},
    )
    assert login_a.status_code == 200
    assert (await browser_a.get("/api/account")).status_code == 200

    # Request token #1 at t=0, then token #2 at t=+61s.
    await verifier.post(
      "/api/auth/forgot-password", json={"email": "alice@example.com"}
    )
    await web_app.wait_background_tasks()
    fake_clock.advance(RESET_ACCOUNT_COOLDOWN_SEC + 1.0)
    await verifier.post(
      "/api/auth/forgot-password", json={"email": "alice@example.com"}
    )
    await web_app.wait_background_tasks()
    assert len(dummy.sent) == 2
    _u1, tok1 = _extract_reset_url_and_token(dummy.sent[0])
    _u2, tok2 = _extract_reset_url_and_token(dummy.sent[1])

    # Browser B completes reset using token #2.
    new_pw = "brand_new_alice_pass"
    res_b = await browser_b.post(
      "/api/auth/reset-password",
      json={"token": tok2, "new_password": new_pw},
    )
    assert res_b.status_code == 200
    payload = res_b.json()
    validate_typed_dict_payload(payload, ResetPasswordResponseDict)
    assert payload["ok"] is True
    assert payload["authenticated"] is True
    assert payload["account_id"] == "alice"
    assert payload["email"] == "alice@example.com"
    set_cookie = res_b.headers.get("Set-Cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie

    # Browser B is immediately logged in.
    st_b = await browser_b.get("/api/auth/status")
    assert st_b.status_code == 200
    assert st_b.json()["authenticated"] is True
    assert (await browser_b.get("/api/account")).status_code == 200

    # Browser A's previous session was invalidated by `pv` rotation.
    acct_a = await browser_a.get("/api/account")
    assert acct_a.status_code == 401
    idx_a = await browser_a.get("/")
    assert idx_a.status_code == 302
    assert idx_a.headers.get("Location") == "/login"

    # Disk credentials reflect new password; old password is rejected.
    reloaded_acct = mgr.get().account_by_id("alice")
    assert reloaded_acct is not None
    assert verify_web_password(new_pw, reloaded_acct.web_password_hash)
    assert (
      await verifier.post(
        "/api/auth/login",
        json={"email": "alice@example.com", "password": TEST_WEB_PASSWORD},
      )
    ).status_code == 401
    assert (
      await verifier.post(
        "/api/auth/login",
        json={"email": "alice@example.com", "password": new_pw},
      )
    ).status_code == 200

    # Neither consumed token #2 nor sibling unclicked token #1 works again.
    for stale_tok in (tok2, tok1):
      assert (
        await verifier.get("/reset-password", params={"token": stale_tok})
      ).status_code == 400
      assert (
        await verifier.post(
          "/api/auth/reset-password",
          json={"token": stale_tok, "new_password": "another_pass_999"},
        )
      ).status_code == 400

    # Persisted `session_revoked.json` still blocks `tok2` across a restart.
    fresh_auth = WebAuthManager(
      WebAuthConfig(
        accounts=mgr.get().accounts,
        secret_file=cache_dir / "session_secret.key",
      ),
      clock=fake_clock,
    )
    assert fresh_auth.verify_password_reset_token(tok2) is None
    assert fresh_auth.verify_password_reset_token(tok1) is None


@pytest.mark.asyncio
async def test_reset_token_ttl_expiry(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Tokens expire strictly at `PASSWORD_RESET_TTL_SEC` (900 seconds)."""
  assert PASSWORD_RESET_TTL_SEC == 900
  async with (
    _run_reset_app(tmp_path, clock=fake_clock) as (
      base_url,
      web_app,
      _mgr,
      dummy,
      _cfg,
      _cache,
    ),
    TestHttpClient(base_url=base_url) as client,
  ):
    await client.post(
      "/api/auth/forgot-password", json={"email": "alice@example.com"}
    )
    await web_app.wait_background_tasks()
    _url, tok = _extract_reset_url_and_token(dummy.sent[0])

    fake_clock.advance(PASSWORD_RESET_TTL_SEC - 1)
    assert (
      await client.get("/reset-password", params={"token": tok})
    ).status_code == 200

    fake_clock.advance(1.0)
    assert (
      await client.get("/reset-password", params={"token": tok})
    ).status_code == 400
    assert (
      await client.post(
        "/api/auth/reset-password",
        json={"token": tok, "new_password": "expired_attempt_pw"},
      )
    ).status_code == 400


@pytest.mark.asyncio
async def test_token_domain_separation_blocks_session_and_reset_swap(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Session cookies cannot reset passwords; reset tokens cannot login."""
  async with _run_reset_app(tmp_path, clock=fake_clock) as (
    base_url,
    web_app,
    mgr,
    dummy,
    _cfg,
    _cache,
  ):
    alice = mgr.get().account_by_id("alice")
    assert alice is not None
    session_tok, _ = web_app.auth.create_session_token(alice)

    async with TestHttpClient(base_url=base_url) as client:
      # Session token cannot open `/reset-password` or reset password.
      assert (
        await client.get("/reset-password", params={"token": session_tok})
      ).status_code == 400
      assert (
        await client.post(
          "/api/auth/reset-password",
          json={"token": session_tok, "new_password": "new_alice_pass_99"},
        )
      ).status_code == 400

      # Reset token cannot authenticate as `bellhop_session` cookie.
      await client.post(
        "/api/auth/forgot-password", json={"email": "alice@example.com"}
      )
      await web_app.wait_background_tasks()
      _url, reset_tok = _extract_reset_url_and_token(dummy.sent[0])
      client.cookies.set(SESSION_COOKIE_NAME, reset_tok)
      assert (await client.get("/api/account")).status_code == 401
      st_r = await client.get("/api/auth/status")
      assert st_r.status_code == 200
      assert st_r.json()["authenticated"] is False


@pytest.mark.asyncio
async def test_dual_rate_limiting_account_cooldown_and_ip_throttles(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Enforce silent per-account cooldown/cap and per-IP `429` limits."""
  assert RESET_REQUEST_IP_MAX == 5
  assert RESET_REQUEST_IP_WINDOW_SEC == 300.0
  assert RESET_ACCOUNT_COOLDOWN_SEC == 60.0
  assert RESET_ACCOUNT_WINDOW_MAX == 3
  assert RESET_ACCOUNT_WINDOW_SEC == 900.0

  async with (
    _run_reset_app(tmp_path, clock=fake_clock, trust_proxy_headers=True) as (
      base_url,
      web_app,
      _mgr,
      dummy,
      _cfg,
      _cache,
    ),
    TestHttpClient(base_url=base_url) as client,
  ):
    # 1. Per-account cooldown (60s gap, 3 sends max in 900s) via distinct IPs.
    async def _forgot_from(ip: str, email: str = "alice@example.com") -> int:
      r = await client.post(
        "/api/auth/forgot-password",
        json={"email": email},
        headers={"X-Forwarded-For": ip},
      )
      await web_app.wait_background_tasks()
      return r.status_code

    assert await _forgot_from("198.51.100.1") == 200
    assert len(dummy.sent) == 1

    # 30s later -> suppressed silently (generic 200, no 2nd email).
    fake_clock.advance(30.0)
    assert await _forgot_from("198.51.100.2") == 200
    assert len(dummy.sent) == 1

    # +30s (60s total since t=0) -> 2nd email sent.
    fake_clock.advance(30.0)
    assert await _forgot_from("198.51.100.3") == 200
    assert len(dummy.sent) == 2

    # +60s -> 3rd email sent (window max 3 reached).
    fake_clock.advance(60.0)
    assert await _forgot_from("198.51.100.4") == 200
    assert len(dummy.sent) == 3

    # +60s -> even after 60s cooldown, 15m cap blocks 4th email silently.
    fake_clock.advance(60.0)
    assert await _forgot_from("198.51.100.5") == 200
    assert len(dummy.sent) == 3

    # At t=+901s since first send, oldest entry expired -> 4th email sends.
    fake_clock.advance(721.0)
    assert await _forgot_from("198.51.100.6") == 200
    assert len(dummy.sent) == 4

    # 2. Per-IP `/api/auth/forgot-password` limit (5 in 300s, IPv6 `/64`).
    for idx in range(5):
      r = await client.post(
        "/api/auth/forgot-password",
        json={"email": f"nobody{idx}@example.com"},
        headers={"X-Forwarded-For": f"2001:db8:abcd:1::{idx + 10}"},
      )
      assert r.status_code == 200

    throttled = await client.post(
      "/api/auth/forgot-password",
      json={"email": "alice@example.com"},
      headers={"X-Forwarded-For": "2001:db8:abcd:1::99"},
    )
    assert throttled.status_code == 429
    assert int(throttled.headers["Retry-After"]) >= 1

    # Different IPv6 `/64` prefix is not throttled.
    other_net = await client.post(
      "/api/auth/forgot-password",
      json={"email": "nobody@example.com"},
      headers={"X-Forwarded-For": "2001:db8:abcd:2::1"},
    )
    assert other_net.status_code == 200

    # Advancing beyond 300s restores the throttled IPv6 `/64` prefix.
    fake_clock.advance(RESET_REQUEST_IP_WINDOW_SEC + 1.0)
    recovered = await client.post(
      "/api/auth/forgot-password",
      json={"email": "nobody@example.com"},
      headers={"X-Forwarded-For": "2001:db8:abcd:1::10"},
    )
    assert recovered.status_code == 200

    # 3. Per-IP `/api/auth/reset-password` limit (8 failures per 60s).
    ip_rst = "203.0.113.77"
    for _ in range(8):
      r_bad = await client.post(
        "/api/auth/reset-password",
        json={"token": "invalid.token", "new_password": "some_valid_pass"},
        headers={"X-Forwarded-For": ip_rst},
      )
      assert r_bad.status_code == 400

    r_429 = await client.post(
      "/api/auth/reset-password",
      json={"token": "invalid.token", "new_password": "some_valid_pass"},
      headers={"X-Forwarded-For": ip_rst},
    )
    assert r_429.status_code == 429
    assert int(r_429.headers["Retry-After"]) >= 1

    fake_clock.advance(61.0)
    _u_last, tok_last = _extract_reset_url_and_token(dummy.sent[-1])
    r_ok = await client.post(
      "/api/auth/reset-password",
      json={"token": tok_last, "new_password": "recovered_password_9"},
      headers={"X-Forwarded-For": ip_rst},
    )
    assert r_ok.status_code == 200


@pytest.mark.asyncio
async def test_csrf_content_type_and_concurrent_replay_guard(
  tmp_path: Path, fake_clock: FakeClock
) -> None:
  """Block CSRF/bad JSON/bad Content-Type & reject concurrent same-JTI race."""

  def _slow_wrapper(
    inner: Callable[[str, str], UserAccount],
  ) -> Callable[[str, str], UserAccount]:
    def _wrapped(account_id: str, new_password: str) -> UserAccount:
      time.sleep(0.06)
      return inner(account_id, new_password)

    return _wrapped

  async with (
    _run_reset_app(
      tmp_path, clock=fake_clock, resetter_wrapper=_slow_wrapper
    ) as (base_url, web_app, _mgr, dummy, _cfg, _cache),
    TestHttpClient(base_url=base_url) as client,
  ):
    # Cross-site mutation protections.
    for path in ("/api/auth/forgot-password", "/api/auth/reset-password"):
      r_origin = await client.post(
        path,
        json={"email": "alice@example.com"},
        headers={"Origin": "https://evil.example"},
      )
      assert r_origin.status_code == 403
      r_sfs = await client.post(
        path,
        json={"email": "alice@example.com"},
        headers={"Sec-Fetch-Site": "cross-site"},
      )
      assert r_sfs.status_code == 403

    # Content-Type & JSON validation checks.
    assert (
      await client.post(
        "/api/auth/forgot-password", data="email=alice@example.com"
      )
    ).status_code == 415
    assert (
      await client.post(
        "/api/auth/reset-password", data="token=a&new_password=b"
      )
    ).status_code == 415
    assert (
      await client.post(
        "/api/auth/forgot-password",
        data="{not-json",
        headers={"Content-Type": "application/json"},
      )
    ).status_code == 400
    assert (
      await client.post("/api/auth/forgot-password", json=[1, 2])
    ).status_code == 400
    assert (
      await client.post("/api/auth/forgot-password", json={"email": 123})
    ).status_code == 400
    assert (
      await client.post(
        "/api/auth/reset-password",
        json={"token": 123, "new_password": "valid_password"},
      )
    ).status_code == 400

    # Concurrent race against the exact same reset token: only one wins.
    await client.post(
      "/api/auth/forgot-password", json={"email": "alice@example.com"}
    )
    await web_app.wait_background_tasks()
    _url, token = _extract_reset_url_and_token(dummy.sent[0])

    async with (
      TestHttpClient(base_url=base_url) as c1,
      TestHttpClient(base_url=base_url) as c2,
    ):
      r1, r2 = await asyncio.gather(
        c1.post(
          "/api/auth/reset-password",
          json={"token": token, "new_password": "concurrent_pw_one"},
        ),
        c2.post(
          "/api/auth/reset-password",
          json={"token": token, "new_password": "concurrent_pw_two"},
        ),
      )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 400]
