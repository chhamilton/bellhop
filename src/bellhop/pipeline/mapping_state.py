"""Phase (3)–(5) Stateful Channel Mapping Store and Incremental Reconciliation.

Persistent Mapping Schema (`state/channel_mapping.json`, `schema_version = 1`)
------------------------------------------------------------------------------
```json
{
  "schema_version": 1,
  "lineup_id": "CAN-BELLFIBE-MTL-X",
  "provider": "Sample Fiber TV",
  "market": "Sample Market",
  "created_at": "2026-11-18T00:00:00+00:00",
  "updated_at": "2026-11-19T00:00:00+00:00",
  "last_summary": {
    "was_empty_initial_run": false,
    "total_lineup_slots": 120,
    "matched_slots": 115,
    "unmatched_slots": 5,
    "slots_with_native_epg": 90,
    "unique_candidate_streams": 240,
    "raw_catalog_streams": 420,
    "candidate_reduction_percent": 42.86
  },
  "slots": {
    "1108": {
      "chno": 1108,
      "sd_chno": 108,
      "name": "RDS",
      "group": "Sports",
      "language": "fr",
      "preferred_stream_id": 410108,
      "preferred_source": "validated",
      "change_status": "unchanged",
      "candidates": [
        {
          "stream_id": 410108,
          "name": "CA FR: RDS FHD",
          "direct_source": "http://provider/live/user/pass/410108.ts",
          "has_native_epg": true,
          "quality_tier": "fhd",
          "score": 136.5,
          "validation_status": "valid",
          "probe": { ... }
        }
      ]
    }
  }
}
```
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bellhop.common.file_utils import write_private_text
from bellhop.config import AppConfig
from bellhop.pipeline.lineup_loader import (
  DEFAULT_BELL_FIBE_MTL_LINEUP_ID,
  CableLineup,
)
from bellhop.pipeline.matcher import ChannelMatcher
from bellhop.pipeline.models import (
  LineupChannel,
  MappedChannel,
  MatchCandidate,
  ProbeResult,
  XtreamAccountInfo,
  XtreamStream,
)
from bellhop.pipeline.playlist import _resolve_stream_url
from bellhop.pipeline.validator import (
  ProgressCallback,
  StreamValidator,
  check_audio_language_compatibility,
  clone_probe_with_language_mismatch,
  normalize_audio_language_code,
  strip_slot_language_mismatch,
)
from bellhop.xtream.client import XtreamCatalog

VERIFIED_AUDIO_LANG_BONUS = 6.0
AUDIO_LANG_MISMATCH_PENALTY = 25.0

MAPPING_SCHEMA_VERSION = 1


def _is_audio_lang_mismatch(cand: CandidateRecord) -> bool:
  return bool(cand.probe is not None and cand.probe.audio_language_mismatch)


@dataclass(slots=True)
class CandidateRecord:
  """A ranked potential stream candidate stored in the persistent channel
  mapping.
  """

  stream_id: int
  name: str
  category_name: str = ""
  stream_icon: str = ""
  epg_channel_id: str = ""
  direct_source: str = ""
  has_native_epg: bool = False
  quality_tier: str = "unknown"
  score: float = 0.0
  reasons: list[str] = field(default_factory=list)
  validation_status: str = "unvalidated"  # "unvalidated", "valid", "invalid"
  probe: ProbeResult | None = None

  def to_xtream_stream(self) -> XtreamStream:
    return XtreamStream(
      stream_id=self.stream_id,
      name=self.name,
      category_name=self.category_name,
      stream_icon=self.stream_icon,
      epg_channel_id=self.epg_channel_id,
      direct_source=self.direct_source,
    )

  def to_match_candidate(self) -> MatchCandidate:
    return MatchCandidate(
      stream=self.to_xtream_stream(),
      score=self.score,
      reasons=list(self.reasons),
      quality_tier=self.quality_tier,
      has_native_epg=self.has_native_epg,
    )

  def to_dict(self, *, redact_secret: str = "") -> dict[str, Any]:
    probe_dict = (
      self.probe.to_dict(redact_secret=redact_secret)
      if self.probe is not None
      else None
    )
    return {
      "stream_id": self.stream_id,
      "name": self.name,
      "category_name": self.category_name,
      "stream_icon": self.stream_icon,
      "epg_channel_id": self.epg_channel_id,
      "direct_source": self.direct_source,
      "has_native_epg": self.has_native_epg,
      "quality_tier": self.quality_tier,
      "score": self.score,
      "reasons": self.reasons,
      "validation_status": self.validation_status,
      "probe": probe_dict,
    }

  @classmethod
  def from_dict(cls, data: dict[str, Any]) -> CandidateRecord:
    probe_raw = data.get("probe")
    return cls(
      stream_id=int(data["stream_id"]),
      name=str(data.get("name", "")),
      category_name=str(data.get("category_name", "")),
      stream_icon=str(data.get("stream_icon", "")),
      epg_channel_id=str(data.get("epg_channel_id", "")),
      direct_source=str(data.get("direct_source", "")),
      has_native_epg=bool(data.get("has_native_epg", False)),
      quality_tier=str(data.get("quality_tier", "unknown")),
      score=float(data.get("score", 0.0)),
      reasons=[str(r) for r in data.get("reasons", [])],
      validation_status=str(data.get("validation_status", "unvalidated")),
      probe=ProbeResult.from_dict(probe_raw)
      if isinstance(probe_raw, dict)
      else None,
    )


@dataclass(slots=True)
class ChannelMappingSlot:
  """Stateful mapping record for a single lineup slot."""

  chno: int
  name: str
  group: str
  language: str = "en"
  sd_chno: int | None = None
  callsign: str | None = None
  tvg_id: str | None = None
  gracenote_station_id: str | None = None
  pinned_stream_id: int | None = None
  preferred_stream_id: int | None = None
  # "pinned", "validated", "rough_match", or "none"
  preferred_source: str = "none"
  # "new", "unchanged", "upgraded", "upgraded_pending_validation",
  # "replaced_missing", "replaced_invalid", "candidates_updated", "unmatched"
  change_status: str = "new"
  change_detail: str = ""
  candidates: list[CandidateRecord] = field(default_factory=list)

  def selected_candidate(self) -> CandidateRecord | None:
    if self.preferred_stream_id is not None:
      for c in self.candidates:
        if c.stream_id == self.preferred_stream_id:
          return c
    # Fallback to first non-invalid candidate
    for c in self.candidates:
      if c.validation_status != "invalid":
        return c
    return None

  def to_dict(self, *, redact_secret: str = "") -> dict[str, Any]:
    return {
      "chno": self.chno,
      "sd_chno": self.sd_chno,
      "name": self.name,
      "group": self.group,
      "language": self.language,
      "callsign": self.callsign,
      "tvg_id": self.tvg_id,
      "gracenote_station_id": self.gracenote_station_id,
      "pinned_stream_id": self.pinned_stream_id,
      "preferred_stream_id": self.preferred_stream_id,
      "preferred_source": self.preferred_source,
      "change_status": self.change_status,
      "change_detail": self.change_detail,
      "candidates": [
        c.to_dict(redact_secret=redact_secret) for c in self.candidates
      ],
    }

  @classmethod
  def from_dict(cls, data: dict[str, Any]) -> ChannelMappingSlot:
    return cls(
      chno=int(data["chno"]),
      sd_chno=int(data["sd_chno"]) if data.get("sd_chno") is not None else None,
      name=str(data.get("name", "")),
      group=str(data.get("group", "General")),
      language=str(data.get("language", "en")),
      callsign=data.get("callsign"),
      tvg_id=data.get("tvg_id"),
      gracenote_station_id=data.get("gracenote_station_id"),
      pinned_stream_id=(
        int(data["pinned_stream_id"])
        if data.get("pinned_stream_id") is not None
        else None
      ),
      preferred_stream_id=(
        int(data["preferred_stream_id"])
        if data.get("preferred_stream_id") is not None
        else None
      ),
      preferred_source=str(data.get("preferred_source", "none")),
      change_status=str(data.get("change_status", "unchanged")),
      change_detail=str(data.get("change_detail", "")),
      candidates=[
        CandidateRecord.from_dict(c) for c in data.get("candidates", [])
      ],
    )


@dataclass(slots=True)
class MappingUpdateSummary:
  """Summary of incremental changes produced by Phase (3) rough matching."""

  was_empty_initial_run: bool = False
  total_lineup_slots: int = 0
  matched_slots: int = 0
  unmatched_slots: int = 0
  slots_with_native_epg: int = 0
  new_slots: int = 0
  unchanged_slots: int = 0
  upgraded_slots: int = 0
  replaced_missing_slots: int = 0
  replaced_invalid_slots: int = 0
  candidates_updated_slots: int = 0
  removed_lineup_chnos: list[int] = field(default_factory=list)
  unique_candidate_streams: int = 0
  raw_catalog_streams: int = 0

  def to_dict(self) -> dict[str, Any]:
    reduction_pct = (
      round(
        (1.0 - self.unique_candidate_streams / self.raw_catalog_streams)
        * 100.0,
        2,
      )
      if self.raw_catalog_streams > 0
      else 0.0
    )
    return {
      "was_empty_initial_run": self.was_empty_initial_run,
      "total_lineup_slots": self.total_lineup_slots,
      "matched_slots": self.matched_slots,
      "unmatched_slots": self.unmatched_slots,
      "slots_with_native_epg": self.slots_with_native_epg,
      "new_slots": self.new_slots,
      "unchanged_slots": self.unchanged_slots,
      "upgraded_slots": self.upgraded_slots,
      "replaced_missing_slots": self.replaced_missing_slots,
      "replaced_invalid_slots": self.replaced_invalid_slots,
      "candidates_updated_slots": self.candidates_updated_slots,
      "removed_lineup_chnos": self.removed_lineup_chnos,
      "unique_candidate_streams": self.unique_candidate_streams,
      "raw_catalog_streams": self.raw_catalog_streams,
      "candidate_reduction_percent": reduction_pct,
    }


@dataclass(slots=True)
class MappingDocument:
  """Persistent channel mapping file updated across monthly lineup and daily
  catalog runs.
  """

  lineup_id: str = DEFAULT_BELL_FIBE_MTL_LINEUP_ID
  provider: str = ""
  market: str = ""
  created_at: str = ""
  updated_at: str = ""
  slots: dict[int, ChannelMappingSlot] = field(default_factory=dict)
  last_summary: MappingUpdateSummary = field(
    default_factory=MappingUpdateSummary
  )

  @classmethod
  def empty(
    cls, lineup_id: str = DEFAULT_BELL_FIBE_MTL_LINEUP_ID
  ) -> MappingDocument:
    now = datetime.now(timezone.utc).isoformat()
    return cls(lineup_id=lineup_id, created_at=now, updated_at=now)

  def unique_candidate_stream_ids(self) -> set[int]:
    ids: set[int] = set()
    for slot in self.slots.values():
      for cand in slot.candidates:
        ids.add(cand.stream_id)
    return ids

  def to_dict(self, *, redact_secret: str = "") -> dict[str, Any]:
    ordered_slots = {
      str(chno): self.slots[chno].to_dict(redact_secret=redact_secret)
      for chno in sorted(self.slots)
    }
    return {
      "schema_version": MAPPING_SCHEMA_VERSION,
      "lineup_id": self.lineup_id,
      "provider": self.provider,
      "market": self.market,
      "created_at": self.created_at,
      "updated_at": self.updated_at,
      "last_summary": self.last_summary.to_dict(),
      "slots": ordered_slots,
    }

  @classmethod
  def from_dict(cls, data: dict[str, Any]) -> MappingDocument:
    slots_raw = data.get("slots") or {}
    slots: dict[int, ChannelMappingSlot] = {}
    for k, v in slots_raw.items():
      slot = ChannelMappingSlot.from_dict(v)
      slots[int(k)] = slot

    return cls(
      lineup_id=str(data.get("lineup_id") or DEFAULT_BELL_FIBE_MTL_LINEUP_ID),
      provider=str(data.get("provider", "")),
      market=str(data.get("market", "")),
      created_at=str(data.get("created_at", "")),
      updated_at=str(data.get("updated_at", "")),
      slots=slots,
    )


def load_mapping_document(
  path: Path, lineup_id: str = DEFAULT_BELL_FIBE_MTL_LINEUP_ID
) -> MappingDocument:
  """Load an existing channel mapping from disk, or return an empty document on
  first run.
  """
  if not path.is_file():
    return MappingDocument.empty(lineup_id=lineup_id)
  with path.open("r", encoding="utf-8") as fh:
    data = json.load(fh)
  if not data or not isinstance(data, dict):
    return MappingDocument.empty(lineup_id=lineup_id)
  return MappingDocument.from_dict(data)


def save_mapping_document(
  doc: MappingDocument,
  path: Path,
  *,
  redact_secret: str = "",
) -> Path:
  """Persist the channel mapping document atomically to disk (`0o600`)."""
  write_private_text(
    path,
    json.dumps(
      doc.to_dict(redact_secret=redact_secret), indent=2, ensure_ascii=False
    ),
  )
  return path


def reconcile_rough_mapping(
  existing: MappingDocument | None,
  lineup: CableLineup,
  catalog: XtreamCatalog,
  config: AppConfig,
) -> MappingDocument:
  """Phase (3): Reconcile an existing (or empty) mapping against current lineup
  & IPTV catalog.
  """
  now = datetime.now(timezone.utc).isoformat()
  is_initial = existing is None or len(existing.slots) == 0
  base_created = (
    existing.created_at if existing and existing.created_at else now
  )
  prev_slots = existing.slots if existing else {}

  # Collect intrinsic stream validation history across all previous slots
  prior_validation: dict[int, tuple[str, ProbeResult | None]] = {}
  for old_slot in prev_slots.values():
    for old_cand in old_slot.candidates:
      if old_cand.validation_status == "unvalidated":
        continue
      if old_cand.probe is not None:
        intrinsic_probe = strip_slot_language_mismatch(old_cand.probe)
        intrinsic_status = "valid" if intrinsic_probe.ok else "invalid"
        prior_validation[old_cand.stream_id] = (
          intrinsic_status,
          intrinsic_probe,
        )
      elif old_cand.stream_id not in prior_validation:
        prior_validation[old_cand.stream_id] = (
          old_cand.validation_status,
          None,
        )

  # Merge config-level pinned streams with any slot-level pins stored in
  # existing mapping
  effective_pins: dict[int, int] = dict(config.lineup.pinned_streams)
  for chno, old_slot in prev_slots.items():
    if old_slot.pinned_stream_id is not None and chno not in effective_pins:
      effective_pins[chno] = old_slot.pinned_stream_id

  matcher = ChannelMatcher(catalog.streams, pinned_streams=effective_pins)
  max_cands = max(5, config.validation.max_candidates_per_channel)
  hysteresis = config.state.upgrade_hysteresis_score
  catalog_ids = {s.stream_id for s in catalog.streams}

  new_slots: dict[int, ChannelMappingSlot] = {}
  summary = MappingUpdateSummary(
    was_empty_initial_run=is_initial,
    total_lineup_slots=len(lineup.channels),
    raw_catalog_streams=len(catalog.streams),
  )

  lineup_chnos = {ch.chno for ch in lineup.channels}
  summary.removed_lineup_chnos = sorted(set(prev_slots) - lineup_chnos)

  # Build per-slot set of known-dead or audio-language-mismatched stream IDs so
  # Pass 1
  # does not hoard streams that failed or had cross-wired audio on that slot
  globally_invalid_ids = {
    sid for sid, (st, _pr) in prior_validation.items() if st == "invalid"
  }
  valid_with_audio = [
    (sid, pr.audio_languages)
    for sid, (st, pr) in prior_validation.items()
    if st == "valid" and pr is not None and pr.audio_languages
  ]
  audio_bad_by_lang: dict[str, set[int]] = {}
  invalid_by_chno: dict[int, set[int]] = {}
  for ch in lineup.channels:
    allow_ao = "audio" in ch.group.lower() or "music" in ch.group.lower()
    if (
      config.validation.check_audio_language
      and not allow_ao
      and valid_with_audio
    ):
      lang_key = ch.language.strip().lower()
      slot_bad = audio_bad_by_lang.get(lang_key)
      if slot_bad is None:
        slot_bad = set(globally_invalid_ids)
        for sid, audio_langs in valid_with_audio:
          compat, _ = check_audio_language_compatibility(
            ch.language, audio_langs, allow_audio_only=False
          )
          if not compat:
            slot_bad.add(sid)
        audio_bad_by_lang[lang_key] = slot_bad
      invalid_by_chno[ch.chno] = set(slot_bad)
    else:
      invalid_by_chno[ch.chno] = set(globally_invalid_ids)

  multipass_by_chno = {
    mc_row.channel.chno: mc_row.candidates
    for mc_row in matcher.match_lineup(
      lineup.channels,
      max_candidates=max_cands,
      invalid_stream_ids_by_chno=invalid_by_chno,
    )
  }

  for ch in lineup.channels:
    pinned_id = effective_pins.get(ch.chno)
    if pinned_id is None and ch.sd_chno is not None:
      pinned_id = effective_pins.get(ch.sd_chno)
    allow_audio_only = (
      "audio" in ch.group.lower() or "music" in ch.group.lower()
    )

    matched = multipass_by_chno.get(ch.chno, [])
    cand_records: list[CandidateRecord] = []
    for mc in matched:
      val_status, prev_probe = prior_validation.get(
        mc.stream.stream_id, ("unvalidated", None)
      )
      resolved_url = _resolve_stream_url(mc.stream, config)
      slot_probe: ProbeResult | None = None
      if prev_probe is not None:
        clean_url = (
          prev_probe.url.replace("***", config.xtream.password)
          if (
            prev_probe.url
            and "***" in prev_probe.url
            and config.xtream.password
          )
          else prev_probe.url
        )
        slot_probe = replace(
          prev_probe,
          url=clean_url,
          audio_languages=list(prev_probe.audio_languages),
        )

      adj_score = mc.score
      adj_reasons = list(mc.reasons)

      if (
        slot_probe is not None
        and slot_probe.ok
        and config.validation.check_audio_language
        and mc.stream.stream_id != pinned_id
      ):
        compat, verified = check_audio_language_compatibility(
          ch.language,
          slot_probe.audio_languages,
          allow_audio_only=allow_audio_only,
        )
        if not compat:
          val_status = "valid"
          slot_probe = clone_probe_with_language_mismatch(
            slot_probe, ch.language
          )
          adj_score = round(adj_score - AUDIO_LANG_MISMATCH_PENALTY, 2)
          adj_reasons.append(
            f"audio_lang_mismatch:{','.join(slot_probe.audio_languages)}!={ch.language}"
          )
        elif verified:
          norm_expected = (
            normalize_audio_language_code(ch.language) or ch.language
          )
          adj_score = round(adj_score + VERIFIED_AUDIO_LANG_BONUS, 2)
          adj_reasons.append(f"audio_lang_verified:{norm_expected}")

      cand_records.append(
        CandidateRecord(
          stream_id=mc.stream.stream_id,
          name=mc.stream.name,
          category_name=mc.stream.category_name,
          stream_icon=mc.stream.stream_icon,
          epg_channel_id=mc.stream.epg_channel_id,
          direct_source=resolved_url,
          has_native_epg=mc.has_native_epg,
          quality_tier=mc.quality_tier,
          score=adj_score,
          reasons=adj_reasons,
          validation_status=val_status,
          probe=slot_probe,
        )
      )

    # Rank compatible/unvalidated ahead of demoted language-mismatch fallbacks,
    # and invalid last
    cand_records.sort(
      key=lambda c: (
        c.validation_status == "invalid",
        _is_audio_lang_mismatch(c),
        -c.score,
        not c.has_native_epg,
        c.stream_id,
      )
    )

    prior_slot = prev_slots.get(ch.chno)
    slot = _reconcile_single_slot(
      ch=ch,
      candidates=cand_records,
      old_slot=prior_slot,
      pinned_id=pinned_id if (pinned_id in catalog_ids) else None,
      catalog_ids=catalog_ids,
      hysteresis=hysteresis,
    )
    new_slots[ch.chno] = slot

    # Update summary counters
    sel = slot.selected_candidate()
    if sel is not None:
      summary.matched_slots += 1
      if sel.has_native_epg:
        summary.slots_with_native_epg += 1
    else:
      summary.unmatched_slots += 1

    if slot.change_status == "new":
      summary.new_slots += 1
    elif slot.change_status == "unchanged":
      summary.unchanged_slots += 1
    elif slot.change_status in {"upgraded", "upgraded_pending_validation"}:
      summary.upgraded_slots += 1
    elif slot.change_status == "replaced_missing":
      summary.replaced_missing_slots += 1
    elif slot.change_status == "replaced_invalid":
      summary.replaced_invalid_slots += 1
    elif slot.change_status == "candidates_updated":
      summary.candidates_updated_slots += 1

  doc = MappingDocument(
    lineup_id=lineup.lineup_id,
    provider=lineup.provider,
    market=lineup.market,
    created_at=base_created,
    updated_at=now,
    slots=new_slots,
    last_summary=summary,
  )
  summary.unique_candidate_streams = len(doc.unique_candidate_stream_ids())
  return doc


def _reconcile_single_slot(
  *,
  ch: LineupChannel,
  candidates: list[CandidateRecord],
  old_slot: ChannelMappingSlot | None,
  pinned_id: int | None,
  catalog_ids: set[int],
  hysteresis: float,
) -> ChannelMappingSlot:
  viable = [c for c in candidates if c.validation_status != "invalid"]
  top = viable[0] if viable else None

  if not candidates or top is None:
    return ChannelMappingSlot(
      chno=ch.chno,
      sd_chno=ch.sd_chno,
      name=ch.name,
      group=ch.group,
      language=ch.language,
      callsign=ch.callsign,
      tvg_id=ch.tvg_id,
      gracenote_station_id=ch.gracenote_station_id,
      pinned_stream_id=pinned_id,
      preferred_stream_id=None,
      preferred_source="none",
      change_status="unmatched",
      change_detail="No viable candidate stream matched",
      candidates=candidates,
    )

  if pinned_id is not None:
    return ChannelMappingSlot(
      chno=ch.chno,
      sd_chno=ch.sd_chno,
      name=ch.name,
      group=ch.group,
      language=ch.language,
      callsign=ch.callsign,
      tvg_id=ch.tvg_id,
      gracenote_station_id=ch.gracenote_station_id,
      pinned_stream_id=pinned_id,
      preferred_stream_id=pinned_id,
      preferred_source="pinned",
      change_status="new" if old_slot is None else "unchanged",
      change_detail=f"Pinned to stream #{pinned_id}",
      candidates=candidates,
    )

  # First time seeing this lineup channel slot (or previously unmatched)
  if old_slot is None or old_slot.preferred_stream_id is None:
    source = "validated" if top.validation_status == "valid" else "rough_match"
    return ChannelMappingSlot(
      chno=ch.chno,
      sd_chno=ch.sd_chno,
      name=ch.name,
      group=ch.group,
      language=ch.language,
      callsign=ch.callsign,
      tvg_id=ch.tvg_id,
      gracenote_station_id=ch.gracenote_station_id,
      preferred_stream_id=top.stream_id,
      preferred_source=source,
      change_status="new",
      change_detail=f"Initial match -> {top.name} (#{top.stream_id}, score={top.score:.1f})",
      candidates=candidates,
    )

  prev_id = old_slot.preferred_stream_id
  if prev_id not in catalog_ids:
    source = "validated" if top.validation_status == "valid" else "rough_match"
    return ChannelMappingSlot(
      chno=ch.chno,
      sd_chno=ch.sd_chno,
      name=ch.name,
      group=ch.group,
      language=ch.language,
      callsign=ch.callsign,
      tvg_id=ch.tvg_id,
      gracenote_station_id=ch.gracenote_station_id,
      preferred_stream_id=top.stream_id,
      preferred_source=source,
      change_status="replaced_missing",
      change_detail=f"Previous stream #{prev_id} disappeared from catalog; promoted #{top.stream_id}",
      candidates=candidates,
    )

  prev_in_cands = next((c for c in candidates if c.stream_id == prev_id), None)
  if prev_in_cands is None:
    # Renamed away from this channel in today's catalog
    source = "validated" if top.validation_status == "valid" else "rough_match"
    return ChannelMappingSlot(
      chno=ch.chno,
      sd_chno=ch.sd_chno,
      name=ch.name,
      group=ch.group,
      language=ch.language,
      callsign=ch.callsign,
      tvg_id=ch.tvg_id,
      gracenote_station_id=ch.gracenote_station_id,
      preferred_stream_id=top.stream_id,
      preferred_source=source,
      change_status="replaced_missing",
      change_detail=f"Previous stream #{prev_id} no longer matches channel; promoted #{top.stream_id}",
      candidates=candidates,
    )

  if prev_in_cands.validation_status == "invalid":
    source = "validated" if top.validation_status == "valid" else "rough_match"
    return ChannelMappingSlot(
      chno=ch.chno,
      sd_chno=ch.sd_chno,
      name=ch.name,
      group=ch.group,
      language=ch.language,
      callsign=ch.callsign,
      tvg_id=ch.tvg_id,
      gracenote_station_id=ch.gracenote_station_id,
      preferred_stream_id=top.stream_id,
      preferred_source=source,
      change_status="replaced_invalid",
      change_detail=f"Previous stream #{prev_id} marked invalid; promoted #{top.stream_id}",
      candidates=candidates,
    )

  old_cand_ids = [c.stream_id for c in old_slot.candidates]
  new_cand_ids = [c.stream_id for c in candidates]

  if top.stream_id == prev_id:
    status = (
      "unchanged" if old_cand_ids == new_cand_ids else "candidates_updated"
    )
    source = (
      "validated"
      if prev_in_cands.validation_status == "valid"
      else old_slot.preferred_source
    )
    return ChannelMappingSlot(
      chno=ch.chno,
      sd_chno=ch.sd_chno,
      name=ch.name,
      group=ch.group,
      language=ch.language,
      callsign=ch.callsign,
      tvg_id=ch.tvg_id,
      gracenote_station_id=ch.gracenote_station_id,
      preferred_stream_id=prev_id,
      preferred_source=source if source != "none" else "rough_match",
      change_status=status,
      change_detail=f"Retained #{prev_id} ({prev_in_cands.name})",
      candidates=candidates,
    )

  # A new stream outscored (or outranked a language-mismatched fallback) the
  # previous stream!
  escaping_lang_mismatch = _is_audio_lang_mismatch(
    prev_in_cands
  ) and not _is_audio_lang_mismatch(top)
  score_gain = top.score - prev_in_cands.score
  if not escaping_lang_mismatch and score_gain < hysteresis:
    # Within hysteresis window: avoid needless daily stream churn
    status = (
      "unchanged" if old_cand_ids == new_cand_ids else "candidates_updated"
    )
    source = (
      "validated"
      if prev_in_cands.validation_status == "valid"
      else old_slot.preferred_source
    )
    return ChannelMappingSlot(
      chno=ch.chno,
      sd_chno=ch.sd_chno,
      name=ch.name,
      group=ch.group,
      language=ch.language,
      callsign=ch.callsign,
      tvg_id=ch.tvg_id,
      gracenote_station_id=ch.gracenote_station_id,
      preferred_stream_id=prev_id,
      preferred_source=source if source != "none" else "rough_match",
      change_status=status,
      change_detail=f"Kept #{prev_id} within hysteresis (+{score_gain:.1f} < {hysteresis:.1f})",
      candidates=candidates,
    )

  # Significant upgrade (e.g. higher resolution or native EPG added):
  # If previous stream was already live-validated and new top isn't yet
  # validated, keep
  # known-good stream active while queuing the upgrade at candidates[0] for
  # Phase (4)!
  if (
    prev_in_cands.validation_status == "valid"
    and top.validation_status != "valid"
  ):
    return ChannelMappingSlot(
      chno=ch.chno,
      sd_chno=ch.sd_chno,
      name=ch.name,
      group=ch.group,
      language=ch.language,
      callsign=ch.callsign,
      tvg_id=ch.tvg_id,
      gracenote_station_id=ch.gracenote_station_id,
      preferred_stream_id=prev_id,
      preferred_source="validated",
      change_status="upgraded_pending_validation",
      change_detail=(
        f"Queued higher-ranked #{top.stream_id} (+{score_gain:.1f} pts) for validation; "
        f"keeping verified #{prev_id} until validated"
      ),
      candidates=candidates,
    )

  source = "validated" if top.validation_status == "valid" else "rough_match"
  return ChannelMappingSlot(
    chno=ch.chno,
    sd_chno=ch.sd_chno,
    name=ch.name,
    group=ch.group,
    language=ch.language,
    callsign=ch.callsign,
    tvg_id=ch.tvg_id,
    gracenote_station_id=ch.gracenote_station_id,
    preferred_stream_id=top.stream_id,
    preferred_source=source,
    change_status="upgraded",
    change_detail=(
      f"Upgraded #{prev_id} ({prev_in_cands.score:.1f}) -> "
      f"#{top.stream_id} {top.name} ({top.score:.1f})"
    ),
    candidates=candidates,
  )


def _sync_slot_validation(
  slot: ChannelMappingSlot,
  mapped: MappedChannel,
  probe_cache: dict[int, ProbeResult],
  *,
  check_audio_language: bool = True,
) -> None:
  allow_audio_only = (
    "audio" in slot.group.lower() or "music" in slot.group.lower()
  )
  for cand in slot.candidates:
    if cand.stream_id in probe_cache:
      pr = probe_cache[cand.stream_id]
      if not pr.ok:
        cand.probe = pr
        cand.validation_status = "invalid"
      elif check_audio_language and cand.stream_id != slot.pinned_stream_id:
        compat, _ = check_audio_language_compatibility(
          slot.language,
          pr.audio_languages,
          allow_audio_only=allow_audio_only,
        )
        if not compat:
          cand.probe = clone_probe_with_language_mismatch(pr, slot.language)
          cand.validation_status = "valid"
        else:
          cand.probe = pr
          cand.validation_status = "valid"
      else:
        cand.probe = pr
        cand.validation_status = "valid"

  slot.candidates.sort(
    key=lambda c: (
      c.validation_status == "invalid",
      _is_audio_lang_mismatch(c),
      -c.score,
      not c.has_native_epg,
      c.stream_id,
    )
  )

  if mapped.selected is not None:
    slot.preferred_stream_id = mapped.selected.stream.stream_id
    slot.preferred_source = (
      "pinned"
      if slot.pinned_stream_id == slot.preferred_stream_id
      else "validated"
    )
    slot.change_status = "unchanged"
    if mapped.probe is not None and mapped.probe.audio_language_mismatch:
      slot.change_detail = (
        f"Validated fallback stream #{slot.preferred_stream_id} "
        f"({mapped.selected.stream.name}; audio language mismatch)"
      )
    else:
      slot.change_detail = f"Validated working stream #{slot.preferred_stream_id} ({mapped.selected.stream.name})"
  else:
    slot.preferred_stream_id = None
    slot.preferred_source = "none"
    slot.change_status = "unmatched"
    slot.change_detail = "All candidate streams failed live validation"


async def validate_mapping_candidates(
  doc: MappingDocument,
  lineup: CableLineup,
  config: AppConfig,
  account: XtreamAccountInfo | None = None,
  *,
  only_changed: bool = False,
  on_channel_done: ProgressCallback | None = None,
  validator: StreamValidator | None = None,
  checkpoint_path: Path | None = None,
  redact_secret: str = "",
  checkpoint_interval: int = 20,
) -> int:
  """Phase (4): Validate ONLY candidate streams in the mapping document (never
  raw catalog).
  """
  active_validator = validator or StreamValidator(config, account)
  lineup_by_no = {ch.chno: ch for ch in lineup.channels}

  mappings_to_probe: list[MappedChannel] = []
  slot_by_chno: dict[int, ChannelMappingSlot] = {}

  for chno in sorted(doc.slots):
    slot = doc.slots[chno]
    if not slot.candidates:
      continue
    if (
      only_changed
      and slot.preferred_source in {"validated", "pinned"}
      and slot.change_status in {"unchanged", "candidates_updated"}
    ):
      continue

    ch = lineup_by_no.get(chno) or LineupChannel(
      chno=slot.chno,
      sd_chno=slot.sd_chno,
      name=slot.name,
      group=slot.group,
      language=slot.language,
      callsign=slot.callsign,
      tvg_id=slot.tvg_id,
    )
    mappings_to_probe.append(
      MappedChannel(
        channel=ch,
        candidates=[c.to_match_candidate() for c in slot.candidates],
      )
    )
    slot_by_chno[chno] = slot

  if not mappings_to_probe:
    return 0

  probe_cache = active_validator._probe_cache

  def _wrapped_done(
    mapped: MappedChannel, done_count: int, total_count: int
  ) -> None:
    sl = slot_by_chno.get(mapped.channel.chno)
    if sl is not None:
      _sync_slot_validation(
        sl,
        mapped,
        probe_cache,
        check_audio_language=active_validator.val_cfg.check_audio_language,
      )
    if (
      checkpoint_path is not None
      and checkpoint_interval > 0
      and done_count % checkpoint_interval == 0
    ):
      doc.updated_at = datetime.now(timezone.utc).isoformat()
      save_mapping_document(doc, checkpoint_path, redact_secret=redact_secret)
    if on_channel_done is not None:
      on_channel_done(mapped, done_count, total_count)

  await active_validator.validate_mappings(
    mappings_to_probe, on_channel_done=_wrapped_done
  )

  # Final sync pass across all probed slots
  for mapped in mappings_to_probe:
    sl = slot_by_chno.get(mapped.channel.chno)
    if sl is not None:
      _sync_slot_validation(
        sl,
        mapped,
        probe_cache,
        check_audio_language=active_validator.val_cfg.check_audio_language,
      )

  doc.updated_at = datetime.now(timezone.utc).isoformat()
  if checkpoint_path is not None:
    save_mapping_document(doc, checkpoint_path, redact_secret=redact_secret)
  return len(probe_cache)


def mapping_document_to_mapped_channels(
  doc: MappingDocument,
  lineup: CableLineup,
) -> list[MappedChannel]:
  """Phase (5) bridge: convert MappingDocument slots into MappedChannel entries
  for M3U8 output.
  """
  out: list[MappedChannel] = []
  for ch in lineup.channels:
    slot = doc.slots.get(ch.chno)
    if slot is None:
      out.append(MappedChannel(channel=ch))
      continue

    sel_rec = slot.selected_candidate()
    selected_mc = sel_rec.to_match_candidate() if sel_rec is not None else None
    selected_probe = sel_rec.probe if sel_rec is not None else None
    probes_attempted = sum(1 for c in slot.candidates if c.probe is not None)

    out.append(
      MappedChannel(
        channel=ch,
        selected=selected_mc,
        probe=selected_probe,
        candidates=[c.to_match_candidate() for c in slot.candidates],
        probes_attempted=probes_attempted,
      )
    )
  return out
