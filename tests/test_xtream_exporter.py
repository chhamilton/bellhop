"""Tests for Phase (6): Official Xtream Codes API JSON playlist & passthrough EPG verification."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from aiohttp import web

from bellhop.cli import main as cli_main
from bellhop.config import (
  AppConfig,
  EpgConfig,
  LineupConfig,
  OutputConfig,
  XtreamConfig,
)
from bellhop.lineup_loader import CableLineup
from bellhop.mapping_state import (
  mapping_document_to_mapped_channels,
  reconcile_rough_mapping,
)
from bellhop.models import LineupChannel, XtreamAccountInfo, XtreamStream
from bellhop.xtream import XtreamCatalog, load_catalog_from_json
from bellhop.xtream_exporter import export_xtream_playlist_and_guide
from tests.http_mock import MockHttpServer

SAMPLE_XMLTV = """\
<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="MockProviderEPG">
  <channel id="ICITeleMontreal.ca">
    <display-name>CA FR: ICI MONTREAL RAW</display-name>
  </channel>
  <programme start="20261116180000 -0500" stop="20261116190000 -0500" channel="ICITeleMontreal.ca">
    <title lang="fr">Le Téléjournal 18h</title>
  </programme>
</tv>
"""


def _sample_lineup_and_catalog() -> tuple[CableLineup, XtreamCatalog]:
  lineup = CableLineup(
    lineup_id="CAN-BELLFIBE-MTL-X",
    provider="Bell Fibe TV",
    market="Montréal, QC",
    postal_prefix="H2Y",
    channels=[
      LineupChannel(
        chno=1101,
        sd_chno=101,
        name="ICI Radio-Canada Télé (Montréal)",
        callsign="CBFT-DT",
        tvg_id="CBFT.ca",
        logo="https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/canada/ici-tele-ca.png",
        group="Francophone — Généraliste (Montréal)",
        language="fr",
        regions=["montreal", "qc", "ca"],
        aliases=["ICI Montreal", "Radio-Canada"],
      ),
      LineupChannel(
        chno=1108,
        sd_chno=108,
        name="RDS",
        callsign="RDS",
        tvg_id="RDS.ca",
        logo="https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/canada/rds-ca.png",
        group="Sports",
        language="fr",
        regions=["qc", "ca"],
        aliases=["RDS", "Reseau des sports"],
      ),
    ],
  )
  catalog = XtreamCatalog(
    account=XtreamAccountInfo(
      username="sub_user",
      status="Active",
      max_connections=1,
      server_protocol="http",
      server_port="80",
    ),
    categories={"99": "CA| FR RAW"},
    streams=[
      XtreamStream(
        stream_id=410101,
        name="CA FR: ICI MONTREAL ᶠᴴᴰ",
        category_id="99",
        category_name="CA| FR RAW",
        stream_icon="http://raw-provider.example/ugly_icon_1.png",
        epg_channel_id="ICITeleMontreal.ca",
        num=8812,
      ),
      XtreamStream(
        stream_id=410108,
        name="CA FR: RDS ᴿᴬᵂ ⁶⁰ᶠᵖˢ",
        category_id="99",
        category_name="CA| FR RAW",
        stream_icon="http://raw-provider.example/ugly_icon_2.png",
        epg_channel_id="RDS.ca",
        num=8849,
      ),
    ],
  )
  return lineup, catalog


@pytest.mark.asyncio
async def test_phase6_xtream_export_collapsed_hd_and_passthrough_epg(
  tmp_path: Path,
) -> None:
  lineup, catalog = _sample_lineup_and_catalog()

  gzipped_body = gzip.compress(SAMPLE_XMLTV.encode("utf-8"))
  probed_urls: list[str] = []

  def mock_epg(req: web.Request) -> web.Response:
    probed_urls.append(str(req.url))
    assert req.path == "/xmltv.php"
    assert req.query.get("username") == "sub_user"
    assert req.query.get("password") == "secret_pw"
    return web.Response(
      status=200,
      headers={"Content-Type": "application/gzip"},
      body=gzipped_body,
    )

  cfg = AppConfig(
    project_root=tmp_path,
    xtream=XtreamConfig(
      server_url="http://provider.example",
      username="sub_user",
      password="secret_pw",
      stream_format="ts",
    ),
    lineup=LineupConfig(
      lineup_id="CAN-BELLFIBE-MTL-X",
      numbering_mode="hd",
      collapse_sd_dupes=True,
    ),
    epg=EpgConfig(verify=True),
    output=OutputConfig(xtream_dir=Path("output/xtream")),
  )

  doc = reconcile_rough_mapping(None, lineup, catalog, cfg)
  mappings = mapping_document_to_mapped_channels(doc, lineup)

  async with MockHttpServer(mock_epg) as mock, mock.new_session() as client:
    res = await export_xtream_playlist_and_guide(
      lineup,
      mappings,
      cfg,
      account=catalog.account,
      client=client,
    )

  assert len(probed_urls) == 1
  assert res.emitted_streams == 2
  assert res.emitted_categories == 2
  assert res.channels_with_official_logo == 2
  assert res.channels_with_guide_id == 2
  assert res.epg_verified is True
  assert res.epg_verification is not None
  assert res.epg_verification.format_detected == "xmltv_gzip"
  assert (
    res.xmltv_url
    == "http://provider.example/xmltv.php?username=sub_user&password=secret_pw"
  )
  assert "secret_pw" not in res.xmltv_url_redacted

  # Passthrough mode must NOT emit static get_epg.json or xmltv.xml files
  xt_dir = tmp_path / "output" / "xtream"
  assert not (xt_dir / "get_epg.json").exists()
  assert not (xt_dir / "xmltv.xml").exists()

  # 1. Inspect get_live_categories.json (Xtream player_api format)
  cats = json.loads(res.live_categories_path.read_text(encoding="utf-8"))
  assert cats == [
    {
      "category_id": "1",
      "category_name": "Francophone — Généraliste (Montréal) (1101)",
      "parent_id": 0,
    },
    {
      "category_id": "2",
      "category_name": "Sports (1108)",
      "parent_id": 0,
    },
  ]

  # 2. Inspect get_live_streams.json (Xtream player_api format with official Bell Fibe metadata)
  streams = json.loads(res.live_streams_path.read_text(encoding="utf-8"))
  assert len(streams) == 2

  s_ici = streams[0]
  assert s_ici["num"] == 1101
  assert s_ici["name"] == "ICI Radio-Canada Télé (Montréal)"
  assert s_ici["stream_type"] == "live"
  assert s_ici["stream_id"] == 410101
  assert s_ici["stream_icon"] == (
    "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/canada/ici-tele-ca.png"
  )
  assert s_ici["epg_channel_id"] == "ICITeleMontreal.ca"
  assert s_ici["category_id"] == "1"
  assert s_ici["category_ids"] == [1]
  assert s_ici["custom_sid"] == "1101"
  assert (
    s_ici["direct_source"]
    == "http://provider.example/live/sub_user/secret_pw/410101.ts"
  )
  assert "epg_listings" not in s_ici["guide"]

  # 3. Inspect unified bundle passthrough EPG metadata & round-trip via load_catalog_from_json()
  bundle = json.loads(res.bundle_path.read_text(encoding="utf-8"))
  assert bundle["server_info"]["xmltv_url"] == res.xmltv_url
  assert bundle["epg_guide"]["mode"] == "passthrough"
  assert bundle["epg_guide"]["xmltv_url"] == res.xmltv_url
  assert bundle["epg_guide"]["verified"] is True
  assert "secret_pw" not in bundle["epg_guide"]["verification"]["url"]

  roundtrip = load_catalog_from_json(res.bundle_path)
  assert roundtrip.account.username == "sub_user"
  assert roundtrip.categories == {
    "1": "Francophone — Généraliste (Montréal) (1101)",
    "2": "Sports (1108)",
  }
  assert [(s.num, s.name, s.stream_id) for s in roundtrip.streams] == [
    (1101, "ICI Radio-Canada Télé (Montréal)", 410101),
    (1108, "RDS", 410108),
  ]


def test_phase6_both_sd_and_hd_dial_positions_and_cli_commands(
  tmp_path: Path,
) -> None:
  _, catalog = _sample_lineup_and_catalog()

  input_catalog_path = tmp_path / "raw_catalog.json"
  input_catalog_path.write_text(
    json.dumps(
      [
        {
          "stream_id": s.stream_id,
          "name": s.name,
          "category_id": s.category_id,
          "stream_icon": s.stream_icon,
          "epg_channel_id": s.epg_channel_id,
          "num": s.num,
        }
        for s in catalog.streams
      ]
    ),
    encoding="utf-8",
  )
  epg_xml_path = tmp_path / "guide.xml"
  epg_xml_path.write_text(SAMPLE_XMLTV, encoding="utf-8")

  lineup_yaml_path = tmp_path / "mini_lineup.yaml"
  lineup_yaml_path.write_text(
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

  state_dir = tmp_path / "state"
  out_m3u8 = tmp_path / "output" / "test.m3u8"
  out_report = tmp_path / "output" / "report.json"
  xtream_dir = tmp_path / "output" / "xtream_both"

  cfg_both_path = tmp_path / "config_both.yaml"
  cfg_both_path.write_text(
    f"""\
