"""aiohttp web application and single-tuner stream relay for the Bellhop Web Guide."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import mimetypes
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin

import aiohttp
from aiohttp import web

from bellhop_web.api_contracts import (
  AuthStatusResponseDict,
  ChannelEpgResponseDict,
  HealthApiResponseDict,
  LineupApiResponseDict,
  PublicConfigResponseDict,
  TunerActiveInfoDict,
)
from bellhop_web.auth import (
  SESSION_COOKIE_NAME,
  LoginRateLimiter,
  SessionInfo,
  WebAuthManager,
  render_login_page_html,
  sanitize_next_url,
)
from bellhop_web.epg_refresh import (
  EpgRefreshService,
  EpgRefreshStatus,
  EpgStatusDict,
)
from bellhop_web.http_utils import (
  DEFAULT_TRUSTED_PROXY_IPS,
  UPSTREAM_ERRORS,
  ClientSessionFactory,
  IpNetwork,
  bytes_response,
  create_web_app,
  default_session_factory,
  detect_device_ip,
  json_response,
  parse_trusted_proxy_networks,
  redirect_response,
  resolve_request_client_ip,
  upstream_url,
)
from bellhop_web.http_utils import (
  build_access_urls as _build_access_urls,
)
from bellhop_web.http_utils import (
  resolve_bind_host as _resolve_bind_host,
)
from bellhop_web.store import (
  LineupGuideStore,
  LineupSnapshot,
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

logger = logging.getLogger("bellhop_web")

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

UPSTREAM_USER_AGENT = "VLC/3.0.20 LibVLC/3.0.20"
HLS_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _encode_url_token(url: str) -> str:
  return (
    base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
  )


def _decode_url_token(token: str) -> str:
  pad = "=" * ((4 - len(token) % 4) % 4)
  return base64.urlsafe_b64decode((token + pad).encode("ascii")).decode("utf-8")


def _to_hls_url(direct_source: str) -> str:
  if ".m3u8" in direct_source.lower():
    return direct_source
  if re.search(r"\.ts(?:\?|$)", direct_source, flags=re.IGNORECASE):
    return re.sub(
      r"\.ts(\?.*)?$", r".m3u8\1", direct_source, flags=re.IGNORECASE
    )
  return f"{direct_source.rstrip('/')}.m3u8"


def rewrite_hls_manifest(manifest_text: str, base_url: str, chno: int) -> str:
  """Rewrite relative and absolute URIs in an M3U8 manifest through the local proxy."""
  out_lines: list[str] = []

  def _proxy_uri(raw_uri: str) -> str:
    resolved = urljoin(base_url, raw_uri.strip())
    tok = _encode_url_token(resolved)
    return f"/api/hls/{chno}/asset?u={tok}"

  for raw_line in manifest_text.splitlines():
    stripped = raw_line.strip()
    if not stripped:
      out_lines.append("")
      continue
    if stripped.startswith("#"):
      if 'URI="' in stripped:
        rewritten_tag = HLS_URI_ATTR_RE.sub(
          lambda m: f'URI="{_proxy_uri(m.group(1))}"',
          stripped,
        )
        out_lines.append(rewritten_tag)
      else:
        out_lines.append(stripped)
      continue
    out_lines.append(_proxy_uri(stripped))

  return "\n".join(out_lines) + "\n"


@dataclass(slots=True)
class ActiveTunerSession:
  session_id: int
  chno: int
  channel_name: str
  mode: str
  started_at: float
  client_id: str = ""
  cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
  closed_event: asyncio.Event = field(default_factory=asyncio.Event)
  task: asyncio.Task[Any] | None = None


class TunerSessionManager:
  """Coordinates single-tuner stream handoffs so max_connections=1 accounts never collide."""

  def __init__(self, settle_sec: float = 0.18) -> None:
    self.settle_sec = max(0.0, settle_sec)
    self._lock = asyncio.Lock()
    self._seq = 0
    self._active: ActiveTunerSession | None = None

  @property
  def active_info(self) -> TunerActiveInfoDict | None:
    s = self._active
    if s is None or s.closed_event.is_set():
      return None
    return {
      "session_id": s.session_id,
      "chno": s.chno,
      "channel_name": s.channel_name,
      "mode": s.mode,
      "uptime_sec": round(max(0.0, time.time() - s.started_at), 1),
    }

  async def preempt_and_acquire(
    self,
    chno: int,
    channel_name: str,
    mode: str = "mpegts",
    task: asyncio.Task[Any] | None = None,
    client_id: str = "",
  ) -> ActiveTunerSession:
    async with self._lock:
      had_previous = await self._close_active_locked()
      if had_previous and self.settle_sec > 0:
        await asyncio.sleep(self.settle_sec)
      self._seq += 1
      session = ActiveTunerSession(
        session_id=self._seq,
        chno=chno,
        channel_name=channel_name,
        mode=mode,
        started_at=time.time(),
        client_id=client_id,
        task=task,
      )
      self._active = session
      return session

  async def release_all(self, *, client_id: str | None = None) -> bool:
    async with self._lock:
      if (
        client_id
        and self._active is not None
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
    prev = self._active
    if prev is None or prev.closed_event.is_set():
      self._active = None
      return False

    prev.cancel_event.set()
    if prev.task is not None and not prev.task.done():
      prev.task.cancel()
    with contextlib.suppress(asyncio.TimeoutError):
      await asyncio.wait_for(prev.closed_event.wait(), timeout=1.5)
    self._active = None
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
    settle_sec: float = 0.18,
    session_factory: ClientSessionFactory | None = None,
    static_dir: Path = STATIC_DIR,
    epg_refresher: EpgRefreshService | None = None,
    auth: WebAuthManager | None = None,
    login_limiter: LoginRateLimiter | None = None,
    trust_proxy_headers: bool = False,
    trusted_proxy_ips: Sequence[str] = DEFAULT_TRUSTED_PROXY_IPS,
    config_reader: Callable[[], dict[str, Any]] | None = None,
    config_writer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
  ) -> None:
    self.store = store
    self.static_dir = static_dir
    self.tuner = TunerSessionManager(settle_sec=settle_sec)
    self.epg_refresher = epg_refresher
    self.auth = auth
    self.login_limiter: LoginRateLimiter = login_limiter or LoginRateLimiter()
    self.trust_proxy_headers = bool(trust_proxy_headers)
    self._trusted_proxy_networks: tuple[IpNetwork, ...] = (
      parse_trusted_proxy_networks(trusted_proxy_ips)
    )
    self._config_reader = config_reader
    self._config_writer = config_writer
    self._session_factory: ClientSessionFactory = (
      session_factory or default_session_factory
    )
    self._client: aiohttp.ClientSession | None = None
    # Active HLS edge playlist URL cache: chno -> (resolved_edge_url, last_used_epoch)
    self._hls_edge_urls: dict[int, tuple[str, float]] = {}
    self._hls_owner_client: str = ""

  # ---------------------------------------------------------------------------
  # Lifecycle
  # ---------------------------------------------------------------------------
  async def startup(self) -> None:
    """Create the upstream HTTP client and start the EPG refresher (idempotent)."""
    if self._client is None:
      self._client = self._session_factory(
        timeout=aiohttp.ClientTimeout(
          total=None, connect=8.0, sock_connect=8.0, sock_read=15.0
        ),
        headers={"User-Agent": UPSTREAM_USER_AGENT, "Icy-MetaData": "0"},
      )
    if self.epg_refresher is not None:
      await self.epg_refresher.start()

  async def shutdown(self) -> None:
    """Stop the EPG refresher, release the tuner, and close the upstream client."""
    if self.epg_refresher is not None:
      await self.epg_refresher.stop()
    await self.tuner.release_all()
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

    # Public brand, favicon, stylesheet & client-prefs/login assets (needed by `/login`).
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
    r.add_get("/api/auth/status", self._handle_auth_status)
    r.add_post("/api/auth/login", self._handle_auth_login)
    r.add_get("/api/auth/logout", self._handle_auth_logout, allow_head=False)
    r.add_post("/api/auth/logout", self._handle_auth_logout)

    # Authenticated UI, APIs & stream relay.
    r.add_get("/", protect(self._handle_index))
    r.add_get("/index.html", protect(self._handle_index))
    r.add_get("/static/{path:.+}", protect(self._handle_static))
    r.add_get("/api/config", protect(self._handle_config_get))
    r.add_put("/api/config", protect(self._handle_config_put))
    r.add_post("/api/config", protect(self._handle_config_put))
    r.add_get("/api/health", protect(self._handle_health))
    for method in ("GET", "POST"):
      r.add_route(method, "/api/epg/refresh", protect(self._handle_epg_refresh))
      r.add_route(
        method, "/api/tuner/release", protect(self._handle_tuner_release)
      )
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
  # Authentication helpers
  # ---------------------------------------------------------------------------
  @property
  def _auth_enabled(self) -> bool:
    return self.auth is not None and self.auth.enabled

  def _session_for(self, request: web.Request) -> SessionInfo | None:
    if self.auth is None or not self.auth.enabled:
      return None
    return self.auth.extract_session_from_headers(
      {"cookie": request.headers.get("Cookie", "")}
    )

  def _protected(self, handler: _Handler) -> _Handler:
    """Wrap `handler` with Web Guide session enforcement & sliding renewal."""

    async def _wrapped(request: web.Request) -> web.StreamResponse:
      if self.auth is None or not self.auth.enabled:
        return await handler(request)

      session = self._session_for(request)
      if session is None:
        clear_hdr = (
          {"Set-Cookie": self.auth.build_clear_cookie_header()}
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

      response = await handler(request)
      if session.needs_renewal() and not response.prepared:
        new_tok, _ = self.auth.create_session_token(session.username)
        response.headers.add(
          "Set-Cookie", self.auth.build_set_cookie_header(new_tok)
        )
      return response

    return _wrapped

  # ---------------------------------------------------------------------------
  # Public handlers
  # ---------------------------------------------------------------------------
  def _public_asset_handler(self, asset_name: str) -> _Handler:
    async def _handler(_request: web.Request) -> web.StreamResponse:
      return self._static_file_response(self.static_dir / asset_name)

    return _handler

  async def _handle_login_page(
    self, request: web.Request
  ) -> web.StreamResponse:
    next_url = sanitize_next_url(request.query.get("next", "/"))
    if not self._auth_enabled or self._session_for(request) is not None:
      return redirect_response(next_url)
    return bytes_response(
      render_login_page_html(next_url=next_url), "text/html; charset=utf-8"
    )

  async def _handle_auth_status(
    self, request: web.Request
  ) -> web.StreamResponse:
    auth_enabled = self._auth_enabled
    session = self._session_for(request)
    payload: AuthStatusResponseDict = {
      "auth_required": auth_enabled,
      "authenticated": (not auth_enabled) or (session is not None),
      "username": session.username if session is not None else None,
      "expires_at": session.expires_at if session is not None else None,
    }
    return json_response(payload)

  async def _handle_auth_login(
    self, request: web.Request
  ) -> web.StreamResponse:
    if self.auth is None or not self.auth.enabled:
      return json_response(
        {"auth_required": False, "authenticated": True, "username": None}
      )

    is_json = "application/json" in request.content_type.lower()
    client_ip = resolve_request_client_ip(
      request,
      trust_proxy_headers=self.trust_proxy_headers,
      trusted_networks=self._trusted_proxy_networks,
    )
    body = await request.read()
    username = ""
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
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if payload.get("next"):
          next_url = str(payload["next"])
    else:
      form = parse_qs(body.decode("utf-8", errors="replace")) if body else {}
      username = (form.get("username") or [""])[0]
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
        render_login_page_html(next_url=next_url, error_message=msg),
        "text/html; charset=utf-8",
        status=HTTPStatus.TOO_MANY_REQUESTS,
        headers=retry_hdr,
      )

    if not self.auth.verify_credentials(username, password):
      self.login_limiter.record_failure(client_ip)
      if is_json:
        return json_response(
          {"authenticated": False, "error": "Invalid username or password"},
          status=HTTPStatus.UNAUTHORIZED,
        )
      return bytes_response(
        render_login_page_html(
          next_url=next_url, error_message="Invalid username or password."
        ),
        "text/html; charset=utf-8",
        status=HTTPStatus.UNAUTHORIZED,
      )

    self.login_limiter.record_success(client_ip)

    token, session = self.auth.create_session_token(username)
    set_cookie = {"Set-Cookie": self.auth.build_set_cookie_header(token)}
    if is_json:
      return json_response(
        {
          "authenticated": True,
          "username": session.username,
          "expires_at": session.expires_at,
        },
        headers=set_cookie,
      )
    return redirect_response(sanitize_next_url(next_url), headers=set_cookie)

  async def _handle_auth_logout(
    self, request: web.Request
  ) -> web.StreamResponse:
    client_id = request.query.get("client", "").strip() or None
    if client_id:
      await self.tuner.release_all(client_id=client_id)
    extra = (
      {"Set-Cookie": self.auth.build_clear_cookie_header()}
      if self.auth is not None
      else None
    )
    return json_response({"authenticated": False}, headers=extra)

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

  async def _handle_config_get(
    self, _request: web.Request
  ) -> web.StreamResponse:
    if self._config_reader is None:
      unavailable: PublicConfigResponseDict = {
        "available": False,
        "config": None,
        "config_file": None,
      }
      return json_response(unavailable)
    return json_response(self._config_reader())

  async def _handle_config_put(
    self, request: web.Request
  ) -> web.StreamResponse:
    if self._config_writer is None:
      return json_response(
        {"error": "Configuration editing is not enabled in this mode."},
        status=HTTPStatus.NOT_IMPLEMENTED,
      )
    body = await request.read()
    try:
      updates = (
        json.loads(body.decode("utf-8", errors="replace")) if body else {}
      )
      if not isinstance(updates, dict):
        raise ValueError("Request body must be a JSON object.")
      return json_response(self._config_writer(updates))
    except (json.JSONDecodeError, ValueError) as exc:
      return json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

  def _epg_status_dict(
    self, snap: LineupSnapshot, *, now_ts: int | None = None
  ) -> EpgStatusDict:
    if self.epg_refresher is not None:
      return self.epg_refresher.status_dict(now_ts=now_ts)
    return EpgRefreshStatus.disabled_for_snapshot(snap, now_ts=now_ts).to_dict()

  async def _handle_health(self, _request: web.Request) -> web.StreamResponse:
    snap = self.store.get_snapshot()
    total_progs, active_progs, _max_stop = snap.epg_coverage_stats()
    payload: HealthApiResponseDict = {
      "status": "ok",
      "lineup_id": snap.lineup_id,
      "provider": snap.provider,
      "market": snap.market,
      "channel_count": len(snap.channels),
      "category_count": len(snap.categories),
      "epg_program_count": total_progs,
      "epg_active_program_count": active_progs,
      "epg_status": self._epg_status_dict(snap),
      "active_tuner": self.tuner.active_info,
      "auth_required": self._auth_enabled,
    }
    return json_response(payload)

  async def _handle_epg_refresh(
    self, _request: web.Request
  ) -> web.StreamResponse:
    if self.epg_refresher is None:
      snap = self.store.get_snapshot(force_reload=True)
      return json_response(
        {"refreshed": False, "epg_status": self._epg_status_dict(snap)}
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
    client_id = request.query.get("client", "").strip() or None
    if (
      not client_id
      or not self._hls_owner_client
      or self._hls_owner_client == client_id
    ):
      self._hls_edge_urls.clear()
      self._hls_owner_client = ""
    released = await self.tuner.release_all(client_id=client_id)
    return json_response({"released": released})

  async def _handle_lineup(self, request: web.Request) -> web.StreamResponse:
    if self.epg_refresher is not None:
      self.epg_refresher.trigger_background_if_needed(reason_prefix="lineup")
    snap = self.store.get_snapshot()
    now_ts = int(time.time())
    query = request.query

    cat_filter = query.get("category", "").strip()
    lang_filter = query.get("lang", "").strip().lower()
    epg_filter = (
      (query.get("has_epg") or query.get("epg") or "").strip().lower()
    )
    require_epg = epg_filter in {"1", "true", "yes", "only"}
    search_q = query.get("q", "").strip().lower()

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

      progs = snap.programs_by_epg_id.get(ch.epg_channel_id, [])
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

    lineup_payload: LineupApiResponseDict = {
      "server_timestamp": now_ts,
      "lineup": {
        "lineup_id": snap.lineup_id,
        "provider": snap.provider,
        "market": snap.market,
        "postal_prefix": snap.postal_prefix,
        "timezone": snap.timezone,
        "total_channels": len(snap.channels),
        "category_channels": category_channel_count,
        "filtered_channels": len(channels_out),
      },
      "epg_status": self._epg_status_dict(snap, now_ts=now_ts),
      "categories": [c.to_dict() for c in snap.categories],
      "channels": channels_out,
    }
    return json_response(lineup_payload)

  async def _handle_channel_epg(
    self, request: web.Request
  ) -> web.StreamResponse:
    chno = int(request.match_info["chno"])
    if self.epg_refresher is not None:
      self.epg_refresher.trigger_background_if_needed(
        reason_prefix="channel_epg"
      )
    snap = self.store.get_snapshot()
    ch = snap.channels_by_chno.get(chno)
    if ch is None:
      return json_response(
        {"error": f"Unknown channel {chno}"}, status=HTTPStatus.NOT_FOUND
      )

    now_ts = int(time.time())
    progs = snap.programs_by_epg_id.get(ch.epg_channel_id, [])
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
  # Single-tuner stream relay (MPEG-TS & HLS)
  # ---------------------------------------------------------------------------
  @staticmethod
  def _candidate_urls_for(ch: WebChannel) -> list[str]:
    urls: list[str] = []
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

  async def _handle_mpegts_stream(
    self, request: web.Request
  ) -> web.StreamResponse:
    chno = int(request.match_info["chno"])
    client_id = request.query.get("client", "").strip()
    snap = self.store.get_snapshot()
    ch: WebChannel | None = snap.channels_by_chno.get(chno)
    cand_urls = self._candidate_urls_for(ch) if ch is not None else []
    if ch is None or not cand_urls:
      return json_response(
        {"error": f"Channel {chno} has no playable stream URL"},
        status=HTTPStatus.NOT_FOUND,
      )

    if request.method == "HEAD":
      return bytes_response(b"", "video/mp2t")

    self._hls_edge_urls.clear()
    self._hls_owner_client = ""
    session = await self.tuner.preempt_and_acquire(
      chno=ch.chno,
      channel_name=ch.name,
      mode="mpegts",
      task=asyncio.current_task(),
      client_id=client_id,
    )

    response: web.StreamResponse | None = None
    last_err = "No candidate stream succeeded"
    try:
      for idx, target_url in enumerate(cand_urls):
        if session.cancel_event.is_set():
          break
        if idx > 0 and self.tuner.settle_sec > 0:
          await asyncio.sleep(self.tuner.settle_sec)

        try:
          async with self.client.get(upstream_url(target_url)) as resp:
            if resp.status not in (200, 206):
              last_err = f"Upstream HTTP {resp.status}"
              continue
            if (
              self._is_offline_placeholder(str(resp.url), resp.headers)
              and idx < len(cand_urls) - 1
            ):
              last_err = (
                f"Stream redirected to offline placeholder ({resp.url})"
              )
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
            await response.prepare(request)
            async for chunk in resp.content.iter_chunked(16384):
              if session.cancel_event.is_set():
                break
              await response.write(chunk)
            await response.write_eof()
            return response
        except UPSTREAM_ERRORS as exc:
          # Also covers writes to a viewer that already disconnected.
          last_err = f"Upstream connection error: {exc}"
          if response is not None:
            return response
          continue

      if response is not None:
        return response
      return json_response(
        {"error": last_err, "chno": ch.chno}, status=HTTPStatus.BAD_GATEWAY
      )
    except (asyncio.CancelledError, ConnectionResetError):
      # Preempted by another viewer (tuner handoff) or viewer disconnected.
      return response or web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
    finally:
      await self.tuner.mark_closed(session)

  async def _fetch_text(self, url: str) -> tuple[int, str, str]:
    """GET `url`, returning `(status, final_url, decoded_body)`."""
    async with self.client.get(upstream_url(url)) as resp:
      body = await resp.read()
      return resp.status, str(resp.url), body.decode("utf-8", errors="replace")

  async def _handle_hls_index(self, request: web.Request) -> web.StreamResponse:
    chno = int(request.match_info["chno"])
    client_id = request.query.get("client", "").strip()
    snap = self.store.get_snapshot()
    ch = snap.channels_by_chno.get(chno)
    cand_urls = self._candidate_urls_for(ch) if ch is not None else []
    if ch is None or not cand_urls:
      return json_response(
        {"error": f"Channel {chno} not found"}, status=HTTPStatus.NOT_FOUND
      )

    # Close any continuous .ts connection and drop stale HLS sessions for OTHER channels
    await self.tuner.release_all()
    if client_id:
      self._hls_owner_client = client_id
    for other_ch in list(self._hls_edge_urls):
      if other_ch != chno:
        self._hls_edge_urls.pop(other_ch, None)

    # 1. Reuse the cached edge playlist URL so 5s hls.js polls stay on one edge session.
    cached = self._hls_edge_urls.get(chno)
    if cached is not None:
      edge_url, ts = cached
      if time.time() - ts < 60.0:
        try:
          status, final_url, text = await self._fetch_text(edge_url)
          if status == 200 and "#EXTM3U" in text:
            self._hls_edge_urls[chno] = (final_url, time.time())
            return self._manifest_response(
              rewrite_hls_manifest(text, final_url, chno)
            )
        except UPSTREAM_ERRORS:
          pass
      self._hls_edge_urls.pop(chno, None)

    # 2. Acquire a fresh HLS session across candidate URLs (skipping black.ts placeholders).
    last_err = "Upstream HLS unavailable"
    for idx, raw_src in enumerate(cand_urls):
      if idx > 0 and self.tuner.settle_sec > 0:
        await asyncio.sleep(self.tuner.settle_sec)
      hls_url = _to_hls_url(raw_src)
      try:
        status, final_url, manifest_text = await self._fetch_text(hls_url)
      except UPSTREAM_ERRORS as exc:
        last_err = f"HLS upstream error: {exc}"
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
        self._hls_edge_urls[chno] = (final_url, time.time())
      return self._manifest_response(
        rewrite_hls_manifest(manifest_text, final_url, chno)
      )

    return json_response({"error": last_err}, status=HTTPStatus.BAD_GATEWAY)

  @staticmethod
  def _manifest_response(manifest: str) -> web.Response:
    return bytes_response(
      manifest.encode("utf-8"), "application/vnd.apple.mpegurl; charset=utf-8"
    )

  async def _handle_hls_asset(self, request: web.Request) -> web.StreamResponse:
    chno = int(request.match_info["chno"])
    token = request.query.get("u", "")
    if not token:
      return json_response(
        {"error": "Missing asset token"}, status=HTTPStatus.BAD_REQUEST
      )
    try:
      asset_url = _decode_url_token(token)
    except (ValueError, UnicodeDecodeError):
      return json_response(
        {"error": "Invalid asset token"}, status=HTTPStatus.BAD_REQUEST
      )

    response: web.StreamResponse | None = None
    try:
      async with self.client.get(upstream_url(asset_url)) as resp:
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
              rewrite_hls_manifest(text, str(resp.url), chno)
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
        await response.prepare(request)
        async for chunk in resp.content.iter_chunked(32768):
          await response.write(chunk)
        await response.write_eof()
        return response
    except UPSTREAM_ERRORS as exc:
      if response is not None:
        return response
      return json_response(
        {"error": f"HLS segment error: {exc}"}, status=HTTPStatus.BAD_GATEWAY
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
