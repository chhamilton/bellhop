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
import json
import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bellhop_web.api_contracts import (
  GuideCategoryDict as GuideCategoryDict,
)
from bellhop_web.api_contracts import (
  ProgramItemDict as ProgramItemDict,
)
from bellhop_web.api_contracts import (
  PublicChannelDict as PublicChannelDict,
)
from bellhop_web.export_bundle import (
  ExportBundleReader,
  as_str_dict,
  parse_m3u8_fallback,
)

logger = logging.getLogger("bellhop_web.store")


def _as_dict(val: object) -> dict[str, Any]:
  return as_str_dict(val)


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
  """Immutable in-memory snapshot of categories, channels, and EPG schedule items."""

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
  programs_by_epg_id: dict[str, list[ProgramItem]] = field(default_factory=dict)

  def build_epg_channel_lookup(self) -> dict[str, str]:
    """Build lowercase upstream channel ID -> canonical `epg_channel_id` lookup map."""
    lookup: dict[str, str] = {}
    for ch in self.channels:
      canonical = ch.epg_channel_id.strip()
      if not canonical:
        continue
      lookup[canonical.lower()] = canonical
      for alias in ch.epg_aliases:
        cleaned = alias.strip()
        if cleaned:
          lookup[cleaned.lower()] = canonical
    return lookup

  def epg_coverage_stats(
    self, *, now_ts: int | None = None
  ) -> tuple[int, int, int]:
    """Return `(total_programs, active_programs, max_stop_timestamp)`."""
    now = int(time.time()) if now_ts is None else now_ts
    recent_floor = now - 3600
    total = 0
    active = 0
    max_stop = 0
    for plist in self.programs_by_epg_id.values():
      for p in plist:
        total += 1
        if p.stop_timestamp > max_stop:
          max_stop = p.stop_timestamp
        if p.stop_timestamp == 0 or p.stop_timestamp > recent_floor:
          active += 1
    return total, active, max_stop


