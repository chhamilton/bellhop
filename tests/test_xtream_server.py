"""Tests for the passthrough Xtream Codes API server (`bellhop serve`)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from bellhop.config import load_config
from bellhop.runner import PipelineRunner, PipelineRunOptions
from bellhop.serve import UnifiedServeServer
from bellhop.xtream import fetch_xtream_catalog
from tests.fake_xtream_server import (
  FakeProgram,
  FakeStreamDef,
  FakeXtreamServer,
)
from tests.http_mock import TestHttpClient


def _write_mini_lineup(path: Path) -> None:
  path.write_text(
    """\
lineup_id: CAN-BELLFIBE-MTL-X
provider: Bell Fibe TV
market: Montréal, QC
postal_prefix: H2Y
channels:
  - chno: 1101
    sd_chno: 101
    name: ICI Radio-Canada Télé (Montréal)
    callsign: CBFT-DT
    tvg_id: CBFT.ca
    logo: https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/canada/ici-tele-ca.png
    group: Francophone — Généraliste
    language: fr
    regions: [montreal, qc, ca]
    aliases: [ICI Montreal, Radio-Canada]
  - chno: 1108
    sd_chno: 108
    name: RDS
    callsign: RDS
    tvg_id: RDS.ca
    logo: https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/canada/rds-ca.png
    group: Sports
    language: fr
    regions: [qc, ca]
    aliases: [RDS]
""",
    encoding="utf-8",
  )


@pytest.mark.asyncio
async def test_bellhop_serve_xtream_contract_and_302_passthrough(
  tmp_path: Path,
) -> None:
  now_ts = int(time.time())
  upstream = FakeXtreamServer(
    username="upstream_u",
    password="upstream_secret_pw",
    max_connections=1,
    enforce_max_connections=True,
    ts_chunks_per_stream=2,
    categories=[
      {"category_id": "99", "category_name": "RAW| UNSORTED", "parent_id": 0}
    ],
    streams=[
      FakeStreamDef(
        stream_id=410101,
        name="CA FR: ICI MONTREAL FHD",
        category_id="99",
        epg_channel_id="ICITeleMontreal.ca",
        marker_byte=0xA1,
      ),
      FakeStreamDef(
        stream_id=410108,
        name="CA FR: RDS RAW 60FPS",
        category_id="99",
        epg_channel_id="RDS.ca",
        marker_byte=0xB8,
      ),
    ],
    programs=[
      FakeProgram(
        channel_id="ICITeleMontreal.ca",
        title="Le Téléjournal",
        start_ts=now_ts - 600,
        stop_ts=now_ts + 3000,
      ),
    ],
  )

  async with upstream:
    lineup_file = tmp_path / "lineup.yaml"
    _write_mini_lineup(lineup_file)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
      f"""\
paths:
  cache_dir: "cache"
  state_dir: "state"
  lineup_file: "{lineup_file}"
  m3u8_playlist_path: "output/lineup.m3u8"
  m3u8_report_path: "output/report.json"
  xtream_output_dir: "output/xtream"
web_server:
  host: "127.0.0.1"
  port: 0
upstream_xtream:
  server_url: "{upstream.base_url}"
  username: "{upstream.username}"
  password: "{upstream.password}"
  stream_format: "ts"
xtream_server:
  enabled: true
  username: "client_alice"
  password: "client_secret_42"
pipeline:
  lineup:
    backend: "yaml"
    lineup_id: "CAN-BELLFIBE-MTL-X"
    collapse_sd_dupes: true
  validation:
    enabled: true
    use_ffprobe: false
    inter_probe_delay_sec: 0.02
    min_bytes_to_read: 4096
  epg:
    verify: true
  output:
    m3u8_enabled: true
    xtream_enabled: true
