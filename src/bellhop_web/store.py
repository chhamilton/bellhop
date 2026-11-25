"""Read-only data store loading Phase (6) Xtream JSON & XMLTV outputs with hot-reload.

Public Web Guide Wire Layouts
-----------------------------
1. `GET /api/lineup`:
   ```json
   {
     "server_timestamp": 1763510400,
     "lineup": {
       "lineup_id": "CAN-BELLFIBE-MTL-X",
       "provider": "Bell Fibe TV",
       "market": "Montréal, QC",
       "postal_prefix": "H2Y",
       "timezone": "America/Montreal",
       "total_channels": 349,
       "filtered_channels": 349
     },
     "epg_status": { /* EpgStatusDict */ },
     "categories": [
       { "category_id": "1", "category_name": "Francophone - Généralistes", "channel_count": 24 }
     ],
     "channels": [ /* PublicChannelDict (direct_source stripped; local /api/stream/<chno>.ts URL) */ ]
   }
   ```
2. `GET /api/epg/<chno>`:
   ```json
   {
     "chno": 1108,
     "name": "RDS",
     "epg_channel_id": "RDS.ca",
     "programs": [ /* ProgramItemDict */ ]
   }
   ```
"""

from __future__ import annotations

import gzip
import io
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from bellhop_web.api_contracts import (
  GuideCategoryDict as GuideCategoryDict,
)
from bellhop_web.api_contracts import (
  ProgramItemDict as ProgramItemDict,
)
from bellhop_web.api_contracts import (
  PublicChannelDict as PublicChannelDict,
)
from bellhop_web.export_bundle import ExportBundleReader

logger = logging.getLogger("bellhop_web.store")


def parse_xmltv_ts(raw: str) -> int:
  """Parse an XMLTV `YYYYMMDDHHMMSS [±HHMM]` timestamp into a Unix epoch."""
  cleaned = raw.strip()
  if not cleaned:
    return 0
  parts = cleaned.split()
  dt_part = parts[0][:14]
  tz_part = parts[1] if len(parts) > 1 else "+0000"
  try:
    if len(dt_part) == 14 and len(tz_part) == 5 and tz_part[0] in "+-":
      dt = datetime.strptime(f"{dt_part} {tz_part}", "%Y%m%d%H%M%S %z")
    else:
      dt = datetime.strptime(dt_part.ljust(14, "0"), "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
      )
    return int(dt.timestamp())
  except ValueError:
    return 0


def open_xml_stream(path: Path) -> io.BufferedReader | gzip.GzipFile:
  """Open a plain or gzip-compressed XMLTV file for binary streaming."""
  with path.open("rb") as probe_fh:
    magic = probe_fh.read(2)
  if magic == b"\x1f\x8b":
    return gzip.open(path, "rb")
  return path.open("rb")


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
class ProgramItem:
  """Normalized EPG program schedule item."""

  id: str
  channel_id: str
  title: str
  sub_title: str = ""
  description: str = ""
  category: str = ""
  start_timestamp: int = 0
  stop_timestamp: int = 0

  def to_dict(self, *, now_ts: int | None = None) -> ProgramItemDict:
    d: ProgramItemDict = {
      "id": self.id,
      "channel_id": self.channel_id,
      "title": self.title,
      "sub_title": self.sub_title,
      "description": self.description,
      "category": self.category,
      "start_timestamp": self.start_timestamp,
      "stop_timestamp": self.stop_timestamp,
    }
    if (
      now_ts is not None
      and self.start_timestamp > 0
      and self.stop_timestamp > self.start_timestamp
    ):
      if self.start_timestamp <= now_ts < self.stop_timestamp:
        pct = (
          (now_ts - self.start_timestamp)
          / (self.stop_timestamp - self.start_timestamp)
          * 100.0
        )
        d["progress_pct"] = round(min(100.0, max(0.0, pct)), 1)
        d["is_live"] = True
      else:
        d["is_live"] = False
    return d


_BUMPER_OVERLAP_SEC = 300


