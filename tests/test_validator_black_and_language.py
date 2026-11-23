"""Tests for Stage 2/3 black-screen detection, audio-language track checking, and stateful feedback."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from aiohttp import web

from bellhop.config import (
  AppConfig,
  LineupConfig,
  StateConfig,
  ValidationConfig,
  XtreamConfig,
)
from bellhop.lineup_loader import CableLineup
from bellhop.mapping_state import (
  MappingDocument,
  load_mapping_document,
  reconcile_rough_mapping,
  save_mapping_document,
  validate_mapping_candidates,
)
from bellhop.models import (
  LineupChannel,
  XtreamAccountInfo,
  XtreamStream,
)
from bellhop.validator import (
  StreamValidator,
  check_audio_language_compatibility,
  normalize_audio_language_code,
  parse_blackdetect_duration,
)
from bellhop.xtream import XtreamCatalog
from tests.http_mock import MockHttpServer


def _make_mpegts_payload(packets: int = 55) -> bytes:
  pkt = bytes([0x47, 0x40, 0x00, 0x10]) + (b"\xff" * 184)
  return pkt * packets


def _write_fake_ffprobe_and_ffmpeg(bin_dir: Path) -> tuple[str, str]:
  """Create executable Python stand-ins for `ffprobe` and `ffmpeg` driven by URL stream ID."""
  bin_dir.mkdir(parents=True, exist_ok=True)
  py = sys.executable

  ffprobe_script = bin_dir / "fake_ffprobe"
  ffprobe_script.write_text(
    f"""#!{py}
import json
import sys

