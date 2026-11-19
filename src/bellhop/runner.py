"""Reusable 6-phase pipeline runner engine for CLI execution and persistent scheduling."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bellhop.catalog_fetcher import CatalogFetchResult, fetch_daily_iptv_catalog
from bellhop.config import AppConfig
from bellhop.lineup_loader import CableLineup
from bellhop.lineup_retriever import (
  LineupRetrievalResult,
  retrieve_cable_lineup,
)
from bellhop.mapping_state import (
  MappingDocument,
  load_mapping_document,
  mapping_document_to_mapped_channels,
  reconcile_rough_mapping,
  save_mapping_document,
  validate_mapping_candidates,
)
from bellhop.models import MappedChannel
from bellhop.playlist import write_m3u8, write_report
from bellhop.validator import ProgressCallback, StreamValidator
from bellhop.xtream_exporter import (
  XtreamExportResult,
  export_xtream_playlist_and_guide,
)

ALL_PHASES: tuple[str, ...] = (
  "lineup",
  "catalog",
  "match",
  "validate",
  "m3u8",
  "xtream",
)

PHASE_ALIASES: dict[str, tuple[str, ...]] = {
  "1": ("lineup",),
  "lineup": ("lineup",),
  "retrieve-lineup": ("lineup",),
  "retrieve_lineup": ("lineup",),
  "2": ("catalog",),
  "catalog": ("catalog",),
  "fetch": ("catalog",),
  "fetch-catalog": ("catalog",),
  "fetch_catalog": ("catalog",),
  "3": ("match",),
  "match": ("match",),
  "rough-match": ("match",),
  "rough_match": ("match",),
  "4": ("validate",),
  "validate": ("validate",),
  "validation": ("validate",),
  "validate-candidates": ("validate",),
  "validate_candidates": ("validate",),
  "5": ("m3u8",),
  "m3u8": ("m3u8",),
  "playlist": ("m3u8",),
  "generate": ("m3u8",),
  "6": ("xtream",),
  "xtream": ("xtream",),
  "export-xtream": ("xtream",),
  "export_xtream": ("xtream",),
  "export": ("m3u8", "xtream"),
  "outputs": ("m3u8", "xtream"),
  "all": ALL_PHASES,
}

REFRESH_TARGETS = frozenset({"lineup", "catalog", "epg", "all"})


class PipelineConfigError(ValueError):
  """Raised when invalid phase names or run options are requested."""


def parse_phase_tokens(
  raw_values: list[str] | tuple[str, ...] | None,
) -> list[str]:
  """Expand comma-separated phase names/aliases into canonical ordered phase names."""
  if not raw_values:
    return []

  expanded: set[str] = set()
  for raw_item in raw_values:
    for token in raw_item.split(","):
      cleaned = token.strip().lower()
      if not cleaned:
        continue
      mapped = PHASE_ALIASES.get(cleaned)
      if mapped is None:
        valid = ", ".join((*ALL_PHASES, "export"))
        raise PipelineConfigError(
          f"Unknown pipeline phase {token!r}. Valid phases: {valid} (or 1..6)."
        )
      expanded.update(mapped)

  return [p for p in ALL_PHASES if p in expanded]


def parse_refresh_targets(
  raw_values: list[str] | tuple[str, ...] | None,
) -> set[str]:
  """Parse `--force-refresh` target tokens into {'lineup', 'catalog', 'epg'}."""
  if raw_values is None:
    return set()
  if len(raw_values) == 0:
    return {"lineup", "catalog", "epg"}

  targets: set[str] = set()
  for item in raw_values:
    for tok in item.split(","):
      cleaned = tok.strip().lower()
      if not cleaned or cleaned == "all":
        return {"lineup", "catalog", "epg"}
      if cleaned not in REFRESH_TARGETS:
        raise PipelineConfigError(
          f"Unknown refresh target {tok!r}. Expected: lineup, catalog, epg, or all."
        )
      targets.add(cleaned)
  return targets or {"lineup", "catalog", "epg"}


def resolve_active_phases(
  config: AppConfig,
  *,
  include_phases: list[str] | None = None,
  skip_phases: list[str] | None = None,
) -> tuple[list[str], list[str]]:
  """Return `(active_phases, skipped_phases)` in canonical pipeline execution order."""
  skipped_req = set(parse_phase_tokens(skip_phases))

  if include_phases:
    base = parse_phase_tokens(include_phases)
  else:
    base = ["lineup", "catalog", "match"]
    if config.validation.enabled:
      base.append("validate")
    if config.output.m3u8_enabled:
      base.append("m3u8")
    if config.output.xtream_enabled:
      base.append("xtream")

  active = [p for p in base if p not in skipped_req]
  skipped = [p for p in ALL_PHASES if p not in active]
  return active, skipped


@dataclass(slots=True)
class PipelineRunOptions:
  """Ephemeral execution controls for a single pipeline run."""

  phases: list[str] = field(default_factory=list)
  skip_phases: list[str] = field(default_factory=list)
  dry_run: bool = False
  force_refresh: set[str] = field(default_factory=set)
  reprobe_all: bool = False
  reset_mapping: bool = False


@dataclass(slots=True)
class PipelineRunResult:
  """Structured telemetry and artifacts produced by a pipeline run."""

  started_at: str
  finished_at: str
  duration_sec: float
  dry_run: bool
  executed_phases: list[str]
  skipped_phases: list[str]
  lineup_result: LineupRetrievalResult | None = None
  catalog_result: CatalogFetchResult | None = None
  lineup: CableLineup | None = None
  mapping_doc: MappingDocument | None = None
  mapping_path: Path | None = None
  mapping_saved: bool = False
  mappings: list[MappedChannel] = field(default_factory=list)
  validation_probed_streams: int = 0
  validation_would_probe_slots: int = 0
  playlist_path: Path | None = None
  report_path: Path | None = None
  xtream_result: XtreamExportResult | None = None

  def to_dict(self) -> dict[str, Any]:
    return {
      "started_at": self.started_at,
      "finished_at": self.finished_at,
      "duration_sec": round(self.duration_sec, 3),
      "dry_run": self.dry_run,
      "executed_phases": self.executed_phases,
      "skipped_phases": self.skipped_phases,
      "lineup_id": self.lineup.lineup_id if self.lineup else None,
      "lineup_backend": self.lineup_result.backend
      if self.lineup_result
      else None,
      "catalog_streams": (
        len(self.catalog_result.catalog.streams)
        if self.catalog_result
        else None
      ),
      "mapping_summary": (
        self.mapping_doc.last_summary.to_dict() if self.mapping_doc else None
      ),
      "mapping_saved": self.mapping_saved,
      "validation_probed_streams": self.validation_probed_streams,
      "validation_would_probe_slots": self.validation_would_probe_slots,
      "playlist_path": str(self.playlist_path) if self.playlist_path else None,
      "report_path": str(self.report_path) if self.report_path else None,
      "xtream_bundle_path": (
        str(self.xtream_result.bundle_path) if self.xtream_result else None
      ),
      "epg_passthrough_verified": (
        self.xtream_result.epg_verified if self.xtream_result else None
      ),
    }


class PipelineRunner:
  """Executes selected pipeline phases against an `AppConfig`."""

  def __init__(
    self,
    config: AppConfig,
    *,
    validator: StreamValidator | None = None,
  ) -> None:
    self.config = config
    self._custom_validator = validator

  async def run(
    self,
    options: PipelineRunOptions | None = None,
    *,
    on_validation_progress: ProgressCallback | None = None,
  ) -> PipelineRunResult:
    opts = options or PipelineRunOptions()
    active_phases, skipped_phases = resolve_active_phases(
      self.config,
      include_phases=opts.phases or None,
      skip_phases=opts.skip_phases or None,
    )

    t0 = time.perf_counter()
    started_iso = datetime.now(timezone.utc).isoformat()

    lineup_res: LineupRetrievalResult | None = None
    catalog_res: CatalogFetchResult | None = None
    lineup: CableLineup | None = None
    doc: MappingDocument | None = None
    mapping_path = self.config.resolve_path(self.config.state.mapping_path)
    mapping_saved = False
    probed_unique = 0
    would_probe_slots = 0
    playlist_path: Path | None = None
    report_path: Path | None = None
    xtream_res: XtreamExportResult | None = None

    needs_lineup = bool(
      set(active_phases) & {"lineup", "match", "validate", "m3u8", "xtream"}
    )
    needs_catalog = bool(set(active_phases) & {"catalog", "match", "validate"})

    # Phase (1): Lineup Retrieval (or silent prerequisite load)
    if needs_lineup:
      force_lu = ("lineup" in active_phases) and (
        "lineup" in opts.force_refresh
      )
      lu_res = await retrieve_cable_lineup(
        self.config,
        force_refresh=force_lu,
        dry_run=opts.dry_run,
      )
      lineup = lu_res.lineup
      if "lineup" in active_phases:
        lineup_res = lu_res

    # Phase (2): IPTV Catalog Fetch (or silent prerequisite load)
    if needs_catalog:
      force_cat = ("catalog" in active_phases) and (
        "catalog" in opts.force_refresh
      )
      cat_res = await fetch_daily_iptv_catalog(
        self.config,
        force_refresh=force_cat,
        dry_run=opts.dry_run,
      )
      catalog_res = cat_res

    # Phase (3): Stateful Rough Channel Matcher (or load existing mapping for downstream phases)
    if "match" in active_phases:
      assert lineup is not None and catalog_res is not None
      existing_doc = (
        MappingDocument.empty(lineup_id=lineup.lineup_id)
        if opts.reset_mapping
        else load_mapping_document(mapping_path, lineup_id=lineup.lineup_id)
      )
      doc = reconcile_rough_mapping(
        existing_doc, lineup, catalog_res.catalog, self.config
      )
      if not opts.dry_run:
        save_mapping_document(doc, mapping_path)
        mapping_saved = True
    elif set(active_phases) & {"validate", "m3u8", "xtream"}:
      assert lineup is not None
      if not mapping_path.is_file():
        raise FileNotFoundError(
          f"Channel mapping state file not found at {mapping_path}. "
          f"Run the `match` phase (or a full `bellhop run`) first."
        )
      doc = load_mapping_document(mapping_path, lineup_id=lineup.lineup_id)

    # Phase (4): Candidate-Only Stream Validation
    if "validate" in active_phases:
      assert doc is not None and lineup is not None
      only_changed = (
        False if opts.reprobe_all else self.config.validation.only_changed
      )

      # Count eligible slots first (used both for dry-run preview & progress totals)
      for sl in doc.slots.values():
        if not sl.candidates:
          continue
        if (
          only_changed
          and sl.preferred_source in {"validated", "pinned"}
          and sl.change_status in {"unchanged", "candidates_updated"}
        ):
          continue
        would_probe_slots += 1

      if not opts.dry_run and would_probe_slots > 0:
        account = catalog_res.catalog.account if catalog_res else None
        active_validator = self._custom_validator or StreamValidator(
          self.config, account
        )
        probed_unique = await validate_mapping_candidates(
          doc,
          lineup,
          self.config,
          account=account,
          only_changed=only_changed,
          on_channel_done=on_validation_progress,
          validator=active_validator,
          checkpoint_path=mapping_path,
        )
        save_mapping_document(doc, mapping_path)
        mapping_saved = True

    mappings: list[MappedChannel] = []
    if doc is not None and lineup is not None:
      mappings = mapping_document_to_mapped_channels(doc, lineup)

    # Phase (5): M3U8 Playlist & JSON Report Generation
    if "m3u8" in active_phases and not opts.dry_run:
      assert lineup is not None and doc is not None
      any_validated = any(
        sl.preferred_source == "validated" for sl in doc.slots.values()
      )
      raw_total = (
        len(catalog_res.catalog.streams)
        if catalog_res is not None
        else doc.last_summary.raw_catalog_streams
      )
      playlist_path = write_m3u8(lineup, mappings, self.config)
      report_path = write_report(
        lineup,
        mappings,
        self.config,
        total_catalog_streams=raw_total,
        validation_skipped=(
          "validate" not in active_phases and not any_validated
        ),
        mapping_summary=doc.last_summary.to_dict(),
      )

    # Phase (6): Official Xtream API JSON Playlist Export (with Passthrough EPG verification)
    if "xtream" in active_phases and not opts.dry_run:
      assert lineup is not None
      account = catalog_res.catalog.account if catalog_res is not None else None
      xtream_res = await export_xtream_playlist_and_guide(
        lineup,
        mappings,
        self.config,
        account=account,
        verify_epg=self.config.epg.verify,
      )

    finished_iso = datetime.now(timezone.utc).isoformat()
    return PipelineRunResult(
      started_at=started_iso,
      finished_at=finished_iso,
      duration_sec=time.perf_counter() - t0,
      dry_run=opts.dry_run,
      executed_phases=active_phases,
      skipped_phases=skipped_phases,
      lineup_result=lineup_res,
      catalog_result=catalog_res,
      lineup=lineup,
      mapping_doc=doc,
      mapping_path=mapping_path if doc is not None else None,
      mapping_saved=mapping_saved,
      mappings=mappings,
      validation_probed_streams=probed_unique,
      validation_would_probe_slots=would_probe_slots,
      playlist_path=playlist_path,
      report_path=report_path,
      xtream_result=xtream_res,
    )
