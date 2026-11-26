"""Tests for the Bellhop Web Guide & Live Player (`bellhop serve`)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from aiohttp import web
from yarl import URL

from bellhop.common.http_utils import DEFAULT_CLIENT_MAX_SIZE, start_site
from bellhop.epg.db import EpgDatabase
from bellhop.epg.refresh import (
  EpgRefreshConfig,
  EpgRefreshService,
  parse_xmltv_for_channels,
)
from bellhop.web.auth import WebAuthConfig, WebAuthManager
from bellhop.web.server import (
  WebGuideApp,
)
from bellhop.web.store import (
  LineupGuideStore,
  ProgramItem,
  reconcile_non_overlapping_schedule,
)
from tests.conftest import await_until
from tests.http_mock import MockHttpServer, new_test_session


def _make_auth(
  tmp_path: Path, filename: str = "session_secret.key"
) -> WebAuthManager:
  return WebAuthManager(
    WebAuthConfig(
      username="admin",
      password="secret",
      secret_file=tmp_path / filename,
    )
  )


def _write_sample_xtream_dir(xt_dir: Path, _now_ts: int = 0) -> None:
  xt_dir.mkdir(parents=True, exist_ok=True)
  categories = [
    {
      "category_id": "1",
      "category_name": "Francophone - Généralistes",
      "parent_id": 0,
    },
    {"category_id": "2", "category_name": "Sports", "parent_id": 0},
  ]
  streams = [
    {
      "num": 1101,
      "name": "ICI Radio-Canada Télé (Montréal)",
      "stream_type": "live",
      "stream_id": 498715,
      "stream_icon": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/canada/ici-tele-ca.png",
      "epg_channel_id": "ICITeleMontreal.ca",
      "category_id": "1",
      "category_name": "Francophone - Généralistes",
      "callsign": "CBFT",
      "direct_source": "http://upstream.example/live/secret_u/secret_p/498715.ts",
      "guide": {
        "sd_chno": 101,
        "language": "fr",
        "quality_tier": "hd",
        "has_native_epg": True,
      },
    },
    {
      "num": 1401,
      "name": "TSN1",
      "stream_type": "live",
      "stream_id": 500101,
      "stream_icon": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/canada/tsn-1-ca.png",
      "epg_channel_id": "TSN1.ca",
      "category_id": "2",
      "category_name": "Sports",
      "callsign": "TSN1",
      "direct_source": "http://upstream.example/live/secret_u/secret_p/500101.ts",
      "guide": {
        "sd_chno": 401,
        "language": "en",
        "quality_tier": "fhd",
        "has_native_epg": True,
      },
    },
  ]
  bundle = {
    "lineup_info": {
      "lineup_id": "CAN-BELLFIBE-MTL-X",
      "provider": "Bell Fibe TV",
      "market": "Montréal, QC",
      "postal_prefix": "H3B",
    },
    "server_info": {"timezone": "UTC"},
    "live_categories": categories,
    "live_streams": streams,
  }

  (xt_dir / "bellhop_xtream_playlist.json").write_text(
    json.dumps(bundle), encoding="utf-8"
  )
  (xt_dir / "get_live_categories.json").write_text(
    json.dumps(categories), encoding="utf-8"
  )
  (xt_dir / "get_live_streams.json").write_text(
    json.dumps(streams), encoding="utf-8"
  )


@pytest.mark.asyncio
async def test_web_guide_api_filtering_search_and_stream_proxy(
  tmp_path: Path,
) -> None:
  now_ts = int(time.time())
  xt_dir = tmp_path / "output" / "xtream"
  _write_sample_xtream_dir(xt_dir, now_ts)

  ts_bytes = (bytes([0x47, 0x40, 0x00, 0x10]) + b"\xaa" * 184) * 20

  def mock_upstream(req: web.Request) -> web.Response:
    path = req.path
    if path.endswith("/498715.ts") or path.endswith("/500101.ts"):
      return web.Response(
        status=200, headers={"Content-Type": "video/mp2t"}, body=ts_bytes
      )
    if path.endswith("/498715.m3u8"):
      manifest = "#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:4.0,\nseg001.ts\n"
      return web.Response(
        status=200,
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
        text=manifest,
      )
    if path.endswith("/seg001.ts"):
      return web.Response(
        status=200, headers={"Content-Type": "video/mp2t"}, body=ts_bytes
      )
    return web.Response(status=404)

  store = LineupGuideStore(xt_dir)
  ici_progs = [
    ProgramItem(
      id="1",
      channel_id="ICITeleMontreal.ca",
      title="Le Téléjournal",
      description="Bulletin de nouvelles national.",
      start_timestamp=now_ts - 900,
      stop_timestamp=now_ts + 900,
    ),
    ProgramItem(
      id="2",
      channel_id="ICITeleMontreal.ca",
      title="Tout le monde en parle",
      description="Talk-show avec Guy A. Lepage.",
      start_timestamp=now_ts + 900,
      stop_timestamp=now_ts + 4500,
    ),
  ]
  tsn_progs = [
    ProgramItem(
      id="3",
      channel_id="TSN1.ca",
      title="SportsCentre",
      description="Highlights from across the sports world.",
      start_timestamp=now_ts - 600,
      stop_timestamp=now_ts + 1200,
    ),
  ]
  refresher = EpgRefreshService(
    store,
    EpgRefreshConfig(enabled=False, cache_file=tmp_path / "epg.sqlite3"),
  )
  refresher.db.replace_guide_schedules(
    snap=store.get_snapshot(),
    programs_by_id={
      "ICITeleMontreal.ca": ici_progs,
      "TSN1.ca": tsn_progs,
    },
    fetched_at=float(now_ts),
    covered_channel_ids=frozenset({"icitelemontreal.ca", "tsn1.ca"}),
  )
  async with MockHttpServer(mock_upstream) as mock:
    app = WebGuideApp(
      store,
      auth=_make_auth(tmp_path),
      settle_sec=0.01,
      session_factory=mock.session_factory,
      epg_refresher=refresher,
    )
    await app.startup()
    runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
    base_url = f"http://127.0.0.1:{port}"

    try:
      async with new_test_session(base_url, timeout=10.0) as client:
        async with client.post(
          "/api/auth/login",
          json={"username": "admin", "password": "secret"},
        ) as sign_in:
          assert sign_in.status == 200
        # 1. Root index.html (including stateful query-param deep links) &
        #    assets
        async with client.get("/") as idx_resp:
          assert idx_resp.status == 200
          idx_text = await idx_resp.text()
          assert "player-overlay" in idx_text
          assert "fullscreen-btn" in idx_text
          assert "hide-empty-epg-btn" in idx_text
          assert "data-lang=" not in idx_text
        async with client.get(
          "/?cat=2&epg=1&view=timeline&mode=hls&ch=1101"
        ) as deep_resp:
          assert deep_resp.status == 200
          assert "player-overlay" in await deep_resp.text()
        async with client.get("/static/app.js") as app_js_resp:
          assert app_js_resp.status == 200
          app_js_text = await app_js_resp.text()
          assert "readStateFromUrl" in app_js_text
          assert "syncUrlFromState" in app_js_text
          assert "hideEmptyGuide" in app_js_text
        async with client.get("/static/vendor/mpegts.js") as r_mpegts:
          assert r_mpegts.status == 200
        async with client.get("/static/vendor/hls.min.js") as r_hls:
          assert r_hls.status == 200

        # 2. Health endpoint
        async with client.get("/api/health") as r_health:
          health = await r_health.json()
        assert health["channel_count"] == 2
        assert health["category_count"] == 2
        assert health["epg_program_count"] == 3

        # 3. Lineup endpoint: logos, live EPG progress, credential redaction
        async with client.get("/api/lineup") as r_lu:
          lu = await r_lu.json()
        assert len(lu["channels"]) == 2
        raw_json = json.dumps(lu)
        assert (
          "secret_p" not in raw_json
        )  # upstream credentials stay server-side!
        ch_ici = lu["channels"][0]
        assert ch_ici["chno"] == 1101
        assert ch_ici["logo"].endswith("ici-tele-ca.png")
        assert ch_ici["now_playing"]["title"] == "Le Téléjournal"
        assert 40.0 <= ch_ici["now_playing"]["progress_pct"] <= 60.0
        assert ch_ici["up_next"]["title"] == "Tout le monde en parle"

        # 4. Category filter, search filter & empty-guide (`has_epg=1`) filter
        async with client.get("/api/lineup", params={"category": "2"}) as r_sp:
          sports_lu = await r_sp.json()
        assert [c["chno"] for c in sports_lu["channels"]] == [1401]

        async with client.get(
          "/api/lineup", params={"q": "radio-canada"}
        ) as r_q:
          search_lu = await r_q.json()
        assert [c["chno"] for c in search_lu["channels"]] == [1101]

        # Remove TSN1.ca from SQLite EPG schedules and confirm `has_epg=1` hides
        # it
        refresher.db.replace_guide_schedules(
          snap=store.get_snapshot(),
          programs_by_id={"ICITeleMontreal.ca": ici_progs},
          fetched_at=float(now_ts),
          covered_channel_ids=frozenset({"icitelemontreal.ca"}),
        )
        refresher.hydrate_from_cache(now_ts=now_ts)
        async with client.get("/api/lineup") as r_all:
          all_lu = await r_all.json()
        assert [c["chno"] for c in all_lu["channels"]] == [1101, 1401]
        async with client.get("/api/lineup", params={"has_epg": "1"}) as r_epg:
          with_epg_lu = await r_epg.json()
        assert [c["chno"] for c in with_epg_lu["channels"]] == [1101]

        # 5. Per-channel EPG schedule endpoint
        async with client.get("/api/epg/1101") as r_epg_1101:
          epg_1101 = await r_epg_1101.json()
        assert len(epg_1101["programs"]) == 2

        # 6. MPEG-TS stream relay & tuner release
        async with client.get("/api/stream/1101.ts") as ts_resp:
          assert ts_resp.status == 200
          assert await ts_resp.read() == ts_bytes
        async with client.post("/api/tuner/release") as rel_resp:
          assert rel_resp.status == 200

        # 7. HLS manifest rewrite & segment relay (opaque asset tokens — zero
        #    upstream URLs/secrets)
        async with client.get("/api/hls/1101/index.m3u8") as hls_resp:
          assert hls_resp.status == 200
          hls_text = await hls_resp.text()
        assert "/api/hls/1101/asset?u=" in hls_text
        assert "secret_p" not in hls_text
        assert "upstream.example" not in hls_text
        seg_line = next(
          ln
          for ln in hls_text.splitlines()
          if ln.startswith("/api/hls/1101/asset")
        )
        async with client.get(seg_line) as seg_resp:
          assert seg_resp.status == 200
          assert await seg_resp.read() == ts_bytes

        # 8. Server hardening: no wildcard CORS on Web Guide routes, path
        #    traversal 403, 404, 413
        async with client.options("/api/lineup") as opt_resp:
          assert opt_resp.headers.get("Access-Control-Allow-Origin") is None

        async with client.get(
          URL(f"{base_url}/static/%2e%2e/pyproject.toml", encoded=True)
        ) as trav_resp:
          assert trav_resp.status == 403
          assert (await trav_resp.json())["error"] == "Forbidden"

        async with client.get("/does-not-exist") as nf_resp:
          assert nf_resp.status == 404
          assert "Not found" in (await nf_resp.json())["error"]

        oversized_body = b"x" * (DEFAULT_CLIENT_MAX_SIZE + 1024)
        async with client.post(
          "/api/auth/login",
          data=oversized_body,
          headers={"Content-Type": "application/json"},
        ) as big_resp:
          assert big_resp.status == 413
    finally:
      await runner.cleanup()
      await app.shutdown()


@pytest.mark.real_outputs
def test_store_reads_real_pipeline_outputs_if_present() -> None:
  real_xtream_dir = Path(__file__).resolve().parents[1] / "output" / "xtream"
  if not (real_xtream_dir / "get_live_streams.json").is_file():
    pytest.skip("Real pipeline outputs not generated in this environment")
  store = LineupGuideStore(real_xtream_dir)
  snap = store.get_snapshot()
  assert len(snap.channels) >= 250
  assert len(snap.categories) >= 15


@pytest.mark.asyncio
async def test_tuner_release_scoped_by_client_id_prevents_reload_race() -> None:
  from bellhop.web.server import TunerSessionManager

  mgr = TunerSessionManager(settle_sec=0.0)
  session = await mgr.preempt_and_acquire(
    chno=1108,
    channel_name="RDS",
    mode="mpegts",
    client_id="new-tab",
  )
  # A delayed pagehide beacon from the previous page must NOT kill the reloaded
  # page's session
  assert await mgr.release_all(client_id="old-tab") is False
  assert not session.cancel_event.is_set()
  assert mgr.active_info is not None and mgr.active_info["chno"] == 1108

  # Closing from the owning tab (or unscoped release) DOES cancel the active
  # session
  async def _finish() -> None:
    await session.cancel_event.wait()
    await mgr.mark_closed(session)

  watcher = asyncio.create_task(_finish())
  assert await mgr.release_all(client_id="new-tab") is True
  await watcher
  assert mgr.active_info is None


@pytest.mark.asyncio
async def test_web_app_dynamic_passthrough_epg_and_persistent_cache(
  tmp_path: Path,
) -> None:
  from datetime import datetime, timezone

  now_ts = int(time.time())
  xt_dir = tmp_path / "output" / "xtream"
  _write_sample_xtream_dir(xt_dir, now_ts - 43200)
  bundle_path = xt_dir / "bellhop_xtream_playlist.json"
  bundle_data = json.loads(bundle_path.read_text(encoding="utf-8"))
  bundle_data["server_info"]["xmltv_url"] = (
    "http://panel.example/xmltv.php?username=epg_u&password=super_secret_epg_pw"
  )
  bundle_path.write_text(json.dumps(bundle_data), encoding="utf-8")

  def _fmt_xmltv(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
      "%Y%m%d%H%M%S +0000"
    )

  fetch_count = 0

  def mock_epg_upstream(req: web.Request) -> web.Response:
    nonlocal fetch_count
    fetch_count += 1
    assert req.path.endswith("/xmltv.php")
    assert req.query.get("username") == "epg_u"
    assert req.query.get("password") == "super_secret_epg_pw"
    title_now = (
      "Direct Live News" if fetch_count == 1 else "Updated Live Special"
    )
    xml = (
      '<?xml version="1.0" encoding="UTF-8"?>\n'
      "<tv>\n"
      f'  <programme start="{_fmt_xmltv(now_ts - 600)}" stop="{_fmt_xmltv(now_ts + 3000)}" channel="ICITeleMontreal.ca">\n'
      f"    <title>{title_now}</title>\n"
      "    <desc>Short</desc>\n"
      "  </programme>\n"
      f'  <programme start="{_fmt_xmltv(now_ts - 600)}" stop="{_fmt_xmltv(now_ts + 3000)}" channel="ICITeleMontreal.ca">\n'
      f"    <title>{title_now}</title>\n"
      "    <desc>Much richer broadcast description from second feed.</desc>\n"
      "  </programme>\n"
      f'  <programme start="{_fmt_xmltv(now_ts + 3000)}" stop="{_fmt_xmltv(now_ts + 10800)}" channel="ICITeleMontreal.ca">\n'
      "    <title>Late Edition</title>\n"
      "  </programme>\n"
      "</tv>\n"
    )
    return web.Response(status=200, content_type="application/xml", text=xml)

  store = LineupGuideStore(xt_dir)
  cache_file = tmp_path / "cache" / "epg.sqlite3"
  assert (
    EpgDatabase(cache_file).coverage_stats(now_ts=now_ts).active_programs == 0
  )
  async with MockHttpServer(mock_epg_upstream) as mock:
    refresher = EpgRefreshService(
      store,
      EpgRefreshConfig(
        enabled=True,
        refresh_hours=6.0,
        cache_file=cache_file,
        check_interval_sec=60.0,
      ),
      session_factory=mock.session_factory,
    )

    app = WebGuideApp(
      store,
      auth=_make_auth(tmp_path),
      settle_sec=0.01,
      epg_refresher=refresher,
    )
    await app.startup()
    runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
    base_url = f"http://127.0.0.1:{port}"

    try:
      async with new_test_session(base_url, timeout=10.0) as client:
        async with client.post(
          "/api/auth/login",
          json={"username": "admin", "password": "secret"},
        ) as sign_in:
          assert sign_in.status == 200
        health: dict[str, object] = {}

        async def _initial_epg_ready() -> bool:
          nonlocal health
          async with client.get("/api/health") as r_h:
            health = await r_h.json()
          epg_st = health.get("epg_status")
          active_cnt = health.get("epg_active_program_count")
          return bool(
            isinstance(epg_st, dict)
            and epg_st.get("state") == "ok"
            and isinstance(active_cnt, int)
            and active_cnt > 0
          )

        await await_until(
          _initial_epg_ready, description="WebGuideApp initial EPG refresh"
        )

        assert fetch_count == 1
        assert cache_file.is_file()
        # bellhop-web keeps output/xtream/ read-only and stores its own SQLite
        # EPG database
        assert not (xt_dir / "get_epg.json").exists()
        assert b"super_secret_epg_pw" not in cache_file.read_bytes()
        assert "super_secret_epg_pw" not in json.dumps(health)

        # Temp raw XML file was cleaned up immediately after parsing
        assert list(cache_file.parent.glob("xmltv_fetch_*.xml")) == []

        # Lineup serves deduplicated live & upcoming programs automatically
        async with client.get("/api/lineup") as r_lu:
          lu = await r_lu.json()
        assert "super_secret_epg_pw" not in json.dumps(lu)
        ch_ici = next(c for c in lu["channels"] if c["chno"] == 1101)
        assert ch_ici["now_playing"]["title"] == "Direct Live News"
        assert (
          "richer broadcast description" in ch_ici["now_playing"]["description"]
        )
        assert ch_ici["up_next"]["title"] == "Late Edition"

        # Manual POST /api/epg/refresh forces a fresh upstream fetch
        async with client.post("/api/epg/refresh") as ref_resp:
          assert ref_resp.status == 200
          assert (await ref_resp.json())["refreshed"] is True
        assert fetch_count == 2

        async with client.get("/api/lineup") as r_lu2:
          lu2 = await r_lu2.json()
        ch_ici2 = next(c for c in lu2["channels"] if c["chno"] == 1101)
        assert ch_ici2["now_playing"]["title"] == "Updated Live Special"
    finally:
      await runner.cleanup()
      await app.shutdown()

    # Simulate restart: fresh store + refresher hydrate from bellhop-web's cache
    # with 0 network calls
    store_restarted = LineupGuideStore(xt_dir)
    refresher_restarted = EpgRefreshService(
      store_restarted,
      EpgRefreshConfig(
        enabled=True,
        refresh_hours=6.0,
        cache_file=cache_file,
        check_interval_sec=60.0,
      ),
      session_factory=mock.session_factory,
    )
    loaded_count = refresher_restarted.hydrate_from_cache()
    assert loaded_count == 2
    needs, reason = refresher_restarted.needs_refresh()
    assert needs is False
    assert reason == "fresh"
    assert fetch_count == 2  # no redundant fetch after restart!

    # Simulate restart after time advances: mix of stale + fresh entries evicts
    # stale from SQLite
    import contextlib
    import sqlite3

    with contextlib.closing(sqlite3.connect(cache_file)) as raw_conn:
      raw_conn.execute(
        """
        INSERT INTO guide_airings (
          chno, epg_channel_id, start_ts, stop_ts, program_uid, title, sub_title, description, category, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
          1101,
          "ICITeleMontreal.ca",
          now_ts - 20000,
          now_ts - 15000,
          "old-1",
          "Yesterday's News",
          "",
          "",
          "",
          "xmltv",
        ),
      )
      raw_conn.commit()

    store_pruned = LineupGuideStore(xt_dir)
    refresher_pruned = EpgRefreshService(
      store_pruned,
      EpgRefreshConfig(enabled=True, refresh_hours=6.0, cache_file=cache_file),
      session_factory=mock.session_factory,
    )
    assert refresher_pruned.hydrate_from_cache(now_ts=now_ts) == 2
    assert refresher_pruned.db.coverage_stats(now_ts=now_ts).total_programs == 2
    assert all(
      p.title != "Yesterday's News"
      for p in refresher_pruned.db.query_channel_schedule(
        1101, "ICITeleMontreal.ca"
      )
    )

    # Simulate restart when ALL cached entries are stale: evicts cache file &
    # schedules fresh fetch
    store_expired = LineupGuideStore(xt_dir)
    refresher_expired = EpgRefreshService(
      store_expired,
      EpgRefreshConfig(enabled=True, refresh_hours=6.0, cache_file=cache_file),
      session_factory=mock.session_factory,
    )
    assert refresher_expired.hydrate_from_cache(now_ts=now_ts + 50000) == 0
    assert not cache_file.exists()
    needs_after_evict, reason_after_evict = refresher_expired.needs_refresh(
      now_ts=now_ts + 50000
    )
    assert needs_after_evict is True
    assert reason_after_evict == "no_active_programs"


