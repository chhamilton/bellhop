"""Tests for Xtream API client, two-stage stream validator, M3U8 output, and
CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web

from bellhop.cli import main
from bellhop.config import (
  AppConfig,
  LineupConfig,
  OutputConfig,
  ValidationConfig,
  XtreamConfig,
)
from bellhop.pipeline.lineup_loader import CableLineup
from bellhop.pipeline.models import (
  LineupChannel,
  MappedChannel,
  MatchCandidate,
  XtreamAccountInfo,
  XtreamStream,
)
from bellhop.pipeline.playlist import build_report_dict, render_m3u8
from bellhop.pipeline.validator import (
  StreamValidator,
  effective_concurrency,
  has_mpegts_sync,
)
from bellhop.xtream import fetch_xtream_catalog
from tests.http_mock import MockHttpServer


def _make_mpegts_payload(packets: int = 60) -> bytes:
  """Construct valid 188-byte MPEG-TS packets starting with 0x47 sync byte."""
  pkt = bytes([0x47, 0x40, 0x00, 0x10]) + (b"\xff" * 184)
  assert len(pkt) == 188
  return pkt * packets


@pytest.mark.asyncio
async def test_fetch_xtream_catalog_and_cache(tmp_path: Path) -> None:
  calls = {"count": 0}

  def handler(request: web.Request) -> web.Response:
    calls["count"] += 1
    action = request.query.get("action")
    if action is None:
      return web.json_response(
        {
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
        }
      )
    if action == "get_live_categories":
      return web.json_response(
        [{"category_id": "12", "category_name": "Canada | French"}]
      )
    if action == "get_live_streams":
      return web.json_response(
        [
          {
            "num": 1,
            "name": "QC | RDS FHD",
            "stream_type": "live",
            "stream_id": 9001,
            "stream_icon": "http://img.example/rds.png",
            "epg_channel_id": "RDS.ca",
            "category_id": "12",
          }
        ]
      )
    return web.Response(status=400)

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

  async with MockHttpServer(handler) as mock, mock.new_session() as client:
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

  def stream_handler(request: web.Request) -> web.Response:
    path = request.path
    if path.endswith("/1001.ts"):
      # Dead stream #1
      return web.Response(status=502, text="Bad Gateway")
    if path.endswith("/1002.ts"):
      # Working MPEG-TS stream #2
      return web.Response(
        status=200,
        headers={"Content-Type": "video/mp2t"},
        body=ts_bytes,
      )
    if path.endswith("/2001.m3u8"):
      manifest = (
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=4000000\nhls/variant.m3u8\n"
      )
      return web.Response(
        status=200,
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
        text=manifest,
      )
    if path.endswith("/hls/variant.m3u8"):
      media = "#EXTM3U\n#EXTINF:4.0,\nseg0.ts\n"
      return web.Response(
        status=200,
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
        text=media,
      )
    if path.endswith("/hls/seg0.ts"):
      return web.Response(
        status=200,
        headers={"Content-Type": "video/mp2t"},
        body=ts_bytes,
      )
    return web.Response(status=404)

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
  async with MockHttpServer(stream_handler) as mock:
    validator = StreamValidator(
      cfg, account, session_factory=mock.session_factory
    )
    await validator.validate_mappings([m_rds])

    assert m_rds.probes_attempted == 2
    assert m_rds.selected is not None
    assert m_rds.selected.stream.stream_id == 1002
    assert (
      m_rds.probe is not None
      and m_rds.probe.ok
      and m_rds.probe.ts_sync_verified
    )

    # Also verify HLS playlist -> variant -> segment traversal
    cfg.xtream.stream_format = "m3u8"
    m_ctv = MappedChannel(
      channel=ch_ctv,
      candidates=[MatchCandidate(stream=s_hls, score=92.0)],
    )
    hls_validator = StreamValidator(
      cfg, account, session_factory=mock.session_factory
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
paths:
  state_dir: "{tmp_path / "state"}"
  seed_json: "{sample_json}"
  m3u8_playlist_path: "{out_m3u8}"
  m3u8_report_path: "{out_report}"
  xtream_output_dir: "{tmp_path / "xtream_build"}"
pipeline:
  lineup:
    numbering_mode: "hd"
  epg:
    verify: false
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
paths:
  state_dir: "{tmp_path / "state1"}"
  seed_json: "{sample_json}"
  m3u8_playlist_path: "{out_full}"
  m3u8_report_path: "{tmp_path / "r1.json"}"
  xtream_output_dir: "{tmp_path / "xtream1"}"
pipeline:
  lineup:
    collapse_sd_dupes: false
  validation:
    enabled: false
  epg:
    verify: false
  output:
    xtream_enabled: false
""",
    encoding="utf-8",
  )

  # 1. Default (collapse_sd_dupes: false): emits both 101 (SD) and 1101 (HD) +
  #    148 + 901
  assert main(["-c", str(cfg_full), "--quiet"]) == 0
  full_text = out_full.read_text(encoding="utf-8")
  assert 'tvg-chno="101"' in full_text
  assert 'tvg-chno="1101"' in full_text
  assert 'tvg-chno="148"' in full_text
  assert 'tvg-chno="901"' in full_text

  # 2. With collapse_sd_dupes: true in config.yaml: collapses 101 duplicate,
  #    keeps 1101 HD + 148 & 901
  cfg_collapsed = tmp_path / "config_collapsed.yaml"
  cfg_collapsed.write_text(
    f"""\
paths:
  state_dir: "{tmp_path / "state2"}"
  seed_json: "{sample_json}"
  m3u8_playlist_path: "{out_collapsed}"
  m3u8_report_path: "{tmp_path / "r2.json"}"
  xtream_output_dir: "{tmp_path / "xtream2"}"
pipeline:
  lineup:
    collapse_sd_dupes: true
  validation:
    enabled: false
  epg:
    verify: false
  output:
    xtream_enabled: false
""",
    encoding="utf-8",
  )
  assert main(["run", "-c", str(cfg_collapsed), "--quiet"]) == 0
  collapsed_text = out_collapsed.read_text(encoding="utf-8")
  assert 'tvg-chno="101"' not in collapsed_text
  assert 'tvg-chno="1101"' in collapsed_text
  assert 'tvg-chno="148"' in collapsed_text
  assert 'tvg-chno="901"' in collapsed_text


