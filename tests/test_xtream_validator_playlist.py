"""Tests for Xtream API client, two-stage stream validator, M3U8 output, and CLI."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from bellhop.cli import main
from bellhop.config import (
  AppConfig,
  LineupConfig,
  OutputConfig,
  ValidationConfig,
  XtreamConfig,
)
from bellhop.lineup_loader import CableLineup
from bellhop.models import (
  LineupChannel,
  MappedChannel,
  MatchCandidate,
  XtreamAccountInfo,
  XtreamStream,
)
from bellhop.playlist import build_report_dict, render_m3u8
from bellhop.validator import (
  StreamValidator,
  effective_concurrency,
  has_mpegts_sync,
)
from bellhop.xtream import fetch_xtream_catalog


def _make_mpegts_payload(packets: int = 60) -> bytes:
  """Construct valid 188-byte MPEG-TS packets starting with 0x47 sync byte."""
  pkt = bytes([0x47, 0x40, 0x00, 0x10]) + (b"\xff" * 184)
  assert len(pkt) == 188
  return pkt * packets


@pytest.mark.asyncio
async def test_fetch_xtream_catalog_and_cache(tmp_path: Path) -> None:
  calls = {"count": 0}

  def handler(request: httpx.Request) -> httpx.Response:
    calls["count"] += 1
    action = request.url.params.get("action")
    if action is None:
      return httpx.Response(
        200,
        json={
          "user_info": {
            "username": "demo",
            "password": "secret_password",
            "auth": 1,
            "status": "Active",
            "active_cons": "0",
            "max_connections": "2",
            "allowed_output_formats": ["ts", "m3u8"],
          },
          "server_info": {"server_protocol": "http", "port": "8080"},
        },
      )
    if action == "get_live_categories":
      return httpx.Response(
        200,
        json=[{"category_id": "12", "category_name": "Canada | French"}],
      )
    if action == "get_live_streams":
      return httpx.Response(
        200,
        json=[
          {
            "num": 1,
            "name": "QC | RDS FHD",
            "stream_type": "live",
            "stream_id": 9001,
            "stream_icon": "http://img.example/rds.png",
            "epg_channel_id": "RDS.ca",
            "category_id": "12",
          }
        ],
      )
    return httpx.Response(400)

  cfg = AppConfig(
    project_root=tmp_path,
    xtream=XtreamConfig(
      server_url="http://panel.example:8080",
      username="demo",
      password="secret_password",
      cache_dir=Path("cache"),
      cache_ttl_hours=12.0,
    ),
  )

  async with httpx.AsyncClient(
    transport=httpx.MockTransport(handler)
  ) as client:
    cat1 = await fetch_xtream_catalog(cfg, client=client)
    assert not cat1.from_cache
    assert cat1.account.max_connections == 2
    assert len(cat1.streams) == 1
    assert cat1.streams[0].category_name == "Canada | French"
    assert calls["count"] == 3

    # Second call should hit disk cache with zero network requests
    cat2 = await fetch_xtream_catalog(cfg, client=client)
    assert cat2.from_cache
    assert len(cat2.streams) == 1
    assert calls["count"] == 3

  # Verify password was sanitized before writing cache file
  cache_files = list((tmp_path / "cache").glob("xtream_catalog_*.json"))
  assert len(cache_files) == 1
  assert "secret_password" not in cache_files[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_validator_candidate_fallback_and_hls(tmp_path: Path) -> None:
  ts_bytes = _make_mpegts_payload(55)
  assert has_mpegts_sync(ts_bytes)

  def stream_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/1001.ts"):
      # Dead stream #1
      return httpx.Response(502, text="Bad Gateway")
    if path.endswith("/1002.ts"):
      # Working MPEG-TS stream #2
      return httpx.Response(
        200,
        headers={"Content-Type": "video/mp2t"},
        content=ts_bytes,
      )
    if path.endswith("/2001.m3u8"):
      manifest = (
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=4000000\nhls/variant.m3u8\n"
      )
      return httpx.Response(
        200,
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
        text=manifest,
      )
    if path.endswith("/hls/variant.m3u8"):
      media = "#EXTM3U\n#EXTINF:4.0,\nseg0.ts\n"
      return httpx.Response(
        200,
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
        text=media,
      )
    if path.endswith("/hls/seg0.ts"):
      return httpx.Response(
        200,
        headers={"Content-Type": "video/mp2t"},
        content=ts_bytes,
      )
    return httpx.Response(404)

  cfg = AppConfig(
    project_root=tmp_path,
    xtream=XtreamConfig(
      server_url="http://panel.example:8080",
      username="u",
      password="p",
      stream_format="ts",
    ),
    validation=ValidationConfig(
      concurrency=4,
      inter_probe_delay_sec=0.0,
      min_bytes_to_read=4096,
      use_ffprobe=False,
    ),
  )
  account = XtreamAccountInfo(username="u", status="Active", max_connections=1)
  assert effective_concurrency(cfg, account) == 1

  ch_rds = LineupChannel(
    chno=1108,
    sd_chno=108,
    name="RDS",
    group="Francophone - Sports",
    language="fr",
  )
  s_dead = XtreamStream(stream_id=1001, name="QC | RDS Primary FHD")
  s_good = XtreamStream(stream_id=1002, name="QC | RDS Backup HD")
  m_rds = MappedChannel(
    channel=ch_rds,
    candidates=[
      MatchCandidate(stream=s_dead, score=95.0, reasons=["exact"]),
      MatchCandidate(stream=s_good, score=88.0, reasons=["exact"]),
    ],
  )

  ch_ctv = LineupChannel(
    chno=1201,
    sd_chno=201,
    name="CTV (Montréal)",
    group="English",
    language="en",
  )
  s_hls = XtreamStream(
    stream_id=2001,
    name="CA | CTV Montreal HLS",
    direct_source="http://panel.example:8080/live/u/p/2001.m3u8",
  )
  # Test direct HLS URL by clearing xtream server_url on a second validator or via .m3u8 format
  validator = StreamValidator(
    cfg, account, transport=httpx.MockTransport(stream_handler)
  )
  await validator.validate_mappings([m_rds])

  assert m_rds.probes_attempted == 2
  assert m_rds.selected is not None
  assert m_rds.selected.stream.stream_id == 1002
  assert (
    m_rds.probe is not None and m_rds.probe.ok and m_rds.probe.ts_sync_verified
  )

  # Also verify HLS playlist -> variant -> segment traversal
  cfg.xtream.stream_format = "m3u8"
  m_ctv = MappedChannel(
    channel=ch_ctv,
    candidates=[MatchCandidate(stream=s_hls, score=92.0)],
  )
  hls_validator = StreamValidator(
    cfg, account, transport=httpx.MockTransport(stream_handler)
  )
  await hls_validator.validate_mappings([m_ctv])
  assert m_ctv.selected is not None
  assert m_ctv.probe is not None and m_ctv.probe.ok and m_ctv.probe.is_hls


def test_playlist_numbering_modes_and_password_redaction(
  tmp_path: Path,
) -> None:
  lineup = CableLineup(
    provider="Bell Fibe TV",
    market="Montréal, QC",
    postal_prefix="H2Y",
    channels=[
      LineupChannel(
        chno=1101,
        sd_chno=101,
        name="ICI Radio-Canada Télé (Montréal)",
        group="Francophone - Généralistes",
        language="fr",
        tvg_id="CBFT.ca",
      )
    ],
  )
  stream = XtreamStream(
    stream_id=777,
    name="QC | ICI Radio-Canada Montreal FHD",
    stream_icon="http://logo/cbft.png",
  )
  mapped = MappedChannel(
    channel=lineup.channels[0],
    selected=MatchCandidate(stream=stream, score=99.0, reasons=["exact"]),
    candidates=[MatchCandidate(stream=stream, score=99.0, reasons=["exact"])],
  )

  cfg = AppConfig(
    project_root=tmp_path,
    xtream=XtreamConfig(
      server_url="http://panel.example:8080",
      username="alice",
      password="super_secret_pw",
      stream_format="ts",
    ),
    lineup=LineupConfig(numbering_mode="both"),
    output=OutputConfig(),
  )

  m3u8 = render_m3u8(lineup, [mapped], cfg)
  assert "#EXTM3U url-tvg=" in m3u8
  assert 'tvg-chno="101"' in m3u8
  assert 'tvg-chno="1101"' in m3u8
  assert 'tvg-id="CBFT.ca"' in m3u8
  assert "http://panel.example:8080/live/alice/super_secret_pw/777.ts" in m3u8

  report = build_report_dict(lineup, [mapped], cfg, total_catalog_streams=1)
  assert report["summary"]["active_channels_emitted"] == 1
  assert "super_secret_pw" not in json.dumps(report)


def test_cli_build_from_local_json(tmp_path: Path) -> None:
  sample_json = tmp_path / "xtream_streams.json"
  sample_json.write_text(
    json.dumps(
      [
        {
          "stream_id": 11,
          "name": "QC | ICI Radio-Canada Montréal FHD",
          "category_name": "QC",
        },
        {
          "stream_id": 12,
          "name": "QC | TVA Montréal FHD",
          "category_name": "QC",
        },
        {
          "stream_id": 13,
          "name": "CA | TSN 2 FHD",
          "category_name": "CA Sports",
        },
      ]
    ),
    encoding="utf-8",
  )
  out_m3u8 = tmp_path / "bell.m3u8"
  out_report = tmp_path / "report.json"
  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"""\