def test_store_m3u8_and_gzip_xmltv_fallbacks(tmp_path: Path) -> None:
  import gzip
  from datetime import datetime, timezone

  # M3U8 fallback + gzipped XMLTV fallback in LineupGuideStore when
  # output/xtream is empty
  empty_xtream = tmp_path / "output" / "xtream"
  empty_xtream.mkdir(parents=True)
  m3u8_file = tmp_path / "output" / "lineup.m3u8"
  m3u8_file.write_text(
    '#EXTM3U url-tvg="http://panel.example/xmltv.php?username=u&password=p"\n'
    '#EXTINF:-1 tvg-chno="1108" tvg-id="RDS.ca" tvg-logo="https://logo/rds.png" group-title="Sports",RDS\n'
    "http://panel.example/live/u/p/410108.ts\n",
    encoding="utf-8",
  )

  now_ts = int(time.time())
  start_str = datetime.fromtimestamp(now_ts - 600, tz=timezone.utc).strftime(
    "%Y%m%d%H%M%S +0000"
  )
  stop_str = datetime.fromtimestamp(now_ts + 1800, tz=timezone.utc).strftime(
    "%Y%m%d%H%M%S +0000"
  )
  gz_xml = tmp_path / "guide.xml.gz"
  gz_xml.write_bytes(
    gzip.compress(
      f'<?xml version="1.0"?><tv>'
      f'<programme start="{start_str}" stop="{stop_str}" channel="RDS.ca">'
      f"<title>Hockey Canadien</title></programme></tv>".encode()
    )
  )

  store = LineupGuideStore(empty_xtream, m3u8_path=m3u8_file)
  snap = store.get_snapshot()
  assert (
    snap.xmltv_url == "http://panel.example/xmltv.php?username=u&password=p"
  )
  assert len(snap.channels) == 1
  assert snap.channels[0].chno == 1108
  assert snap.channels[0].category_name == "Sports"
  parsed = parse_xmltv_for_channels(
    gz_xml, snap.build_epg_channel_lookup(), now_timestamp=now_ts
  )
  assert parsed["RDS.ca"][0].title == "Hockey Canadien"


