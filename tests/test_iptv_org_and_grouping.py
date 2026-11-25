"""Tests for `iptv-org` ETag conditional caching, `closed`/`replaced_by`, and 2D MDL dial grouping."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from bellhop.config import (
  AppConfig,
  IptvOrgConfig,
  LineupConfig,
  StateConfig,
  XtreamConfig,
)
from bellhop.pipeline.dial_grouping import assign_dial_contiguous_groups
from bellhop.pipeline.iptv_org import (
  IptvOrgClient,
  IptvOrgError,
  IptvOrgIndex,
  fold_latin_accents,
)
from bellhop.pipeline.lineup_retriever import retrieve_cable_lineup
from bellhop.pipeline.matcher import ChannelMatcher
from bellhop.pipeline.models import LineupChannel, XtreamStream
from bellhop.pipeline.providers import (
  RawProviderSlot,
  TvGuideLineupProvider,
  normalize_provider_slots,
)
from tests.conftest import FakeClock
from tests.http_mock import MockHttpServer


def _assert_strictly_dial_contiguous(
  channels: list[LineupChannel],
) -> list[str]:
  """Assert `channels` are sorted by `chno` and each `group` forms one contiguous slice."""
  chnos = [c.chno for c in channels]
  assert chnos == sorted(chnos)
  seen_groups: list[str] = []
  seen_set: set[str] = set()
  prev_group: str | None = None
  for ch in channels:
    if ch.group != prev_group:
      assert ch.group not in seen_set, (
        f"Group {ch.group!r} reappeared non-contiguously at chno {ch.chno}"
      )
      seen_set.add(ch.group)
      seen_groups.append(ch.group)
      prev_group = ch.group
  return seen_groups


@pytest.mark.asyncio
async def test_iptv_org_etag_conditional_cache_304_revalidation_and_lineup_regroup(
  tmp_path: Path,
  fake_clock: FakeClock,
) -> None:
  state: dict[str, Any] = {
    "version": 1,
    "channels_hits": 0,
    "feeds_hits": 0,
    "seen_if_none_match": [],
    "seen_if_modified_since": [],
    "fail_network": False,
  }

  channels_v1 = [
    {
      "id": "CBFT.ca",
      "name": "ICI Radio-Canada Télé",
      "alt_names": ["Radio-Canada"],
      "country": "CA",
      "categories": ["general"],
      "is_nsfw": False,
      "closed": None,
      "replaced_by": None,
    },
    {
      "id": "RDS.ca",
      "name": "RDS",
      "alt_names": ["Réseau des sports"],
      "country": "CA",
      "categories": [],  # Initially uncategorized in v1
      "is_nsfw": False,
      "closed": None,
      "replaced_by": None,
    },
    {
      "id": "CNN.us",
      "name": "CNN",
      "alt_names": [],
      "country": "US",
      "categories": ["news"],
      "is_nsfw": False,
      "closed": None,
      "replaced_by": None,
    },
    {
      "id": "Crave1.ca",
      "name": "Crave 1",
      "alt_names": ["Crave1"],
      "country": "CA",
      "categories": [],  # V1 missing genre; v2 adds "movies"
      "is_nsfw": False,
      "closed": None,
      "replaced_by": None,
    },
    {
      "id": "MysteryBox.ca",
      "name": "Mystery Box",
      "alt_names": [],
      "country": "CA",
      "categories": ["general"],  # V2 reclassifies as "movies"
      "is_nsfw": False,
      "closed": None,
      "replaced_by": None,
    },
  ]

  channels_v2 = [
    *channels_v1[:3],
    {
      **channels_v1[3],
      "categories": ["movies"],
    },
    {
      **channels_v1[4],
      "categories": ["movies"],
      "alt_names": ["Mystery Cinema"],
    },
  ]

  feeds_v1 = [
    {
      "channel": "CBFT.ca",
      "id": "Montreal",
      "is_main": True,
      "languages": ["fra"],
    },
    {"channel": "RDS.ca", "id": "Main", "is_main": True, "languages": ["fra"]},
    {"channel": "CNN.us", "id": "Main", "is_main": True, "languages": ["eng"]},
    {
      "channel": "Crave1.ca",
      "id": "Main",
      "is_main": True,
      "languages": ["eng"],
    },
    {
      "channel": "MysteryBox.ca",
      "id": "Main",
      "is_main": True,
      "languages": ["eng"],
    },
  ]

  def mock_handler(req: web.Request) -> web.Response:
    path = req.path
    if state["fail_network"] and (
      "/channels.json" in path or "/feeds.json" in path
    ):
      return web.Response(status=503, text="Upstream Unavailable")

    if path.endswith("/channels.json"):
      state["channels_hits"] += 1
      inm = req.headers.get("If-None-Match")
      ims = req.headers.get("If-Modified-Since")
      if inm:
        state["seen_if_none_match"].append(inm)
      if ims:
        state["seen_if_modified_since"].append(ims)
      etag = f'"ch-etag-v{state["version"]}"'
      last_mod = (
        "Mon, 16 Nov 2026 00:00:00 GMT"
        if state["version"] == 1
        else "Tue, 17 Nov 2026 00:00:00 GMT"
      )
      if inm == etag:
        return web.Response(
          status=304,
          headers={"ETag": etag, "Last-Modified": last_mod},
        )
      payload = channels_v1 if state["version"] == 1 else channels_v2
      return web.json_response(
        payload,
        headers={
          "ETag": etag,
          "Last-Modified": last_mod,
          "Cache-Control": "public, max-age=3600",
        },
      )

    if path.endswith("/feeds.json"):
      state["feeds_hits"] += 1
      inm = req.headers.get("If-None-Match")
      etag = '"fd-etag-v1"'
      last_mod = "Mon, 16 Nov 2026 00:00:00 GMT"
      if inm == etag:
        return web.Response(
          status=304,
          headers={"ETag": etag, "Last-Modified": last_mod},
        )
      return web.json_response(
        feeds_v1,
        headers={"ETag": etag, "Last-Modified": last_mod},
      )

    if path.endswith("/serviceprovider/9100000607/web"):
      return web.json_response(
        {
          "data": {
            "item": {
              "id": 9100000607,
              "name": "Bell Fibe TV - Montreal (Digital)",
              "city": "Montreal",
              "state": "QC",
              "type": "cable",
            }
          }
        }
      )

    if path.endswith("/serviceprovider/9100000607/sources/web"):
      return web.json_response(
        {
          "data": {
            "items": [
              {
                "fullName": "CBFT Montreal, Quebec (Radio-Canada)",
                "name": "CBFT",
                "number": "1101",
              },
              {"fullName": "RDS HD", "name": "RDSHD", "number": "1108"},
              {"fullName": "Crave1 HD", "name": "CRAVE1", "number": "1300"},
              {
                "fullName": "Mystery Box HD",
                "name": "MYSBOX",
                "number": "1301",
              },
              {"fullName": "CNN HD", "name": "CNNHD", "number": "1500"},
            ]
          }
        }
      )

    return web.Response(status=404)

  async with (
    MockHttpServer(mock_handler) as mock,
    mock.new_session() as session,
  ):
    cache_dir = tmp_path / "cache"
    iptv_client = IptvOrgClient(
      cache_dir=cache_dir,
      base_url=f"{mock.base_url}/iptv-org",
      recheck_hours=24.0,
      client=session,
      clock=fake_clock,
    )

    # 1. Dry-run fetches in memory without writing cache files
    dry_res = await iptv_client.fetch_index(dry_run=True)
    assert dry_res.channels_status == "fetched_200"
    assert not (cache_dir / "iptv_org" / "http_cache_meta.json").exists()

    # 2. Initial real fetch stores ETag, Last-Modified, and SHA-256 on disk
    res1 = await iptv_client.fetch_index()
    assert res1.updated is True
    assert res1.from_cache is False
    assert res1.channels_status == "fetched_200"
    assert res1.metadata["channels.json"].etag == '"ch-etag-v1"'
    assert (
      res1.metadata["channels.json"].last_modified
      == "Mon, 16 Nov 2026 00:00:00 GMT"
    )
    assert (cache_dir / "iptv_org" / "http_cache_meta.json").is_file()
    hits_after_initial = state["channels_hits"]

    # 3. Within 24h `recheck_hours`, skips network (`fresh_local`)
    fake_clock.advance(3600.0)
    res_local = await iptv_client.fetch_index()
    assert res_local.channels_status == "fresh_local"
    assert res_local.updated is False
    assert res_local.from_cache is True
    assert state["channels_hits"] == hits_after_initial

    # 4. Past `recheck_hours` (25h), sends `If-None-Match` + `If-Modified-Since` -> 304 Not Modified!
    fake_clock.advance(25.0 * 3600.0)
    res_304 = await iptv_client.fetch_index()
    assert res_304.channels_status == "not_modified_304"
    assert res_304.feeds_status == "not_modified_304"
    assert res_304.updated is False
    assert res_304.from_cache is True
    assert '"ch-etag-v1"' in state["seen_if_none_match"]
    assert "Mon, 16 Nov 2026 00:00:00 GMT" in state["seen_if_modified_since"]

    # 5. Wire into Phase (1) `retrieve_cable_lineup`: initial snapshot saved, then upstream ETag rotates!
    tvg = TvGuideLineupProvider(
      base_url=f"{mock.base_url}/tvschedules/tvguide",
      client=session,
    )
    cfg = AppConfig(
      project_root=tmp_path,
      xtream=XtreamConfig(cache_dir=Path("cache")),
      lineup=LineupConfig(
        backend="tvguide",
        lineup_id="TVG-9100000607",
        postal_code="H2Y",
        refresh_days=30.0,
        iptv_org=IptvOrgConfig(
          enabled=True,
          base_url=f"{mock.base_url}/iptv-org",
          recheck_hours=24.0,
        ),
      ),
      state=StateConfig(state_dir=Path("state")),
    )

    lu_res1 = await retrieve_cable_lineup(
      cfg, providers=[tvg], iptv_org_client=iptv_client
    )
    assert lu_res1.from_cache is False
    assert lu_res1.lineup.metadata.get("iptv_org_enriched") is True

    # Cached call with unchanged ETag returns `from_cache=True`
    lu_cached = await retrieve_cable_lineup(
      cfg, providers=[tvg], iptv_org_client=iptv_client
    )
    assert lu_cached.from_cache is True

    # Rotate upstream `channels.json` to v2 (`MysteryBox.ca` gains `movies` + `"Mystery Cinema"` alias)
    state["version"] = 2
    fake_clock.advance(25.0 * 3600.0)
    lu_res2 = await retrieve_cable_lineup(
      cfg, providers=[tvg], iptv_org_client=iptv_client
    )
    assert lu_res2.from_cache is False
    assert 1301 in lu_res2.diff.modified_chnos
    ch_1301 = next(c for c in lu_res2.lineup.channels if c.chno == 1301)
    assert "Mystery Cinema" in ch_1301.aliases
    assert "Movies" in ch_1301.group

    # 6. Upstream 503 outage falls back cleanly to cached disk files (`stale_cache_fallback`)
    state["fail_network"] = True
    fake_clock.advance(25.0 * 3600.0)
    res_fallback = await iptv_client.fetch_index()
    assert res_fallback.channels_status == "stale_cache_fallback"
    assert res_fallback.index.station_count == 5

    # 7. Uncached client with `allow_offline_empty=False` raises `IptvOrgError` on outage
    empty_client = IptvOrgClient(
      cache_dir=tmp_path / "empty_cache",
      base_url=f"{mock.base_url}/iptv-org",
      client=session,
      clock=fake_clock,
    )
    with pytest.raises(IptvOrgError):
      await empty_client.fetch_index(allow_offline_empty=False)


def test_closed_and_replaced_by_lifecycle_resolution() -> None:
  idx = IptvOrgIndex.from_json_payloads(
    channels_raw=[
      # Permanently shut down with no successor (`Telemagino`, `Yoopa`)
      {
        "id": "Telemagino.ca",
        "name": "Télémagino",
        "alt_names": ["Telemagino"],
        "country": "CA",
        "categories": ["kids"],
        "closed": "2023-05-31",
        "replaced_by": None,
      },
      {
        "id": "Yoopa.ca",
        "name": "Yoopa",
        "alt_names": [],
        "country": "CA",
        "categories": ["kids"],
        "closed": "2024-01-11",
        "replaced_by": None,
      },
      # Multi-step / `@FeedId` rebrand chain: Discovery -> USANetwork.ca@East -> active USANetwork.ca
      {
        "id": "DiscoveryChannel.ca",
        "name": "Discovery Channel",
        "alt_names": ["Discovery Canada"],
        "country": "CA",
        "categories": ["science"],
        "closed": "2025-01-01",
        "replaced_by": "USANetwork.ca@East",
      },
      {
        "id": "USANetwork.ca",
        "name": "USA Network",
        "alt_names": ["USA Network Canada"],
        "country": "CA",
        "categories": ["series"],
        "closed": None,
        "replaced_by": None,
      },
      {
        "id": "DisneyChannel.ca",
        "name": "Disney Channel",
        "alt_names": ["Disney"],
        "country": "CA",
        "categories": ["kids"],
        "closed": None,
        "replaced_by": None,
      },
    ],
    feeds_raw=[
      {
        "channel": "USANetwork.ca",
        "id": "East",
        "is_main": True,
        "languages": ["eng"],
      },
      {
        "channel": "DisneyChannel.ca",
        "id": "Main",
        "is_main": True,
        "languages": ["eng"],
      },
    ],
  )

  # 1. Defunct unreplaced aliases (`Telemagino`, `Yoopa`) are stripped while primary/active survive
  assert idx.is_permanently_closed_alias("Telemagino", country="CA") is True
  assert idx.is_permanently_closed_alias("Yoopa", country="CA") is True
  assert (
    idx.is_permanently_closed_alias("Disney Channel", country="CA") is False
  )
  assert idx.is_permanently_closed_id("Yoopa.ca") is True

  channels = [
    LineupChannel(
      chno=1140,
      name="Disney Channel",
      group="Old",
      language="en",
      tvg_id="Telemagino.ca",  # Stale defunct tvg_id gets replaced!
      aliases=["Disney Channel", "Telemagino", "Yoopa"],
    ),
    LineupChannel(
      chno=1520,
      name="Discovery Channel",
      group="Old",
      language="en",
      tvg_id="DiscoveryChannel.ca",
      aliases=["Discovery Channel"],
    ),
  ]
  assign_dial_contiguous_groups(
    channels, iptv_org_index=idx, market="Montreal, QC", country="CAN"
  )

  disney = channels[0]
  assert "Telemagino" not in disney.aliases
  assert "Yoopa" not in disney.aliases
  assert "Disney Channel" in disney.aliases
  assert disney.tvg_id == "DisneyChannel.ca"

  # 2. Stale "Discovery Channel" slot resolves `replaced_by: USANetwork.ca@East` -> `USANetwork.ca`
  rebranded = channels[1]
  assert rebranded.tvg_id == "USANetwork.ca"
  assert "USA Network" in rebranded.aliases
  assert "USA Network Canada" in rebranded.aliases


def test_2d_language_genre_mdl_segmentation_across_bilingual_intl_and_french_majority() -> (
  None
):
  idx = IptvOrgIndex.from_json_payloads(
    channels_raw=[
      # French sports & kids & movies
      {
        "id": "RDS.ca",
        "name": "RDS",
        "country": "CA",
        "categories": ["sports"],
      },
      {
        "id": "RDS2.ca",
        "name": "RDS 2",
        "country": "CA",
        "categories": ["sports"],
      },
      {
        "id": "RDSInfo.ca",
        "name": "RDS Info",
        "country": "CA",
        "categories": ["sports"],
      },
      {
        "id": "TVASports.ca",
        "name": "TVA Sports",
        "country": "CA",
        "categories": ["sports"],
      },
      {
        "id": "Teletoon.ca",
        "name": "Télétoon",
        "country": "CA",
        "categories": ["kids"],
      },
      {
        "id": "Tiji.fr",
        "name": "Tiji",
        "country": "FR",
        "categories": ["kids"],
      },
      {
        "id": "Gulli.fr",
        "name": "Gulli",
        "country": "FR",
        "categories": ["animation"],
      },
      {
        "id": "SuperEcran1.ca",
        "name": "Super Écran 1",
        "country": "CA",
        "categories": ["movies"],
      },
      {
        "id": "SuperEcran2.ca",
        "name": "Super Écran 2",
        "country": "CA",
        "categories": ["movies"],
      },
      {
        "id": "SuperEcran3.ca",
        "name": "Super Écran 3",
        "country": "CA",
        "categories": ["movies"],
      },
      {
        "id": "Cinepop.ca",
        "name": "Cinépop",
        "country": "CA",
        "categories": ["movies"],
      },
      # International news block (different minority languages, same genre = news!)
      {
        "id": "DW.de",
        "name": "Deutsche Welle",
        "country": "DE",
        "categories": ["news"],
      },
      {
        "id": "AlJazeera.qa",
        "name": "Al Jazeera Arabic",
        "country": "QA",
        "categories": ["news"],
      },
      {
        "id": "NHKWorld.jp",
        "name": "NHK World Japan",
        "country": "JP",
        "categories": ["news"],
      },
      {
        "id": "RTPA.es",
        "name": "Canal 24 Horas",
        "country": "ES",
        "categories": ["news"],
      },
      {
        "id": "RaiNews24.it",
        "name": "Rai News 24",
        "country": "IT",
        "categories": ["news"],
      },
      # English majority blocks (Movies, Sports, Science, Kids, Audio)
      {
        "id": "Crave1.ca",
        "name": "Crave 1",
        "country": "CA",
        "categories": ["movies"],
      },
      {
        "id": "HBO1.ca",
        "name": "HBO 1",
        "country": "CA",
        "categories": ["movies"],
      },
      {
        "id": "Starz1.ca",
        "name": "Starz 1",
        "country": "CA",
        "categories": ["movies"],
      },
      {
        "id": "TCM.us",
        "name": "Turner Classic Movies",
        "country": "US",
        "categories": ["classic"],
      },
      {
        "id": "TSN1.ca",
        "name": "TSN 1",
        "country": "CA",
        "categories": ["sports"],
      },
      {
        "id": "TSN2.ca",
        "name": "TSN 2",
        "country": "CA",
        "categories": ["sports"],
      },
      {
        "id": "SN1.ca",
        "name": "Sportsnet One",
        "country": "CA",
        "categories": ["sports"],
      },
      {
        "id": "ESPN.us",
        "name": "ESPN",
        "country": "US",
        "categories": ["sports"],
      },
      {
        "id": "History.ca",
        "name": "History",
        "country": "CA",
        "categories": ["documentary"],
      },
      {
        "id": "NatGeo.us",
        "name": "National Geographic",
        "country": "US",
        "categories": ["science"],
      },
      {
        "id": "Smithsonian.ca",
        "name": "Smithsonian Channel",
        "country": "CA",
        "categories": ["education"],
      },
      {
        "id": "BBCEarth.ca",
        "name": "BBC Earth",
        "country": "CA",
        "categories": ["documentary"],
      },
      {"id": "YTV.ca", "name": "YTV", "country": "CA", "categories": ["kids"]},
      {
        "id": "Treehouse.ca",
        "name": "Treehouse",
        "country": "CA",
        "categories": ["kids"],
      },
      {
        "id": "CartoonNetwork.ca",
        "name": "Cartoon Network",
        "country": "CA",
        "categories": ["animation"],
      },
      {
        "id": "DisneyChannel.ca",
        "name": "Disney Channel",
        "country": "CA",
        "categories": ["kids"],
      },
    ],
    feeds_raw=[
      {"channel": "RDS.ca", "id": "M", "is_main": True, "languages": ["fra"]},
      {"channel": "RDS2.ca", "id": "M", "is_main": True, "languages": ["fra"]},
      {
        "channel": "RDSInfo.ca",
        "id": "M",
        "is_main": True,
        "languages": ["fra"],
      },
      {
        "channel": "TVASports.ca",
        "id": "M",
        "is_main": True,
        "languages": ["fra"],
      },
      {
        "channel": "Teletoon.ca",
        "id": "M",
        "is_main": True,
        "languages": ["fra"],
      },
      {"channel": "Tiji.fr", "id": "M", "is_main": True, "languages": ["fra"]},
      {"channel": "Gulli.fr", "id": "M", "is_main": True, "languages": ["fra"]},
      {
        "channel": "SuperEcran1.ca",
        "id": "M",
        "is_main": True,
        "languages": ["fra"],
      },
      {
        "channel": "SuperEcran2.ca",
        "id": "M",
        "is_main": True,
        "languages": ["fra"],
      },
      {
        "channel": "SuperEcran3.ca",
        "id": "M",
        "is_main": True,
        "languages": ["fra"],
      },
      {
        "channel": "Cinepop.ca",
        "id": "M",
        "is_main": True,
        "languages": ["fra"],
      },
      {"channel": "DW.de", "id": "M", "is_main": True, "languages": ["deu"]},
      {
        "channel": "AlJazeera.qa",
        "id": "M",
        "is_main": True,
        "languages": ["ara"],
      },
      {
        "channel": "NHKWorld.jp",
        "id": "M",
        "is_main": True,
        "languages": ["jpn"],
      },
      {"channel": "RTPA.es", "id": "M", "is_main": True, "languages": ["spa"]},
      {
        "channel": "RaiNews24.it",
        "id": "M",
        "is_main": True,
        "languages": ["ita"],
      },
      {
        "channel": "Crave1.ca",
        "id": "M",
        "is_main": True,
        "languages": ["eng"],
      },
      {"channel": "HBO1.ca", "id": "M", "is_main": True, "languages": ["eng"]},
      {
        "channel": "Starz1.ca",
        "id": "M",
        "is_main": True,
        "languages": ["eng"],
      },
      {"channel": "TCM.us", "id": "M", "is_main": True, "languages": ["eng"]},
      {"channel": "TSN1.ca", "id": "M", "is_main": True, "languages": ["eng"]},
      {"channel": "TSN2.ca", "id": "M", "is_main": True, "languages": ["eng"]},
      {"channel": "SN1.ca", "id": "M", "is_main": True, "languages": ["eng"]},
      {"channel": "ESPN.us", "id": "M", "is_main": True, "languages": ["eng"]},
      {
        "channel": "History.ca",
        "id": "M",
        "is_main": True,
        "languages": ["eng"],
      },
      {
        "channel": "NatGeo.us",
        "id": "M",
        "is_main": True,
        "languages": ["eng"],
      },
      {
        "channel": "Smithsonian.ca",
        "id": "M",
        "is_main": True,
        "languages": ["eng"],
      },
      {
        "channel": "BBCEarth.ca",
        "id": "M",
        "is_main": True,
        "languages": ["eng"],
      },
      {"channel": "YTV.ca", "id": "M", "is_main": True, "languages": ["eng"]},
      {
        "channel": "Treehouse.ca",
        "id": "M",
        "is_main": True,
        "languages": ["eng"],
      },
      {
        "channel": "CartoonNetwork.ca",
        "id": "M",
        "is_main": True,
        "languages": ["eng"],
      },
      {
        "channel": "DisneyChannel.ca",
        "id": "M",
        "is_main": True,
        "languages": ["eng"],
      },
    ],
  )

  raw_slots = [
    # 700s: International News run across 5 languages -> "International — News & Info"
    RawProviderSlot(chno=701, raw_name="Deutsche Welle", country="CAN"),
    RawProviderSlot(chno=702, raw_name="Al Jazeera Arabic", country="CAN"),
    RawProviderSlot(chno=703, raw_name="NHK World Japan", country="CAN"),
    RawProviderSlot(chno=704, raw_name="Canal 24 Horas", country="CAN"),
    RawProviderSlot(chno=705, raw_name="Rai News 24", country="CAN"),
    # 900s: Music & Audio block #1
    RawProviderSlot(chno=901, raw_name="Stingray Rock", country="CAN"),
    RawProviderSlot(chno=902, raw_name="Stingray Jazz", country="CAN"),
    RawProviderSlot(chno=903, raw_name="Stingray Classical", country="CAN"),
    RawProviderSlot(chno=904, raw_name="Stingray Pop", country="CAN"),
    # 1100s: French Sports, French Kids, French Movies
    RawProviderSlot(chno=1108, raw_name="RDS HD", country="CAN"),
    RawProviderSlot(chno=1109, raw_name="RDS 2 HD", country="CAN"),
    RawProviderSlot(chno=1110, raw_name="RDS Info HD", country="CAN"),
    RawProviderSlot(chno=1111, raw_name="TVA Sports HD", country="CAN"),
    RawProviderSlot(
      chno=1150, raw_name="Télétoon", language="fr", country="CAN"
    ),
    RawProviderSlot(chno=1151, raw_name="Tiji", language="fr", country="CAN"),
    RawProviderSlot(chno=1152, raw_name="Gulli", language="fr", country="CAN"),
    RawProviderSlot(chno=1161, raw_name="Super Écran 1", country="CAN"),
    RawProviderSlot(chno=1162, raw_name="Super Écran 2", country="CAN"),
    RawProviderSlot(chno=1163, raw_name="Super Écran 3", country="CAN"),
    RawProviderSlot(chno=1164, raw_name="Cinépop", country="CAN"),
    # 1300s–1550s: English majority blocks + unknown slot absorbed cleanly into Sports
    RawProviderSlot(chno=1300, raw_name="Crave 1", country="CAN"),
    RawProviderSlot(chno=1301, raw_name="HBO 1", country="CAN"),
    RawProviderSlot(chno=1302, raw_name="Starz 1", country="CAN"),
    RawProviderSlot(chno=1303, raw_name="Turner Classic Movies", country="CAN"),
    RawProviderSlot(chno=1400, raw_name="TSN 1", country="CAN"),
    RawProviderSlot(chno=1401, raw_name="TSN 2", country="CAN"),
    RawProviderSlot(chno=1402, raw_name="Mystery Arena Feed", country="CAN"),
    RawProviderSlot(chno=1403, raw_name="Sportsnet One", country="CAN"),
    RawProviderSlot(chno=1404, raw_name="ESPN", country="CAN"),
    RawProviderSlot(chno=1520, raw_name="History", country="CAN"),
    RawProviderSlot(chno=1521, raw_name="National Geographic", country="CAN"),
    RawProviderSlot(chno=1522, raw_name="Smithsonian Channel", country="CAN"),
    RawProviderSlot(chno=1523, raw_name="BBC Earth", country="CAN"),
    RawProviderSlot(chno=1550, raw_name="YTV", country="CAN"),
    RawProviderSlot(chno=1551, raw_name="Treehouse", country="CAN"),
    RawProviderSlot(chno=1552, raw_name="Cartoon Network", country="CAN"),
    RawProviderSlot(chno=1553, raw_name="Disney Channel", country="CAN"),
    # 1640s: Second non-adjacent Music & Audio block -> disambiguated by dial range!
    RawProviderSlot(chno=1640, raw_name="Stingray Loud", country="CAN"),
    RawProviderSlot(chno=1641, raw_name="Stingray Retro", country="CAN"),
    RawProviderSlot(chno=1642, raw_name="Stingray Vibe", country="CAN"),
    RawProviderSlot(chno=1643, raw_name="Stingray Country", country="CAN"),
  ]

  normalized = normalize_provider_slots(
    raw_slots,
    postal_code="H2Y",
    market="Montreal, QC",
    provider_name="Bell Fibe TV",
    lineup_id="TVG-9100000607",
    iptv_org_index=idx,
  )
  ordered_groups = _assert_strictly_dial_contiguous(normalized)
  by_chno = {c.chno: c for c in normalized}

  assert by_chno[701].group == "International — News & Info (701–705)"
  assert by_chno[1108].group == "French — Sports (1108–1111)"
  assert by_chno[1150].group == "French — Kids & Family (1150–1152)"
  assert by_chno[1161].group == "French — Movies (1161–1164)"
  assert by_chno[1300].group == "Movies (1300–1303)"
  # Unknown channel 1402 ("Mystery Arena Feed") absorbed into surrounding Sports run!
  assert by_chno[1402].group == "Sports (1400–1404)"
  assert by_chno[1520].group == "Science & Docs (1520–1523)"
  assert by_chno[1550].group == "Kids & Family (1550–1553)"
  # Two non-adjacent Music & Audio blocks get distinct dial-range titles
  assert by_chno[901].group == "Music & Audio (901–904)"
  assert by_chno[1640].group == "Music & Audio (1640–1643)"
  assert len(ordered_groups) == len(set(ordered_groups))

  # Flip market majority: French-majority lineup (no Quebec hardcoding!) ->
  # French becomes default ("Sports", "Movies") and minority English gets "English — ..."!
  fr_majority_slots = [
    RawProviderSlot(chno=101, raw_name="RDS HD", country="CAN"),
    RawProviderSlot(chno=102, raw_name="RDS 2 HD", country="CAN"),
    RawProviderSlot(chno=103, raw_name="RDS Info HD", country="CAN"),
    RawProviderSlot(chno=104, raw_name="TVA Sports HD", country="CAN"),
    RawProviderSlot(chno=201, raw_name="Super Écran 1", country="CAN"),
    RawProviderSlot(chno=202, raw_name="Super Écran 2", country="CAN"),
    RawProviderSlot(chno=203, raw_name="Super Écran 3", country="CAN"),
    RawProviderSlot(chno=204, raw_name="Cinépop", country="CAN"),
    RawProviderSlot(chno=601, raw_name="TSN 1", country="CAN"),
    RawProviderSlot(chno=602, raw_name="TSN 2", country="CAN"),
    RawProviderSlot(chno=603, raw_name="Sportsnet One", country="CAN"),
  ]
  fr_norm = normalize_provider_slots(
    fr_majority_slots,
    postal_code="G1R",
    market="Quebec City, QC",
    provider_name="Videotron",
    lineup_id="TVG-9166060733",
    iptv_org_index=idx,
  )
  _assert_strictly_dial_contiguous(fr_norm)
  fr_by_no = {c.chno: c for c in fr_norm}
  assert fr_by_no[101].group == "Sports (101–104)"
  assert fr_by_no[201].group == "Movies (201–204)"
  assert fr_by_no[601].group == "English — Sports (601–603)"


def test_latin_accent_ligature_folding_and_corpus_idf_fuzzy_matching() -> None:
  assert fold_latin_accents("Télé-Québec Français") == "Tele-Quebec Francais"
  assert fold_latin_accents("Cœur & Sœur Æon") == "Coeur & Soeur Aeon"
  assert fold_latin_accents("Straße Øresund Łódź") == "Strasse Oresund Lodz"

  idx = IptvOrgIndex.from_json_payloads(
    channels_raw=[
      {
        "id": "CBFT.ca",
        "name": "ICI Radio-Canada Télé",
        "alt_names": ["ICI Télé", "Radio-Canada"],
        "country": "CA",
        "categories": ["general"],
      },
      {
        "id": "NationalGeographic.ca",
        "name": "National Geographic",
        "alt_names": ["Nat Geo"],
        "country": "CA",
        "categories": ["science", "documentary"],
      },
      {
        "id": "NationalGeographic.fr",
        "name": "National Geographic Channel",
        "alt_names": [],
        "country": "FR",
        "categories": ["documentary"],
      },
      {
        "id": "Teletoon.ca",
        "name": "Télétoon",
        "alt_names": [],
        "country": "CA",
        "categories": ["animation", "kids"],
      },
      {
        "id": "RDS.ca",
        "name": "RDS",
        "alt_names": ["Réseau des sports"],
        "country": "CA",
        "categories": ["sports"],
      },
      {
        "id": "RDS2.ca",
        "name": "RDS 2",
        "alt_names": [],
        "country": "CA",
        "categories": ["sports"],
      },
      # Extra background stations so generic words ("channel", "tv") naturally carry lower IDF
      *[
        {
          "id": f"Generic{i}.ca",
          "name": f"Sample {i} Channel TV",
          "alt_names": [],
          "country": "CA",
          "categories": ["entertainment"],
        }
        for i in range(12)
      ],
    ],
    feeds_raw=[
      {"channel": "CBFT.ca", "id": "M", "is_main": True, "languages": ["fra"]},
      {
        "channel": "NationalGeographic.ca",
        "id": "M",
        "is_main": True,
        "languages": ["eng"],
      },
      {
        "channel": "NationalGeographic.fr",
        "id": "M",
        "is_main": True,
        "languages": ["fra"],
      },
      {
        "channel": "Teletoon.ca",
        "id": "M",
        "is_main": True,
        "languages": ["fra"],
      },
      {"channel": "RDS.ca", "id": "M", "is_main": True, "languages": ["fra"]},
      {"channel": "RDS2.ca", "id": "M", "is_main": True, "languages": ["fra"]},
    ],
  )

  # 1. Domestic CA fuzzy match ("National Geographic") beats foreign FR exact ("National Geographic Channel")!
  natgeo_hit = idx.lookup("National Geographic Channel", country="CA")
  assert natgeo_hit is not None
  assert natgeo_hit.channel.channel_id == "NationalGeographic.ca"
  assert natgeo_hit.channel.languages == ("eng",)

  # 2. Enrich slots via `normalize_provider_slots` and match against raw IPTV catalog
  slots = normalize_provider_slots(
    [
      RawProviderSlot(
        chno=1101,
        raw_name="CBFT Montreal, Quebec (Radio-Canada)",
        callsign="CBFT",
        country="CAN",
      ),
      RawProviderSlot(
        chno=153,
        raw_name="Télétoon Français",
        language="fr",
        country="CAN",
      ),
      RawProviderSlot(
        chno=1524,
        raw_name="National Geographic Channel HD",
        country="CAN",
      ),
      RawProviderSlot(
        chno=1108,
        raw_name="RDS HD",
        country="CAN",
      ),
    ],
    postal_code="H2Y",
    market="Montréal, QC",
    provider_name="Bell Fibe TV",
    lineup_id="TVG-9100000607",
    iptv_org_index=idx,
  )
  by_no = {c.chno: c for c in slots}
  assert by_no[1524].language == "en"
  assert "Nat Geo" in by_no[1524].aliases
  assert any("ICI" in a for a in by_no[1101].aliases)

  catalog_streams = [
    XtreamStream(stream_id=101, name="CA: ICI MONTREAL FHD"),
    XtreamStream(stream_id=153, name="CA FR: TELETOON HD"),
    XtreamStream(stream_id=524, name="US: NATIONAL GEOGRAPHIC FHD"),
    XtreamStream(stream_id=525, name="CA: NAT GEO HD"),
    XtreamStream(stream_id=801, name="CA FR: RDS 2 FHD"),
    XtreamStream(stream_id=802, name="CA FR: RDS INFO FHD"),
    *[
      XtreamStream(stream_id=900 + i, name=f"US: SAMPLE {i} CHANNEL HD")
      for i in range(15)
    ],
  ]
  matcher = ChannelMatcher(catalog_streams)

  c1101 = matcher.find_candidates(by_no[1101])
  assert c1101 and c1101[0].stream.stream_id == 101

  c153 = matcher.find_candidates(by_no[153])
  assert c153 and c153[0].stream.stream_id == 153

  c1524 = matcher.find_candidates(by_no[1524])
  matched_1524_ids = {c.stream.stream_id for c in c1524}
  assert {524, 525} <= matched_1524_ids

  # Also verify direct fuzzy matching WITHOUT alias expansion ("National Geographic Channel" -> "NATIONAL GEOGRAPHIC")
  bare_natgeo = LineupChannel(
    chno=1524,
    name="National Geographic Channel",
    group="Science & Docs",
    language="en",
    regions=["ca", "us"],
    aliases=["National Geographic Channel"],
  )
  bare_cands = matcher.find_candidates(bare_natgeo)
  assert any(c.stream.stream_id == 524 for c in bare_cands)

  # Digit & spin-off safety: "RDS" must NOT fuzzily match "RDS 2" or "RDS INFO"
  rds_cands = matcher.find_candidates(by_no[1108])
  assert rds_cands == []

  # Corporate parent `network` field must NOT collide with station names (`Cinemax` has `network="HBO"`)
  hbo_idx = IptvOrgIndex.from_json_payloads(
    channels_raw=[
      {
        "id": "Cinemax.us",
        "name": "Cinemax",
        "alt_names": [],
        "network": "HBO",
        "country": "US",
        "categories": ["movies"],
      },
      {
        "id": "HBO.us",
        "name": "HBO",
        "alt_names": ["Home Box Office"],
        "network": None,
        "country": "US",
        "categories": ["movies"],
      },
      {
        "id": "StoriesbyAMC.us",
        "name": "Stories by AMC",
        "alt_names": [],
        "network": "AMC",
        "country": "US",
        "categories": ["entertainment"],
      },
      {
        "id": "AMC.us",
        "name": "AMC",
        "alt_names": [],
        "network": None,
        "country": "US",
        "categories": ["movies"],
      },
    ],
    feeds_raw=[],
  )
  hbo_match = hbo_idx.lookup("HBO", callsign="HBOCAN", country="CA")
  assert hbo_match is not None
  assert hbo_match.channel.channel_id == "HBO.us"
  amc_match = hbo_idx.lookup("AMC", callsign="AMC", country="CA")
  assert amc_match is not None
  assert amc_match.channel.channel_id == "AMC.us"