xtream:
  seed_json: "{sample_json}"
lineup:
  numbering_mode: "hd"
state:
  state_dir: "{tmp_path / "state"}"
epg:
  fetch: false
output:
  m3u8:
    playlist_path: "{out_m3u8}"
    report_path: "{out_report}"
  xtream:
    dir: "{tmp_path / "xtream_build"}"
""",
    encoding="utf-8",
  )

  rc = main(["run", "-c", str(cfg_path), "--skip", "validate", "--quiet"])
  assert rc == 0
  playlist_text = out_m3u8.read_text(encoding="utf-8")
  assert 'tvg-chno="1101"' in playlist_text
  assert 'tvg-chno="1103"' in playlist_text
  assert 'tvg-chno="1402"' in playlist_text


def test_collapse_sd_dupes_keeps_hd_and_sd_only_channels(
  tmp_path: Path,
) -> None:
  sample_json = tmp_path / "xtream_streams.json"
  sample_json.write_text(
    json.dumps(
      [
        {
          "stream_id": 11,
          "name": "QC | ICI Radio-Canada Montréal FHD",
          "category_name": "QC",
        },
        {"stream_id": 14, "name": "CA FR: CANAL M FM", "category_name": "QC"},
        {
          "stream_id": 15,
          "name": "AT&T: STINGRAY CLASSIC ROCK",
          "category_name": "US",
        },
      ]
    ),
    encoding="utf-8",
  )
  out_full = tmp_path / "bell_full.m3u8"
  out_collapsed = tmp_path / "bell_collapsed.m3u8"

  cfg_full = tmp_path / "config_full.yaml"
  cfg_full.write_text(
    f"""\