@pytest.mark.asyncio
async def test_public_logo_and_favicon_served_before_auth(
  tmp_path: Path,
) -> None:
  from bellhop.web.auth import WebAuthConfig, WebAuthManager

  xt_dir = tmp_path / "output" / "xtream"
  _write_sample_xtream_dir(xt_dir, int(time.time()))

  auth = WebAuthManager(
    WebAuthConfig(
      username="admin",
      password="secret",
      secret_file=tmp_path / "session_secret.key",
    )
  )
  app = WebGuideApp(LineupGuideStore(xt_dir), auth=auth, settle_sec=0.01)
  await app.startup()
  runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
  base = f"http://127.0.0.1:{port}"

  try:
    async with new_test_session(base) as client:
      # Unauthenticated /login renders centered Bellhop splash logo, favicons &
      # theme toggle
      async with client.get("/login", allow_redirects=False) as login_resp:
        assert login_resp.status == 200
        csp = login_resp.headers.get("Content-Security-Policy", "")
        assert "script-src 'self' blob:" in csp
        assert "script-src 'unsafe-inline'" not in csp
        assert login_resp.headers.get("X-Content-Type-Options") == "nosniff"
        login_text = await login_resp.text()
        assert "/static/bellhop-logo.svg" in login_text
        assert 'href="/favicon.ico"' in login_text
        assert "/static/prefs.js" in login_text
        assert "/static/login.js" in login_text
        assert 'href="/static/styles.css"' in login_text
        assert 'class="login-page"' in login_text
        assert "<script>" not in login_text
        assert "<style>" not in login_text
        assert 'id="theme-toggle-btn"' in login_text
        assert 'class="icon-sun"' in login_text
        assert 'class="icon-moon"' in login_text

      # Brand, favicon, shared stylesheet, prefs & login controller are public
      # before auth
      async with client.get("/static/styles.css") as pre_styles:
        assert pre_styles.status == 200
        assert "text/css" in pre_styles.headers["Content-Type"]
        assert "body.login-page" in await pre_styles.text()

      async with client.get("/static/login.js") as login_js:
        assert login_js.status == 200
        assert "javascript" in login_js.headers["Content-Type"]
        assert "/api/auth/login" in await login_js.text()

      async with client.get("/static/bellhop-logo.svg") as svg_resp:
        assert svg_resp.status == 200
        assert "image/svg+xml" in svg_resp.headers["Content-Type"]
        assert "<svg" in await svg_resp.text()

      async with client.get("/favicon.svg") as fav_svg:
        assert fav_svg.status == 200
        assert "image/svg+xml" in fav_svg.headers["Content-Type"]

      async with client.get("/favicon.ico") as fav_ico:
        assert fav_ico.status == 200
        assert (await fav_ico.read())[:4] == b"\x00\x00\x01\x00"

      async with client.get("/static/prefs.js") as prefs_js:
        assert prefs_js.status == 200
        assert "javascript" in prefs_js.headers["Content-Type"]
        assert "bellhop.settings" in await prefs_js.text()

      # Private app JS modules remain protected behind auth
      for protected_js in ("/static/app.js", "/static/guide_logic.js"):
        async with client.get(protected_js, allow_redirects=False) as r_app:
          assert r_app.status == 401

      # Authenticated index.html renders upper-left logo, favicons, prefs.js &
      # theme toggle
      async with client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
      ) as sign_in:
        assert sign_in.status == 200
      async with client.get("/") as index_resp:
        assert index_resp.status == 200
        index_text = await index_resp.text()
        assert 'class="brand-logo"' in index_text
        assert "/static/bellhop-logo.svg" in index_text
        assert 'href="/favicon.ico"' in index_text
        assert "/static/prefs.js" in index_text
        assert 'type="module" src="/static/app.js"' in index_text
        assert 'id="theme-toggle-btn"' in index_text
        assert 'data-view="cards"' in index_text
        assert ">Cards</button>" in index_text
        assert ">Timeline</button>" in index_text

      async with client.get("/static/styles.css") as styles_resp:
        assert styles_resp.status == 200
        styles_text = await styles_resp.text()
        assert ':root[data-theme="light"]' in styles_text
        assert "--tl-col-width: 255px;" in styles_text
        assert ".cat-count.is-matched" in styles_text
        assert ".cat-count.is-empty" in styles_text
        assert ".timeline-body" in styles_text

      js_modules: list[str] = []
      for mod_name in (
        "app.js",
        "guide_logic.js",
        "render.js",
        "player.js",
        "settings.js",
      ):
        async with client.get(f"/static/{mod_name}") as mod_resp:
          assert mod_resp.status == 200
          js_modules.append(await mod_resp.text())
      combined_js = "\n".join(js_modules)
      assert "&#39;" in combined_js
      assert "onerror=" not in combined_js
      assert "data-logo-fallback" in combined_js
      assert "updateLiveProgressInPlace" in combined_js
  finally:
    await runner.cleanup()
    await app.shutdown()


