"""Tests for the Bellhop Web Guide & Live Player (`bellhop serve`)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from aiohttp import web
from yarl import URL

from bellhop_web.epg_refresh import EpgRefreshConfig, EpgRefreshService
from bellhop_web.http_utils import DEFAULT_CLIENT_MAX_SIZE, start_site
from bellhop_web.server import (
  WebGuideApp,
  build_access_urls,
  resolve_bind_host,
)
from bellhop_web.store import LineupGuideStore
from tests.http_mock import MockHttpServer, new_test_session


def _write_sample_xtream_dir(xt_dir: Path, now_ts: int) -> None:
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
  epg = {
    "epg_listings": [
      {
        "id": "1",
        "channel_id": "ICITeleMontreal.ca",
        "title": "Le Téléjournal",
        "description": "Bulletin de nouvelles national.",
        "start_timestamp": now_ts - 900,
        "stop_timestamp": now_ts + 900,
      },
      {
        "id": "2",
        "channel_id": "ICITeleMontreal.ca",
        "title": "Tout le monde en parle",
        "description": "Talk-show avec Guy A. Lepage.",
        "start_timestamp": now_ts + 900,
        "stop_timestamp": now_ts + 4500,
      },
      {
        "id": "3",
        "channel_id": "TSN1.ca",
        "title": "SportsCentre",
        "description": "Highlights from across the sports world.",
        "start_timestamp": now_ts - 600,
        "stop_timestamp": now_ts + 1200,
      },
    ]
  }
  bundle = {
    "lineup_info": {
      "lineup_id": "CAN-BELLFIBE-MTL-X",
      "provider": "Bell Fibe TV",
      "market": "Montréal, QC",
      "postal_prefix": "H2Y",
    },
    "server_info": {"timezone": "America/Montreal"},
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
  (xt_dir / "get_epg.json").write_text(json.dumps(epg), encoding="utf-8")


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
  async with MockHttpServer(mock_upstream) as mock:
    app = WebGuideApp(
      store,
      settle_sec=0.01,
      session_factory=mock.session_factory,
    )
    await app.startup()
    runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
    base_url = f"http://127.0.0.1:{port}"

    try:
      async with new_test_session(base_url, timeout=10.0) as client:
        # 1. Root index.html (including stateful query-param deep links) & assets
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

        # Add an EPG-less channel to store snapshot via reload and confirm `has_epg=1` hides it
        epg_data = json.loads(
          (xt_dir / "get_epg.json").read_text(encoding="utf-8")
        )
        epg_data["epg_listings"] = [
          p for p in epg_data["epg_listings"] if p["channel_id"] != "TSN1.ca"
        ]
        (xt_dir / "get_epg.json").write_text(
          json.dumps(epg_data), encoding="utf-8"
        )
        store.get_snapshot(force_reload=True)
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

        # 7. HLS manifest rewrite & segment relay
        async with client.get("/api/hls/1101/index.m3u8") as hls_resp:
          assert hls_resp.status == 200
          hls_text = await hls_resp.text()
        assert "/api/hls/1101/asset?u=" in hls_text
        seg_line = next(
          ln
          for ln in hls_text.splitlines()
          if ln.startswith("/api/hls/1101/asset")
        )
        async with client.get(seg_line) as seg_resp:
          assert seg_resp.status == 200
          assert await seg_resp.read() == ts_bytes

        # 8. Server hardening: CORS preflight, path traversal 403, unknown 404, 413 body limit
        async with client.options("/api/lineup") as opt_resp:
          assert opt_resp.status == 204
          assert opt_resp.headers.get("Access-Control-Allow-Origin") == "*"

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


def test_store_reads_real_pipeline_outputs_if_present() -> None:
  real_xtream_dir = Path(__file__).resolve().parents[1] / "output" / "xtream"
  if not (real_xtream_dir / "get_live_streams.json").is_file():
    pytest.skip("Real pipeline outputs not generated in this environment")
  store = LineupGuideStore(real_xtream_dir)
  snap = store.get_snapshot()
  assert len(snap.channels) >= 340
  assert len(snap.categories) >= 15


@pytest.mark.asyncio
async def test_tuner_release_scoped_by_client_id_prevents_reload_race() -> None:
  from bellhop_web.server import TunerSessionManager

  mgr = TunerSessionManager(settle_sec=0.0)
  session = await mgr.preempt_and_acquire(
    chno=1108,
    channel_name="RDS",
    mode="mpegts",
    client_id="new-tab",
  )
  # A delayed pagehide beacon from the previous page must NOT kill the reloaded page's session
  assert await mgr.release_all(client_id="old-tab") is False
  assert not session.cancel_event.is_set()
  assert mgr.active_info is not None and mgr.active_info["chno"] == 1108

  # Closing from the owning tab (or unscoped release) DOES cancel the active session
  async def _finish() -> None:
    await session.cancel_event.wait()
    await mgr.mark_closed(session)

  watcher = asyncio.create_task(_finish())
  assert await mgr.release_all(client_id="new-tab") is True
  await watcher
  assert mgr.active_info is None


def test_resolve_bind_host_and_access_urls(
  monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
  from bellhop.config import load_config

  assert resolve_bind_host(None) == "0.0.0.0"
  assert resolve_bind_host("") == "0.0.0.0"
  assert resolve_bind_host("0.0.0.0") == "0.0.0.0"
  assert resolve_bind_host("127.0.0.1") == "127.0.0.1"
  assert resolve_bind_host("192.168.1.50") == "192.168.1.50"

  monkeypatch.setattr(
    "bellhop_web.server.detect_device_ip", lambda: "192.168.1.42"
  )
  assert resolve_bind_host("auto") == "192.168.1.42"
  assert resolve_bind_host("lan") == "192.168.1.42"
  assert build_access_urls("0.0.0.0", 8080) == [
    "http://192.168.1.42:8080",
    "http://localhost:8080",
  ]
  assert build_access_urls("192.168.1.42", 8080) == ["http://192.168.1.42:8080"]
  assert build_access_urls("127.0.0.1", 8080) == ["http://localhost:8080"]

  monkeypatch.setattr("bellhop_web.server.detect_device_ip", lambda: None)
  assert resolve_bind_host("auto") == "0.0.0.0"
  assert build_access_urls("0.0.0.0", 8080) == ["http://localhost:8080"]

  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    "xtream:\n  server_url: http://example.invalid\n", encoding="utf-8"
  )
  cfg = load_config(cfg_path)
  assert cfg.web.host == "0.0.0.0"


@pytest.mark.asyncio
async def test_web_app_dynamic_passthrough_epg_and_persistent_cache(
  tmp_path: Path,
) -> None:
  from datetime import datetime, timezone

  now_ts = int(time.time())
  xt_dir = tmp_path / "output" / "xtream"
  _write_sample_xtream_dir(xt_dir, now_ts - 43200)
  # Remove legacy get_epg.json so output/xtream/ mirrors pure passthrough export,
  # embedding the upstream passthrough xmltv_url directly in the bundle.
  (xt_dir / "get_epg.json").unlink()
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
  _tot, active_before, _max_stop = store.get_snapshot().epg_coverage_stats(
    now_ts=now_ts
  )
  assert active_before == 0

  cache_file = tmp_path / "cache" / "bellhop_web" / "epg_cache.json"
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

    app = WebGuideApp(store, settle_sec=0.01, epg_refresher=refresher)
    await app.startup()
    runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
    base_url = f"http://127.0.0.1:{port}"

    try:
      async with new_test_session(base_url, timeout=10.0) as client:
        health: dict[str, object] = {}
        for _ in range(50):
          async with client.get("/api/health") as r_h:
            health = await r_h.json()
          epg_st = health.get("epg_status")
          active_cnt = health.get("epg_active_program_count")
          if (
            isinstance(epg_st, dict)
            and epg_st.get("state") == "ok"
            and isinstance(active_cnt, int)
            and active_cnt > 0
          ):
            break
          await asyncio.sleep(0.05)

        assert fetch_count == 1
        assert cache_file.is_file()
        # bellhop-web keeps output/xtream/ read-only and stores its own compact JSON cache
        assert not (xt_dir / "get_epg.json").exists()
        assert "super_secret_epg_pw" not in cache_file.read_text(
          encoding="utf-8"
        )
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

    # Simulate restart: fresh store + refresher hydrate from bellhop-web's cache with 0 network calls
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

    # Simulate restart after time advances: mix of stale + fresh entries evicts stale from disk
    mixed_cache = json.loads(cache_file.read_text(encoding="utf-8"))
    mixed_cache["channels"]["ICITeleMontreal.ca"].insert(
      0,
      {
        "id": "old-1",
        "title": "Yesterday's News",
        "sub_title": "",
        "description": "",
        "category": "",
        "start_timestamp": now_ts - 20000,
        "stop_timestamp": now_ts - 15000,
      },
    )
    cache_file.write_text(json.dumps(mixed_cache), encoding="utf-8")

    store_pruned = LineupGuideStore(xt_dir)
    refresher_pruned = EpgRefreshService(
      store_pruned,
      EpgRefreshConfig(enabled=True, refresh_hours=6.0, cache_file=cache_file),
      session_factory=mock.session_factory,
    )
    assert refresher_pruned.hydrate_from_cache(now_ts=now_ts) == 2
    pruned_disk = json.loads(cache_file.read_text(encoding="utf-8"))
    assert pruned_disk["program_count"] == 2
    assert all(
      p["title"] != "Yesterday's News"
      for p in pruned_disk["channels"]["ICITeleMontreal.ca"]
    )

    # Simulate restart when ALL cached entries are stale: evicts cache file & schedules fresh fetch
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


def test_web_auth_session_cookie_expiry_renewal_and_secret_persistence(
  tmp_path: Path,
) -> None:
  from bellhop_web.auth import (
    WebAuthConfig,
    WebAuthManager,
    sanitize_next_url,
  )

  assert sanitize_next_url("/?cat=2&ch=1108") == "/?cat=2&ch=1108"
  assert sanitize_next_url("//evil.example/phish") == "/"
  assert sanitize_next_url("https://evil.example") == "/"
  assert sanitize_next_url("/login?next=/") == "/"

  secret_file = tmp_path / "cache" / "bellhop_web" / "session_secret.key"
  auth1 = WebAuthManager(
    WebAuthConfig(
      username="admin",
      password="secret_web_password",
      session_ttl_hours=2.0,
      secret_file=secret_file,
    )
  )
  assert auth1.enabled is True
  assert auth1.verify_credentials("admin", "secret_web_password") is True
  assert auth1.verify_credentials("admin", "wrong") is False

  now_ts = int(time.time())
  token, session = auth1.create_session_token("admin", now_ts=now_ts)
  assert secret_file.is_file()
  assert session.expires_at == now_ts + 7200
  assert session.needs_renewal(now_ts=now_ts + 100) is False
  assert session.needs_renewal(now_ts=now_ts + 4000) is True

  # Tampered token rejected
  assert auth1.verify_session_token(token + "x", now_ts=now_ts + 10) is None
  # Expired token rejected
  assert auth1.verify_session_token(token, now_ts=now_ts + 7201) is None

  # Restart with same secret_file keeps existing cookie valid
  auth2 = WebAuthManager(
    WebAuthConfig(
      username="admin",
      password="secret_web_password",
      session_ttl_hours=2.0,
      secret_file=secret_file,
    )
  )
  verified = auth2.verify_session_token(token, now_ts=now_ts + 60)
  assert verified is not None and verified.username == "admin"

  # Changing password invalidates old session cookies immediately
  auth_changed = WebAuthManager(
    WebAuthConfig(
      username="admin",
      password="brand_new_password",
      session_ttl_hours=2.0,
      secret_file=secret_file,
    )
  )
  assert auth_changed.verify_session_token(token, now_ts=now_ts + 60) is None


def test_store_m3u8_and_gzip_xmltv_fallbacks(tmp_path: Path) -> None:
  import gzip
  from datetime import datetime, timezone

  # M3U8 fallback + gzipped XMLTV fallback in LineupGuideStore when output/xtream is empty
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

  store = LineupGuideStore(empty_xtream, m3u8_path=m3u8_file, xmltv_path=gz_xml)
  snap = store.get_snapshot()
  assert (
    snap.xmltv_url == "http://panel.example/xmltv.php?username=u&password=p"
  )
  assert len(snap.channels) == 1
  assert snap.channels[0].chno == 1108
  assert snap.channels[0].category_name == "Sports"
  assert snap.programs_by_epg_id["RDS.ca"][0].title == "Hockey Canadien"


@pytest.mark.asyncio
async def test_public_logo_and_favicon_served_before_auth(
  tmp_path: Path,
) -> None:
  from bellhop_web.auth import WebAuthConfig, WebAuthManager

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
      # Unauthenticated /login renders centered Bellhop splash logo, favicons & theme toggle
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

      # Brand, favicon, shared stylesheet, prefs & login controller are public before auth
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

      # Private app JS remains protected behind auth
      async with client.get("/static/app.js", allow_redirects=False) as r_app:
        assert r_app.status == 401

      # Authenticated index.html renders upper-left logo, favicons, prefs.js & theme toggle
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
        assert 'id="theme-toggle-btn"' in index_text
        assert 'data-view="cards"' in index_text
        assert ">Cards</button>" in index_text
        assert ">Timeline</button>" in index_text

      async with client.get("/static/styles.css") as styles_resp:
        assert styles_resp.status == 200
        styles_text = await styles_resp.text()
        assert ':root[data-theme="light"]' in styles_text
        assert ".cat-count.is-matched" in styles_text
        assert ".cat-count.is-empty" in styles_text
        assert ".timeline-body" in styles_text

      async with client.get("/static/app.js") as auth_app_js:
        assert auth_app_js.status == 200
        app_js_body = await auth_app_js.text()
        assert "&#39;" in app_js_body
        assert "onerror=" not in app_js_body
        assert "data-logo-fallback" in app_js_body
  finally:
    await runner.cleanup()
    await app.shutdown()


@pytest.mark.asyncio
async def test_login_rate_limiter_and_html_single_quote_escaping(
  tmp_path: Path,
) -> None:
  from bellhop_web.auth import (
    LoginRateLimiter,
    WebAuthConfig,
    WebAuthManager,
    render_login_page_html,
  )

  # 1. HTML escaping on /login escapes single quotes as &#39;
  rendered = render_login_page_html(
    next_url="/?q=O'Connor", error_message="Can't sign in"
  ).decode("utf-8")
  assert "&#39;" in rendered
  assert "Can't" not in rendered
  assert "<script>" not in rendered

  # 2. Deterministic sliding-window unit checks on LoginRateLimiter
  unit_limiter = LoginRateLimiter(max_failures=3, window_sec=30.0)
  assert unit_limiter.check("10.0.0.5", now=100.0) == 0
  unit_limiter.record_failure("10.0.0.5", now=100.0)
  unit_limiter.record_failure("10.0.0.5", now=105.0)
  assert unit_limiter.check("10.0.0.5", now=106.0) == 0
  unit_limiter.record_failure("10.0.0.5", now=110.0)
  # 3rd failure within 30s throttles only that IP
  assert unit_limiter.check("10.0.0.5", now=110.0) == 20
  assert unit_limiter.check("10.0.0.6", now=110.0) == 0
  # Window slide after oldest failure expires (t=130.1) unblocks 1 slot
  assert unit_limiter.check("10.0.0.5", now=130.1) == 0
  # Successful login clears recorded failures immediately
  unit_limiter.record_failure("10.0.0.5", now=131.0)
  assert unit_limiter.check("10.0.0.5", now=131.0) > 0
  unit_limiter.record_success("10.0.0.5")
  assert unit_limiter.check("10.0.0.5", now=131.0) == 0

  # 3. Live HTTP 429 Too Many Requests + Retry-After on POST /api/auth/login
  xt_dir = tmp_path / "output" / "xtream"
  _write_sample_xtream_dir(xt_dir, int(time.time()))
  auth = WebAuthManager(
    WebAuthConfig(
      username="admin",
      password="correct_password",
      secret_file=tmp_path / "session_secret.key",
    )
  )
  live_limiter = LoginRateLimiter(max_failures=3, window_sec=60.0)
  app = WebGuideApp(
    LineupGuideStore(xt_dir),
    auth=auth,
    login_limiter=live_limiter,
    settle_sec=0.01,
  )
  await app.startup()
  runner, port = await start_site(app.build_app(), "127.0.0.1", 0)
  base = f"http://127.0.0.1:{port}"

  try:
    async with new_test_session(base) as client:
      for _ in range(3):
        async with client.post(
          "/api/auth/login",
          json={"username": "admin", "password": "wrong"},
        ) as bad_resp:
          assert bad_resp.status == 401

      # 4th attempt is throttled with 429 + Retry-After (even if password is right!)
      async with client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "correct_password"},
      ) as throttled_json:
        assert throttled_json.status == 429
        assert int(throttled_json.headers["Retry-After"]) >= 1
        assert "Too many" in (await throttled_json.json())["error"]

      async with client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "wrong"},
        allow_redirects=False,
      ) as throttled_form:
        assert throttled_form.status == 429
        assert int(throttled_form.headers["Retry-After"]) >= 1

      # By default (`trust_proxy_headers=False`), spoofing X-Forwarded-For CANNOT bypass 429!
      async with client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "correct_password"},
        headers={"X-Forwarded-For": "203.0.113.77"},
      ) as spoof_attempt:
        assert spoof_attempt.status == 429
  finally:
    await runner.cleanup()
    await app.shutdown()


@pytest.mark.asyncio
async def test_trusted_reverse_proxy_xff_resolution_and_allowlist(
  tmp_path: Path,
) -> None:
  from bellhop_web.auth import (
    LoginRateLimiter,
    WebAuthConfig,
    WebAuthManager,
  )
  from bellhop_web.http_utils import (
    parse_trusted_proxy_networks,
    resolve_client_ip,
  )

  # 1. Pure unit tests: disabled flag, untrusted peer, multi-hop chain, IPv4-mapped IPv6
  nets = parse_trusted_proxy_networks(["127.0.0.1", "::1", "172.16.0.0/12"])
  # Flag disabled -> always returns remote peer IP
  assert (
    resolve_client_ip(
      "127.0.0.1",
      xff_headers=["203.0.113.5"],
      trust_proxy_headers=False,
      trusted_networks=nets,
    )
    == "127.0.0.1"
  )
  # Flag enabled, but peer IP is NOT in hardcoded trusted_proxy_ips -> ignores XFF!
  assert (
    resolve_client_ip(
      "198.51.100.9",
      xff_headers=["203.0.113.5"],
      trust_proxy_headers=True,
      trusted_networks=nets,
    )
    == "198.51.100.9"
  )
  # Flag enabled, peer is trusted (`::ffff:127.0.0.1`), client spoofed `1.1.1.1` on the left,
  # edge proxy (`172.18.0.2`) appended real client `203.0.113.42` -> picks `203.0.113.42`!
  assert (
    resolve_client_ip(
      "::ffff:127.0.0.1",
      xff_headers=["1.1.1.1", "203.0.113.42, 172.18.0.2"],
      trust_proxy_headers=True,
      trusted_networks=nets,
    )
    == "203.0.113.42"
  )
  # Fallback to X-Real-IP when X-Forwarded-For is absent on a trusted proxy
  assert (
    resolve_client_ip(
      "127.0.0.1",
      real_ip_header="203.0.113.88",
      trust_proxy_headers=True,
      trusted_networks=nets,
    )
    == "203.0.113.88"
  )

  # 2. Live HTTP check: trusted proxy (`127.0.0.1` in allowlist) isolates rate limit per XFF client,
  #    while a hardcoded allowlist excluding `127.0.0.1` (`["10.99.0.1"]`) ignores XFF!
  xt_dir = tmp_path / "output" / "xtream"
  _write_sample_xtream_dir(xt_dir, int(time.time()))
  auth = WebAuthManager(
    WebAuthConfig(
      username="admin",
      password="correct_password",
      secret_file=tmp_path / "session_secret.key",
    )
  )

  # Case A: `trust_proxy_headers=True` with `trusted_proxy_ips=["127.0.0.1"]`
  app_trusted = WebGuideApp(
    LineupGuideStore(xt_dir),
    auth=auth,
    login_limiter=LoginRateLimiter(max_failures=2, window_sec=60.0),
    trust_proxy_headers=True,
    trusted_proxy_ips=["127.0.0.1"],
    settle_sec=0.01,
  )
  await app_trusted.startup()
  runner_a, port_a = await start_site(app_trusted.build_app(), "127.0.0.1", 0)
  try:
    async with new_test_session(f"http://127.0.0.1:{port_a}") as client:
      # Attacker at 203.0.113.10 trips rate limit (even when injecting fake leftmost XFF!)
      for fake_prefix in ("8.8.8.8", "9.9.9.9"):
        async with client.post(
          "/api/auth/login",
          json={"username": "admin", "password": "wrong"},
          headers={"X-Forwarded-For": f"{fake_prefix}, 203.0.113.10"},
        ) as r_bad:
          assert r_bad.status == 401

      async with client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "correct_password"},
        headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.10"},
      ) as r_blocked:
        assert r_blocked.status == 429

      # Legitimate user at 203.0.113.20 behind the same reverse proxy is NOT throttled!
      async with client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "correct_password"},
        headers={"X-Forwarded-For": "203.0.113.20"},
      ) as r_legit:
        assert r_legit.status == 200
  finally:
    await runner_a.cleanup()
    await app_trusted.shutdown()

  # Case B: `trust_proxy_headers=True`, but `127.0.0.1` is NOT in `trusted_proxy_ips=["10.99.0.1"]`
  app_unlisted = WebGuideApp(
    LineupGuideStore(xt_dir),
    auth=auth,
    login_limiter=LoginRateLimiter(max_failures=2, window_sec=60.0),
    trust_proxy_headers=True,
    trusted_proxy_ips=["10.99.0.1"],
    settle_sec=0.01,
  )
  await app_unlisted.startup()
  runner_b, port_b = await start_site(app_unlisted.build_app(), "127.0.0.1", 0)
  try:
    async with new_test_session(f"http://127.0.0.1:{port_b}") as client:
      for spoof_ip in ("203.0.113.1", "203.0.113.2"):
        async with client.post(
          "/api/auth/login",
          json={"username": "admin", "password": "wrong"},
          headers={"X-Forwarded-For": spoof_ip},
        ) as r_bad2:
          assert r_bad2.status == 401

      # Because 127.0.0.1 is not in trusted_proxy_ips, XFF was ignored and 127.0.0.1 is throttled!
      async with client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "correct_password"},
        headers={"X-Forwarded-For": "203.0.113.99"},
      ) as r_blocked2:
        assert r_blocked2.status == 429
  finally:
    await runner_b.cleanup()
    await app_unlisted.shutdown()


def test_wcag_aa_contrast_landmarks_and_keyboard_a11y_contracts() -> None:
  import re

  static_dir = (
    Path(__file__).resolve().parents[1] / "src" / "bellhop_web" / "static"
  )
  index_html = (static_dir / "index.html").read_text(encoding="utf-8")
  styles_css = (static_dir / "styles.css").read_text(encoding="utf-8")
  app_js = (static_dir / "app.js").read_text(encoding="utf-8")

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

  # 2. Focus trap, focus restore & Timeline keyboard navigation in compiled app.js
  assert "trapFocusInContainer" in app_js
  assert "restoreFocus" in app_js
  assert "ArrowRight" in app_js and "ArrowLeft" in app_js
  assert ".tl-ch-cell:focus-visible" in styles_css

  # 3. WCAG 2.1 AA contrast >= 4.5:1 for muted/empty category text in both themes
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
