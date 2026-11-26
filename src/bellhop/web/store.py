"""Read-only Web Guide store loading Phase (6) Xtream JSON / M3U8 outputs with
hot-reload.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from bellhop.common.api_contracts import (
  GuideCategoryDict as GuideCategoryDict,
)
from bellhop.common.api_contracts import (
  ProgramItemDict as ProgramItemDict,
)
from bellhop.common.api_contracts import (
  PublicChannelDict as PublicChannelDict,
)
from bellhop.common.export_bundle import (
  ExportBundleReader,
  build_epg_chains_from_pairs,
  build_epg_lookup_from_pairs,
)
from bellhop.epg.constants import EPG_RECENT_FLOOR_SEC
from bellhop.epg.models import (
  ProgramItem as ProgramItem,
)
from bellhop.epg.models import (
  _compute_raw_guide_fingerprint,
)
from bellhop.epg.models import (
  open_xml_stream as open_xml_stream,
)
from bellhop.epg.models import (
  parse_xmltv_ts as parse_xmltv_ts,
)
from bellhop.epg.models import (
  reconcile_non_overlapping_schedule as reconcile_non_overlapping_schedule,
)

logger = logging.getLogger("bellhop.web.store")


@dataclass(slots=True)
class GuideCategory:
  """Lineup category with live channel count for the Web Guide sidebar."""

  category_id: str
  category_name: str
  channel_count: int = 0

  def to_dict(self) -> GuideCategoryDict:
    return {
      "category_id": self.category_id,
      "category_name": self.category_name,
      "channel_count": self.channel_count,
    }


@dataclass(slots=True)
class WebChannel:
  """A mapped lineup channel ready for web guide display and local stream
  proxying.
  """

  chno: int
  sd_chno: int | None
  name: str
  callsign: str | None
  logo: str
  category_id: str
  category_name: str
  language: str
  quality_tier: str
  epg_channel_id: str
  has_native_epg: bool
  stream_id: int
  direct_source: str
  fallback_sources: list[str] = field(default_factory=list)
  epg_aliases: tuple[str, ...] = ()
  gracenote_station_id: str = ""

  def to_public_dict(
    self,
    programs: list[ProgramItem],
    *,
    now_ts: int,
    upcoming_limit: int = 48,
  ) -> PublicChannelDict:
    """Serialize channel for browser UI without leaking upstream provider
    credentials.
    """
    now_prog: ProgramItemDict | None = None
    next_prog: ProgramItemDict | None = None
    upcoming: list[ProgramItemDict] = []
    recent_floor = now_ts - EPG_RECENT_FLOOR_SEC

    for p in programs:
      if p.stop_timestamp > 0 and p.stop_timestamp <= recent_floor:
        continue
      p_dict = p.to_dict(now_ts=now_ts)
      if now_prog is None and p.start_timestamp <= now_ts < p.stop_timestamp:
        now_prog = p_dict
      elif (
        now_prog is not None
        and next_prog is None
        and p.start_timestamp >= now_ts
        or now_prog is None
        and next_prog is None
        and p.start_timestamp > now_ts
      ):
        next_prog = p_dict

      if len(upcoming) < upcoming_limit:
        upcoming.append(p_dict)

    return {
      "chno": self.chno,
      "sd_chno": self.sd_chno,
      "name": self.name,
      "callsign": self.callsign,
      "logo": self.logo,
      "category_id": self.category_id,
      "category_name": self.category_name,
      "language": self.language,
      "quality_tier": self.quality_tier,
      "epg_channel_id": self.epg_channel_id,
      "has_native_epg": self.has_native_epg,
      "stream_id": self.stream_id,
      "stream_url": f"/api/stream/{self.chno}.ts",
      "hls_url": f"/api/hls/{self.chno}/index.m3u8",
      "now_playing": now_prog,
      "up_next": next_prog,
      "schedule": upcoming,
    }


@dataclass(slots=True)
class LineupSnapshot:
  """Immutable in-memory snapshot of lineup categories and mapped channels."""

  lineup_id: str = ""
  provider: str = "Bellhop"
  market: str = ""
  postal_prefix: str = ""
  timezone: str = "UTC"
  loaded_at: float = 0.0
  xmltv_url: str = ""
  categories: list[GuideCategory] = field(default_factory=list)
  channels: list[WebChannel] = field(default_factory=list)
  channels_by_chno: dict[int, WebChannel] = field(default_factory=dict)
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
    """Build lowercase upstream channel ID -> canonical `epg_channel_id` lookup
    map.
    """
    if self._epg_lookup is None:
      self._epg_lookup = build_epg_lookup_from_pairs(
        (ch.epg_channel_id, ch.epg_aliases) for ch in self.channels
      )
    return self._epg_lookup

  def build_epg_candidate_chains(self) -> dict[str, tuple[str, ...]]:
    """Build ordered fallback chains `canonical_epg_id -> (primary_id,
    fallback_alias, ...)`.
    """
    if self._epg_chains is None:
      self._epg_chains = build_epg_chains_from_pairs(
        (ch.epg_channel_id, ch.epg_aliases) for ch in self.channels
      )
    return self._epg_chains


class LineupGuideStore:
  """Loads Phase (6) Xtream JSON outputs (or M3U8 fallback) with automatic
  hot-reload.
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
    self.m3u8_path = m3u8_path.resolve() if m3u8_path else None
    self.fallback_xmltv_url = fallback_xmltv_url
    self._bundle_reader = bundle_reader or ExportBundleReader(
      self.xtream_dir,
      m3u8_path=self.m3u8_path,
      fallback_xmltv_url=fallback_xmltv_url,
    )
    self._last_signature: tuple[float, ...] = ()
    self._last_updated_at: float = 0.0
    self._snapshot: LineupSnapshot | None = None
    self._epg_overlay_url: str = ""

  @property
  def bundle_signature(self) -> tuple[float, ...]:
    """Return the `(mtime_ns, size)` signature of the currently loaded export
    bundle.
    """
    return self._last_signature

  def _next_updated_at(self) -> float:
    candidate = round(time.time(), 3)
    if candidate <= self._last_updated_at:
      candidate = round(self._last_updated_at + 0.001, 3)
    self._last_updated_at = candidate
    return candidate

  def _watched_files(self) -> list[Path]:
    return self._bundle_reader.watched_files()

  def _compute_signature(self) -> tuple[float, ...]:
    return self._bundle_reader.compute_signature()

  def get_snapshot(self, *, force_reload: bool = False) -> LineupSnapshot:
    """Return the current `LineupSnapshot`, reloading from disk when files
    changed.
    """
    sig = self._compute_signature()
    prev_snap = self._snapshot
    if (
      not force_reload and prev_snap is not None and sig == self._last_signature
    ):
      return prev_snap

    snap = self._load_from_disk(force_reload=force_reload)
    if self._epg_overlay_url and not snap.xmltv_url:
      snap.xmltv_url = self._epg_overlay_url
    snap.loaded_at = self._next_updated_at()
    self._snapshot = snap
    self._last_signature = sig
    if prev_snap is not None:
      delta = len(snap.channels) - len(prev_snap.channels)
      delta_str = f" ({delta:+d})" if delta else ""
      logger.info(
        "Reloaded Web Guide lineup from %s: %d channels%s across %d categories [%s].",
        self.xtream_dir,
        len(snap.channels),
        delta_str,
        len(snap.categories),
        snap.lineup_id,
      )
    return snap

  async def async_get_snapshot(
    self, *, force_reload: bool = False
  ) -> LineupSnapshot:
    """Async-safe snapshot retrieval offloading disk reloads off event
    loop.
    """
    sig = self._compute_signature()
    prev_snap = self._snapshot
    if (
      not force_reload and prev_snap is not None and sig == self._last_signature
    ):
      return prev_snap
    return await asyncio.to_thread(self.get_snapshot, force_reload=force_reload)

  def touch_epg_revision(self, *, xmltv_url: str = "") -> None:
    """Bump snapshot revision timestamp after a SQLite EPG commit."""
    snap = self.get_snapshot()
    if xmltv_url:
      self._epg_overlay_url = xmltv_url
      snap.xmltv_url = xmltv_url
    snap.loaded_at = self._next_updated_at()

  def _load_from_disk(self, *, force_reload: bool = False) -> LineupSnapshot:
    _, bundle_data = self._bundle_reader.read_cached(
      force_reload=force_reload,
      fallback_xmltv_url=self.fallback_xmltv_url,
    )
    if not bundle_data.initialized:
      return LineupSnapshot(
        lineup_id=bundle_data.lineup_id,
        provider=bundle_data.provider,
        market=bundle_data.market,
        postal_prefix=bundle_data.postal_prefix,
        timezone=bundle_data.timezone,
        loaded_at=bundle_data.loaded_at,
        xmltv_url=self._epg_overlay_url or bundle_data.xmltv_url,
      )

    normalized_cats = [
      GuideCategory(
        category_id=cat.category_id,
        category_name=cat.category_name,
        channel_count=cat.channel_count,
      )
      for cat in bundle_data.categories
    ]

    channels: list[WebChannel] = []
    channels_by_chno: dict[int, WebChannel] = {}

    for rec in bundle_data.streams:
      web_ch = WebChannel(
        chno=rec.num,
        sd_chno=rec.sd_chno,
        name=rec.name,
        callsign=rec.callsign,
        logo=rec.stream_icon,
        category_id=rec.category_id,
        category_name=rec.category_name,
        language=rec.language or "en",
        quality_tier=rec.quality_tier,
        epg_channel_id=rec.epg_channel_id,
        has_native_epg=rec.has_native_epg,
        stream_id=rec.stream_id,
        direct_source=rec.direct_source,
        fallback_sources=list(rec.fallback_sources),
        epg_aliases=rec.epg_aliases,
        gracenote_station_id=rec.gracenote_station_id,
      )
      channels.append(web_ch)
      channels_by_chno[rec.num] = web_ch

    channels.sort(key=lambda c: c.chno)

    snap = LineupSnapshot(
      lineup_id=bundle_data.lineup_id,
      provider=bundle_data.provider,
      market=bundle_data.market,
      postal_prefix=bundle_data.postal_prefix,
      timezone=bundle_data.timezone,
      loaded_at=bundle_data.loaded_at,
      xmltv_url=bundle_data.xmltv_url,
      categories=normalized_cats,
      channels=channels,
      channels_by_chno=channels_by_chno,
    )
    snap.build_epg_channel_lookup()
    snap.build_epg_candidate_chains()
    _ = snap.guide_fingerprint
    return snap