def test_wcag_aa_contrast_landmarks_and_keyboard_a11y_contracts() -> None:
  import re

  static_dir = (
    Path(__file__).resolve().parents[1] / "src" / "bellhop" / "web" / "static"
  )
  index_html = (static_dir / "index.html").read_text(encoding="utf-8")
  styles_css = (static_dir / "styles.css").read_text(encoding="utf-8")
  compiled_js = "\n".join(
    (static_dir / name).read_text(encoding="utf-8")
    for name in (
      "app.js",
      "guide_logic.js",
      "render.js",
      "player.js",
      "settings.js",
    )
  )

  # 1. Landmarks, live regions, dialog labels, and tablist roles in index.html
  assert (
    'id="guide-main" class="guide-main" aria-label="TV Guide"' in index_html
  )
  assert (
    'id="cards-view" class="cards-sections" aria-label="Channel cards"'
    in index_html
  )
  assert (
    'id="timeline-view" class="timeline-wrapper" aria-label="Program timeline"'
    in index_html
  )
  assert (
    'id="player-overlay"' in index_html
    and 'aria-labelledby="player-name"' in index_html
  )
  assert (
    'id="channel-counter" class="meta-text" aria-live="polite"' in index_html
  )
  assert 'role="tab"' in index_html and 'aria-selected="true"' in index_html
  assert 'aria-label="Close player"' in index_html
  assert 'aria-label="Close settings"' in index_html

  # 2. Focus trap, focus restore & Timeline keyboard navigation in compiled JS
  assert "trapFocusInContainer" in compiled_js
  assert "restoreFocus" in compiled_js
  assert "ArrowRight" in compiled_js and "ArrowLeft" in compiled_js
  assert "--tl-col-width" in compiled_js
  assert ".tl-ch-cell:focus-visible" in styles_css

  # 3. WCAG 2.1 AA contrast >= 4.5:1 for muted/empty category text in both
  #    themes
  def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.strip().lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

  def _rel_lum(rgb: tuple[int, int, int]) -> float:
    def _chan(c: int) -> float:
      v = c / 255.0
      return v / 12.92 if v <= 0.04045 else float(((v + 0.055) / 1.055) ** 2.4)

    return (
      0.2126 * _chan(rgb[0]) + 0.7152 * _chan(rgb[1]) + 0.0722 * _chan(rgb[2])
    )

  def _contrast(fg_hex: str, bg_hex: str) -> float:
    l1, l2 = sorted(
      (_rel_lum(_hex_to_rgb(fg_hex)), _rel_lum(_hex_to_rgb(bg_hex))),
      reverse=True,
    )
    return (l1 + 0.05) / (l2 + 0.05)

  dark_block, light_block = styles_css.split(':root[data-theme="light"]', 1)

  def _var(block: str, name: str) -> str:
    m = re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", block)
    assert m is not None, f"Missing hex token {name}"
    return m.group(1)

  dark_bg = _var(dark_block, "--bg-surface")
  for tok in (
    "--cat-count-empty-text",
    "--cat-item-empty-text",
    "--cat-active-empty-text",
  ):
    ratio = _contrast(_var(dark_block, tok), dark_bg)
    assert ratio >= 4.5, f"Dark {tok} contrast {ratio:.2f} < 4.5"

  light_bg = _var(light_block, "--bg-surface")
  for tok in (
    "--cat-count-empty-text",
    "--cat-item-empty-text",
    "--cat-active-empty-text",
  ):
    ratio = _contrast(_var(light_block, tok), light_bg)
    assert ratio >= 4.5, f"Light {tok} contrast {ratio:.2f} < 4.5"


