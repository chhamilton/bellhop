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
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

M3U_ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')


class ProgramItemDict(TypedDict, total=False):
  """Serialized program schedule entry returned in `/api/lineup` and `/api/epg/<chno>`."""

  id: str
  channel_id: str
  title: str
  sub_title: str
  description: str
  category: str
  start_timestamp: int
  stop_timestamp: int
  progress_pct: float
  is_live: bool


class GuideCategoryDict(TypedDict):
  """Serialized lineup category returned in `/api/lineup`."""

  category_id: str
  category_name: str
  channel_count: int


class PublicChannelDict(TypedDict):
  """Browser-safe channel payload returned in `/api/lineup` (`direct_source` never exposed)."""

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
  stream_url: str
  hls_url: str
  now_playing: ProgramItemDict | None
  up_next: ProgramItemDict | None
  schedule: list[ProgramItemDict]


def _as_dict(val: object) -> dict[str, Any]:
  return dict(val) if isinstance(val, Mapping) else {}


def parse_xmltv_ts(raw: str) -> int:
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
    self._last_signature: tuple[float, ...] = ()
    self._snapshot: LineupSnapshot | None = None
    self._epg_overlay: dict[str, list[ProgramItem]] | None = None
    self._epg_overlay_url: str = ""

  def _watched_files(self) -> list[Path]:
    files = [
      self.xtream_dir / "bellhop_xtream_playlist.json",
      self.xtream_dir / "bell_fibe_xtream_playlist.json",
      self.xtream_dir / "get_live_streams.json",
      self.xtream_dir / "get_live_categories.json",
      self.xtream_dir / "get_epg.json",
      self.xtream_dir / "xmltv.xml",
    ]
    if self.m3u8_path is not None:
      files.append(self.m3u8_path)
    if self.xmltv_override is not None:
      files.append(self.xmltv_override)
    return files

  def _compute_signature(self) -> tuple[float, ...]:
    sig: list[float] = []
    for p in self._watched_files():
      try:
        sig.append(p.stat().st_mtime if p.is_file() else 0.0)
      except OSError:
        sig.append(0.0)
    return tuple(sig)

  def get_snapshot(self, *, force_reload: bool = False) -> LineupSnapshot:
    sig = self._compute_signature()
    if (
      not force_reload
      and self._snapshot is not None
      and sig == self._last_signature
    ):
      return self._snapshot

    snap = self._load_from_disk()
    if self._epg_overlay is not None:
      snap.programs_by_epg_id = {
        k: list(v) for k, v in self._epg_overlay.items()
      }
      if self._epg_overlay_url and not snap.xmltv_url:
        snap.xmltv_url = self._epg_overlay_url
    self._snapshot = snap
    self._last_signature = sig
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
    snap.loaded_at = time.time()
    return total_programs

  def _load_from_disk(self) -> LineupSnapshot:
    primary_bundle = self.xtream_dir / "bellhop_xtream_playlist.json"
    legacy_bundle = self.xtream_dir / "bell_fibe_xtream_playlist.json"
    bundle_path = primary_bundle if primary_bundle.is_file() else legacy_bundle
    streams_path = self.xtream_dir / "get_live_streams.json"
    cats_path = self.xtream_dir / "get_live_categories.json"
    epg_json_path = self.xtream_dir / "get_epg.json"
    xmltv_path = self.xmltv_override or (self.xtream_dir / "xmltv.xml")

    lineup_info: dict[str, Any] = {}
    server_info: dict[str, Any] = {}
    bundle_xmltv_url = ""
    raw_categories: list[Any] = []
    raw_streams: list[Any] = []

    if bundle_path.is_file():
      bundle = _as_dict(json.loads(bundle_path.read_text(encoding="utf-8")))
      lineup_info = _as_dict(bundle.get("lineup_info"))
      server_info = _as_dict(bundle.get("server_info"))
      epg_guide_block = _as_dict(bundle.get("epg_guide"))
      bundle_xmltv_url = str(
        server_info.get("xmltv_url") or epg_guide_block.get("xmltv_url") or ""
      ).strip()
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

    if not raw_streams and self.m3u8_path and self.m3u8_path.is_file():
      raw_categories, raw_streams, m3u_xmltv_url = self._parse_m3u8_fallback(
        self.m3u8_path
      )
      if m3u_xmltv_url and not bundle_xmltv_url:
        bundle_xmltv_url = m3u_xmltv_url

    if not raw_streams:
      # Allow `bellhop serve` to boot cleanly on a fresh/empty volume (e.g. first
      # Docker startup) and hot-reload automatically once `bellhop run` exports.
      return LineupSnapshot(
        lineup_id=str(lineup_info.get("lineup_id") or "UNINITIALIZED"),
        provider=str(lineup_info.get("provider") or "Bellhop"),
        market=str(lineup_info.get("market") or ""),
        postal_prefix=str(lineup_info.get("postal_prefix") or ""),
        timezone=str(server_info.get("timezone") or "America/Montreal"),
        loaded_at=time.time(),
        xmltv_url=self._epg_overlay_url or bundle_xmltv_url,
      )

    cat_by_id: dict[str, GuideCategory] = {}
    normalized_cats: list[GuideCategory] = []
    for raw_cat in raw_categories:
      item = _as_dict(raw_cat)
      cid = str(item.get("category_id") or "").strip()
      cname = str(item.get("category_name") or "General")
      if cid and cid not in cat_by_id:
        cat = GuideCategory(category_id=cid, category_name=cname)
        cat_by_id[cid] = cat
        normalized_cats.append(cat)

    channels: list[WebChannel] = []
    channels_by_chno: dict[int, WebChannel] = {}
    programs_by_epg_id: dict[str, list[ProgramItem]] = {}

    for idx, raw_entry in enumerate(raw_streams):
      entry = _as_dict(raw_entry)
      if not entry:
        continue
      chno = int(entry.get("num") or (idx + 1))
      guide = _as_dict(entry.get("guide"))
      cat_id = str(entry.get("category_id") or "1")
      existing_cat = cat_by_id.get(cat_id)
      cat_name = str(
        entry.get("category_name")
        or guide.get("group_title")
        or (existing_cat.category_name if existing_cat else None)
        or "General"
      )
      if existing_cat is None:
        existing_cat = GuideCategory(category_id=cat_id, category_name=cat_name)
        cat_by_id[cat_id] = existing_cat
        normalized_cats.append(existing_cat)

      epg_id = str(
        entry.get("epg_channel_id") or guide.get("epg_channel_id") or ""
      ).strip()
      has_native_epg = bool(guide.get("has_native_epg", bool(epg_id)))
      epg_aliases_list: list[str] = []
      if epg_id:
        epg_aliases_list.append(epg_id)
      official_tvg_id = str(guide.get("official_tvg_id") or "").strip()
      if official_tvg_id and official_tvg_id not in epg_aliases_list:
        epg_aliases_list.append(official_tvg_id)
      upstream_epg_id = str(guide.get("upstream_epg_channel_id") or "").strip()
      if (
        has_native_epg
        and upstream_epg_id
        and upstream_epg_id not in epg_aliases_list
      ):
        epg_aliases_list.append(upstream_epg_id)

      sd_raw = guide.get("sd_chno")
      sd_chno = int(sd_raw) if sd_raw is not None else None
      callsign_raw = entry.get("callsign") or guide.get("callsign")
      raw_fallbacks = guide.get("fallback_sources")
      fallbacks = (
        [str(u) for u in raw_fallbacks if u and "***" not in str(u)]
        if isinstance(raw_fallbacks, list)
        else []
      )

      web_ch = WebChannel(
        chno=chno,
        sd_chno=sd_chno,
        name=str(entry.get("name") or f"Channel {chno}"),
        callsign=str(callsign_raw) if callsign_raw else None,
        logo=str(entry.get("stream_icon") or guide.get("official_logo") or ""),
        category_id=cat_id,
        category_name=cat_name,
        language=str(guide.get("language") or "en"),
        quality_tier=str(guide.get("quality_tier") or "unknown"),
        epg_channel_id=epg_id,
        has_native_epg=has_native_epg,
        stream_id=int(entry.get("stream_id") or 0),
        direct_source=str(entry.get("direct_source") or ""),
        fallback_sources=fallbacks,
        epg_aliases=tuple(epg_aliases_list),
      )
      channels.append(web_ch)
      channels_by_chno[chno] = web_ch

      if epg_id and epg_id not in programs_by_epg_id:
        inline_list = guide.get("epg_listings")
        if isinstance(inline_list, list) and inline_list:
          programs_by_epg_id[epg_id] = [
            self._program_from_dict(p, default_cid=epg_id)
            for p in inline_list
            if isinstance(p, Mapping) and p.get("title")
          ]

    if epg_json_path.is_file():
      epg_data = _as_dict(json.loads(epg_json_path.read_text(encoding="utf-8")))
      if not bundle_xmltv_url:
        bundle_xmltv_url = str(epg_data.get("xmltv_url") or "").strip()
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

    counts_by_cat: dict[str, int] = {}
    for ch in channels:
      counts_by_cat[ch.category_id] = counts_by_cat.get(ch.category_id, 0) + 1
    for cat in normalized_cats:
      cat.channel_count = counts_by_cat.get(cat.category_id, 0)

    channels.sort(key=lambda c: c.chno)

    return LineupSnapshot(
      lineup_id=str(lineup_info.get("lineup_id") or "CAN-BELLFIBE-MTL-X"),
      provider=str(lineup_info.get("provider") or "Bell Fibe TV"),
      market=str(lineup_info.get("market") or "Montréal, QC"),
      postal_prefix=str(lineup_info.get("postal_prefix") or "H2Y"),
      timezone=str(server_info.get("timezone") or "America/Montreal"),
      loaded_at=time.time(),
      xmltv_url=bundle_xmltv_url,
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
    cats_by_name: dict[str, str] = {}
    cats: list[dict[str, str]] = []
    streams: list[dict[str, Any]] = []
    xmltv_url = ""
    pending_attrs: dict[str, str] | None = None
    pending_name = ""

    for raw_line in m3u_path.read_text(
      encoding="utf-8", errors="replace"
    ).splitlines():
      line = raw_line.strip()
      if not line:
        continue
      if line.startswith("#EXTM3U"):
        hdr_attrs = dict(M3U_ATTR_RE.findall(line))
        xmltv_url = (
          hdr_attrs.get("url-tvg") or hdr_attrs.get("x-tvg-url") or ""
        ).strip()
        continue
      if line.startswith("#EXTINF:"):
        attrs = dict(M3U_ATTR_RE.findall(line))
        comma_idx = line.rfind(",")
        pending_name = (
          line[comma_idx + 1 :].strip() if comma_idx >= 0 else "Channel"
        )
        pending_attrs = attrs
      elif not line.startswith("#") and pending_attrs is not None:
        grp = pending_attrs.get("group-title", "General")
        if grp not in cats_by_name:
          cid = str(len(cats) + 1)
          cats_by_name[grp] = cid
          cats.append({"category_id": cid, "category_name": grp})
        chno_str = pending_attrs.get("tvg-chno") or str(len(streams) + 1)
        try:
          chno = int(chno_str)
        except ValueError:
          chno = len(streams) + 1
        streams.append(
          {
            "num": chno,
            "name": pending_name,
            "stream_icon": pending_attrs.get("tvg-logo", ""),
            "epg_channel_id": pending_attrs.get("tvg-id", ""),
            "category_id": cats_by_name[grp],
            "category_name": grp,
            "direct_source": line,
          }
        )
        pending_attrs = None
    return cats, streams, xmltv_url