""",
      encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    redacted = cfg.to_dict(redact_secrets=True)
    assert redacted["serve"]["xtream"]["username"] == "client_alice"
    assert redacted["serve"]["xtream"]["password"] == "***"

    # 1. Run pipeline to produce calculated lineup categories & streams
    run_res = await PipelineRunner(cfg).run(PipelineRunOptions())
    assert run_res.xtream_result is not None
    assert run_res.xtream_result.epg_verified is True

    # 2. Launch unified `bellhop serve` (Web Guide + Xtream API on the same port!)
    unified_srv = UnifiedServeServer.from_config(cfg)
    assert unified_srv.shared_listener is True
    await unified_srv.start()
    serve_base = f"http://127.0.0.1:{unified_srv.bound_port}"

    try:
      async with TestHttpClient(
        base_url=serve_base, timeout=10.0, follow_redirects=False
      ) as client:
        # Unified /health check
        health = (await client.get("/health")).json()
        assert health["status"] == "ok"
        assert health["web_guide_enabled"] is True
        assert health["xtream_api_enabled"] is True
        assert health["category_count"] == 2
        assert health["channel_count"] == 2

        # --- Web Guide routes on the same port ---
        idx = await client.get("/?cat=2&view=timeline&ch=1108")
        assert idx.status_code == 200
        assert "player-overlay" in idx.text
        assert (await client.get("/static/app.js")).status_code == 200

        # Wait for automatic dynamic EPG fetch into the Web Guide's disk cache
        for _ in range(50):
          api_health = (await client.get("/api/health")).json()
          if (
            api_health["epg_status"]["state"] == "ok"
            and api_health["epg_active_program_count"] > 0
          ):
            break
          await asyncio.sleep(0.05)
        assert (tmp_path / "cache" / "bellhop_web" / "epg_cache.json").is_file()

        lu = (await client.get("/api/lineup")).json()
        assert "upstream_secret_pw" not in json.dumps(lu)
        ch_ici = next(c for c in lu["channels"] if c["chno"] == 1101)
        assert ch_ici["now_playing"]["title"] == "Le Téléjournal"

        # Critical routing check: `/api/stream/1101.ts` MUST hit WebGuideApp (200 TS stream),
        # NOT Xtream's `/<user>/<pass>/<id>.ts` (401/302)!
        web_ts = await client.get("/api/stream/1101.ts?client=web-tab")
        assert web_ts.status_code == 200
        assert web_ts.content[0] == 0x47 and web_ts.content[4] == 0xA1
        assert (
          await client.post("/api/tuner/release?client=web-tab")
        ).status_code == 200

        # --- Xtream Codes API routes on the same port ---
        # Reject bad credentials
        bad_auth = (
          await client.get(
            "/player_api.php",
            params={"username": "client_alice", "password": "wrong"},
          )
        ).json()
        assert bad_auth == {"user_info": {"auth": 0}}
        assert (
          await client.get(
            "/xmltv.php",
            params={"username": "client_alice", "password": "wrong"},
          )
        ).status_code == 401
        assert (
          await client.get("/live/client_alice/wrong/410101.ts")
        ).status_code == 401

        # Handshake via GET and POST form body; never leak upstream password
        for method in ("GET", "POST"):
          if method == "GET":
            resp = await client.get(
              "/player_api.php",
              params={
                "username": "client_alice",
                "password": "client_secret_42",
              },
            )
          else:
            resp = await client.post(
              "/player_api.php",
              data={"username": "client_alice", "password": "client_secret_42"},
            )
          assert resp.status_code == 200
          body = resp.json()
          assert "upstream_secret_pw" not in resp.text
          assert body["user_info"]["auth"] == 1
          assert body["user_info"]["username"] == "client_alice"
          assert body["server_info"]["url"] == "127.0.0.1"
          assert body["server_info"]["port"] == str(unified_srv.bound_port)

        # Categories reflect lineup retrieval groups (NOT upstream "RAW| UNSORTED")
        cats = (
          await client.get(
            "/player_api.php",
            params={
              "username": "client_alice",
              "password": "client_secret_42",
              "action": "get_live_categories",
            },
          )
        ).json()
        assert [c["category_name"] for c in cats] == [
          "Francophone — Généraliste (1101)",
          "Sports (1108)",
        ]

        # Streams (all + category_id filter)
        all_streams = (
          await client.get(
            "/player_api.php",
            params={
              "username": "client_alice",
              "password": "client_secret_42",
              "action": "get_live_streams",
            },
          )
        ).json()
        assert "upstream_secret_pw" not in json.dumps(all_streams)
        assert [(s["num"], s["name"], s["stream_id"]) for s in all_streams] == [
          (1101, "ICI Radio-Canada Télé (Montréal)", 410101),
          (1108, "RDS", 410108),
        ]
        assert all(s["direct_source"] == "" for s in all_streams)

        sports_only = (
          await client.get(
            "/player_api.php",
            params={
              "username": "client_alice",
              "password": "client_secret_42",
              "action": "get_live_streams",
              "category_id": "2",
            },
          )
        ).json()
        assert [s["num"] for s in sports_only] == [1108]

        # VOD/Series/EPG full player_api.php + player.php alias compatibility
        for stub_action, expected_body in (
          ("get_vod_categories", []),
          ("get_series_categories", []),
          ("get_vod_streams", []),
          ("get_series", []),
          ("get_vod_info", {"info": {}, "movie_data": {}}),
          ("get_series_info", {"seasons": [], "info": {}, "episodes": {}}),
          ("get_short_epg", {"epg_listings": []}),
          ("get_simple_data_table", {"epg_listings": []}),
        ):
          r = await client.get(
            "/player_api.php",
            params={
              "username": "client_alice",
              "password": "client_secret_42",
              "action": stub_action,
            },
          )
          assert r.status_code == 200
          assert r.json() == expected_body

        # `/player.php` alias & `/panel_api.php`
        player_alias = await client.get(
          "/player.php",
          params={
            "username": "client_alice",
            "password": "client_secret_42",
            "action": "get_live_categories",
          },
        )
        assert player_alias.status_code == 200
        assert [c["category_name"] for c in player_alias.json()] == [
          "Francophone — Généraliste (1101)",
          "Sports (1108)",
        ]

        panel = await client.get(
          "/panel_api.php",
          params={"username": "client_alice", "password": "client_secret_42"},
        )
        assert panel.status_code == 200
        panel_json = panel.json()
        assert "1101" in panel_json["available_channels"]
        assert "upstream_secret_pw" not in panel.text

        # Auth-checked non-live routes return 401 on bad creds and 404 on valid creds
        assert (
          await client.get("/movie/client_alice/wrong/123.mp4")
        ).status_code == 401
        assert (
          await client.get("/movie/client_alice/client_secret_42/123.mp4")
        ).status_code == 404

        # XMLTV endpoint (+ /epg.xml alias) -> 302 passthrough redirect straight to upstream xmltv.php
        for epg_path in ("/xmltv.php", "/epg.xml"):
          xmltv_redir = await client.get(
            epg_path,
            params={"username": "client_alice", "password": "client_secret_42"},
          )
          assert xmltv_redir.status_code == 302
          assert xmltv_redir.headers["location"] == upstream.xmltv_url

        # Stream endpoints -> 302 passthrough redirects (TS, HLS, and shorthand without /live)
        ts_redir = await client.get(
          "/live/client_alice/client_secret_42/410101.ts"
        )
        assert ts_redir.status_code == 302
        assert ts_redir.headers["location"] == upstream.stream_url(410101, "ts")

        hls_redir = await client.get(
          "/live/client_alice/client_secret_42/410108.m3u8"
        )
        assert hls_redir.status_code == 302
        assert hls_redir.headers["location"] == upstream.stream_url(
          410108, "m3u8"
        )

        short_redir = await client.get("/client_alice/client_secret_42/410108")
        assert short_redir.status_code == 302
        assert short_redir.headers["location"] == upstream.stream_url(
          410108, "ts"
        )

        # /get.php M3U modes: m3u_plus (ts & hls), basic m3u, category filter, HEAD, & /playlist.m3u8
        m3u_plus_ts = await client.get(
          "/get.php",
          params={
            "username": "client_alice",
            "password": "client_secret_42",
            "type": "m3u_plus",
            "output": "ts",
          },
        )
        assert m3u_plus_ts.status_code == 200
        assert "playlist.m3u8" in m3u_plus_ts.headers.get(
          "content-disposition", ""
        )
        assert "url-tvg=" in m3u_plus_ts.text
        assert 'group-title="Sports (1108)"' in m3u_plus_ts.text
        assert 'tvg-language="fr"' in m3u_plus_ts.text
        assert (
          "/live/client_alice/client_secret_42/410108.ts" in m3u_plus_ts.text
        )

        m3u_plus_hls = await client.get(
          "/get.php",
          params={
            "username": "client_alice",
            "password": "client_secret_42",
            "type": "m3u_plus",
            "output": "hls",
            "category_id": "2",
          },
        )
        assert m3u_plus_hls.status_code == 200
        assert (
          "/live/client_alice/client_secret_42/410108.m3u8" in m3u_plus_hls.text
        )
        assert "410101" not in m3u_plus_hls.text

        m3u_basic = await client.get(
          "/get.php",
          params={
            "username": "client_alice",
            "password": "client_secret_42",
            "type": "m3u",
            "output": "mpegts",
          },
        )
        assert m3u_basic.status_code == 200
        assert "playlist.m3u" in m3u_basic.headers.get(
          "content-disposition", ""
        )
        assert m3u_basic.text.startswith("#EXTM3U\n")
        assert "#EXTINF:-1,RDS\n" in m3u_basic.text
        assert "tvg-logo=" not in m3u_basic.text
        assert "/live/client_alice/client_secret_42/410108.ts" in m3u_basic.text

        head_m3u = await client.head(
          "/playlist.m3u8",
          params={"username": "client_alice", "password": "client_secret_42"},
        )
        assert head_m3u.status_code == 200
        assert int(head_m3u.headers["content-length"]) > 0

      # Follow redirects end-to-end: XMLTV and live TS bytes come directly from upstream
      async with TestHttpClient(
        base_url=serve_base, timeout=10.0, follow_redirects=True
      ) as follow_client:
        xml_live = await follow_client.get(
          "/xmltv.php",
          params={"username": "client_alice", "password": "client_secret_42"},
        )
        assert xml_live.status_code == 200
        assert "Le Téléjournal" in xml_live.text

        ts_live = await follow_client.get(
          "/live/client_alice/client_secret_42/410101.ts"
        )
        assert ts_live.status_code == 200
        assert ts_live.content[0] == 0x47 and ts_live.content[4] == 0xA1

        # Round-trip with our own Xtream client (`fetch_xtream_catalog`)!
        cfg_client = load_config(cfg_path)
        cfg_client.xtream.server_url = serve_base
        cfg_client.xtream.username = "client_alice"
        cfg_client.xtream.password = "client_secret_42"
        roundtrip_cat = await fetch_xtream_catalog(
          cfg_client, force_refresh=True
        )
        assert roundtrip_cat.account.username == "client_alice"
        assert roundtrip_cat.categories == {
          "1": "Francophone — Généraliste (1101)",
          "2": "Sports (1108)",
        }
        assert [
          (s.num, s.name, s.stream_id) for s in roundtrip_cat.streams
        ] == [
          (1101, "ICI Radio-Canada Télé (Montréal)", 410101),
          (1108, "RDS", 410108),
        ]

        # Hot-reload test: update bundle on disk and verify immediate reflection
        bundle_path = (
          tmp_path / "output" / "xtream" / "bellhop_xtream_playlist.json"
        )
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle["live_categories"].append(
          {"category_id": "3", "category_name": "News", "parent_id": 0}
        )
        await asyncio.sleep(0.02)
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        cats_reloaded = (
          await follow_client.get(
            "/player_api.php",
            params={
              "username": "client_alice",
              "password": "client_secret_42",
              "action": "get_live_categories",
            },
          )
        ).json()
        assert [c["category_name"] for c in cats_reloaded] == [
          "Francophone — Généraliste (1101)",
          "Sports (1108)",
          "News",
        ]
    finally:
      await unified_srv.stop()


@pytest.mark.asyncio
async def test_bellhop_serve_default_bind_config_and_unconfigured_xtream_auth(
  tmp_path: Path,
) -> None:
  xt_dir = tmp_path / "output" / "xtream"
  xt_dir.mkdir(parents=True, exist_ok=True)
  bundle = {
    "lineup_info": {
      "lineup_id": "CAN-BELLFIBE-MTL-X",
      "provider": "Bell Fibe TV",
      "market": "Montréal, QC",
    },
    "server_info": {"timezone": "America/Montreal"},
    "live_categories": [
      {"category_id": "1", "category_name": "Sports", "parent_id": 0}
    ],
    "live_streams": [
      {
        "num": 1108,
        "name": "RDS",
        "stream_type": "live",
        "stream_id": 410108,
        "epg_channel_id": "RDS.ca",
        "category_id": "1",
        "category_name": "Sports",
        "direct_source": "http://upstream.example/live/u/p/410108.ts",
        "guide": {"language": "fr"},
      }
    ],
  }
  (xt_dir / "bellhop_xtream_playlist.json").write_text(
    json.dumps(bundle), encoding="utf-8"
  )

  # Default config binds 0.0.0.0:8080
  cfg_default_path = tmp_path / "default_cfg.yaml"
  cfg_default_path.write_text(
    "upstream_xtream:\n  server_url: http://example.invalid\n",
    encoding="utf-8",
  )
  cfg_default = load_config(cfg_default_path)
  assert cfg_default.serve.host == "0.0.0.0"
  assert cfg_default.serve.port == 8080

  # Custom bind address/port with no serve credentials: Web Guide works, Xtream API stays locked
  cfg_custom_path = tmp_path / "custom_cfg.yaml"
  cfg_custom_path.write_text(
    f"""\