@pytest.mark.asyncio
async def test_hot_reload_bumps_lineup_updated_at_and_refetches_epg_on_lineup_change(
  tmp_path: Path,
  caplog: pytest.LogCaptureFixture,
) -> None:
  """Verify `/api/health` exposes `lineup_updated_at`, logs reload, &
  auto-fetches EPG.
  """
  import logging
  from datetime import datetime, timezone

  caplog.set_level(logging.INFO, logger="bellhop.web.store")

  now_ts = int(time.time())
  xt_dir = tmp_path / "output" / "xtream"
  _write_sample_xtream_dir(xt_dir, now_ts - 43200)
  bundle_path = xt_dir / "bellhop_xtream_playlist.json"
  bundle_data = json.loads(bundle_path.read_text(encoding="utf-8"))
  bundle_data["server_info"]["xmltv_url"] = (
    "http://panel.example/xmltv.php?username=epg_u&password=pw"
  )
  bundle_path.write_text(json.dumps(bundle_data), encoding="utf-8")

  def _fmt_xmltv(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
      "%Y%m%d%H%M%S +0000"
    )

  xmltv_fetches = 0

  def mock_epg_upstream(_req: web.Request) -> web.Response:
    nonlocal xmltv_fetches
    xmltv_fetches += 1
    xml = (
      '<?xml version="1.0" encoding="UTF-8"?>\n'
      "<tv>\n"
      f'  <programme start="{_fmt_xmltv(now_ts - 600)}" stop="{_fmt_xmltv(now_ts + 14400)}" channel="ICITeleMontreal.ca">\n'
      "    <title>Téléjournal</title>\n"
      "  </programme>\n"
      f'  <programme start="{_fmt_xmltv(now_ts - 600)}" stop="{_fmt_xmltv(now_ts + 14400)}" channel="ASN.ca">\n'
      "    <title>Atlantic Live At 5</title>\n"
      "  </programme>\n"
      "</tv>\n"
    )
    return web.Response(status=200, content_type="application/xml", text=xml)

  store = LineupGuideStore(xt_dir)
  cache_file = tmp_path / "cache" / "epg.sqlite3"
  async with MockHttpServer(mock_epg_upstream) as mock:
    refresher = EpgRefreshService(
      store,
      EpgRefreshConfig(
        enabled=True,
        refresh_hours=6.0,
        cache_file=cache_file,
        check_interval_sec=0.04,
        min_retry_sec=0.01,
      ),
      session_factory=mock.session_factory,
    )
    app = WebGuideApp(
      store,
      auth=_make_auth(tmp_path),
      settle_sec=0.01,
      epg_refresher=refresher,
    )
    await app.startup()
    runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
    base_url = f"http://127.0.0.1:{port}"
    try:
      async with new_test_session(base_url, timeout=10.0) as client:
        async with client.post(
          "/api/auth/login",
          json={"username": "admin", "password": "secret"},
        ) as sign_in:
          assert sign_in.status == 200

        # 1. Wait for initial EPG fetch (only initial channels extracted; ASN.ca
        #    not yet in lineup)
        async def _first_xmltv_done() -> bool:
          async with client.get("/api/health") as r_h:
            h1 = await r_h.json()
          return bool(
            h1["epg_active_program_count"] > 0
            and not h1["epg_status"]["refreshing"]
          )

        await await_until(
          _first_xmltv_done, description="initial hot-reload XMLTV fetch"
        )
        assert xmltv_fetches == 1

        async with client.get("/api/lineup") as r_lu1:
          lu1 = await r_lu1.json()
        ts1 = lu1["lineup"]["lineup_updated_at"]
        assert isinstance(ts1, (int, float)) and ts1 > 0
        init_count = lu1["lineup"]["total_channels"]

        # 2. Repeated `/api/health` checks with no disk change keep
        #    `lineup_updated_at` identical
        async with client.get("/api/health") as r_h_idle:
          h_idle = await r_h_idle.json()
        assert h_idle["lineup_updated_at"] == ts1
        assert xmltv_fetches == 1

        # 3. Simulate `bellhop run` exporting a newly matched channel (`1258 A
        #    Atlantic`, `ASN.ca`)
        bundle_data["live_streams"].append(
          {
            "num": 1258,
            "name": "A Atlantic",
            "stream_id": 414114,
            "stream_icon": "",
            "epg_channel_id": "ASN.ca",
            "category_id": "1",
            "category_name": "Broadcast",
            "direct_source": "http://panel.example/live/u/p/414114.ts",
            "guide": {
              "callsign": "ASN-C",
              "language": "en",
              "quality_tier": "hd",
              "has_native_epg": True,
            },
          }
        )
        bundle_path.write_text(json.dumps(bundle_data), encoding="utf-8")

        # 4. `/api/health` immediately bumps `lineup_updated_at`, logs reload, &
        #    kicks off EPG refresh
        async with client.get("/api/health") as r_h2:
          h2 = await r_h2.json()
        assert h2["lineup_updated_at"] > ts1
        assert h2["channel_count"] == init_count + 1
        assert any(
          "Reloaded Web Guide lineup" in rec.message and "(+1)" in rec.message
          for rec in caplog.records
        )

        await await_until(
          lambda: (
            xmltv_fetches >= 2 and not refresher.status_dict()["refreshing"]
          ),
          description="post-reload XMLTV fetch",
        )
        assert xmltv_fetches == 2
        assert "lineup_changed" in refresher.status_dict()["last_reason"]

        # 5. Reloaded `/api/lineup` includes channel 1258 AND its newly fetched
        #    `ASN.ca` guide data!
        async with client.get("/api/lineup") as r_lu2:
          lu2 = await r_lu2.json()
        assert lu2["lineup"]["lineup_updated_at"] >= h2["lineup_updated_at"]
        ch_1258 = next(c for c in lu2["channels"] if c["chno"] == 1258)
        assert ch_1258["now_playing"] is not None
        assert ch_1258["now_playing"]["title"] == "Atlantic Live At 5"
    finally:
      await runner.cleanup()
      await app.shutdown()


