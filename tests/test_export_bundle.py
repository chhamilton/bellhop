"""Focused unit tests for `ExportBundleReader` and `bellhop_web` boundary."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from bellhop.xtream_server import XtreamExportStore
from bellhop_web.export_bundle import ExportBundleReader
from bellhop_web.store import LineupGuideStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_export_bundle_reader_empty_volume_and_store_parity(
  tmp_path: Path,
) -> None:
  """Both stores share identical empty-volume initialization via ExportBundleReader."""
  xtream_dir = tmp_path / "xtream"
  xtream_dir.mkdir()

  reader = ExportBundleReader(xtream_dir)
  empty = reader.read(fallback_xmltv_url="http://panel.example/xmltv.php")
  assert empty.initialized is False
  assert empty.lineup_id == "UNINITIALIZED"
  assert empty.provider == "Bellhop"
  assert empty.xmltv_url == "http://panel.example/xmltv.php"
  assert empty.categories == []
  assert empty.streams == []

  guide_snap = LineupGuideStore(xtream_dir).get_snapshot()
  xt_snap = XtreamExportStore(
    xtream_dir, fallback_xmltv_url="http://panel.example/xmltv.php"
  ).get_snapshot()
  assert guide_snap.lineup_id == xt_snap.lineup_id == "UNINITIALIZED"
  assert guide_snap.provider == xt_snap.provider == "Bellhop"
  assert guide_snap.channels == []
  assert xt_snap.streams == []
  assert xt_snap.xmltv_url == "http://panel.example/xmltv.php"


def test_export_bundle_reader_fallbacks_and_category_synthesis(
  tmp_path: Path,
) -> None:
  """Verify M3U8 -> split JSON -> legacy bundle -> primary bundle precedence."""
  xtream_dir = tmp_path / "xtream"
  xtream_dir.mkdir()
  m3u_path = tmp_path / "bellhop.m3u8"
  m3u_path.write_text(
    '#EXTM3U url-tvg="http://m3u.example/epg.xml"\n'
    '#EXTINF:-1 tvg-chno="1108" tvg-id="RDS.ca" tvg-logo="https://img/rds.png"'
    ' group-title="Sports",RDS\n'
    "http://up.example/live/u/p/108.ts\n",
    encoding="utf-8",
  )

  reader = ExportBundleReader(xtream_dir, m3u8_path=m3u_path)
  sig_m3u = reader.compute_signature()
  from_m3u = reader.read()
  assert from_m3u.initialized is True
  assert from_m3u.xmltv_url == "http://m3u.example/epg.xml"
  assert len(from_m3u.categories) == 1
  assert from_m3u.categories[0].category_name == "Sports"
  assert from_m3u.categories[0].channel_count == 1
  assert from_m3u.streams[0].num == 1108
  assert (
    from_m3u.streams[0].direct_source == "http://up.example/live/u/p/108.ts"
  )

  # Split JSON overrides M3U8 fallback and synthesizes missing category_id "9"
  (xtream_dir / "get_live_streams.json").write_text(
    json.dumps(
      [
        {
          "num": 1201,
          "name": "CTV Montreal",
          "stream_id": 403,
          "category_id": "9",
          "direct_source": "http://up.example/live/u/p/403.ts",
          "guide": {
            "group_title": "English Networks",
            "official_logo": "https://img/ctv.png",
            "epg_channel_id": "CFCF.ca",
            "official_tvg_id": "CTVMtl.ca",
            "upstream_epg_channel_id": "cfcf.up",
            "has_native_epg": True,
            "fallback_sources": [
              "http://up.example/live/u/***/404.ts",
              "http://up.example/live/u/p/405.ts",
            ],
          },
        }
      ]
    ),
    encoding="utf-8",
  )
  assert reader.compute_signature() != sig_m3u
  from_split = reader.read()
  assert from_split.streams[0].num == 1201
  assert from_split.streams[0].stream_icon == "https://img/ctv.png"
  assert from_split.streams[0].fallback_sources == (
    "http://up.example/live/u/p/405.ts",
  )
  assert from_split.streams[0].epg_aliases == (
    "CFCF.ca",
    "CTVMtl.ca",
    "cfcf.up",
  )
  assert from_split.categories[0].category_id == "9"
  assert from_split.categories[0].category_name == "English Networks"
  assert from_split.categories[0].channel_count == 1

  # Legacy bundle takes precedence over split JSON, and primary over legacy
  legacy_payload = {
    "lineup_info": {"lineup_id": "LEGACY-ID", "provider": "Legacy"},
    "server_info": {"timezone": "America/Toronto"},
    "user_info": {"max_connections": "2", "allowed_output_formats": ["ts"]},
    "epg_guide": {"xmltv_url": "http://legacy.example/xmltv.php"},
    "live_categories": [{"category_id": "1", "category_name": "News"}],
    "live_streams": [
      {
        "num": 1001,
        "name": "CBC News",
        "stream_id": 901,
        "category_id": "1",
        "direct_source": "http://up.example/live/u/p/901.ts",
      }
    ],
  }
  (xtream_dir / "bell_fibe_xtream_playlist.json").write_text(
    json.dumps(legacy_payload), encoding="utf-8"
  )
  from_legacy = reader.read()
  assert from_legacy.lineup_id == "LEGACY-ID"
  assert from_legacy.allowed_output_formats == ("ts",)
  assert from_legacy.xmltv_url == "http://legacy.example/xmltv.php"

  primary_payload = dict(
    legacy_payload,
    lineup_info={"lineup_id": "PRIMARY-ID", "provider": "Bellhop Primary"},
  )
  (xtream_dir / "bellhop_xtream_playlist.json").write_text(
    json.dumps(primary_payload), encoding="utf-8"
  )
  from_primary = reader.read()
  assert from_primary.lineup_id == "PRIMARY-ID"
  assert from_primary.provider == "Bellhop Primary"


def test_bellhop_web_package_never_imports_bellhop() -> None:
  """Enforce architectural boundary: `src/bellhop_web` never imports `bellhop.*`."""
  web_pkg = REPO_ROOT / "src" / "bellhop_web"
  violations: list[str] = []
  for py_file in sorted(web_pkg.rglob("*.py")):
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    for node in ast.walk(tree):
      if isinstance(node, ast.Import):
        for alias in node.names:
          if alias.name == "bellhop" or alias.name.startswith("bellhop."):
            violations.append(f"{py_file.name}:{node.lineno} -> {alias.name}")
      elif isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        if mod == "bellhop" or mod.startswith("bellhop."):
          violations.append(f"{py_file.name}:{node.lineno} -> {mod}")
  assert violations == [], (
    f"Package boundary violated in bellhop_web: {violations}"
  )