paths:
  xtream_output_dir: "{xt_dir}"
web_server:
  host: "127.0.0.1"
  port: 0
""",
    encoding="utf-8",
  )
  cfg_custom = load_config(cfg_custom_path)
  srv = UnifiedServeServer.from_config(cfg_custom)
  await srv.start()
  try:
    async with TestHttpClient(
      base_url=f"http://127.0.0.1:{srv.bound_port}", timeout=5.0
    ) as client:
      health = (await client.get("/health")).json()
      assert health["web_guide_enabled"] is True
      assert health["xtream_api_enabled"] is False
      assert (await client.get("/")).status_code == 200
      assert (await client.get("/api/lineup")).status_code == 200
      assert (await client.get("/player_api.php")).json() == {
        "user_info": {"auth": 0}
      }
      assert (await client.get("/xmltv.php")).status_code == 401
  finally:
    await srv.stop()


@pytest.mark.asyncio
async def test_bellhop_serve_separate_ports_mode(tmp_path: Path) -> None:
  xt_dir = tmp_path / "output" / "xtream"
  xt_dir.mkdir(parents=True, exist_ok=True)
  bundle = {
    "lineup_info": {
      "lineup_id": "CAN-BELLFIBE-MTL-X",
      "provider": "Bell Fibe TV",
      "market": "Montréal, QC",
    },
    "server_info": {
      "timezone": "America/Montreal",
      "xmltv_url": "http://upstream.example/xmltv.php?username=u&password=p",
    },
    "live_categories": [
      {"category_id": "1", "category_name": "Sports", "parent_id": 0}
    ],
    "live_streams": [
      {
        "num": 1108,
        "name": "RDS",
        "stream_type": "live",
        "stream_id": 410108,
        "epg_channel_id": "RDS.ca",
        "category_id": "1",
        "category_name": "Sports",
        "direct_source": "http://upstream.example/live/u/p/410108.ts",
        "guide": {"language": "fr"},
      }
    ],
  }
  (xt_dir / "bellhop_xtream_playlist.json").write_text(
    json.dumps(bundle), encoding="utf-8"
  )

  # Pick two distinct free ports to verify explicit separate-port configuration in config.yaml
  import socket

  def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
      s.bind(("127.0.0.1", 0))
      return int(s.getsockname()[1])

  web_p = _free_port()
  xt_p = _free_port()
  while xt_p == web_p:
    xt_p = _free_port()

  cfg_path = tmp_path / "split_cfg.yaml"
  cfg_path.write_text(
    f"""\
