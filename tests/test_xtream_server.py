"""Tests for the passthrough Xtream Codes API server (`bellhop serve`)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from bellhop.common.accounts import (
  STREAM_CREDENTIAL_ALPHABET,
  STREAM_CREDENTIAL_LENGTH,
  hash_web_password,
)
from bellhop.config import load_config
from bellhop.pipeline.runner import PipelineRunner, PipelineRunOptions
from bellhop.serve import UnifiedServeServer
from bellhop.xtream import (
  XtreamExportStore,
  XtreamPassthroughServer,
  fetch_xtream_catalog,
)
from tests.conftest import (
  await_until,
  make_test_account,
  write_minimal_xtream_export,
)
from tests.fake_xtream_server import (
  FakeProgram,
  FakeStreamDef,
  FakeXtreamServer,
)
from tests.http_mock import TestHttpClient, new_test_session


def _write_mini_lineup(path: Path) -> None:
  path.write_text(
    """\
lineup_id: CAN-BELLFIBE-MTL-X
provider: Bell Fibe TV
market: Montréal, QC
postal_prefix: H3B
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

    creds_path = tmp_path / "credentials.yaml"
    creds_path.write_text(
      f"""\
accounts:
  primary:
    email: "web_u@example.com"
    web_password_hash: "{hash_web_password("web_p")}"
    xtream_username: "client_alice"
    xtream_password: "client_secret_42"
    upstream_username: "{upstream.username}"
    upstream_password: "{upstream.password}"
    pipeline: true
""",
      encoding="utf-8",
    )

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
      f"""\
credentials_file: "{creds_path}"
paths:
  cache_dir: "cache"
  state_dir: "state"
  lineup_file: "{lineup_file}"
  report_path: "output/report.json"
  xtream_output_dir: "output/xtream"
web_server:
  host: "127.0.0.1"
  port: 0
upstream_xtream:
  server_url: "{upstream.base_url}"
  stream_format: "ts"
xtream_server:
  enabled: true
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
""",
      encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    redacted = cfg.to_dict(redact_secrets=True)
    assert redacted["accounts"]["primary"]["xtream_username"] == "client_alice"
    assert redacted["accounts"]["primary"]["xtream_password"] == "***"
    assert redacted["accounts"]["primary"]["upstream_password"] == "***"

    # 1. Run pipeline to produce calculated lineup categories & streams
    run_res = await PipelineRunner(cfg).run(PipelineRunOptions())
    assert run_res.xtream_result is not None
    assert run_res.xtream_result.epg_verified is True

    # 2. Launch unified `bellhop serve` (Web Guide + Xtream API on the same
    #    port!)
    unified_srv = UnifiedServeServer.from_config(cfg)
    assert unified_srv.shared_listener is True
    await unified_srv.start()
    serve_base = f"http://127.0.0.1:{unified_srv.bound_port}"

    try:
      async with TestHttpClient(
        base_url=serve_base, timeout=10.0, follow_redirects=False
      ) as client:
        # Unified /health (unauthenticated: no active_tuner or lineup leak)
        health = (await client.get("/health")).json()
        assert health["status"] == "ok"
        assert health["web_guide_enabled"] is True
        assert health["xtream_api_enabled"] is True
        assert health["category_count"] == 2
        assert health["channel_count"] == 2
        assert health["active_tuner"] is None
        assert "lineup_id" not in health
        assert "provider" not in health
        assert "market" not in health

        # Unauthenticated Web Guide routes require login; sign in first
        assert (await client.get("/api/lineup")).status_code == 401
        login_ok = await client.post(
          "/api/auth/login",
          json={"email": "web_u@example.com", "password": "web_p"},
        )
        assert login_ok.status_code == 200

        authed_health = (await client.get("/health")).json()
        assert authed_health["lineup_id"] == "CAN-BELLFIBE-MTL-X"
        assert authed_health["provider"] == "Bell Fibe TV"
        assert authed_health["market"] == "Montréal, QC"

        # --- Web Guide routes on the same port ---
        idx = await client.get("/?cat=2&view=timeline&ch=1108")
        assert idx.status_code == 200
        assert "player-overlay" in idx.text
        assert (await client.get("/static/app.js")).status_code == 200

        # Wait for automatic dynamic EPG fetch into the Web Guide's disk cache
        async def _epg_ready() -> bool:
          api_health = (await client.get("/api/health")).json()
          return bool(
            api_health["epg_status"]["state"] == "ok"
            and api_health["epg_active_program_count"] > 0
          )

        await await_until(
          _epg_ready, description="initial EPG SQLite hydration"
        )
        assert (tmp_path / "cache" / "epg.sqlite3").is_file()

        lu = (await client.get("/api/lineup")).json()
        assert "upstream_secret_pw" not in json.dumps(lu)
        ch_ici = next(c for c in lu["channels"] if c["chno"] == 1101)
        assert ch_ici["now_playing"]["title"] == "Le Téléjournal"

        # Xtream `get_short_epg` serves SQLite listings (base64 UTF-8
        # title/description)
        short_epg = (
          await client.get(
            "/player_api.php",
            params={
              "username": "client_alice",
              "password": "client_secret_42",
              "action": "get_short_epg",
              "stream_id": "410101",
            },
          )
        ).json()
        assert len(short_epg["epg_listings"]) >= 1
        import base64 as _b64

        assert (
          _b64.b64decode(short_epg["epg_listings"][0]["title"]).decode("utf-8")
          == "Le Téléjournal"
        )

        # Critical routing check: `/api/stream/1101.ts` MUST hit WebGuideApp
        # (200 TS stream),
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
          assert "Access-Control-Allow-Origin" not in resp.headers
          assert resp.headers.get("Referrer-Policy") == "no-referrer"
          body = resp.json()
          assert "upstream_secret_pw" not in resp.text
          assert body["user_info"]["auth"] == 1
          assert body["user_info"]["username"] == "client_alice"
          assert body["server_info"]["url"] == "127.0.0.1"
          assert body["server_info"]["port"] == str(unified_srv.bound_port)
          assert body["server_info"]["xmltv_url"] == (
            f"http://127.0.0.1:{unified_srv.bound_port}"
            "/xmltv.php?username=client_alice&password=client_secret_42"
          )

        # Categories reflect lineup retrieval groups (NOT upstream "RAW|
        # UNSORTED")
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
        assert panel_json["server_info"]["xmltv_url"] == (
          f"http://127.0.0.1:{unified_srv.bound_port}"
          "/xmltv.php?username=client_alice&password=client_secret_42"
        )
        assert "upstream_secret_pw" not in panel.text

        # Auth-checked non-live routes return 401 on bad creds and 404 on valid
        # creds
        assert (
          await client.get("/movie/client_alice/wrong/123.mp4")
        ).status_code == 401
        assert (
          await client.get("/movie/client_alice/client_secret_42/123.mp4")
        ).status_code == 404

        # XMLTV endpoint (+ /epg.xml alias) -> served locally (200) from SQLite
        # with zero upstream secret leak!
        for epg_path in ("/xmltv.php", "/epg.xml"):
          xmltv_local = await client.get(
            epg_path,
            params={"username": "client_alice", "password": "client_secret_42"},
          )
          assert xmltv_local.status_code == 200
          assert "Access-Control-Allow-Origin" not in xmltv_local.headers
          assert xmltv_local.headers.get("Referrer-Policy") == "no-referrer"
          assert "Le Téléjournal" in xmltv_local.text
          assert "upstream_secret_pw" not in xmltv_local.text
        head_xmltv = await client.head(
          "/xmltv.php",
          params={"username": "client_alice", "password": "client_secret_42"},
        )
        assert head_xmltv.status_code == 200

        # When SQLite EPG database is empty OR holds only expired listings,
        # /xmltv.php gracefully falls back to 302 upstream redirect!
        assert unified_srv.web_app is not None
        assert unified_srv.web_app.epg_refresher is not None
        unified_srv.web_app.epg_refresher.db.delete_database()
        xmltv_redir = await client.get(
          "/xmltv.php",
          params={"username": "client_alice", "password": "client_secret_42"},
        )
        assert xmltv_redir.status_code == 302
        assert xmltv_redir.headers["location"] == upstream.xmltv_url

        from bellhop.web.store import ProgramItem

        unified_srv.web_app.epg_refresher.db.replace_guide_schedules(
          snap=unified_srv.web_app.store.get_snapshot(),
          programs_by_id={
            "RDS.ca": [
              ProgramItem(
                id="expired-1",
                channel_id="RDS.ca",
                title="Expired Show",
                start_timestamp=100,
                stop_timestamp=200,
              )
            ]
          },
          fetched_at=100.0,
        )
        xmltv_expired_redir = await client.get(
          "/xmltv.php",
          params={"username": "client_alice", "password": "client_secret_42"},
        )
        assert xmltv_expired_redir.status_code == 302
        assert xmltv_expired_redir.headers["location"] == upstream.xmltv_url

        unified_srv.web_app.epg_refresher.db.delete_database()
        await unified_srv.web_app.epg_refresher.refresh_now(
          force=True, reason="restore"
        )

        # Stream endpoints -> 302 passthrough redirects (TS, HLS, and shorthand
        # without /live)
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

        # /get.php M3U modes: m3u_plus (ts & hls), basic m3u, category filter,
        # HEAD, & /playlist.m3u8
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

        m3u_default = await client.get(
          "/playlist.m3u",
          params={"username": "client_alice", "password": "client_secret_42"},
        )
        assert m3u_default.status_code == 200
        assert (
          f'url-tvg="http://127.0.0.1:{unified_srv.bound_port}'
          '/xmltv.php?username=client_alice&password=client_secret_42"'
        ) in m3u_default.text
        assert 'tvg-id="RDS.ca"' in m3u_default.text

      # Follow redirects end-to-end: XMLTV and live TS bytes come directly from
      # upstream
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
        roundtrip_acct = make_test_account(
          upstream_username="client_alice",
          upstream_password="client_secret_42",
        )
        roundtrip_cat = await fetch_xtream_catalog(
          cfg_client, account=roundtrip_acct, force_refresh=True
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
    "server_info": {"timezone": "UTC"},
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

  base_creds_path = tmp_path / "credentials.yaml"
  base_creds_path.write_text(
    f"""\
accounts:
  primary:
    email: "web_u@example.com"
    web_password_hash: "{hash_web_password("web_p")}"
    xtream_username: "xt_user"
    xtream_password: "xt_pass"
    upstream_username: "up_user"
    upstream_password: "up_pass"
    pipeline: true
""",
    encoding="utf-8",
  )

  # Default config binds 0.0.0.0:8080
  cfg_default_path = tmp_path / "default_cfg.yaml"
  cfg_default_path.write_text(
    f'credentials_file: "{base_creds_path}"\n'
    "upstream_xtream:\n  server_url: http://example.invalid\n",
    encoding="utf-8",
  )
  cfg_default = load_config(cfg_default_path)
  assert cfg_default.serve.host == "0.0.0.0"
  assert cfg_default.serve.port == 8080

  # Empty accounts in credentials.yaml MUST fail fast with ValueError
  empty_creds_path = tmp_path / "empty_credentials.yaml"
  empty_creds_path.write_text("accounts: {}\n", encoding="utf-8")
  cfg_no_web_creds_path = tmp_path / "no_web_creds.yaml"
  cfg_no_web_creds_path.write_text(
    f"""\
credentials_file: "{empty_creds_path}"
paths:
  xtream_output_dir: "{xt_dir}"
web_server:
  enabled: true
  host: "127.0.0.1"
  port: 0
""",
    encoding="utf-8",
  )
  with pytest.raises(ValueError, match="accounts"):
    load_config(cfg_no_web_creds_path)

  # Custom bind address/port with xtream_server disabled: Web Guide works,
  # Xtream API disabled
  cfg_custom_path = tmp_path / "custom_cfg.yaml"
  cfg_custom_path.write_text(
    f"""\
credentials_file: "{base_creds_path}"
paths:
  cache_dir: "{tmp_path / "cache"}"
  xtream_output_dir: "{xt_dir}"
web_server:
  host: "127.0.0.1"
  port: 0
xtream_server:
  enabled: false
""",
    encoding="utf-8",
  )
  cfg_custom = load_config(cfg_custom_path)
  srv = UnifiedServeServer.from_config(cfg_custom)
  await srv.start()
  try:
    async with TestHttpClient(
      base_url=f"http://127.0.0.1:{srv.bound_port}",
      timeout=5.0,
      follow_redirects=False,
    ) as client:
      health = (await client.get("/health")).json()
      assert health["web_guide_enabled"] is True
      assert health["xtream_api_enabled"] is False
      assert (await client.get("/")).status_code == 302
      assert (
        await client.post(
          "/api/auth/login",
          json={"email": "web_u@example.com", "password": "web_p"},
        )
      ).status_code == 200
      assert (await client.get("/")).status_code == 200
      assert (await client.get("/api/lineup")).status_code == 200
      assert (await client.get("/player_api.php")).status_code == 404
      assert (await client.get("/xmltv.php")).status_code == 404
  finally:
    await srv.stop()

  # XtreamPassthroughServer with zero accounts locks all Xtream routes.
  locked_xt = XtreamPassthroughServer(
    XtreamExportStore(xt_dir), accounts=(), host="127.0.0.1", port=0
  )
  await locked_xt.start()
  try:
    async with TestHttpClient(
      base_url=f"http://127.0.0.1:{locked_xt.bound_port}",
      timeout=5.0,
    ) as xt_cli:
      assert (await xt_cli.get("/player_api.php")).json() == {
        "user_info": {"auth": 0}
      }
      assert (await xt_cli.get("/xmltv.php")).status_code == 401
  finally:
    await locked_xt.stop()


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
      "timezone": "UTC",
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

  # Pick two distinct free ports to verify explicit separate-port configuration
  # in config.yaml
  import socket

  def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
      s.bind(("127.0.0.1", 0))
      return int(s.getsockname()[1])

  web_p = _free_port()
  xt_p = _free_port()
  while xt_p == web_p:
    xt_p = _free_port()

  creds_path = tmp_path / "split_creds.yaml"
  creds_path.write_text(
    f"""\
accounts:
  split_acct:
    email: "web_u@example.com"
    web_password_hash: "{hash_web_password("web_p")}"
    xtream_username: "split_user"
    xtream_password: "split_password"
    upstream_username: "u"
    upstream_password: "p"
    pipeline: true
""",
    encoding="utf-8",
  )

  cfg_path = tmp_path / "split_cfg.yaml"
  cfg_path.write_text(
    f"""\
credentials_file: "{creds_path}"
paths:
  cache_dir: "{tmp_path / "cache"}"
  xtream_output_dir: "{xt_dir}"
web_server:
  enabled: true
  host: "127.0.0.1"
  port: {web_p}
xtream_server:
  enabled: true
  port: {xt_p}
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
      assert (
        await client.post(
          f"{web_base}/api/auth/login",
          json={"email": "web_u@example.com", "password": "web_p"},
        )
      ).status_code == 200
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

      # Repeated bad Xtream credentials trigger 429 LoginRateLimiter brute-force
      # protection
      for _ in range(10):
        await client.get(
          f"{xt_base}/xmltv.php",
          params={"username": "split_user", "password": "wrong_password"},
        )
      throttled = await client.get(
        f"{xt_base}/xmltv.php",
        params={"username": "split_user", "password": "split_password"},
      )
      assert throttled.status_code == 429
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
      "timezone": "UTC",
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

  creds_path = tmp_path / "auth_creds.yaml"
  creds_path.write_text(
    f"""\
accounts:
  web_admin:
    email: "web_admin@example.com"
    web_password_hash: "{hash_web_password("web_secret_password")}"
    xtream_username: "xt_player"
    xtream_password: "xt_secret_password"
    upstream_username: "u"
    upstream_password: "p"
    pipeline: true
""",
    encoding="utf-8",
  )

  cfg_path = tmp_path / "auth_cfg.yaml"
  cfg_path.write_text(
    f"""\
credentials_file: "{creds_path}"
paths:
  cache_dir: "{tmp_path / "cache"}"
  xtream_output_dir: "{xt_dir}"
web_server:
  enabled: true
  host: "127.0.0.1"
  port: 0
  session_ttl_hours: 24.0
xtream_server:
  enabled: true
""",
    encoding="utf-8",
  )
  cfg = load_config(cfg_path)
  redacted = cfg.to_dict(redact_secrets=True)
  assert redacted["accounts"]["web_admin"]["email"] == "web_admin@example.com"
  assert redacted["accounts"]["web_admin"]["web_password_hash"] == "***"
  assert redacted["accounts"]["web_admin"]["xtream_username"] == "xt_player"
  assert redacted["accounts"]["web_admin"]["xtream_password"] == "***"

  srv = UnifiedServeServer.from_config(cfg)
  await srv.start()
  base = f"http://127.0.0.1:{srv.bound_port}"

  try:
    async with TestHttpClient(
      base_url=base, timeout=5.0, follow_redirects=False
    ) as client:
      # 1. Unauthenticated browser request redirects to /login preserving
      #    stateful query string
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
        json={
          "email": "xt_player@example.com",
          "password": "xt_secret_password",
        },
      )
      assert wrong_login.status_code == 401

      # 3. Valid Web Guide login sets HttpOnly SameSite=Lax session cookie
      ok_login = await client.post(
        "/api/auth/login",
        json={
          "email": "web_admin@example.com",
          "password": "web_secret_password",
        },
      )
      assert ok_login.status_code == 200
      assert ok_login.json()["authenticated"] is True
      assert ok_login.json()["account_id"] == "web_admin"
      assert ok_login.json()["email"] == "web_admin@example.com"
      set_cookie = ok_login.headers.get("set-cookie", "")
      assert "bellhop_session=" in set_cookie
      assert "HttpOnly" in set_cookie
      assert "SameSite=Lax" in set_cookie
      assert "Max-Age=86400" in set_cookie

      # Authenticated session accesses Web Guide, static assets, and APIs
      # cleanly
      assert (await client.get("/")).status_code == 200
      assert (await client.get("/static/app.js")).status_code == 200
      assert (await client.get("/api/lineup")).status_code == 200
      st_auth = (await client.get("/api/auth/status")).json()
      assert st_auth["authenticated"] is True
      assert st_auth["account_id"] == "web_admin"
      assert st_auth["email"] == "web_admin@example.com"

      # 4. On the same port, Xtream API enforces its own distinct credentials
      assert (
        await client.get(
          "/player_api.php",
          params={
            "username": "web_admin@example.com",
            "password": "web_secret_password",
          },
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

    # 6. Visiting a specific deep-link URL with an invalid/expired token clears
    #    the bad cookie,
    # redirects to /login?next=..., and form login redirects back to the exact
    # original URL
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

      # Submit HTML form login with preserved `next` -> 302 right back to exact
      # original URL
      r_post = await bad_client.post(
        "/api/auth/login",
        data={
          "email": "web_admin@example.com",
          "password": "web_secret_password",
          "next": deep_target,
        },
      )
      assert r_post.status_code == 302
      assert r_post.headers["location"] == deep_target
      assert "bellhop_session=" in r_post.headers.get("set-cookie", "")

      # Following the redirect now loads the Web Guide at the exact deep-link
      # URL
      r_final = await bad_client.get(r_post.headers["location"])
      assert r_final.status_code == 200
      assert "player-overlay" in r_final.text
      assert "settings-btn" in r_final.text
      assert "settings-overlay" in r_final.text
  finally:
    await srv.stop()


@pytest.mark.asyncio
async def test_account_api_credential_rotation_preserves_comments_and_isolates_accounts(
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
    "server_info": {"timezone": "UTC"},
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
        "direct_source": "",
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
  creds_path = tmp_path / "credentials.yaml"
  creds_path.write_text(
    f"""\
# Account credentials header comment
accounts:
  web_admin:
    email: "web_admin@example.com"
    web_password_hash: "{hash_web_password("web_secret_pass")}"
    xtream_username: "xt_user"  # custom user comment
    xtream_password: "xt_secret_pass"
    upstream_username: "upstream_secret_user"
    upstream_password: "upstream_secret_pass"
    pipeline: true
  bob:
    email: "bob@example.com"
    web_password_hash: "{hash_web_password("bob_secret_pass")}"
    xtream_username: "bobxt001"
    xtream_password: "bobxtpw1"
    upstream_username: "bob_upstream"
    upstream_password: "bob_upstream_pw"
    pipeline: false

schedules_direct:
  username: "sd_secret_user"
  password: "sd_secret_pass"
""",
    encoding="utf-8",
  )

  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"""\
credentials_file: "{creds_path}"
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
  trust_proxy_headers: true
  trusted_proxy_ips:
    - "127.0.0.1"
    - "172.16.0.0/12"

upstream_xtream:
  server_url: "http://upstream.example"
  stream_format: "ts"
  request_timeout_sec: 30.0
  refresh_hours: 24.0

xtream_server:
  enabled: true
  host: null
  port: null
  server_protocol: "http"
""",
    encoding="utf-8",
  )

  cfg = load_config(cfg_path)
  srv = UnifiedServeServer.from_config(cfg)
  await srv.start()
  base = f"http://127.0.0.1:{srv.bound_port}"

  try:
    async with TestHttpClient(base_url=base, timeout=5.0) as client:
      # 1. Removed /api/config requires auth (401) and 404s when signed in.
      assert (await client.get("/api/account")).status_code == 401
      assert (await client.get("/api/config")).status_code == 401

      login_resp = await client.post(
        "/api/auth/login",
        json={"email": "web_admin@example.com", "password": "web_secret_pass"},
      )
      assert login_resp.status_code == 200
      assert (await client.get("/api/config")).status_code == 404

      # 2. GET /api/account exposes ONLY current account info + player URLs,
      #    NEVER upstream creds, SD creds, sibling accounts, or paths
      get_resp = await client.get("/api/account")
      assert get_resp.status_code == 200
      raw_text = get_resp.text
      for hidden in (
        "upstream_secret_user",
        "upstream_secret_pass",
        "sd_secret_user",
        "sd_secret_pass",
        "web_secret_pass",
        "bob@example.com",
        "bobxt001",
        "bob_upstream_pw",
        str(cache_dir),
        str(state_dir),
        str(xt_dir),
      ):
        assert hidden not in raw_text

      acct_payload = get_resp.json()
      assert acct_payload["account_id"] == "web_admin"
      assert acct_payload["email"] == "web_admin@example.com"
      assert acct_payload["xtream_username"] == "xt_user"
      assert acct_payload["xtream_password"] == "xt_secret_pass"
      assert "xt_user" in acct_payload["player_urls"]["m3u_url"]

      # 3. Regenerate Bellhop stream creds updates live Xtream server & disk
      #    while keeping sibling account intact and preserving YAML comments!
      regen_resp = await client.post(
        "/api/account/stream-credentials/regenerate", json={}
      )
      assert regen_resp.status_code == 200
      regen_data = regen_resp.json()
      new_xt_u = regen_data["xtream_username"]
      new_xt_p = regen_data["xtream_password"]
      assert new_xt_u != "xt_user"
      assert new_xt_p != "xt_secret_pass"
      assert len(new_xt_u) == STREAM_CREDENTIAL_LENGTH
      assert len(new_xt_p) == STREAM_CREDENTIAL_LENGTH
      assert all(c in STREAM_CREDENTIAL_ALPHABET for c in (new_xt_u + new_xt_p))

      # Live Xtream server rejects old creds and accepts new creds + Bob's creds
      assert (
        await client.get(
          "/player_api.php",
          params={"username": "xt_user", "password": "xt_secret_pass"},
        )
      ).json() == {"user_info": {"auth": 0}}
      assert (
        await client.get(
          "/player_api.php",
          params={"username": new_xt_u, "password": new_xt_p},
        )
      ).json()["user_info"]["auth"] == 1
      assert (
        await client.get(
          "/player_api.php",
          params={"username": "bobxt001", "password": "bobxtpw1"},
        )
      ).json()["user_info"]["auth"] == 1

      assert srv.web_app is not None
      assert srv.web_app.tuner_for("web_admin").settle_sec == pytest.approx(
        0.18
      )

      saved_creds = creds_path.read_text(encoding="utf-8")
      assert "# Account credentials header comment" in saved_creds

      # 4. Non-JSON Content-Type -> 415, wrong current password -> 403, short
      #    password -> 400, valid update -> 200
      wrong_ct = await client.post(
        "/api/account/password",
        data=b"{}",
        headers={"Content-Type": "text/plain"},
      )
      assert wrong_ct.status_code == 415

      wrong_pw = await client.post(
        "/api/account/password",
        json={
          "current_password": "wrong_current",
          "new_password": "new_valid_password",
        },
      )
      assert wrong_pw.status_code == 403

      short_pw = await client.post(
        "/api/account/password",
        json={
          "current_password": "web_secret_pass",
          "new_password": "tiny",
        },
      )
      assert short_pw.status_code == 400

      ok_pw = await client.post(
        "/api/account/password",
        json={
          "current_password": "web_secret_pass",
          "new_password": "brand_new_web_password",
        },
      )
      assert ok_pw.status_code == 200
      assert ok_pw.json() == {"ok": True, "email": "web_admin@example.com"}

    # Verify new password authenticates cleanly in fresh session
    async with TestHttpClient(base_url=base, timeout=5.0) as fresh:
      assert (
        await fresh.post(
          "/api/auth/login",
          json={
            "email": "web_admin@example.com",
            "password": "web_secret_pass",
          },
        )
      ).status_code == 401
      assert (
        await fresh.post(
          "/api/auth/login",
          json={
            "email": "web_admin@example.com",
            "password": "brand_new_web_password",
          },
        )
      ).status_code == 200
  finally:
    await srv.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ("action", "raw_limit", "expected_limit"),
  [
    ("get_short_epg", "", 12),
    ("get_short_epg", "not_a_number", 12),
    ("get_short_epg", "0", 1),
    ("get_short_epg", "-7", 1),
    ("get_short_epg", "9999", 200),
    ("get_simple_data_table", "", 50),
    ("get_simple_data_table", "-1", 1),
    ("get_simple_data_table", "500", 200),
  ],
)
async def test_short_epg_limit_clamping_and_unknown_stream_ids(
  tmp_path: Path,
  action: str,
  raw_limit: str,
  expected_limit: int,
) -> None:
  """Verify `get_short_epg` / `get_simple_data_table` limits and IDs."""
  xt_dir = write_minimal_xtream_export(tmp_path / "output" / "xtream")
  observed_limits: list[int] = []

  async def _fake_short_provider(
    chno: int, epg_id: str, lang: str, limit: int
  ) -> list[dict[str, object]]:
    observed_limits.append(limit)
    return [{"chno": chno, "epg_id": epg_id, "lang": lang, "limit": limit}]

  srv = XtreamPassthroughServer(
    XtreamExportStore(xt_dir),
    accounts=(make_test_account(xtream_username="u", xtream_password="p"),),
    host="127.0.0.1",
    port=0,
    epg_short_provider=_fake_short_provider,
  )
  await srv.start()
  try:
    async with TestHttpClient(
      base_url=f"http://127.0.0.1:{srv.bound_port}"
    ) as client:
      params = {
        "username": "u",
        "password": "p",
        "action": action,
        "stream_id": "410101",
      }
      if raw_limit != "":
        params["limit"] = raw_limit
      ok_resp = await client.get("/player_api.php", params=params)
      assert ok_resp.status_code == 200
      assert observed_limits == [expected_limit]
      assert ok_resp.json()["epg_listings"][0]["limit"] == expected_limit

      for bad_sid in ("999999", "not_numeric", ""):
        bad_resp = await client.get(
          "/player_api.php",
          params={
            "username": "u",
            "password": "p",
            "action": action,
            "stream_id": bad_sid,
          },
        )
        assert bad_resp.status_code == 200
        assert bad_resp.json() == {"epg_listings": []}
  finally:
    await srv.stop()


@pytest.mark.asyncio
async def test_xmltv_head_and_small_chunk_streaming_and_xtream_only_health(
  tmp_path: Path,
) -> None:
  """Verify `HEAD /xmltv.php`, streamed `GET`, & Xtream-only `/health`."""
  from collections.abc import Iterator

  from bellhop.config import (
    AppConfig,
    EpgConfig,
    OutputConfig,
    ServeConfig,
    ServeWebConfig,
    ServeXtreamConfig,
    XtreamConfig,
  )
  from bellhop.epg.db import EpgDatabase
  from bellhop.epg.models import ProgramItem
  from bellhop.web.store import LineupGuideStore

  xt_dir = write_minimal_xtream_export(tmp_path / "output" / "xtream")
  store = LineupGuideStore(xt_dir)
  snap = store.get_snapshot()

  db = EpgDatabase(tmp_path / "cache" / "epg.sqlite3")
  now_ts = 1_763_510_400
  db.replace_guide_schedules(
    snap=snap,
    programs_by_id={
      "ICIRDI.ca": [
        ProgramItem(
          id=f"rdi-{i}",
          channel_id="ICIRDI.ca",
          title=f"Téléjournal Segment {i}",
          description="Bulletin complet d'information.",
          start_timestamp=now_ts + i * 1800,
          stop_timestamp=now_ts + (i + 1) * 1800,
        )
        for i in range(4)
      ]
    },
    fetched_at=float(now_ts),
  )

  closed_box = {"count": 0}

  class _ClosableXmltvChunks:
    def __init__(self, inner: Iterator[bytes]) -> None:
      self._inner = inner

    def __iter__(self) -> Iterator[bytes]:
      return self

    def __next__(self) -> bytes:
      return next(self._inner)

    def close(self) -> None:
      close_fn = getattr(self._inner, "close", None)
      if callable(close_fn):
        close_fn()
      closed_box["count"] += 1

  async def _tiny_chunks_provider() -> Iterator[bytes] | None:
    return _ClosableXmltvChunks(
      db.iter_xmltv_chunks(snap, chunk_target_bytes=64, page_rows=2)
    )

  xt_srv = XtreamPassthroughServer(
    XtreamExportStore(xt_dir),
    accounts=(
      make_test_account(xtream_username="xt_u", xtream_password="xt_p"),
    ),
    host="127.0.0.1",
    port=0,
    epg_xmltv_chunks_provider=_tiny_chunks_provider,
  )
  await xt_srv.start()
  try:
    base_url = f"http://127.0.0.1:{xt_srv.bound_port}"
    async with new_test_session() as session:
      # 1. `HEAD /xmltv.php` returns 200 XML headers, empty body, and closes
      #    generator cleanly
      async with session.head(
        f"{base_url}/xmltv.php",
        params={"username": "xt_u", "password": "xt_p"},
      ) as r_head:
        assert r_head.status == 200
        assert "application/xml" in r_head.headers.get("Content-Type", "")
        assert await r_head.read() == b""
      assert closed_box["count"] == 1

      # 2. `GET /xmltv.php` flushes multiple 64-byte chunks into complete valid
      #    XMLTV
      async with session.get(
        f"{base_url}/xmltv.php",
        params={"username": "xt_u", "password": "xt_p"},
      ) as r_get:
        assert r_get.status == 200
        body = await r_get.text()
        assert "Téléjournal Segment 0" in body
        assert "Téléjournal Segment 3" in body
      await await_until(
        lambda: closed_box["count"] == 2,
        description="XMLTV chunk iterator close after EOF",
      )
  finally:
    await xt_srv.stop()

  # 3. Xtream-only `UnifiedServeServer` (`web.enabled=False`) serves `/health`
  #    cleanly
  cfg_xt_only = AppConfig(
    project_root=tmp_path,
    accounts=(
      make_test_account(
        xtream_username="xt_u",
        xtream_password="xt_p",
        upstream_username="up",
        upstream_password="pw",
      ),
    ),
    xtream=XtreamConfig(
      server_url="http://panel.example",
      cache_dir=Path("cache"),
    ),
    epg=EpgConfig(verify=False, refresh_hours=6.0),
    output=OutputConfig(xtream_dir=Path("output/xtream")),
    serve=ServeConfig(
      web=ServeWebConfig(enabled=False, host="127.0.0.1", port=0),
      xtream=ServeXtreamConfig(
        enabled=True,
        host="127.0.0.1",
        port=0,
      ),
    ),
  )
  unified = UnifiedServeServer.from_config(cfg_xt_only)
  assert unified.web_app is None
  assert not (tmp_path / "cache" / "session_secret.key").exists()
  await unified.start()
  try:
    async with TestHttpClient(
      base_url=f"http://127.0.0.1:{unified.xtream_bound_port}"
    ) as client:
      health = (await client.get("/health")).json()
      assert health["status"] == "ok"
      assert health["web_guide_enabled"] is False
      assert health["web_auth_enabled"] is False
      assert health["xtream_api_enabled"] is True
      assert health["shared_listener"] is False
      assert health["active_tuner"] is None
      assert health["channel_count"] == 2
      assert "lineup_id" not in health
      assert "provider" not in health
      assert "market" not in health
      opt_xt = await client.request("OPTIONS", "/player_api.php")
      assert opt_xt.status_code == 405
      assert "Access-Control-Allow-Origin" not in opt_xt.headers
  finally:
    await unified.stop()


@pytest.mark.asyncio
async def test_xmltv_fallback_blocks_self_redirect_loop(
  tmp_path: Path,
) -> None:
  """Self-referential fallback XMLTV URLs must return 404 instead of looping."""
  xt_dir = write_minimal_xtream_export(tmp_path / "output" / "xtream")
  srv = XtreamPassthroughServer(
    XtreamExportStore(xt_dir),
    accounts=(
      make_test_account(xtream_username="xt_u", xtream_password="xt_p"),
    ),
    host="127.0.0.1",
    port=0,
  )
  await srv.start()
  try:
    bundle_path = xt_dir / "bellhop_xtream_playlist.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["server_info"]["xmltv_url"] = (
      f"http://127.0.0.1:{srv.bound_port}/xmltv.php?username=xt_u&password=xt_p"
    )
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    async with TestHttpClient(
      base_url=f"http://127.0.0.1:{srv.bound_port}"
    ) as client:
      resp = await client.get(
        "/xmltv.php",
        params={"username": "xt_u", "password": "xt_p"},
      )
      assert resp.status_code == 404
  finally:
    await srv.stop()


@pytest.mark.asyncio
async def test_multi_credential_xtream_passthrough_routing_and_concurrency(
  tmp_path: Path,
) -> None:
  """Verify multi-account Xtream routing, dynamic M3U8, & concurrency."""
  upstream = FakeXtreamServer(
    username="up_a",
    password="up_pw_a",
    extra_accounts={"up_b": "up_pw_b"},
    max_connections=1,
    enforce_max_connections=True,
    ts_chunks_per_stream=2,
    ts_chunk_interval_sec=0.05,
    categories=[
      {"category_id": "1", "category_name": "General", "parent_id": 0}
    ],
    streams=[
      FakeStreamDef(
        stream_id=410101,
        name="ICI",
        category_id="1",
        epg_channel_id="CBFT.ca",
        marker_byte=0xA1,
      ),
      FakeStreamDef(
        stream_id=410108,
        name="RDS",
        category_id="1",
        epg_channel_id="RDS.ca",
        marker_byte=0xB8,
      ),
    ],
  )
  async with upstream:
    xt_dir = tmp_path / "output" / "xtream"
    xt_dir.mkdir(parents=True)
    (xt_dir / "bellhop_xtream_playlist.json").write_text(
      json.dumps(
        {
          "lineup_info": {"lineup_id": "TEST", "provider": "Bell Fibe TV"},
          "server_info": {"timezone": "UTC", "xmltv_url": ""},
          "live_categories": [
            {"category_id": "1", "category_name": "General", "parent_id": 0}
          ],
          "live_streams": [
            {
              "num": 1101,
              "name": "ICI",
              "stream_type": "live",
              "stream_id": 410101,
              "epg_channel_id": "CBFT.ca",
              "category_id": "1",
              "category_name": "General",
              "direct_source": "",
            },
            {
              "num": 1108,
              "name": "RDS",
              "stream_type": "live",
              "stream_id": 410108,
              "epg_channel_id": "RDS.ca",
              "category_id": "1",
              "category_name": "General",
              "direct_source": "",
            },
          ],
        }
      ),
      encoding="utf-8",
    )

    acct_a = make_test_account(
      account_id="alice",
      email="alice@example.com",
      xtream_username="xt_a",
      xtream_password="pw_a",
      upstream_username="up_a",
      upstream_password="up_pw_a",
      pipeline=True,
    )
    acct_b = make_test_account(
      account_id="bob",
      email="bob@example.com",
      xtream_username="xt_b",
      xtream_password="pw_b",
      upstream_username="up_b",
      upstream_password="up_pw_b",
      pipeline=False,
    )
    srv = XtreamPassthroughServer(
      XtreamExportStore(xt_dir),
      accounts=(acct_a, acct_b),
      upstream_server_url=upstream.base_url,
      upstream_stream_format="ts",
      host="127.0.0.1",
      port=0,
    )
    await srv.start()
    base_url = f"http://127.0.0.1:{srv.bound_port}"
    try:
      async with TestHttpClient(
        base_url=base_url, timeout=10.0, follow_redirects=False
      ) as client:
        # 1. /player_api.php scopes user_info + xmltv_url to each caller
        pa_a = (
          await client.get(
            "/player_api.php", params={"username": "xt_a", "password": "pw_a"}
          )
        ).json()
        assert pa_a["user_info"]["auth"] == 1
        assert pa_a["user_info"]["username"] == "xt_a"
        assert "username=xt_a&password=pw_a" in pa_a["server_info"]["xmltv_url"]

        pa_b = (
          await client.get(
            "/player_api.php", params={"username": "xt_b", "password": "pw_b"}
          )
        ).json()
        assert pa_b["user_info"]["auth"] == 1
        assert pa_b["user_info"]["username"] == "xt_b"
        assert "username=xt_b&password=pw_b" in pa_b["server_info"]["xmltv_url"]

        # Cross-account password mismatch fails
        assert (
          await client.get(
            "/player_api.php", params={"username": "xt_a", "password": "pw_b"}
          )
        ).json() == {"user_info": {"auth": 0}}
        assert (
          await client.get("/live/xt_a/pw_b/410101.ts")
        ).status_code == 401

        # 2. /get.php embeds ONLY caller's Bellhop Xtream credentials
        m3u_a = await client.get(
          "/get.php",
          params={"username": "xt_a", "password": "pw_a", "type": "m3u_plus"},
        )
        assert m3u_a.status_code == 200
        assert "/live/xt_a/pw_a/410101.ts" in m3u_a.text
        assert "xt_b" not in m3u_a.text

        m3u_b = await client.get(
          "/get.php",
          params={"username": "xt_b", "password": "pw_b", "type": "m3u_plus"},
        )
        assert m3u_b.status_code == 200
        assert "/live/xt_b/pw_b/410108.ts" in m3u_b.text
        assert "xt_a" not in m3u_b.text

        # 3. 302 redirects route each account to its own upstream credentials
        r_a = await client.get("/live/xt_a/pw_a/410101.ts")
        assert r_a.status_code == 302
        assert r_a.headers["location"].endswith("/live/up_a/up_pw_a/410101.ts")
        r_b = await client.get("/live/xt_b/pw_b/410108.ts")
        assert r_b.status_code == 302
        assert r_b.headers["location"].endswith("/live/up_b/up_pw_b/410108.ts")

      # 4. Concurrent streaming honors per-account tuners (0 violations).
      async with (
        TestHttpClient(
          base_url=base_url, timeout=10.0, follow_redirects=True
        ) as cli_a,
        TestHttpClient(
          base_url=base_url, timeout=10.0, follow_redirects=True
        ) as cli_b,
      ):
        res_a, res_b = await asyncio.gather(
          cli_a.get("/live/xt_a/pw_a/410101.ts"),
          cli_b.get("/live/xt_b/pw_b/410108.ts"),
        )
        assert res_a.status_code == 200
        assert res_b.status_code == 200
        assert res_a.content[0] == 0x47 and res_a.content[4] == 0xA1
        assert res_b.content[0] == 0x47 and res_b.content[4] == 0xB8

      assert upstream.connection_limit_violations == 0
      assert ("up_a", 410101) in upstream.stream_requests_by_user
      assert ("up_b", 410108) in upstream.stream_requests_by_user
    finally:
      await srv.stop()
