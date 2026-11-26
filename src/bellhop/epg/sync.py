"""Factory wiring `AppConfig` + `ExportBundleReader` into `EpgRefreshService`.

Used by both `bellhop.pipeline.runner` (`bellhop run`) and `bellhop.serve`
(`bellhop serve`) without pulling HTTP server modules into the pipeline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from bellhop.common.export_bundle import (
  ExportBundleReader,
  build_epg_chains_from_pairs,
  build_epg_lookup_from_pairs,
)
from bellhop.common.http_utils import ClientSessionFactory
from bellhop.config import DEFAULT_SD_BASE_URL, AppConfig
from bellhop.epg.models import (
  GuideSnapshotProto,
  GuideStoreProto,
  _compute_raw_guide_fingerprint,
)
from bellhop.epg.refresh import EpgRefreshConfig, EpgRefreshService
from bellhop.epg.sd_epg import SchedulesDirectEpgConfig
from bellhop.epg.xmltv import build_passthrough_xmltv_url


@dataclass(slots=True)
class BundleGuideChannel:
  """Headless channel binding record read directly from `ExportBundleReader`."""

  chno: int
  sd_chno: int | None
  name: str
  callsign: str | None
  logo: str
  language: str
  epg_channel_id: str
  epg_aliases: tuple[str, ...] = ()
  gracenote_station_id: str = ""


@dataclass(slots=True)
class BundleGuideSnapshot:
  """Headless lineup snapshot read directly from `ExportBundleReader`."""

  lineup_id: str = ""
  provider: str = "Bellhop"
  market: str = ""
  postal_prefix: str = ""
  xmltv_url: str = ""
  channels: list[BundleGuideChannel] = field(default_factory=list)
  _epg_lookup: dict[str, str] | None = field(
    default=None, init=False, repr=False
  )
  _epg_chains: dict[str, tuple[str, ...]] | None = field(
    default=None, init=False, repr=False
  )
  _guide_fingerprint: str = field(default="", init=False, repr=False)

  @property
  def guide_fingerprint(self) -> str:
    """Cached SHA-256 fingerprint of channel-to-EPG bindings."""
    if not self._guide_fingerprint:
      self._guide_fingerprint = _compute_raw_guide_fingerprint(self)
    return self._guide_fingerprint

  def build_epg_channel_lookup(self) -> dict[str, str]:
    if self._epg_lookup is None:
      self._epg_lookup = build_epg_lookup_from_pairs(
        (ch.epg_channel_id, ch.epg_aliases) for ch in self.channels
      )
    return self._epg_lookup

  def build_epg_candidate_chains(self) -> dict[str, tuple[str, ...]]:
    if self._epg_chains is None:
      self._epg_chains = build_epg_chains_from_pairs(
        (ch.epg_channel_id, ch.epg_aliases) for ch in self.channels
      )
    return self._epg_chains


class ExportBundleGuideStore:
  """Headless `GuideStoreProto` adapter over `ExportBundleReader`."""

  def __init__(
    self,
    xtream_dir: Path,
    *,
    m3u8_path: Path | None = None,
    fallback_xmltv_url: str = "",
    bundle_reader: ExportBundleReader | None = None,
  ) -> None:
    self._reader = bundle_reader or ExportBundleReader(
      xtream_dir,
      m3u8_path=m3u8_path,
      fallback_xmltv_url=fallback_xmltv_url,
    )
    self._overlay_url = ""
    self._last_sig: tuple[float, ...] = ()
    self._snapshot: BundleGuideSnapshot | None = None

  @property
  def fallback_xmltv_url(self) -> str:
    return self._reader.fallback_xmltv_url

  @fallback_xmltv_url.setter
  def fallback_xmltv_url(self, value: str) -> None:
    self._reader.fallback_xmltv_url = value.strip()

  def get_snapshot(self, *, force_reload: bool = False) -> GuideSnapshotProto:
    sig, bundle = self._reader.read_cached(force_reload=force_reload)
    if (
      not force_reload and self._snapshot is not None and sig == self._last_sig
    ):
      return self._snapshot
    channels = [
      BundleGuideChannel(
        chno=rec.num,
        sd_chno=rec.sd_chno,
        name=rec.name,
        callsign=rec.callsign,
        logo=rec.stream_icon,
        language=rec.language,
        epg_channel_id=rec.epg_channel_id,
        epg_aliases=rec.epg_aliases,
        gracenote_station_id=rec.gracenote_station_id,
      )
      for rec in bundle.streams
    ]
    channels.sort(key=lambda c: c.chno)
    snap = BundleGuideSnapshot(
      lineup_id=bundle.lineup_id,
      provider=bundle.provider,
      market=bundle.market,
      postal_prefix=bundle.postal_prefix,
      xmltv_url=bundle.xmltv_url or self._overlay_url,
      channels=channels,
    )
    snap.build_epg_channel_lookup()
    snap.build_epg_candidate_chains()
    _ = snap.guide_fingerprint
    self._snapshot = snap
    self._last_sig = sig
    return snap

  async def async_get_snapshot(
    self, *, force_reload: bool = False
  ) -> GuideSnapshotProto:
    """Async-safe snapshot retrieval offloading disk reloads."""
    sig = self._reader.compute_signature()
    if (
      not force_reload and self._snapshot is not None and sig == self._last_sig
    ):
      return self._snapshot
    return await asyncio.to_thread(self.get_snapshot, force_reload=force_reload)

  def touch_epg_revision(self, *, xmltv_url: str = "") -> None:
    if xmltv_url:
      self._overlay_url = xmltv_url
      if self._snapshot is not None and not self._snapshot.xmltv_url:
        self._snapshot.xmltv_url = xmltv_url


def build_epg_refresher_from_config(
  config: AppConfig,
  store: GuideStoreProto | None = None,
  *,
  session_factory: ClientSessionFactory | None = None,
  enabled: bool = True,
  sd_base_url: str = DEFAULT_SD_BASE_URL,
) -> EpgRefreshService:
  """Construct an `EpgRefreshService` wired to `config`'s SD-JSON & XMLTV.

  Args:
    config: Loaded application configuration.
    store: Optional guide store (defaults to `ExportBundleGuideStore`).
    session_factory: Optional `aiohttp.ClientSession` factory override.
    enabled: Whether background refresh timers are enabled on startup.
    sd_base_url: Schedules Direct API base URL (overridable in tests).

  Returns:
    Configured `EpgRefreshService` backed by `cache/epg.sqlite3`.
  """
  xmltv_override = (
    config.resolve_path(config.epg.xmltv_file)
    if config.epg.xmltv_file is not None
    else None
  )
  cache_dir = config.resolve_path(config.cache_dir)
  epg_db_file = cache_dir / "epg.sqlite3"
  fallback_xmltv_url = build_passthrough_xmltv_url(config, redact_secret=False)
  if store is None:
    m3u_cand = config.resolve_path(config.output.playlist_path)
    store = ExportBundleGuideStore(
      config.resolve_path(config.output.xtream_dir),
      m3u8_path=m3u_cand if m3u_cand.is_file() else None,
      fallback_xmltv_url=fallback_xmltv_url,
    )

  sd_raw = config.lineup.schedules_direct
  sd_epg_cfg = SchedulesDirectEpgConfig(
    username=sd_raw.username,
    password=sd_raw.password,
    base_url=sd_base_url,
    auto_add_lineup=sd_raw.auto_add_lineup,
    preferred_lineup_id=config.lineup.lineup_id,
    postal_code=config.lineup.postal_code,
    timeout_sec=config.xtream.request_timeout_sec,
    token_cache_file=cache_dir / "sd_token.json",
  )
  return EpgRefreshService(
    store,
    EpgRefreshConfig(
      enabled=enabled,
      refresh_hours=config.epg.refresh_hours,
      request_timeout_sec=config.xtream.request_timeout_sec,
      xmltv_url=fallback_xmltv_url,
      xmltv_file=xmltv_override,
      cache_file=epg_db_file,
      sd_epg=sd_epg_cfg,
    ),
    session_factory=session_factory,
  )
