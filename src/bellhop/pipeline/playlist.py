"""Phase (5): Extended M3U8 cable lineup playlist writer and structured
diagnostics report.

Diagnostics Report Schema (`output/lineup_report.json`)
-------------------------------------------------------
```json
{
  "generated_at": "2026-11-19T00:00:00+00:00",
  "lineup_id": "CAN-BELLFIBE-MTL-X",
  "provider": "Sample Fiber TV",
  "market": "Sample Market",
  "postal_prefix": "",
  "numbering_mode": "hd",
  "collapse_sd_dupes": true,
  "validation_skipped": false,
  "summary": {
    "total_catalog_streams": 420,
    "total_lineup_channels": 120,
    "total_dial_positions": 120,
    "candidates_matched": 115,
    "active_channels_emitted": 115,
    "active_dial_positions_emitted": 115,
    "active_with_native_epg": 90,
    "native_epg_percent": 78.3,
    "quality_distribution": { "4k": 4, "fhd": 25, "hd": 80, "unknown": 4,
    "sd": 2 },
    "failed_validation": 0,
    "unmatched": 5,
    "coverage_percent": 95.8
  },
  "groups": {
    "Sports": { "total": 28, "active": 27, "native_epg": 22,
    "failed_validation": 0, "unmatched": 1 }
  },
  "channels": [ /* ReportChannelRecord entries */ ]
}
```
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from bellhop.common.export_bundle import (
  assign_unique_slot_epg_ids,
  resolve_preferred_epg_base_id,
)
from bellhop.common.file_utils import write_private_text
from bellhop.config import AppConfig
from bellhop.epg.xmltv import build_passthrough_xmltv_url, build_serve_xmltv_url
from bellhop.pipeline.lineup_loader import CableLineup
from bellhop.pipeline.models import (
  MappedChannel,
  MatchCandidate,
  ProbeResultDict,
  XtreamStream,
)

ChannelReportStatus = Literal["active", "failed_validation", "unmatched"]

TRAILING_DIAL_RANGE_RE = re.compile(r"\s*\(\d+(?:[–-]\d+)?\)\s*$")


def strip_dial_range_suffix(group_title: str) -> str:
  """Remove any trailing `(lo–hi)` or `(chno)` dial-range suffix from
  `group_title`.
  """
  return TRAILING_DIAL_RANGE_RE.sub("", group_title).strip()


def format_group_with_dial_range(raw_group: str, chnos: Sequence[int]) -> str:
  """Format `raw_group` with the surviving `(min–max)` or `(chno)` dial
  range.
  """
  base = strip_dial_range_suffix(raw_group) or raw_group.strip() or "General"
  if not chnos:
    return raw_group.strip() or base
  lo = min(chnos)
  hi = max(chnos)
  return f"{base} ({lo}–{hi})" if lo != hi else f"{base} ({lo})"


def _escape_attr(value: str) -> str:
  return value.replace('"', "'").replace("\n", " ").replace("\r", " ").strip()


def _normalize_base_url(server_url: str) -> str:
  base = server_url.strip().rstrip("/")
  if base and not base.startswith(("http://", "https://")):
    return f"http://{base}"
  return base


def _resolve_stream_url(stream: XtreamStream, config: AppConfig) -> str:
  xt = config.xtream
  if xt.server_url and xt.username and xt.password:
    return stream.build_url(
      xt.server_url, xt.username, xt.password, xt.stream_format
    )
  if stream.direct_source:
    return stream.direct_source
  return stream.build_url(
    xt.server_url or "http://localhost",
    xt.username or "user",
    xt.password or "pass",
    xt.stream_format,
  )


def resolve_playback_url(mapped: MappedChannel, config: AppConfig) -> str:
  """Resolve the playable upstream stream URL for a selected mapped channel."""
  assert mapped.selected is not None
  xt = config.xtream
  stream = mapped.selected.stream
  if xt.server_url and xt.username and xt.password:
    return _resolve_stream_url(stream, config)
  if (
    mapped.probe is not None
    and mapped.probe.url
    and "***" not in mapped.probe.url
  ):
    return mapped.probe.url
  return _resolve_stream_url(stream, config)


def _expand_entries_by_numbering(
  mappings: list[MappedChannel],
  numbering_mode: str,
  *,
  collapse_sd_dupes: bool = False,
) -> list[tuple[int, str, MappedChannel]]:
  """Expand mapped channels into `(chno, display_name, mapped)` tuples sorted by
  channel number.
  """
  mode = "hd" if collapse_sd_dupes else numbering_mode.lower().strip()
  entries: list[tuple[int, str, MappedChannel]] = []

  for mapped in mappings:
    ch = mapped.channel
    if mode == "sd":
      target_no = ch.sd_chno if ch.sd_chno is not None else ch.chno
      entries.append((target_no, ch.name, mapped))
    elif mode == "both":
      if ch.sd_chno is not None and ch.sd_chno != ch.chno:
        entries.append((ch.sd_chno, ch.name, mapped))
      entries.append((ch.chno, ch.name, mapped))
    else:
      entries.append((ch.chno, ch.name, mapped))

  entries.sort(key=lambda item: item[0])
  return entries


def resolve_group_display_map(
  mappings: list[MappedChannel],
  numbering_mode: str,
  *,
  collapse_sd_dupes: bool = False,
) -> dict[str, str]:
  """Map each `channel.group` key to its surviving-channel `(lo–hi)` display
  title.
  """
  entries = _expand_entries_by_numbering(
    mappings,
    numbering_mode,
    collapse_sd_dupes=collapse_sd_dupes,
  )
  active_chnos_by_group: dict[str, list[int]] = {}
  all_chnos_by_group: dict[str, list[int]] = {}
  for chno, _name, mapped in entries:
    grp = mapped.channel.group
    all_chnos_by_group.setdefault(grp, []).append(chno)
    if mapped.selected is not None:
      active_chnos_by_group.setdefault(grp, []).append(chno)

  resolved: dict[str, str] = {}
  for grp, all_chnos in all_chnos_by_group.items():
    surviving = active_chnos_by_group.get(grp)
    resolved[grp] = format_group_with_dial_range(
      grp, surviving if surviving else all_chnos
    )
  return resolved


def render_m3u8(
  lineup: CableLineup,
  mappings: list[MappedChannel],
  config: AppConfig,
) -> str:
  """Render an Extended M3U8 playlist emulating the target cable provider
  channel lineup.
  """
  header_attrs: list[str] = []
  xmltv_url = build_serve_xmltv_url(config) or build_passthrough_xmltv_url(
    config, redact_secret=False
  )
  if xmltv_url:
    esc_xmltv = _escape_attr(xmltv_url)
    header_attrs.append(f'url-tvg="{esc_xmltv}"')
    header_attrs.append(f'x-tvg-url="{esc_xmltv}"')

  header_line = "#EXTM3U" + (
    " " + " ".join(header_attrs) if header_attrs else ""
  )
  lines: list[str] = [
    header_line,
    f"#PLAYLIST:{lineup.provider} ({lineup.market}) [{lineup.lineup_id}]",
  ]

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
  include_unmatched = config.output.include_unmatched_comment
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

  for chno, display_name, mapped in entries:
    ch = mapped.channel
    if mapped.selected is None:
      if include_unmatched:
        reason = (
          "failed stream validation"
          if mapped.candidates
          else "no matching stream"
        )
        lines.append(f"# UNMAPPED {chno} {display_name} ({reason})")
      continue

    stream = mapped.selected.stream
    url = resolve_playback_url(mapped, config)
    tvg_id = unique_epg_by_chno.get(
      chno,
      resolve_preferred_epg_base_id(
        chno=chno,
        gracenote_station_id=ch.gracenote_station_id,
        has_native_epg=mapped.selected.has_native_epg,
        upstream_epg_channel_id=stream.epg_channel_id,
        official_tvg_id=ch.tvg_id or "",
      ),
    )
    logo = ch.logo or stream.stream_icon or ""

    attrs = [
      f'tvg-chno="{chno}"',
      f'channel-id="{chno}"',
      f'tvg-id="{_escape_attr(tvg_id)}"',
      f'tvg-name="{_escape_attr(display_name)}"',
    ]
    if ch.gracenote_station_id:
      attrs.append(
        f'tvc-guide-stationid="{_escape_attr(ch.gracenote_station_id)}"'
      )
    if logo:
      attrs.append(f'tvg-logo="{_escape_attr(logo)}"')
    grp_label = group_labels.get(ch.group, ch.group)
    attrs.append(f'group-title="{_escape_attr(grp_label)}"')

    if ch.language:
      attrs.append(f'tvg-language="{_escape_attr(ch.language)}"')

    safe_title = display_name.replace("\n", " ").strip()
    lines.append(f"#EXTINF:-1 {' '.join(attrs)},{safe_title}")
    lines.append(url)

  return "\n".join(lines) + "\n"


def write_m3u8(
  lineup: CableLineup,
  mappings: list[MappedChannel],
  config: AppConfig,
  output_path: Path | None = None,
) -> Path:
  """Write the rendered M3U8 playlist to disk (`0o600`) and return its Path."""
  dest = config.resolve_path(output_path or config.output.playlist_path)
  content = render_m3u8(lineup, mappings, config)
  write_private_text(dest, content)
  return dest


@dataclass(slots=True)
class GroupCoverageStats:
  """Per-category coverage counters inside `lineup_report.json`."""

  total: int = 0
  active: int = 0
  native_epg: int = 0
  failed_validation: int = 0
  unmatched: int = 0

  def record(self, mapped: MappedChannel) -> ChannelReportStatus:
    self.total += 1
    if mapped.selected is not None:
      self.active += 1
      if mapped.selected.has_native_epg:
        self.native_epg += 1
      return "active"
    if mapped.candidates:
      self.failed_validation += 1
      return "failed_validation"
    self.unmatched += 1
    return "unmatched"

  def to_dict(self) -> dict[str, int]:
    return {
      "total": self.total,
      "active": self.active,
      "native_epg": self.native_epg,
      "failed_validation": self.failed_validation,
      "unmatched": self.unmatched,
    }


@dataclass(slots=True)
class ReportCandidateInfo:
  """Candidate stream summary serialized inside `lineup_report.json`."""

  stream_id: int
  name: str
  category_name: str
  epg_channel_id: str
  has_native_epg: bool
  quality_tier: str
  score: float
  reasons: list[str]

  @classmethod
  def from_candidate(cls, cand: MatchCandidate) -> ReportCandidateInfo:
    return cls(
      stream_id=cand.stream.stream_id,
      name=cand.stream.name,
      category_name=cand.stream.category_name,
      epg_channel_id=cand.stream.epg_channel_id,
      has_native_epg=cand.has_native_epg,
      quality_tier=cand.quality_tier,
      score=cand.score,
      reasons=list(cand.reasons),
    )

  def to_dict(self) -> dict[str, Any]:
    return {
      "stream_id": self.stream_id,
      "name": self.name,
      "category_name": self.category_name,
      "epg_channel_id": self.epg_channel_id,
      "has_native_epg": self.has_native_epg,
      "quality_tier": self.quality_tier,
      "score": self.score,
      "reasons": self.reasons,
    }


@dataclass(slots=True)
class ReportChannelRecord:
  """Per-slot diagnostics record inside `lineup_report.json`."""

  chno: int
  sd_chno: int | None
  name: str
  callsign: str | None
  tvg_id: str | None
  gracenote_station_id: str | None
  group: str
  language: str
  status: ChannelReportStatus
  probes_attempted: int
  selected_stream: ReportCandidateInfo | None
  probe: ProbeResultDict | None
  top_candidates: list[ReportCandidateInfo]

  def to_dict(self) -> dict[str, Any]:
    return {
      "chno": self.chno,
      "sd_chno": self.sd_chno,
      "name": self.name,
      "callsign": self.callsign,
      "tvg_id": self.tvg_id,
      "gracenote_station_id": self.gracenote_station_id,
      "group": self.group,
      "language": self.language,
      "status": self.status,
      "probes_attempted": self.probes_attempted,
      "selected_stream": self.selected_stream.to_dict()
      if self.selected_stream
      else None,
      "probe": self.probe,
      "top_candidates": [c.to_dict() for c in self.top_candidates],
    }


@dataclass(slots=True)
class ReportSummaryStats:
  """Top-level aggregate coverage statistics inside `lineup_report.json`."""

  total_catalog_streams: int
  total_lineup_channels: int
  total_dial_positions: int
  candidates_matched: int
  active_channels_emitted: int
  active_dial_positions_emitted: int
  active_with_native_epg: int
  native_epg_percent: float
  quality_distribution: dict[str, int]
  failed_validation: int
  unmatched: int
  coverage_percent: float

  def to_dict(self) -> dict[str, Any]:
    return {
      "total_catalog_streams": self.total_catalog_streams,
      "total_lineup_channels": self.total_lineup_channels,
      "total_dial_positions": self.total_dial_positions,
      "candidates_matched": self.candidates_matched,
      "active_channels_emitted": self.active_channels_emitted,
      "active_dial_positions_emitted": self.active_dial_positions_emitted,
      "active_with_native_epg": self.active_with_native_epg,
      "native_epg_percent": self.native_epg_percent,
      "quality_distribution": self.quality_distribution,
      "failed_validation": self.failed_validation,
      "unmatched": self.unmatched,
      "coverage_percent": self.coverage_percent,
    }


@dataclass(slots=True)
class LineupDiagnosticsReport:
  """Complete structured representation of `output/lineup_report.json`."""

  generated_at: str
  lineup_id: str
  provider: str
  market: str
  postal_prefix: str
  numbering_mode: str
  collapse_sd_dupes: bool
  validation_skipped: bool
  summary: ReportSummaryStats
  groups: dict[str, GroupCoverageStats] = field(default_factory=dict)
  channels: list[ReportChannelRecord] = field(default_factory=list)
  mapping_update_summary: dict[str, Any] | None = None

  def to_dict(self) -> dict[str, Any]:
    out: dict[str, Any] = {
      "generated_at": self.generated_at,
      "lineup_id": self.lineup_id,
      "provider": self.provider,
      "market": self.market,
      "postal_prefix": self.postal_prefix,
      "numbering_mode": self.numbering_mode,
      "collapse_sd_dupes": self.collapse_sd_dupes,
      "validation_skipped": self.validation_skipped,
      "summary": self.summary.to_dict(),
      "groups": {k: v.to_dict() for k, v in self.groups.items()},
      "channels": [ch.to_dict() for ch in self.channels],
    }
    if self.mapping_update_summary is not None:
      out["mapping_update_summary"] = self.mapping_update_summary
    return out


def build_diagnostics_report(
  lineup: CableLineup,
  mappings: list[MappedChannel],
  config: AppConfig,
  *,
  total_catalog_streams: int = 0,
  validation_skipped: bool = False,
  mapping_summary: dict[str, Any] | None = None,
) -> LineupDiagnosticsReport:
  """Build a strongly-typed `LineupDiagnosticsReport` from mapped channels."""
  total_slots = len(mappings)
  matched_slots = sum(1 for m in mappings if m.candidates)
  active_slots = sum(1 for m in mappings if m.selected is not None)
  native_epg_slots = sum(
    1 for m in mappings if m.selected is not None and m.selected.has_native_epg
  )
  failed_validation_slots = sum(
    1 for m in mappings if m.candidates and m.selected is None
  )
  unmatched_slots = total_slots - matched_slots

  quality_counts: dict[str, int] = {}
  groups: dict[str, GroupCoverageStats] = {}
  channels_report: list[ReportChannelRecord] = []
  group_labels = resolve_group_display_map(
    mappings,
    config.lineup.numbering_mode,
    collapse_sd_dupes=config.lineup.collapse_sd_dupes,
  )

  for m in mappings:
    ch = m.channel
    grp_label = group_labels.get(ch.group, ch.group)
    g_stats = groups.setdefault(grp_label, GroupCoverageStats())
    status = g_stats.record(m)

    if m.selected is not None:
      qt = m.selected.quality_tier or "unknown"
      quality_counts[qt] = quality_counts.get(qt, 0) + 1

    probe_dict = (
      m.probe.to_dict(redact_secret=config.xtream.password)
      if m.probe is not None
      else None
    )
    channels_report.append(
      ReportChannelRecord(
        chno=ch.chno,
        sd_chno=ch.sd_chno,
        name=ch.name,
        callsign=ch.callsign,
        tvg_id=ch.tvg_id,
        gracenote_station_id=ch.gracenote_station_id,
        group=grp_label,
        language=ch.language,
        status=status,
        probes_attempted=m.probes_attempted,
        selected_stream=(
          ReportCandidateInfo.from_candidate(m.selected)
          if m.selected is not None
          else None
        ),
        probe=probe_dict,
        top_candidates=[
          ReportCandidateInfo.from_candidate(c) for c in m.candidates[:5]
        ],
      )
    )

  coverage_pct = (
    round((active_slots / total_slots) * 100.0, 1) if total_slots else 0.0
  )
  epg_pct = (
    round((native_epg_slots / active_slots) * 100.0, 1) if active_slots else 0.0
  )

  dial_entries = _expand_entries_by_numbering(
    [m for m in mappings if m.selected is not None],
    config.lineup.numbering_mode,
    collapse_sd_dupes=config.lineup.collapse_sd_dupes,
  )
  total_dial_slots = len(
    _expand_entries_by_numbering(
      mappings,
      config.lineup.numbering_mode,
      collapse_sd_dupes=config.lineup.collapse_sd_dupes,
    )
  )

  return LineupDiagnosticsReport(
    generated_at=datetime.now(timezone.utc).isoformat(),
    lineup_id=lineup.lineup_id,
    provider=lineup.provider,
    market=lineup.market,
    postal_prefix=lineup.postal_prefix,
    numbering_mode="hd"
    if config.lineup.collapse_sd_dupes
    else config.lineup.numbering_mode,
    collapse_sd_dupes=config.lineup.collapse_sd_dupes,
    validation_skipped=validation_skipped,
    summary=ReportSummaryStats(
      total_catalog_streams=total_catalog_streams,
      total_lineup_channels=total_slots,
      total_dial_positions=total_dial_slots,
      candidates_matched=matched_slots,
      active_channels_emitted=active_slots,
      active_dial_positions_emitted=len(dial_entries),
      active_with_native_epg=native_epg_slots,
      native_epg_percent=epg_pct,
      quality_distribution=quality_counts,
      failed_validation=failed_validation_slots,
      unmatched=unmatched_slots,
      coverage_percent=coverage_pct,
    ),
    groups=groups,
    channels=channels_report,
    mapping_update_summary=mapping_summary,
  )


def build_report_dict(
  lineup: CableLineup,
  mappings: list[MappedChannel],
  config: AppConfig,
  *,
  total_catalog_streams: int = 0,
  validation_skipped: bool = False,
  mapping_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Build a JSON-serializable diagnostic report dictionary
  (`output/lineup_report.json`).
  """
  return build_diagnostics_report(
    lineup,
    mappings,
    config,
    total_catalog_streams=total_catalog_streams,
    validation_skipped=validation_skipped,
    mapping_summary=mapping_summary,
  ).to_dict()


def write_report(
  lineup: CableLineup,
  mappings: list[MappedChannel],
  config: AppConfig,
  *,
  output_path: Path | None = None,
  total_catalog_streams: int = 0,
  validation_skipped: bool = False,
  mapping_summary: dict[str, Any] | None = None,
) -> Path:
  """Write the JSON diagnostics report to disk and return its resolved Path."""
  dest = config.resolve_path(output_path or config.output.report_path)
  payload = build_report_dict(
    lineup,
    mappings,
    config,
    total_catalog_streams=total_catalog_streams,
    validation_skipped=validation_skipped,
    mapping_summary=mapping_summary,
  )
  write_private_text(dest, json.dumps(payload, indent=2, ensure_ascii=False))
  return dest
