"""Tests for the stateful pipeline phases and incremental tracking.

Covers monthly lineup, daily catalog, matching, candidate validation,
and M3U8 exports.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from bellhop.config import (
  AppConfig,
  LineupConfig,
  StateConfig,
  ValidationConfig,
  XtreamConfig,
)
from bellhop.pipeline.catalog_fetcher import (
  compute_catalog_diff,
  fetch_daily_iptv_catalog,
)
from bellhop.pipeline.lineup_loader import CableLineup
from bellhop.pipeline.lineup_retriever import (
  GracenoteLineupId,
  LineupRetrieverError,
  compute_lineup_diff,
  retrieve_cable_lineup,
)
from bellhop.pipeline.mapping_state import (
  MappingDocument,
  mapping_document_to_mapped_channels,
  reconcile_rough_mapping,
  validate_mapping_candidates,
)
from bellhop.pipeline.matcher import ChannelMatcher
from bellhop.pipeline.models import (
  LineupChannel,
  XtreamAccountInfo,
  XtreamStream,
)
from bellhop.pipeline.playlist import render_m3u8
from bellhop.pipeline.validator import StreamValidator
from bellhop.xtream import XtreamCatalog
from tests.http_mock import MockHttpServer


def _ts_payload(packets: int = 50) -> bytes:
  return (bytes([0x47, 0x40, 0x00, 0x10]) + b"\xff" * 184) * packets


@pytest.mark.asyncio
async def test_gracenote_lineup_retriever_monthly_cadence_and_diff(
  tmp_path: Path,
) -> None:
  parsed = GracenoteLineupId.parse("can-0000002-x")
  assert parsed.raw_id == "CAN-0000002-X"
  assert parsed.country == "CAN"
  assert parsed.headend == "0000002"
  assert parsed.device == "X"

  with pytest.raises(LineupRetrieverError):
    GracenoteLineupId.parse("not_gracenote")

  repo_lineup_yaml = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "lineups"
    / "bell_fibe_montreal.yaml"
  )
  cfg = AppConfig(
    project_root=tmp_path,
    lineup=LineupConfig(
      lineup_id="CAN-BELLFIBE-MTL-X",
      postal_code="H3B 1A1",
      refresh_days=30.0,
      file=repo_lineup_yaml,
    ),
    state=StateConfig(state_dir=Path("state")),
  )

  res1 = await retrieve_cable_lineup(cfg)
  assert not res1.from_cache
  assert res1.lineup.lineup_id == "CAN-BELLFIBE-MTL-X"
  assert len(res1.diff.added_chnos) == len(res1.lineup.channels)

  # Second call within 30 days hits monthly state snapshot
  res2 = await retrieve_cable_lineup(cfg)
  assert res2.from_cache
  assert len(res2.lineup.channels) == len(res1.lineup.channels)

  # Verify LineupDiff detects added, removed, and renamed channels
  lu_a = CableLineup(
    provider="Bell Fibe TV",
    market="Montréal",
    postal_prefix="H3B",
    channels=[
      LineupChannel(chno=1101, name="ICI Radio-Canada", group="FR"),
      LineupChannel(chno=1102, name="Noovo", group="FR"),
    ],
  )
  lu_b = CableLineup(
    provider="Bell Fibe TV",
    market="Montréal",
    postal_prefix="H3B",
    channels=[
      LineupChannel(chno=1101, name="ICI Radio-Canada Télé", group="FR"),
      LineupChannel(chno=1108, name="RDS", group="Sports"),
    ],
  )
  diff = compute_lineup_diff(lu_a, lu_b)
  assert diff.added_chnos == [1108]
  assert diff.removed_chnos == [1102]
  assert diff.modified_chnos == [1101]


@pytest.mark.asyncio
async def test_daily_iptv_fetcher_cadence_and_diff(tmp_path: Path) -> None:
  s1 = [
    XtreamStream(
      stream_id=1, name="CA FR: RDS", category_name="CA", epg_channel_id=""
    ),
    XtreamStream(
      stream_id=2,
      name="CA FR: TVA",
      category_name="CA",
      epg_channel_id="TVA.ca",
    ),
  ]
  s2 = [
    XtreamStream(
      stream_id=1,
      name="CA FR: RDS HD",
      category_name="CA",
      epg_channel_id="RDS.ca",
    ),
    XtreamStream(
      stream_id=3,
      name="CA: TSN 2 FHD",
      category_name="CA",
      epg_channel_id="TSN2.ca",
    ),
  ]
  cdiff = compute_catalog_diff(s1, s2)
  assert cdiff.added_stream_ids == [3]
  assert cdiff.removed_stream_ids == [2]
  assert cdiff.renamed_stream_ids == [1]
  assert cdiff.epg_updated_stream_ids == [1]

  calls = {"n": 0}

  def api_handler(request: web.Request) -> web.Response:
    calls["n"] += 1
    action = request.query.get("action")
    if action is None:
      return web.json_response(
        {
          "user_info": {
            "username": "u",
            "auth": 1,
            "status": "Active",
            "max_connections": "1",
          }
        }
      )
    if action == "get_live_categories":
      return web.json_response(
        [{"category_id": "1", "category_name": "CA| FR"}]
      )
    return web.json_response(
      [
        {
          "stream_id": 10,
          "name": "CA FR: RDS HD",
          "category_id": "1",
          "epg_channel_id": "RDS.ca",
        }
      ]
    )

  cfg = AppConfig(
    project_root=tmp_path,
    xtream=XtreamConfig(
      server_url="http://panel.example",
      username="u",
      password="p",
      cache_ttl_hours=24.0,
    ),
    state=StateConfig(state_dir=Path("state")),
  )

  async with MockHttpServer(api_handler) as mock, mock.new_session() as client:
    r1 = await fetch_daily_iptv_catalog(cfg, client=client)
    assert not r1.from_cache
    assert r1.streams_with_native_epg == 1
    assert calls["n"] == 3

    # Subsequent run within 24h reuses daily snapshot with 0 HTTP calls
    r2 = await fetch_daily_iptv_catalog(cfg, client=client)
    assert r2.from_cache
    assert calls["n"] == 3


def test_matcher_prefers_native_epg_and_higher_quality_and_filters_bad_epg() -> (
  None
):
  ch_noovo = LineupChannel(
    chno=1102,
    sd_chno=102,
    name="Noovo (Montréal)",
    callsign="CFJP",
    tvg_id="CFJP.ca",
    group="Francophone",
    language="fr",
    regions=["montreal", "qc", "ca"],
    aliases=["Noovo Montreal", "Noovo", "V Montreal"],
  )
  ch_se4 = LineupChannel(
    chno=1164,
    sd_chno=164,
    name="Super Écran 4",
    tvg_id="SuperEcran4.ca",
    group="Cinéma",
    language="fr",
    regions=["qc", "ca"],
    aliases=["Super Ecran 4"],
    exclude=["1", "2", "3"],
  )
  ch_telemag = LineupChannel(
    chno=1119,
    sd_chno=119,
    name="Télé-Mag",
    tvg_id="TeleMag.ca",
    group="Francophone",
    language="fr",
    regions=["qc", "ca"],
    aliases=["Tele-Mag", "Telemag"],
  )

  streams = [
    XtreamStream(
      stream_id=1,
      name="CA FR: NOOVO SD",
      category_name="CA| FR",
      epg_channel_id="",
    ),
    XtreamStream(
      stream_id=2,
      name="CA FR: NOOVO HD",
      category_name="CA| FR",
      epg_channel_id="",
    ),
    XtreamStream(
      stream_id=3,
      name="CA FR: NOOVO FHD 60FPS",
      category_name="CA| FR",
      epg_channel_id="Noovo.ca",
    ),
    # Mislabeled EPG from real provider panel
    XtreamStream(
      stream_id=412880,
      name="CA FR: SUPER ECRAN 4",
      category_name="CA| FR",
      epg_channel_id="Telemag.ca",
    ),
    XtreamStream(
      stream_id=412881,
      name="CA FR: SUPER ECRAN 4 HD",
      category_name="CA| FR",
      epg_channel_id="SuperEcran4.ca",
    ),
  ]

  matcher = ChannelMatcher(streams)
  noovo_cands = matcher.find_candidates(ch_noovo)
  assert [c.stream.stream_id for c in noovo_cands] == [3, 2, 1]
  assert noovo_cands[0].has_native_epg is True
  assert noovo_cands[0].quality_tier == "fhd"
  assert noovo_cands[2].quality_tier == "sd"

  # Télé-Mag must NOT match SUPER ECRAN 4 despite epg_channel_id="Telemag.ca"
  assert matcher.find_candidates(ch_telemag) == []

  # Super Écran 4 prefers #412881 (HD + plausible native EPG) and marks #412880
  # has_native_epg=False
  se4_cands = matcher.find_candidates(ch_se4)
  assert se4_cands[0].stream.stream_id == 412881
  assert se4_cands[0].has_native_epg is True
  mislabeled = next(c for c in se4_cands if c.stream.stream_id == 412880)
  assert mislabeled.has_native_epg is False


def test_incremental_rough_mapping_updates(tmp_path: Path) -> None:
  cfg = AppConfig(project_root=tmp_path)
  lineup_v1 = CableLineup(
    provider="Bell Fibe TV",
    market="Montréal, QC",
    postal_prefix="H3B",
    channels=[
      LineupChannel(
        chno=1108,
        name="RDS",
        group="Sports",
        language="fr",
        regions=["qc", "ca"],
        aliases=["RDS"],
      ),
      LineupChannel(
        chno=1201,
        name="CTV (Montréal)",
        group="EN",
        language="en",
        regions=["montreal", "ca"],
        aliases=["CTV Montreal", "CTV"],
      ),
      LineupChannel(
        chno=1402,
        name="TSN2",
        group="Sports",
        language="en",
        regions=["ca"],
        aliases=["TSN 2", "TSN2"],
      ),
    ],
  )
  cat_day1 = XtreamCatalog(
    account=XtreamAccountInfo(username="u", max_connections=1),
    categories={},
    streams=[
      XtreamStream(
        stream_id=10, name="CA FR: RDS", category_name="CA", epg_channel_id=""
      ),
      XtreamStream(
        stream_id=20,
        name="CA: CTV MONTREAL HD",
        category_name="CA",
        epg_channel_id="CFCF.ca",
      ),
      XtreamStream(
        stream_id=30, name="CA: TSN 2 HD", category_name="CA", epg_channel_id=""
      ),
    ],
  )

  # Run 1: empty initial mapping
  doc1 = reconcile_rough_mapping(
    MappingDocument.empty(), lineup_v1, cat_day1, cfg
  )
  assert doc1.last_summary.was_empty_initial_run is True
  assert doc1.last_summary.new_slots == 3
  assert doc1.slots[1108].preferred_stream_id == 10

  # Run 2: identical catalog & lineup -> all 3 unchanged
  doc2 = reconcile_rough_mapping(doc1, lineup_v1, cat_day1, cfg)
  assert doc2.last_summary.was_empty_initial_run is False
  assert doc2.last_summary.unchanged_slots == 3

  # Run 3:
  # - RDS gets a new FHD + native EPG stream (#11) -> upgraded!
  # - TSN2's old stream (#30) disappears, replaced by #31 -> replaced_missing!
  # - Lineup drops 1201 and adds 1115 LCN -> removed_lineup_chnos=[1201],
  # new_slots=1!
  lineup_v2 = CableLineup(
    provider="Bell Fibe TV",
    market="Montréal, QC",
    postal_prefix="H3B",
    channels=[
      LineupChannel(
        chno=1108,
        name="RDS",
        group="Sports",
        language="fr",
        regions=["qc", "ca"],
        aliases=["RDS"],
      ),
      LineupChannel(
        chno=1115,
        name="LCN",
        group="News",
        language="fr",
        regions=["qc", "ca"],
        aliases=["LCN"],
      ),
      LineupChannel(
        chno=1402,
        name="TSN2",
        group="Sports",
        language="en",
        regions=["ca"],
        aliases=["TSN 2", "TSN2"],
      ),
    ],
  )
  cat_day2 = XtreamCatalog(
    account=XtreamAccountInfo(username="u", max_connections=1),
    categories={},
    streams=[
      XtreamStream(
        stream_id=10, name="CA FR: RDS", category_name="CA", epg_channel_id=""
      ),
      XtreamStream(
        stream_id=11,
        name="CA FR: RDS FHD 60FPS",
        category_name="CA",
        epg_channel_id="RDS.ca",
      ),
      XtreamStream(
        stream_id=31,
        name="CA: TSN 2 HD",
        category_name="CA",
        epg_channel_id="TSN2.ca",
      ),
      XtreamStream(
        stream_id=40,
        name="CA FR: LCN HD",
        category_name="CA",
        epg_channel_id="LCN.ca",
      ),
    ],
  )

  doc3 = reconcile_rough_mapping(doc2, lineup_v2, cat_day2, cfg)
  assert doc3.last_summary.removed_lineup_chnos == [1201]
  assert doc3.slots[1108].change_status == "upgraded"
  assert doc3.slots[1108].preferred_stream_id == 11
  assert doc3.slots[1402].change_status == "replaced_missing"
  assert doc3.slots[1402].preferred_stream_id == 31
  assert doc3.slots[1115].change_status == "new"
  assert doc3.slots[1115].preferred_stream_id == 40


@pytest.mark.asyncio
async def test_candidate_only_validation_and_native_epg_m3u8(
  tmp_path: Path,
) -> None:
  lineup = CableLineup(
    lineup_id="CAN-BELLFIBE-MTL-X",
    provider="Bell Fibe TV",
    market="Montréal, QC",
    postal_prefix="H3B",
    channels=[
      LineupChannel(
        chno=1101,
        sd_chno=101,
        name="ICI Radio-Canada Télé (Montréal)",
        tvg_id="CBFT.ca",
        group="Francophone",
        language="fr",
        regions=["montreal", "qc", "ca"],
        aliases=["ICI Montreal", "Radio-Canada"],
      )
    ],
  )

  # 200 unrelated raw streams + 2 candidate streams for 1101
  unrelated = [
    XtreamStream(
      stream_id=5000 + i, name=f"AR: RANDOM CHANNEL {i}", category_name="AR"
    )
    for i in range(200)
  ]
  cands = [
    XtreamStream(
      stream_id=901,
      name="CA FR: ICI MONTREAL FHD",
      category_name="CA",
      epg_channel_id="ICITeleMontreal.ca",
    ),
    XtreamStream(
      stream_id=902,
      name="CA FR: ICI MONTREAL HD",
      category_name="CA",
      epg_channel_id="ICITeleMontreal.ca",
    ),
  ]
  catalog = XtreamCatalog(
    account=XtreamAccountInfo(username="u", max_connections=1),
    categories={},
    streams=unrelated + cands,
  )

  cfg = AppConfig(
    project_root=tmp_path,
    xtream=XtreamConfig(
      server_url="provider.example", username="u", password="p"
    ),
    validation=ValidationConfig(
      concurrency=1,
      inter_probe_delay_sec=0.0,
      min_bytes_to_read=2048,
      use_ffprobe=False,
    ),
  )

  doc = reconcile_rough_mapping(None, lineup, catalog, cfg)
  # Candidate pool contains ONLY the 2 matched streams (99% reduction from 202
  # raw streams!)
  assert doc.unique_candidate_stream_ids() == {901, 902}

  probed_urls: list[str] = []
  ts_data = _ts_payload(40)

  def mock_stream(req: web.Request) -> web.Response:
    probed_urls.append(str(req.url))
    if req.path.endswith("/901.ts"):
      return web.Response(status=503, text="Offline")
    if req.path.endswith("/902.ts"):
      return web.Response(
        status=200, headers={"Content-Type": "video/mp2t"}, body=ts_data
      )
    return web.Response(status=404)

  async with MockHttpServer(mock_stream) as mock:
    validator = StreamValidator(
      cfg,
      catalog.account,
      session_factory=mock.session_factory,
    )
    count = await validate_mapping_candidates(
      doc, lineup, cfg, catalog.account, validator=validator
    )
    assert count == 2
    assert len(probed_urls) == 2
    assert doc.slots[1101].preferred_stream_id == 902
    assert doc.slots[1101].preferred_source == "validated"

    # Re-reconcile on next day with no changes + run
    # validate_mapping_candidates(only_changed=True):
    # should perform ZERO probes because 1101 is validated & unchanged!
    doc_day2 = reconcile_rough_mapping(doc, lineup, catalog, cfg)
    validator2 = StreamValidator(
      cfg,
      catalog.account,
      session_factory=mock.session_factory,
    )
    count2 = await validate_mapping_candidates(
      doc_day2,
      lineup,
      cfg,
      catalog.account,
      only_changed=True,
      validator=validator2,
    )
    assert count2 == 0
    assert len(probed_urls) == 2

  # Phase (5): M3U8 uses provider's native epg_channel_id ("ICITeleMontreal.ca")
  # so xmltv.php binds!
  mapped_list = mapping_document_to_mapped_channels(doc_day2, lineup)
  m3u8 = render_m3u8(lineup, mapped_list, cfg)
  assert (
    'url-tvg="http://provider.example/xmltv.php?username=u&password=p"' in m3u8
  )
  assert 'tvg-id="ICITeleMontreal.ca"' in m3u8
  assert 'tvg-chno="1101"' in m3u8