paths:
  state_dir: "{state_dir}"
  lineup_file: "{lineup_yaml_path}"
  seed_json: "{input_catalog_path}"
  xmltv_file: "{epg_xml_path}"
  m3u8_playlist_path: "{out_m3u8}"
  m3u8_report_path: "{out_report}"
  xtream_output_dir: "{xtream_dir}"
upstream_xtream:
  server_url: "http://provider.example"
  username: "u"
  password: "p"
pipeline:
  lineup:
    lineup_id: "CAN-BELLFIBE-MTL-X"
    numbering_mode: "both"
    collapse_sd_dupes: false
  epg:
    verify: true
""",
    encoding="utf-8",
  )

  rc = cli_main(
    ["run", "--config", str(cfg_both_path), "--skip", "validate", "--quiet"]
  )
  assert rc == 0

  # Both M3U8 and Xtream JSON embed the passthrough upstream xmltv.php URL
  m3u8_text = out_m3u8.read_text(encoding="utf-8")
  assert (
    'url-tvg="http://provider.example/xmltv.php?username=u&password=p"'
    in m3u8_text
  )

  both_streams = json.loads(
    (xtream_dir / "get_live_streams.json").read_text(encoding="utf-8")
  )
  assert [s["num"] for s in both_streams] == [101, 108, 1101, 1108]

  bundle = json.loads(
    (xtream_dir / "bellhop_xtream_playlist.json").read_text(encoding="utf-8")
  )
  assert bundle["epg_guide"]["mode"] == "passthrough"
  assert bundle["epg_guide"]["verified"] is True
  assert not (xtream_dir / "xmltv.xml").exists()
  assert not (xtream_dir / "get_epg.json").exists()

  # Now run single-phase `bellhop run --phase xtream` with collapse_sd_dupes: true in config
  xtream_hd_dir = tmp_path / "output" / "xtream_hd"
  cfg_hd_path = tmp_path / "config_hd.yaml"
  cfg_hd_path.write_text(
    f"""\
paths:
  state_dir: "{state_dir}"
  lineup_file: "{lineup_yaml_path}"
  xtream_output_dir: "{xtream_hd_dir}"
upstream_xtream:
  server_url: "http://provider.example"
  username: "u"
  password: "p"
pipeline:
  lineup:
    lineup_id: "CAN-BELLFIBE-MTL-X"
    collapse_sd_dupes: true
  epg:
    verify: false
""",
    encoding="utf-8",
  )
  rc2 = cli_main(
    ["run", "--config", str(cfg_hd_path), "--phase", "xtream", "--quiet"]
  )
  assert rc2 == 0
  hd_streams = json.loads(
    (xtream_hd_dir / "get_live_streams.json").read_text(encoding="utf-8")
  )
  assert [s["num"] for s in hd_streams] == [1101, 1108]
