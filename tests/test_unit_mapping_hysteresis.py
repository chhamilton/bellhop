"""Focused unit tests for Phase (3) `mapping_state` 5.0-pt hysteresis rules."""

from __future__ import annotations

from bellhop.pipeline.mapping_state import (
  CandidateRecord,
  ChannelMappingSlot,
  _reconcile_single_slot,
)
from bellhop.pipeline.models import LineupChannel


def _rds_channel() -> LineupChannel:
  return LineupChannel(
    chno=1108,
    sd_chno=108,
    name="RDS",
    group="Sports",
    language="fr",
    callsign="RDS",
    tvg_id="RDS.ca",
  )


def test_hysteresis_retains_incumbent_below_threshold_and_queues_upgrade_above() -> (
  None
):
  """Verify <5.0 gain keeps incumbent while >=5.0 queues pending validation."""
  ch = _rds_channel()
  incumbent = CandidateRecord(
    stream_id=100,
    name="QC | RDS HD",
    score=100.0,
    validation_status="valid",
  )
  old_slot = ChannelMappingSlot(
    chno=1108,
    name="RDS",
    group="Sports",
    preferred_stream_id=100,
    preferred_source="validated",
    change_status="unchanged",
    candidates=[incumbent],
  )

  # 1. +4.9 score gain (< 5.0 hysteresis): incumbent #100 retained without churn
  minor_new = CandidateRecord(
    stream_id=101,
    name="QC | RDS FHD",
    score=104.9,
    validation_status="unvalidated",
  )
  kept = _reconcile_single_slot(
    ch=ch,
    candidates=[minor_new, incumbent],
    old_slot=old_slot,
    pinned_id=None,
    catalog_ids={100, 101},
    hysteresis=5.0,
  )
  assert kept.preferred_stream_id == 100
  assert kept.preferred_source == "validated"
  assert kept.change_status == "candidates_updated"

  # 2. +6.0 score gain (>= 5.0 hysteresis) with unvalidated challenger:
  #    keeps validated incumbent #100 active for live viewers while flagging
  #    `upgraded_pending_validation` so Phase (4) probes #102!
  major_new = CandidateRecord(
    stream_id=102,
    name="QC | RDS 4K 60FPS",
    score=106.0,
    validation_status="unvalidated",
  )
  pending = _reconcile_single_slot(
    ch=ch,
    candidates=[major_new, incumbent],
    old_slot=old_slot,
    pinned_id=None,
    catalog_ids={100, 102},
    hysteresis=5.0,
  )
  assert pending.preferred_stream_id == 100
  assert pending.preferred_source == "validated"
  assert pending.change_status == "upgraded_pending_validation"
  assert pending.candidates[0].stream_id == 102


def test_reconcile_missing_invalid_pinned_and_unmatched_transitions() -> None:
  """Verify missing/invalid incumbent promotion, pinned override, &
  unmatched.
  """
  ch = _rds_channel()
  cand_a = CandidateRecord(
    stream_id=200,
    name="QC | RDS Primary",
    score=110.0,
    validation_status="valid",
  )
  cand_b = CandidateRecord(
    stream_id=201, name="QC | RDS Backup", score=98.0, validation_status="valid"
  )

  # Initial match on empty state
  initial = _reconcile_single_slot(
    ch=ch,
    candidates=[cand_a, cand_b],
    old_slot=None,
    pinned_id=None,
    catalog_ids={200, 201},
    hysteresis=5.0,
  )
  assert initial.change_status == "new"
  assert initial.preferred_stream_id == 200
  assert initial.preferred_source == "validated"

  # Previous stream disappeared from upstream catalog -> promotes #201
  missing = _reconcile_single_slot(
    ch=ch,
    candidates=[cand_b],
    old_slot=initial,
    pinned_id=None,
    catalog_ids={201},
    hysteresis=5.0,
  )
  assert missing.change_status == "replaced_missing"
  assert missing.preferred_stream_id == 201

  # Previous stream marked invalid during probing -> promotes #201
  invalid_a = CandidateRecord(
    stream_id=200,
    name="QC | RDS Primary",
    score=110.0,
    validation_status="invalid",
  )
  replaced_inv = _reconcile_single_slot(
    ch=ch,
    candidates=[invalid_a, cand_b],
    old_slot=initial,
    pinned_id=None,
    catalog_ids={200, 201},
    hysteresis=5.0,
  )
  assert replaced_inv.change_status == "replaced_invalid"
  assert replaced_inv.preferred_stream_id == 201

  # Pinned stream overrides scoring
  pinned = _reconcile_single_slot(
    ch=ch,
    candidates=[cand_a, cand_b],
    old_slot=initial,
    pinned_id=201,
    catalog_ids={200, 201},
    hysteresis=5.0,
  )
  assert pinned.preferred_stream_id == 201
  assert pinned.preferred_source == "pinned"

  # All candidates invalid -> unmatched
  unmatched = _reconcile_single_slot(
    ch=ch,
    candidates=[invalid_a],
    old_slot=initial,
    pinned_id=None,
    catalog_ids={200},
    hysteresis=5.0,
  )
  assert unmatched.change_status == "unmatched"
  assert unmatched.preferred_stream_id is None
