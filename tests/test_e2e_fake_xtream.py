"""End-to-end integration tests driving `PipelineRunner` + `WebGuideApp` against `FakeXtreamServer`."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from bellhop.config import load_config
from bellhop.runner import PipelineRunner, PipelineRunOptions
from bellhop_web.epg_refresh import EpgRefreshConfig, EpgRefreshService
from bellhop_web.server import WebGuideApp
from bellhop_web.store import LineupGuideStore
from tests.fake_xtream_server import (
  FakeProgram,
  FakeStreamDef,
  FakeXtreamServer,
)


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
    group: Francophone
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


async def _read_first_ts_chunk(
  resp: httpx.Response,
  *,
  min_bytes: int = 188,
  timeout: float = 5.0,
) -> bytes:
  buf = bytearray()

  async def _collect() -> bytes:
    async for chunk in resp.aiter_bytes():
      buf.extend(chunk)
      if len(buf) >= min_bytes:
        return bytes(buf)
    return bytes(buf)

  return await asyncio.wait_for(_collect(), timeout=timeout)


@pytest.mark.asyncio
async def test_e2e_pipeline_to_web_guide_passthrough_epg_and_single_tuner(
  tmp_path: Path,
) -> None:
  now_ts = int(time.time())
  fake = FakeXtreamServer(
    username="e2e_user",
    password="e2e_secret_password",
    max_connections=1,
    enforce_max_connections=True,
    categories=[
      {"category_id": "99", "category_name": "CA| FR RAW", "parent_id": 0}
    ],
    streams=[
      # Higher-ranked 4K stream redirects to /video/black.ts offline placeholder -> validator must reject!
      FakeStreamDef(
        stream_id=410100,
        name="CA FR: ICI MONTREAL 4K",
        category_id="99",
        epg_channel_id="ICITeleMontreal.ca",
        offline_placeholder=True,
      ),
      # Working fallback FHD stream -> validator must rescue Ch 1101 with this stream!
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
        start_ts=now_ts - 900,
        stop_ts=now_ts + 2700,
        description="Short",
      ),
      FakeProgram(
        channel_id="ICITeleMontreal.ca",
        title="Le Téléjournal",
        start_ts=now_ts - 900,
        stop_ts=now_ts + 2700,
        description="Édition complète de Montréal en direct.",
      ),
      FakeProgram(
        channel_id="ICITeleMontreal.ca",
        title="Tout le monde en parle",
        start_ts=now_ts + 2700,
        stop_ts=now_ts + 10800,
      ),
      FakeProgram(
        channel_id="RDS.ca",
        title="Hockey 360",
        start_ts=now_ts - 600,
        stop_ts=now_ts + 9000,
      ),
    ],
  )

  async with fake:
    lineup_file = tmp_path / "lineup.yaml"
    _write_mini_lineup(lineup_file)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
      f"""\
xtream:
  server_url: "{fake.base_url}"
  username: "{fake.username}"
  password: "{fake.password}"
  stream_format: "ts"
  cache_dir: "cache"
lineup:
  backend: "yaml"
  lineup_id: "CAN-BELLFIBE-MTL-X"
  file: "{lineup_file}"
  collapse_sd_dupes: true
state:
  state_dir: "state"
validation:
  enabled: true
  only_changed: true
  concurrency: 4
  ignore_account_limit: false
  inter_probe_delay_sec: 0.05
  http_timeout_sec: 5.0
  min_bytes_to_read: 4096
  use_ffprobe: false
epg:
  verify: true
  refresh_hours: 6.0
output:
  m3u8:
    enabled: true
    playlist_path: "output/lineup.m3u8"
    report_path: "output/report.json"
  xtream:
    enabled: true
    dir: "output/xtream"