def _program_quality_key(
  item: ProgramItem, seq_idx: int
) -> tuple[int, int, int, int]:
  return (
    1 if "live" in item.title.lower() else 0,
    len(item.description),
    len(item.sub_title),
    seq_idx,
  )


def reconcile_non_overlapping_schedule(
  items: Sequence[ProgramItem],
) -> list[ProgramItem]:
  """Deduplicate and solve 1D weighted interval scheduling so programs never overlap."""
  if not items:
    return []

  # 1. Exact `(start, stop, title)` dedup + separate finite vs open-ended entries
  exact_map: dict[tuple[int, int, str], tuple[ProgramItem, int]] = {}
  open_ended: list[ProgramItem] = []
  for idx, p in enumerate(items):
    if p.start_timestamp <= 0 or not p.title:
      continue
    if p.stop_timestamp <= 0:
      open_ended.append(p)
      continue
    if p.stop_timestamp <= p.start_timestamp:
      continue
    key = (p.start_timestamp, p.stop_timestamp, p.title.casefold())
    prev = exact_map.get(key)
    if prev is None or _program_quality_key(p, idx) > _program_quality_key(
      prev[0], prev[1]
    ):
      exact_map[key] = (p, idx)

  if not exact_map:
    open_ended.sort(key=lambda p: (p.start_timestamp, p.id))
    return open_ended

  # 2. Sort finite intervals and coalesce shifted same-title duplicates from multi-grabbers
  ordered_pairs = sorted(
    exact_map.values(),
    key=lambda pair: (
      pair[0].start_timestamp,
      pair[0].stop_timestamp,
      pair[1],
    ),
  )
  coalesced: list[tuple[ProgramItem, int]] = []
  for cand, idx in ordered_pairs:
    if coalesced:
      prev_p, prev_idx = coalesced[-1]
      if prev_p.title.casefold() == cand.title.casefold():
        ov = min(prev_p.stop_timestamp, cand.stop_timestamp) - max(
          prev_p.start_timestamp, cand.start_timestamp
        )
        min_dur = min(
          prev_p.stop_timestamp - prev_p.start_timestamp,
          cand.stop_timestamp - cand.start_timestamp,
        )
        if ov > 0 and ov >= 0.40 * min_dur:
          if _program_quality_key(cand, idx) >= _program_quality_key(
            prev_p, prev_idx
          ):
            coalesced[-1] = (cand, idx)
          continue
    coalesced.append((cand, idx))

  # Fast path when already strictly non-overlapping
  has_any_overlap = any(
    coalesced[i][0].stop_timestamp > coalesced[i + 1][0].start_timestamp
    for i in range(len(coalesced) - 1)
  )
  if not has_any_overlap:
    return [p for p, _ in coalesced]

  # 3. 1D Weighted Interval Scheduling DP (with <=5m boundary bumper tolerance + contiguity bonus)
  n = len(coalesced)
  dp = [0.0] * n
  parent = [-1] * n

  def _base_weight(p: ProgramItem, seq_idx: int) -> float:
    dur = float(p.stop_timestamp - p.start_timestamp)
    live_bonus = 300.0 if "live" in p.title.lower() else 0.0
    desc_bonus = float(min(180, len(p.description)) + min(60, len(p.sub_title)))
    return dur + live_bonus + desc_bonus + 0.01 * seq_idx

  for j in range(n):
    pj, idx_j = coalesced[j]
    dur_j = pj.stop_timestamp - pj.start_timestamp
    wj = _base_weight(pj, idx_j)
    best_val = wj
    best_prev = -1
    for i in range(j - 1, -1, -1):
      pi, _ = coalesced[i]
      if pi.start_timestamp >= pj.start_timestamp:
        continue
      if pj.stop_timestamp <= pi.stop_timestamp:
        continue
      ov = pi.stop_timestamp - pj.start_timestamp
      dur_i = pi.stop_timestamp - pi.start_timestamp
      if ov > 0 and (ov > _BUMPER_OVERLAP_SEC or ov > 0.20 * min(dur_i, dur_j)):
        continue
      trimmed_penalty = float(max(0, ov))
      contig_bonus = (
        240.0 if abs(pi.stop_timestamp - pj.start_timestamp) <= 60 else 0.0
      )
      cand_val = dp[i] + wj - trimmed_penalty + contig_bonus
      if cand_val > best_val:
        best_val = cand_val
        best_prev = i
    dp[j] = best_val
    parent[j] = best_prev

  best_end = max(range(n), key=lambda k: dp[k])
  chosen_rev: list[ProgramItem] = []
  cur = best_end
  while cur != -1:
    chosen_rev.append(coalesced[cur][0])
    cur = parent[cur]
  chosen = list(reversed(chosen_rev))

  # 4. Trim any minor <=5m boundary bumper overlap so stop_i <= start_{i+1} holds strictly
  result: list[ProgramItem] = []
  for idx, item in enumerate(chosen):
    if (
      idx + 1 < len(chosen)
      and item.stop_timestamp > chosen[idx + 1].start_timestamp
    ):
      item = replace(item, stop_timestamp=chosen[idx + 1].start_timestamp)
    if item.stop_timestamp > item.start_timestamp:
      result.append(item)
  return result


