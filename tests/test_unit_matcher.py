"""Focused unit tests for `ChannelMatcher` scoring, EPG plausibility, & filters."""

from __future__ import annotations

from bellhop.matcher import ChannelMatcher, _is_plausible_native_epg
from bellhop.models import LineupChannel, XtreamStream


def test_native_epg_plausibility_rejects_typos_and_accepts_matches() -> None:
  """Verify `_is_plausible_native_epg` filters panel EPG typos."""
  alias_specs: list[tuple[int, str, tuple[str, ...]]] = [
    (0, "Discovery Channel", ("discovery", "channel"))
  ]
  assert (
    _is_plausible_native_epg(
      "discovery.ca",
      tvg_id_lower="discoverychannel.ca",
      callsign_tok=None,
      alias_specs=alias_specs,
    )
    is True
  )
  assert (
    _is_plausible_native_epg(
      "cartoonnetwork.us",
      tvg_id_lower="discoverychannel.ca",
      callsign_tok=None,
      alias_specs=alias_specs,
    )
    is False
  )


def test_matcher_quality_tiers_backup_timeshift_and_nonlinear_filters() -> None:
  """Verify 4K > FHD > HD > SD, backup/timeshift penalties, and 24/7 rejection."""
  streams = [
    XtreamStream(stream_id=1, name="CA | TSN 1 SD", category_name="Sports"),
    XtreamStream(stream_id=2, name="CA | TSN 1 HD", category_name="Sports"),
    XtreamStream(stream_id=3, name="CA | TSN 1 FHD", category_name="Sports"),
    XtreamStream(stream_id=4, name="CA | TSN 1 4K UHD", category_name="Sports"),
    XtreamStream(
      stream_id=5, name="CA | TSN 1 FHD (BACKUP)", category_name="Sports"
    ),
    XtreamStream(stream_id=6, name="CA | TSN 1 FHD +1", category_name="Sports"),
    XtreamStream(stream_id=7, name="24/7 | TSN 1 FHD", category_name="24/7"),
  ]
  ch = LineupChannel(
    chno=1401,
    name="TSN1",
    group="Sports",
    language="en",
    regions=["ca", "us"],
    aliases=["TSN 1", "TSN1"],
  )
  cands = ChannelMatcher(streams).find_candidates(ch, limit=10)
  ranked_ids = [c.stream.stream_id for c in cands]
  # 24/7 stream (#7) is completely excluded; 4K > FHD > HD > backup/timeshift/SD
  assert 7 not in ranked_ids
  assert ranked_ids[:3] == [4, 3, 2]
  assert cands[0].quality_tier == "4k"


def test_matcher_foreign_region_penalties_and_spinoff_isolation() -> None:
  """Verify UK/France/foreign penalties and spinoff rejection."""
  streams = [
    XtreamStream(
      stream_id=10, name="UK | Comedy Central FHD", category_name="UK"
    ),
    XtreamStream(
      stream_id=11, name="AU | Comedy Central FHD", category_name="AU"
    ),
    XtreamStream(
      stream_id=12, name="US | Comedy Central FHD", category_name="US"
    ),
    XtreamStream(
      stream_id=20, name="FR | Canal Vie FHD", category_name="France"
    ),
    XtreamStream(
      stream_id=21, name="CA-FR | Canal Vie FHD", category_name="Canada FR"
    ),
  ]
  matcher = ChannelMatcher(streams)

  comedy = LineupChannel(
    chno=1625,
    name="Comedy Central",
    group="Entertainment",
    language="en",
    regions=["ca", "us"],
    aliases=["Comedy Central"],
  )
  cc_cands = matcher.find_candidates(comedy)
  assert cc_cands[0].stream.stream_id == 12

  canal_vie = LineupChannel(
    chno=1124,
    name="Canal Vie",
    group="Francophone",
    language="fr",
    regions=["montreal", "qc", "ca"],
    aliases=["Canal Vie"],
  )
  cv_cands = matcher.find_candidates(canal_vie)
  assert cv_cands[0].stream.stream_id == 21
