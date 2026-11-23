"""Focused unit tests for `ChannelMatcher` scoring, EPG plausibility, & filters."""

from __future__ import annotations

from bellhop.matcher import ChannelMatcher, _is_plausible_native_epg
from bellhop.models import LineupChannel, XtreamStream
from bellhop.providers.normalizer import (
  RawProviderSlot,
  normalize_provider_slots,
)


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


def test_weighted_edit_distance_compounds_symbols_decades_and_multipass_claiming() -> (
  None
):
  """Verify IDF-weighted edit distance + multipass exclusive station-group claiming."""
  streams = [
    XtreamStream(
      stream_id=101,
      name="CA: CTV SCIFI HD",
      epg_channel_id="ctvscifichannel.ca",
    ),
    XtreamStream(
      stream_id=102, name="CA: DEJAVIEW", epg_channel_id="DejaView.ca"
    ),
    XtreamStream(
      stream_id=103,
      name="CA: CRIME & INVESTIGATION",
      epg_channel_id="CrimeInvestigation.ca",
    ),
    XtreamStream(
      stream_id=104,
      name="CA EN: WEATHER NETWORK HD",
      epg_channel_id="TheWeatherNetwork.ca",
    ),
    XtreamStream(stream_id=105, name="PRIME: HOLLYWOOD SUITE 70S+ ᴿᴬᵂ"),
    XtreamStream(stream_id=106, name="PRIME: HOLLYWOOD SUITE 80S & 90S ᴿᴬᵂ"),
    XtreamStream(stream_id=107, name="PRIME: HOLLYWOOD SUITE 2000S ᴿᴬᵂ"),
    XtreamStream(
      stream_id=108,
      name="CA: BNN",
      epg_channel_id="BusinessNewsNetworkBloomberg.ca",
    ),
    XtreamStream(stream_id=109, name="CA FR: CANAL Z HD"),
    XtreamStream(stream_id=110, name="CA FR: ZESTE HD"),
    XtreamStream(
      stream_id=111, name="CA EN: CTV2 BARRIE HD", epg_channel_id="CKVR.ca"
    ),
    XtreamStream(
      stream_id=112,
      name="CA FR: TELETOON HD",
      epg_channel_id="TeletoonFrench.ca",
    ),
    XtreamStream(
      stream_id=113,
      name="CA EN: CARTOON NETWORK HD",
      epg_channel_id="CartoonNetwork.ca",
    ),
    XtreamStream(stream_id=114, name="US: CNN FHD", epg_channel_id="CNN.us"),
    XtreamStream(
      stream_id=115, name="CA EN: CRAVE 1 FHD", epg_channel_id="Crave1.ca"
    ),
  ]
  matcher = ChannelMatcher(streams)

  channels = [
    LineupChannel(
      chno=1627,
      name="CTV Sci-Fi",
      group="Entertainment",
      language="en",
      regions=["ca", "us"],
      aliases=["CTV Sci-Fi"],
    ),
    LineupChannel(
      chno=636,
      name="Deja View",
      group="Entertainment",
      language="en",
      regions=["ca", "us"],
      aliases=["Deja View"],
    ),
    LineupChannel(
      chno=1638,
      name="Crime + Investigation",
      group="Entertainment",
      language="en",
      regions=["ca", "us"],
      aliases=["Crime + Investigation"],
    ),
    LineupChannel(
      chno=1505,
      name="The Weather Network",
      group="News",
      language="en",
      regions=["ca", "us"],
      aliases=["The Weather Network"],
    ),
    LineupChannel(
      chno=1315,
      name="Hollywood Suite 70s+ Movies",
      group="Movies",
      language="en",
      regions=["ca", "us"],
      aliases=["Hollywood Suite 70s+ Movies"],
    ),
    LineupChannel(
      chno=1316,
      name="Hollywood Suite 80s & 90s Movies",
      group="Movies",
      language="en",
      regions=["ca", "us"],
      aliases=["Hollywood Suite 80s & 90s Movies"],
    ),
    LineupChannel(
      chno=1317,
      name="Hollywood Suite 00s Movies",
      group="Movies",
      language="en",
      regions=["ca", "us"],
      aliases=["Hollywood Suite 00s Movies"],
    ),
    LineupChannel(
      chno=1504,
      name="BNN Bloomberg",
      group="News",
      language="en",
      regions=["ca", "us"],
      aliases=["BNN Bloomberg"],
    ),
    LineupChannel(
      chno=1121,
      name="Z",
      group="French",
      language="fr",
      regions=["qc", "ca"],
      aliases=["Z"],
    ),
    LineupChannel(
      chno=1126,
      name="Zeste",
      group="French",
      language="fr",
      regions=["qc", "ca"],
      aliases=["Zeste"],
    ),
    LineupChannel(
      chno=1259,
      name="CTV Two (Barrie)",
      group="Broadcast",
      language="en",
      callsign="CKVR",
      regions=["on", "ca"],
      aliases=["CTV Two Barrie", "CTV Two"],
    ),
    # Multipass test 1: 153 Teletoon Francais locks #112 away from English 1554 Cartoon Network
    LineupChannel(
      chno=153,
      name="Teletoon Francais",
      group="French — Kids",
      language="fr",
      regions=["qc", "ca"],
      aliases=["Télétoon", "Teletoon Francais"],
    ),
    LineupChannel(
      chno=1554,
      name="Cartoon Network",
      group="Kids",
      language="en",
      regions=["ca", "us"],
      aliases=["Cartoon Network", "Teletoon"],
    ),
    # Multipass test 2: 1500 CNN locks domestic #114 away from 511 CNN International
    LineupChannel(
      chno=1500,
      name="CNN",
      group="News",
      language="en",
      regions=["ca", "us"],
      aliases=["CNN"],
    ),
    LineupChannel(
      chno=511,
      name="CNN International",
      group="News",
      language="en",
      regions=["ca", "us"],
      aliases=["CNN International"],
    ),
    # Multipass test 3: Dual-dial twins sharing gracenote_station_id BOTH keep #115!
    LineupChannel(
      chno=1300,
      name="Crave1",
      group="Movies",
      language="en",
      gracenote_station_id="9266071088",
      regions=["ca"],
      aliases=["Crave 1"],
    ),
    LineupChannel(
      chno=1310,
      name="Crave1",
      group="Movies",
      language="en",
      gracenote_station_id="9266071088",
      regions=["ca"],
      aliases=["Crave 1"],
    ),
  ]

  mapped = {
    m.channel.chno: m.candidates for m in matcher.match_lineup(channels)
  }
  assert mapped[1627][0].stream.stream_id == 101
  assert mapped[636][0].stream.stream_id == 102
  assert mapped[1638][0].stream.stream_id == 103
  assert mapped[1505][0].stream.stream_id == 104
  assert [c.stream.stream_id for c in mapped[1315]] == [105]
  assert [c.stream.stream_id for c in mapped[1316]] == [106]
  assert [c.stream.stream_id for c in mapped[1317]] == [107]
  assert mapped[1504][0].stream.stream_id == 108
  assert [c.stream.stream_id for c in mapped[1121]] == [109]
  assert [c.stream.stream_id for c in mapped[1126]] == [110]
  assert mapped[1259][0].stream.stream_id == 111
  assert mapped[153][0].stream.stream_id == 112
  assert [c.stream.stream_id for c in mapped[1554]] == [113]
  assert mapped[1500][0].stream.stream_id == 114
  assert mapped[511] == []
  assert mapped[1300][0].stream.stream_id == 115
  assert mapped[1310][0].stream.stream_id == 115