def test_epg_priority_fallback_and_weighted_interval_deoverlapping(
  tmp_path: Path,
) -> None:
  """Verify EPG alias priority fallback (no cross-feed pooling), v1 cache
  invalidation, and DP de-overlapping.
  """
  now_ts = 1_764_000_000
  xt_dir = tmp_path / "xtream"
  xt_dir.mkdir(parents=True, exist_ok=True)

  bundle = {
    "meta": {"lineup_id": "TVG-9100000001", "provider": "Bell Fibe TV"},
    "categories": [{"category_id": "1", "category_name": "Movies (1306–1307)"}],
    "live_streams": [
      {
        "num": 1306,
        "name": "HBO",
        "stream_id": 1001,
        "epg_channel_id": "HBO.us",
        "category_id": "1",
        "category_name": "Movies (1306–1307)",
        "direct_source": "http://panel.example/live/u/p/1001.ts",
        "guide": {
          "callsign": "HBOCAN",
          "official_tvg_id": "Cinemax.us",
          "has_native_epg": True,
        },
      },
      {
        "num": 1307,
        "name": "Cinemax",
        "stream_id": 1002,
        "epg_channel_id": "Cinemax.us",
        "category_id": "1",
        "category_name": "Movies (1306–1307)",
        "direct_source": "http://panel.example/live/u/p/1002.ts",
        "guide": {
          "callsign": "MAX",
          "official_tvg_id": "Cinemax.us",
          "has_native_epg": True,
        },
      },
      {
        "num": 1510,
        "name": "BBC News",
        "stream_id": 1003,
        "epg_channel_id": "BBCWorld.ca",
        "category_id": "1",
        "category_name": "Movies (1306–1307)",
        "direct_source": "http://panel.example/live/u/p/1003.ts",
        "guide": {
          "callsign": "BBCW",
          "official_tvg_id": "BBCNews.uk",
          "has_native_epg": False,
        },
      },
    ],
  }
  (xt_dir / "bellhop_xtream_playlist.json").write_text(
    json.dumps(bundle), encoding="utf-8"
  )

  store = LineupGuideStore(xt_dir)
  snap = store.get_snapshot()
  lookup = snap.build_epg_channel_lookup()
  # Primary `Cinemax.us` on 1307 must NEVER be overwritten by 1306's secondary
  # alias
  assert lookup["hbo.us"] == "HBO.us"
  assert lookup["cinemax.us"] == "Cinemax.us"
  assert lookup["bbcnews.uk"] == "BBCWorld.ca"

  chains = snap.build_epg_candidate_chains()
  assert chains["HBO.us"] == ("hbo.us", "cinemax.us")
  assert chains["BBCWorld.ca"] == ("bbcworld.ca", "bbcnews.uk")

  def _fmt(ts: int) -> str:
    return time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(ts))

  xml_path = tmp_path / "upstream.xml"
  xml_path.write_text(
    f"""<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <programme start="{_fmt(now_ts)}" stop="{_fmt(now_ts + 3600)}" channel="HBO.us">
    <title>The Gilded Age</title>
  </programme>
  <programme start="{_fmt(now_ts + 3600)}" stop="{_fmt(now_ts + 7200)}" channel="HBO.us">
    <title>This Is Not a War Story</title>
  </programme>
  <programme start="{_fmt(now_ts + 900)}" stop="{_fmt(now_ts + 6300)}" channel="Cinemax.us">
    <title>Happy Death Day</title>
  </programme>
  <programme start="{_fmt(now_ts)}" stop="{_fmt(now_ts + 3600)}" channel="BBCNews.uk">
    <title>BBC News Now</title>
  </programme>
</tv>
""",
    encoding="utf-8",
  )

  parsed = parse_xmltv_for_channels(
    xml_path,
    lookup,
    candidate_chains=chains,
    now_timestamp=now_ts,
  )
  # 1306 (`HBO.us`) keeps ONLY `HBO.us` programmes — never interleaved with
  # `Cinemax.us`!
  assert [p.title for p in parsed["HBO.us"]] == [
    "The Gilded Age",
    "This Is Not a War Story",
  ]
  assert [p.title for p in parsed["Cinemax.us"]] == ["Happy Death Day"]
  # 1510 (`BBCWorld.ca`) has no primary XMLTV entries, so it cleanly falls back
  # to `BBCNews.uk`
  assert [p.title for p in parsed["BBCWorld.ca"]] == ["BBC News Now"]

  # Pre-fix `v1` EPG cache files on disk must be ignored so stale merged feeds
  # re-fetch cleanly
  v1_cache = tmp_path / "epg_cache.json"
  v1_cache.write_text(
    json.dumps(
      {
        "version": 1,
        "updated_at": float(now_ts),
        "xmltv_url": "http://panel.example/xmltv.php?username=u&password=p",
        "channels": {
          "HBO.us": [
            {
              "title": "Stale Merged Entry",
              "sub_title": "",
              "description": "",
              "category": "",
              "start": now_ts,
              "stop": now_ts + 3600,
            }
          ]
        },
      }
    ),
    encoding="utf-8",
  )
  refresher = EpgRefreshService(
    store,
    EpgRefreshConfig(enabled=True, cache_file=v1_cache),
  )
  assert refresher.hydrate_from_cache(now_ts=now_ts) == 0

  # Single-channel multi-grabber overlaps: shifted duplicates, <=5m bumper
  # overlap, & live sports preemption
  base = now_ts
  messy_items = [
    # Movie 1 ending 3m after Movie 2's grabber A start (`<=5m` boundary bumper)
    ProgramItem(
      id="1",
      channel_id="TSN3.ca",
      title="Early Morning Feature",
      start_timestamp=base,
      stop_timestamp=base + 5400,
    ),
    # Shifted duplicate pair from 2 grabbers (`+5220..+11340` vs
    # `+5400..+12600`)
    ProgramItem(
      id="2",
      channel_id="TSN3.ca",
      title="Morning Documentary",
      start_timestamp=base + 5220,
      stop_timestamp=base + 11340,
    ),
    ProgramItem(
      id="3",
      channel_id="TSN3.ca",
      title="Morning Documentary",
      description="Full synopsis from richer grabber.",
      start_timestamp=base + 5400,
      stop_timestamp=base + 12600,
    ),
    # Competing afternoon tracks: generic `SportsCentre` filler vs `SC` + `Live:
    # FIFA World Cup` preemption
    ProgramItem(
      id="4",
      channel_id="TSN3.ca",
      title="SportsCentre",
      start_timestamp=base + 12600,
      stop_timestamp=base + 16200,
    ),
    ProgramItem(
      id="5",
      channel_id="TSN3.ca",
      title="SportsCentre",
      start_timestamp=base + 16200,
      stop_timestamp=base + 19800,
    ),
    ProgramItem(
      id="6",
      channel_id="TSN3.ca",
      title="SC",
      start_timestamp=base + 12600,
      stop_timestamp=base + 13680,
    ),
    ProgramItem(
      id="7",
      channel_id="TSN3.ca",
      title="Live: 2026 FIFA U-17 World Cup",
      description="Quarterfinal live broadcast.",
      start_timestamp=base + 13680,
      stop_timestamp=base + 21600,
    ),
    ProgramItem(
      id="8",
      channel_id="TSN3.ca",
      title="SportsCentre",
      start_timestamp=base + 21600,
      stop_timestamp=base + 23400,
    ),
  ]
  clean = reconcile_non_overlapping_schedule(messy_items)
  assert [p.title for p in clean] == [
    "Early Morning Feature",
    "Morning Documentary",
    "SC",
    "Live: 2026 FIFA U-17 World Cup",
    "SportsCentre",
  ]
  assert clean[1].description == "Full synopsis from richer grabber."
  assert all(
    a.start_timestamp < a.stop_timestamp <= b.start_timestamp
    for a, b in zip(clean, clean[1:], strict=False)
  )