class LineupGuideStore:
  """Loads Phase (6) Xtream JSON / XMLTV outputs (or M3U8 fallback) with automatic hot-reload."""

  def __init__(
    self,
    xtream_dir: Path,
    *,
    m3u8_path: Path | None = None,
    xmltv_path: Path | None = None,
  ) -> None:
    self.xtream_dir = xtream_dir.resolve()
    self.m3u8_path = m3u8_path.resolve() if m3u8_path else None
    self.xmltv_override = xmltv_path.resolve() if xmltv_path else None
    extra_watched = [
      self.xtream_dir / "get_epg.json",
      self.xtream_dir / "xmltv.xml",
    ]
    if self.xmltv_override is not None:
      extra_watched.append(self.xmltv_override)
    self._bundle_reader = ExportBundleReader(
      self.xtream_dir,
      m3u8_path=self.m3u8_path,
      extra_watched_files=extra_watched,
    )
    self._last_signature: tuple[float, ...] = ()
    self._last_updated_at: float = 0.0
    self._snapshot: LineupSnapshot | None = None
    self._epg_overlay: dict[str, list[ProgramItem]] | None = None
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
    sig = self._compute_signature()
    prev_snap = self._snapshot
    if (
      not force_reload and prev_snap is not None and sig == self._last_signature
    ):
      return prev_snap

    snap = self._load_from_disk()
    if self._epg_overlay is not None:
      snap.programs_by_epg_id = {
        k: list(v) for k, v in self._epg_overlay.items()
      }
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

  def apply_epg_programs(
    self,
    programs_by_epg_id: Mapping[str, list[ProgramItem]],
    *,
    xmltv_url: str = "",
  ) -> int:
    """Overlay dynamically fetched/cached EPG programs into memory (read-only on `output/`)."""
    snap = self.get_snapshot()
    normalized: dict[str, list[ProgramItem]] = {}
    total_programs = 0
    for cid, plist in programs_by_epg_id.items():
      ordered = sorted(
        plist,
        key=lambda item: (item.start_timestamp, item.stop_timestamp, item.id),
      )
      if ordered:
        normalized[cid] = ordered
        total_programs += len(ordered)

    self._epg_overlay = normalized
    if xmltv_url:
      self._epg_overlay_url = xmltv_url
      snap.xmltv_url = xmltv_url
    snap.programs_by_epg_id = {k: list(v) for k, v in normalized.items()}
    snap.loaded_at = self._next_updated_at()
    return total_programs

  def _load_from_disk(self) -> LineupSnapshot:
    bundle_data = self._bundle_reader.read()
    epg_json_path = self.xtream_dir / "get_epg.json"
    xmltv_path = self.xmltv_override or (self.xtream_dir / "xmltv.xml")

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
    programs_by_epg_id: dict[str, list[ProgramItem]] = {}

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
      )
      channels.append(web_ch)
      channels_by_chno[rec.num] = web_ch

      if (
        rec.epg_channel_id
        and rec.epg_channel_id not in programs_by_epg_id
        and rec.inline_epg_listings
      ):
        programs_by_epg_id[rec.epg_channel_id] = [
          self._program_from_dict(p, default_cid=rec.epg_channel_id)
          for p in rec.inline_epg_listings
          if p.get("title")
        ]

    resolved_xmltv_url = bundle_data.xmltv_url
    if epg_json_path.is_file():
      epg_data = _as_dict(json.loads(epg_json_path.read_text(encoding="utf-8")))
      if not resolved_xmltv_url:
        resolved_xmltv_url = str(epg_data.get("xmltv_url") or "").strip()
      listings = epg_data.get("epg_listings")
      if isinstance(listings, list) and listings:
        programs_by_epg_id.clear()
        for p in listings:
          if not isinstance(p, Mapping) or not p.get("title"):
            continue
          cid = str(p.get("channel_id") or p.get("epg_id") or "").strip()
          if cid:
            programs_by_epg_id.setdefault(cid, []).append(
              self._program_from_dict(p, default_cid=cid)
            )
    elif xmltv_path.is_file():
      programs_by_epg_id = self._parse_xmltv(xmltv_path)

    for plist in programs_by_epg_id.values():
      plist.sort(
        key=lambda item: (item.start_timestamp, item.stop_timestamp, item.id)
      )

    channels.sort(key=lambda c: c.chno)

    return LineupSnapshot(
      lineup_id=bundle_data.lineup_id,
      provider=bundle_data.provider,
      market=bundle_data.market,
      postal_prefix=bundle_data.postal_prefix,
      timezone=bundle_data.timezone,
      loaded_at=bundle_data.loaded_at,
      xmltv_url=resolved_xmltv_url,
      categories=normalized_cats,
      channels=channels,
      channels_by_chno=channels_by_chno,
      programs_by_epg_id=programs_by_epg_id,
    )

  @staticmethod
  def _program_from_dict(
    data: Mapping[str, Any], *, default_cid: str
  ) -> ProgramItem:
    return ProgramItem(
      id=str(data.get("id") or ""),
      channel_id=str(
        data.get("channel_id") or data.get("epg_id") or default_cid
      ),
      title=str(data.get("title") or ""),
      sub_title=str(data.get("sub_title") or ""),
      description=str(data.get("description") or ""),
      category=str(data.get("category") or ""),
      start_timestamp=int(data.get("start_timestamp") or 0),
      stop_timestamp=int(data.get("stop_timestamp") or 0),
    )

  @staticmethod
  def _parse_xmltv(xml_path: Path) -> dict[str, list[ProgramItem]]:
    by_id: dict[str, list[ProgramItem]] = {}
    seq = 0
    with open_xml_stream(xml_path) as fh:
      try:
        for _ev, elem in ET.iterparse(fh, events=("end",)):
          tag = elem.tag.rsplit("}", 1)[-1]
          if tag == "programme":
            cid = (elem.attrib.get("channel") or "").strip()
            title_el = elem.find("title")
            title = (
              (title_el.text or "").strip() if title_el is not None else ""
            )
            if cid and title:
              seq += 1
              sub_el = elem.find("sub-title")
              desc_el = elem.find("desc")
              cat_el = elem.find("category")
              by_id.setdefault(cid, []).append(
                ProgramItem(
                  id=str(seq),
                  channel_id=cid,
                  title=title,
                  sub_title=(sub_el.text or "").strip()
                  if sub_el is not None
                  else "",
                  description=(desc_el.text or "").strip()
                  if desc_el is not None
                  else "",
                  category=(cat_el.text or "").strip()
                  if cat_el is not None
                  else "",
                  start_timestamp=parse_xmltv_ts(
                    elem.attrib.get("start") or ""
                  ),
                  stop_timestamp=parse_xmltv_ts(elem.attrib.get("stop") or ""),
                )
              )
            elem.clear()
          elif tag == "channel":
            elem.clear()
      except ET.ParseError:
        pass
    return by_id

  @staticmethod
  def _parse_m3u8_fallback(
    m3u_path: Path,
  ) -> tuple[list[dict[str, str]], list[dict[str, Any]], str]:
    return parse_m3u8_fallback(m3u_path)