def test_callsign_epg_stem_ota_virtual_channel_tail_and_rare_feed_suffixes() -> (
  None
):
  """Verify callsign EPG stem (`ASN.ca`), OTA tail (`WPIX`->`PIX 11`), `YTVW`, `ATN`, & rare feed suffixes."""
  norm_slots = normalize_provider_slots(
    [
      RawProviderSlot(
        chno=552,
        raw_name="Youth Television Pacific",
        callsign="YTVW",
        country="CAN",
      ),
      RawProviderSlot(
        chno=727,
        raw_name="New Tang Dynasty Television",
        callsign="NTDTV",
        country="CAN",
      ),
      RawProviderSlot(
        chno=793,
        raw_name="ATN Aastha",
        callsign="ATNASTH",
        country="CAN",
      ),
      RawProviderSlot(
        chno=795,
        raw_name="ATN B4U Music",
        callsign="ATNB4UM",
        country="CAN",
      ),
      RawProviderSlot(
        chno=808,
        raw_name="Aaj Tak TV",
        callsign="AAJTAK",
        country="CAN",
      ),
      RawProviderSlot(
        chno=1258,
        raw_name="A Atlantic",
        callsign="ASN-C",
        country="CAN",
      ),
      RawProviderSlot(
        chno=1277,
        raw_name="CHAN Burnaby, British Columbia (Global)",
        callsign="CHAN",
        country="CAN",
      ),
      RawProviderSlot(
        chno=1297,
        raw_name="WPIX New York, New York (CW)",
        callsign="WPIX",
        country="USA",
      ),
      RawProviderSlot(
        chno=220,
        raw_name="WPTZ Plattsburg, New York (NBC)",
        callsign="WPTZ",
        country="USA",
      ),
      RawProviderSlot(
        chno=221,
        raw_name="WCAX Burlington, Vermont (CBS)",
        callsign="WCAX",
        country="USA",
      ),
    ],
    postal_code="H2Y",
    market="Montréal, QC",
    provider_name="Bell Fibe TV",
    lineup_id="TVG-9100000607",
  )

  streams = [
    XtreamStream(stream_id=5520, name="CA: YTV"),
    XtreamStream(stream_id=7270, name="AT&T: NTD NEWS ᴿᴬᵂ"),
    XtreamStream(stream_id=7930, name="AT&T: AASTHA TV ᴿᴬᵂ"),
    XtreamStream(stream_id=7950, name="AT&T: B4U MUSIC ᴿᴬᵂ"),
    XtreamStream(stream_id=8080, name="AT&T: AAJTAK INTERNATIONAL ᴿᴬᵂ"),
    XtreamStream(
      stream_id=12580,
      name="CA EN: CTV2 ATLANTIC HD",
      epg_channel_id="ASN.ca",
    ),
    XtreamStream(stream_id=12770, name="US: JACKIE CHAN ADVENTURES 4K"),
    XtreamStream(stream_id=12970, name="US: CW PIX 11 HD"),
    XtreamStream(stream_id=2200, name="US: NBC 5 PLATTSBURGH NY (WPTZ) HD"),
    XtreamStream(stream_id=2210, name="US: CBS NEWS HD"),
  ]
  matcher = ChannelMatcher(streams)
  mapped = {
    m.channel.chno: m.candidates for m in matcher.match_lineup(norm_slots)
  }

  assert mapped[552] and mapped[552][0].stream.stream_id == 5520
  assert mapped[727] and mapped[727][0].stream.stream_id == 7270
  assert mapped[793] and mapped[793][0].stream.stream_id == 7930
  assert mapped[795] and mapped[795][0].stream.stream_id == 7950
  assert mapped[808] and mapped[808][0].stream.stream_id == 8080
  assert mapped[1258] and mapped[1258][0].stream.stream_id == 12580
  # OTA callsign "CHAN" must not match surname inside "JACKIE CHAN ADVENTURES"
  assert mapped[1277] == []
  assert mapped[1297] and mapped[1297][0].stream.stream_id == 12970
  assert mapped[220] and mapped[220][0].stream.stream_id == 2200
  # Mainstream umbrella brands ("CBS") must NEVER collapse into "<Brand> News"
  assert mapped[221] == []