paths:
  xtream_output_dir: "{xt_dir}"
web_server:
  enabled: true
  host: "127.0.0.1"
  port: {web_p}
xtream_server:
  enabled: true
  port: {xt_p}
  username: "split_user"
  password: "split_password"
""",
    encoding="utf-8",
  )
  cfg = load_config(cfg_path)
  assert cfg.serve.web_port == web_p
  assert cfg.serve.xtream_port == xt_p

  srv = UnifiedServeServer.from_config(cfg)
  assert srv.shared_listener is False
  await srv.start()
  try:
    assert srv.web_bound_port == web_p
    assert srv.xtream_bound_port == xt_p

    async with TestHttpClient(timeout=5.0, follow_redirects=False) as client:
      web_base = f"http://127.0.0.1:{web_p}"
      xt_base = f"http://127.0.0.1:{xt_p}"

      # Web listener serves Web Guide only (404 on Xtream routes)
      web_health = (await client.get(f"{web_base}/health")).json()
      assert web_health["web_guide_enabled"] is True
      assert web_health["xtream_api_enabled"] is False
      assert web_health["shared_listener"] is False
      assert (await client.get(f"{web_base}/")).status_code == 200
      assert (await client.get(f"{web_base}/api/lineup")).status_code == 200
      assert (await client.get(f"{web_base}/player_api.php")).status_code == 404
      assert (await client.get(f"{web_base}/xmltv.php")).status_code == 404

      # Xtream listener serves Xtream API only (404 on Web Guide routes)
      xt_health = (await client.get(f"{xt_base}/health")).json()
      assert xt_health["web_guide_enabled"] is False
      assert xt_health["xtream_api_enabled"] is True
      assert xt_health["shared_listener"] is False
      assert (await client.get(f"{xt_base}/")).status_code == 404
      assert (await client.get(f"{xt_base}/api/lineup")).status_code == 404

      auth_resp = (
        await client.get(
          f"{xt_base}/player_api.php",
          params={"username": "split_user", "password": "split_password"},
        )
      ).json()
      assert auth_resp["user_info"]["auth"] == 1
      assert auth_resp["server_info"]["port"] == str(xt_p)

      xml_redir = await client.get(
        f"{xt_base}/xmltv.php",
        params={"username": "split_user", "password": "split_password"},
      )
      assert xml_redir.status_code == 302
      assert (
        xml_redir.headers["location"]
        == "http://upstream.example/xmltv.php?username=u&password=p"
      )
  finally:
    await srv.stop()


@pytest.mark.asyncio
async def test_unified_serve_distinct_web_login_and_xtream_credentials(
  tmp_path: Path,
) -> None:
  xt_dir = tmp_path / "output" / "xtream"
  xt_dir.mkdir(parents=True, exist_ok=True)
  bundle = {
    "lineup_info": {
      "lineup_id": "CAN-BELLFIBE-MTL-X",
      "provider": "Bell Fibe TV",
      "market": "Montréal, QC",
    },
    "server_info": {
      "timezone": "America/Montreal",
      "xmltv_url": "http://upstream.example/xmltv.php?username=u&password=p",
    },
    "live_categories": [
      {"category_id": "1", "category_name": "Sports", "parent_id": 0}
    ],
    "live_streams": [
      {
        "num": 1108,
        "name": "RDS",
        "stream_type": "live",
        "stream_id": 410108,
        "epg_channel_id": "RDS.ca",
        "category_id": "1",
        "category_name": "Sports",
        "direct_source": "http://upstream.example/live/u/p/410108.ts",
        "guide": {"language": "fr"},
      }
    ],
  }
  (xt_dir / "bellhop_xtream_playlist.json").write_text(
    json.dumps(bundle), encoding="utf-8"
  )

  cfg_path = tmp_path / "auth_cfg.yaml"
  cfg_path.write_text(
    f"""\