""",
      encoding="utf-8",
    )

    # 1. Run full 6-phase pipeline against FakeXtreamServer
    cfg = load_config(cfg_path)
    result = await PipelineRunner(cfg).run(PipelineRunOptions())
    assert result.xtream_result is not None
    assert result.xtream_result.epg_verified is True
    assert result.xtream_result.emitted_streams == 2
    assert fake.peak_concurrent_streams == 1
    assert fake.connection_limit_violations == 0

    # Passthrough EPG only: no static get_epg.json / xmltv.xml written
    xt_dir = tmp_path / "output" / "xtream"
    assert not (xt_dir / "get_epg.json").exists()
    assert not (xt_dir / "xmltv.xml").exists()
    assert fake.xmltv_url in (tmp_path / "output" / "lineup.m3u8").read_text(
      encoding="utf-8"
    )

    # Validator rejected /video/black.ts (#410100) and rescued Ch 1101 with #410101
    exported_streams = json.loads(
      (xt_dir / "get_live_streams.json").read_text(encoding="utf-8")
    )
    stream_by_chno = {s["num"]: s for s in exported_streams}
    assert stream_by_chno[1101]["stream_id"] == 410101
    assert stream_by_chno[1108]["stream_id"] == 410108

    # 2. Boot real Bellhop Web Guide server against the exported files & FakeXtreamServer
    store = LineupGuideStore(xt_dir)
    epg_cache = tmp_path / "cache" / "bellhop_web" / "epg_cache.json"
    refresher = EpgRefreshService(
      store,
      EpgRefreshConfig(enabled=True, refresh_hours=6.0, cache_file=epg_cache),
    )
    app = WebGuideApp(store, settle_sec=0.05, epg_refresher=refresher)
    await app.startup()
    web_server = await asyncio.start_server(
      app.handle_connection, host="127.0.0.1", port=0
    )
    web_port = web_server.sockets[0].getsockname()[1]
    web_base = f"http://127.0.0.1:{web_port}"

    try:
      async with httpx.AsyncClient(base_url=web_base, timeout=10.0) as client:
        # Wait for automatic passthrough EPG fetch & disk cache persistence
        for _ in range(60):
          health = (await client.get("/api/health")).json()
          if (
            health["epg_status"]["state"] == "ok"
            and health["epg_active_program_count"] > 0
          ):
            break
          await asyncio.sleep(0.05)

        assert epg_cache.is_file()
        assert "e2e_secret_password" not in epg_cache.read_text(
          encoding="utf-8"
        )
        assert "e2e_secret_password" not in json.dumps(health)

        lu = (await client.get("/api/lineup")).json()
        ch_1101 = next(c for c in lu["channels"] if c["chno"] == 1101)
        assert ch_1101["now_playing"]["title"] == "Le Téléjournal"
        assert "Montréal en direct" in ch_1101["now_playing"]["description"]

        # Stream Ch 1101 live, then switch to Ch 1108 without releasing first (tuner preemption!)
        async with client.stream(
          "GET", "/api/stream/1101.ts?client=viewer-1"
        ) as s1:
          assert s1.status_code == 200
          data1 = await _read_first_ts_chunk(s1)
          assert data1[0] == 0x47 and data1[4] == 0xA1

          async with client.stream(
            "GET", "/api/stream/1108.ts?client=viewer-1"
          ) as s2:
            assert s2.status_code == 200
            data2 = await _read_first_ts_chunk(s2)
            assert data2[0] == 0x47 and data2[4] == 0xB8

        rel = await client.post("/api/tuner/release?client=viewer-1")
        assert rel.status_code == 200
        assert fake.peak_concurrent_streams == 1
        assert fake.connection_limit_violations == 0
    finally:
      web_server.close()
      await web_server.wait_closed()
      await app.shutdown()


@pytest.mark.asyncio
async def test_e2e_reload_channel_deep_link_race_placeholder_failover_and_hls(
  tmp_path: Path,
) -> None:
  fake = FakeXtreamServer(
    username="u",
    password="p",
    max_connections=1,
    enforce_max_connections=True,
    categories=[
      {"category_id": "1", "category_name": "General", "parent_id": 0}
    ],
    streams=[
      FakeStreamDef(
        stream_id=100,
        name="Dead Primary",
        category_id="1",
        offline_placeholder=True,
      ),
      FakeStreamDef(
        stream_id=101, name="Live Backup", category_id="1", marker_byte=0x55
      ),
      FakeStreamDef(
        stream_id=108, name="RDS Live", category_id="1", marker_byte=0x77
      ),
    ],
  )

  async with fake:
    xt_dir = tmp_path / "output" / "xtream"
    xt_dir.mkdir(parents=True, exist_ok=True)
    categories = [
      {"category_id": "1", "category_name": "General", "parent_id": 0}
    ]
    streams = [
      {
        "num": 1101,
        "name": "Channel With Dead Primary",
        "stream_type": "live",
        "stream_id": 100,
        "epg_channel_id": "ch1101.ca",
        "category_id": "1",
        "category_name": "General",
        # Primary redirects to /video/black.ts -> WebGuideApp must fail over to #101!
        "direct_source": fake.stream_url(100),
        "guide": {
          "language": "fr",
          "quality_tier": "hd",
          "fallback_sources": [fake.stream_url(101)],
        },
      },
      {
        "num": 1108,
        "name": "RDS",
        "stream_type": "live",
        "stream_id": 108,
        "epg_channel_id": "RDS.ca",
        "category_id": "1",
        "category_name": "General",
        "direct_source": fake.stream_url(108),
        "guide": {
          "language": "fr",
          "quality_tier": "fhd",
          "fallback_sources": [],
        },
      },
    ]
    bundle = {
      "lineup_info": {
        "lineup_id": "CAN-BELLFIBE-MTL-X",
        "provider": "Bell Fibe TV",
        "market": "Montréal, QC",
      },
      "server_info": {
        "timezone": "America/Montreal",
        "xmltv_url": fake.xmltv_url,
      },
      "live_categories": categories,
      "live_streams": streams,
    }
    (xt_dir / "bellhop_xtream_playlist.json").write_text(
      json.dumps(bundle), encoding="utf-8"
    )

    store = LineupGuideStore(xt_dir)
    app = WebGuideApp(store, settle_sec=0.05)
    await app.startup()
    web_server = await asyncio.start_server(
      app.handle_connection, host="127.0.0.1", port=0
    )
    web_port = web_server.sockets[0].getsockname()[1]
    web_base = f"http://127.0.0.1:{web_port}"

    try:
      async with httpx.AsyncClient(base_url=web_base, timeout=10.0) as client:
        # 1. Reload race: tab-old is streaming Ch 1101 (failing over past /video/black.ts to #101),
        #    then reloaded tab-new opens /api/stream/1101.ts?client=tab-new,
        #    then tab-old's delayed pagehide beacon hits /api/tuner/release?client=tab-old!
        async with client.stream(
          "GET", "/api/stream/1101.ts?client=tab-old"
        ) as old_stream:
          assert old_stream.status_code == 200
          old_chunk = await _read_first_ts_chunk(old_stream)
          assert (
            old_chunk[0] == 0x47 and old_chunk[4] == 0x55
          )  # rescued via fallback #101!

          async with client.stream(
            "GET", "/api/stream/1101.ts?client=tab-new"
          ) as new_stream:
            assert new_stream.status_code == 200
            new_iter = new_stream.aiter_bytes()
            new_chunk_1 = await asyncio.wait_for(anext(new_iter), timeout=5.0)
            assert new_chunk_1[0] == 0x47 and new_chunk_1[4] == 0x55

            # Delayed pagehide beacon from tab-old arrives AFTER tab-new acquired the tuner
            stale_rel = await client.post("/api/tuner/release?client=tab-old")
            assert stale_rel.status_code == 200
            assert stale_rel.json()["released"] is False

            # tab-new MUST keep receiving live chunks uninterrupted!
            new_chunk_2 = await asyncio.wait_for(anext(new_iter), timeout=5.0)
            assert len(new_chunk_2) > 0
            assert bytes([0x47, 0x40, 0x00, 0x10, 0x55]) in (
              new_chunk_1 + new_chunk_2
            )

        # Releasing from tab-new closes the tuner cleanly
        own_rel = await client.post("/api/tuner/release?client=tab-new")
        assert own_rel.status_code == 200
        await asyncio.sleep(0.05)
        assert fake.active_live_streams == 0
        assert fake.peak_concurrent_streams == 1
        assert fake.connection_limit_violations == 0

        # 2. HLS mode: 302 edge redirect + manifest rewrite + 60s edge session affinity
        hls1 = await client.get("/api/hls/1108/index.m3u8?client=hls-tab")
        assert hls1.status_code == 200
        seg_url = next(
          ln
          for ln in hls1.text.splitlines()
          if ln.startswith("/api/hls/1108/asset?u=")
        )
        seg_resp = await client.get(seg_url)
        assert seg_resp.status_code == 200
        assert seg_resp.content[0] == 0x47 and seg_resp.content[4] == 0x77

        # Second manifest refresh reuses cached /edge/tok1/108.m3u8 without minting tok2
        hls2 = await client.get("/api/hls/1108/index.m3u8?client=hls-tab")
        assert hls2.status_code == 200
        assert fake.hls_manifest_requests == [
          "/edge/tok1/108.m3u8",
          "/edge/tok1/108.m3u8",
        ]
    finally:
      web_server.close()
      await web_server.wait_closed()
      await app.shutdown()
