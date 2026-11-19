"""Passthrough Xtream Codes API server (`bellhop serve`) backed by calculated pipeline outputs.

Xtream Codes HTTP Contract & Wire Layouts
-----------------------------------------
- `GET/POST /player_api.php` (and `/player.php`):
  - No `action`: returns `{"user_info": {...}, "server_info": {...}}` (`{"user_info": {"auth": 0}}` on invalid credentials).
  - `action=get_live_categories`: returns `list[PassthroughCategoryDict]`.
  - `action=get_live_streams` (+ optional `category_id`): returns `list[PlayerApiStreamDict]` (`direct_source` is always `""` so upstream credentials never leak).
  - VOD / Series / Short-EPG actions return valid empty Xtream structures (`[]` or `{...}`).
- `GET/POST /panel_api.php`: returns unified `user_info`, `server_info`, `categories.live`, and `available_channels` keyed by channel number.
- `GET/HEAD /get.php`, `/playlist.m3u`, `/playlist.m3u8`: dynamically renders `m3u` or `m3u_plus` playlists (`output=ts` or `hls`/`m3u8`, optional `category_id` filter) pointing at this server's `/live/<user>/<pass>/<id>.<ext>` and `/xmltv.php` endpoints.
- `GET/HEAD /xmltv.php`, `/epg.xml`, `/live/<user>/<pass>/<id>[.<ext>]`, `/<user>/<pass>/<id>[.<ext>]`: authenticates `serve` credentials and issues an HTTP `302` redirect to the upstream IPTV server.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, TypedDict

from bellhop.config import AppConfig
from bellhop.epg_loader import build_passthrough_xmltv_url
from bellhop_web.http_utils import (
  HttpRequest,
  bound_server_port,
  build_access_urls,
  detect_device_ip,
  resolve_bind_host,
  send_bytes,
  send_json,
  send_redirect,
  serve_http_connection,
)

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
  return dict(val) if isinstance(val, Mapping) else {}


def _rewrite_stream_extension(upstream_url: str, requested_ext: str) -> str:
  """Adapt `.ts` / `.m3u8` extension on an upstream stream URL when requested by a client."""
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
    """Serialize public Xtream `player_api.php` stream item (never exposing `upstream_url`)."""
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
  """Hot-reloadable snapshot of Phase (6) exported lineup categories, streams, and passthrough URLs."""

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
  """Reads Phase (6) `output/xtream/` artifacts with automatic mtime-based hot reload."""

  def __init__(self, xtream_dir: Path, *, fallback_xmltv_url: str = "") -> None:
    self.xtream_dir = xtream_dir.resolve()
    self.fallback_xmltv_url = fallback_xmltv_url
    self._last_sig: tuple[float, ...] = ()
    self._snapshot: XtreamExportSnapshot | None = None

  def _watched_files(self) -> list[Path]:
    return [
      self.xtream_dir / "bellhop_xtream_playlist.json",
      self.xtream_dir / "bell_fibe_xtream_playlist.json",
      self.xtream_dir / "get_live_streams.json",
      self.xtream_dir / "get_live_categories.json",
    ]

  def _signature(self) -> tuple[float, ...]:
    out: list[float] = []
    for path in self._watched_files():
      try:
        out.append(path.stat().st_mtime if path.is_file() else 0.0)
      except OSError:
        out.append(0.0)
    return tuple(out)

  def get_snapshot(self, *, force_reload: bool = False) -> XtreamExportSnapshot:
    sig = self._signature()
    if (
      not force_reload and self._snapshot is not None and sig == self._last_sig
    ):
      return self._snapshot
    snap = self._load_from_disk()
    self._snapshot = snap
    self._last_sig = sig
    return snap

  def _load_from_disk(self) -> XtreamExportSnapshot:
    primary_bundle = self.xtream_dir / "bellhop_xtream_playlist.json"
    legacy_bundle = self.xtream_dir / "bell_fibe_xtream_playlist.json"
    bundle_path = primary_bundle if primary_bundle.is_file() else legacy_bundle
    streams_path = self.xtream_dir / "get_live_streams.json"
    cats_path = self.xtream_dir / "get_live_categories.json"

    lineup_info: dict[str, Any] = {}
    server_info: dict[str, Any] = {}
    user_info: dict[str, Any] = {}
    epg_guide: dict[str, Any] = {}
    raw_categories: list[Any] = []
    raw_streams: list[Any] = []

    if bundle_path.is_file():
      bundle = _as_dict(json.loads(bundle_path.read_text(encoding="utf-8")))
      lineup_info = _as_dict(bundle.get("lineup_info"))
      server_info = _as_dict(bundle.get("server_info"))
      user_info = _as_dict(bundle.get("user_info"))
      epg_guide = _as_dict(bundle.get("epg_guide"))
      raw_categories = list(bundle.get("live_categories") or [])
      raw_streams = list(bundle.get("live_streams") or [])
    else:
      if cats_path.is_file():
        loaded_cats = json.loads(cats_path.read_text(encoding="utf-8"))
        if isinstance(loaded_cats, list):
          raw_categories = loaded_cats
      if streams_path.is_file():
        loaded_streams = json.loads(streams_path.read_text(encoding="utf-8"))
        if isinstance(loaded_streams, list):
          raw_streams = loaded_streams

    if not raw_streams:
      # Allow `bellhop serve` to start cleanly before the first pipeline export
      # and automatically hot-reload once `output/xtream/` is populated.
      return XtreamExportSnapshot(
        lineup_id=str(lineup_info.get("lineup_id") or "UNINITIALIZED"),
        provider=str(lineup_info.get("provider") or "Bellhop"),
        market=str(lineup_info.get("market") or ""),
        timezone=str(server_info.get("timezone") or "America/Montreal"),
        xmltv_url=self.fallback_xmltv_url,
        loaded_at=time.time(),
      )

    categories: list[PassthroughCategory] = []
    seen_cats: set[str] = set()
    for raw_cat in raw_categories:
      c = _as_dict(raw_cat)
      cid = str(c.get("category_id") or "").strip()
      cname = str(c.get("category_name") or "General").strip()
      if cid and cid not in seen_cats:
        seen_cats.add(cid)
        categories.append(
          PassthroughCategory(
            category_id=cid,
            category_name=cname,
            parent_id=int(c.get("parent_id") or 0),
          )
        )

    streams: list[PassthroughStream] = []
    stream_by_id: dict[int, PassthroughStream] = {}
    stream_by_num: dict[int, PassthroughStream] = {}

    for idx, raw_item in enumerate(raw_streams):
      if not isinstance(raw_item, Mapping):
        continue
      num = int(raw_item.get("num") or (idx + 1))
      stream_id = int(raw_item.get("stream_id") or num)
      cat_id = str(raw_item.get("category_id") or "1")
      cat_name = str(raw_item.get("category_name") or "General")
      if cat_id not in seen_cats:
        seen_cats.add(cat_id)
        categories.append(
          PassthroughCategory(category_id=cat_id, category_name=cat_name)
        )

      guide = _as_dict(raw_item.get("guide"))
      normalized = PassthroughStream(
        num=num,
        name=str(raw_item.get("name") or f"Channel {num}"),
        stream_type="live",
        stream_id=stream_id,
        stream_icon=str(raw_item.get("stream_icon") or ""),
        epg_channel_id=str(raw_item.get("epg_channel_id") or ""),
        added=str(raw_item.get("added") or int(time.time())),
        is_adult=int(raw_item.get("is_adult") or 0),
        category_id=cat_id,
        category_ids=[int(cat_id)] if cat_id.isdigit() else [1],
        category_name=cat_name,
        custom_sid=str(raw_item.get("custom_sid") or num),
        tv_archive=int(raw_item.get("tv_archive") or 0),
        tv_archive_duration=int(raw_item.get("tv_archive_duration") or 0),
        language=str(guide.get("language") or ""),
        gracenote_station_id=str(guide.get("gracenote_station_id") or ""),
        upstream_url=str(raw_item.get("direct_source") or "").strip(),
      )
      streams.append(normalized)
      stream_by_id.setdefault(stream_id, normalized)
      stream_by_num[num] = normalized

    xmltv_url = str(
      server_info.get("xmltv_url")
      or epg_guide.get("xmltv_url")
      or self.fallback_xmltv_url
      or ""
    ).strip()

    raw_fmts = user_info.get("allowed_output_formats")
    allowed_formats = (
      [str(f) for f in raw_fmts if f]
      if isinstance(raw_fmts, list) and raw_fmts
      else ["m3u8", "ts"]
    )
    exp_raw = user_info.get("exp_date")

    return XtreamExportSnapshot(
      lineup_id=str(lineup_info.get("lineup_id") or "CAN-BELLFIBE-MTL-X"),
      provider=str(lineup_info.get("provider") or "Bell Fibe TV"),
      market=str(lineup_info.get("market") or "Montréal, QC"),
      timezone=str(server_info.get("timezone") or "America/Montreal"),
      max_connections=str(user_info.get("max_connections") or "1"),
      allowed_output_formats=allowed_formats,
      exp_date=str(exp_raw) if exp_raw is not None else None,
      is_trial=str(user_info.get("is_trial") or "0"),
      xmltv_url=xmltv_url,
      categories=categories,
      streams=streams,
      stream_by_id=stream_by_id,
      stream_by_num=stream_by_num,
      loaded_at=time.time(),
    )


class XtreamPassthroughServer:
  """Async HTTP/1.1 server implementing the Xtream Codes API with 302 upstream passthrough."""

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
  ) -> None:
    self.store = store
    self.username = username
    self.password = password
    self.bind_host = resolve_bind_host(host)
    self.port = port
    self.public_host = public_host
    self.public_port = public_port
    self.server_protocol = server_protocol
    self._server: asyncio.Server | None = None
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

  @property
  def bound_port(self) -> int:
    return bound_server_port(self._server, self.port)

  async def start(self) -> XtreamPassthroughServer:
    self._server = await asyncio.start_server(
      self.handle_connection,
      host=self.bind_host,
      port=self.port,
    )
    self.port = self.bound_port
    return self

  async def stop(self) -> None:
    if self._server is not None:
      self._server.close()
      await self._server.wait_closed()
      self._server = None

  async def serve_forever(self) -> None:
    await self.start()
    assert self._server is not None
    try:
      async with self._server:
        await self._server.serve_forever()
    finally:
      await self.stop()

  def _is_authorized(self, user: str, passwd: str) -> bool:
    if not self.username or not self.password:
      return False
    return hmac.compare_digest(user, self.username) and hmac.compare_digest(
      passwd, self.password
    )

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

  async def handle_connection(
    self,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
  ) -> None:
    async def _on_req(req: HttpRequest, w: asyncio.StreamWriter) -> None:
      await self._route(req.method, req.path, req.query, req.headers, w)

    await serve_http_connection(
      reader,
      writer,
      _on_req,
      logger=logger,
      merge_form_query=True,
    )

  async def dispatch(
    self,
    method: str,
    path: str,
    params: dict[str, list[str]],
    headers: Mapping[str, str],
    writer: asyncio.StreamWriter,
  ) -> None:
    """Route a parsed HTTP request to the Xtream Codes API & 302 passthrough handlers."""
    await self._route(method, path, params, headers, writer)

  async def _route(
    self,
    method: str,
    path: str,
    params: dict[str, list[str]],
    headers: Mapping[str, str],
    writer: asyncio.StreamWriter,
  ) -> None:
    if path in ("/health", "/api/health") and method == "GET":
      snap = self.store.get_snapshot()
      await self._send_json(
        writer,
        HTTPStatus.OK,
        {
          "status": "ok",
          "lineup_id": snap.lineup_id,
          "provider": snap.provider,
          "market": snap.market,
          "category_count": len(snap.categories),
          "channel_count": len(snap.streams),
        },
      )
      return

    if path in ("/player_api.php", "/player.php") and method in ("GET", "POST"):
      await self._handle_player_api(params, headers, writer)
      return

    if path == "/panel_api.php" and method in ("GET", "POST"):
      await self._handle_panel_api(params, headers, writer)
      return

    if path in ("/xmltv.php", "/epg.xml") and method in ("GET", "HEAD"):
      await self._handle_xmltv_passthrough(params, writer)
      return

    if path in ("/get.php", "/playlist.m3u", "/playlist.m3u8") and method in (
      "GET",
      "HEAD",
    ):
      default_type = "m3u_plus" if path != "/playlist.m3u" else "m3u"
      await self._handle_get_m3u(
        params,
        headers,
        writer,
        method=method,
        default_type=default_type,
      )
      return

    m_nonlive = re.match(
      r"^/(?:movie|series|timeshift)/([^/]+)/([^/]+)/.+$",
      path,
    )
    if m_nonlive and method in ("GET", "HEAD"):
      user, passwd = m_nonlive.groups()
      if not self._is_authorized(user, passwd):
        await self._send_bytes(
          writer, HTTPStatus.UNAUTHORIZED, b"Unauthorized", "text/plain"
        )
        return
      await self._send_bytes(
        writer, HTTPStatus.NOT_FOUND, b"Not found", "text/plain"
      )
      return

    m_live = re.match(
      r"^(?:/live)?/([^/]+)/([^/]+)/(\d+)(?:\.([A-Za-z0-9]+))?$",
      path,
    )
    if m_live and method in ("GET", "HEAD"):
      user, passwd, sid_str, ext = m_live.groups()
      await self._handle_stream_passthrough(
        user=user,
        passwd=passwd,
        stream_key=int(sid_str),
        ext=ext or "",
        writer=writer,
      )
      return

    await self._send_json(
      writer, HTTPStatus.NOT_FOUND, {"error": f"Not found: {path}"}
    )

  async def _handle_player_api(
    self,
    params: dict[str, list[str]],
    headers: Mapping[str, str],
    writer: asyncio.StreamWriter,
  ) -> None:
    user = (params.get("username") or [""])[0]
    passwd = (params.get("password") or [""])[0]
    if not self._is_authorized(user, passwd):
      await self._send_json(writer, HTTPStatus.OK, {"user_info": {"auth": 0}})
      return

    snap = self.store.get_snapshot()
    action = (params.get("action") or [""])[0].strip().lower()

    if action == "get_live_categories":
      await self._send_json(
        writer, HTTPStatus.OK, [c.to_dict() for c in snap.categories]
      )
      return

    if action == "get_live_streams":
      cat_filter = (params.get("category_id") or [""])[0].strip()
      items = [
        s.to_player_api_dict()
        for s in snap.streams
        if not cat_filter or s.category_id == cat_filter
      ]
      await self._send_json(writer, HTTPStatus.OK, items)
      return

    if action in {
      "get_vod_categories",
      "get_series_categories",
      "get_vod_streams",
      "get_series",
    }:
      await self._send_json(writer, HTTPStatus.OK, [])
      return

    if action == "get_vod_info":
      await self._send_json(
        writer, HTTPStatus.OK, {"info": {}, "movie_data": {}}
      )
      return

    if action == "get_series_info":
      await self._send_json(
        writer,
        HTTPStatus.OK,
        {"seasons": [], "info": {}, "episodes": {}},
      )
      return

    if action in {"get_short_epg", "get_simple_data_table"}:
      await self._send_json(writer, HTTPStatus.OK, {"epg_listings": []})
      return

    user_info, server_info = self._build_user_and_server_info(snap, headers)
    await self._send_json(
      writer,
      HTTPStatus.OK,
      {"user_info": user_info, "server_info": server_info},
    )

  async def _handle_panel_api(
    self,
    params: dict[str, list[str]],
    headers: Mapping[str, str],
    writer: asyncio.StreamWriter,
  ) -> None:
    user = (params.get("username") or [""])[0]
    passwd = (params.get("password") or [""])[0]
    if not self._is_authorized(user, passwd):
      await self._send_json(writer, HTTPStatus.OK, {"user_info": {"auth": 0}})
      return

    snap = self.store.get_snapshot()
    user_info, server_info = self._build_user_and_server_info(snap, headers)
    available = {str(s.num): s.to_player_api_dict() for s in snap.streams}
    await self._send_json(
      writer,
      HTTPStatus.OK,
      {
        "user_info": user_info,
        "server_info": server_info,
        "categories": {"live": [c.to_dict() for c in snap.categories]},
        "available_channels": available,
      },
    )

  async def _handle_xmltv_passthrough(
    self,
    params: dict[str, list[str]],
    writer: asyncio.StreamWriter,
  ) -> None:
    user = (params.get("username") or [""])[0]
    passwd = (params.get("password") or [""])[0]
    if not self._is_authorized(user, passwd):
      await self._send_bytes(
        writer, HTTPStatus.UNAUTHORIZED, b"Unauthorized", "text/plain"
      )
      return

    snap = self.store.get_snapshot()
    if not snap.xmltv_url:
      await self._send_json(
        writer,
        HTTPStatus.NOT_FOUND,
        {"error": "No upstream XMLTV passthrough URL configured."},
      )
      return

    logger.debug("Passthrough redirect /xmltv.php -> upstream XMLTV")
    await self._send_redirect(writer, snap.xmltv_url)

  async def _handle_stream_passthrough(
    self,
    *,
    user: str,
    passwd: str,
    stream_key: int,
    ext: str,
    writer: asyncio.StreamWriter,
  ) -> None:
    if not self._is_authorized(user, passwd):
      await self._send_bytes(
        writer, HTTPStatus.UNAUTHORIZED, b"Unauthorized", "text/plain"
      )
      return

    snap = self.store.get_snapshot()
    item = snap.stream_by_id.get(stream_key) or snap.stream_by_num.get(
      stream_key
    )
    if item is None or not item.upstream_url:
      await self._send_bytes(
        writer, HTTPStatus.NOT_FOUND, b"Stream not found", "text/plain"
      )
      return

    target_url = _rewrite_stream_extension(item.upstream_url, ext)
    logger.debug("Passthrough redirect stream %s -> upstream", stream_key)
    await self._send_redirect(writer, target_url)

  async def _handle_get_m3u(
    self,
    params: dict[str, list[str]],
    headers: Mapping[str, str],
    writer: asyncio.StreamWriter,
    *,
    method: str = "GET",
    default_type: str = "m3u_plus",
  ) -> None:
    user = (params.get("username") or [""])[0]
    passwd = (params.get("password") or [""])[0]
    if not self._is_authorized(user, passwd):
      await self._send_bytes(
        writer, HTTPStatus.UNAUTHORIZED, b"Unauthorized", "text/plain"
      )
      return

    snap = self.store.get_snapshot()
    raw_type = (params.get("type") or [default_type])[0].strip().lower()
    is_plus = raw_type in {
      "m3u_plus",
      "m3u8",
      "m3u_plus_ts",
      "m3u_plus_hls",
      "m3u_plus_m3u8",
      "",
    }

    raw_out = (params.get("output") or [""])[0].strip().lower()
    if not raw_out:
      raw_out = (
        "m3u8"
        if (raw_type.endswith(("_hls", "_m3u8")) or raw_type == "m3u8")
        else "ts"
      )
    ext = "m3u8" if raw_out in {"hls", "m3u8"} else "ts"

    raw_cats = (params.get("category_id") or [""])[0].strip()
    allowed_cats = (
      {c.strip() for c in raw_cats.split(",") if c.strip()}
      if raw_cats
      else set()
    )

    adv_host, adv_port = self._resolve_advertised_host_port(headers)
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

    payload = ("\n".join(lines) + "\n").encode("utf-8")
    filename = "playlist.m3u8" if is_plus else "playlist.m3u"
    extra_headers = {
      "Content-Disposition": f'attachment; filename="{filename}"'
    }
    await self._send_bytes(
      writer,
      HTTPStatus.OK,
      b"" if method == "HEAD" else payload,
      "audio/x-mpegurl; charset=utf-8",
      content_length=len(payload),
      extra_headers=extra_headers,
    )

  _send_redirect = staticmethod(send_redirect)
  _send_json = staticmethod(send_json)
  _send_bytes = staticmethod(send_bytes)