xtream:
  seed_json: "{sample_json}"
lineup:
  collapse_sd_dupes: false
state:
  state_dir: "{tmp_path / "state1"}"
validation:
  enabled: false
epg:
  fetch: false
output:
  m3u8:
    playlist_path: "{out_full}"
    report_path: "{tmp_path / "r1.json"}"
  xtream:
    enabled: false
    dir: "{tmp_path / "xtream1"}"
""",
    encoding="utf-8",
  )

  # 1. Default (collapse_sd_dupes: false): emits both 101 (SD) and 1101 (HD) + 148 + 901
  assert main(["-c", str(cfg_full), "--quiet"]) == 0
  full_text = out_full.read_text(encoding="utf-8")
  assert 'tvg-chno="101"' in full_text
  assert 'tvg-chno="1101"' in full_text
  assert 'tvg-chno="148"' in full_text
  assert 'tvg-chno="901"' in full_text

  # 2. With collapse_sd_dupes: true in config.yaml: collapses 101 duplicate, keeps 1101 HD + 148 & 901
  cfg_collapsed = tmp_path / "config_collapsed.yaml"
  cfg_collapsed.write_text(
    f"""\
xtream:
  seed_json: "{sample_json}"
lineup:
  collapse_sd_dupes: true
state:
  state_dir: "{tmp_path / "state2"}"
validation:
  enabled: false
epg:
  fetch: false
output:
  m3u8:
    playlist_path: "{out_collapsed}"
    report_path: "{tmp_path / "r2.json"}"
  xtream:
    enabled: false
    dir: "{tmp_path / "xtream2"}"
""",
    encoding="utf-8",
  )
  assert main(["run", "-c", str(cfg_collapsed), "--quiet"]) == 0
  collapsed_text = out_collapsed.read_text(encoding="utf-8")
  assert 'tvg-chno="101"' not in collapsed_text
  assert 'tvg-chno="1101"' in collapsed_text
  assert 'tvg-chno="148"' in collapsed_text
  assert 'tvg-chno="901"' in collapsed_text