@dataclass(slots=True)
class WebChannel:
  """A mapped lineup channel ready for web guide display and local stream proxying."""

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
    """Serialize channel for browser UI without leaking upstream provider credentials."""
    now_prog: ProgramItemDict | None = None
    next_prog: ProgramItemDict | None = None
    upcoming: list[ProgramItemDict] = []
    recent_floor = now_ts - 3600

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

  lineup_id: str = "CAN-BELLFIBE-MTL-X"
  provider: str = "Bell Fibe TV"
  market: str = "Montréal, QC"
  postal_prefix: str = "H2Y"
  timezone: str = "America/Montreal"
  loaded_at: float = 0.0
  xmltv_url: str = ""
  categories: list[GuideCategory] = field(default_factory=list)
  channels: list[WebChannel] = field(default_factory=list)
  channels_by_chno: dict[int, WebChannel] = field(default_factory=dict)

  def build_epg_channel_lookup(self) -> dict[str, str]:
    """Build lowercase upstream channel ID -> canonical `epg_channel_id` lookup map."""
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
    """Build ordered fallback chains `canonical_epg_id -> (primary_id, fallback_alias, ...)`."""
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


class LineupGuideStore:
  """Loads Phase (6) Xtream JSON outputs (or M3U8 fallback) with automatic hot-reload."""

  def __init__(
    self,
    xtream_dir: Path,
    *,
    m3u8_path: Path | None = None,
  ) -> None:
    self.xtream_dir = xtream_dir.resolve()
    self.m3u8_path = m3u8_path.resolve() if m3u8_path else None
    self._bundle_reader = ExportBundleReader(
      self.xtream_dir,
      m3u8_path=self.m3u8_path,
    )
    self._last_signature: tuple[float, ...] = ()
    self._last_updated_at: float = 0.0
    self._snapshot: LineupSnapshot | None = None
    self._epg_overlay_url: str = ""

  @property
  def bundle_signature(self) -> tuple[float, ...]:
    """Return the `(mtime_ns, size)` signature of the currently loaded export bundle."""
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
    """Return the current `LineupSnapshot`, reloading from disk when files changed."""
    sig = self._compute_signature()
    prev_snap = self._snapshot
    if (
      not force_reload and prev_snap is not None and sig == self._last_signature
    ):
      return prev_snap

    snap = self._load_from_disk()
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

  def touch_epg_revision(self, *, xmltv_url: str = "") -> None:
    """Bump snapshot revision timestamp after a SQLite EPG commit."""
    snap = self.get_snapshot()
    if xmltv_url:
      self._epg_overlay_url = xmltv_url
      snap.xmltv_url = xmltv_url
    snap.loaded_at = self._next_updated_at()

  def _load_from_disk(self) -> LineupSnapshot:
    bundle_data = self._bundle_reader.read()
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

    return LineupSnapshot(
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
