"""Unified `bellhop serve` HTTP server combining the Web Guide and Xtream API.

Both internal applications can share a single `aiohttp` listener (co-existing
without route collisions) OR bind dedicated ports / interfaces inside the same
asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from aiohttp import web

from bellhop.common.accounts import UserAccount
from bellhop.common.export_bundle import ExportBundleReader
from bellhop.common.http_utils import (
  ClientSessionFactory,
  create_web_app,
  json_response,
  resolve_bind_host,
  start_site,
)
from bellhop.common.mailer import (
  EmailSenderProtocol,
  MailerConfigError,
  SmtpConfig,
  SmtpMailer,
)
from bellhop.config import (
  DEFAULT_SD_BASE_URL,
  AppConfig,
  ConfigManager,
)
from bellhop.epg.models import GuideSnapshotProto
from bellhop.epg.refresh import EpgRefreshService, EpgRefreshStatus
from bellhop.epg.sync import (
  ExportBundleGuideStore,
  build_epg_refresher_from_config,
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
    web_app: WebGuideApp | None,
    xtream_app: XtreamPassthroughServer,
    *,
    epg_refresher: EpgRefreshService | None = None,
    config_manager: ConfigManager | None = None,
    web_enabled: bool = True,
    web_host: str = "0.0.0.0",
    web_port: int = 8080,
    xtream_enabled: bool = True,
    xtream_host: str = "0.0.0.0",
    xtream_port: int = 8080,
    server_protocol: str = "http",
    separate_listeners: bool = False,
  ) -> None:
    self.web_app: WebGuideApp | None = web_app
    self.xtream_app = xtream_app
    self.epg_refresher: EpgRefreshService | None = epg_refresher or (
      web_app.epg_refresher if web_app is not None else None
    )
    self.config_manager = config_manager
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
    config_manager: ConfigManager | None = None,
    session_factory: ClientSessionFactory | None = None,
    epg_session_factory: ClientSessionFactory | None = None,
    email_sender: EmailSenderProtocol | None = None,
    separate_listeners: bool = False,
    sd_base_url: str = DEFAULT_SD_BASE_URL,
  ) -> UnifiedServeServer:
    mgr = config_manager or ConfigManager(
      config.config_file, initial_config=config
    )
    xtream_dir = config.resolve_path(config.output.xtream_dir)
    cache_dir = config.resolve_path(config.cache_dir)
    fallback_xmltv_url = build_passthrough_xmltv_url(
      config, redact_secret=False
    )

    def _accounts_provider() -> tuple[UserAccount, ...]:
      return mgr.get().accounts

    def _upstream_config_provider() -> tuple[str, str]:
      cur = mgr.get()
      return cur.xtream.server_url, cur.xtream.stream_format

    def _fallback_xmltv_url_provider() -> str:
      return build_passthrough_xmltv_url(mgr.get(), redact_secret=False)

    cached_email_cfg: SmtpConfig | None = None
    cached_mailer: SmtpMailer | None = None

    def _email_sender_provider() -> EmailSenderProtocol | None:
      nonlocal cached_email_cfg, cached_mailer
      cur_cfg = mgr.get().email
      if not cur_cfg.enabled:
        return None
      try:
        cur_cfg.validate(require_enabled=True)
      except MailerConfigError:
        return None
      if cached_mailer is None or cached_email_cfg != cur_cfg:
        cached_email_cfg = cur_cfg
        cached_mailer = SmtpMailer(cur_cfg)
      return cached_mailer

    def _public_base_urls_provider() -> tuple[str, ...]:
      return tuple(mgr.get().serve.web.public_base_urls)

    shared_reader = ExportBundleReader(
      xtream_dir,
      fallback_xmltv_url=fallback_xmltv_url,
      fallback_xmltv_url_provider=_fallback_xmltv_url_provider,
      extra_watched_files=(
        mgr.target_path,
        config.resolve_path(config.credentials_file),
      ),
    )
    xtream_store = XtreamExportStore(
      xtream_dir,
      bundle_reader=shared_reader,
    )

    web_store: LineupGuideStore | None = None
    headless_store: ExportBundleGuideStore | None = None
    if config.serve.web.enabled:
      if not config.accounts:
        raise ValueError(
          "Web Guide login requires at least one configured account under "
          "`accounts:` in credentials.yaml."
        )
      web_store = LineupGuideStore(
        xtream_dir,
        bundle_reader=shared_reader,
      )
      epg_refresher = build_epg_refresher_from_config(
        config,
        web_store,
        session_factory=epg_session_factory or session_factory,
        enabled=True,
        sd_base_url=sd_base_url,
        xmltv_url_provider=_fallback_xmltv_url_provider,
      )
    else:
      headless_store = ExportBundleGuideStore(
        xtream_dir,
        bundle_reader=shared_reader,
      )
      epg_refresher = build_epg_refresher_from_config(
        config,
        headless_store,
        session_factory=epg_session_factory or session_factory,
        enabled=config.serve.xtream.enabled,
        sd_base_url=sd_base_url,
        xmltv_url_provider=_fallback_xmltv_url_provider,
      )

    async def _provide_xmltv_chunks() -> Iterator[bytes] | None:
      epg_refresher.trigger_background_if_needed(reason_prefix="xmltv")
      snap: GuideSnapshotProto
      if web_store is not None:
        snap = await web_store.async_get_snapshot()
      elif headless_store is not None:
        snap = await headless_store.async_get_snapshot()
      else:
        snap = await asyncio.to_thread(epg_refresher.store.get_snapshot)
      now_epoch = epg_refresher.current_epoch()
      await asyncio.to_thread(epg_refresher.sync_external_if_needed, now_epoch)
      cov = epg_refresher.db.cached_coverage_stats(now_ts=now_epoch)
      if cov.active_programs == 0 and epg_refresher.db.db_path.is_file():
        cov = await asyncio.to_thread(
          epg_refresher.db.coverage_stats, now_ts=now_epoch
        )
      if cov.active_programs > 0:
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
    xtream_app = XtreamPassthroughServer(
      xtream_store,
      accounts=config.accounts,
      accounts_provider=_accounts_provider,
      upstream_server_url=config.xtream.server_url,
      upstream_stream_format=config.xtream.stream_format,
      upstream_config_provider=_upstream_config_provider,
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

    web_app: WebGuideApp | None = None
    if config.serve.web.enabled:
      assert web_store is not None
      web_auth = WebAuthManager(
        WebAuthConfig(
          accounts=config.accounts,
          accounts_provider=_accounts_provider,
          session_ttl_hours=config.serve.web.session_ttl_hours,
          secret_file=cache_dir / "session_secret.key",
        )
      )
      web_app = WebGuideApp(
        web_store,
        settle_sec=max(0, config.serve.web.settle_ms) / 1000.0,
        session_factory=session_factory,
        epg_refresher=epg_refresher,
        auth=web_auth,
        trust_proxy_headers=config.serve.web.trust_proxy_headers,
        trusted_proxy_ips=config.serve.web.trusted_proxy_ips,
        upstream_server_url=config.xtream.server_url,
        upstream_stream_format=config.xtream.stream_format,
        upstream_config_provider=_upstream_config_provider,
        xtream_api_enabled=config.serve.xtream.enabled,
        player_base_url_provider=xtream_app.format_public_base_url,
        password_changer=mgr.change_account_web_password,
        password_resetter=mgr.set_account_web_password,
        stream_creds_regenerator=mgr.regenerate_account_stream_credentials,
        email_sender=email_sender,
        email_sender_provider=None
        if email_sender is not None
        else _email_sender_provider,
        public_base_urls=config.serve.web.public_base_urls,
        public_base_urls_provider=_public_base_urls_provider,
        server_protocol=config.serve.xtream.server_protocol,
      )

    return cls(
      web_app,
      xtream_app,
      epg_refresher=epg_refresher,
      config_manager=mgr,
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
      and self.web_app is not None
      and self.web_app.auth is not None
      and self.web_app.auth.enabled
    )

  @property
  def xtream_auth_configured(self) -> bool:
    return self.xtream_enabled and self.xtream_app.auth_configured

  def _build_listener_app(
    self, *, serve_web: bool, serve_xtream: bool
  ) -> web.Application:
    app = create_web_app()
    app.router.add_get(
      "/health",
      self._make_health_handler(serve_web=serve_web, serve_xtream=serve_xtream),
    )
    if serve_web and self.web_app is not None:
      self.web_app.add_routes(app)
    if serve_xtream:
      self.xtream_app.add_routes(app, include_health=False)
    return app

  async def start(self) -> UnifiedServeServer:
    if not self.web_enabled and not self.xtream_enabled:
      raise ValueError(
        "At least one of `serve.web.enabled` or `serve.xtream.enabled` must be true."
      )

    if self.web_enabled and self.web_app is not None:
      await self.web_app.startup()
    elif self.epg_refresher is not None and self.epg_refresher.config.enabled:
      await self.epg_refresher.start()

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
    if self.web_enabled and self.web_app is not None:
      await self.web_app.shutdown()
    elif self.epg_refresher is not None:
      await self.epg_refresher.stop()

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
      session = (
        self.web_app.session_for(request)
        if self.web_enabled and self.web_app is not None
        else None
      )
      is_authed = session is not None
      epg_st = (
        self.epg_refresher.status_dict()
        if self.epg_refresher is not None
        else EpgRefreshStatus.disabled().to_dict()
      )
      if self.web_enabled and self.web_app is not None:
        snap = await self.web_app.store.async_get_snapshot()
        lineup_id = snap.lineup_id
        provider = snap.provider
        market = snap.market
        channel_count = len(snap.channels)
        category_count = len(snap.categories)
        active_tuner = (
          self.web_app.active_tuner_for(session.account_id)
          if session is not None
          else None
        )
      else:
        xt_snap = await self.xtream_app.store.async_get_snapshot()
        lineup_id = xt_snap.lineup_id
        provider = xt_snap.provider
        market = xt_snap.market
        channel_count = len(xt_snap.streams)
        category_count = len(xt_snap.categories)
        active_tuner = None

      payload: dict[str, Any] = {
        "status": "ok",
        "channel_count": channel_count,
        "category_count": category_count,
        "epg_program_count": epg_st["program_count"],
        "epg_active_program_count": epg_st["active_program_count"],
        "web_guide_enabled": serve_web,
        "web_auth_enabled": serve_web and self.web_auth_enabled,
        "xtream_api_enabled": serve_xtream and self.xtream_auth_configured,
        "shared_listener": self.shared_listener,
        "active_tuner": active_tuner,
      }
      if is_authed:
        payload["lineup_id"] = lineup_id
        payload["provider"] = provider
        payload["market"] = market
      return json_response(payload)

    return _handle_health
