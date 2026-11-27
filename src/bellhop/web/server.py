"""aiohttp web app and single-tuner relay for the Bellhop Web Guide."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html as _html
import json
import logging
import mimetypes
import re
import secrets
import time
from collections.abc import (
  Awaitable,
  Callable,
  Collection,
  Coroutine,
  Mapping,
  Sequence,
)
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import aiohttp
from aiohttp import web

from bellhop.common.accounts import (
  UserAccount,
  build_upstream_stream_url,
  validate_new_web_password,
)
from bellhop.common.api_contracts import (
  AccountInfoResponseDict,
  AuthStatusResponseDict,
  ChannelEpgResponseDict,
  EpgStatusDict,
  ForgotPasswordResponseDict,
  HealthApiResponseDict,
  LineupApiResponseDict,
  PasswordChangeResponseDict,
  PlayerUrlsDict,
  ResetPasswordResponseDict,
  TunerActiveInfoDict,
)
from bellhop.common.http_utils import (
  DEFAULT_TRUSTED_PROXY_IPS,
  UPSTREAM_ERRORS,
  ClientSessionFactory,
  IpNetwork,
  LoginRateLimiter,
  UpstreamTargetBlockedError,
  bytes_response,
  create_web_app,
  default_session_factory,
  detect_device_ip,
  extract_explicit_private_origins,
  format_advertised_base_url,
  is_trusted_proxy_peer,
  json_response,
  match_public_base_url,
  normalize_public_base_urls,
  open_upstream_url,
  parse_origin_identity,
  parse_trusted_proxy_networks,
  redact_url_secrets,
  redirect_response,
  resolve_request_client_ip,
  validate_upstream_url,
)
from bellhop.common.http_utils import (
  build_access_urls as _build_access_urls,
)
from bellhop.common.http_utils import (
  resolve_bind_host as _resolve_bind_host,
)
from bellhop.common.mailer import EmailSenderProtocol
from bellhop.epg.constants import EPG_RECENT_FLOOR_SEC
from bellhop.epg.refresh import (
  EpgRefreshService,
  EpgRefreshStatus,
)
from bellhop.web.auth import (
  GENERIC_PASSWORD_RESET_MESSAGE,
  PASSWORD_RESET_TTL_SEC,
  SESSION_COOKIE_NAME,
  SessionInfo,
  WebAuthManager,
  render_login_page_html,
  render_reset_password_page_html,
  sanitize_next_url,
)
from bellhop.web.store import (
  LineupGuideStore,
  ProgramItem,
  PublicChannelDict,
  WebChannel,
)

__all__ = [
  "ActiveTunerSession",
  "TunerActiveInfoDict",
  "TunerSessionManager",
  "WebGuideApp",
  "build_access_urls",
  "detect_device_ip",
  "resolve_bind_host",
  "rewrite_hls_manifest",
]

logger = logging.getLogger("bellhop.web")

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

UPSTREAM_USER_AGENT = "VLC/3.0.20 LibVLC/3.0.20"
HLS_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')
STATIC_DIR = Path(__file__).resolve().parent / "static"
HLS_ASSET_TTL_SEC = 300.0
HLS_MAX_ASSETS = 1024
HLS_SESSION_LEASE_SEC = 20.0
DEFAULT_WRITE_TIMEOUT_SEC = 10.0
_PREEMPTED_HLS_TTL_SEC = 120.0
RESET_REQUEST_IP_MAX = 5
RESET_REQUEST_IP_WINDOW_SEC = 300.0
RESET_ACCOUNT_COOLDOWN_SEC = 60.0
RESET_ACCOUNT_WINDOW_MAX = 3
RESET_ACCOUNT_WINDOW_SEC = 900.0
_MAX_ACCOUNT_RESET_TRACKED = 1024
_REQ_SESSION_KEY: web.RequestKey[SessionInfo] = web.RequestKey(
  "bellhop_session", SessionInfo
)
_REQ_ACCOUNT_KEY: web.RequestKey[UserAccount] = web.RequestKey(
  "bellhop_account", UserAccount
)
_REQ_COOKIE_ROTATED_KEY: web.RequestKey[bool] = web.RequestKey(
  "bellhop_cookie_rotated", bool
)


def _redact_upstream_text(text: str, *, secret: str = "") -> str:
  """Mask upstream credentials in logged error strings."""
  return redact_url_secrets(text, secret=secret)


def _to_hls_url(direct_source: str) -> str:
  if ".m3u8" in direct_source.lower():
    return direct_source
  if re.search(r"\.ts(?:\?|$)", direct_source, flags=re.IGNORECASE):
    return re.sub(
      r"\.ts(\?.*)?$", r".m3u8\1", direct_source, flags=re.IGNORECASE
    )
  return f"{direct_source.rstrip('/')}.m3u8"


def rewrite_hls_manifest(
  manifest_text: str,
  base_url: str,
  chno: int,
  *,
  register_asset: Callable[[int, str], str],
) -> str:
  """Rewrite relative and absolute URIs in an M3U8 manifest to tokens."""
  out_lines: list[str] = []

  def _proxy_uri(raw_uri: str) -> str | None:
    resolved = urljoin(base_url, raw_uri.strip())
    tok = register_asset(chno, resolved)
    if not tok:
      return None
    return f"/api/hls/{chno}/asset?u={tok}"

  for raw_line in manifest_text.splitlines():
    stripped = raw_line.strip()
    if not stripped:
      out_lines.append("")
      continue
    if stripped.startswith("#"):
      if 'URI="' in stripped:
        blocked = False

        def _replace_attr(m: re.Match[str]) -> str:
          nonlocal blocked
          proxied = _proxy_uri(m.group(1))
          if proxied is None:
            blocked = True
            return m.group(0)
          return f'URI="{proxied}"'

        rewritten_tag = HLS_URI_ATTR_RE.sub(_replace_attr, stripped)
        if blocked:
          continue
        out_lines.append(rewritten_tag)
      else:
        out_lines.append(stripped)
      continue
    proxied_line = _proxy_uri(stripped)
    if proxied_line is None:
      continue
    out_lines.append(proxied_line)

  return "\n".join(out_lines) + "\n"


@dataclass(slots=True)
class ActiveTunerSession:
  """Live-stream relay session currently holding the single upstream tuner."""

  session_id: int
  chno: int
  channel_name: str
  mode: str
  started_at: float
  client_id: str = ""
  tune_token: str = ""
  lease_expires_at: float | None = None
  cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
  closed_event: asyncio.Event = field(default_factory=asyncio.Event)
  task: asyncio.Task[Any] | None = None

  def is_expired(self, now: float) -> bool:
    return self.closed_event.is_set() or (
      self.lease_expires_at is not None and now >= self.lease_expires_at
    )


class TunerSessionManager:
  """Coordinate single-tuner handoffs for `max_connections=1` streams."""

  def __init__(
    self,
    settle_sec: float = 0.18,
    *,
    clock: Callable[[], float] = time.time,
  ) -> None:
    self.settle_sec = max(0.0, settle_sec)
    self._clock = clock
    self._lock = asyncio.Lock()
    self._seq = 0
    self._wait_ticket = 0
    self._pending_settle = False
    self._active: ActiveTunerSession | None = None
    self._preempted_hls_tunes: dict[tuple[str, int, str], float] = {}

  def _prune_preempted_locked(self, now: float) -> None:
    for key, exp in list(self._preempted_hls_tunes.items()):
      if exp <= now:
        self._preempted_hls_tunes.pop(key, None)

  @staticmethod
  def _cancelled_dummy(
    *, chno: int, channel_name: str, mode: str, client_id: str, now: float
  ) -> ActiveTunerSession:
    dummy = ActiveTunerSession(
      session_id=0,
      chno=chno,
      channel_name=channel_name,
      mode=mode,
      started_at=now,
      client_id=client_id,
    )
    dummy.cancel_event.set()
    dummy.closed_event.set()
    return dummy

  @property
  def active_info(self) -> TunerActiveInfoDict | None:
    s = self._active
    if s is None or s.is_expired(self._clock()):
      return None
    return {
      "session_id": s.session_id,
      "chno": s.chno,
      "channel_name": s.channel_name,
      "mode": s.mode,
      "uptime_sec": round(max(0.0, self._clock() - s.started_at), 1),
    }

  async def preempt_and_acquire(
    self,
    chno: int,
    channel_name: str,
    mode: str = "mpegts",
    task: asyncio.Task[Any] | None = None,
    client_id: str = "",
    *,
    tune_token: str = "",
    lease_ttl_sec: float | None = None,
    is_disconnected: Callable[[], bool] | None = None,
    on_preempt: Callable[[], None] | None = None,
  ) -> ActiveTunerSession:
    self._wait_ticket += 1
    my_ticket = self._wait_ticket
    async with self._lock:
      await self._close_active_locked()
      if on_preempt is not None:
        on_preempt()
      if my_ticket != self._wait_ticket or (
        is_disconnected is not None and is_disconnected()
      ):
        return self._cancelled_dummy(
          chno=chno,
          channel_name=channel_name,
          mode=mode,
          client_id=client_id,
          now=self._clock(),
        )
      if self._pending_settle and self.settle_sec > 0:
        self._pending_settle = False
        await asyncio.sleep(self.settle_sec)
        if my_ticket != self._wait_ticket or (
          is_disconnected is not None and is_disconnected()
        ):
          return self._cancelled_dummy(
            chno=chno,
            channel_name=channel_name,
            mode=mode,
            client_id=client_id,
            now=self._clock(),
          )
      self._pending_settle = False
      self._seq += 1
      now = self._clock()
      session = ActiveTunerSession(
        session_id=self._seq,
        chno=chno,
        channel_name=channel_name,
        mode=mode,
        started_at=now,
        client_id=client_id,
        tune_token=tune_token,
        lease_expires_at=(
          now + lease_ttl_sec if lease_ttl_sec is not None else None
        ),
        task=task,
      )
      self._active = session
      return session

  async def acquire_or_renew_hls(
    self,
    *,
    chno: int,
    channel_name: str,
    client_id: str = "",
    tune_token: str = "",
    lease_ttl_sec: float = HLS_SESSION_LEASE_SEC,
    task: asyncio.Task[Any] | None = None,
    is_disconnected: Callable[[], bool] | None = None,
    on_preempt: Callable[[int], None] | None = None,
  ) -> tuple[ActiveTunerSession | None, bool]:
    """Acquire or renew an HLS lease without preempting a newer viewer."""
    self._wait_ticket += 1
    my_ticket = self._wait_ticket
    async with self._lock:
      now = self._clock()
      self._prune_preempted_locked(now)
      if (
        tune_token
        and (client_id, chno, tune_token) in self._preempted_hls_tunes
      ):
        return None, False
      cur = self._active
      if (
        cur is not None
        and not cur.is_expired(now)
        and cur.mode == "hls"
        and cur.chno == chno
        and cur.client_id == client_id
        and cur.tune_token == tune_token
      ):
        cur.lease_expires_at = now + lease_ttl_sec
        cur.task = task
        return cur, False

      await self._close_active_locked()
      if on_preempt is not None:
        on_preempt(chno)
      if my_ticket != self._wait_ticket or (
        is_disconnected is not None and is_disconnected()
      ):
        return None, False
      if self._pending_settle and self.settle_sec > 0:
        self._pending_settle = False
        await asyncio.sleep(self.settle_sec)
        if my_ticket != self._wait_ticket or (
          is_disconnected is not None and is_disconnected()
        ):
          return None, False
      self._pending_settle = False
      self._seq += 1
      now = self._clock()
      session = ActiveTunerSession(
        session_id=self._seq,
        chno=chno,
        channel_name=channel_name,
        mode="hls",
        started_at=now,
        client_id=client_id,
        tune_token=tune_token,
        lease_expires_at=now + lease_ttl_sec,
        task=task,
      )
      self._active = session
      return session, True

  def touch_hls_lease(
    self, chno: int, *, lease_ttl_sec: float = HLS_SESSION_LEASE_SEC
  ) -> bool:
    """Extend the active HLS lease for `chno`; return `False` if preempted."""
    s = self._active
    now = self._clock()
    if s is None or s.is_expired(now):
      return True
    if s.mode != "hls" or s.chno != chno:
      return False
    s.lease_expires_at = now + lease_ttl_sec
    return True

  async def release_all(self, *, client_id: str | None = None) -> bool:
    async with self._lock:
      now = self._clock()
      if (
        client_id
        and self._active is not None
        and not self._active.is_expired(now)
        and self._active.client_id
        and self._active.client_id != client_id
      ):
        return False
      return await self._close_active_locked()

  async def mark_closed(self, session: ActiveTunerSession) -> None:
    session.closed_event.set()
    if self._active is session:
      self._active = None

  async def _close_active_locked(self) -> bool:
    now = self._clock()
    self._prune_preempted_locked(now)
    prev = self._active
    if prev is None or prev.closed_event.is_set():
      self._active = None
      return False
    if prev.lease_expires_at is not None and now >= prev.lease_expires_at:
      prev.closed_event.set()
      self._active = None
      return False

    if prev.mode == "hls" and prev.tune_token:
      self._preempted_hls_tunes[
        (prev.client_id, prev.chno, prev.tune_token)
      ] = now + _PREEMPTED_HLS_TTL_SEC
    prev.cancel_event.set()
    if prev.task is not None and not prev.task.done():
      prev.task.cancel()
    if prev.mode == "hls" and (prev.task is None or prev.task.done()):
      prev.closed_event.set()
    else:
      with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(prev.closed_event.wait(), timeout=1.5)
    self._active = None
    self._pending_settle = True
    return True


class WebGuideApp:
  """aiohttp application serving the Web Guide UI, EPG APIs, and stream relay.

  Routes are registered on an `aiohttp.web.Application` via `add_routes()`
  (or `build_app()` for a standalone app). Public routes (login page, auth
  API, brand/favicon/prefs assets) bypass authentication; every other route
  is wrapped by `_protected()`, which enforces the `bellhop_session` cookie
  when Web Guide login is configured and slides the session expiry.
  """

  def __init__(
    self,
    store: LineupGuideStore,
    *,
    auth: WebAuthManager,
    settle_sec: float = 0.18,
    session_factory: ClientSessionFactory | None = None,
    static_dir: Path = STATIC_DIR,
    epg_refresher: EpgRefreshService | None = None,
    login_limiter: LoginRateLimiter | None = None,
    trust_proxy_headers: bool = False,
    trusted_proxy_ips: Sequence[str] = DEFAULT_TRUSTED_PROXY_IPS,
    upstream_server_url: str = "",
    upstream_stream_format: str = "ts",
    upstream_config_provider: Callable[[], tuple[str, str]] | None = None,
    xtream_api_enabled: bool = True,
    player_base_url_provider: Callable[[Mapping[str, str]], str] | None = None,
    password_changer: Callable[[str, str, str], UserAccount] | None = None,
    password_resetter: Callable[[str, str], UserAccount] | None = None,
    stream_creds_regenerator: Callable[[str], UserAccount] | None = None,
    email_sender: EmailSenderProtocol | None = None,
    email_sender_provider: Callable[[], EmailSenderProtocol | None]
    | None = None,
    public_base_urls: Sequence[str] = (),
    public_base_urls_provider: Callable[[], Sequence[str]] | None = None,
    server_protocol: str = "http",
    password_reset_ttl_sec: int = PASSWORD_RESET_TTL_SEC,
    reset_request_limiter: LoginRateLimiter | None = None,
    clock: Callable[[], float] = time.time,
    write_timeout_sec: float = DEFAULT_WRITE_TIMEOUT_SEC,
  ) -> None:
    if not auth.has_credentials:
      raise ValueError(
        "WebGuideApp requires at least one configured account in credentials.yaml; "
        "Web Guide login cannot be disabled."
      )
    self.store = store
    self.static_dir = static_dir
    self._clock = clock
    self._settle_sec = max(0.0, float(settle_sec))
    self.write_timeout_sec = max(0.01, float(write_timeout_sec))
    self._tuners: dict[str, TunerSessionManager] = {}
    self.epg_refresher = epg_refresher
    self.auth: WebAuthManager = auth
    self.login_limiter: LoginRateLimiter = login_limiter or LoginRateLimiter(
      clock=clock
    )
    self.reset_request_limiter: LoginRateLimiter = (
      reset_request_limiter
      or LoginRateLimiter(
        max_failures=RESET_REQUEST_IP_MAX,
        window_sec=RESET_REQUEST_IP_WINDOW_SEC,
        clock=clock,
      )
    )
    self.trust_proxy_headers = bool(trust_proxy_headers)
    self._trusted_proxy_networks: tuple[IpNetwork, ...] = (
      parse_trusted_proxy_networks(trusted_proxy_ips)
    )
    self._upstream_server_url = upstream_server_url
    self._upstream_stream_format = upstream_stream_format or "ts"
    self._upstream_config_provider = upstream_config_provider
    self._xtream_api_enabled = bool(xtream_api_enabled)
    self._player_base_url_provider = player_base_url_provider
    self._password_changer = password_changer
    self._password_resetter = password_resetter
    self._stream_creds_regenerator = stream_creds_regenerator
    self._email_sender = email_sender
    self._email_sender_provider = email_sender_provider
    self._public_base_urls: tuple[str, ...] = normalize_public_base_urls(
      public_base_urls
    )
    self._public_base_urls_provider = public_base_urls_provider
    self._server_protocol = (
      "https" if str(server_protocol).strip().lower() == "https" else "http"
    )
    self._password_reset_ttl_sec = max(1, int(password_reset_ttl_sec))
    self._account_reset_sends: dict[str, list[float]] = {}
    self._inflight_reset_jtis: set[str] = set()
    self._bg_tasks: set[asyncio.Task[Any]] = set()
    self._session_factory: ClientSessionFactory = (
      session_factory or default_session_factory
    )
    self._client: aiohttp.ClientSession | None = None
    # Per-account HLS edge playlist URL cache: (account_id, chno) -> (url, ts)
    self._hls_edge_urls: dict[tuple[str, int], tuple[str, float]] = {}
    self._hls_owner_clients: dict[str, str] = {}
    self._hls_secret: bytes = secrets.token_bytes(32)
    # Opaque server-side HLS asset table:
    # token -> (account_id, chno, resolved_url, expires_at)
    self._hls_assets: dict[str, tuple[str, int, str, float]] = {}

  def tuner_for(self, account_id: str) -> TunerSessionManager:
    """Return the dedicated single-tuner coordinator for `account_id`."""
    tuner = self._tuners.get(account_id)
    if tuner is None:
      tuner = TunerSessionManager(
        settle_sec=self._settle_sec, clock=self._clock
      )
      self._tuners[account_id] = tuner
    return tuner

  def active_tuner_for(self, account_id: str) -> TunerActiveInfoDict | None:
    """Return active tuner status for `account_id`, or `None` when idle."""
    tuner = self._tuners.get(account_id)
    return tuner.active_info if tuner is not None else None

  # ---------------------------------------------------------------------------
  # Lifecycle
  # ---------------------------------------------------------------------------
  async def startup(self) -> None:
    """Create upstream HTTP client and start EPG refresher cleanly."""
    if self._client is None:
      self._client = self._session_factory(
        timeout=aiohttp.ClientTimeout(
          total=None, connect=8.0, sock_connect=8.0, sock_read=15.0
        ),
        headers={"User-Agent": UPSTREAM_USER_AGENT, "Icy-MetaData": "0"},
      )
    if self.epg_refresher is not None:
      await self.epg_refresher.start()

  async def wait_background_tasks(self) -> None:
    """Await any pending async tasks (e.g., outbound password-reset email)."""
    while self._bg_tasks:
      pending = list(self._bg_tasks)
      await asyncio.gather(*pending, return_exceptions=True)

  def _spawn_background_task(
    self, coro: Coroutine[Any, Any, Any]
  ) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro)
    self._bg_tasks.add(task)
    task.add_done_callback(self._bg_tasks.discard)
    return task

  async def shutdown(self) -> None:
    """Stop EPG refresher, drain tasks, release tuners, and close client."""
    await self.wait_background_tasks()
    if self.epg_refresher is not None:
      await self.epg_refresher.stop()
    for tuner in list(self._tuners.values()):
      await tuner.release_all()
    if self._client is not None:
      await self._client.close()
      self._client = None

  @property
  def client(self) -> aiohttp.ClientSession:
    assert self._client is not None, (
      "WebGuideApp.startup() must be called before serving."
    )
    return self._client

  def build_app(self) -> web.Application:
    """Return a standalone `aiohttp` application serving only the Web Guide."""
    app = create_web_app()
    self.add_routes(app)
    return app

  def add_routes(self, app: web.Application) -> None:
    """Register all Web Guide routes on `app`."""
    r = app.router
    protect = self._protected

    # Public brand, favicon, stylesheet & client-prefs/login assets (needed by
    # `/login`).
    for path, asset in (
      ("/favicon.ico", "favicon.ico"),
      ("/static/favicon.ico", "favicon.ico"),
      ("/favicon.svg", "bellhop-logo.svg"),
      ("/static/bellhop-logo.svg", "bellhop-logo.svg"),
      ("/static/styles.css", "styles.css"),
      ("/static/prefs.js", "prefs.js"),
      ("/static/login.js", "login.js"),
    ):
      r.add_get(path, self._public_asset_handler(asset))

    # Public authentication endpoints.
    r.add_get("/login", self._handle_login_page)
    r.add_get("/reset-password", self._handle_reset_password_page)
    r.add_get("/api/auth/status", self._handle_auth_status)
    r.add_post("/api/auth/login", self._handle_auth_login)
    r.add_post("/api/auth/logout", self._handle_auth_logout)
    r.add_post("/api/auth/forgot-password", self._handle_auth_forgot_password)
    r.add_post("/api/auth/reset-password", self._handle_auth_reset_password)

    # Authenticated UI, APIs & stream relay.
    r.add_get("/", protect(self._handle_index))
    r.add_get("/index.html", protect(self._handle_index))
    r.add_get("/static/{path:.+}", protect(self._handle_static))
    r.add_get("/api/account", protect(self._handle_account_get))
    r.add_post("/api/account/password", protect(self._handle_account_password))
    r.add_post(
      "/api/account/stream-credentials/regenerate",
      protect(self._handle_account_regenerate_creds),
    )
    r.add_get("/api/health", protect(self._handle_health))
    r.add_post("/api/epg/refresh", protect(self._handle_epg_refresh))
    r.add_post("/api/tuner/release", protect(self._handle_tuner_release))
    r.add_get("/api/lineup", protect(self._handle_lineup))
    r.add_get(r"/api/epg/{chno:\d+}", protect(self._handle_channel_epg))
    r.add_get(
      r"/api/stream/{chno:\d+}{ext:(\.ts)?}",
      protect(self._handle_mpegts_stream),
    )
    r.add_get(
      r"/api/hls/{chno:\d+}/index.m3u8", protect(self._handle_hls_index)
    )
    r.add_get(r"/api/hls/{chno:\d+}/asset", protect(self._handle_hls_asset))
    # Unknown `/api/*` paths: authenticate first, then JSON 404.
    r.add_route("*", "/api/{tail:.*}", protect(self._handle_api_not_found))

  # ---------------------------------------------------------------------------
  # Authentication & CSRF helpers
  # ---------------------------------------------------------------------------
  @staticmethod
  def _is_cross_site_mutation(
    request: web.Request, *, allowed_origins: Sequence[str] = ()
  ) -> bool:
    """Reject cross-site state-changing requests via fetch/origin headers.

    Validates `Sec-Fetch-Site`, `Origin`, and `Referer` against same-origin
    or the configured `public_base_urls` allowlist.
    """
    sec_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sec_site and sec_site not in {"same-origin", "none"}:
      return True
    allowed_ids = {
      ident
      for ident in (parse_origin_identity(u) for u in allowed_origins)
      if ident is not None
    }
    req_host = (request.headers.get("Host") or "").strip().lower()
    origin = (request.headers.get("Origin") or "").strip()
    if origin:
      if origin.lower() == "null":
        return True
      origin_host = urlsplit(origin).netloc.strip().lower()
      if origin_host and req_host and origin_host == req_host:
        return False
      origin_id = parse_origin_identity(origin)
      return not (origin_id is not None and origin_id in allowed_ids)
    referer = (request.headers.get("Referer") or "").strip()
    if referer:
      ref_host = urlsplit(referer).netloc.strip().lower()
      if ref_host and req_host and ref_host == req_host:
        return False
      ref_id = parse_origin_identity(referer)
      return not (ref_id is not None and ref_id in allowed_ids)
    return False

  def _reject_cross_site(self, request: web.Request) -> bool:
    return self._is_cross_site_mutation(
      request, allowed_origins=self._configured_public_base_urls()
    )

  def _is_https_request(self, request: web.Request) -> bool:
    """Return True when `request` used TLS or a trusted HTTPS proxy."""
    if request.secure:
      return True
    if not is_trusted_proxy_peer(
      request.remote,
      trust_proxy_headers=self.trust_proxy_headers,
      trusted_networks=self._trusted_proxy_networks,
    ):
      return False
    xf_proto = (
      (request.headers.get("X-Forwarded-Proto") or "")
      .split(",", 1)[0]
      .strip()
      .lower()
    )
    return xf_proto == "https"

  def session_for(
    self, request: web.Request, *, now_ts: int | None = None
  ) -> SessionInfo | None:
    """Extract and validate the signed `SessionInfo` on `request`."""
    return self._session_for(request, now_ts=now_ts)

  def _session_for(
    self, request: web.Request, *, now_ts: int | None = None
  ) -> SessionInfo | None:
    effective_now = int(self._clock()) if now_ts is None else now_ts
    return self.auth.extract_session_from_headers(
      {"cookie": request.headers.get("Cookie", "")},
      now_ts=effective_now,
    )

  @staticmethod
  def _require_request_context(
    request: web.Request,
  ) -> tuple[SessionInfo, UserAccount]:
    return request[_REQ_SESSION_KEY], request[_REQ_ACCOUNT_KEY]

  def _protected(self, handler: _Handler) -> _Handler:
    """Wrap `handler` with Web Guide session auth and sliding renewal."""

    async def _wrapped(request: web.Request) -> web.StreamResponse:
      now_ts = int(self._clock())
      session = self._session_for(request, now_ts=now_ts)
      account = (
        self.auth.account_by_id(session.account_id)
        if session is not None
        else None
      )
      if session is None or account is None:
        is_https = self._is_https_request(request)
        clear_hdr = (
          {"Set-Cookie": self.auth.build_clear_cookie_header(secure=is_https)}
          if f"{SESSION_COOKIE_NAME}=" in request.headers.get("Cookie", "")
          else None
        )
        if request.path in ("/", "/index.html"):
          return redirect_response(
            self.auth.login_redirect_location(request.raw_path),
            headers=clear_hdr,
          )
        return json_response(
          {"error": "Authentication required", "login_url": "/login"},
          status=HTTPStatus.UNAUTHORIZED,
          headers=clear_hdr,
        )

      if request.method in ("POST", "PUT", "DELETE", "PATCH") and (
        self._reject_cross_site(request)
      ):
        return json_response(
          {"error": "Cross-site state mutations are forbidden"},
          status=HTTPStatus.FORBIDDEN,
        )

      request[_REQ_SESSION_KEY] = session
      request[_REQ_ACCOUNT_KEY] = account
      response = await handler(request)
      if (
        not request.get(_REQ_COOKIE_ROTATED_KEY, False)
        and session.needs_renewal(now_ts=now_ts)
        and not response.prepared
      ):
        latest_acct = self.auth.account_by_id(session.account_id) or account
        self.auth.revoke_session_from_headers(
          request.headers, now_ts=now_ts, grace_sec=30
        )
        new_tok, _ = self.auth.create_session_token(latest_acct, now_ts=now_ts)
        response.headers.add(
          "Set-Cookie",
          self.auth.build_set_cookie_header(
            new_tok, secure=self._is_https_request(request)
          ),
        )
      return response

    return _wrapped

  # ---------------------------------------------------------------------------
  # Public & password-reset helpers/handlers
  # ---------------------------------------------------------------------------
  def _active_email_sender(self) -> EmailSenderProtocol | None:
    if self._email_sender_provider is not None:
      return self._email_sender_provider()
    return self._email_sender

  @property
  def password_reset_enabled(self) -> bool:
    """Return `True` when both password persistence and email sender exist."""
    return (
      self._password_resetter is not None
      and self._active_email_sender() is not None
    )

  def _configured_public_base_urls(self) -> tuple[str, ...]:
    raw = (
      self._public_base_urls_provider()
      if self._public_base_urls_provider is not None
      else self._public_base_urls
    )
    return normalize_public_base_urls(raw)

  def _resolve_reset_base_url(self, request: web.Request) -> str:
    allowed = self._configured_public_base_urls()
    if allowed:
      matched = match_public_base_url(
        allowed,
        origin_header=request.headers.get("Origin"),
        host_header=request.headers.get("Host"),
        forwarded_host_header=request.headers.get("X-Forwarded-Host"),
        trust_forwarded_host=is_trusted_proxy_peer(
          request.remote,
          trust_proxy_headers=self.trust_proxy_headers,
          trusted_networks=self._trusted_proxy_networks,
        ),
        is_https=self._is_https_request(request),
      )
      assert matched is not None
      return matched

    sock_host = ""
    sock_port: int | None = None
    transport = request.transport
    if transport is not None:
      sockname = transport.get_extra_info("sockname")
      if isinstance(sockname, tuple) and len(sockname) >= 2:
        sock_host = str(sockname[0]).strip("[]").split("%", 1)[0]
        try:
          sock_port = int(sockname[1])
        except (TypeError, ValueError):
          sock_port = None

    if not sock_host or sock_host in ("0.0.0.0", "::"):
      adv_host = detect_device_ip() or "127.0.0.1"
    else:
      adv_host = sock_host
    host_fmt = f"[{adv_host}]" if ":" in adv_host else adv_host
    proto = self._server_protocol
    default_port = 443 if proto == "https" else 80
    if sock_port is None:
      sock_port = 443 if proto == "https" else 8080
    port_suffix = "" if sock_port == default_port else f":{sock_port}"
    return f"{proto}://{host_fmt}{port_suffix}"

  def _allow_account_reset_send(self, account_id: str, now: float) -> bool:
    cutoff = now - RESET_ACCOUNT_WINDOW_SEC
    prev = self._account_reset_sends.get(account_id)
    fresh = [ts for ts in prev if ts > cutoff] if prev else []
    if fresh:
      self._account_reset_sends[account_id] = fresh
    else:
      self._account_reset_sends.pop(account_id, None)

    if fresh and (now - fresh[-1]) < RESET_ACCOUNT_COOLDOWN_SEC:
      return False
    if len(fresh) >= RESET_ACCOUNT_WINDOW_MAX:
      return False

    if (
      account_id not in self._account_reset_sends
      and len(self._account_reset_sends) >= _MAX_ACCOUNT_RESET_TRACKED
    ):
      for key in list(self._account_reset_sends):
        kept = [ts for ts in self._account_reset_sends[key] if ts > cutoff]
        if kept:
          self._account_reset_sends[key] = kept
        else:
          self._account_reset_sends.pop(key, None)
      if len(self._account_reset_sends) >= _MAX_ACCOUNT_RESET_TRACKED:
        oldest = next(iter(self._account_reset_sends))
        self._account_reset_sends.pop(oldest, None)

    fresh.append(now)
    self._account_reset_sends[account_id] = fresh
    return True

  async def _send_password_reset_email(
    self,
    *,
    sender: EmailSenderProtocol,
    to_address: str,
    reset_url: str,
    ttl_sec: int,
  ) -> None:
    if ttl_sec >= 60:
      mins = max(1, round(ttl_sec / 60))
      expiry_label = f"{mins} minute" if mins == 1 else f"{mins} minutes"
    else:
      expiry_label = (
        f"{ttl_sec} second" if ttl_sec == 1 else f"{ttl_sec} seconds"
      )
    text_body = (
      "You requested a password reset for your Bellhop account.\n\n"
      f"Open the link below within {expiry_label} to choose a new password:\n"
      f"{reset_url}\n\n"
      "If you did not request this, you can safely ignore this email — your "
      "current password will continue working unless you open the link and "
      "submit a new password.\n"
    )
    esc_url = _html.escape(reset_url, quote=True)
    html_body = (
      "<p>You requested a password reset for your Bellhop account.</p>"
      f'<p><a href="{esc_url}">Reset your Bellhop password</a> '
      f"(expires in {expiry_label}).</p>"
      "<p>If you did not request this, you can safely ignore this email "
      "&mdash; your current password will keep working unless you open the "
      "link and submit a new password.</p>"
    )
    try:
      await sender.send(
        to_address=to_address,
        subject="Bellhop password reset",
        text_body=text_body,
        html_body=html_body,
      )
    except Exception as exc:
      logger.warning(
        "Password reset email delivery failed: %s",
        redact_url_secrets(str(exc)),
      )

  @staticmethod
  async def _read_json_object(
    request: web.Request,
  ) -> tuple[dict[str, Any] | None, web.Response | None]:
    if "application/json" not in request.content_type.lower():
      return None, json_response(
        {"error": "Content-Type must be application/json."},
        status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
      )
    body = await request.read()
    try:
      payload = (
        json.loads(body.decode("utf-8", errors="replace")) if body else {}
      )
    except json.JSONDecodeError as exc:
      return None, json_response(
        {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST
      )
    if not isinstance(payload, dict):
      return None, json_response(
        {"error": "Request body must be a JSON object."},
        status=HTTPStatus.BAD_REQUEST,
      )
    return payload, None

  def _public_asset_handler(self, asset_name: str) -> _Handler:
    async def _handler(_request: web.Request) -> web.StreamResponse:
      return self._static_file_response(self.static_dir / asset_name)

    return _handler

  async def _handle_login_page(
    self, request: web.Request
  ) -> web.StreamResponse:
    next_url = sanitize_next_url(request.query.get("next", "/"))
    if self._session_for(request) is not None:
      return redirect_response(next_url)
    return bytes_response(
      render_login_page_html(
        next_url=next_url,
        password_reset_enabled=self.password_reset_enabled,
      ),
      "text/html; charset=utf-8",
    )

  async def _handle_reset_password_page(
    self, request: web.Request
  ) -> web.StreamResponse:
    no_ref = {"Referrer-Policy": "no-referrer"}
    token = request.query.get("token", "").strip()
    now_ts = int(self._clock())
    info = (
      self.auth.verify_password_reset_token(token, now_ts=now_ts)
      if self.password_reset_enabled and token
      else None
    )
    if info is None:
      return bytes_response(
        render_reset_password_page_html(
          error_message="This password reset link is invalid or has expired."
        ),
        "text/html; charset=utf-8",
        status=HTTPStatus.BAD_REQUEST,
        headers=no_ref,
      )
    return bytes_response(
      render_reset_password_page_html(token=token, email=info.email),
      "text/html; charset=utf-8",
      headers=no_ref,
    )

  async def _handle_auth_status(
    self, request: web.Request
  ) -> web.StreamResponse:
    session = self._session_for(request)
    payload: AuthStatusResponseDict = {
      "auth_required": True,
      "authenticated": session is not None,
      "account_id": session.account_id if session is not None else None,
      "email": session.email if session is not None else None,
      "expires_at": session.expires_at if session is not None else None,
    }
    return json_response(payload)

  async def _handle_auth_login(
    self, request: web.Request
  ) -> web.StreamResponse:
    if self._reject_cross_site(request):
      return json_response(
        {"authenticated": False, "error": "Cross-site login is forbidden"},
        status=HTTPStatus.FORBIDDEN,
      )
    is_json = "application/json" in request.content_type.lower()
    client_ip = resolve_request_client_ip(
      request,
      trust_proxy_headers=self.trust_proxy_headers,
      trusted_networks=self._trusted_proxy_networks,
    )
    body = await request.read()
    email = ""
    password = ""
    next_url = request.query.get("next", "/")

    if is_json and body:
      try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
      except json.JSONDecodeError:
        return json_response(
          {"error": "Invalid JSON body"}, status=HTTPStatus.BAD_REQUEST
        )
      if isinstance(payload, dict):
        email = str(payload.get("email") or "")
        password = str(payload.get("password") or "")
        if payload.get("next"):
          next_url = str(payload["next"])
    else:
      form = parse_qs(body.decode("utf-8", errors="replace")) if body else {}
      email = (form.get("email") or [""])[0]
      password = (form.get("password") or [""])[0]
      if form.get("next"):
        next_url = form["next"][0]

    retry_after = self.login_limiter.check(client_ip)
    if retry_after > 0:
      retry_hdr = {"Retry-After": str(retry_after)}
      msg = f"Too many failed sign-in attempts. Retry in {retry_after}s."
      if is_json:
        return json_response(
          {"authenticated": False, "error": msg},
          status=HTTPStatus.TOO_MANY_REQUESTS,
          headers=retry_hdr,
        )
      return bytes_response(
        render_login_page_html(
          next_url=next_url,
          error_message=msg,
          password_reset_enabled=self.password_reset_enabled,
        ),
        "text/html; charset=utf-8",
        status=HTTPStatus.TOO_MANY_REQUESTS,
        headers=retry_hdr,
      )

    acct = self.auth.authenticate(email, password)
    if acct is None:
      self.login_limiter.record_failure(client_ip)
      if is_json:
        return json_response(
          {"authenticated": False, "error": "Invalid email or password"},
          status=HTTPStatus.UNAUTHORIZED,
        )
      return bytes_response(
        render_login_page_html(
          next_url=next_url,
          error_message="Invalid email or password.",
          password_reset_enabled=self.password_reset_enabled,
        ),
        "text/html; charset=utf-8",
        status=HTTPStatus.UNAUTHORIZED,
      )

    self.login_limiter.record_success(client_ip)

    token, session = self.auth.create_session_token(acct)
    set_cookie = {
      "Set-Cookie": self.auth.build_set_cookie_header(
        token, secure=self._is_https_request(request)
      )
    }
    if is_json:
      return json_response(
        {
          "authenticated": True,
          "account_id": session.account_id,
          "email": session.email,
          "expires_at": session.expires_at,
        },
        headers=set_cookie,
      )
    return redirect_response(sanitize_next_url(next_url), headers=set_cookie)

  async def _handle_auth_logout(
    self, request: web.Request
  ) -> web.StreamResponse:
    if self._reject_cross_site(request):
      return json_response(
        {"error": "Cross-site logout is forbidden"},
        status=HTTPStatus.FORBIDDEN,
      )
    session = self._session_for(request)
    self.auth.revoke_session_from_headers(
      request.headers, now_ts=int(self._clock())
    )
    client_id = request.query.get("client", "").strip() or None
    if client_id and session is not None:
      await self.tuner_for(session.account_id).release_all(client_id=client_id)
    extra = {
      "Set-Cookie": self.auth.build_clear_cookie_header(
        secure=self._is_https_request(request)
      )
    }
    return json_response({"authenticated": False}, headers=extra)

  async def _handle_auth_forgot_password(
    self, request: web.Request
  ) -> web.StreamResponse:
    if self._reject_cross_site(request):
      return json_response(
        {"error": "Cross-site password reset is forbidden"},
        status=HTTPStatus.FORBIDDEN,
      )
    if not self.password_reset_enabled:
      return json_response(
        {"error": "Password reset via email is not enabled."},
        status=HTTPStatus.NOT_FOUND,
      )
    payload, err_resp = await self._read_json_object(request)
    if err_resp is not None:
      return err_resp
    assert payload is not None
    raw_email = payload.get("email")
    if not isinstance(raw_email, str):
      return json_response(
        {"error": "`email` must be a string."}, status=HTTPStatus.BAD_REQUEST
      )

    client_ip = resolve_request_client_ip(
      request,
      trust_proxy_headers=self.trust_proxy_headers,
      trusted_networks=self._trusted_proxy_networks,
    )
    now_mono = float(self._clock())
    retry_after = self.reset_request_limiter.check(client_ip, now=now_mono)
    if retry_after > 0:
      return json_response(
        {
          "error": (
            f"Too many password reset requests. Retry in {retry_after}s."
          )
        },
        status=HTTPStatus.TOO_MANY_REQUESTS,
        headers={"Retry-After": str(retry_after)},
      )
    self.reset_request_limiter.record_failure(client_ip, now=now_mono)

    acct = self.auth.account_by_email(raw_email)
    sender = self._active_email_sender()
    if (
      acct is not None
      and sender is not None
      and self._allow_account_reset_send(acct.account_id, now_mono)
    ):
      reset_tok, _info = self.auth.create_password_reset_token(
        acct,
        ttl_sec=self._password_reset_ttl_sec,
        now_ts=int(now_mono),
      )
      base_url = self._resolve_reset_base_url(request).rstrip("/")
      reset_url = f"{base_url}/reset-password?token={quote(reset_tok, safe='')}"
      self._spawn_background_task(
        self._send_password_reset_email(
          sender=sender,
          to_address=acct.email,
          reset_url=reset_url,
          ttl_sec=self._password_reset_ttl_sec,
        )
      )
    else:
      self.auth.run_dummy_reset_token_work()

    res_payload: ForgotPasswordResponseDict = {
      "ok": True,
      "message": GENERIC_PASSWORD_RESET_MESSAGE,
    }
    return json_response(res_payload)

  async def _handle_auth_reset_password(
    self, request: web.Request
  ) -> web.StreamResponse:
    if self._reject_cross_site(request):
      return json_response(
        {"error": "Cross-site password reset is forbidden"},
        status=HTTPStatus.FORBIDDEN,
      )
    if not self.password_reset_enabled:
      return json_response(
        {"error": "Password reset via email is not enabled."},
        status=HTTPStatus.NOT_FOUND,
      )
    payload, err_resp = await self._read_json_object(request)
    if err_resp is not None:
      return err_resp
    assert payload is not None
    raw_token = payload.get("token")
    new_pw = payload.get("new_password")
    if not isinstance(raw_token, str) or not isinstance(new_pw, str):
      return json_response(
        {"error": "`token` and `new_password` must be strings."},
        status=HTTPStatus.BAD_REQUEST,
      )

    client_ip = resolve_request_client_ip(
      request,
      trust_proxy_headers=self.trust_proxy_headers,
      trusted_networks=self._trusted_proxy_networks,
    )
    rst_scope = "rst"
    retry_after = self.login_limiter.check(client_ip, scope=rst_scope)
    if retry_after > 0:
      return json_response(
        {
          "error": (
            f"Too many password reset attempts. Retry in {retry_after}s."
          )
        },
        status=HTTPStatus.TOO_MANY_REQUESTS,
        headers={"Retry-After": str(retry_after)},
      )

    clean_token = raw_token.strip()
    now_ts = int(self._clock())
    info = self.auth.verify_password_reset_token(clean_token, now_ts=now_ts)
    if info is None or info.jti in self._inflight_reset_jtis:
      self.login_limiter.record_failure(client_ip, scope=rst_scope)
      return json_response(
        {"error": "This password reset link is invalid or has expired."},
        status=HTTPStatus.BAD_REQUEST,
      )

    try:
      validate_new_web_password(new_pw)
    except ValueError as exc:
      return json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    assert self._password_resetter is not None
    self._inflight_reset_jtis.add(info.jti)
    try:
      updated = await asyncio.to_thread(
        self._password_resetter, info.account_id, new_pw
      )
    except (ValueError, KeyError) as exc:
      return json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
    finally:
      self._inflight_reset_jtis.discard(info.jti)

    self.auth.revoke_reset_token_info(
      info, raw_token=clean_token, now_ts=now_ts
    )
    self.login_limiter.record_success(client_ip, scope=rst_scope)
    self.auth.revoke_session_from_headers(request.headers, now_ts=now_ts)
    new_session_tok, new_session = self.auth.create_session_token(
      updated, now_ts=now_ts
    )
    res_payload: ResetPasswordResponseDict = {
      "ok": True,
      "authenticated": True,
      "account_id": new_session.account_id,
      "email": new_session.email,
      "expires_at": new_session.expires_at,
    }
    return json_response(
      res_payload,
      headers={
        "Set-Cookie": self.auth.build_set_cookie_header(
          new_session_tok, secure=self._is_https_request(request)
        )
      },
    )

  # ---------------------------------------------------------------------------
  # Authenticated UI & API handlers
  # ---------------------------------------------------------------------------
  async def _handle_index(self, _request: web.Request) -> web.StreamResponse:
    return self._static_file_response(self.static_dir / "index.html")

  async def _handle_static(self, request: web.Request) -> web.StreamResponse:
    root = self.static_dir.resolve()
    target = (root / request.match_info["path"]).resolve()
    if not target.is_relative_to(root):
      return json_response({"error": "Forbidden"}, status=HTTPStatus.FORBIDDEN)
    return self._static_file_response(target)

  async def _handle_api_not_found(
    self, request: web.Request
  ) -> web.StreamResponse:
    return json_response(
      {"error": f"Not found: {request.path}"}, status=HTTPStatus.NOT_FOUND
    )

  def _build_player_urls(
    self, request: web.Request, account: UserAccount
  ) -> PlayerUrlsDict:
    if not self._xtream_api_enabled:
      return {
        "xtream_server_url": "",
        "m3u_url": "",
        "m3u_hls_url": "",
        "xmltv_url": "",
      }
    if self._player_base_url_provider is not None:
      base = self._player_base_url_provider(request.headers).rstrip("/")
    else:
      default_port = request.url.port or (
        443 if request.scheme == "https" else 80
      )
      base = format_advertised_base_url(
        request.headers,
        bind_host="127.0.0.1",
        bound_port=default_port,
        server_protocol=request.scheme or "http",
      ).rstrip("/")
    user_q = quote(account.xtream_username, safe="")
    pass_q = quote(account.xtream_password, safe="")
    return {
      "xtream_server_url": base,
      "m3u_url": f"{base}/get.php?username={user_q}&password={pass_q}&type=m3u_plus&output=ts",
      "m3u_hls_url": f"{base}/get.php?username={user_q}&password={pass_q}&type=m3u_plus&output=hls",
      "xmltv_url": f"{base}/xmltv.php?username={user_q}&password={pass_q}",
    }

  def _account_info_payload(
    self, request: web.Request, account: UserAccount
  ) -> AccountInfoResponseDict:
    return {
      "account_id": account.account_id,
      "email": account.email,
      "xtream_username": account.xtream_username,
      "xtream_password": account.xtream_password,
      "player_urls": self._build_player_urls(request, account),
    }

  async def _handle_account_get(
    self, request: web.Request
  ) -> web.StreamResponse:
    _session, account = self._require_request_context(request)
    return json_response(self._account_info_payload(request, account))

  async def _handle_account_password(
    self, request: web.Request
  ) -> web.StreamResponse:
    if self._password_changer is None:
      return json_response(
        {"error": "Password updates are not enabled in this mode."},
        status=HTTPStatus.NOT_IMPLEMENTED,
      )
    payload, err_resp = await self._read_json_object(request)
    if err_resp is not None:
      return err_resp
    assert payload is not None
    current_pw = payload.get("current_password")
    new_pw = payload.get("new_password")
    if not isinstance(current_pw, str) or not isinstance(new_pw, str):
      return json_response(
        {"error": "`current_password` and `new_password` must be strings."},
        status=HTTPStatus.BAD_REQUEST,
      )

    session, _account = self._require_request_context(request)
    client_ip = resolve_request_client_ip(
      request,
      trust_proxy_headers=self.trust_proxy_headers,
      trusted_networks=self._trusted_proxy_networks,
    )
    pwd_scope = f"pwd:{session.account_id}"
    retry_after = self.login_limiter.check(client_ip, scope=pwd_scope)
    if retry_after > 0:
      return json_response(
        {
          "error": (
            f"Too many failed password attempts. Retry in {retry_after}s."
          )
        },
        status=HTTPStatus.TOO_MANY_REQUESTS,
        headers={"Retry-After": str(retry_after)},
      )

    try:
      updated = await asyncio.to_thread(
        self._password_changer, session.account_id, current_pw, new_pw
      )
    except PermissionError as exc:
      self.login_limiter.record_failure(client_ip, scope=pwd_scope)
      return json_response({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
    except (ValueError, KeyError) as exc:
      return json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    self.login_limiter.record_success(client_ip, scope=pwd_scope)
    now_ts = int(self._clock())
    self.auth.revoke_session_from_headers(request.headers, now_ts=now_ts)
    new_tok, _ = self.auth.create_session_token(updated, now_ts=now_ts)
    request[_REQ_COOKIE_ROTATED_KEY] = True
    res_payload: PasswordChangeResponseDict = {
      "ok": True,
      "email": updated.email,
    }
    return json_response(
      res_payload,
      headers={
        "Set-Cookie": self.auth.build_set_cookie_header(
          new_tok, secure=self._is_https_request(request)
        )
      },
    )

  async def _handle_account_regenerate_creds(
    self, request: web.Request
  ) -> web.StreamResponse:
    if self._stream_creds_regenerator is None:
      return json_response(
        {"error": "Credential regeneration is not enabled in this mode."},
        status=HTTPStatus.NOT_IMPLEMENTED,
      )
    session, _account = self._require_request_context(request)
    try:
      updated = await asyncio.to_thread(
        self._stream_creds_regenerator, session.account_id
      )
    except (ValueError, KeyError) as exc:
      return json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
    return json_response(self._account_info_payload(request, updated))

  def epg_status(self, *, now_ts: int | None = None) -> EpgStatusDict:
    """Return current runtime EPG refresh and coverage status."""
    if self.epg_refresher is not None:
      return self.epg_refresher.status_dict(now_ts=now_ts)
    return EpgRefreshStatus.disabled().to_dict()

  async def _handle_health(self, request: web.Request) -> web.StreamResponse:
    session, _account = self._require_request_context(request)
    snap = await self.store.async_get_snapshot()
    if self.epg_refresher is not None:
      self.epg_refresher.trigger_background_if_needed(reason_prefix="health")
    now_ts = int(self._clock())
    epg_status = self.epg_status(now_ts=now_ts)
    payload: HealthApiResponseDict = {
      "status": "ok",
      "lineup_id": snap.lineup_id,
      "provider": snap.provider,
      "market": snap.market,
      "lineup_updated_at": snap.loaded_at,
      "channel_count": len(snap.channels),
      "category_count": len(snap.categories),
      "epg_program_count": epg_status["program_count"],
      "epg_active_program_count": epg_status["active_program_count"],
      "epg_status": epg_status,
      "active_tuner": self.active_tuner_for(session.account_id),
      "auth_required": True,
    }
    return json_response(payload)

  async def _handle_epg_refresh(
    self, _request: web.Request
  ) -> web.StreamResponse:
    if self.epg_refresher is None:
      await self.store.async_get_snapshot(force_reload=True)
      return json_response(
        {"refreshed": False, "epg_status": self.epg_status()}
      )
    status = await self.epg_refresher.refresh_now(force=True, reason="manual")
    http_status = (
      HTTPStatus.OK
      if status.get("state") != "error"
      else HTTPStatus.BAD_GATEWAY
    )
    return json_response(
      {"refreshed": status.get("state") == "ok", "epg_status": status},
      status=http_status,
    )

  async def _handle_tuner_release(
    self, request: web.Request
  ) -> web.StreamResponse:
    session, _account = self._require_request_context(request)
    account_id = session.account_id
    tuner = self.tuner_for(account_id)
    client_id = request.query.get("client", "").strip() or None
    released = await tuner.release_all(client_id=client_id)
    owner_client = self._hls_owner_clients.get(account_id, "")
    if released or (
      tuner.active_info is None
      and (not client_id or not owner_client or owner_client == client_id)
    ):
      self._clear_all_hls_state(account_id)
    return json_response({"released": released})

  async def _handle_lineup(self, request: web.Request) -> web.StreamResponse:
    snap = await self.store.async_get_snapshot()
    if self.epg_refresher is not None:
      self.epg_refresher.trigger_background_if_needed(reason_prefix="lineup")
    now_ts = int(self._clock())
    query = request.query

    cat_filter = query.get("category", "").strip()
    lang_filter = query.get("lang", "").strip().lower()
    epg_filter = (
      (query.get("has_epg") or query.get("epg") or "").strip().lower()
    )
    require_epg = epg_filter in {"1", "true", "yes", "only"}
    search_q = query.get("q", "").strip().lower()
    refresher = self.epg_refresher

    def _build_lineup_bytes() -> bytes:
      by_chno: dict[int, list[ProgramItem]] = {}
      by_epg_id: dict[str, list[ProgramItem]] = {}
      if refresher is not None:
        refresher.sync_external_if_needed(now_ts)
        by_chno, by_epg_id = refresher.db.query_lineup_window(
          min_stop_ts=now_ts - EPG_RECENT_FLOOR_SEC,
          max_start_ts=now_ts + 172800,
          per_channel_limit=48,
        )

      channels_out: list[PublicChannelDict] = []
      category_channel_count = 0
      for ch in snap.channels:
        if (
          cat_filter
          and cat_filter != "all"
          and ch.category_id != cat_filter
          and ch.category_name.lower() != cat_filter.lower()
        ):
          continue
        category_channel_count += 1
        if (
          lang_filter
          and lang_filter != "all"
          and ch.language.lower() != lang_filter
        ):
          continue

        progs = by_chno.get(ch.chno) or by_epg_id.get(ch.epg_channel_id, [])
        pub = ch.to_public_dict(progs, now_ts=now_ts, upcoming_limit=48)
        if require_epg and not (
          pub["now_playing"] or pub["up_next"] or pub["schedule"]
        ):
          continue

        if search_q:
          now_playing = pub["now_playing"]
          now_title = (
            (now_playing.get("title") or "").lower() if now_playing else ""
          )
          haystack = (
            f"{ch.chno} {ch.sd_chno or ''} {ch.name.lower()} "
            f"{(ch.callsign or '').lower()} {ch.category_name.lower()} {now_title}"
          )
          if search_q not in haystack:
            continue

        channels_out.append(pub)

      epg_st = (
        refresher.status_dict(now_ts=now_ts)
        if refresher is not None
        else EpgRefreshStatus.disabled().to_dict()
      )
      lineup_payload: LineupApiResponseDict = {
        "server_timestamp": now_ts,
        "lineup": {
          "lineup_id": snap.lineup_id,
          "provider": snap.provider,
          "market": snap.market,
          "postal_prefix": snap.postal_prefix,
          "timezone": snap.timezone,
          "lineup_updated_at": snap.loaded_at,
          "total_channels": len(snap.channels),
          "category_channels": category_channel_count,
          "filtered_channels": len(channels_out),
        },
        "epg_status": epg_st,
        "categories": [c.to_dict() for c in snap.categories],
        "channels": channels_out,
      }
      return json.dumps(lineup_payload, ensure_ascii=False).encode("utf-8")

    payload_bytes = await asyncio.to_thread(_build_lineup_bytes)
    return bytes_response(payload_bytes, "application/json; charset=utf-8")

  async def _handle_channel_epg(
    self, request: web.Request
  ) -> web.StreamResponse:
    chno = int(request.match_info["chno"])
    if self.epg_refresher is not None:
      self.epg_refresher.trigger_background_if_needed(
        reason_prefix="channel_epg"
      )
    snap = await self.store.async_get_snapshot()
    ch = snap.channels_by_chno.get(chno)
    if ch is None:
      return json_response(
        {"error": f"Unknown channel {chno}"}, status=HTTPStatus.NOT_FOUND
      )

    now_ts = int(self._clock())
    progs: list[ProgramItem] = []
    if self.epg_refresher is not None:
      progs = await asyncio.to_thread(
        self.epg_refresher.db.query_channel_schedule,
        ch.chno,
        ch.epg_channel_id,
        min_stop_ts=now_ts - 1800,
        limit=120,
      )
    upcoming = [
      p.to_dict(now_ts=now_ts)
      for p in progs
      if p.stop_timestamp == 0 or p.stop_timestamp > now_ts - 1800
    ]
    epg_payload: ChannelEpgResponseDict = {
      "chno": ch.chno,
      "name": ch.name,
      "epg_channel_id": ch.epg_channel_id,
      "programs": upcoming,
    }
    return json_response(epg_payload)

  # ---------------------------------------------------------------------------
  # Per-account single-tuner stream relay (MPEG-TS & HLS)
  # ---------------------------------------------------------------------------
  def _upstream_settings(self) -> tuple[str, str]:
    if self._upstream_config_provider is not None:
      return self._upstream_config_provider()
    return self._upstream_server_url, self._upstream_stream_format

  def _candidate_urls_for(
    self, ch: WebChannel, account: UserAccount | None = None
  ) -> list[str]:
    urls: list[str] = []
    server_url, stream_format = self._upstream_settings()
    if (
      server_url
      and account is not None
      and account.upstream_username
      and account.upstream_password
    ):
      seen_ids: set[int] = set()
      for sid in (ch.stream_id, *ch.fallback_stream_ids):
        if sid <= 0 or sid in seen_ids:
          continue
        seen_ids.add(sid)
        built = build_upstream_stream_url(
          server_url,
          account.upstream_username,
          account.upstream_password,
          sid,
          ext=stream_format,
        )
        if built and built not in urls:
          urls.append(built)
    for u in [ch.direct_source, *ch.fallback_sources]:
      if u and "***" not in u and u not in urls:
        urls.append(u)
    return urls

  @staticmethod
  def _is_offline_placeholder(
    final_url: str, headers: Mapping[str, str]
  ) -> bool:
    path_lower = final_url.split("?", 1)[0].lower()
    if any(
      m in path_lower
      for m in ("/black.ts", "/offline.ts", "/dummy.ts", "/unavailable.ts")
    ):
      return True
    return bool(
      not path_lower.endswith(".m3u8")
      and headers.get("last-modified") is not None
      and headers.get("etag") is not None
      and headers.get("content-length") is not None
    )

  def _clear_all_hls_state(self, account_id: str) -> None:
    for aid, chno in list(self._hls_edge_urls):
      if aid == account_id:
        self._hls_edge_urls.pop((aid, chno), None)
    for tok, (asset_aid, _ch, _u, _exp) in list(self._hls_assets.items()):
      if asset_aid == account_id:
        self._hls_assets.pop(tok, None)
    self._hls_owner_clients.pop(account_id, None)

  def _prune_hls_other_channels(self, account_id: str, chno: int) -> None:
    for aid, other_ch in list(self._hls_edge_urls):
      if aid == account_id and other_ch != chno:
        self._hls_edge_urls.pop((aid, other_ch), None)
    for tok, (asset_aid, asset_ch, _u, _exp) in list(self._hls_assets.items()):
      if asset_aid == account_id and asset_ch != chno:
        self._hls_assets.pop(tok, None)

  @staticmethod
  def _is_request_disconnected(request: web.Request) -> bool:
    return request.transport is None or request.transport.is_closing()

  def _allowed_private_origins_for(
    self, ch: WebChannel | None, account: UserAccount | None = None
  ) -> frozenset[tuple[str, int]]:
    if ch is None:
      return frozenset()
    urls = list(self._candidate_urls_for(ch, account))
    if account is not None and (
      edge := self._hls_edge_urls.get((account.account_id, ch.chno))
    ):
      urls.append(edge[0])
    return extract_explicit_private_origins(urls)

  def _register_hls_asset(
    self,
    account: UserAccount,
    chno: int,
    resolved_url: str,
  ) -> str:
    """Register an HTTP(S) HLS URL and return its opaque relay token."""
    ch = self.store.get_snapshot().channels_by_chno.get(chno)
    allowed_origins = self._allowed_private_origins_for(ch, account)
    try:
      validate_upstream_url(
        resolved_url, allowed_private_origins=allowed_origins
      )
    except (UpstreamTargetBlockedError, ValueError):
      return ""
    now = self._clock()
    if len(self._hls_assets) >= HLS_MAX_ASSETS:
      for k, (_aid, _ch, _u, exp) in list(self._hls_assets.items()):
        if exp <= now:
          self._hls_assets.pop(k, None)
      if len(self._hls_assets) >= HLS_MAX_ASSETS:
        oldest_k = next(iter(self._hls_assets))
        self._hls_assets.pop(oldest_k, None)
    tok = hmac.new(
      self._hls_secret,
      f"{account.account_id}:{chno}:{resolved_url}".encode(),
      hashlib.sha256,
    ).hexdigest()[:24]
    self._hls_assets[tok] = (
      account.account_id,
      chno,
      resolved_url,
      now + HLS_ASSET_TTL_SEC,
    )
    return tok

  def _resolve_hls_asset(
    self, account_id: str, chno: int, token: str
  ) -> str | None:
    """Resolve an opaque HLS asset token scoped to `(account_id, chno)`."""
    entry = self._hls_assets.get(token)
    if entry is None:
      return None
    entry_aid, entry_chno, resolved_url, exp = entry
    now = self._clock()
    if exp <= now:
      self._hls_assets.pop(token, None)
      return None
    if entry_aid != account_id or entry_chno != chno:
      return None
    self._hls_assets[token] = (
      account_id,
      chno,
      resolved_url,
      now + HLS_ASSET_TTL_SEC,
    )
    return resolved_url

  async def _handle_mpegts_stream(
    self, request: web.Request
  ) -> web.StreamResponse:
    session_ctx, account = self._require_request_context(request)
    account_id = session_ctx.account_id
    tuner = self.tuner_for(account_id)
    chno = int(request.match_info["chno"])
    client_id = request.query.get("client", "").strip()
    snap = await self.store.async_get_snapshot()
    ch: WebChannel | None = snap.channels_by_chno.get(chno)
    cand_urls = self._candidate_urls_for(ch, account) if ch is not None else []
    if ch is None or not cand_urls:
      return json_response(
        {"error": f"Channel {chno} has no playable stream URL"},
        status=HTTPStatus.NOT_FOUND,
      )

    if request.method == "HEAD":
      return bytes_response(b"", "video/mp2t")

    allowed_origins = self._allowed_private_origins_for(ch, account)
    session = await tuner.preempt_and_acquire(
      chno=ch.chno,
      channel_name=ch.name,
      mode="mpegts",
      task=asyncio.current_task(),
      client_id=client_id,
      is_disconnected=lambda: self._is_request_disconnected(request),
      on_preempt=lambda: self._clear_all_hls_state(account_id),
    )
    if session.cancel_event.is_set():
      return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)

    response: web.StreamResponse | None = None
    last_err = "No candidate stream succeeded"
    try:
      for idx, target_url in enumerate(cand_urls):
        if session.cancel_event.is_set():
          break
        if idx > 0 and tuner.settle_sec > 0:
          await asyncio.sleep(tuner.settle_sec)

        try:
          async with open_upstream_url(
            self.client,
            target_url,
            allowed_private_origins=allowed_origins,
          ) as resp:
            if resp.status not in (200, 206):
              last_err = f"Upstream HTTP {resp.status}"
              continue
            if (
              self._is_offline_placeholder(str(resp.url), resp.headers)
              and idx < len(cand_urls) - 1
            ):
              last_err = "Stream redirected to offline placeholder"
              continue

            ctype = resp.headers.get("Content-Type") or "video/mp2t"
            if "mpegurl" in ctype.lower():
              ctype = "application/vnd.apple.mpegurl"
            elif "octet-stream" in ctype.lower():
              ctype = "video/mp2t"

            response = web.StreamResponse(
              status=HTTPStatus.OK,
              headers={
                "Content-Type": ctype,
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "X-Accel-Buffering": "no",
              },
            )
            response.enable_chunked_encoding()
            await asyncio.wait_for(
              response.prepare(request), timeout=self.write_timeout_sec
            )
            async for chunk in resp.content.iter_chunked(16384):
              if session.cancel_event.is_set():
                break
              await asyncio.wait_for(
                response.write(chunk), timeout=self.write_timeout_sec
              )
            await asyncio.wait_for(
              response.write_eof(), timeout=self.write_timeout_sec
            )
            return response
        except UpstreamTargetBlockedError as exc:
          logger.warning(
            "Blocked upstream stream target on ch %d: %s", chno, exc
          )
          last_err = "Upstream target blocked by SSRF policy"
          if response is not None:
            return response
          continue
        except UPSTREAM_ERRORS as exc:
          logger.debug(
            "MPEG-TS candidate failed on ch %d: %s",
            chno,
            _redact_upstream_text(str(exc), secret=account.upstream_password),
          )
          last_err = "Upstream connection failed"
          if response is not None:
            return response
          continue

      if response is not None:
        return response
      return json_response(
        {"error": last_err, "chno": ch.chno}, status=HTTPStatus.BAD_GATEWAY
      )
    except (
      asyncio.CancelledError,
      asyncio.TimeoutError,
      ConnectionResetError,
      BrokenPipeError,
    ):
      # Preempted by another viewer (tuner handoff) or stalled/dead client.
      return response or web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
    finally:
      await tuner.mark_closed(session)

  async def _fetch_text(
    self,
    url: str,
    *,
    allowed_private_origins: Collection[tuple[str, int]] = (),
  ) -> tuple[int, str, str]:
    """GET `url`, returning `(status, final_url, decoded_body)`."""
    async with open_upstream_url(
      self.client,
      url,
      allowed_private_origins=allowed_private_origins,
    ) as resp:
      body = await resp.read()
      return resp.status, str(resp.url), body.decode("utf-8", errors="replace")

  async def _handle_hls_index(self, request: web.Request) -> web.StreamResponse:
    session_ctx, account = self._require_request_context(request)
    account_id = session_ctx.account_id
    tuner = self.tuner_for(account_id)
    chno = int(request.match_info["chno"])
    client_id = request.query.get("client", "").strip()
    tune_token = request.query.get("t", "").strip()
    snap = await self.store.async_get_snapshot()
    ch = snap.channels_by_chno.get(chno)
    cand_urls = self._candidate_urls_for(ch, account) if ch is not None else []
    if ch is None or not cand_urls:
      return json_response(
        {"error": f"Channel {chno} not found"}, status=HTTPStatus.NOT_FOUND
      )

    allowed_origins = self._allowed_private_origins_for(ch, account)
    session, is_new = await tuner.acquire_or_renew_hls(
      chno=ch.chno,
      channel_name=ch.name,
      client_id=client_id,
      tune_token=tune_token,
      task=asyncio.current_task(),
      is_disconnected=lambda: self._is_request_disconnected(request),
      on_preempt=lambda active_ch: self._prune_hls_other_channels(
        account_id, active_ch
      ),
    )
    if session is None or session.cancel_event.is_set():
      return json_response(
        {"error": "Stream preempted by another viewer"},
        status=HTTPStatus.SERVICE_UNAVAILABLE,
      )
    if client_id:
      self._hls_owner_clients[account_id] = client_id

    def _reg_asset(ch_num: int, resolved_u: str) -> str:
      return self._register_hls_asset(account, ch_num, resolved_u)

    served_manifest = False
    try:
      # 1. Reuse the cached edge playlist URL so 5s hls.js polls stay on one
      #    edge session for this account.
      cached = self._hls_edge_urls.get((account_id, chno))
      if cached is not None:
        edge_url, ts = cached
        if self._clock() - ts < 60.0:
          try:
            status, final_url, text = await self._fetch_text(
              edge_url, allowed_private_origins=allowed_origins
            )
            if status == 200 and "#EXTM3U" in text:
              self._hls_edge_urls[(account_id, chno)] = (
                final_url,
                self._clock(),
              )
              tuner.touch_hls_lease(chno)
              served_manifest = True
              return self._manifest_response(
                rewrite_hls_manifest(
                  text,
                  final_url,
                  chno,
                  register_asset=_reg_asset,
                )
              )
          except UPSTREAM_ERRORS:
            pass
        self._hls_edge_urls.pop((account_id, chno), None)

      # 2. Acquire a fresh HLS session across candidate URLs (skipping black.ts
      #    placeholders).
      last_err = "Upstream HLS unavailable"
      for idx, raw_src in enumerate(cand_urls):
        if session.cancel_event.is_set():
          return json_response(
            {"error": "Stream preempted by another viewer"},
            status=HTTPStatus.SERVICE_UNAVAILABLE,
          )
        if idx > 0 and tuner.settle_sec > 0:
          await asyncio.sleep(tuner.settle_sec)
        hls_url = _to_hls_url(raw_src)
        try:
          status, final_url, manifest_text = await self._fetch_text(
            hls_url, allowed_private_origins=allowed_origins
          )
        except UpstreamTargetBlockedError as exc:
          logger.warning("Blocked HLS target on ch %d: %s", chno, exc)
          last_err = "HLS target blocked by SSRF policy"
          continue
        except UPSTREAM_ERRORS as exc:
          logger.debug(
            "HLS candidate failed on ch %d: %s",
            chno,
            _redact_upstream_text(str(exc), secret=account.upstream_password),
          )
          last_err = "HLS upstream connection failed"
          continue
        if status != 200:
          last_err = f"Upstream HLS returned HTTP {status}"
          continue
        if "#EXTM3U" not in manifest_text:
          last_err = "Invalid M3U8 response"
          continue
        if "/black.ts" in manifest_text.lower() and idx < len(cand_urls) - 1:
          last_err = "Stream redirected to offline black.ts placeholder"
          continue
        if final_url != hls_url:
          self._hls_edge_urls[(account_id, chno)] = (final_url, self._clock())
        tuner.touch_hls_lease(chno)
        served_manifest = True
        return self._manifest_response(
          rewrite_hls_manifest(
            manifest_text,
            final_url,
            chno,
            register_asset=_reg_asset,
          )
        )

      return json_response({"error": last_err}, status=HTTPStatus.BAD_GATEWAY)
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
      return json_response(
        {"error": "Stream preempted by another viewer"},
        status=HTTPStatus.SERVICE_UNAVAILABLE,
      )
    finally:
      session.task = None
      if session.cancel_event.is_set() or (is_new and not served_manifest):
        await tuner.mark_closed(session)

  @staticmethod
  def _manifest_response(manifest: str) -> web.Response:
    return bytes_response(
      manifest.encode("utf-8"), "application/vnd.apple.mpegurl; charset=utf-8"
    )

  async def _handle_hls_asset(self, request: web.Request) -> web.StreamResponse:
    session_ctx, account = self._require_request_context(request)
    account_id = session_ctx.account_id
    tuner = self.tuner_for(account_id)
    chno = int(request.match_info["chno"])
    token = request.query.get("u", "").strip()
    if not token:
      return json_response(
        {"error": "Missing asset token"}, status=HTTPStatus.BAD_REQUEST
      )
    if not tuner.touch_hls_lease(chno):
      return json_response(
        {"error": "HLS session preempted"},
        status=HTTPStatus.SERVICE_UNAVAILABLE,
      )
    asset_url = self._resolve_hls_asset(account_id, chno, token)
    if asset_url is None:
      return json_response(
        {"error": "Invalid or expired HLS asset token"},
        status=HTTPStatus.FORBIDDEN,
      )

    ch = (await self.store.async_get_snapshot()).channels_by_chno.get(chno)
    allowed_origins = self._allowed_private_origins_for(ch, account)
    response: web.StreamResponse | None = None
    try:
      async with open_upstream_url(
        self.client,
        asset_url,
        allowed_private_origins=allowed_origins,
      ) as resp:
        if resp.status not in (200, 206):
          return json_response(
            {"error": f"HLS asset HTTP {resp.status}"},
            status=HTTPStatus.BAD_GATEWAY,
          )
        ctype = resp.headers.get("Content-Type", "").lower()
        is_playlist = "mpegurl" in ctype or asset_url.split("?", 1)[
          0
        ].lower().endswith(".m3u8")
        if is_playlist:
          body = await resp.read()
          text = body.decode("utf-8", errors="replace")
          if "#EXTM3U" in text:
            return self._manifest_response(
              rewrite_hls_manifest(
                text,
                str(resp.url),
                chno,
                register_asset=lambda ch_num, resolved_u: (
                  self._register_hls_asset(account, ch_num, resolved_u)
                ),
              )
            )
          return bytes_response(body, ctype or "application/octet-stream")

        # Stream binary TS/fMP4 HLS segment via HTTP/1.1 chunked encoding.
        response = web.StreamResponse(
          status=HTTPStatus.OK,
          headers={
            "Content-Type": resp.headers.get("Content-Type") or "video/mp2t",
            "Cache-Control": "no-store",
          },
        )
        response.enable_chunked_encoding()
        await asyncio.wait_for(
          response.prepare(request), timeout=self.write_timeout_sec
        )
        async for chunk in resp.content.iter_chunked(32768):
          await asyncio.wait_for(
            response.write(chunk), timeout=self.write_timeout_sec
          )
        await asyncio.wait_for(
          response.write_eof(), timeout=self.write_timeout_sec
        )
        return response
    except (
      asyncio.TimeoutError,
      ConnectionResetError,
      BrokenPipeError,
      asyncio.CancelledError,
    ):
      return response or web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
    except UpstreamTargetBlockedError as exc:
      logger.warning("Blocked HLS segment redirect on ch %d: %s", chno, exc)
      if response is not None:
        return response
      return json_response(
        {"error": "HLS segment blocked by SSRF policy"},
        status=HTTPStatus.BAD_GATEWAY,
      )
    except UPSTREAM_ERRORS as exc:
      logger.debug(
        "HLS segment error on ch %d: %s",
        chno,
        _redact_upstream_text(str(exc), secret=account.upstream_password),
      )
      if response is not None:
        return response
      return json_response(
        {"error": "HLS segment fetch failed"}, status=HTTPStatus.BAD_GATEWAY
      )

  # ---------------------------------------------------------------------------
  # Static files
  # ---------------------------------------------------------------------------
  @staticmethod
  def _static_file_response(file_path: Path) -> web.StreamResponse:
    if not file_path.is_file():
      return json_response(
        {"error": "Static file not found"}, status=HTTPStatus.NOT_FOUND
      )
    mime, _ = mimetypes.guess_type(str(file_path))
    content_type = mime or "application/octet-stream"
    if content_type.startswith("text/") or content_type in (
      "application/javascript",
      "application/json",
    ):
      content_type = f"{content_type}; charset=utf-8"
    cache_hdr = (
      "public, max-age=3600"
      if ("vendor" in file_path.parts or file_path.suffix in (".svg", ".ico"))
      else "no-cache"
    )
    return web.FileResponse(
      file_path,
      headers={"Content-Type": content_type, "Cache-Control": cache_hdr},
    )


def resolve_bind_host(configured_host: str | None) -> str:
  """Resolve `web.host` from config into a concrete bind address."""
  return _resolve_bind_host(configured_host, ip_detector=detect_device_ip)


def build_access_urls(
  bind_host: str,
  port: int,
  *,
  device_ip: str | None = None,
) -> list[str]:
  """Build user-facing HTTP URLs for the given bind address and port."""
  return _build_access_urls(
    bind_host, port, device_ip=device_ip, ip_detector=detect_device_ip
  )