paths:
  cache_dir: "{tmp_path / "cache"}"
  xtream_output_dir: "{xt_dir}"
web_server:
  enabled: true
  host: "127.0.0.1"
  port: 0
  username: "web_admin"
  password: "web_secret_password"
  session_ttl_hours: 24.0
xtream_server:
  enabled: true
  username: "xt_player"
  password: "xt_secret_password"
""",
    encoding="utf-8",
  )
  cfg = load_config(cfg_path)
  redacted = cfg.to_dict(redact_secrets=True)
  assert redacted["serve"]["web"]["username"] == "web_admin"
  assert redacted["serve"]["web"]["password"] == "***"
  assert redacted["serve"]["xtream"]["username"] == "xt_player"
  assert redacted["serve"]["xtream"]["password"] == "***"

  srv = UnifiedServeServer.from_config(cfg)
  await srv.start()
  base = f"http://127.0.0.1:{srv.bound_port}"

  try:
    async with TestHttpClient(
      base_url=base, timeout=5.0, follow_redirects=False
    ) as client:
      # 1. Unauthenticated browser request redirects to /login preserving stateful query string
      redir = await client.get("/?cat=1&view=timeline&ch=1108")
      assert redir.status_code == 302
      assert (
        redir.headers["location"]
        == "/login?next=%2F%3Fcat%3D1%26view%3Dtimeline%26ch%3D1108"
      )

      # Unauthenticated API & static requests are blocked with 401
      assert (await client.get("/api/lineup")).status_code == 401
      assert (await client.get("/api/stream/1108.ts")).status_code == 401
      assert (await client.get("/static/app.js")).status_code == 401

      # Login page & /api/auth/status are public
      login_page = await client.get("/login?next=%2F%3Fcat%3D1%26ch%3D1108")
      assert login_page.status_code == 200
      assert "Sign In" in login_page.text
      st_unauth = (await client.get("/api/auth/status")).json()
      assert st_unauth["auth_required"] is True
      assert st_unauth["authenticated"] is False

      # 2. Xtream credentials MUST NOT work for Web Guide login
      wrong_login = await client.post(
        "/api/auth/login",
        json={"username": "xt_player", "password": "xt_secret_password"},
      )
      assert wrong_login.status_code == 401

      # 3. Valid Web Guide login sets HttpOnly SameSite=Lax session cookie
      ok_login = await client.post(
        "/api/auth/login",
        json={"username": "web_admin", "password": "web_secret_password"},
      )
      assert ok_login.status_code == 200
      assert ok_login.json()["authenticated"] is True
      set_cookie = ok_login.headers.get("set-cookie", "")
      assert "bellhop_session=" in set_cookie
      assert "HttpOnly" in set_cookie
      assert "SameSite=Lax" in set_cookie
      assert "Max-Age=86400" in set_cookie

      # Authenticated session accesses Web Guide, static assets, and APIs cleanly
      assert (await client.get("/")).status_code == 200
      assert (await client.get("/static/app.js")).status_code == 200
      assert (await client.get("/api/lineup")).status_code == 200
      st_auth = (await client.get("/api/auth/status")).json()
      assert st_auth["authenticated"] is True
      assert st_auth["username"] == "web_admin"

      # 4. On the same port, Xtream API enforces its own distinct credentials
      assert (
        await client.get(
          "/player_api.php",
          params={"username": "web_admin", "password": "web_secret_password"},
        )
      ).json() == {"user_info": {"auth": 0}}
      xt_ok = (
        await client.get(
          "/player_api.php",
          params={"username": "xt_player", "password": "xt_secret_password"},
        )
      ).json()
      assert xt_ok["user_info"]["auth"] == 1

      # 5. Logout clears session cookie and revokes access immediately
      logout = await client.post("/api/auth/logout")
      assert logout.status_code == 200
      assert "Max-Age=0" in logout.headers.get("set-cookie", "")
      assert (await client.get("/api/lineup")).status_code == 401

    # 6. Visiting a specific deep-link URL with an invalid/expired token clears the bad cookie,
    #    redirects to /login?next=..., and form login redirects back to the exact original URL
    deep_target = "/?cat=1&q=T%C3%A9l%C3%A9&view=timeline&ch=1108"
    async with TestHttpClient(
      base_url=base, timeout=5.0, follow_redirects=False
    ) as bad_client:
      bad_client.cookies.set("bellhop_session", "invalid.tampered.token")
      r_redir = await bad_client.get(deep_target)
      assert r_redir.status_code == 302
      login_loc = r_redir.headers["location"]
      assert login_loc.startswith("/login?next=")
      assert "Max-Age=0" in r_redir.headers.get("set-cookie", "")

      r_login_page = await bad_client.get(login_loc)
      assert r_login_page.status_code == 200
      assert (
        'value="/?cat=1&amp;q=T%C3%A9l%C3%A9&amp;view=timeline&amp;ch=1108"'
        in r_login_page.text
      )

      # Submit HTML form login with preserved `next` -> 302 right back to exact original URL
      r_post = await bad_client.post(
        "/api/auth/login",
        data={
          "username": "web_admin",
          "password": "web_secret_password",
          "next": deep_target,
        },
      )
      assert r_post.status_code == 302
      assert r_post.headers["location"] == deep_target
      assert "bellhop_session=" in r_post.headers.get("set-cookie", "")

      # Following the redirect now loads the Web Guide at the exact deep-link URL
      r_final = await bad_client.get(r_post.headers["location"])
      assert r_final.status_code == 200
      assert "player-overlay" in r_final.text
      assert "settings-btn" in r_final.text
      assert "settings-overlay" in r_final.text
  finally:
    await srv.stop()


@pytest.mark.asyncio
async def test_settings_api_inspect_update_and_strict_credential_exclusion(
  tmp_path: Path,
) -> None:
  xt_dir = tmp_path / "output" / "xtream"
  xt_dir.mkdir(parents=True, exist_ok=True)
  bundle = {
    "lineup_info": {
      "lineup_id": "CAN-BELLFIBE-MTL-X",
      "provider": "Bell Fibe TV",
      "market": "Montréal, QC",
    },
    "server_info": {"timezone": "America/Montreal"},
    "live_categories": [
      {"category_id": "1", "category_name": "Sports", "parent_id": 0}
    ],
    "live_streams": [
      {
        "num": 1108,
        "name": "RDS",
        "stream_type": "live",
        "stream_id": 410108,
        "epg_channel_id": "RDS.ca",
        "category_id": "1",
        "category_name": "Sports",
        "direct_source": "http://upstream.example/live/u/p/410108.ts",
        "guide": {"language": "fr"},
      }
    ],
  }
  (xt_dir / "bellhop_xtream_playlist.json").write_text(
    json.dumps(bundle), encoding="utf-8"
  )

  cache_dir = tmp_path / "cache"
  state_dir = tmp_path / "state"
  mapping_path = state_dir / "channel_mapping.json"
  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"""\