def test_exported_group_dial_ranges_reflect_surviving_validated_channels(
  tmp_path: Path,
) -> None:
  """Verify every exported group carries `(lo–hi)` computed from surviving
  channels only.
  """
  from bellhop.pipeline.xtream_exporter import build_xtream_export_bundle

  lineup = CableLineup(
    lineup_id="TVG-9100000607",
    provider="Bell Fibe TV",
    market="Montréal, QC",
    postal_prefix="H2Y",
    channels=[
      # Science & Docs initially had no range, and ch 1520 failed validation ->
      # (1522–1528)
      LineupChannel(
        chno=1520, name="Dead Doc", group="Science & Docs", language="en"
      ),
      LineupChannel(
        chno=1522, name="History", group="Science & Docs", language="en"
      ),
      LineupChannel(
        chno=1528, name="Discovery", group="Science & Docs", language="en"
      ),
      # Entertainment initially had stale raw range (1600–2309); 1600 & 2309
      # failed -> (1602–1660)
      LineupChannel(
        chno=1600,
        name="Dead Lifestyle",
        group="Entertainment & Lifestyle (1600–2309)",
        language="en",
      ),
      LineupChannel(
        chno=1602,
        name="HGTV",
        group="Entertainment & Lifestyle (1600–2309)",
        language="en",
      ),
      LineupChannel(
        chno=1660,
        name="Slice",
        group="Entertainment & Lifestyle (1600–2309)",
        language="en",
      ),
      LineupChannel(
        chno=2309,
        name="International Barker",
        group="Entertainment & Lifestyle (1600–2309)",
        language="en",
      ),
    ],
  )

  def _ok_cand(sid: int, title: str) -> MatchCandidate:
    return MatchCandidate(
      stream=XtreamStream(stream_id=sid, name=title),
      score=100.0,
      reasons=["exact"],
      quality_tier="hd",
      has_native_epg=False,
    )

  ch_map = {c.chno: c for c in lineup.channels}
  mappings = [
    MappedChannel(channel=ch_map[1520], candidates=[], selected=None),
    MappedChannel(
      channel=ch_map[1522],
      candidates=[_ok_cand(1, "History")],
      selected=_ok_cand(1, "History"),
    ),
    MappedChannel(
      channel=ch_map[1528],
      candidates=[_ok_cand(2, "Discovery")],
      selected=_ok_cand(2, "Discovery"),
    ),
    MappedChannel(channel=ch_map[1600], candidates=[], selected=None),
    MappedChannel(
      channel=ch_map[1602],
      candidates=[_ok_cand(3, "HGTV")],
      selected=_ok_cand(3, "HGTV"),
    ),
    MappedChannel(
      channel=ch_map[1660],
      candidates=[_ok_cand(4, "Slice")],
      selected=_ok_cand(4, "Slice"),
    ),
    MappedChannel(channel=ch_map[2309], candidates=[], selected=None),
  ]

  cfg = AppConfig(
    project_root=tmp_path,
    lineup=LineupConfig(collapse_sd_dupes=True),
  )
  m3u8 = render_m3u8(lineup, mappings, cfg)
  assert 'group-title="Science & Docs (1522–1528)"' in m3u8
  assert 'group-title="Entertainment & Lifestyle (1602–1660)"' in m3u8
  assert "1600–2309" not in m3u8

  report = build_report_dict(lineup, mappings, cfg)
  assert set(report["groups"].keys()) == {
    "Science & Docs (1522–1528)",
    "Entertainment & Lifestyle (1602–1660)",
  }

  xt_bundle = build_xtream_export_bundle(lineup, mappings, cfg)
  assert [c.category_name for c in xt_bundle.categories] == [
    "Science & Docs (1522–1528)",
    "Entertainment & Lifestyle (1602–1660)",
  ]
  assert {s.category_name for s in xt_bundle.streams} == {
    "Science & Docs (1522–1528)",
    "Entertainment & Lifestyle (1602–1660)",
  }


