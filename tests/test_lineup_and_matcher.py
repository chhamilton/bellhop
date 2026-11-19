"""Tests for Bell Fibe Montreal YAML lineup integrity and multi-signal channel matching."""

from __future__ import annotations

from pathlib import Path

from bellhop.lineup_loader import load_lineup
from bellhop.matcher import ChannelMatcher
from bellhop.models import XtreamStream

LINEUP_PATH = (
  Path(__file__).resolve().parents[1]
  / "data"
  / "lineups"
  / "bell_fibe_montreal.yaml"
)


def test_bell_fibe_montreal_lineup_integrity() -> None:
  lineup = load_lineup(LINEUP_PATH)
  assert lineup.provider == "Bell Fibe TV"
  assert "Montr" in lineup.market
  assert len(lineup.channels) >= 100

  hd_numbers = [c.chno for c in lineup.channels]
  sd_numbers = [c.sd_chno for c in lineup.channels if c.sd_chno is not None]
  assert len(hd_numbers) == len(set(hd_numbers)), (
    "Duplicate HD channel numbers found"
  )
  assert len(sd_numbers) == len(set(sd_numbers)), (
    "Duplicate SD channel numbers found"
  )

  by_chno = {c.chno: c for c in lineup.channels}
  # Key Montreal French & English channels
  assert by_chno[1101].callsign == "CBFT"
  assert by_chno[1102].callsign == "CFJP"
  assert by_chno[1103].callsign == "CFTM"
  assert by_chno[1108].name == "RDS"
  assert by_chno[1111].name == "TVA Sports"
  assert by_chno[1201].callsign == "CFCF"
  assert by_chno[1203].callsign == "CKMI"
  assert by_chno[1205].callsign == "CBMT"
  assert by_chno[1221].callsign == "WVNY"
  assert by_chno[1402].tvg_id == "TSN2.ca"


def test_matcher_prioritizes_montreal_and_isolates_siblings() -> None:
  lineup = load_lineup(LINEUP_PATH)
  by_chno = {c.chno: c for c in lineup.channels}

  sample_streams = [
    XtreamStream(
      stream_id=10, name="##### CANADA FRENCH #####", category_name="QC"
    ),
    XtreamStream(
      stream_id=101,
      name="QC | ICI Radio-Canada Ottawa HD",
      category_name="Canada FR",
    ),
    XtreamStream(
      stream_id=102, name="QC | ICI Radio-Canada HD", category_name="Canada FR"
    ),
    XtreamStream(
      stream_id=103,
      name="QC | ICI Radio-Canada Montréal (CBFT) FHD",
      category_name="Canada FR",
      epg_channel_id="CBFT.ca",
    ),
    XtreamStream(
      stream_id=201, name="QC | RDS 2 FHD", category_name="Canada Sports"
    ),
    XtreamStream(
      stream_id=202, name="QC | RDS Info FHD", category_name="Canada Sports"
    ),
    XtreamStream(
      stream_id=203, name="QC | RDS FHD 60FPS", category_name="Canada FR"
    ),
    XtreamStream(
      stream_id=301, name="QC | Séries+ FHD", category_name="Canada FR"
    ),
    XtreamStream(stream_id=302, name="QC | Z FHD", category_name="Canada FR"),
    XtreamStream(
      stream_id=303, name="24/7 | Dragon Ball Z FHD", category_name="24/7"
    ),
    XtreamStream(
      stream_id=401,
      name="CA | CTV Toronto (CFTO) FHD",
      category_name="Canada EN",
    ),
    XtreamStream(
      stream_id=402,
      name="CA | CTV Sci-Fi Channel FHD",
      category_name="Canada EN",
    ),
    XtreamStream(
      stream_id=403, name="CA | CTV Montreal FHD", category_name="Canada EN"
    ),
    XtreamStream(
      stream_id=501, name="CA | TSN1 FHD", category_name="Canada Sports"
    ),
    XtreamStream(
      stream_id=502, name="CA | TSN2 FHD", category_name="Canada Sports"
    ),
    XtreamStream(
      stream_id=601, name="UK | E! Entertainment FHD", category_name="UK"
    ),
    XtreamStream(stream_id=602, name="CA | E! HD", category_name="Canada EN"),
  ]

  matcher = ChannelMatcher(sample_streams, pinned_streams={1401: 501})

  # 1101 ICI Radio-Canada Montreal: picks Montreal FHD (#103), ranks generic (#102) 2nd, excludes Ottawa (#101)
  rc_cands = matcher.find_candidates(by_chno[1101])
  assert [c.stream.stream_id for c in rc_cands] == [103, 102]

  # 1108 RDS vs 1109 RDS2 vs 1110 RDS Info
  assert matcher.find_candidates(by_chno[1108])[0].stream.stream_id == 203
  assert len(matcher.find_candidates(by_chno[1108])) == 1
  assert matcher.find_candidates(by_chno[1109])[0].stream.stream_id == 201
  assert matcher.find_candidates(by_chno[1110])[0].stream.stream_id == 202

  # 1122 Séries Plus matches "Séries+" via "+" -> "plus" normalization
  assert matcher.find_candidates(by_chno[1122])[0].stream.stream_id == 301

  # 1121 Z matches "QC | Z FHD" and ignores 24/7 Dragon Ball Z
  z_cands = matcher.find_candidates(by_chno[1121])
  assert [c.stream.stream_id for c in z_cands] == [302]

  # 1201 CTV Montreal excludes CTV Toronto & CTV Sci-Fi
  ctv_cands = matcher.find_candidates(by_chno[1201])
  assert [c.stream.stream_id for c in ctv_cands] == [403]

  # 1402 TSN2 vs pinned 1401 TSN1
  assert matcher.find_candidates(by_chno[1402])[0].stream.stream_id == 502
  tsn1_top = matcher.find_candidates(by_chno[1401])[0]
  assert tsn1_top.stream.stream_id == 501
  assert "pinned_stream_id" in tsn1_top.reasons

  # 1621 E! prefers Canadian feed (#602) and rejects UK feed (#601)
  e_cands = matcher.find_candidates(by_chno[1621])
  assert [c.stream.stream_id for c in e_cands] == [602]


def test_every_bell_fibe_channel_matches_canonical_xtream_stream() -> None:
  """Verify all 163 Bell Fibe Montreal channels match realistic CA/QC Xtream stream names."""
  lineup = load_lineup(LINEUP_PATH)
  synthetic_streams = [
    XtreamStream(
      stream_id=10000 + ch.chno,
      name=f"{'QC' if ch.language == 'fr' else 'CA'} | {ch.aliases[0]} FHD",
      category_name="Canada FR" if ch.language == "fr" else "Canada EN",
    )
    for ch in lineup.channels
  ]
  matcher = ChannelMatcher(synthetic_streams)
  mappings = matcher.match_lineup(lineup.channels)

  unmatched = [m.channel.name for m in mappings if not m.candidates]
  assert unmatched == [], f"Channels failed self-matching: {unmatched}"

  wrong_top = [
    (m.channel.chno, m.channel.name, m.candidates[0].stream.name)
    for m in mappings
    if m.candidates[0].stream.stream_id != 10000 + m.channel.chno
  ]
  assert wrong_top == [], f"Channels matched wrong sibling stream: {wrong_top}"
