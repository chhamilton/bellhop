"""Passthrough Xtream Codes & SQLite EPG server (`bellhop serve`, aiohttp).

Xtream Codes HTTP Contract & Wire Layouts
-----------------------------------------
- `GET/POST /player_api.php` (and `/player.php`):
  - No `action`: returns `{"user_info": {...}, "server_info": {...}}`
    (`{"user_info": {"auth": 0}}` on invalid credentials, rate-limited per IP).
  - `action=get_live_categories`: returns `list[PassthroughCategoryDict]`.
  - `action=get_live_streams` (+ optional `category_id`): returns
    `list[PlayerApiStreamDict]` (`direct_source` is always `""` so upstream
    credentials never leak).
  - `action=get_short_epg` / `get_simple_data_table`: serves lineup-filtered,
    base64-encoded UTF-8 listings locally from `cache/epg.sqlite3` (`limit`
    clamped to `1..200`).
  - VOD / Series actions return valid empty Xtream structures (`[]` or `{...}`).
- `GET/POST /panel_api.php`: returns unified `user_info`, `server_info`,
  `categories.live`, and `available_channels` keyed by channel number.
- `GET/HEAD /get.php`, `/playlist.m3u`, `/playlist.m3u8`: dynamically renders
  `m3u` or `m3u_plus` playlists (`output=ts` or `hls`/`m3u8`, optional
  `category_id` filter) pointing at this server's `/live/<user>/<pass>/<id>`
  and `/xmltv.php` endpoints.
- `GET/HEAD /xmltv.php`, `/epg.xml`: streams lineup-filtered XMLTV directly
  from `cache/epg.sqlite3` via keyset pagination (`302` redirecting to the
  upstream XMLTV URL only as a fallback when SQLite has 0 listings).
- `GET/HEAD /live/<user>/<pass>/<id>[.<ext>]`, `/<user>/<pass>/<id>[.<ext>]`:
  authenticates `xtream_server` credentials and issues an HTTP `302` redirect
  to the upstream IPTV stream URL.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, TypedDict

from aiohttp import web

from bellhop.common.export_bundle import ExportBundleReader, as_str_dict
from bellhop.common.http_utils import (
  DEFAULT_TRUSTED_PROXY_IPS,
  IpNetwork,
  LoginRateLimiter,
  build_access_urls,
  bytes_response,
  create_web_app,
  detect_device_ip,
  json_response,
  parse_trusted_proxy_networks,
  redirect_response,
  resolve_bind_host,
  resolve_request_client_ip,
  start_site,
)
from bellhop.config import AppConfig
from bellhop.epg.xmltv import build_passthrough_xmltv_url

__all__ = [
  "PassthroughCategory",
  "PassthroughCategoryDict",
  "PassthroughStream",
  "PlayerApiStreamDict",
  "XtreamExportSnapshot",
  "XtreamExportStore",
  "XtreamPassthroughServer",
  "build_access_urls",
  "detect_device_ip",
  "resolve_bind_host",
]

logger = logging.getLogger("bellhop.serve")

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class PassthroughCategoryDict(TypedDict):
  """Xtream `get_live_categories` wire item."""

  category_id: str
  category_name: str
  parent_id: int


class PlayerApiStreamDict(TypedDict):
  """Public Xtream `get_live_streams` wire item (`direct_source` redacted)."""

  num: int
  name: str
  stream_type: str
  stream_id: int
  stream_icon: str
  epg_channel_id: str
  added: str
  is_adult: int
  category_id: str
  category_ids: list[int]
  custom_sid: str
  tv_archive: int
  direct_source: str
  tv_archive_duration: int


def _as_dict(val: object) -> dict[str, Any]:
  return as_str_dict(val)


def _rewrite_stream_extension(upstream_url: str, requested_ext: str) -> str:
  """Adapt `.ts` / `.m3u8` extension on an upstream stream URL when requested by
  a client.
  """
  ext = requested_ext.strip().lstrip(".").lower()
  if not ext or ext not in {"ts", "m3u8"}:
    return upstream_url
  if re.search(r"\.(?:ts|m3u8)(?:\?|$)", upstream_url, flags=re.IGNORECASE):
    return re.sub(
      r"\.(?:ts|m3u8)(\?.*)?$",
      f".{ext}\\1",
      upstream_url,
      flags=re.IGNORECASE,
    )
  return f"{upstream_url.rstrip('/')}.{ext}"


def _escape_m3u_attr(value: str) -> str:
  return value.replace('"', "'").replace("\n", " ").replace("\r", " ").strip()


@dataclass(slots=True)
class PassthroughCategory:
  """Normalized lineup category served by `XtreamPassthroughServer`."""

  category_id: str
  category_name: str
  parent_id: int = 0

  def to_dict(self) -> PassthroughCategoryDict:
    return {
      "category_id": self.category_id,
      "category_name": self.category_name,
      "parent_id": self.parent_id,
    }


@dataclass(slots=True)
class PassthroughStream:
  """Normalized mapped channel with internal upstream passthrough URL."""

  num: int
  name: str
  stream_id: int
  stream_icon: str
  epg_channel_id: str
  added: str
  category_id: str
  category_ids: list[int]
  category_name: str
  custom_sid: str
  upstream_url: str
  stream_type: str = "live"
  is_adult: int = 0
  tv_archive: int = 0
  tv_archive_duration: int = 0
  language: str = ""
  gracenote_station_id: str = ""

  def to_player_api_dict(self) -> PlayerApiStreamDict:
    """Serialize public Xtream `player_api.php` stream item (never exposing
    `upstream_url`).
    """
    return {
      "num": self.num,
      "name": self.name,
      "stream_type": self.stream_type,
      "stream_id": self.stream_id,
      "stream_icon": self.stream_icon,
      "epg_channel_id": self.epg_channel_id,
      "added": self.added,
      "is_adult": self.is_adult,
      "category_id": self.category_id,
      "category_ids": list(self.category_ids),
      "custom_sid": self.custom_sid,
      "tv_archive": self.tv_archive,
      "direct_source": "",
      "tv_archive_duration": self.tv_archive_duration,
    }


@dataclass(slots=True)
class XtreamExportSnapshot:
  """Hot-reloadable snapshot of Phase (6) exported lineup categories, streams,
  and passthrough URLs.
  """

  lineup_id: str = "CAN-BELLFIBE-MTL-X"
  provider: str = "Bell Fibe TV"
  market: str = "Montréal, QC"
  timezone: str = "America/Montreal"
  max_connections: str = "1"
  allowed_output_formats: list[str] = field(
    default_factory=lambda: ["m3u8", "ts"]
  )
  exp_date: str | None = None
  is_trial: str = "0"
  xmltv_url: str = ""
  categories: list[PassthroughCategory] = field(default_factory=list)
  streams: list[PassthroughStream] = field(default_factory=list)
  stream_by_id: dict[int, PassthroughStream] = field(default_factory=dict)
  stream_by_num: dict[int, PassthroughStream] = field(default_factory=dict)
  loaded_at: float = 0.0


class XtreamExportStore:
  """Reads Phase (6) `output/xtream/` artifacts with automatic mtime-based hot
  reload.
  """

  def __init__(
    self,
    xtream_dir: Path,
    *,
    m3u8_path: Path | None = None,
    fallback_xmltv_url: str = "",
    bundle_reader: ExportBundleReader | None = None,
  ) -> None:
    self.xtream_dir = xtream_dir.resolve()
    self.fallback_xmltv_url = fallback_xmltv_url
    self._bundle_reader = bundle_reader or ExportBundleReader(
      self.xtream_dir,
      m3u8_path=m3u8_path,
      fallback_xmltv_url=fallback_xmltv_url,
    )
    self._last_sig: tuple[float, ...] = ()
    self._snapshot: XtreamExportSnapshot | None = None

  def _watched_files(self) -> list[Path]:
    return self._bundle_reader.watched_files()

  def _signature(self) -> tuple[float, ...]:
    return self._bundle_reader.compute_signature()

  def get_snapshot(self, *, force_reload: bool = False) -> XtreamExportSnapshot:
    sig = self._signature()
    prev_snap = self._snapshot
    if not force_reload and prev_snap is not None and sig == self._last_sig:
      return prev_snap
    snap = self._load_from_disk(force_reload=force_reload)
    self._snapshot = snap
    self._last_sig = sig
    if prev_snap is not None:
      delta = len(snap.streams) - len(prev_snap.streams)
      delta_str = f" ({delta:+d})" if delta else ""
      logger.info(
        "Reloaded Xtream API playlist from %s: %d streams%s across %d categories [%s].",
        self.xtream_dir,
        len(snap.streams),
        delta_str,
        len(snap.categories),
        snap.lineup_id,
      )
    return snap

  def _load_from_disk(
    self, *, force_reload: bool = False
  ) -> XtreamExportSnapshot:
    _, bundle_data = self._bundle_reader.read_cached(
      force_reload=force_reload,
      fallback_xmltv_url=self.fallback_xmltv_url,
    )
    if not bundle_data.initialized:
      return XtreamExportSnapshot(
        lineup_id=bundle_data.lineup_id,
        provider=bundle_data.provider,
        market=bundle_data.market,
        timezone=bundle_data.timezone,
        xmltv_url=bundle_data.xmltv_url,
        loaded_at=bundle_data.loaded_at,
      )

    categories = [
      PassthroughCategory(
        category_id=cat.category_id,
        category_name=cat.category_name,
        parent_id=cat.parent_id,
      )
      for cat in bundle_data.categories
    ]

    streams: list[PassthroughStream] = []
    stream_by_id: dict[int, PassthroughStream] = {}
    stream_by_num: dict[int, PassthroughStream] = {}

    for rec in bundle_data.streams:
      stream_id = rec.stream_id or rec.num
      normalized = PassthroughStream(
        num=rec.num,
        name=rec.name,
        stream_type=rec.stream_type,
        stream_id=stream_id,
        stream_icon=rec.stream_icon,
        epg_channel_id=rec.epg_channel_id,
        added=rec.added,
        is_adult=rec.is_adult,
        category_id=rec.category_id,
        category_ids=list(rec.category_ids),
        category_name=rec.category_name,
        custom_sid=rec.custom_sid,
        tv_archive=rec.tv_archive,
        tv_archive_duration=rec.tv_archive_duration,
        language=rec.language,
        gracenote_station_id=rec.gracenote_station_id,
        upstream_url=rec.direct_source,
      )
      streams.append(normalized)
      stream_by_id.setdefault(stream_id, normalized)
      stream_by_num[rec.num] = normalized

    return XtreamExportSnapshot(
      lineup_id=bundle_data.lineup_id,
      provider=bundle_data.provider,
      market=bundle_data.market,
      timezone=bundle_data.timezone,
      max_connections=bundle_data.max_connections,
      allowed_output_formats=list(bundle_data.allowed_output_formats),
      exp_date=bundle_data.exp_date,
      is_trial=bundle_data.is_trial,
      xmltv_url=bundle_data.xmltv_url,
      categories=categories,
      streams=streams,
      stream_by_id=stream_by_id,
      stream_by_num=stream_by_num,
      loaded_at=bundle_data.loaded_at,
    )


class XtreamPassthroughServer:
  """aiohttp Xtream Codes API server with `302` upstream passthrough.

  Routes are registered via `add_routes()` (shared listener with the Web
  Guide) or `build_app()` / `start()` (dedicated listener).
  """

  def __init__(
    self,
    store: XtreamExportStore,
    *,
    username: str,
    password: str,
    host: str = "0.0.0.0",
    port: int = 8081,
    public_host: str | None = None,
    public_port: int | None = None,
    server_protocol: str = "http",
    login_limiter: LoginRateLimiter | None = None,
    trust_proxy_headers: bool = False,
    trusted_proxy_ips: Sequence[str] = DEFAULT_TRUSTED_PROXY_IPS,
    epg_xmltv_chunks_provider: Callable[[], Awaitable[Iterator[bytes] | None]]
    | None = None,
    epg_short_provider: Callable[
      [int, str, str, int], Awaitable[list[dict[str, Any]]]
    ]
    | None = None,
  ) -> None:
    self.store = store
    self.username = username
    self.password = password
    self.bind_host = resolve_bind_host(host)
    self.port = port
    self.public_host = public_host
    self.public_port = public_port
    self.server_protocol = server_protocol
    self.login_limiter: LoginRateLimiter = login_limiter or LoginRateLimiter()
    self.trust_proxy_headers = bool(trust_proxy_headers)
    self._trusted_proxy_networks: tuple[IpNetwork, ...] = (
      parse_trusted_proxy_networks(trusted_proxy_ips)
    )
    self.epg_xmltv_chunks_provider = epg_xmltv_chunks_provider
    self.epg_short_provider = epg_short_provider
    self._runner: web.AppRunner | None = None
    self._created_at = int(time.time())

  @classmethod
  def from_config(cls, config: AppConfig) -> XtreamPassthroughServer:
    xtream_dir = config.resolve_path(config.output.xtream_dir)
    fallback_xmltv = build_passthrough_xmltv_url(config, redact_secret=False)
    store = XtreamExportStore(xtream_dir, fallback_xmltv_url=fallback_xmltv)
    return cls(
      store,
      username=config.serve.username,
      password=config.serve.password,
      host=config.serve.host,
      port=config.serve.port,
      public_host=config.serve.public_host,
      public_port=config.serve.public_port,
      server_protocol=config.serve.server_protocol,
    )

  # ---------------------------------------------------------------------------
  # Lifecycle (dedicated listener)
  # ---------------------------------------------------------------------------
  @property
  def bound_port(self) -> int:
    return self.port

  def build_app(self) -> web.Application:
    """Return a standalone `aiohttp` application serving only the Xtream API."""
    app = create_web_app()
    self.add_routes(app, include_health=True)
    return app

  async def start(self) -> XtreamPassthroughServer:
    self._runner, self.port = await start_site(
      self.build_app(), self.bind_host, self.port
    )
    return self

  async def stop(self) -> None:
    if self._runner is not None:
      await self._runner.cleanup()
      self._runner = None

  async def serve_forever(self) -> None:
    await self.start()
    try:
      await asyncio.Event().wait()
    finally:
      await self.stop()

  def add_routes(
    self, app: web.Application, *, include_health: bool = False
  ) -> None:
    """Register Xtream Codes API & passthrough routes on `app`.

    Args:
      app: Target application.
      include_health: Also register `/health` and `/api/health` (only for a
        dedicated listener; the shared listener provides its own `/health`).
    """
    r = app.router
    if include_health:
      r.add_get("/health", self._handle_health)
      r.add_get("/api/health", self._handle_health)
    for path in ("/player_api.php", "/player.php"):
      r.add_get(path, self._handle_player_api)
      r.add_post(path, self._handle_player_api)
    r.add_get("/panel_api.php", self._handle_panel_api)
    r.add_post("/panel_api.php", self._handle_panel_api)
    for path in ("/xmltv.php", "/epg.xml"):
      r.add_get(path, self._handle_xmltv_passthrough)
    r.add_get("/get.php", self._m3u_handler("m3u_plus"))
    r.add_get("/playlist.m3u8", self._m3u_handler("m3u_plus"))
    r.add_get("/playlist.m3u", self._m3u_handler("m3u"))
    r.add_get(
      r"/{kind:(?:movie|series|timeshift)}/{user}/{passwd}/{tail:.+}",
      self._handle_nonlive,
    )
    stream_pattern = r"{user}/{passwd}/{sid:\d+}{ext:(?:\.[A-Za-z0-9]+)?}"
    r.add_get(f"/live/{stream_pattern}", self._handle_stream_passthrough)
    r.add_get(f"/{stream_pattern}", self._handle_stream_passthrough)

  # ---------------------------------------------------------------------------
  # Helpers
  # ---------------------------------------------------------------------------
  @staticmethod
  async def _params(request: web.Request) -> dict[str, str]:
    """Merge query parameters with urlencoded POST form fields (query wins)."""
    params: dict[str, str] = {}
    if request.method == "POST" and request.can_read_body:
      ctype = request.content_type.lower()
      if ctype in ("", "application/x-www-form-urlencoded"):
        form = await request.post()
        for key, val in form.items():
          if isinstance(val, str):
            params.setdefault(key, val)
    for key, val in request.query.items():
      params[key] = val
    return params

  def _is_authorized(self, user: str, passwd: str) -> bool:
    if not self.username or not self.password:
      return False
    return hmac.compare_digest(
      user.encode("utf-8"), self.username.encode("utf-8")
    ) and hmac.compare_digest(
      passwd.encode("utf-8"), self.password.encode("utf-8")
    )

  def _verify_request_auth(
    self,
    request: web.Request,
    user: str,
    passwd: str,
    *,
    json_fail: bool = False,
  ) -> web.Response | None:
    """Return `None` when authorized, or a `401`/`200 auth:0`/`429` response."""
    client_ip = resolve_request_client_ip(
      request,
      trust_proxy_headers=self.trust_proxy_headers,
      trusted_networks=self._trusted_proxy_networks,
    )
    retry_after = self.login_limiter.check(client_ip)
    if retry_after > 0:
      hdrs = {"Retry-After": str(retry_after)}
      if json_fail:
        return json_response(
          {"user_info": {"auth": 0}, "error": "Too many failed attempts"},
          status=HTTPStatus.TOO_MANY_REQUESTS,
          headers=hdrs,
        )
      return bytes_response(
        b"Too Many Requests",
        "text/plain",
        status=HTTPStatus.TOO_MANY_REQUESTS,
        headers=hdrs,
      )
    if not self._is_authorized(user, passwd):
      if self.username and self.password:
        self.login_limiter.record_failure(client_ip)
      if json_fail:
        return json_response({"user_info": {"auth": 0}})
      return self._unauthorized()
    self.login_limiter.record_success(client_ip)
    return None

  def _resolve_advertised_host_port(
    self, headers: Mapping[str, str]
  ) -> tuple[str, int]:
    if self.public_host:
      return self.public_host, (self.public_port or self.bound_port)

    host_hdr = (headers.get("host") or "").strip()
    if host_hdr:
      if host_hdr.startswith("["):
        m_v6 = re.match(r"^\[([^\]]+)\](?::(\d+))?$", host_hdr)
        if m_v6:
          h = m_v6.group(1)
          p = (
            int(m_v6.group(2))
            if m_v6.group(2)
            else (443 if self.server_protocol == "https" else 80)
          )
          return h, (self.public_port or p)
      elif ":" in host_hdr:
        h, p_str = host_hdr.rsplit(":", 1)
        if p_str.isdigit():
          return h, (self.public_port or int(p_str))
      return host_hdr, (
        self.public_port or (443 if self.server_protocol == "https" else 80)
      )

    fallback_host = (
      detect_device_ip() or "127.0.0.1"
      if self.bind_host in ("0.0.0.0", "::", "")
      else self.bind_host
    )
    return fallback_host, (self.public_port or self.bound_port)

  def _build_user_and_server_info(
    self,
    snap: XtreamExportSnapshot,
    headers: Mapping[str, str],
  ) -> tuple[dict[str, Any], dict[str, Any]]:
    now_epoch = int(time.time())
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    adv_host, adv_port = self._resolve_advertised_host_port(headers)

    user_info: dict[str, Any] = {
      "username": self.username,
      "password": self.password,
      "message": f"{snap.provider} ({snap.market}) [{snap.lineup_id}]",
      "auth": 1,
      "status": "Active",
      "exp_date": snap.exp_date,
      "is_trial": snap.is_trial,
      "active_cons": "0",
      "created_at": str(self._created_at),
      "max_connections": snap.max_connections,
      "allowed_output_formats": list(snap.allowed_output_formats),
    }
    server_info: dict[str, Any] = {
      "url": adv_host,
      "port": str(adv_port),
      "https_port": str(adv_port if self.server_protocol == "https" else 443),
      "server_protocol": self.server_protocol,
      "rtmp_port": "0",
      "timezone": snap.timezone,
      "timestamp_now": now_epoch,
      "time_now": now_iso,
      "process": True,
    }
    return user_info, server_info

  @staticmethod
  def _unauthorized() -> web.Response:
    return bytes_response(
      b"Unauthorized", "text/plain", status=HTTPStatus.UNAUTHORIZED
    )

  # ---------------------------------------------------------------------------
  # Handlers
  # ---------------------------------------------------------------------------
  async def _handle_health(self, _request: web.Request) -> web.StreamResponse:
    snap = self.store.get_snapshot()
    return json_response(
      {
        "status": "ok",
        "lineup_id": snap.lineup_id,
        "provider": snap.provider,
        "market": snap.market,
        "category_count": len(snap.categories),
        "channel_count": len(snap.streams),
      }
    )

  async def _handle_player_api(
    self, request: web.Request
  ) -> web.StreamResponse:
    params = await self._params(request)
    denied = self._verify_request_auth(
      request,
      params.get("username", ""),
      params.get("password", ""),
      json_fail=True,
    )
    if denied is not None:
      return denied

    snap = self.store.get_snapshot()
    action = params.get("action", "").strip().lower()

    if action == "get_live_categories":
      return json_response([c.to_dict() for c in snap.categories])

    if action == "get_live_streams":
      cat_filter = params.get("category_id", "").strip()
      return json_response(
        [
          s.to_player_api_dict()
          for s in snap.streams
          if not cat_filter or s.category_id == cat_filter
        ]
      )

    if action in {
      "get_vod_categories",
      "get_series_categories",
      "get_vod_streams",
      "get_series",
    }:
      return json_response([])
    if action == "get_vod_info":
      return json_response({"info": {}, "movie_data": {}})
    if action == "get_series_info":
      return json_response({"seasons": [], "info": {}, "episodes": {}})
    if action in {"get_short_epg", "get_simple_data_table"}:
      raw_sid = params.get("stream_id", "").strip()
      if raw_sid.isdigit() and self.epg_short_provider is not None:
        sid = int(raw_sid)
        item = snap.stream_by_id.get(sid) or snap.stream_by_num.get(sid)
        if item is not None:
          default_limit = 4 if action == "get_short_epg" else 50
          raw_limit = params.get("limit", "").strip()
          try:
            parsed_limit = int(raw_limit)
          except ValueError:
            limit = default_limit
          else:
            limit = max(1, min(200, parsed_limit))
          listings = await self.epg_short_provider(
            item.num,
            item.epg_channel_id,
            item.language,
            limit,
          )
          return json_response({"epg_listings": listings})
      return json_response({"epg_listings": []})

    user_info, server_info = self._build_user_and_server_info(
      snap, request.headers
    )
    return json_response({"user_info": user_info, "server_info": server_info})

  async def _handle_panel_api(self, request: web.Request) -> web.StreamResponse:
    params = await self._params(request)
    denied = self._verify_request_auth(
      request,
      params.get("username", ""),
      params.get("password", ""),
      json_fail=True,
    )
    if denied is not None:
      return denied

    snap = self.store.get_snapshot()
    user_info, server_info = self._build_user_and_server_info(
      snap, request.headers
    )
    return json_response(
      {
        "user_info": user_info,
        "server_info": server_info,
        "categories": {"live": [c.to_dict() for c in snap.categories]},
        "available_channels": {
          str(s.num): s.to_player_api_dict() for s in snap.streams
        },
      }
    )

  async def _handle_xmltv_passthrough(
    self, request: web.Request
  ) -> web.StreamResponse:
    params = await self._params(request)
    denied = self._verify_request_auth(
      request, params.get("username", ""), params.get("password", "")
    )
    if denied is not None:
      return denied

    if self.epg_xmltv_chunks_provider is not None:
      chunk_iter = await self.epg_xmltv_chunks_provider()
      if chunk_iter is not None:
        close_iter = getattr(chunk_iter, "close", None)
        try:
          if request.method == "HEAD":
            return web.Response(
              status=HTTPStatus.OK,
              headers={"Content-Type": "application/xml; charset=utf-8"},
            )
          resp = web.StreamResponse(
            status=HTTPStatus.OK,
            headers={"Content-Type": "application/xml; charset=utf-8"},
          )
          await resp.prepare(request)
          while True:
            chunk = await asyncio.to_thread(next, chunk_iter, None)
            if chunk is None:
              break
            if chunk:
              await resp.write(chunk)
          await resp.write_eof()
          return resp
        finally:
          if callable(close_iter):
            await asyncio.to_thread(close_iter)

    snap = self.store.get_snapshot()
    if not snap.xmltv_url:
      return json_response(
        {"error": "No upstream XMLTV passthrough URL configured."},
        status=HTTPStatus.NOT_FOUND,
      )
    logger.debug("Passthrough redirect /xmltv.php -> upstream XMLTV")
    return redirect_response(snap.xmltv_url)

  async def _handle_nonlive(self, request: web.Request) -> web.StreamResponse:
    denied = self._verify_request_auth(
      request, request.match_info["user"], request.match_info["passwd"]
    )
    if denied is not None:
      return denied
    return bytes_response(
      b"Not found", "text/plain", status=HTTPStatus.NOT_FOUND
    )

  async def _handle_stream_passthrough(
    self, request: web.Request
  ) -> web.StreamResponse:
    info = request.match_info
    denied = self._verify_request_auth(request, info["user"], info["passwd"])
    if denied is not None:
      return denied

    stream_key = int(info["sid"])
    snap = self.store.get_snapshot()
    item = snap.stream_by_id.get(stream_key) or snap.stream_by_num.get(
      stream_key
    )
    if item is None or not item.upstream_url:
      return bytes_response(
        b"Stream not found", "text/plain", status=HTTPStatus.NOT_FOUND
      )

    target_url = _rewrite_stream_extension(
      item.upstream_url, info["ext"].lstrip(".")
    )
    logger.debug("Passthrough redirect stream %s -> upstream", stream_key)
    return redirect_response(target_url)

  def _m3u_handler(self, default_type: str) -> _Handler:
    async def _handler(request: web.Request) -> web.StreamResponse:
      return await self._handle_get_m3u(request, default_type=default_type)

    return _handler

  async def _handle_get_m3u(
    self, request: web.Request, *, default_type: str
  ) -> web.StreamResponse:
    params = await self._params(request)
    user = params.get("username", "")
    passwd = params.get("password", "")
    denied = self._verify_request_auth(request, user, passwd)
    if denied is not None:
      return denied

    snap = self.store.get_snapshot()
    raw_type = params.get("type", default_type).strip().lower()
    is_plus = raw_type in {
      "m3u_plus",
      "m3u8",
      "m3u_plus_ts",
      "m3u_plus_hls",
      "m3u_plus_m3u8",
      "",
    }

    raw_out = params.get("output", "").strip().lower()
    if not raw_out:
      raw_out = (
        "m3u8"
        if (raw_type.endswith(("_hls", "_m3u8")) or raw_type == "m3u8")
        else "ts"
      )
    ext = "m3u8" if raw_out in {"hls", "m3u8"} else "ts"

    raw_cats = params.get("category_id", "").strip()
    allowed_cats = (
      {c.strip() for c in raw_cats.split(",") if c.strip()}
      if raw_cats
      else set()
    )

    adv_host, adv_port = self._resolve_advertised_host_port(request.headers)
    host_fmt = (
      f"[{adv_host}]"
      if ":" in adv_host and not adv_host.startswith("[")
      else adv_host
    )
    base = f"{self.server_protocol}://{host_fmt}:{adv_port}"
    epg_url = f"{base}/xmltv.php?username={user}&password={passwd}"

    if is_plus:
      lines = [
        f'#EXTM3U url-tvg="{_escape_m3u_attr(epg_url)}" x-tvg-url="{_escape_m3u_attr(epg_url)}"',
        f"#PLAYLIST:{snap.provider} ({snap.market}) [{snap.lineup_id}]",
      ]
    else:
      lines = ["#EXTM3U"]

    for s in snap.streams:
      if allowed_cats and s.category_id not in allowed_cats:
        continue
      safe_name = s.name.replace("\n", " ").strip()
      stream_url = f"{base}/live/{user}/{passwd}/{s.stream_id}.{ext}"
      if is_plus:
        attrs = [
          f'tvg-chno="{s.num}"',
          f'channel-id="{s.num}"',
          f'tvg-id="{_escape_m3u_attr(s.epg_channel_id)}"',
          f'tvg-name="{_escape_m3u_attr(s.name)}"',
        ]
        if s.gracenote_station_id:
          attrs.append(
            f'tvc-guide-stationid="{_escape_m3u_attr(s.gracenote_station_id)}"'
          )
        if s.stream_icon:
          attrs.append(f'tvg-logo="{_escape_m3u_attr(s.stream_icon)}"')
        attrs.append(f'group-title="{_escape_m3u_attr(s.category_name)}"')
        if s.language:
          attrs.append(f'tvg-language="{_escape_m3u_attr(s.language)}"')
        lines.append(f"#EXTINF:-1 {' '.join(attrs)},{safe_name}")
      else:
        lines.append(f"#EXTINF:-1,{safe_name}")
      lines.append(stream_url)

    filename = "playlist.m3u8" if is_plus else "playlist.m3u"
    return bytes_response(
      ("\n".join(lines) + "\n").encode("utf-8"),
      "audio/x-mpegurl; charset=utf-8",
      headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