@pytest.mark.asyncio
async def test_validator_and_report_redact_secrets_inside_error_strings(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  """Verify `ProbeResult.to_dict` and `StreamValidator` scrub error secrets."""
  from bellhop.pipeline.models import ProbeResult

  raw_probe = ProbeResult(
    stream_id=777,
    url="http://provider.example/live/alice/s3cr3t_pw/777.ts",
    ok=False,
    error=(
      "ffprobe failed: http://provider.example/live/alice/s3cr3t_pw/777.ts: "
      "s3cr3t_pw rejected"
    ),
  )
  redacted_dict = raw_probe.to_dict(redact_secret="s3cr3t_pw")
  assert "s3cr3t_pw" not in str(redacted_dict["url"])
  assert "s3cr3t_pw" not in str(redacted_dict["error"])
  assert "***" in str(redacted_dict["error"])

  ch = LineupChannel(chno=1101, name="RDS", group="Sports", language="fr")
  stream = XtreamStream(stream_id=777, name="CA: RDS")
  cand = MatchCandidate(
    stream=stream, score=100.0, reasons=["exact"], quality_tier="hd"
  )
  cfg = AppConfig(
    project_root=tmp_path,
    xtream=XtreamConfig(
      server_url="http://provider.example",
      username="alice",
      password="s3cr3t_pw",
      stream_format="ts",
    ),
    validation=ValidationConfig(
      concurrency=1,
      inter_probe_delay_sec=0.0,
      min_bytes_to_read=512,
      use_ffprobe=True,
      detect_black_screen=False,
    ),
  )

  ts_payload = _make_mpegts_payload(20)

  def _ok_stream(_req: web.Request) -> web.Response:
    return web.Response(
      status=200, headers={"Content-Type": "video/mp2t"}, body=ts_payload
    )

  class _FakeProc:
    returncode = 1

    async def communicate(self) -> tuple[bytes, bytes]:
      return (
        b"",
        b"http://provider.example/live/alice/s3cr3t_pw/777.ts: Server error",
      )

  async def _fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
    return _FakeProc()

  monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

  async with MockHttpServer(_ok_stream) as mock, mock.new_session() as client:
    validator = StreamValidator(
      cfg,
      XtreamAccountInfo(username="alice", max_connections=1),
      session_factory=mock.session_factory,
    )
    validator.ffprobe_path = "/usr/bin/ffprobe"
    res = await validator.probe_stream(stream, client)
    assert res.ok is False
    assert res.error is not None
    assert "s3cr3t_pw" not in res.error

  mapped = MappedChannel(
    channel=ch, candidates=[cand], selected=cand, probe=res
  )
  report = build_report_dict(
    CableLineup(
      lineup_id="TEST",
      provider="TestTV",
      market="Test",
      postal_prefix="",
      channels=[ch],
    ),
    [mapped],
    cfg,
  )
  assert "s3cr3t_pw" not in json.dumps(report)