url = sys.argv[-1]
if "/4001." in url:
  # Valid H.264/AAC French headers, but video frames are pure black (caught in Stage 3)
  streams = [
    {{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001"}},
    {{"codec_type": "audio", "codec_name": "aac", "tags": {{"language": "fra"}}}},
  ]
elif "/5001." in url:
  # Cross-wired English feed (e.g. Yes TV on a Telemagino stream ID!)
  streams = [
    {{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "60/1"}},
    {{"codec_type": "audio", "codec_name": "aac", "tags": {{"LANGUAGE": "eng"}}}},
  ]
elif "/5002." in url:
  # Bilingual or French track present alongside undetermined track
  streams = [
    {{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720, "avg_frame_rate": "30/1"}},
    {{"codec_type": "audio", "codec_name": "ac3", "tags": {{"language": "und"}}}},
    {{"codec_type": "audio", "codec_name": "aac", "tags": {{"language": "fre"}}}},
  ]
elif "/6001." in url:
  # Untagged raw restream (no language tag -> neutral, must NOT be rejected)
  streams = [
    {{"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080, "r_frame_rate": "60/1"}},
    {{"codec_type": "audio", "codec_name": "aac", "tags": {{"language": "und"}}}},
  ]
else:
  streams = [
    {{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "30/1"}},
    {{"codec_type": "audio", "codec_name": "aac", "tags": {{"language": "fra"}}}},
  ]

print(json.dumps({{"streams": streams}}))
""",
    encoding="utf-8",
  )
  ffprobe_script.chmod(0o755)

  ffmpeg_script = bin_dir / "fake_ffmpeg"
  ffmpeg_script.write_text(
    f"""#!{py}
import sys

args = " ".join(sys.argv[1:])
if "/4001." in args:
  # Pure black 1.50s window -> triggers blackdetect rejection
  sys.stderr.write("[blackdetect @ 0x7f01] black_start:0 black_end:1.50 black_duration:1.50\\n")
elif "/5002." in args:
  # Brief 0.20s scene transition -> well below 85% threshold, must pass
  sys.stderr.write("[blackdetect @ 0x7f01] black_start:0.40 black_end:0.60 black_duration:0.20\\n")
sys.exit(0)
""",
    encoding="utf-8",
  )
  ffmpeg_script.chmod(0o755)

  return str(ffprobe_script), str(ffmpeg_script)


def test_language_normalization_and_blackdetect_parsing_helpers() -> None:
  assert normalize_audio_language_code("fra") == "fr"
  assert normalize_audio_language_code("FRE") == "fr"
  assert normalize_audio_language_code("en-US") == "en"
  assert normalize_audio_language_code("und") is None
  assert normalize_audio_language_code("mul") is None
  assert normalize_audio_language_code("") is None

  assert check_audio_language_compatibility("fr", ["en"]) == (False, False)
  assert check_audio_language_compatibility("fr", ["en", "fra"]) == (True, True)
  assert check_audio_language_compatibility("fr", ["und"]) == (True, False)
  assert check_audio_language_compatibility(
    "fr", ["en"], allow_audio_only=True
  ) == (
    True,
    False,
  )

  stderr_sample = (
    "[blackdetect @ 0x1] black_start:0 black_end:0.75 black_duration:0.75\n"
    "[blackdetect @ 0x1] black_start:0.80 black_end:1.50 black_duration:0.70\n"
  )
  assert parse_blackdetect_duration(stderr_sample) == pytest.approx(1.45)


@pytest.mark.asyncio
async def test_black_screen_rejection_and_slot_relative_audio_language_failover_and_state(
  tmp_path: Path,
) -> None:
  ts_bytes = _make_mpegts_payload()
  ffprobe_bin, ffmpeg_bin = _write_fake_ffprobe_and_ffmpeg(tmp_path / "bin")

  def stream_handler(_request: web.Request) -> web.Response:
    return web.Response(
      status=200,
      headers={"Content-Type": "video/mp2t"},
      body=ts_bytes,
    )

  lineup = CableLineup(
    lineup_id="TVG-9100000607",
    provider="Bell Fibe TV",
    market="Montréal, QC",
    postal_prefix="H2Y",
    channels=[
      # 1143: Top candidate #4001 is pure black -> must fail over to #4002!
      LineupChannel(
        chno=1143,
        name="Canal Famille",
        group="French — Kids & Family",
        language="fr",
        aliases=["Canal Famille"],
      ),
      # 1140: Top candidate #5001 has English audio (Yes TV!) -> must reject #5001 for 1140 and pick #5002 (fra)!
      LineupChannel(
        chno=1140,
        name="Télétoon",
        group="French — Kids & Family",
        language="fr",
        aliases=["Teletoon", "Telemagino"],
      ),
      # 1651: English channel sharing #5001 -> must ACCEPT #5001 (en == en) from cache!
      LineupChannel(
        chno=1651,
        name="Yes TV",
        group="Broadcast & Networks",
        language="en",
        aliases=["Yes TV", "Telemagino"],
      ),
      # 1120: Untagged audio ("und") on #6001 -> treated as neutral and accepted!
      LineupChannel(
        chno=1120,
        name="Addik",
        group="French — Entertainment & Lifestyle",
        language="fr",
        aliases=["Addik"],
      ),
    ],
  )

  catalog = XtreamCatalog(
    account=XtreamAccountInfo(username="u", status="Active", max_connections=1),
    categories={"1": "CA| FR", "2": "CA| EN"},
    streams=[
      XtreamStream(
        stream_id=4001, name="CA FR: CANAL FAMILLE FHD", epg_channel_id="CF.ca"
      ),
      XtreamStream(
        stream_id=4002, name="CA FR: CANAL FAMILLE HD", epg_channel_id=""
      ),
      XtreamStream(
        stream_id=5001,
        name="CA FR: TELEMAGINO FHD",
        epg_channel_id="Telemagino.ca",
      ),
      XtreamStream(
        stream_id=5002, name="CA FR: TELETOON HD", epg_channel_id=""
      ),
      XtreamStream(
        stream_id=6001, name="CA FR: ADDIK FHD", epg_channel_id="Addik.ca"
      ),
    ],
  )

  cfg = AppConfig(
    project_root=tmp_path,
    xtream=XtreamConfig(
      server_url="http://panel.example:8080",
      username="u",
      password="secret",
      stream_format="ts",
    ),
    lineup=LineupConfig(lineup_id="TVG-9100000607"),
    state=StateConfig(
      state_dir=Path("state"),
      mapping_path=Path("state/channel_mapping.json"),
    ),
    validation=ValidationConfig(
      concurrency=1,
      inter_probe_delay_sec=0.0,
      min_bytes_to_read=2048,
      use_ffprobe=True,
      detect_black_screen=True,
      black_sample_sec=1.5,
      check_audio_language=True,
    ),
  )

  # Phase (3) initial rough mapping: #4001 and #5001 win initially by FHD + EPG score
  doc1 = reconcile_rough_mapping(None, lineup, catalog, cfg)
  assert doc1.slots[1143].preferred_stream_id == 4001
  assert doc1.slots[1140].preferred_stream_id == 5001
  assert doc1.slots[1651].preferred_stream_id == 5001

  mapping_path = tmp_path / "state" / "channel_mapping.json"

  async with MockHttpServer(stream_handler) as mock:
    validator = StreamValidator(
      cfg, catalog.account, session_factory=mock.session_factory
    )
    validator.ffprobe_path = ffprobe_bin
    validator.ffmpeg_path = ffmpeg_bin

    await validate_mapping_candidates(
      doc1,
      lineup,
      cfg,
      catalog.account,
      validator=validator,
      checkpoint_path=mapping_path,
    )

  # 1. Slot 1143 rejected pure-black #4001 and promoted #4002!
  slot_1143 = doc1.slots[1143]
  assert slot_1143.preferred_stream_id == 4002
  cand_4001 = next(c for c in slot_1143.candidates if c.stream_id == 4001)
  assert cand_4001.validation_status == "invalid"
  assert cand_4001.probe is not None
  assert cand_4001.probe.black_screen is True
  assert cand_4001.probe.ok is False
  assert "Black screen" in (cand_4001.probe.error or "")

  # 2. French Slot 1140 rejected English-audio #5001 and promoted French #5002!
  slot_1140 = doc1.slots[1140]
  assert slot_1140.preferred_stream_id == 5002
  cand_5001_fr = next(c for c in slot_1140.candidates if c.stream_id == 5001)
  assert cand_5001_fr.validation_status == "invalid"
  assert cand_5001_fr.probe is not None
  assert "Audio track language mismatch" in (cand_5001_fr.probe.error or "")

  # 3. English Slot 1651 ACCEPTED the exact same stream #5001 (en == en)!
  slot_1651 = doc1.slots[1651]
  assert slot_1651.preferred_stream_id == 5001
  cand_5001_en = next(c for c in slot_1651.candidates if c.stream_id == 5001)
  assert cand_5001_en.validation_status == "valid"
  assert cand_5001_en.probe is not None
  assert cand_5001_en.probe.ok is True
  assert cand_5001_en.probe.audio_languages == ["en"]

  # 4. Slot 1120 accepted untagged ("und") #6001 as neutral
  assert doc1.slots[1120].preferred_stream_id == 6001
  assert doc1.slots[1120].candidates[0].probe is not None
  assert doc1.slots[1120].candidates[0].probe.audio_languages == []

  # 5. Reload `channel_mapping.json` and run Phase (3) `reconcile_rough_mapping` again:
  #    - #4001 stays globally invalid (`black_screen=True`)
  #    - #5001 stays invalid on French 1140 AND valid (+6.0 verified bonus) on English 1651!
  #    - #5002 gets `+6.0` `audio_lang_verified:fr` bonus on French 1140!
  save_mapping_document(doc1, mapping_path)
  reloaded: MappingDocument = load_mapping_document(
    mapping_path, lineup_id=lineup.lineup_id
  )
  doc2 = reconcile_rough_mapping(reloaded, lineup, catalog, cfg)

  assert doc2.slots[1143].preferred_stream_id == 4002
  assert doc2.slots[1140].preferred_stream_id == 5002
  assert doc2.slots[1651].preferred_stream_id == 5001

  c5002_day2 = next(
    c for c in doc2.slots[1140].candidates if c.stream_id == 5002
  )
  assert "audio_lang_verified:fr" in c5002_day2.reasons

  c5001_en_day2 = next(
    c for c in doc2.slots[1651].candidates if c.stream_id == 5001
  )
  assert c5001_en_day2.validation_status == "valid"
  assert "audio_lang_verified:en" in c5001_en_day2.reasons

  c5001_fr_day2 = next(
    c for c in doc2.slots[1140].candidates if c.stream_id == 5001
  )
  assert c5001_fr_day2.validation_status == "invalid"
  assert any(
    r.startswith("audio_lang_mismatch:") for r in c5001_fr_day2.reasons
  )
