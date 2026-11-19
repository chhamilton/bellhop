"""Unified `bellhop serve` HTTP server combining the Bellhop Web Guide (`bellhop_web`) and Xtream Codes API.

Both internal servers can share a single bind address & port (co-existing without route
collisions) OR optionally run on separate ports / bind addresses from the same process.
"""

from __future__ import annotations

import asyncio
import logging
from http import HTTPStatus
from typing import Any

import httpx

from bellhop.config import (
  AppConfig,
  apply_public_config_update,
  export_public_config,
  load_config,
)
from bellhop.epg_loader import build_passthrough_xmltv_url
from bellhop.xtream_server import (
  XtreamExportStore,
  XtreamPassthroughServer,
  resolve_bind_host,
)
from bellhop_web.auth import WebAuthConfig, WebAuthManager
from bellhop_web.epg_refresh import EpgRefreshConfig, EpgRefreshService
from bellhop_web.http_utils import (
  HttpRequest,
  bound_server_port,
  send_json,
  serve_http_connection,
)
from bellhop_web.server import WebGuideApp
from bellhop_web.store import LineupGuideStore

logger = logging.getLogger("bellhop.serve")


class UnifiedServeServer:
  """Async HTTP/1.1 router hosting `WebGuideApp` and `XtreamPassthroughServer`.

  - When both servers resolve to the same `(bind_host, port)`, a single shared listener
    routes `/`, `/index.html`, `/static/*`, `/api/*` to `WebGuideApp` and Xtream routes
    (`/player_api.php`, `/player.php`, `/panel_api.php`, `/xmltv.php`, `/epg.xml`,
    `/get.php`, `/playlist.m3u*`, `/live/*`, `/<user>/<pass>/<id>[.<ext>]`, etc.)
    to `XtreamPassthroughServer`.
  - When configured on different ports or bind addresses, each server binds its own
    listener inside the same asyncio event loop.
  """

  def __init__(
    self,
    web_app: WebGuideApp,
    xtream_app: XtreamPassthroughServer,
    *,
    web_enabled: bool = True,
    web_host: str = "0.0.0.0",
    web_port: int = 8080,
    xtream_enabled: bool = True,
    xtream_host: str = "0.0.0.0",
    xtream_port: int = 8080,
    server_protocol: str = "http",
    separate_listeners: bool = False,
  ) -> None:
    self.web_app = web_app
    self.xtream_app = xtream_app
    self.web_enabled = web_enabled
    self.web_bind_host = resolve_bind_host(web_host)
    self.web_port = web_port
    self.xtream_enabled = xtream_enabled
    self.xtream_bind_host = resolve_bind_host(xtream_host)
    self.xtream_port = xtream_port
    self.server_protocol = server_protocol
    self._force_separate = separate_listeners

    self._web_server: asyncio.Server | None = None
    self._xtream_server: asyncio.Server | None = None
    self._servers: list[asyncio.Server] = []

  @classmethod
  def from_config(
    cls,
    config: AppConfig,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    epg_transport: httpx.AsyncBaseTransport | None = None,
    separate_listeners: bool = False,
  ) -> UnifiedServeServer:
    xtream_dir = config.resolve_path(config.output.xtream_dir)
    m3u8_cand = config.resolve_path(config.output.playlist_path)
    m3u8_path = m3u8_cand if m3u8_cand.is_file() else None
    xmltv_override = (
      config.resolve_path(config.epg.xmltv_file)
      if config.epg.xmltv_file is not None
      else None
    )
    cache_dir = config.resolve_path(config.xtream.cache_dir)
    epg_cache_file = cache_dir / "bellhop_web" / "epg_cache.json"

    fallback_xmltv_url = build_passthrough_xmltv_url(
      config, redact_secret=False
    )

    web_store = LineupGuideStore(
      xtream_dir,
      m3u8_path=m3u8_path,
      xmltv_path=xmltv_override,
    )
    epg_refresher = EpgRefreshService(
      web_store,
      EpgRefreshConfig(
        enabled=config.serve.web.enabled,
        refresh_hours=config.epg.refresh_hours,
        request_timeout_sec=config.xtream.request_timeout_sec,
        xmltv_url=fallback_xmltv_url,
        xmltv_file=xmltv_override,
        cache_file=epg_cache_file,
      ),
      transport=epg_transport,
    )
    web_auth = WebAuthManager(
      WebAuthConfig(
        username=config.serve.web.username,
        password=config.serve.web.password,
        session_ttl_hours=config.serve.web.session_ttl_hours,
        session_secret=config.serve.web.session_secret,
        secret_file=cache_dir / "bellhop_web" / "session_secret.key",
        secure_cookie=(config.serve.xtream.server_protocol == "https"),
      )
    )
    web_app = WebGuideApp(
      web_store,
      settle_sec=max(0, config.serve.web.settle_ms) / 1000.0,
      upstream_transport=upstream_transport,
      epg_refresher=epg_refresher,
      auth=web_auth,
    )

    xtream_store = XtreamExportStore(
      xtream_dir, fallback_xmltv_url=fallback_xmltv_url
    )
    xtream_app = XtreamPassthroughServer(
      xtream_store,
      username=config.serve.xtream.username,
      password=config.serve.xtream.password,
      host=config.serve.xtream_host,
      port=config.serve.xtream_port,
      public_host=config.serve.xtream.public_host,
      public_port=config.serve.xtream.public_port,
      server_protocol=config.serve.xtream.server_protocol,
    )

    config_target = config.config_file or (config.project_root / "config.yaml")

    def _read_public_config() -> dict[str, Any]:
      live_cfg = (
        load_config(config_target) if config_target.is_file() else config
      )
      return {
        "available": True,
        "config_file": str(config_target),
        "config": export_public_config(live_cfg),
      }

    def _write_public_config(updates: dict[str, Any]) -> dict[str, Any]:
      new_cfg, restart_required = apply_public_config_update(
        config_target, updates
      )
      # Apply live-updatable operational knobs immediately (host-level web_server is untouched)
      if web_app.epg_refresher is not None:
        web_app.epg_refresher.config.refresh_hours = new_cfg.epg.refresh_hours
        web_app.epg_refresher.config.request_timeout_sec = (
          new_cfg.xtream.request_timeout_sec
        )
      xtream_app.public_host = new_cfg.serve.xtream.public_host
      xtream_app.public_port = new_cfg.serve.xtream.public_port
      xtream_app.server_protocol = new_cfg.serve.xtream.server_protocol
      return {
        "saved": True,
        "restart_required": restart_required,
        "config_file": str(config_target),
        "config": export_public_config(new_cfg),
      }

    web_app._config_reader = _read_public_config
    web_app._config_writer = _write_public_config

    return cls(
      web_app,
      xtream_app,
      web_enabled=config.serve.web.enabled,
      web_host=config.serve.web_host,
      web_port=config.serve.web_port,
      xtream_enabled=config.serve.xtream.enabled,
      xtream_host=config.serve.xtream_host,
      xtream_port=config.serve.xtream_port,
      server_protocol=config.serve.xtream.server_protocol,
      separate_listeners=separate_listeners,
    )

  @property
  def shared_listener(self) -> bool:
    return (
      not self._force_separate
      and self.web_enabled
      and self.xtream_enabled
      and self.web_bind_host == self.xtream_bind_host
      and self.web_port == self.xtream_port
    )

  @property
  def web_bound_port(self) -> int:
    return bound_server_port(self._web_server, self.web_port)

  @property
  def xtream_bound_port(self) -> int:
    return bound_server_port(self._xtream_server, self.xtream_port)

  @property
  def bound_port(self) -> int:
    """Primary bound port (convenience property when sharing one listener)."""
    if self.web_enabled:
      return self.web_bound_port
    return self.xtream_bound_port

  @property
  def bind_host(self) -> str:
    return self.web_bind_host if self.web_enabled else self.xtream_bind_host

  @property
  def port(self) -> int:
    return self.web_port if self.web_enabled else self.xtream_port

  @property
  def web_auth_enabled(self) -> bool:
    return bool(
      self.web_enabled
      and self.web_app.auth is not None
      and self.web_app.auth.enabled
    )

  @property
  def xtream_auth_configured(self) -> bool:
    return self.xtream_enabled and bool(
      self.xtream_app.username and self.xtream_app.password
    )

  async def start(self) -> UnifiedServeServer:
    if not self.web_enabled and not self.xtream_enabled:
      raise ValueError(
        "At least one of `serve.web.enabled` or `serve.xtream.enabled` must be true."
      )

    if self.web_enabled:
      await self.web_app.startup()

    self._servers = []
    if self.shared_listener:
      srv = await asyncio.start_server(
        lambda r, w: self._handle_connection(
          r, w, serve_web=True, serve_xtream=True
        ),
        host=self.web_bind_host,
        port=self.web_port,
      )
      self._web_server = srv
      self._xtream_server = srv
      self._servers.append(srv)
      bound = self.web_bound_port
      self.web_port = bound
      self.xtream_port = bound
      self.xtream_app.port = bound
    else:
      if self.web_enabled:
        web_srv = await asyncio.start_server(
          lambda r, w: self._handle_connection(
            r, w, serve_web=True, serve_xtream=False
          ),
          host=self.web_bind_host,
          port=self.web_port,
        )
        self._web_server = web_srv
        self._servers.append(web_srv)
        self.web_port = self.web_bound_port

      if self.xtream_enabled:
        xt_srv = await asyncio.start_server(
          lambda r, w: self._handle_connection(
            r, w, serve_web=False, serve_xtream=True
          ),
          host=self.xtream_bind_host,
          port=self.xtream_port,
        )
        self._xtream_server = xt_srv
        self._servers.append(xt_srv)
        self.xtream_port = self.xtream_bound_port
        self.xtream_app.port = self.xtream_port

    return self

  async def stop(self) -> None:
    servers = list(self._servers)
    self._servers = []
    self._web_server = None
    self._xtream_server = None
    for srv in servers:
      srv.close()
    for srv in servers:
      await srv.wait_closed()
    if self.web_enabled:
      await self.web_app.shutdown()

  async def serve_forever(self) -> None:
    await self.start()
    try:
      await asyncio.gather(*(srv.serve_forever() for srv in self._servers))
    finally:
      await self.stop()

  @staticmethod
  def _is_web_guide_path(path: str) -> bool:
    return (
      path in ("/", "/index.html", "/login", "/favicon.ico", "/favicon.svg")
      or path.startswith("/static/")
      or path.startswith("/api/")
    )

  async def _handle_connection(
    self,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    serve_web: bool,
    serve_xtream: bool,
  ) -> None:
    async def _on_req(req: HttpRequest, w: asyncio.StreamWriter) -> None:
      if req.path == "/health" and req.method == "GET":
        await self._handle_health(
          w, serve_web=serve_web, serve_xtream=serve_xtream
        )
        return

      if self._is_web_guide_path(req.path):
        if serve_web:
          await self.web_app.dispatch(
            req.method,
            req.path,
            req.query,
            w,
            headers=req.headers,
            body=req.body,
            raw_target=req.raw_target,
          )
        else:
          await send_json(
            w, HTTPStatus.NOT_FOUND, {"error": f"Not found: {req.path}"}
          )
      else:
        if serve_xtream:
          await self.xtream_app.dispatch(
            req.method, req.path, req.query, req.headers, w
          )
        else:
          await send_json(
            w, HTTPStatus.NOT_FOUND, {"error": f"Not found: {req.path}"}
          )

    await serve_http_connection(
      reader,
      writer,
      _on_req,
      logger=logger,
      merge_form_query=True,
    )

  async def _handle_health(
    self,
    writer: asyncio.StreamWriter,
    *,
    serve_web: bool,
    serve_xtream: bool,
  ) -> None:
    if self.web_enabled:
      snap = self.web_app.store.get_snapshot()
      total_progs, active_progs, _ = snap.epg_coverage_stats()
      lineup_id = snap.lineup_id
      provider = snap.provider
      market = snap.market
      channel_count = len(snap.channels)
      category_count = len(snap.categories)
      active_tuner = self.web_app.tuner.active_info
    else:
      xt_snap = self.xtream_app.store.get_snapshot()
      total_progs, active_progs = 0, 0
      lineup_id = xt_snap.lineup_id
      provider = xt_snap.provider
      market = xt_snap.market
      channel_count = len(xt_snap.streams)
      category_count = len(xt_snap.categories)
      active_tuner = None

    await send_json(
      writer,
      HTTPStatus.OK,
      {
        "status": "ok",
        "lineup_id": lineup_id,
        "provider": provider,
        "market": market,
        "channel_count": channel_count,
        "category_count": category_count,
        "epg_program_count": total_progs,
        "epg_active_program_count": active_progs,
        "web_guide_enabled": serve_web,
        "web_auth_enabled": serve_web and self.web_auth_enabled,
        "xtream_api_enabled": serve_xtream and self.xtream_auth_configured,
        "shared_listener": self.shared_listener,
        "active_tuner": active_tuner,
      },
    )
