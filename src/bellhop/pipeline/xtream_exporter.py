"""Phase (6): Export the mapped lineup as structured Xtream Codes JSON artifacts
with passthrough EPG.

Exported Directory Layout (`output/xtream/`)
--------------------------------------------
1. `get_live_categories.json` (`list[ExportedCategoryDict]`):
   ```json
   [{ "category_id": "1", "category_name": "Francophone - Généralistes",
   "parent_id": 0 }]
   ```
2. `get_live_streams.json` (`list[ExportedLiveStream]`):
   ```json
   [
     {
       "num": 1108,
       "name": "RDS",
       "stream_type": "live",
       "stream_id": 410108,
       "stream_icon": "https://.../rds-ca.png",
       "epg_channel_id": "RDS.ca",
       "added": "1763510400",
       "is_adult": 0,
       "category_id": "2",
       "category_ids": [2],
       "category_name": "Sports",
       "custom_sid": "1108",
       "tv_archive": 0,
       "direct_source": "http://provider/live/user/pass/410108.ts",
       "tv_archive_duration": 0,
       "callsign": "RDS",
       "guide": {
         "lineup_id": "CAN-BELLFIBE-MTL-X",
         "provider": "Sample Fiber TV",
         "market": "Sample Market",
         "postal_prefix": "",
         "chno": 1108,
         "hd_chno": 1108,
         "sd_chno": 108,
         "official_name": "RDS",
         "callsign": "RDS",
         "official_logo": "https://.../rds-ca.png",
         "group_title": "Sports",
         "language": "fr",
         "epg_channel_id": "RDS.ca",
         "official_tvg_id": "RDS.ca",
         "upstream_epg_channel_id": "RDS.ca",
         "gracenote_station_id": "14873",
         "has_native_epg": true,
         "quality_tier": "fhd",
         "fallback_sources": ["http://provider/live/user/pass/410999.ts"]
       }
     }
   ]
   ```
3. `bellhop_xtream_playlist.json` (`XtreamExportBundle`):
   Unified bundle containing `user_info`, `server_info` (with passthrough
   `xmltv_url`),
   `lineup_info`, `categories`, `live_categories`, `live_streams`,
   `available_channels`,
   and `epg_guide` (`mode: "passthrough"`, `verification`, `channels`).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlparse

import aiohttp

from bellhop.common.export_bundle import (
  assign_unique_slot_epg_ids,
  resolve_preferred_epg_base_id,
)
from bellhop.common.file_utils import write_private_text
from bellhop.config import AppConfig
from bellhop.epg.xmltv import (
  EpgVerificationResult,
  build_passthrough_xmltv_url,
  verify_epg_endpoint,
)
from bellhop.pipeline.lineup_loader import CableLineup
from bellhop.pipeline.models import MappedChannel, XtreamAccountInfo
from bellhop.pipeline.playlist import (
  _expand_entries_by_numbering,
  _normalize_base_url,
  _resolve_stream_url,
  resolve_group_display_map,
  resolve_playback_url,
)


class ExportedCategoryDict(TypedDict):
  """Xtream `get_live_categories` JSON entry."""

  category_id: str
  category_name: str
  parent_id: int


@dataclass(slots=True)
class ExportedCategory:
  """Lineup category exported for Xtream Codes `get_live_categories`."""

  category_id: str
  category_name: str
  parent_id: int = 0

  def to_dict(self) -> ExportedCategoryDict:
    return {
      "category_id": self.category_id,
      "category_name": self.category_name,
      "parent_id": self.parent_id,
    }


@dataclass(slots=True)
class ExportedStreamGuideMeta:
  """Enriched cable-lineup guide metadata attached under `guide` on each
  exported stream.
  """

  lineup_id: str
  provider: str
  market: str
  postal_prefix: str
  chno: int
  hd_chno: int
  sd_chno: int | None
  official_name: str
  callsign: str | None
  official_logo: str
  group_title: str
  language: str
  epg_channel_id: str
  official_tvg_id: str | None
  upstream_epg_channel_id: str | None
  gracenote_station_id: str | None
  has_native_epg: bool
  quality_tier: str
  fallback_sources: list[str] = field(default_factory=list)

  def to_dict(self) -> dict[str, Any]:
    return {
      "lineup_id": self.lineup_id,
      "provider": self.provider,
      "market": self.market,
      "postal_prefix": self.postal_prefix,
      "chno": self.chno,
      "hd_chno": self.hd_chno,
      "sd_chno": self.sd_chno,
      "official_name": self.official_name,
      "callsign": self.callsign,
      "official_logo": self.official_logo,
      "group_title": self.group_title,
      "language": self.language,
      "epg_channel_id": self.epg_channel_id,
      "official_tvg_id": self.official_tvg_id,
      "upstream_epg_channel_id": self.upstream_epg_channel_id,
      "gracenote_station_id": self.gracenote_station_id,
      "has_native_epg": self.has_native_epg,
      "quality_tier": self.quality_tier,
      "fallback_sources": list(self.fallback_sources),
    }


@dataclass(slots=True)
class ExportedLiveStream:
  """Single dial position entry written to `get_live_streams.json` and
  `live_streams[]`.
  """

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
  tv_archive: int
  direct_source: str
  callsign: str | None
  guide: ExportedStreamGuideMeta
  stream_type: str = "live"
  is_adult: int = 0
  tv_archive_duration: int = 0

  def to_dict(self) -> dict[str, Any]:
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
      "category_name": self.category_name,
      "custom_sid": self.custom_sid,
      "tv_archive": self.tv_archive,
      "direct_source": self.direct_source,
      "tv_archive_duration": self.tv_archive_duration,
      "callsign": self.callsign,
      "guide": self.guide.to_dict(),
    }


@dataclass(slots=True)
class ExportedChannelGuideBinding:
  """Canonical EPG channel binding stored in
  `epg_guide.channels[epg_channel_id]`.
  """

  epg_channel_id: str
  chno: int
  chnos: list[int]
  name: str
  callsign: str | None
  icon: str
  group: str
  language: str
  gracenote_station_id: str | None
  has_native_epg: bool

  def register_dial_position(
    self,
    chno: int,
    *,
    is_primary_hd: bool,
    display_name: str,
  ) -> None:
    if chno not in self.chnos:
      self.chnos.append(chno)
    if is_primary_hd:
      self.chno = chno
      self.name = display_name

  def to_dict(self) -> dict[str, Any]:
    return {
      "epg_channel_id": self.epg_channel_id,
      "chno": self.chno,
      "chnos": list(self.chnos),
      "name": self.name,
      "callsign": self.callsign,
      "icon": self.icon,
      "group": self.group,
      "language": self.language,
      "gracenote_station_id": self.gracenote_station_id,
      "has_native_epg": self.has_native_epg,
    }


@dataclass(slots=True)
class XtreamExportBundle:
  """Structured representation of `bellhop_xtream_playlist.json`."""

  user_info: dict[str, Any]
  server_info: dict[str, Any]
  lineup_info: dict[str, Any]
  categories: list[ExportedCategory]
  streams: list[ExportedLiveStream]
  epg_xmltv_url: str
  epg_verification: EpgVerificationResult | None
  channel_guides: dict[str, ExportedChannelGuideBinding]

  def to_export_tuple(
    self,
  ) -> tuple[dict[str, Any], list[ExportedCategoryDict], list[dict[str, Any]]]:
    cat_dicts = [c.to_dict() for c in self.categories]
    stream_dicts = [s.to_dict() for s in self.streams]
    available_channels = {str(s["num"]): s for s in stream_dicts}
    epg_guide_block: dict[str, Any] = {
      "mode": "passthrough",
      "xmltv_url": self.epg_xmltv_url,
      "verified": bool(self.epg_verification.verified)
      if self.epg_verification
      else None,
      "verification": self.epg_verification.to_dict()
      if self.epg_verification
      else None,
      "channels": {cid: g.to_dict() for cid, g in self.channel_guides.items()},
    }
    bundle_dict: dict[str, Any] = {
      "user_info": self.user_info,
      "server_info": self.server_info,
      "lineup_info": self.lineup_info,
      "categories": {"live": cat_dicts},
      "live_categories": cat_dicts,
      "live_streams": stream_dicts,
      "available_channels": available_channels,
      "epg_guide": epg_guide_block,
    }
    return bundle_dict, cat_dicts, stream_dicts


@dataclass(slots=True)
class XtreamExportResult:
  """Paths and statistics produced by Phase (6) Xtream JSON playlist export."""

  bundle_path: Path
  live_streams_path: Path
  live_categories_path: Path
  emitted_streams: int
  emitted_categories: int
  channels_with_official_logo: int
  channels_with_guide_id: int
  xmltv_url: str = ""
  xmltv_url_redacted: str = ""
  epg_verification: EpgVerificationResult | None = None

  @property
  def epg_verified(self) -> bool:
    return bool(self.epg_verification and self.epg_verification.verified)


def _resolve_canonical_epg_id(mapped: MappedChannel) -> str:
  """Determine the canonical EPG channel ID for Xtream JSON passthrough
  binding.
  """
  ch = mapped.channel
  cand = mapped.selected
  stream = cand.stream if cand is not None else None
  return resolve_preferred_epg_base_id(
    chno=ch.chno,
    gracenote_station_id=ch.gracenote_station_id,
    has_native_epg=cand.has_native_epg if cand is not None else False,
    upstream_epg_channel_id=stream.epg_channel_id if stream is not None else "",
    official_tvg_id=ch.tvg_id or "",
  )


def build_xtream_export_bundle(
  lineup: CableLineup,
  mappings: list[MappedChannel],
  config: AppConfig,
  *,
  account: XtreamAccountInfo | None = None,
  epg_verification: EpgVerificationResult | None = None,
) -> XtreamExportBundle:
  """Build a strongly-typed `XtreamExportBundle` from mapped channels and lineup
  metadata.
  """
  now_epoch = int(time.time())
  now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

  entries = _expand_entries_by_numbering(
    mappings,
    config.lineup.numbering_mode,
    collapse_sd_dupes=config.lineup.collapse_sd_dupes,
  )
  group_labels = resolve_group_display_map(
    mappings,
    config.lineup.numbering_mode,
    collapse_sd_dupes=config.lineup.collapse_sd_dupes,
  )

  category_id_by_group: dict[str, str] = {}
  categories: list[ExportedCategory] = []
  for _chno, _name, mapped in entries:
    if mapped.selected is None:
      continue
    grp = group_labels.get(mapped.channel.group, mapped.channel.group)
    if grp not in category_id_by_group:
      cat_id = str(len(categories) + 1)
      category_id_by_group[grp] = cat_id
      categories.append(
        ExportedCategory(category_id=cat_id, category_name=grp, parent_id=0)
      )

  xt = config.xtream
  base_url = (
    _normalize_base_url(xt.server_url) if xt.server_url else "http://localhost"
  )
  parsed_url = urlparse(base_url)
  upstream_xmltv_url = build_passthrough_xmltv_url(config, redact_secret=False)

  unique_epg_by_chno = assign_unique_slot_epg_ids(
    [
      (
        chno,
        mapped.channel.sd_chno,
        resolve_preferred_epg_base_id(
          chno=chno,
          gracenote_station_id=mapped.channel.gracenote_station_id,
          has_native_epg=mapped.selected.has_native_epg,
          upstream_epg_channel_id=mapped.selected.stream.epg_channel_id,
          official_tvg_id=mapped.channel.tvg_id or "",
        ),
      )
      for chno, _name, mapped in entries
      if mapped.selected is not None
    ]
  )

  streams: list[ExportedLiveStream] = []
  channel_guides: dict[str, ExportedChannelGuideBinding] = {}

  for chno, display_name, mapped in entries:
    if mapped.selected is None:
      continue

    ch = mapped.channel
    cand = mapped.selected
    stream = cand.stream
    grp_label = group_labels.get(ch.group, ch.group)
    cat_id = category_id_by_group[grp_label]
    cat_id_int = int(cat_id)

    official_logo = ch.logo or stream.stream_icon or ""
    canonical_epg_id = unique_epg_by_chno.get(
      chno, _resolve_canonical_epg_id(mapped)
    )
    playback_url = resolve_playback_url(mapped, config)

    fallback_sources: list[str] = []
    for alt_cand in mapped.candidates:
      alt_url = _resolve_stream_url(alt_cand.stream, config)
      if (
        alt_url and alt_url != playback_url and alt_url not in fallback_sources
      ):
        fallback_sources.append(alt_url)

    guide_meta = ExportedStreamGuideMeta(
      lineup_id=lineup.lineup_id,
      provider=lineup.provider,
      market=lineup.market,
      postal_prefix=lineup.postal_prefix,
      chno=chno,
      hd_chno=ch.chno,
      sd_chno=ch.sd_chno,
      official_name=display_name,
      callsign=ch.callsign,
      official_logo=official_logo,
      group_title=grp_label,
      language=ch.language,
      epg_channel_id=canonical_epg_id,
      official_tvg_id=ch.tvg_id,
      upstream_epg_channel_id=stream.epg_channel_id or None,
      gracenote_station_id=ch.gracenote_station_id,
      has_native_epg=cand.has_native_epg,
      quality_tier=cand.quality_tier,
      fallback_sources=fallback_sources,
    )

    streams.append(
      ExportedLiveStream(
        num=chno,
        name=display_name,
        stream_id=stream.stream_id,
        stream_icon=official_logo,
        epg_channel_id=canonical_epg_id,
        added=str(now_epoch),
        category_id=cat_id,
        category_ids=[cat_id_int],
        category_name=grp_label,
        custom_sid=str(chno),
        tv_archive=1 if stream.tv_archive else 0,
        direct_source=playback_url,
        callsign=ch.callsign,
        guide=guide_meta,
      )
    )

    existing_guide = channel_guides.get(canonical_epg_id)
    if existing_guide is None:
      channel_guides[canonical_epg_id] = ExportedChannelGuideBinding(
        epg_channel_id=canonical_epg_id,
        chno=chno,
        chnos=[chno],
        name=display_name,
        callsign=ch.callsign,
        icon=official_logo,
        group=ch.group,
        language=ch.language,
        gracenote_station_id=ch.gracenote_station_id,
        has_native_epg=cand.has_native_epg,
      )
    else:
      existing_guide.register_dial_position(
        chno,
        is_primary_hd=(chno == ch.chno),
        display_name=display_name,
      )

  acc = account or XtreamAccountInfo(
    username=xt.username or "bellhop_user", status="Active"
  )
  user_info: dict[str, Any] = {
    "username": acc.username,
    "message": f"{lineup.provider} ({lineup.market}) [{lineup.lineup_id}]",
    "auth": 1,
    "status": acc.status,
    "exp_date": acc.exp_date,
    "is_trial": "1" if acc.is_trial else "0",
    "active_cons": str(acc.active_connections),
    "created_at": str(now_epoch),
    "max_connections": str(acc.max_connections),
    "allowed_output_formats": list(acc.allowed_output_formats),
  }
  exported_timezone = (
    acc.timezone or os.environ.get("TZ") or "UTC"
  ).strip() or "UTC"
  server_info: dict[str, Any] = {
    "url": parsed_url.hostname or "localhost",
    "port": str(
      parsed_url.port
      or acc.server_port
      or (443 if parsed_url.scheme == "https" else 80)
    ),
    "https_port": "443",
    "server_protocol": parsed_url.scheme or acc.server_protocol or "http",
    "timezone": exported_timezone,
    "timestamp_now": now_epoch,
    "time_now": now_iso,
    "process": True,
    "xmltv_url": upstream_xmltv_url,
  }
  lineup_info: dict[str, Any] = {
    "lineup_id": lineup.lineup_id,
    "provider": lineup.provider,
    "market": lineup.market,
    "postal_prefix": lineup.postal_prefix,
    "collapse_sd_dupes": config.lineup.collapse_sd_dupes,
    "numbering_mode": (
      "hd" if config.lineup.collapse_sd_dupes else config.lineup.numbering_mode
    ),
    "emitted_channels": len(streams),
    "emitted_categories": len(categories),
  }

  return XtreamExportBundle(
    user_info=user_info,
    server_info=server_info,
    lineup_info=lineup_info,
    categories=categories,
    streams=streams,
    epg_xmltv_url=upstream_xmltv_url,
    epg_verification=epg_verification,
    channel_guides=channel_guides,
  )


def build_xtream_export_payloads(
  lineup: CableLineup,
  mappings: list[MappedChannel],
  config: AppConfig,
  *,
  account: XtreamAccountInfo | None = None,
  epg_verification: EpgVerificationResult | None = None,
) -> tuple[
  dict[str, Any],
  list[ExportedCategoryDict],
  list[dict[str, Any]],
]:
  """Build `(bundle_dict, live_categories, live_streams)` with passthrough EPG
  metadata.
  """
  bundle = build_xtream_export_bundle(
    lineup,
    mappings,
    config,
    account=account,
    epg_verification=epg_verification,
  )
  return bundle.to_export_tuple()


async def export_xtream_playlist_and_guide(
  lineup: CableLineup,
  mappings: list[MappedChannel],
  config: AppConfig,
  *,
  account: XtreamAccountInfo | None = None,
  output_dir: Path | None = None,
  verify_epg: bool | None = None,
  client: aiohttp.ClientSession | None = None,
) -> XtreamExportResult:
  """Phase (6): Verify upstream XMLTV passthrough and export Xtream API JSON
  playlists.
  """
  dest_dir = config.resolve_path(output_dir or config.output.xtream_dir)
  dest_dir.mkdir(parents=True, exist_ok=True)

  should_verify = config.epg.verify if verify_epg is None else verify_epg
  if should_verify:
    epg_ver = await verify_epg_endpoint(config, client=client)
  else:
    epg_ver = EpgVerificationResult(
      url=build_passthrough_xmltv_url(config, redact_secret=False),
      url_redacted=build_passthrough_xmltv_url(config, redact_secret=True),
      verified=False,
      skipped=True,
    )

  bundle_dict, live_categories, live_streams = build_xtream_export_payloads(
    lineup,
    mappings,
    config,
    account=account,
    epg_verification=epg_ver,
  )

  bundle_path = dest_dir / "bellhop_xtream_playlist.json"
  streams_path = dest_dir / "get_live_streams.json"
  cats_path = dest_dir / "get_live_categories.json"

  write_private_text(
    bundle_path,
    json.dumps(bundle_dict, indent=2, ensure_ascii=False) + "\n",
  )
  write_private_text(
    streams_path,
    json.dumps(live_streams, indent=2, ensure_ascii=False) + "\n",
  )
  write_private_text(
    cats_path,
    json.dumps(live_categories, indent=2, ensure_ascii=False) + "\n",
  )

  official_logo_count = sum(
    1 for m in mappings if m.selected is not None and bool(m.channel.logo)
  )
  guide_id_count = sum(
    1 for item in live_streams if bool(item.get("epg_channel_id"))
  )

  return XtreamExportResult(
    bundle_path=bundle_path,
    live_streams_path=streams_path,
    live_categories_path=cats_path,
    emitted_streams=len(live_streams),
    emitted_categories=len(live_categories),
    channels_with_official_logo=official_logo_count,
    channels_with_guide_id=guide_id_count,
    xmltv_url=build_passthrough_xmltv_url(config, redact_secret=False),
    xmltv_url_redacted=build_passthrough_xmltv_url(config, redact_secret=True),
    epg_verification=epg_ver,
  )
