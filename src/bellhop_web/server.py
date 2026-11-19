"""Async HTTP web server and single-tuner stream relay for the Bellhop Web Guide."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import mimetypes
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import parse_qs, urljoin

import httpx

from bellhop_web.auth import (
  SESSION_COOKIE_NAME,
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
  HttpRequest,
  detect_device_ip,
  send_bytes,
  send_json,
  send_redirect,
  serve_http_connection,
  write_chunk,
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
  "run_web_server",
]

logger = logging.getLogger("bellhop_web")


class TunerActiveInfoDict(TypedDict):
  """Telemetry snapshot of the single active live-stream tuner session."""

  session_id: int
  chno: int
  channel_name: str
  mode: str
  uptime_sec: float


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
  """Async HTTP/1.1 application serving the Bell Fibe Web Guide UI, EPG APIs, and stream proxy."""

  def __init__(
    self,
    store: LineupGuideStore,
    *,
    settle_sec: float = 0.18,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    static_dir: Path = STATIC_DIR,
    epg_refresher: EpgRefreshService | None = None,
    auth: WebAuthManager | None = None,
    config_reader: Callable[[], dict[str, Any]] | None = None,
    config_writer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
  ) -> None:
    self.store = store
    self.static_dir = static_dir
    self.tuner = TunerSessionManager(settle_sec=settle_sec)
    self.epg_refresher = epg_refresher
    self.auth = auth
    self._config_reader = config_reader
    self._config_writer = config_writer
    self._transport = upstream_transport
    self._client: httpx.AsyncClient | None = None
    # Active HLS edge playlist URL cache: chno -> (resolved_edge_url, last_used_epoch)
    self._hls_edge_urls: dict[int, tuple[str, float]] = {}
    self._hls_owner_client: str = ""

  async def startup(self) -> None:
    if self._client is None:
      self._client = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=8.0, read=15.0),
        headers={"User-Agent": UPSTREAM_USER_AGENT, "Icy-MetaData": "0"},
        follow_redirects=True,
        transport=self._transport,
      )
    if self.epg_refresher is not None:
      await self.epg_refresher.start()

  async def shutdown(self) -> None:
    if self.epg_refresher is not None:
      await self.epg_refresher.stop()
    await self.tuner.release_all()
    if self._client is not None:
      await self._client.aclose()
      self._client = None

  def _epg_status_dict(
    self, snap: LineupSnapshot, *, now_ts: int | None = None
  ) -> EpgStatusDict:
    if self.epg_refresher is not None:
      return self.epg_refresher.status_dict(now_ts=now_ts)
    return EpgRefreshStatus.disabled_for_snapshot(snap, now_ts=now_ts).to_dict()

  @property
  def client(self) -> httpx.AsyncClient:
    assert self._client is not None, (
      "WebGuideApp.startup() must be called before serving."
    )
    return self._client

  async def handle_connection(
    self,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
  ) -> None:
    async def _on_req(req: HttpRequest, w: asyncio.StreamWriter) -> None:
      await self.dispatch(
        req.method,
        req.path,
        req.query,
        w,
        headers=req.headers,
        body=req.body,
        raw_target=req.raw_target,
      )

    await serve_http_connection(reader, writer, _on_req, logger=logger)

  async def dispatch(
    self,
    method: str,
    path: str,
    query: dict[str, list[str]],
    writer: asyncio.StreamWriter,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    raw_target: str | None = None,
  ) -> None:
    """Route a parsed HTTP request to the Web Guide UI, Auth API, EPG API, or stream relay."""
    await self._route(
      method,
      path,
      query,
      writer,
      headers=headers or {},
      body=body,
      raw_target=raw_target or path,
    )

  async def _handle_auth_login(
    self,
    query: dict[str, list[str]],
    headers: dict[str, str],
    body: bytes,
    writer: asyncio.StreamWriter,
  ) -> None:
    if self.auth is None or not self.auth.enabled:
      await self._send_json(
        writer,
        HTTPStatus.OK,
        {"auth_required": False, "authenticated": True, "username": None},
      )
      return

    ctype = (headers.get("content-type") or "").lower()
    is_json = "application/json" in ctype
    username = ""
    password = ""
    next_url = (query.get("next") or ["/"])[0]

    if is_json and body:
      try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
        if isinstance(payload, dict):
          username = str(payload.get("username") or "")
          password = str(payload.get("password") or "")
          if payload.get("next"):
            next_url = str(payload["next"])
      except json.JSONDecodeError:
        await self._send_json(
          writer, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"}
        )
        return
    else:
      form = parse_qs(body.decode("utf-8", errors="replace")) if body else {}
      username = (form.get("username") or query.get("username") or [""])[0]
      password = (form.get("password") or query.get("password") or [""])[0]
      if form.get("next"):
        next_url = form["next"][0]

    if not self.auth.verify_credentials(username, password):
      if is_json:
        await self._send_json(
          writer,
          HTTPStatus.UNAUTHORIZED,
          {"authenticated": False, "error": "Invalid username or password"},
        )
      else:
        await self._send_bytes(
          writer,
          HTTPStatus.UNAUTHORIZED,
          render_login_page_html(
            next_url=next_url,
            error_message="Invalid username or password.",
          ),
          "text/html; charset=utf-8",
        )
      return

    token, session = self.auth.create_session_token(username)
    set_cookie = self.auth.build_set_cookie_header(token)
    if is_json:
      await self._send_json(
        writer,
        HTTPStatus.OK,
        {
          "authenticated": True,
          "username": session.username,
          "expires_at": session.expires_at,
        },
        extra_headers={"Set-Cookie": set_cookie},
      )
    else:
      await self._send_redirect(
        writer,
        sanitize_next_url(next_url),
        extra_headers={"Set-Cookie": set_cookie},
      )

  async def _route(
    self,
    method: str,
    path: str,
    query: dict[str, list[str]],
    writer: asyncio.StreamWriter,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    raw_target: str | None = None,
  ) -> None:
    req_headers = headers or {}
    req_target = raw_target or path
    auth_enabled = self.auth is not None and self.auth.enabled
    session = (
      self.auth.extract_session_from_headers(req_headers)
      if (self.auth is not None and auth_enabled)
      else None
    )

    # Public brand, favicon & client-prefs assets (available before login for /login)
    if method == "GET" and path in (
      "/favicon.ico",
      "/static/favicon.ico",
      "/favicon.svg",
      "/static/bellhop-logo.svg",
      "/static/prefs.js",
    ):
      if path.endswith(".ico"):
        asset_name = "favicon.ico"
      elif path.endswith("prefs.js"):
        asset_name = "prefs.js"
      else:
        asset_name = "bellhop-logo.svg"
      await self._serve_static_file(writer, self.static_dir / asset_name)
      return

    # Public authentication endpoints
    if path == "/login" and method == "GET":
      next_url = sanitize_next_url((query.get("next") or ["/"])[0])
      if not auth_enabled or session is not None:
        await self._send_redirect(writer, next_url)
        return
      await self._send_bytes(
        writer,
        HTTPStatus.OK,
        render_login_page_html(next_url=next_url),
        "text/html; charset=utf-8",
      )
      return

    if path == "/api/auth/status" and method == "GET":
      await self._send_json(
        writer,
        HTTPStatus.OK,
        {
          "auth_required": auth_enabled,
          "authenticated": (not auth_enabled) or (session is not None),
          "username": session.username if session is not None else None,
          "expires_at": session.expires_at if session is not None else None,
        },
      )
      return

    if path == "/api/auth/login" and method == "POST":
      await self._handle_auth_login(query, req_headers, body, writer)
      return

    if path == "/api/auth/logout" and method in ("POST", "GET"):
      client_id = (query.get("client") or [""])[0].strip() or None
      if client_id:
        await self.tuner.release_all(client_id=client_id)
      extra = (
        {"Set-Cookie": self.auth.build_clear_cookie_header()}
        if self.auth is not None
        else None
      )
      await self._send_json(
        writer,
        HTTPStatus.OK,
        {"authenticated": False},
        extra_headers=extra,
      )
      return

    # Enforce Web Guide authentication when configured
    renewal_headers: dict[str, str] | None = None
    if auth_enabled:
      assert self.auth is not None
      if session is None:
        clear_hdr = (
          {"Set-Cookie": self.auth.build_clear_cookie_header()}
          if f"{SESSION_COOKIE_NAME}=" in (req_headers.get("cookie") or "")
          else None
        )
        if path in ("/", "/index.html"):
          await self._send_redirect(
            writer,
            self.auth.login_redirect_location(req_target),
            extra_headers=clear_hdr,
          )
        else:
          await self._send_json(
            writer,
            HTTPStatus.UNAUTHORIZED,
            {"error": "Authentication required", "login_url": "/login"},
            extra_headers=clear_hdr,
          )
        return
      if session.needs_renewal():
        new_tok, _ = self.auth.create_session_token(session.username)
        renewal_headers = {
          "Set-Cookie": self.auth.build_set_cookie_header(new_tok)
        }

    if path in ("/", "/index.html"):
      await self._serve_static_file(
        writer,
        self.static_dir / "index.html",
        extra_headers=renewal_headers,
      )
      return

    if path.startswith("/static/"):
      rel = path[len("/static/") :]
      target = (self.static_dir / rel).resolve()
      if not str(target).startswith(str(self.static_dir.resolve())):
        await self._send_json(
          writer, HTTPStatus.FORBIDDEN, {"error": "Forbidden"}
        )
        return
      await self._serve_static_file(writer, target)
      return

    if path == "/api/config" and method == "GET":
      if self._config_reader is None:
        await self._send_json(
          writer,
          HTTPStatus.OK,
          {"available": False, "config": None, "config_file": None},
          extra_headers=renewal_headers,
        )
        return
      payload = self._config_reader()
      await self._send_json(
        writer,
        HTTPStatus.OK,
        payload,
        extra_headers=renewal_headers,
      )
      return

    if path == "/api/config" and method in ("PUT", "POST"):
      if self._config_writer is None:
        await self._send_json(
          writer,
          HTTPStatus.NOT_IMPLEMENTED,
          {"error": "Configuration editing is not enabled in this mode."},
        )
        return
      try:
        updates = (
          json.loads(body.decode("utf-8", errors="replace")) if body else {}
        )
        if not isinstance(updates, dict):
          raise ValueError("Request body must be a JSON object.")
        result = self._config_writer(updates)
        await self._send_json(
          writer,
          HTTPStatus.OK,
          result,
          extra_headers=renewal_headers,
        )
      except (json.JSONDecodeError, ValueError) as exc:
        await self._send_json(
          writer,
          HTTPStatus.BAD_REQUEST,
          {"error": str(exc)},
        )
      return

    if path == "/api/health":
      snap = self.store.get_snapshot()
      total_progs, active_progs, _max_stop = snap.epg_coverage_stats()
      await self._send_json(
        writer,
        HTTPStatus.OK,
        {
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
          "auth_required": auth_enabled,
        },
        extra_headers=renewal_headers,
      )
      return

    if path == "/api/epg/refresh" and method in ("POST", "GET"):
      if self.epg_refresher is None:
        snap = self.store.get_snapshot(force_reload=True)
        await self._send_json(
          writer,
          HTTPStatus.OK,
          {"refreshed": False, "epg_status": self._epg_status_dict(snap)},
        )
        return
      status = await self.epg_refresher.refresh_now(force=True, reason="manual")
      http_status = (
        HTTPStatus.OK
        if status.get("state") != "error"
        else HTTPStatus.BAD_GATEWAY
      )
      await self._send_json(
        writer,
        http_status,
        {"refreshed": status.get("state") == "ok", "epg_status": status},
      )
      return

    if path == "/api/tuner/release" and method in ("POST", "GET"):
      client_id = (query.get("client") or [""])[0].strip() or None
      if (
        not client_id
        or not self._hls_owner_client
        or self._hls_owner_client == client_id
      ):
        self._hls_edge_urls.clear()
        self._hls_owner_client = ""
      released = await self.tuner.release_all(client_id=client_id)
      await self._send_json(writer, HTTPStatus.OK, {"released": released})
      return

    if path == "/api/lineup" and method == "GET":
      await self._handle_lineup(query, writer)
      return

    m_epg = re.match(r"^/api/epg/(\d+)$", path)
    if m_epg and method == "GET":
      await self._handle_channel_epg(int(m_epg.group(1)), writer)
      return

    m_ts = re.match(r"^/api/stream/(\d+)(?:\.ts)?$", path)
    if m_ts and method in ("GET", "HEAD"):
      client_id = (query.get("client") or [""])[0].strip()
      await self._handle_mpegts_stream(
        int(m_ts.group(1)), method, writer, client_id=client_id
      )
      return

    m_hls_idx = re.match(r"^/api/hls/(\d+)/index\.m3u8$", path)
    if m_hls_idx and method == "GET":
      client_id = (query.get("client") or [""])[0].strip()
      await self._handle_hls_index(
        int(m_hls_idx.group(1)), writer, client_id=client_id
      )
      return

    m_hls_seg = re.match(r"^/api/hls/(\d+)/asset$", path)
    if m_hls_seg and method == "GET":
      tok = (query.get("u") or [""])[0]
      await self._handle_hls_asset(int(m_hls_seg.group(1)), tok, writer)
      return

    await self._send_json(
      writer, HTTPStatus.NOT_FOUND, {"error": f"Not found: {path}"}
    )

  async def _handle_lineup(
    self,
    query: dict[str, list[str]],
    writer: asyncio.StreamWriter,
  ) -> None:
    if self.epg_refresher is not None:
      self.epg_refresher.trigger_background_if_needed(reason_prefix="lineup")
    snap = self.store.get_snapshot()
    now_ts = int(time.time())

    cat_filter = (query.get("category") or [""])[0].strip()
    lang_filter = (query.get("lang") or [""])[0].strip().lower()
    epg_filter = (
      (query.get("has_epg") or query.get("epg") or [""])[0].strip().lower()
    )
    require_epg = epg_filter in {"1", "true", "yes", "only"}
    search_q = (query.get("q") or [""])[0].strip().lower()

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

    payload = {
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
    await self._send_json(writer, HTTPStatus.OK, payload)

  async def _handle_channel_epg(
    self, chno: int, writer: asyncio.StreamWriter
  ) -> None:
    if self.epg_refresher is not None:
      self.epg_refresher.trigger_background_if_needed(
        reason_prefix="channel_epg"
      )
    snap = self.store.get_snapshot()
    ch = snap.channels_by_chno.get(chno)
    if ch is None:
      await self._send_json(
        writer, HTTPStatus.NOT_FOUND, {"error": f"Unknown channel {chno}"}
      )
      return

    now_ts = int(time.time())
    progs = snap.programs_by_epg_id.get(ch.epg_channel_id, [])
    upcoming = [
      p.to_dict(now_ts=now_ts)
      for p in progs
      if p.stop_timestamp == 0 or p.stop_timestamp > now_ts - 1800
    ]
    await self._send_json(
      writer,
      HTTPStatus.OK,
      {
        "chno": ch.chno,
        "name": ch.name,
        "epg_channel_id": ch.epg_channel_id,
        "programs": upcoming,
      },
    )

  @staticmethod
  def _candidate_urls_for(ch: WebChannel) -> list[str]:
    urls: list[str] = []
    for u in [ch.direct_source, *ch.fallback_sources]:
      if u and "***" not in u and u not in urls:
        urls.append(u)
    return urls

  @staticmethod
  def _is_offline_placeholder(final_url: str, headers: httpx.Headers) -> bool:
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

  _write_chunk = staticmethod(write_chunk)

  async def _handle_mpegts_stream(
    self,
    chno: int,
    method: str,
    writer: asyncio.StreamWriter,
    *,
    client_id: str = "",
  ) -> None:
    snap = self.store.get_snapshot()
    ch: WebChannel | None = snap.channels_by_chno.get(chno)
    cand_urls = self._candidate_urls_for(ch) if ch is not None else []
    if ch is None or not cand_urls:
      await self._send_json(
        writer,
        HTTPStatus.NOT_FOUND,
        {"error": f"Channel {chno} has no playable stream URL"},
      )
      return

    if method == "HEAD":
      await self._send_bytes(writer, HTTPStatus.OK, b"", "video/mp2t")
      return

    self._hls_edge_urls.clear()
    self._hls_owner_client = ""
    current_task = asyncio.current_task()
    session = await self.tuner.preempt_and_acquire(
      chno=ch.chno,
      channel_name=ch.name,
      mode="mpegts",
      task=current_task,
      client_id=client_id,
    )

    headers_sent = False
    last_err = "No candidate stream succeeded"
    try:
      for idx, target_url in enumerate(cand_urls):
        if session.cancel_event.is_set() or writer.is_closing():
          return
        if idx > 0 and self.tuner.settle_sec > 0:
          await asyncio.sleep(self.tuner.settle_sec)

        try:
          async with self.client.stream("GET", target_url) as resp:
            if resp.status_code not in (200, 206):
              last_err = f"Upstream HTTP {resp.status_code}"
              continue

            if (
              self._is_offline_placeholder(str(resp.url), resp.headers)
              and idx < len(cand_urls) - 1
            ):
              last_err = (
                f"Stream redirected to offline placeholder ({resp.url})"
              )
              continue

            ctype = resp.headers.get("content-type") or "video/mp2t"
            if "mpegurl" in ctype.lower():
              ctype = "application/vnd.apple.mpegurl"
            elif "octet-stream" in ctype.lower() or not ctype:
              ctype = "video/mp2t"

            header_block = (
              f"HTTP/1.1 200 OK\r\n"
              f"Content-Type: {ctype}\r\n"
              f"Transfer-Encoding: chunked\r\n"
              f"Cache-Control: no-store, no-cache, must-revalidate\r\n"
              f"Pragma: no-cache\r\n"
              f"X-Accel-Buffering: no\r\n"
              f"Access-Control-Allow-Origin: *\r\n"
              f"Connection: close\r\n\r\n"
            ).encode("latin-1")
            writer.write(header_block)
            await writer.drain()
            headers_sent = True

            async for chunk in resp.aiter_bytes(chunk_size=16384):
              if session.cancel_event.is_set() or writer.is_closing():
                break
              if chunk:
                await self._write_chunk(writer, chunk)

            if not writer.is_closing():
              writer.write(b"0\r\n\r\n")
              await writer.drain()
            return
        except httpx.HTTPError as exc:
          last_err = f"Upstream connection error: {exc}"
          if headers_sent:
            return
          continue

      if not headers_sent:
        await self._send_json(
          writer,
          HTTPStatus.BAD_GATEWAY,
          {"error": last_err, "chno": ch.chno},
        )
    except asyncio.CancelledError:
      return
    except (ConnectionResetError, BrokenPipeError):
      return
    finally:
      await self.tuner.mark_closed(session)

  async def _handle_hls_index(
    self,
    chno: int,
    writer: asyncio.StreamWriter,
    *,
    client_id: str = "",
  ) -> None:
    snap = self.store.get_snapshot()
    ch = snap.channels_by_chno.get(chno)
    cand_urls = self._candidate_urls_for(ch) if ch is not None else []
    if ch is None or not cand_urls:
      await self._send_json(
        writer,
        HTTPStatus.NOT_FOUND,
        {"error": f"Channel {chno} not found"},
      )
      return

    # Close any continuous .ts connection and drop stale HLS sessions for OTHER channels
    await self.tuner.release_all()
    if client_id:
      self._hls_owner_client = client_id
    for other_ch in list(self._hls_edge_urls):
      if other_ch != chno:
        self._hls_edge_urls.pop(other_ch, None)

    # 1. Try active cached edge playlist URL first (so 5s hls.js polls stay on the same edge session)
    cached = self._hls_edge_urls.get(chno)
    if cached is not None:
      edge_url, ts = cached
      if time.time() - ts < 60.0:
        try:
          resp = await self.client.get(edge_url)
          if resp.status_code == 200 and "#EXTM3U" in resp.text:
            self._hls_edge_urls[chno] = (str(resp.url), time.time())
            rewritten = rewrite_hls_manifest(resp.text, str(resp.url), chno)
            await self._send_bytes(
              writer,
              HTTPStatus.OK,
              rewritten.encode("utf-8"),
              "application/vnd.apple.mpegurl; charset=utf-8",
            )
            return
        except httpx.HTTPError:
          pass
      self._hls_edge_urls.pop(chno, None)

    # 2. Acquire fresh HLS session across candidate URLs (skipping black.ts placeholders)
    last_err = "Upstream HLS unavailable"
    for idx, raw_src in enumerate(cand_urls):
      if idx > 0 and self.tuner.settle_sec > 0:
        await asyncio.sleep(self.tuner.settle_sec)
      hls_url = _to_hls_url(raw_src)
      try:
        resp = await self.client.get(hls_url)
        if resp.status_code != 200:
          last_err = f"Upstream HLS returned HTTP {resp.status_code}"
          continue
        manifest_text = resp.text
        if "#EXTM3U" not in manifest_text:
          last_err = "Invalid M3U8 response"
          continue
        if "/black.ts" in manifest_text.lower() and idx < len(cand_urls) - 1:
          last_err = "Stream redirected to offline black.ts placeholder"
          continue
        final_edge_url = str(resp.url)
        if final_edge_url != hls_url:
          self._hls_edge_urls[chno] = (final_edge_url, time.time())
        rewritten = rewrite_hls_manifest(manifest_text, final_edge_url, chno)
        await self._send_bytes(
          writer,
          HTTPStatus.OK,
          rewritten.encode("utf-8"),
          "application/vnd.apple.mpegurl; charset=utf-8",
        )
        return
      except httpx.HTTPError as exc:
        last_err = f"HLS upstream error: {exc}"

    await self._send_json(writer, HTTPStatus.BAD_GATEWAY, {"error": last_err})

  async def _handle_hls_asset(
    self, chno: int, token: str, writer: asyncio.StreamWriter
  ) -> None:
    if not token:
      await self._send_json(
        writer, HTTPStatus.BAD_REQUEST, {"error": "Missing asset token"}
      )
      return
    try:
      upstream_url = _decode_url_token(token)
    except Exception:
      await self._send_json(
        writer, HTTPStatus.BAD_REQUEST, {"error": "Invalid asset token"}
      )
      return

    try:
      async with self.client.stream("GET", upstream_url) as resp:
        if resp.status_code not in (200, 206):
          await self._send_json(
            writer,
            HTTPStatus.BAD_GATEWAY,
            {"error": f"HLS asset HTTP {resp.status_code}"},
          )
          return
        ctype = resp.headers.get("content-type", "").lower()
        is_playlist = "mpegurl" in ctype or upstream_url.split("?", 1)[
          0
        ].lower().endswith(".m3u8")
        if is_playlist:
          body = await resp.aread()
          text = body.decode("utf-8", errors="replace")
          if "#EXTM3U" in text:
            rewritten = rewrite_hls_manifest(text, str(resp.url), chno)
            await self._send_bytes(
              writer,
              HTTPStatus.OK,
              rewritten.encode("utf-8"),
              "application/vnd.apple.mpegurl; charset=utf-8",
            )
            return
          await self._send_bytes(
            writer, HTTPStatus.OK, body, ctype or "application/octet-stream"
          )
          return

        # Stream binary TS/fMP4 HLS segment via HTTP/1.1 chunked encoding
        out_ct = resp.headers.get("content-type") or "video/mp2t"
        hdr = (
          f"HTTP/1.1 200 OK\r\n"
          f"Content-Type: {out_ct}\r\n"
          f"Transfer-Encoding: chunked\r\n"
          f"Cache-Control: no-store\r\n"
          f"Access-Control-Allow-Origin: *\r\n"
          f"Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(hdr)
        await writer.drain()
        async for chunk in resp.aiter_bytes(chunk_size=32768):
          if writer.is_closing():
            break
          if chunk:
            await self._write_chunk(writer, chunk)
        if not writer.is_closing():
          writer.write(b"0\r\n\r\n")
          await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
      return
    except httpx.HTTPError as exc:
      await self._send_json(
        writer,
        HTTPStatus.BAD_GATEWAY,
        {"error": f"HLS segment error: {exc}"},
      )

  async def _serve_static_file(
    self,
    writer: asyncio.StreamWriter,
    file_path: Path,
    *,
    extra_headers: dict[str, str] | None = None,
  ) -> None:
    if not file_path.is_file():
      await self._send_json(
        writer, HTTPStatus.NOT_FOUND, {"error": "Static file not found"}
      )
      return
    mime, _ = mimetypes.guess_type(str(file_path))
    content_type = mime or "application/octet-stream"
    if content_type.startswith("text/") or content_type in (
      "application/javascript",
      "application/json",
    ):
      content_type = f"{content_type}; charset=utf-8"
    data = file_path.read_bytes()
    cache_hdr = (
      "public, max-age=3600"
      if ("vendor/" in str(file_path) or file_path.suffix in (".svg", ".ico"))
      else "no-cache"
    )
    await self._send_bytes(
      writer,
      HTTPStatus.OK,
      data,
      content_type,
      cache_control=cache_hdr,
      extra_headers=extra_headers,
    )

  _send_redirect = staticmethod(send_redirect)
  _send_json = staticmethod(send_json)
  _send_bytes = staticmethod(send_bytes)


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


async def run_web_server(
  app: WebGuideApp,
  *,
  host: str = "0.0.0.0",
  port: int = 8080,
) -> None:
  """Run the async HTTP server until cancelled or interrupted."""
  bind_host = resolve_bind_host(host)
  await app.startup()
  server = await asyncio.start_server(
    app.handle_connection, host=bind_host, port=port
  )
  try:
    async with server:
      await server.serve_forever()
  finally:
    await app.shutdown()
