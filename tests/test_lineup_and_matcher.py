"""Tests for Bell Fibe Montreal YAML lineup integrity and multi-signal channel matching."""

from __future__ import annotations

from pathlib import Path

from bellhop.lineup_loader import CableLineup, load_lineup
from bellhop.matcher import ChannelMatcher
from bellhop.models import XtreamStream
from bellhop.providers.normalizer import (
  RawProviderSlot,
  normalize_provider_slots,
)

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


def test_non_montreal_lineup_regional_and_timezone_generalization() -> None:
  """Verify Toronto, Vancouver, New York, and LA lineups infer local feeds & logos."""
  streams = [
    XtreamStream(
      stream_id=101,
      name="CA | CTV Montreal (CFCF) FHD",
      category_name="Canada EN",
    ),
    XtreamStream(
      stream_id=102,
      name="CA | CTV Toronto (CFTO) FHD",
      category_name="Canada EN",
    ),
    XtreamStream(
      stream_id=103,
      name="CA | CTV Vancouver (CIVT) FHD",
      category_name="Canada EN",
    ),
    XtreamStream(
      stream_id=201,
      name="CA | CBC Toronto (CBLT) FHD",
      category_name="Canada EN",
    ),
    XtreamStream(
      stream_id=202,
      name="CA | CBC Vancouver (CBUT) FHD",
      category_name="Canada EN",
    ),
    XtreamStream(
      stream_id=301,
      name="CA | TSN East FHD",
      category_name="Canada Sports",
    ),
    XtreamStream(
      stream_id=302,
      name="CA | TSN West FHD",
      category_name="Canada Sports",
    ),
    XtreamStream(
      stream_id=401,
      name="US | NBC New York (WNBC) FHD",
      category_name="USA Locals",
    ),
    XtreamStream(
      stream_id=402,
      name="US | NBC Los Angeles (KNBC) FHD",
      category_name="USA Locals",
    ),
    XtreamStream(
      stream_id=501,
      name="US | HBO East FHD",
      category_name="USA Movies",
    ),
    XtreamStream(
      stream_id=502,
      name="US | HBO West FHD",
      category_name="USA Movies",
    ),
  ]
  matcher = ChannelMatcher(streams)

  # 1. Rogers Toronto (M5V): chno 1101 keeps its CFTO logo (not Bell Fibe CBFT)
  #    and prioritizes CTV Toronto (#102), while out-of-market CTV Vancouver (#1250)
  #    picks #103.
  toronto_channels = normalize_provider_slots(
    [
      RawProviderSlot(
        chno=1101,
        raw_name="CTV HD",
        callsign="CFTO-HD",
        logo_url="https://logos.example/cfto.png",
        country="CAN",
      ),
      RawProviderSlot(
        chno=1250,
        raw_name="CTV Vancouver HD",
        callsign="CIVT-HD",
        country="CAN",
      ),
    ],
    postal_code="M5V 2T6",
    market="Toronto, ON",
    provider_name="Rogers - Toronto (Digital)",
    lineup_id="TVG-9100009999",
  )
  tor_by_chno = {c.chno: c for c in toronto_channels}
  assert tor_by_chno[1101].logo == "https://logos.example/cfto.png"
  assert "toronto" in tor_by_chno[1101].regions
  assert "vancouver" in tor_by_chno[1250].regions
  assert "toronto" not in tor_by_chno[1250].regions
  assert matcher.find_candidates(tor_by_chno[1101])[0].stream.stream_id == 102
  assert matcher.find_candidates(tor_by_chno[1250])[0].stream.stream_id == 103

  # Snapshot round-trip of non-Bell-Fibe lineup also avoids Bell Fibe chno logo bleed
  reloaded_tor = CableLineup.from_dict(
    {
      "lineup_id": "TVG-9100009999",
      "provider": "Rogers - Toronto (Digital)",
      "market": "Toronto, ON",
      "postal_prefix": "M5V",
      "channels": [{"chno": 1101, "name": "CTV", "group": "Local", "logo": ""}],
    }
  )
  assert reloaded_tor.channels[0].logo is None

  # 2. Telus Vancouver (V6B): CBC picks Vancouver (#202) and TSN prefers West (#302)
  van_channels = normalize_provider_slots(
    [
      RawProviderSlot(
        chno=101,
        raw_name="CBC HD",
        callsign="CBUT-DT",
        country="CAN",
      ),
      RawProviderSlot(
        chno=400,
        raw_name="TSN HD",
        callsign="TSN",
        country="CAN",
      ),
    ],
    postal_code="V6B 1A1",
    market="Vancouver, BC",
    provider_name="Telus Optik TV - Vancouver",
    lineup_id="CAN-TELUS-VAN",
  )
  van_by_chno = {c.chno: c for c in van_channels}
  assert "vancouver" in van_by_chno[101].regions
  assert "west" in van_by_chno[400].regions
  assert matcher.find_candidates(van_by_chno[101])[0].stream.stream_id == 202
  tsn_west = matcher.find_candidates(van_by_chno[400])[0]
  assert tsn_west.stream.stream_id == 302
  assert "tz:west" in tsn_west.reasons

  # 3. New York (10001) vs Los Angeles (90001): local NBC + East/West HBO feeds
  ny_channels = normalize_provider_slots(
    [
      RawProviderSlot(chno=4, raw_name="NBC", callsign="WNBC", country="USA"),
      RawProviderSlot(chno=501, raw_name="HBO", callsign="HBO", country="USA"),
    ],
    postal_code="10001",
    market="New York, NY",
    provider_name="Astound - Manhattan",
    lineup_id="TVG-9133001919",
  )
  ny_by_chno = {c.chno: c for c in ny_channels}
  assert matcher.find_candidates(ny_by_chno[4])[0].stream.stream_id == 401
  assert matcher.find_candidates(ny_by_chno[501])[0].stream.stream_id == 501

  la_channels = normalize_provider_slots(
    [
      RawProviderSlot(chno=4, raw_name="NBC", callsign="KNBC", country="USA"),
      RawProviderSlot(chno=501, raw_name="HBO", callsign="HBO", country="USA"),
    ],
    postal_code="90001",
    market="Los Angeles, CA",
    provider_name="Spectrum - Los Angeles",
    lineup_id="USA-LA-X",
  )
  la_by_chno = {c.chno: c for c in la_channels}
  assert matcher.find_candidates(la_by_chno[4])[0].stream.stream_id == 402
  assert matcher.find_candidates(la_by_chno[501])[0].stream.stream_id == 502
