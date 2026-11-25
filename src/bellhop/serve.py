"""Unified `bellhop serve` HTTP server combining the Web Guide and Xtream API.

Both internal applications can share a single `aiohttp` listener (co-existing
without route collisions) OR bind dedicated ports / interfaces inside the same
asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from aiohttp import web

from bellhop.common.export_bundle import ExportBundleReader
from bellhop.common.http_utils import (
  ClientSessionFactory,
  create_web_app,
  json_response,
  resolve_bind_host,
  start_site,
)
from bellhop.config import (
  AppConfig,
  apply_public_config_update,
  export_public_config,
  load_config,
)
from bellhop.epg.sync import (
  build_epg_refresher_from_config as build_epg_refresher_from_config,
)
from bellhop.epg.xmltv import build_passthrough_xmltv_url
from bellhop.web.auth import WebAuthConfig, WebAuthManager
from bellhop.web.server import WebGuideApp
from bellhop.web.store import LineupGuideStore
from bellhop.xtream.server import (
  XtreamExportStore,
  XtreamPassthroughServer,
)

logger = logging.getLogger("bellhop.serve")

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class UnifiedServeServer:
  """aiohttp server hosting `WebGuideApp` and `XtreamPassthroughServer`.

  - When both servers resolve to the same `(bind_host, port)`, a single shared
    `aiohttp.web.Application` mounts both route sets without collisions.
  - When configured on different ports or bind addresses, each server starts
    its own `web.AppRunner` inside the same asyncio event loop.
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

    self._runners: list[web.AppRunner] = []
    self._web_bound_port: int | None = None
    self._xtream_bound_port: int | None = None

  @classmethod
  def from_config(
    cls,
    config: AppConfig,
    *,
    session_factory: ClientSessionFactory | None = None,
    epg_session_factory: ClientSessionFactory | None = None,
    separate_listeners: bool = False,
  ) -> UnifiedServeServer:
    xtream_dir = config.resolve_path(config.output.xtream_dir)
    m3u8_cand = config.resolve_path(config.output.playlist_path)
    m3u8_path = m3u8_cand if m3u8_cand.is_file() else None
    cache_dir = config.resolve_path(config.xtream.cache_dir)
    fallback_xmltv_url = build_passthrough_xmltv_url(
      config, redact_secret=False
    )

    shared_reader = ExportBundleReader(
      xtream_dir,
      m3u8_path=m3u8_path,
      fallback_xmltv_url=fallback_xmltv_url,
    )
    web_store = LineupGuideStore(
      xtream_dir,
      m3u8_path=m3u8_path,
      fallback_xmltv_url=fallback_xmltv_url,
      bundle_reader=shared_reader,
    )
    epg_refresher = build_epg_refresher_from_config(
      config,
      web_store,
      session_factory=epg_session_factory or session_factory,
      enabled=(config.serve.web.enabled or config.serve.xtream.enabled),
    )

    web_username = config.serve.web.username.strip()
    web_password = config.serve.web.password.strip()
    if config.serve.web.enabled and (not web_username or not web_password):
      raise ValueError(
        "Web Guide login cannot be disabled: configure `web_server.username` "
        "and `web_server.password` in credentials.yaml, config.yaml, or "
        "BELLHOP_WEB_USERNAME / BELLHOP_WEB_PASSWORD."
      )
    if not config.serve.web.enabled and (not web_username or not web_password):
      web_username = "_disabled"
      web_password = secrets.token_hex(24)

    web_auth = WebAuthManager(
      WebAuthConfig(
        username=web_username,
        password=web_password,
        session_ttl_hours=config.serve.web.session_ttl_hours,
        session_secret=config.serve.web.session_secret,
        secret_file=cache_dir / "session_secret.key",
        secure_cookie=(config.serve.xtream.server_protocol == "https"),
      )
    )

    config_target = config.config_file or (config.project_root / "config.yaml")

    def _read_public_config() -> dict[str, Any]:
      live_cfg = (
        load_config(config_target) if config_target.is_file() else config
      )
      return {
        "available": True,
        "config_file": config_target.name,
        "config": export_public_config(live_cfg),
      }

    def _write_public_config(updates: dict[str, Any]) -> dict[str, Any]:
      new_cfg, restart_required = apply_public_config_update(
        config_target, updates
      )
      epg_refresher.config.refresh_hours = new_cfg.epg.refresh_hours
      epg_refresher.config.request_timeout_sec = (
        new_cfg.xtream.request_timeout_sec
      )
      return {
        "saved": True,
        "restart_required": restart_required,
        "config_file": config_target.name,
        "config": export_public_config(new_cfg),
      }

    web_app = WebGuideApp(
      web_store,
      settle_sec=max(0, config.serve.web.settle_ms) / 1000.0,
      session_factory=session_factory,
      epg_refresher=epg_refresher,
      auth=web_auth,
      trust_proxy_headers=config.serve.web.trust_proxy_headers,
      trusted_proxy_ips=config.serve.web.trusted_proxy_ips,
      config_reader=_read_public_config,
      config_writer=_write_public_config,
    )

    async def _provide_xmltv_chunks() -> Iterator[bytes] | None:
      epg_refresher.trigger_background_if_needed(reason_prefix="xmltv")
      snap = web_store.get_snapshot()
      now_epoch = epg_refresher.current_epoch()
      await asyncio.to_thread(epg_refresher.sync_external_if_needed, now_epoch)
      cov = epg_refresher.db.cached_coverage_stats(now_ts=now_epoch)
      if cov.total_programs == 0 and epg_refresher.db.db_path.is_file():
        cov = await asyncio.to_thread(
          epg_refresher.db.coverage_stats, now_ts=now_epoch
        )
      if cov.total_programs > 0:
        return epg_refresher.db.iter_xmltv_chunks(snap)
      return None

    async def _provide_short_epg(
      chno: int, epg_channel_id: str, language: str, limit: int
    ) -> list[dict[str, Any]]:
      epg_refresher.trigger_background_if_needed(reason_prefix="short_epg")
      now_epoch = epg_refresher.current_epoch()

      def _load_short() -> list[dict[str, Any]]:
        epg_refresher.sync_external_if_needed(now_epoch)
        return epg_refresher.db.query_xtream_epg_listings(
          chno=chno,
          epg_channel_id=epg_channel_id,
          language=language,
          now_ts=now_epoch,
          limit=limit,
        )

      return await asyncio.to_thread(_load_short)

    epg_active = epg_refresher.config.enabled and epg_refresher.is_configured
    xtream_store = XtreamExportStore(
      xtream_dir,
      m3u8_path=m3u8_path,
      fallback_xmltv_url=fallback_xmltv_url,
      bundle_reader=shared_reader,
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
      trust_proxy_headers=config.serve.web.trust_proxy_headers,
      trusted_proxy_ips=config.serve.web.trusted_proxy_ips,
      epg_xmltv_chunks_provider=_provide_xmltv_chunks if epg_active else None,
      epg_short_provider=_provide_short_epg if epg_active else None,
    )

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
    return (
      self._web_bound_port
      if self._web_bound_port is not None
      else self.web_port
    )

  @property
  def xtream_bound_port(self) -> int:
    return (
      self._xtream_bound_port
      if self._xtream_bound_port is not None
      else self.xtream_port
    )

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

  def _build_listener_app(
    self, *, serve_web: bool, serve_xtream: bool
  ) -> web.Application:
    app = create_web_app()
    app.router.add_get(
      "/health",
      self._make_health_handler(serve_web=serve_web, serve_xtream=serve_xtream),
    )
    if serve_web:
      self.web_app.add_routes(app)
    if serve_xtream:
      self.xtream_app.add_routes(app, include_health=False)
    return app

  async def start(self) -> UnifiedServeServer:
    if not self.web_enabled and not self.xtream_enabled:
      raise ValueError(
        "At least one of `serve.web.enabled` or `serve.xtream.enabled` must be true."
      )

    if self.web_enabled:
      await self.web_app.startup()
    elif (
      self.web_app.epg_refresher is not None
      and self.web_app.epg_refresher.config.enabled
    ):
      await self.web_app.epg_refresher.start()

    self._runners = []
    if self.shared_listener:
      app = self._build_listener_app(serve_web=True, serve_xtream=True)
      runner, bound = await start_site(app, self.web_bind_host, self.web_port)
      self._runners.append(runner)
      self._web_bound_port = bound
      self._xtream_bound_port = bound
      self.web_port = bound
      self.xtream_port = bound
      self.xtream_app.port = bound
    else:
      if self.web_enabled:
        web_only = self._build_listener_app(serve_web=True, serve_xtream=False)
        web_runner, web_bound = await start_site(
          web_only, self.web_bind_host, self.web_port
        )
        self._runners.append(web_runner)
        self._web_bound_port = web_bound
        self.web_port = web_bound

      if self.xtream_enabled:
        xt_only = self._build_listener_app(serve_web=False, serve_xtream=True)
        xt_runner, xt_bound = await start_site(
          xt_only, self.xtream_bind_host, self.xtream_port
        )
        self._runners.append(xt_runner)
        self._xtream_bound_port = xt_bound
        self.xtream_port = xt_bound
        self.xtream_app.port = xt_bound

    return self

  async def stop(self) -> None:
    runners = list(self._runners)
    self._runners = []
    self._web_bound_port = None
    self._xtream_bound_port = None
    for runner in runners:
      await runner.cleanup()
    if self.web_enabled:
      await self.web_app.shutdown()
    elif self.web_app.epg_refresher is not None:
      await self.web_app.epg_refresher.stop()

  async def serve_forever(self) -> None:
    await self.start()
    try:
      await asyncio.Event().wait()
    finally:
      await self.stop()

  def _make_health_handler(
    self, *, serve_web: bool, serve_xtream: bool
  ) -> _Handler:
    async def _handle_health(request: web.Request) -> web.StreamResponse:
      if self.web_enabled:
        snap = self.web_app.store.get_snapshot()
        epg_st = self.web_app.epg_status()
        total_progs = epg_st["program_count"]
        active_progs = epg_st["active_program_count"]
        lineup_id = snap.lineup_id
        provider = snap.provider
        market = snap.market
        channel_count = len(snap.channels)
        category_count = len(snap.categories)
        is_authed = self.web_app.is_authenticated(request)
        active_tuner = self.web_app.tuner.active_info if is_authed else None
      else:
        xt_snap = self.xtream_app.store.get_snapshot()
        if self.web_app.epg_refresher is not None:
          epg_st = self.web_app.epg_refresher.status_dict()
          total_progs = epg_st["program_count"]
          active_progs = epg_st["active_program_count"]
        else:
          total_progs, active_progs = 0, 0
        lineup_id = xt_snap.lineup_id
        provider = xt_snap.provider
        market = xt_snap.market
        channel_count = len(xt_snap.streams)
        category_count = len(xt_snap.categories)
        active_tuner = None

      return json_response(
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
        }
      )

    return _handle_health
