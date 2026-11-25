"""Tests for Schedules Direct SD-JSON EPG priority, MD5 caching, and XMLTV backfill."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from bellhop.config import (
  AppConfig,
  EpgConfig,
  LineupConfig,
  OutputConfig,
  SchedulesDirectConfig,
  ServeConfig,
  ServeWebConfig,
  ServeXtreamConfig,
  XtreamConfig,
)
from bellhop.epg.db import EpgDatabase
from bellhop.epg.refresh import EpgRefreshConfig, EpgRefreshService
from bellhop.epg.sd_epg import SchedulesDirectEpgConfig, hash_sd_password
from bellhop.serve import UnifiedServeServer
from bellhop.web.store import LineupGuideStore
from tests.http_mock import MockHttpServer, new_test_session


def _iso_utc(ts: int) -> str:
  return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
    "%Y-%m-%dT%H:%M:%SZ"
  )


def _xmltv_utc(ts: int) -> str:
  return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
    "%Y%m%d%H%M%S +0000"
  )


def _date_utc(ts: int) -> str:
  return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_sd_epg_priority_xmltv_backfill_md5_cache_and_serve_xmltv(
  tmp_path: Path,
) -> None:
  """Verify SD-JSON EPG takes priority, XMLTV backfills gaps, MD5 skips unchanged fetches, and `/xmltv.php` serves merged XMLTV."""
  now_ts = 1_795_530_000  # 2026-11-24T14:20:00Z
  today_str = _date_utc(now_ts)

  xt_dir = tmp_path / "output" / "xtream"
  xt_dir.mkdir(parents=True, exist_ok=True)
  cache_dir = tmp_path / "cache"

  # Track API calls to verify differential MD5 caching & token refresh
  counts = {
    "token": 0,
    "status": 0,
    "headends": 0,
    "put_lineup": 0,
    "get_lineup": 0,
    "schedules_md5": 0,
    "schedules": 0,
    "programs": 0,
    "xmltv": 0,
  }
  subscribed_lineups: list[dict[str, str]] = []
  force_expired_once = {"armed": False}

  async def _sd_token(request: web.Request) -> web.Response:
    counts["token"] += 1
    body = await request.json()
    if body.get("username") != "chhamilton" or body.get(
      "password"
    ) != hash_sd_password("sd_secret_pw"):
      return web.json_response(
        {"code": 4003, "message": "Invalid credentials"}, status=403
      )
    return web.json_response(
      {"code": 0, "token": f"tok-{counts['token']}", "message": "OK"}
    )

  def _check_tok(request: web.Request) -> web.Response | None:
    tok = request.headers.get("token", "")
    if not tok.startswith("tok-"):
      return web.json_response(
        {"code": 4003, "message": "Bad token"}, status=403
      )
    if force_expired_once["armed"]:
      force_expired_once["armed"] = False
      return web.json_response(
        {"code": 4003, "message": "Expired token"}, status=200
      )
    return None

  async def _sd_status(request: web.Request) -> web.Response:
    bad = _check_tok(request)
    if bad is not None:
      return bad
    counts["status"] += 1
    return web.json_response(
      {
        "code": 0,
        "account": {"expires": "2026-11-30T21:44:13Z", "maxLineups": 4},
        "lineups": list(subscribed_lineups),
      }
    )

  async def _sd_headends(request: web.Request) -> web.Response:
    bad = _check_tok(request)
    if bad is not None:
      return bad
    counts["headends"] += 1
    assert request.query.get("country") == "CAN"
    assert request.query.get("postalcode") == "H2Y1C6"
    return web.json_response(
      [
        {
          "headend": "0004980",
          "transport": "Cable",
          "location": "Montréal",
          "lineups": [
            {"name": "Bell Fibe TV", "lineup": "CAN-0004980-X"},
          ],
        }
      ]
    )

  async def _sd_lineup(request: web.Request, lid: str) -> web.Response:
    bad = _check_tok(request)
    if bad is not None:
      return bad
    if request.method == "PUT":
      counts["put_lineup"] += 1
      subscribed_lineups.append(
        {"lineup": lid, "modified": "2026-11-23T21:02:41Z"}
      )
      return web.json_response({"code": 0, "response": "OK"})

    counts["get_lineup"] += 1
    return web.json_response(
      {
        "metadata": {
          "lineup": "Bell Fibe TV",
          "location": "Montréal",
          "transport": "Cable",
          "modified": "2026-11-23T21:02:41Z",
        },
        "stations": [
          {
            "stationID": "91109",
            "name": "AMI-télé",
            "callsign": "AMITELE",
            "broadcastLanguage": ["fr-CA"],
          },
          {
            "stationID": "19548",
            "name": "HBO Canada",
            "callsign": "HBOCAN",
            "broadcastLanguage": ["en"],
          },
          {
            "stationID": "11912",
            "name": "WPTZ-DT",
            "callsign": "WPTZDT",
            "broadcastLanguage": ["en"],
          },
          {
            "stationID": "10518",
            "name": "KING-DT",
            "callsign": "KINGDT",
            "broadcastLanguage": ["en"],
          },
        ],
        "map": [
          {"stationID": "91109", "channel": "50"},
          {"stationID": "19548", "channel": "1306"},
          {"stationID": "11912", "channel": "220"},
          {"stationID": "10518", "channel": "1285"},
        ],
      }
    )

  station_airings: dict[str, tuple[str, str, str]] = {
    "91109": ("md5-sched-ami", "EP046291270012", "md5-prog-ami"),
    "19548": ("md5-sched-hbo", "MV012345670000", "md5-prog-hbo"),
    "11912": ("md5-sched-wptz", "SH001111110000", "md5-prog-wptz"),
    "10518": ("md5-sched-king", "SP002222220000", "md5-prog-king"),
  }

  async def _sd_schedules_md5(request: web.Request) -> web.Response:
    bad = _check_tok(request)
    if bad is not None:
      return bad
    counts["schedules_md5"] += 1
    body = await request.json()
    resp_obj: dict[str, Any] = {}
    for entry in body:
      sid = entry["stationID"]
      if sid in station_airings:
        s_md5 = station_airings[sid][0]
        req_dates = entry.get("date") or entry.get("dates") or [today_str]
        resp_obj[sid] = {
          dt: {"code": 0, "md5": f"{s_md5}-{dt}"} for dt in req_dates
        }
    return web.json_response(resp_obj)

  async def _sd_schedules(request: web.Request) -> web.Response:
    bad = _check_tok(request)
    if bad is not None:
      return bad
    counts["schedules"] += 1
    body = await request.json()
    out: list[dict[str, Any]] = []
    for entry in body:
      sid = entry["stationID"]
      if sid not in station_airings:
        out.append({"stationID": sid, "code": 7020, "message": "Not available"})
        continue
      s_md5, pid, p_md5 = station_airings[sid]
      req_dates = entry.get("date") or entry.get("dates") or [today_str]
      for idx, dt in enumerate(req_dates):
        slot_start = now_ts - 600 + (idx * 86400)
        out.append(
          {
            "stationID": sid,
            "metadata": {"startDate": dt, "md5": f"{s_md5}-{dt}"},
            "programs": [
              {
                "programID": pid,
                "airDateTime": _iso_utc(slot_start),
                "duration": 3600,
                "md5": p_md5,
                "liveTapeDelay": "Live" if sid == "10518" else "Tape",
              }
            ],
          }
        )
    return web.json_response(out)

  programs_db: dict[str, dict[str, Any]] = {
    "EP046291270012": {
      "programID": "EP046291270012",
      "md5": "md5-prog-ami",
      "titles": [{"title120": "Et si c'était toi?"}],
      "episodeTitle150": "Rencontres d'automne",
      "metadata": [{"Gracenote": {"season": 2, "episode": 4}}],
      "descriptions": {
        "description1000": [
          {"description": "Émission documentaire sur AMI-télé."}
        ]
      },
      "genres": ["Reality"],
    },
    "MV012345670000": {
      "programID": "MV012345670000",
      "md5": "md5-prog-hbo",
      "titles": [{"title120": "Dune: Part Two"}],
      "movie": {"year": "2024"},
      "descriptions": {
        "description100": [{"description": "Authoritative SD HBO feature."}]
      },
      "entityType": "Movie",
    },
    "SH001111110000": {
      "programID": "SH001111110000",
      "md5": "md5-prog-wptz",
      "titles": [{"title120": "NBC 5 News at Noon"}],
      "descriptions": {
        "description100": [
          {"description": "Plattsburgh-Burlington local news."}
        ]
      },
      "genres": ["News"],
    },
    "SP002222220000": {
      "programID": "SP002222220000",
      "md5": "md5-prog-king",
      "titles": [{"title120": "Seahawks Pre-Game"}],
      "eventDetails": {"teams": [{"name": "49ers"}, {"name": "Seahawks"}]},
      "genres": ["Football"],
    },
  }

  async def _sd_programs(request: web.Request) -> web.Response:
    bad = _check_tok(request)
    if bad is not None:
      return bad
    counts["programs"] += 1
    pids = await request.json()
    return web.json_response(
      [programs_db[pid] for pid in pids if pid in programs_db]
    )

  async def _upstream_xmltv(_request: web.Request) -> web.Response:
    counts["xmltv"] += 1
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <programme start="{_xmltv_utc(now_ts - 600)}" stop="{_xmltv_utc(now_ts + 3000)}" channel="HBO.us">
    <title>Inferior XMLTV Scraper HBO Listing</title>
  </programme>
  <programme start="{_xmltv_utc(now_ts - 600)}" stop="{_xmltv_utc(now_ts + 3000)}" channel="IntlSpec.tv">
    <title>Global World Cinema</title>
    <desc>Backfilled from upstream XMLTV.</desc>
  </programme>
</tv>
"""
    return web.Response(text=xml, content_type="application/xml")

  async def _router(request: web.Request) -> web.Response:
    p = request.path
    if request.method == "POST" and p == "/20141201/token":
      return await _sd_token(request)
    if request.method == "GET" and p == "/20141201/status":
      return await _sd_status(request)
    if request.method == "GET" and p == "/20141201/headends":
      return await _sd_headends(request)
    if p.startswith("/20141201/lineups/"):
      lid = p.rsplit("/", 1)[-1]
      return await _sd_lineup(request, lid)
    if request.method == "POST" and p == "/20141201/schedules/md5":
      return await _sd_schedules_md5(request)
    if request.method == "POST" and p == "/20141201/schedules":
      return await _sd_schedules(request)
    if request.method == "POST" and p == "/20141201/programs":
      return await _sd_programs(request)
    if request.method == "GET" and p == "/xmltv.php":
      return await _upstream_xmltv(request)
    return web.Response(status=404, text="Not found")

  async with MockHttpServer(_router) as mock:
    xmltv_url = f"{mock.base_url}/xmltv.php?username=u&password=p"
    sd_base_url = f"{mock.base_url}/20141201"

    bundle = {
      "lineup_info": {
        "lineup_id": "TVG-9100000607",
        "provider": "Bell Fibe TV - Montreal (Digital)",
        "market": "Montreal, QC",
        "postal_prefix": "H2Y",
      },
      "server_info": {"xmltv_url": xmltv_url},
      "live_categories": [
        {
          "category_id": "1",
          "category_name": "General (50–2621)",
          "parent_id": 0,
        }
      ],
      "live_streams": [
        {
          "num": 50,
          "name": "AMI-tele",
          "stream_id": 1050,
          "epg_channel_id": "bell.50.ca",
          "category_id": "1",
          "category_name": "General (50–2621)",
          "direct_source": "http://panel.example/live/u/p/1050.ts",
          "guide": {
            "callsign": "AMITELE",
            "language": "fr",
            "gracenote_station_id": "9266073169",
            "has_native_epg": False,
          },
        },
        {
          "num": 220,
          "name": "NBC (Plattsburg)",
          "stream_id": 1220,
          "epg_channel_id": "NBC.us",
          "category_id": "1",
          "category_name": "General (50–2621)",
          "direct_source": "http://panel.example/live/u/p/1220.ts",
          "guide": {
            "callsign": "WPTZ",
            "language": "en",
            "gracenote_station_id": "9200002511",
            "has_native_epg": False,
          },
        },
        {
          "num": 1285,
          "name": "NBC (Seattle)",
          "stream_id": 2285,
          "epg_channel_id": "NBC.us",
          "category_id": "1",
          "category_name": "General (50–2621)",
          "direct_source": "http://panel.example/live/u/p/2285.ts",
          "guide": {
            "callsign": "KING",
            "language": "en",
            "gracenote_station_id": "9200009999",
            "has_native_epg": False,
          },
        },
        {
          "num": 1306,
          "name": "HBO",
          "stream_id": 2306,
          "epg_channel_id": "HBO.us",
          "category_id": "1",
          "category_name": "General (50–2621)",
          "direct_source": "http://panel.example/live/u/p/2306.ts",
          "guide": {
            "callsign": "HBOCAN",
            "language": "en",
            "has_native_epg": True,
          },
        },
        {
          "num": 2621,
          "name": "Intl Specialty",
          "stream_id": 3621,
          "epg_channel_id": "IntlSpec.tv",
          "category_id": "1",
          "category_name": "General (50–2621)",
          "direct_source": "http://panel.example/live/u/p/3621.ts",
          "guide": {
            "callsign": "INTLSP",
            "language": "en",
            "has_native_epg": True,
          },
        },
      ],
    }
    (xt_dir / "bellhop_xtream_playlist.json").write_text(
      json.dumps(bundle), encoding="utf-8"
    )

    store = LineupGuideStore(xt_dir)
    epg_db_file = cache_dir / "bellhop_web" / "epg.sqlite3"
    sd_cfg = SchedulesDirectEpgConfig(
      username="chhamilton",
      password="sd_secret_pw",
      base_url=sd_base_url,
      auto_add_lineup=True,
      postal_code="H2Y",
      days=2,
      token_cache_file=cache_dir / "sd_token.json",
    )
    refresher = EpgRefreshService(
      store,
      EpgRefreshConfig(
        enabled=True,
        xmltv_url=xmltv_url,
        cache_file=epg_db_file,
        sd_epg=sd_cfg,
      ),
      session_factory=mock.session_factory,
      clock=lambda: float(now_ts),
    )

    status1 = await refresher.refresh_now(force=True, reason="initial")
    assert status1["state"] == "ok"
    assert status1["channels_with_epg"] == 5
    assert counts["put_lineup"] == 1
    assert counts["schedules"] == 1
    assert counts["programs"] == 1
    assert counts["xmltv"] == 1
    assert epg_db_file.is_file()

    snap = store.get_snapshot()
    # In-memory snapshot holds ZERO ProgramItem attributes — all airings live in SQLite!
    assert len(snap.channels) == 5
    assert not hasattr(snap, "programs_by_epg_id")
    assert not hasattr(snap, "programs_by_chno")

    # 1. `50 AMI-télé` (missing from XMLTV) is populated from Schedules Direct with S02E04 subtitle!
    ami_progs = refresher.db.query_channel_schedule(50, "bell.50.ca")
    assert ami_progs[0].title == "Et si c'était toi?"
    assert ami_progs[0].sub_title == "S02E04 · Rencontres d'automne"

    # 2. `1306 HBO` (present in both) prioritizes Schedules Direct over upstream XMLTV!
    hbo_progs = refresher.db.query_channel_schedule(1306, "HBO.us")
    assert hbo_progs[0].title == "Dune: Part Two"
    assert hbo_progs[0].sub_title == "2024"

    # 3. `220 NBC (Plattsburgh)` and `1285 NBC (Seattle)` get distinct per-station schedules despite sharing `NBC.us`!
    wptz_progs = refresher.db.query_channel_schedule(220, "NBC.us")
    king_progs = refresher.db.query_channel_schedule(1285, "NBC.us")
    assert wptz_progs[0].title == "NBC 5 News at Noon"
    assert king_progs[0].title == "Live: Seahawks Pre-Game"
    assert king_progs[0].sub_title == "49ers vs. Seahawks"

    # 4. `2621 Intl Specialty` (absent from SD) is seamlessly backfilled from upstream XMLTV!
    intl_progs = refresher.db.query_channel_schedule(2621, "IntlSpec.tv")
    assert intl_progs[0].title == "Global World Cinema"

    # 5. Second refresh with unchanged MD5s + simulated expired token (`4003`) re-auths once
    #    and makes ZERO `/schedules` or `/programs` fetches!
    force_expired_once["armed"] = True
    status2 = await refresher.refresh_now(force=True, reason="md5_recheck")
    assert status2["state"] == "ok"
    assert counts["token"] == 2
    assert counts["schedules_md5"] == 2
    assert counts["schedules"] == 1
    assert counts["programs"] == 1

    await refresher.stop()

    # 6. `UnifiedServeServer` hydrates SQLite on startup, serves distinct affiliate EPGs
    #    on `/api/epg/<chno>`, serves base64 `get_short_epg` / `get_simple_data_table`,
    #    and streams merged XMLTV chunks directly from SQLite on `/xmltv.php`!
    app_cfg = AppConfig(
      project_root=tmp_path,
      xtream=XtreamConfig(
        server_url=mock.base_url,
        username="u",
        password="p",
        cache_dir=Path("cache"),
      ),
      lineup=LineupConfig(
        backend="tvguide",
        lineup_id="TVG-9100000607",
        postal_code="H2Y",
        schedules_direct=SchedulesDirectConfig(
          username="chhamilton",
          password="sd_secret_pw",
          base_url=sd_base_url,
          auto_add_lineup=True,
        ),
      ),
      epg=EpgConfig(verify=False, refresh_hours=6.0),
      output=OutputConfig(xtream_dir=Path("output/xtream")),
      serve=ServeConfig(
        web=ServeWebConfig(
          enabled=True,
          host="127.0.0.1",
          port=0,
          username="web_admin",
          password="web_pw",
        ),
        xtream=ServeXtreamConfig(
          enabled=True,
          host="127.0.0.1",
          port=0,
          username="xt_user",
          password="xt_pass",
        ),
      ),
    )
    unified = UnifiedServeServer.from_config(
      app_cfg,
      session_factory=mock.session_factory,
      epg_session_factory=mock.session_factory,
    )
    unified.web_app._clock = lambda: float(now_ts)
    assert unified.web_app.epg_refresher is not None
    unified.web_app.epg_refresher._clock = lambda: float(now_ts)
    await unified.start()
    try:
      base_url = f"http://127.0.0.1:{unified.bound_port}"
      async with new_test_session() as client:
        async with client.post(
          f"{base_url}/api/auth/login",
          json={"username": "web_admin", "password": "web_pw"},
        ) as r_login:
          assert r_login.status == 200
        async with client.get(f"{base_url}/api/epg/220") as r220:
          assert r220.status == 200
          j220 = await r220.json()
          assert j220["programs"][0]["title"] == "NBC 5 News at Noon"

        async with client.get(f"{base_url}/api/epg/1285") as r1285:
          assert r1285.status == 200
          j1285 = await r1285.json()
          assert j1285["programs"][0]["title"] == "Live: Seahawks Pre-Game"

        async with client.get(
          f"{base_url}/player_api.php",
          params={
            "username": "xt_user",
            "password": "xt_pass",
            "action": "get_short_epg",
            "stream_id": "2306",
          },
        ) as r_short:
          assert r_short.status == 200
          short_json = await r_short.json()
          listings = short_json["epg_listings"]
          assert len(listings) >= 1
          assert (
            base64.b64decode(listings[0]["title"]).decode("utf-8")
            == "Dune: Part Two"
          )

        async with client.get(
          f"{base_url}/player_api.php",
          params={
            "username": "xt_user",
            "password": "xt_pass",
            "action": "get_simple_data_table",
            "stream_id": "1050",
          },
        ) as r_table:
          assert r_table.status == 200
          tbl_json = await r_table.json()
          assert (
            base64.b64decode(tbl_json["epg_listings"][0]["title"]).decode(
              "utf-8"
            )
            == "Et si c'était toi?"
          )

        async with client.get(
          f"{base_url}/xmltv.php",
          params={"username": "xt_user", "password": "xt_pass"},
          allow_redirects=False,
        ) as r_xml:
          assert r_xml.status == 200
          xml_body = await r_xml.text()
          assert "Et si c'était toi?" in xml_body
          assert "Dune: Part Two" in xml_body
          assert "Global World Cinema" in xml_body
          assert "Inferior XMLTV Scraper HBO Listing" not in xml_body
    finally:
      await unified.stop()