@pytest.mark.asyncio
async def test_hls_ssrf_defense_cross_tab_preemption_and_write_timeout(
  tmp_path: Path,
) -> None:
  """Verify HLS SSRF filtering, HLS/TS cross-tab isolation, & stall timeouts."""
  from aiohttp import web

  from bellhop.web.server import TunerSessionManager

  xt_dir = tmp_path / "output" / "xtream"
  _write_sample_xtream_dir(xt_dir, int(time.time()))
  ts_chunk = (bytes([0x47, 0x40, 0x00, 0x10]) + b"\xcc" * 184) * 16
  huge_chunk = (bytes([0x47, 0x40, 0x00, 0x10]) + b"\xaa" * 184) * 1400
  hls_mode = "ssrf_manifest"
  ts_connected = asyncio.Event()
  ts_closed = asyncio.Event()

  async def mock_upstream(req: web.Request) -> web.StreamResponse:
    path = req.path
    if path.endswith("/498715.m3u8"):
      if hls_mode == "redirect_loopback":
        return web.Response(
          status=302, headers={"Location": "http://127.0.0.1:9999/secret"}
        )
      manifest = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="http://127.0.0.1:9999/key"\n'
        "http://127.0.0.1:9999/secret.ts\n"
        "http://169.254.169.254/latest/meta-data\n"
        "http://192.168.1.1/router.ts\n"
        "file:///etc/passwd\n"
        "seg001.ts\n"
      )
      return web.Response(
        status=200,
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
        text=manifest,
      )
    if path.endswith("/seg001.ts"):
      return web.Response(
        status=200, headers={"Content-Type": "video/mp2t"}, body=ts_chunk
      )
    if path.endswith("/500101.ts"):
      ts_connected.set()
      resp = web.StreamResponse(
        status=200, headers={"Content-Type": "video/mp2t"}
      )
      await resp.prepare(req)
      try:
        for _ in range(300):
          await resp.write(huge_chunk)
          await asyncio.sleep(0.002)
      finally:
        ts_closed.set()
      return resp
    return web.Response(status=404)

  store = LineupGuideStore(xt_dir)
  async with MockHttpServer(mock_upstream) as mock:
    app = WebGuideApp(
      store,
      auth=_make_auth(tmp_path),
      settle_sec=0.01,
      write_timeout_sec=0.05,
      session_factory=mock.session_factory,
    )
    await app.startup()
    runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
    base_url = f"http://127.0.0.1:{port}"
    try:
      async with new_test_session(base_url, timeout=10.0) as client:
        async with client.post(
          "/api/auth/login",
          json={"username": "admin", "password": "secret"},
        ) as sign_in:
          assert sign_in.status == 200
          cookie_hdr = sign_in.headers["Set-Cookie"].split(";", 1)[0].strip()

        # 1. Manifest SSRF filtering strips all private/loopback/file lines.
        async with client.get(
          "/api/hls/1101/index.m3u8?t=1000&client=tab-a"
        ) as r_hls:
          assert r_hls.status == 200
          rewritten = await r_hls.text()
        assert "/api/hls/1101/asset?u=" in rewritten
        assert "?u=\n" not in rewritten and not rewritten.endswith("?u=")
        assert "127.0.0.1" not in rewritten
        assert "169.254.169.254" not in rewritten
        assert "192.168.1.1" not in rewritten
        assert "/etc/passwd" not in rewritten
        assert "EXT-X-KEY" not in rewritten
        asset_lines = [
          ln for ln in rewritten.splitlines() if ln.startswith("/api/hls/")
        ]
        assert len(asset_lines) == 1
        async with client.get(asset_lines[0]) as r_seg:
          assert r_seg.status == 200
          assert await r_seg.read() == ts_chunk

        # Active HLS session is tracked in `/api/health`.
        async with client.get("/api/health") as r_h1:
          h1 = await r_h1.json()
        assert h1["active_tuner"]["mode"] == "hls"
        assert h1["active_tuner"]["chno"] == 1101

        # 2. Tab B opens MPEG-TS on ch 1401, preempting Tab A's HLS session.
        async with client.get("/api/stream/1401.ts?client=tab-b") as r_ts:
          assert r_ts.status == 200
          first = await r_ts.content.readexactly(188)
          assert first[0] == 0x47

          # Stale Tab A poll receives 503 and MUST NOT kill Tab B's stream!
          async with client.get(
            "/api/hls/1101/index.m3u8?t=1000&client=tab-a"
          ) as r_stale:
            assert r_stale.status == 503
          second = await r_ts.content.readexactly(188)
          assert second[0] == 0x47
          async with client.get("/api/health") as r_h2:
            h2 = await r_h2.json()
          assert h2["active_tuner"]["mode"] == "mpegts"
          assert h2["active_tuner"]["chno"] == 1401

        await await_until(
          lambda: app.tuner.active_info is None, description="TS tuner close"
        )

        # 3. `302` redirect to loopback returns 502 instead of fetching local.
        hls_mode = "redirect_loopback"
        async with client.get(
          "/api/hls/1101/index.m3u8?t=2000&client=tab-a"
        ) as r_ssrf:
          assert r_ssrf.status == 502

        # 4. Stalled client TCP reader hits `write_timeout_sec` and frees tuner.
        ts_connected.clear()
        ts_closed.clear()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
          req_raw = (
            "GET /api/stream/1401.ts?client=stalled HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Cookie: {cookie_hdr}\r\n"
            "Connection: close\r\n\r\n"
          ).encode("ascii")
          writer.write(req_raw)
          await writer.drain()
          await asyncio.wait_for(ts_connected.wait(), timeout=2.0)
          # Read only headers, then pause reading so socket send buffer fills.
          while True:
            hdr_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if hdr_line in (b"\r\n", b"\n", b""):
              break
          await await_until(
            lambda: app.tuner.active_info is None,
            timeout_sec=5.0,
            description="write timeout releases tuner on stalled client",
          )
        finally:
          writer.close()
          await writer.wait_closed()
    finally:
      await runner.cleanup()
      await app.shutdown()

  # 5. Rapid-zap ticket superseding cancels queued intermediate requests early.
  mgr = TunerSessionManager(settle_sec=0.05)
  s0 = await mgr.preempt_and_acquire(
    chno=100, channel_name="C0", client_id="tab"
  )

  async def _delayed_close_s0() -> None:
    await s0.cancel_event.wait()
    await asyncio.sleep(0.02)
    await mgr.mark_closed(s0)

  closer = asyncio.create_task(_delayed_close_s0())
  t1 = asyncio.create_task(
    mgr.preempt_and_acquire(chno=101, channel_name="C1", client_id="tab")
  )
  await asyncio.sleep(0.005)
  t2 = asyncio.create_task(
    mgr.preempt_and_acquire(chno=102, channel_name="C2", client_id="tab")
  )
  t3 = asyncio.create_task(
    mgr.preempt_and_acquire(chno=103, channel_name="C3", client_id="tab")
  )
  s1, s2, s3 = await asyncio.gather(t1, t2, t3)
  await closer
  assert s1.cancel_event.is_set() and s1.session_id == 0
  assert s2.cancel_event.is_set() and s2.session_id == 0
  assert not s3.cancel_event.is_set() and s3.session_id > 0
  assert mgr.active_info is not None and mgr.active_info["chno"] == 103
  await mgr.mark_closed(s3)
