"""Tests for the pluggable `LineupProvider` interface, `TvGuideLineupProvider`, and `SchedulesDirectLineupProvider`."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from aiohttp import web

from bellhop.config import (
  AppConfig,
  LineupConfig,
  SchedulesDirectConfig,
  StateConfig,
)
from bellhop.lineup_retriever import (
  build_default_providers,
  discover_provider_lineups,
  retrieve_cable_lineup,
  select_lineup_provider,
)
from bellhop.matcher import ChannelMatcher
from bellhop.models import XtreamStream
from bellhop.providers import (
  SchedulesDirectLineupProvider,
  TvGuideLineupProvider,
  YamlLineupProvider,
  hash_sd_password,
)
from tests.http_mock import MockHttpServer


@pytest.mark.asyncio
async def test_tvguide_provider_discovery_retrieval_pairing_and_matching(
  tmp_path: Path,
) -> None:
  def tvguide_handler(req: web.Request) -> web.Response:
    path = req.path
    if path.endswith("/serviceproviders/country/CAN/web"):
      return web.json_response(
        {
          "data": {
            "country": "CAN",
            "items": [
              {
                "id": 9100000607,
                "name": "Bell Fibe TV - Montreal (Digital)",
                "city": "Montreal",
                "state": "QC",
                "type": "cable",
              },
              {
                "id": 9166060733,
                "name": "Videotron - Montreal (Digital)",
                "city": "Montreal",
                "state": "QC",
                "type": "cable",
              },
              {
                "id": 9100009999,
                "name": "Rogers - Toronto (Digital)",
                "city": "Toronto",
                "state": "ON",
                "type": "cable",
              },
            ],
          }
        }
      )
    if path.endswith("/serviceproviders/zipcode/10001/web"):
      return web.json_response(
        {
          "data": {
            "zipcode": "10001",
            "items": [
              {
                "id": 9133001919,
                "name": "Astound - Manhattan (Digital)",
                "city": "New York",
                "state": "NY",
                "type": "cable",
              }
            ],
          }
        }
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
                "number": "101",
                "sourceId": 9200011674,
                "networkName": None,
                "logo": "/provider/8/4/8-cbft.png",
              },
              {
                "fullName": "CBFT Montreal, Quebec (Radio-Canada)",
                "name": "CBFT",
                "number": "1101",
                "sourceId": 9200011674,
                "networkName": None,
                "logo": "/provider/8/4/8-cbft.png",
              },
              {
                "fullName": "Reseau Des Sports Channel 11 (Canada)",
                "name": "RDS",
                "number": "108",
                "sourceId": 9233000418,
                "networkName": None,
                "logo": "/provider/8/4/8-rds.png",
              },
              {
                "fullName": "Reseau Des Sports Channel 11 HD (Canada)",
                "name": "RDSHD",
                "number": "1108",
                "sourceId": 9266072913,
                "networkName": None,
                "logo": "/provider/8/4/8-rds.png",
              },
              {
                "fullName": "RDS2 HD (Canada)",
                "name": "RDS2HD",
                "number": "1109",
                "sourceId": 9233011769,
                "networkName": None,
                "logo": "/provider/8/4/8-rds2.png",
              },
            ]
          }
        }
      )
    return web.Response(status=404)

  async with (
    MockHttpServer(tvguide_handler) as mock,
    mock.new_session() as client,
  ):
    tvg = TvGuideLineupProvider(
      base_url=f"{mock.base_url}/tvschedules/tvguide",
      client=client,
    )

    # 1. Discover Canadian providers narrowed automatically by H2Y Montreal FSA
    ca_headends = await tvg.discover_lineups(
      country="CAN", postal_code="H2Y 1C6"
    )
    assert [h.lineup_id for h in ca_headends] == [
      "TVG-9100000607",
      "TVG-9166060733",
    ]

    # 2. Discover US providers by 5-digit ZIP code
    us_headends = await tvg.discover_lineups(postal_code="10001")
    assert [h.lineup_id for h in us_headends] == ["TVG-9133001919"]

    # 3. Retrieve lineup via Phase (1) `retrieve_cable_lineup` + verify SD/HD pairing & monthly cache
    cfg = AppConfig(
      project_root=tmp_path,
      lineup=LineupConfig(
        backend="tvguide",
        lineup_id="TVG-9100000607",
        postal_code="H2Y",
      ),
      state=StateConfig(state_dir=Path("state")),
    )
    res1 = await retrieve_cable_lineup(cfg, providers=[tvg])
    assert res1.backend == "tvguide"
    assert not res1.from_cache
    assert res1.lineup.provider == "Bell Fibe TV - Montreal (Digital)"
    # 5 raw slots (101, 1101, 108, 1108, 1109) paired into 3 unique networks (total_dial_positions=5)!
    assert len(res1.lineup.channels) == 3
    assert res1.lineup.total_dial_positions == 5

    ch_by_no = {c.chno: c for c in res1.lineup.channels}
    assert ch_by_no[1101].sd_chno == 101
    assert ch_by_no[1101].language == "fr"
    assert ch_by_no[1108].sd_chno == 108
    assert ch_by_no[1108].group == "Sports"
    # Automatic sibling exclusion inferred: RDS (1108) excludes "2" so RDS2 never bleeds into RDS!
    assert (
      "2" in ch_by_no[1108].exclude
      or "rds2" in ch_by_no[1108].exclude
      or any("2" in ex for ex in ch_by_no[1108].exclude)
    )

    # Second call reuses monthly snapshot
    res2 = await retrieve_cable_lineup(cfg, providers=[tvg])
    assert res2.from_cache

    # 4. Verify Phase (3) ChannelMatcher matches raw IPTV streams against dynamic TVGuide channels
    matcher = ChannelMatcher(
      [
        XtreamStream(
          stream_id=1,
          name="CA FR: ICI RADIO-CANADA MONTREAL FHD",
          epg_channel_id="CBFT.ca",
        ),
        XtreamStream(
          stream_id=2, name="CA FR: RDS 2 FHD", epg_channel_id="RDS2.ca"
        ),
        XtreamStream(
          stream_id=3, name="CA FR: RDS FHD", epg_channel_id="RDS.ca"
        ),
      ]
    )
    assert matcher.find_candidates(ch_by_no[1101])[0].stream.stream_id == 1
    assert matcher.find_candidates(ch_by_no[1108])[0].stream.stream_id == 3
    assert matcher.find_candidates(ch_by_no[1109])[0].stream.stream_id == 2


@pytest.mark.asyncio
async def test_schedules_direct_provider_auth_reauth_auto_add_and_retrieval(
  tmp_path: Path,
) -> None:
  expected_sha1 = hashlib.sha1(b"my_sd_password").hexdigest().lower()
  assert hash_sd_password("my_sd_password") == expected_sha1
  # Passing an already-hashed SHA-1 string is idempotent
  assert hash_sd_password(expected_sha1) == expected_sha1

  state = {"token_calls": 0, "added_lineup": False}

  async def sd_handler(req: web.Request) -> web.Response:
    path = req.path
    if path.endswith("/token") and req.method == "POST":
      body = await req.json()
      assert body["username"] == "sd_user"
      assert body["password"] == expected_sha1
      state["token_calls"] += 1
      return web.json_response(
        {
          "code": 0,
          "message": "OK",
          "token": f"tok_{state['token_calls']}",
        }
      )

    tok = req.headers.get("token")
    if tok == "expired_tok":
      return web.json_response(
        {"code": 4003, "message": "INVALID_USER / EXPIRED_TOKEN"},
        status=403,
      )

    if path.endswith("/headends") and req.method == "GET":
      assert req.query.get("country") == "CAN"
      assert req.query.get("postalcode") == "H2Y1C6"
      return web.json_response(
        [
          {
            "headend": "0005993",
            "transport": "Cable",
            "location": "Montreal, QC",
            "lineups": [
              {
                "name": "Bell Fibe TV - Montreal",
                "lineup": "CAN-0005993-X",
                "uri": "/20141201/lineups/CAN-0005993-X",
              }
            ],
          }
        ]
      )

    if path.endswith("/lineups/CAN-0005993-X") and req.method == "PUT":
      state["added_lineup"] = True
      return web.json_response({"code": 0, "response": "OK"})

    if path.endswith("/lineups/CAN-0005993-X") and req.method == "GET":
      if not state["added_lineup"]:
        return web.json_response(
          {
            "code": 2100,
            "message": "Lineup CAN-0005993-X not in user account.",
          },
          status=400,
        )
      return web.json_response(
        {
          "map": [
            {"stationID": "19001", "channel": "108"},
            {"stationID": "64201", "channel": "1108"},
            {"stationID": "55001", "channel": "1401"},
          ],
          "stations": [
            {
              "stationID": "19001",
              "name": "Réseau des sports",
              "callsign": "RDS",
              "broadcastLanguage": ["fr"],
              "logo": {"URL": "https://schedulesdirect.example/logos/rds.png"},
            },
            {
              "stationID": "64201",
              "name": "Réseau des sports HD",
              "callsign": "RDSHD",
              "broadcastLanguage": ["fr"],
              "isHD": True,
              "stationLogo": [
                {"URL": "https://schedulesdirect.example/logos/rds.png"}
              ],
            },
            {
              "stationID": "55001",
              "name": "The Sports Network",
              "callsign": "TSN1",
              "broadcastLanguage": ["en"],
              "isHD": True,
              "logo": {"URL": "https://schedulesdirect.example/logos/tsn1.png"},
            },
          ],
          "metadata": {
            "lineup": "Bell Fibe TV - Montreal",
            "modified": "2026-11-01T00:00:00Z",
            "transport": "Cable",
          },
        }
      )
    return web.Response(status=404)

  token_file = tmp_path / "state" / "sd_token.json"
  # Pre-seed an expired token to verify automatic re-authentication on 4003/403!
  token_file.parent.mkdir(parents=True, exist_ok=True)
  token_file.write_text(
    json.dumps(
      {"username": "sd_user", "token": "expired_tok", "saved_at": 9999999999.0}
    ),
    encoding="utf-8",
  )

  async with MockHttpServer(sd_handler) as mock, mock.new_session() as client:
    sd = SchedulesDirectLineupProvider(
      username="sd_user",
      password="my_sd_password",
      base_url=f"{mock.base_url}/20141201",
      token_cache_path=token_file,
      auto_add_lineup=True,
      client=client,
    )

    cfg = AppConfig(
      project_root=tmp_path,
      lineup=LineupConfig(
        backend="schedules_direct",
        lineup_id="CAN-0005993-X",
        postal_code="H2Y 1C6",
        schedules_direct=SchedulesDirectConfig(
          username="sd_user",
          password="my_sd_password",
          auto_add_lineup=True,
        ),
      ),
      state=StateConfig(state_dir=Path("state")),
    )

    # 1. Headend discovery strips space from H2Y 1C6 and re-authenticates expired token
    discovered = await discover_provider_lineups(
      cfg,
      backend="schedules_direct",
      country="CAN",
      postal_code="H2Y 1C6",
      providers=[sd],
    )
    assert len(discovered) == 1
    assert discovered[0].lineup_id == "CAN-0005993-X"
    assert state["token_calls"] == 1

    # 2. Lineup retrieval auto-subscribes (`PUT /lineups/CAN-0005993-X`) and pairs 108 + 1108
    res = await retrieve_cable_lineup(cfg, providers=[sd])
    assert res.backend == "schedules_direct"
    assert state["added_lineup"] is True
    assert len(res.lineup.channels) == 2
    assert res.lineup.total_dial_positions == 3
    rds_ch = next(c for c in res.lineup.channels if c.chno == 1108)
    assert rds_ch.sd_chno == 108
    assert rds_ch.gracenote_station_id == "64201"
    assert rds_ch.language == "fr"


def test_provider_selection_registry_routing(tmp_path: Path) -> None:
  repo_yaml = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "lineups"
    / "bell_fibe_montreal.yaml"
  )
  cfg = AppConfig(
    project_root=tmp_path,
    lineup=LineupConfig(
      file=repo_yaml,
      schedules_direct=SchedulesDirectConfig(username="u", password="p"),
    ),
  )
  providers = build_default_providers(cfg)

  assert isinstance(
    select_lineup_provider(
      "TVG-9100000607", backend_hint="auto", providers=providers
    ),
    TvGuideLineupProvider,
  )
  assert isinstance(
    select_lineup_provider(
      "CAN-BELLFIBE-MTL-X", backend_hint="auto", providers=providers
    ),
    YamlLineupProvider,
  )
  assert isinstance(
    select_lineup_provider(
      "CAN-0005993-X", backend_hint="schedules_direct", providers=providers
    ),
    SchedulesDirectLineupProvider,
  )
  assert isinstance(
    select_lineup_provider(
      "SD:USA-NY10001-X", backend_hint="auto", providers=providers
    ),
    SchedulesDirectLineupProvider,
  )
