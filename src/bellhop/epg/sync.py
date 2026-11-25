"""Factory wiring `AppConfig` + `ExportBundleReader` into `EpgRefreshService`.

Used by both `bellhop.pipeline.runner` (`bellhop run`) and `bellhop.serve`
(`bellhop serve`) without pulling HTTP server modules into the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bellhop.common.export_bundle import ExportBundleReader
from bellhop.common.http_utils import ClientSessionFactory
from bellhop.config import AppConfig
from bellhop.epg.models import GuideSnapshotProto, GuideStoreProto
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

  lineup_id: str = "CAN-BELLFIBE-MTL-X"
  provider: str = "Bell Fibe TV"
  market: str = "Montréal, QC"
  postal_prefix: str = "H2Y"
  xmltv_url: str = ""
  channels: list[BundleGuideChannel] = field(default_factory=list)

  def build_epg_channel_lookup(self) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for ch in self.channels:
      canonical = ch.epg_channel_id.strip()
      if canonical:
        lookup[canonical.lower()] = canonical
    for ch in self.channels:
      canonical = ch.epg_channel_id.strip()
      if not canonical:
        continue
      for alias in ch.epg_aliases:
        cleaned = alias.strip()
        if cleaned and cleaned.lower() not in lookup:
          lookup[cleaned.lower()] = canonical
    return lookup

  def build_epg_candidate_chains(self) -> dict[str, tuple[str, ...]]:
    chains: dict[str, list[str]] = {}
    for ch in self.channels:
      canonical = ch.epg_channel_id.strip()
      if not canonical:
        continue
      bucket = chains.setdefault(canonical, [])
      for cand in (canonical, *ch.epg_aliases):
        low = cand.strip().lower()
        if low and low not in bucket:
          bucket.append(low)
    return {k: tuple(v) for k, v in chains.items()}


class ExportBundleGuideStore:
  """Headless `GuideStoreProto` adapter over `ExportBundleReader` for `bellhop
  run`.
  """

  def __init__(
    self,
    xtream_dir: Path,
    *,
    m3u8_path: Path | None = None,
    fallback_xmltv_url: str = "",
  ) -> None:
    self._reader = ExportBundleReader(
      xtream_dir,
      m3u8_path=m3u8_path,
      fallback_xmltv_url=fallback_xmltv_url,
    )
    self._overlay_url = ""

  def get_snapshot(self, *, force_reload: bool = False) -> GuideSnapshotProto:
    _, bundle = self._reader.read_cached(force_reload=force_reload)
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
    return BundleGuideSnapshot(
      lineup_id=bundle.lineup_id,
      provider=bundle.provider,
      market=bundle.market,
      postal_prefix=bundle.postal_prefix,
      xmltv_url=bundle.xmltv_url or self._overlay_url,
      channels=channels,
    )

  def touch_epg_revision(self, *, xmltv_url: str = "") -> None:
    if xmltv_url:
      self._overlay_url = xmltv_url


def build_epg_refresher_from_config(
  config: AppConfig,
  store: GuideStoreProto | None = None,
  *,
  session_factory: ClientSessionFactory | None = None,
  enabled: bool = True,
) -> EpgRefreshService:
  """Construct an `EpgRefreshService` wired to `config`'s SD-JSON & XMLTV.

  Args:
    config: Loaded application configuration.
    store: Optional guide store (defaults to `ExportBundleGuideStore`).
    session_factory: Optional `aiohttp.ClientSession` factory override.
    enabled: Whether background refresh timers are enabled on startup.

  Returns:
    Configured `EpgRefreshService` backed by `cache/epg.sqlite3`.
  """
  xmltv_override = (
    config.resolve_path(config.epg.xmltv_file)
    if config.epg.xmltv_file is not None
    else None
  )
  cache_dir = config.resolve_path(config.xtream.cache_dir)
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
    base_url=sd_raw.base_url,
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