# Tier-1 infrastructure settings (must survive PUT /api/config)
paths:
  cache_dir: "{cache_dir}"
  state_dir: "{state_dir}"
  mapping_path: "{mapping_path}"
  xtream_output_dir: "{xt_dir}"

web_server:
  enabled: true
  host: "127.0.0.1"
  port: 0
  settle_ms: 180
  session_ttl_hours: 168.0
  username: "web_admin"
  password: "web_secret_pass"
  trust_proxy_headers: true
  trusted_proxy_ips:
    - "127.0.0.1"
    - "172.16.0.0/12"

# Tier-2 operational settings
upstream_xtream:
  server_url: "http://upstream.example"
  username: "upstream_secret_user"
  password: "upstream_secret_pass"
  stream_format: "ts"  # inline format comment
  request_timeout_sec: 30.0
  refresh_hours: 24.0

xtream_server:
  enabled: true
  host: null
  port: null
  username: "xt_user"
  password: "xt_secret_pass"
  server_protocol: "http"

pipeline:
  lineup:
    backend: "yaml"
    lineup_id: "CAN-BELLFIBE-MTL-X"
    collapse_sd_dupes: true
    schedules_direct:
      username: "sd_secret_user"
      password: "sd_secret_pass"
  matching:
    upgrade_hysteresis_score: 5.0
  validation:
    enabled: true
    max_candidates_per_channel: 5
  epg:
    verify: true
    refresh_hours: 6.0
  output:
    m3u8_enabled: true
    xtream_enabled: true
    include_unmatched_comment: false
