"""Tests for the Bellhop Web Guide & Live Player (`bellhop serve`)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from bellhop_web.epg_refresh import EpgRefreshConfig, EpgRefreshService
from bellhop_web.server import (
  WebGuideApp,
  build_access_urls,
  resolve_bind_host,
)
from bellhop_web.store import LineupGuideStore


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

  def mock_upstream(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path.endswith("/498715.ts") or path.endswith("/500101.ts"):
      return httpx.Response(
        200, headers={"content-type": "video/mp2t"}, content=ts_bytes
      )
    if path.endswith("/498715.m3u8"):
      manifest = "#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:4.0,\nseg001.ts\n"
      return httpx.Response(
        200,
        headers={"content-type": "application/vnd.apple.mpegurl"},
        text=manifest,
      )
    if path.endswith("/seg001.ts"):
      return httpx.Response(
        200, headers={"content-type": "video/mp2t"}, content=ts_bytes
      )
    return httpx.Response(404)

  store = LineupGuideStore(xt_dir)
  app = WebGuideApp(
    store,
    settle_sec=0.01,
    upstream_transport=httpx.MockTransport(mock_upstream),
  )
  await app.startup()
  server = await asyncio.start_server(
    app.handle_connection, host="127.0.0.1", port=0
  )
  port = server.sockets[0].getsockname()[1]
  base_url = f"http://127.0.0.1:{port}"

  try:
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
      # 1. Root index.html (including stateful query-param deep links) & assets
      idx_resp = await client.get("/")
      assert idx_resp.status_code == 200
      assert "player-overlay" in idx_resp.text
      assert "fullscreen-btn" in idx_resp.text
      assert "hide-empty-epg-btn" in idx_resp.text
      assert "data-lang=" not in idx_resp.text
      deep_resp = await client.get(
        "/?cat=2&epg=1&view=timeline&mode=hls&ch=1101"
      )
      assert deep_resp.status_code == 200
      assert "player-overlay" in deep_resp.text
      app_js_resp = await client.get("/static/app.js")
      assert app_js_resp.status_code == 200
      assert "readStateFromUrl" in app_js_resp.text
      assert "syncUrlFromState" in app_js_resp.text
      assert "hideEmptyGuide" in app_js_resp.text
      assert (await client.get("/static/vendor/mpegts.js")).status_code == 200
      assert (await client.get("/static/vendor/hls.min.js")).status_code == 200

      # 2. Health endpoint
      health = (await client.get("/api/health")).json()
      assert health["channel_count"] == 2
      assert health["category_count"] == 2
      assert health["epg_program_count"] == 3

      # 3. Lineup endpoint: logos, live EPG progress, credential redaction
      lu = (await client.get("/api/lineup")).json()
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
      sports_lu = (
        await client.get("/api/lineup", params={"category": "2"})
      ).json()
      assert [c["chno"] for c in sports_lu["channels"]] == [1401]

      search_lu = (
        await client.get("/api/lineup", params={"q": "radio-canada"})
      ).json()
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
      all_lu = (await client.get("/api/lineup")).json()
      assert [c["chno"] for c in all_lu["channels"]] == [1101, 1401]
      with_epg_lu = (
        await client.get("/api/lineup", params={"has_epg": "1"})
      ).json()
      assert [c["chno"] for c in with_epg_lu["channels"]] == [1101]

      # 5. Per-channel EPG schedule endpoint
      epg_1101 = (await client.get("/api/epg/1101")).json()
      assert len(epg_1101["programs"]) == 2

      # 6. MPEG-TS stream relay & tuner release
      ts_resp = await client.get("/api/stream/1101.ts")
      assert ts_resp.status_code == 200
      assert ts_resp.content == ts_bytes
      rel_resp = await client.post("/api/tuner/release")
      assert rel_resp.status_code == 200

      # 7. HLS manifest rewrite & segment relay
      hls_resp = await client.get("/api/hls/1101/index.m3u8")
      assert hls_resp.status_code == 200
      assert "/api/hls/1101/asset?u=" in hls_resp.text
      seg_line = next(
        ln
        for ln in hls_resp.text.splitlines()
        if ln.startswith("/api/hls/1101/asset")
      )
      seg_resp = await client.get(seg_line)
      assert seg_resp.status_code == 200
      assert seg_resp.content == ts_bytes
  finally:
    server.close()
    await server.wait_closed()
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

  def mock_epg_upstream(req: httpx.Request) -> httpx.Response:
    nonlocal fetch_count
    fetch_count += 1
    assert req.url.path.endswith("/xmltv.php")
    assert req.url.params.get("username") == "epg_u"
    assert req.url.params.get("password") == "super_secret_epg_pw"
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
    return httpx.Response(
      200, headers={"content-type": "application/xml"}, text=xml
    )

  store = LineupGuideStore(xt_dir)
  _tot, active_before, _max_stop = store.get_snapshot().epg_coverage_stats(
    now_ts=now_ts
  )
  assert active_before == 0

  cache_file = tmp_path / "cache" / "bellhop_web" / "epg_cache.json"
  refresher = EpgRefreshService(
    store,
    EpgRefreshConfig(
      enabled=True,
      refresh_hours=6.0,
      cache_file=cache_file,
      check_interval_sec=60.0,
    ),
    transport=httpx.MockTransport(mock_epg_upstream),
  )

  app = WebGuideApp(store, settle_sec=0.01, epg_refresher=refresher)
  await app.startup()
  server = await asyncio.start_server(
    app.handle_connection, host="127.0.0.1", port=0
  )
  port = server.sockets[0].getsockname()[1]
  base_url = f"http://127.0.0.1:{port}"

  try:
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
      for _ in range(50):
        health = (await client.get("/api/health")).json()
        if (
          health["epg_status"]["state"] == "ok"
          and health["epg_active_program_count"] > 0
        ):
          break
        await asyncio.sleep(0.05)

      assert fetch_count == 1
      assert cache_file.is_file()
      # bellhop-web keeps output/xtream/ read-only and stores its own compact JSON cache
      assert not (xt_dir / "get_epg.json").exists()
      assert "super_secret_epg_pw" not in cache_file.read_text(encoding="utf-8")
      assert "super_secret_epg_pw" not in json.dumps(health)

      # Temp raw XML file was cleaned up immediately after parsing
      assert list(cache_file.parent.glob("xmltv_fetch_*.xml")) == []

      # Lineup serves deduplicated live & upcoming programs automatically
      lu = (await client.get("/api/lineup")).json()
      assert "super_secret_epg_pw" not in json.dumps(lu)
      ch_ici = next(c for c in lu["channels"] if c["chno"] == 1101)
      assert ch_ici["now_playing"]["title"] == "Direct Live News"
      assert (
        "richer broadcast description" in ch_ici["now_playing"]["description"]
      )
      assert ch_ici["up_next"]["title"] == "Late Edition"

      # Manual POST /api/epg/refresh forces a fresh upstream fetch
      ref_resp = await client.post("/api/epg/refresh")
      assert ref_resp.status_code == 200
      assert ref_resp.json()["refreshed"] is True
      assert fetch_count == 2

      lu2 = (await client.get("/api/lineup")).json()
      ch_ici2 = next(c for c in lu2["channels"] if c["chno"] == 1101)
      assert ch_ici2["now_playing"]["title"] == "Updated Live Special"
  finally:
    server.close()
    await server.wait_closed()
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
    transport=httpx.MockTransport(mock_epg_upstream),
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
    transport=httpx.MockTransport(mock_epg_upstream),
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
    transport=httpx.MockTransport(mock_epg_upstream),
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
  from bellhop_web.http_utils import bound_server_port

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
  server = await asyncio.start_server(app.handle_connection, "127.0.0.1", 0)
  port = bound_server_port(server, 0)
  base = f"http://127.0.0.1:{port}"

  try:
    async with httpx.AsyncClient(
      base_url=base, follow_redirects=False
    ) as client:
      # Unauthenticated /login renders centered Bellhop splash logo, favicons & theme toggle
      login_resp = await client.get("/login")
      assert login_resp.status_code == 200
      assert "/static/bellhop-logo.svg" in login_resp.text
      assert 'href="/favicon.ico"' in login_resp.text
      assert "/static/prefs.js" in login_resp.text
      assert 'id="theme-toggle-btn"' in login_resp.text
      assert 'class="icon-sun"' in login_resp.text
      assert 'class="icon-moon"' in login_resp.text

      # Brand, favicon & client-prefs assets are publicly accessible before authentication
      svg_resp = await client.get("/static/bellhop-logo.svg")
      assert svg_resp.status_code == 200
      assert "image/svg+xml" in svg_resp.headers["content-type"]
      assert "<svg" in svg_resp.text

      fav_svg = await client.get("/favicon.svg")
      assert fav_svg.status_code == 200
      assert "image/svg+xml" in fav_svg.headers["content-type"]

      fav_ico = await client.get("/favicon.ico")
      assert fav_ico.status_code == 200
      assert fav_ico.content[:4] == b"\x00\x00\x01\x00"

      prefs_js = await client.get("/static/prefs.js")
      assert prefs_js.status_code == 200
      assert "javascript" in prefs_js.headers["content-type"]
      assert "bellhop.settings" in prefs_js.text

      # Private app JS remains protected behind auth
      assert (await client.get("/static/app.js")).status_code == 401

      # Authenticated index.html renders upper-left logo, favicons, prefs.js & theme toggle
      sign_in = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
      )
      assert sign_in.status_code == 200
      index_resp = await client.get("/")
      assert index_resp.status_code == 200
      assert 'class="brand-logo"' in index_resp.text
      assert "/static/bellhop-logo.svg" in index_resp.text
      assert 'href="/favicon.ico"' in index_resp.text
      assert "/static/prefs.js" in index_resp.text
      assert 'id="theme-toggle-btn"' in index_resp.text
      assert 'data-view="cards"' in index_resp.text
      assert ">Cards</button>" in index_resp.text
      assert ">Timeline</button>" in index_resp.text

      styles_resp = await client.get("/static/styles.css")
      assert styles_resp.status_code == 200
      assert ':root[data-theme="light"]' in styles_resp.text
      assert ".cat-count.is-matched" in styles_resp.text
      assert ".cat-count.is-empty" in styles_resp.text
      assert ".timeline-body" in styles_resp.text
  finally:
    server.close()
    await server.wait_closed()
    await app.shutdown()