@pytest.mark.asyncio
async def test_epg_db_permissions_corrupt_recovery_schema_bump_and_cli_banner(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """Verify `0o600` DB mode, corrupt/schema-bump recovery, and `bellhop serve` SQLite banner."""
  import argparse
  import contextlib
  import io
  import sqlite3
  import stat

  from rich.console import Console

  from bellhop import cli
  from bellhop.epg.db import (
    SD_PROGRAM_RETENTION_DAYS,
    SD_PROGRAM_RETENTION_SEC,
  )
  from bellhop.web.store import ProgramItem

  assert SD_PROGRAM_RETENTION_DAYS == 14

  cache_dir = tmp_path / "cache" / "bellhop_web"
  db_path = cache_dir / "epg.sqlite3"
  now_ts = int(datetime.now(tz=timezone.utc).timestamp())

  # 1. Corrupt non-SQLite file on disk is safely replaced and restricted to 0o600
  cache_dir.mkdir(parents=True)
  db_path.write_bytes(b"not-a-valid-sqlite-header-at-all")
  db = EpgDatabase(db_path)
  db.ensure_initialized()
  assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
  assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700

  # 2. Schema version mismatch dynamically drops arbitrary tables via sqlite_master
  with contextlib.closing(sqlite3.connect(db_path)) as raw_conn:
    raw_conn.execute("CREATE TABLE obsolete_future_table (x TEXT)")
    raw_conn.execute("PRAGMA user_version = 99")
    raw_conn.commit()

  db_reopened = EpgDatabase(db_path)
  db_reopened.ensure_initialized()
  with contextlib.closing(sqlite3.connect(db_path)) as raw_conn:
    tables = {
      str(r[0])
      for r in raw_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
      ).fetchall()
    }
    ver = int(raw_conn.execute("PRAGMA user_version").fetchone()[0])
    auto_vac = int(raw_conn.execute("PRAGMA auto_vacuum").fetchone()[0])
  assert "obsolete_future_table" not in tables
  assert "guide_airings" in tables
  assert ver == 1
  assert auto_vac == 2  # INCREMENTAL auto-vacuum

  # 2b. `touch_and_prune_sd_programs` retains active & <14d programs and prunes >14d orphans
  db_reopened.upsert_sd_programs(
    {
      "MV_ACTIVE": {"md5": "m1", "title": "Active Movie"},
      "MV_RECENT": {"md5": "m2", "title": "Recent Rerun"},
      "MV_STALE": {"md5": "m3", "title": "Expired Three Weeks Ago"},
    },
    updated_at=float(now_ts) - (SD_PROGRAM_RETENTION_SEC + 3600.0),
  )
  db_reopened.upsert_sd_programs(
    {"MV_RECENT": {"md5": "m2", "title": "Recent Rerun"}},
    updated_at=float(now_ts) - (5 * 86400.0),
  )
  pruned_cnt = db_reopened.touch_and_prune_sd_programs(
    ["MV_ACTIVE"], now_ts=float(now_ts)
  )
  assert pruned_cnt == 1
  surviving = db_reopened.load_sd_programs(
    ["MV_ACTIVE", "MV_RECENT", "MV_STALE"]
  )
  assert set(surviving.keys()) == {"MV_ACTIVE", "MV_RECENT"}

  # 3. `bellhop serve` CLI startup banner reads active/cached counts from SQLite
  xt_dir = tmp_path / "output" / "xtream"
  xt_dir.mkdir(parents=True)
  (xt_dir / "bellhop_xtream_playlist.json").write_text(
    json.dumps(
      {
        "lineup_info": {
          "lineup_id": "CAN-0004980-X",
          "provider": "Bell Fibe TV",
          "market": "Montréal",
        },
        "server_info": {
          "timezone": "America/Montreal",
          "xmltv_url": "http://panel.example/xmltv.php?username=u&password=p",
        },
        "live_categories": [
          {"category_id": "1", "category_name": "Movies", "parent_id": 0}
        ],
        "live_streams": [
          {
            "num": 1306,
            "name": "HBO",
            "stream_type": "live",
            "stream_id": 2306,
            "epg_channel_id": "HBO.us",
            "category_id": "1",
            "category_name": "Movies",
            "direct_source": "http://panel.example/live/u/p/2306.ts",
          }
        ],
      }
    ),
    encoding="utf-8",
  )
  db_reopened.replace_guide_schedules(
    snap=LineupGuideStore(xt_dir).get_snapshot(),
    programs_by_id={
      "HBO.us": [
        ProgramItem(
          id="1",
          channel_id="HBO.us",
          title="Dune: Part Two",
          start_timestamp=now_ts - 600,
          stop_timestamp=now_ts + 3000,
        )
      ]
    },
    fetched_at=float(now_ts),
    covered_channel_ids=frozenset({"hbo.us"}),
  )
  cfg_file = tmp_path / "config.yaml"
  cfg_file.write_text(
    f"""\
paths:
  cache_dir: "{tmp_path / "cache"}"
  xtream_output_dir: "{xt_dir}"
web_server:
  enabled: true
  host: "127.0.0.1"
  port: 0
  username: "banner_admin"
  password: "banner_secret"
xtream_server:
  enabled: false
""",
    encoding="utf-8",
  )

  buf = io.StringIO()
  monkeypatch.setattr(
    cli, "console", Console(file=buf, force_terminal=False, width=120)
  )

  async def _stop_immediately(self: UnifiedServeServer) -> None:
    await self.stop()

  monkeypatch.setattr(UnifiedServeServer, "serve_forever", _stop_immediately)
  rc = await cli._execute_cli_serve(
    argparse.Namespace(config=cfg_file, quiet=False, verbose=False)
  )
  assert rc == 0
  banner_text = buf.getvalue()
  assert "1 active / 1 cached" in banner_text
  assert "epg.sqlite3" in banner_text

  # 4. Keyset-paginated `iter_xmltv_chunks` yields multi-page chunks without leaking synthetic IDs
  snap_for_xml = LineupGuideStore(xt_dir).get_snapshot()
  chunks = list(
    db_reopened.iter_xmltv_chunks(
      snap_for_xml, chunk_target_bytes=64, page_rows=1
    )
  )
  assert len(chunks) >= 3
  xml_out = b"".join(chunks).decode("utf-8")
  assert "Dune: Part Two" in xml_out
  assert 'channel="ch.' not in xml_out