""",
    encoding="utf-8",
  )

  cfg = load_config(cfg_path)
  srv = UnifiedServeServer.from_config(cfg)
  await srv.start()
  base = f"http://127.0.0.1:{srv.bound_port}"

  def _collect_keys(obj: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
      for k, v in obj.items():
        keys.add(str(k))
        keys.update(_collect_keys(v))
    elif isinstance(obj, list):
      for item in obj:
        keys.update(_collect_keys(item))
    return keys

  try:
    async with TestHttpClient(base_url=base, timeout=5.0) as client:
      # 1. Unauthenticated /api/config is blocked with 401
      assert (await client.get("/api/config")).status_code == 401
      assert (await client.put("/api/config", json={})).status_code == 401

      # Sign in to Web Guide
      login_resp = await client.post(
        "/api/auth/login",
        json={"username": "web_admin", "password": "web_secret_pass"},
      )
      assert login_resp.status_code == 200

      # 2. GET /api/config exposes ONLY upstream_xtream, xtream_server, and pipeline
      #    with ZERO credentials, ZERO web_server settings, and ZERO filesystem paths/directories
      get_resp = await client.get("/api/config")
      assert get_resp.status_code == 200
      raw_text = get_resp.text
      for secret in (
        "upstream_secret_user",
        "upstream_secret_pass",
        "sd_secret_user",
        "sd_secret_pass",
        "web_admin",
        "web_secret_pass",
        "xt_user",
        "xt_secret_pass",
        str(cache_dir),
        str(state_dir),
        str(xt_dir),
      ):
        assert secret not in raw_text

      payload = get_resp.json()
      assert payload["available"] is True
      assert set(payload["config"].keys()) == {
        "upstream_xtream",
        "xtream_server",
        "pipeline",
      }

      all_keys = _collect_keys(payload["config"])
      forbidden_keys = {
        "username",
        "password",
        "session_secret",
        "web_server",
        "paths",
        "serve",
        "web",
        "settle_ms",
        "session_ttl_hours",
        "trust_proxy_headers",
        "trusted_proxy_ips",
        "cache_dir",
        "state_dir",
        "mapping_path",
        "file",
        "lineup_file",
        "playlist_path",
        "report_path",
        "dir",
        "xtream_output_dir",
        "seed_json",
        "xmltv_file",
      }
      assert forbidden_keys.isdisjoint(all_keys)

      # 3. PUT /api/config updates operational fields and ignores any injected
      #    web_server, paths, directory, or credential mutations
      put_resp = await client.put(
        "/api/config",
        json={
          "web_server": {
            "host": "9.9.9.9",
            "port": 9999,
            "settle_ms": 999,
            "session_ttl_hours": 1.0,
            "username": "HACKED",
            "password": "HACKED",
            "trust_proxy_headers": False,
            "trusted_proxy_ips": ["0.0.0.0/0"],
          },
          "paths": {
            "cache_dir": "/tmp/hacked_cache",
            "state_dir": "/tmp/hacked_state",
            "xtream_output_dir": "/tmp/hacked_xtream",
          },
          "upstream_xtream": {
            "stream_format": "m3u8",
            "refresh_hours": 12.0,
            "cache_dir": "/tmp/hacked",
            "username": "HACKED",
            "password": "HACKED",
          },
          "xtream_server": {
            "public_host": "tv.local",
            "server_protocol": "https",
            "username": "HACKED",
            "password": "HACKED",
          },
          "pipeline": {
            "lineup": {
              "collapse_sd_dupes": False,
              "exclude_audio": True,
              "file": "/tmp/hacked.yaml",
              "pinned_streams": {"1108": 410108},
              "excluded_channels": [1107, 1424],
              "schedules_direct": {
                "auto_add_lineup": True,
                "password": "HACKED",
              },
            },
            "matching": {"upgrade_hysteresis_score": 7.5},
            "validation": {"max_candidates_per_channel": 8},
            "epg": {"refresh_hours": 12.0, "xmltv_file": "/tmp/hacked.xml"},
            "output": {
              "m3u8_enabled": False,
              "include_unmatched_comment": True,
            },
          },
        },
      )
      assert put_resp.status_code == 200
      put_data = put_resp.json()
      assert put_data["saved"] is True
      assert put_data["restart_required"] is False
      assert put_data["config"]["upstream_xtream"]["stream_format"] == "m3u8"
      assert put_data["config"]["xtream_server"]["public_host"] == "tv.local"
      assert put_data["config"]["xtream_server"]["server_protocol"] == "https"
      assert (
        put_data["config"]["pipeline"]["lineup"]["collapse_sd_dupes"] is False
      )
      assert put_data["config"]["pipeline"]["lineup"]["pinned_streams"] == {
        "1108": 410108
      }
      assert put_data["config"]["pipeline"]["lineup"]["excluded_channels"] == [
        1107,
        1424,
      ]
      assert (
        put_data["config"]["pipeline"]["matching"]["upgrade_hysteresis_score"]
        == 7.5
      )
      assert (
        put_data["config"]["pipeline"]["validation"][
          "max_candidates_per_channel"
        ]
        == 8
      )
      assert put_data["config"]["pipeline"]["output"]["m3u8_enabled"] is False
      assert (
        put_data["config"]["pipeline"]["output"]["include_unmatched_comment"]
        is True
      )

      # Live runtime operational knobs applied immediately; web_server tuner untouched
      assert srv.web_app.tuner.settle_sec == pytest.approx(0.18)
      assert srv.web_app.epg_refresher is not None
      assert srv.web_app.epg_refresher.config.refresh_hours == pytest.approx(
        12.0
      )
      assert srv.xtream_app.public_host == "tv.local"
      assert srv.xtream_app.server_protocol == "https"

      # Reload from disk: ALL credentials, web_server settings, paths, AND comments were preserved!
      saved_yaml = cfg_path.read_text(encoding="utf-8")
      assert (
        "# Tier-1 infrastructure settings (must survive PUT /api/config)"
        in saved_yaml
      )
      assert "# Tier-2 operational settings" in saved_yaml
      assert "# inline format comment" in saved_yaml
      reloaded = load_config(cfg_path)
      assert reloaded.serve.web_host == "127.0.0.1"
      assert reloaded.serve.web_port == 0
      assert reloaded.serve.web.settle_ms == 180
      assert reloaded.serve.web.session_ttl_hours == pytest.approx(168.0)
      assert reloaded.xtream.cache_dir == cache_dir
      assert reloaded.state.state_dir == state_dir
      assert reloaded.state.mapping_path == mapping_path
      assert reloaded.output.xtream_dir == xt_dir
      assert reloaded.xtream.username == "upstream_secret_user"
      assert reloaded.xtream.password == "upstream_secret_pass"
      assert reloaded.lineup.schedules_direct.username == "sd_secret_user"
      assert reloaded.lineup.schedules_direct.password == "sd_secret_pass"
      assert reloaded.serve.web.username == "web_admin"
      assert reloaded.serve.web.password == "web_secret_pass"
      assert reloaded.serve.web.trust_proxy_headers is True
      assert reloaded.serve.web.trusted_proxy_ips == [
        "127.0.0.1",
        "172.16.0.0/12",
      ]
      assert reloaded.serve.xtream.username == "xt_user"
      assert reloaded.serve.xtream.password == "xt_secret_pass"

      # 4. Changing Xtream passthrough server port sets restart_required=True
      port_resp = await client.put(
        "/api/config",
        json={"xtream_server": {"port": 8081}},
      )
      assert port_resp.status_code == 200
      assert port_resp.json()["restart_required"] is True

      # 5. Invalid config values return 400 and leave config.yaml valid
      bad_resp = await client.put(
        "/api/config",
        json={"xtream_server": {"port": 99999}},
      )
      assert bad_resp.status_code == 400
      assert "65535" in bad_resp.json()["error"]
      assert load_config(cfg_path).serve.xtream.port == 8081
  finally:
    await srv.stop()
