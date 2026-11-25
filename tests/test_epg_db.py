"""Focused unit and failure-mode tests for `EpgDatabase` (`src/bellhop/epg/db.py`)."""

from __future__ import annotations

import contextlib
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

import pytest

from bellhop.epg.db import (
  EPG_DB_SCHEMA_VERSION,
  SD_PROGRAM_RETENTION_SEC,
  EpgDatabase,
  SdLineupRecord,
  SdStationRow,
)
from bellhop.epg.models import ProgramItem


@dataclass(frozen=True, slots=True)
class _StubChannel:
  chno: int
  name: str
  callsign: str | None
  epg_channel_id: str
  sd_chno: int | None = None
  logo: str = ""
  language: str = "fr"
  epg_aliases: tuple[str, ...] = ()
  gracenote_station_id: str = ""


@dataclass(frozen=True, slots=True)
class _StubSnapshot:
  channels: tuple[_StubChannel, ...]
  lineup_id: str = "CAN-BELLFIBE-MTL-X"
  provider: str = "Bell Fibe TV"
  market: str = "Montréal, QC"
  postal_prefix: str = "H2Y"
  xmltv_url: str = ""

  def build_epg_channel_lookup(self) -> dict[str, str]:
    return {
      ch.epg_channel_id.lower(): ch.epg_channel_id
      for ch in self.channels
      if ch.epg_channel_id
    }

  def build_epg_candidate_chains(self) -> dict[str, tuple[str, ...]]:
    return {
      ch.epg_channel_id: (ch.epg_channel_id, *ch.epg_aliases)
      for ch in self.channels
      if ch.epg_channel_id
    }


def test_missing_database_queries_return_empty_without_creating_file(
  tmp_path: Path,
) -> None:
  db_path = tmp_path / "cache" / "missing.sqlite3"
  db = EpgDatabase(db_path)

  assert db.get_meta() == {}
  assert db.has_sd_cache() is False
  assert db.coverage_stats(now_ts=1_700_000_000).total_programs == 0
  assert db.cached_coverage_stats(now_ts=1_700_000_000).active_programs == 0
  assert db.prune_stale_airings(stale_cutoff_ts=1_700_000_000) == (0, 0)
  assert db.query_channel_schedule(1101, "RDS.ca") == []
  assert db.query_lineup_window(min_stop_ts=0, max_start_ts=100) == ({}, {})
  assert db.load_sd_lineups() == {}
  assert db.get_sd_schedule_md5s(["10001"], ["2026-11-25"]) == {}
  assert db.load_sd_airings(["10001"], ["2026-11-25"]) == {}
  assert db.get_sd_program_md5s(["EP1"]) == {}
  assert db.load_sd_programs(["EP1"]) == {}
  assert db.touch_and_prune_sd_programs(["EP1"], now_ts=1_700_000_000.0) == 0
  assert not db_path.exists()


@pytest.mark.parametrize(
  "corrupt_kind",
  ["zero_byte", "non_sqlite_header", "corrupted_btree_pages"],
)
def test_corrupt_or_empty_database_recovers_cleanly(
  tmp_path: Path, corrupt_kind: str
) -> None:
  db_path = tmp_path / "cache" / "epg.sqlite3"
  db_path.parent.mkdir(parents=True, exist_ok=True)

  if corrupt_kind == "zero_byte":
    db_path.write_bytes(b"")
  elif corrupt_kind == "non_sqlite_header":
    db_path.write_bytes(b"NOT_A_SQLITE_FILE_HEADER_1234567890")
  else:
    # Valid 16-byte SQLite header followed by garbage page bytes triggers DatabaseError
    db_path.write_bytes(b"SQLite format 3\x00" + b"\xff" * 8192)

  db = EpgDatabase(db_path)
  db.ensure_initialized()

  assert db_path.is_file()
  assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
  with contextlib.closing(sqlite3.connect(db_path)) as conn:
    user_ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
    auto_vac = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
    journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
  assert user_ver == EPG_DB_SCHEMA_VERSION
  assert auto_vac == 2
  assert journal == "wal"


def test_schema_version_mismatch_drops_dynamic_tables_and_recreates(
  tmp_path: Path,
) -> None:
  db_path = tmp_path / "cache" / "epg.sqlite3"
  db_path.parent.mkdir(parents=True, exist_ok=True)

  with contextlib.closing(sqlite3.connect(db_path)) as conn:
    conn.execute("CREATE TABLE legacy_orphan_table (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO legacy_orphan_table (id) VALUES (42)")
    conn.execute("PRAGMA user_version = 999")
    conn.commit()

  db = EpgDatabase(db_path)
  db.ensure_initialized()

  with contextlib.closing(sqlite3.connect(db_path)) as conn:
    tables = {
      str(r[0])
      for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
      ).fetchall()
    }
    user_ver = int(conn.execute("PRAGMA user_version").fetchone()[0])

  assert "legacy_orphan_table" not in tables
  assert "guide_airings" in tables
  assert "sd_programs" in tables
  assert user_ver == EPG_DB_SCHEMA_VERSION


def test_synthetic_chno_ids_and_orphans_never_leak_into_chunked_xmltv(
  tmp_path: Path,
) -> None:
  db = EpgDatabase(tmp_path / "cache" / "epg.sqlite3")
  snap = _StubSnapshot(
    channels=(
      _StubChannel(
        chno=1101,
        name="ICI RDI",
        callsign="RDI",
        epg_channel_id="ICIRDI.ca",
        logo="https://logos.example/rdi.png",
      ),
      # Duplicate SD/HD channel sharing `ICIRDI.ca` must not double-emit programmes
      _StubChannel(
        chno=101,
        name="ICI RDI SD",
        callsign="RDI",
        epg_channel_id="ICIRDI.ca",
      ),
      # Channel with empty `epg_channel_id` uses internal `ch.1200` for `/api/epg/1200`
      _StubChannel(
        chno=1200,
        name="Local Community Access",
        callsign="LOCAL",
        epg_channel_id="",
      ),
    )
  )

  now_ts = 1_763_510_400
  rdi_items = [
    ProgramItem(
      id=f"rdi-{idx}",
      channel_id="ICIRDI.ca",
      title=f"Bulletin {idx} & Direct",
      sub_title=f"Édition {idx}",
      description="Nouvelles en continu.",
      category="News",
      start_timestamp=now_ts + idx * 1800,
      stop_timestamp=now_ts + (idx + 1) * 1800,
    )
    for idx in range(5)
  ]
  local_items = [
    ProgramItem(
      id="loc-1",
      channel_id="",
      title="City Council Live",
      start_timestamp=now_ts,
      stop_timestamp=now_ts + 3600,
    )
  ]
  orphan_items = [
    ProgramItem(
      id="orph-1",
      channel_id="ch.9999",
      title="Unmapped Synthetic Leak",
      start_timestamp=now_ts,
      stop_timestamp=now_ts + 3600,
    ),
    ProgramItem(
      id="orph-2",
      channel_id="UndeclaredChannel.tv",
      title="Undeclared Channel Leak",
      start_timestamp=now_ts,
      stop_timestamp=now_ts + 3600,
    ),
  ]

  inserted = db.replace_guide_schedules(
    snap=snap,
    programs_by_id={
      "ICIRDI.ca": rdi_items,
      "ch.9999": orphan_items[:1],
      "UndeclaredChannel.tv": orphan_items[1:],
    },
    programs_by_chno={1200: local_items},
    fetched_at=float(now_ts),
    guide_fingerprint="fp-test-1",
    xmltv_url_redacted="http://panel.example/xmltv.php?username=alice&password=***",
  )
  assert inserted > 5
  assert db.get_meta()["guide_fingerprint"] == "fp-test-1"

  # Web Guide `/api/epg/1200` still serves the `epg_channel_id=""` channel by `chno`
  ch1200_sched = db.query_channel_schedule(1200, "")
  assert len(ch1200_sched) == 1
  assert ch1200_sched[0].title == "City Council Live"

  # Stream XMLTV with tiny 64-byte chunk flushes and 2-row keyset pages
  chunks = list(db.iter_xmltv_chunks(snap, chunk_target_bytes=64, page_rows=2))
  assert len(chunks) >= 6
  xml_text = b"".join(chunks).decode("utf-8")

  assert "ch.1200" not in xml_text
  assert "ch.9999" not in xml_text
  assert "UndeclaredChannel.tv" not in xml_text
  assert "City Council Live" not in xml_text

  root = ElementTree.fromstring(xml_text)
  declared_channels = {
    el.attrib["id"] for el in root.findall("channel") if "id" in el.attrib
  }
  assert declared_channels == {"ICIRDI.ca"}
  programmes = root.findall("programme")
  # 5 programmes for primary chno 1101 only (duplicate chno 101 suppressed)
  assert len(programmes) == 5
  assert {el.attrib["channel"] for el in programmes} <= declared_channels


def test_sd_program_retention_pruning_and_stale_airings_eviction(
  tmp_path: Path,
) -> None:
  db = EpgDatabase(tmp_path / "cache" / "epg.sqlite3")
  now_ts = 1_763_510_400.0

  rec = SdLineupRecord(
    lineup_id="CAN-0004980-X",
    name="Bell Fibe Montreal",
    location="Montreal",
    modified="2026-11-01T00:00:00Z",
    fetched_at=now_ts - 3600.0,
    stations={
      "10001": SdStationRow(
        station_id="10001", name="RDS", callsign="RDS", language="fr"
      )
    },
    chno_to_station={1108: "10001"},
  )
  db.save_sd_lineup(rec)
  db.touch_sd_lineup_fetched_at("CAN-0004980-X", now_ts)
  loaded_lu = db.load_sd_lineups()
  assert loaded_lu["CAN-0004980-X"].fetched_at == now_ts
  assert loaded_lu["CAN-0004980-X"].chno_to_station == {1108: "10001"}

  old_ts = now_ts - SD_PROGRAM_RETENTION_SEC - 7200.0
  db.upsert_sd_programs(
    {
      "EP_ACTIVE": {"md5": "m1", "title": "Active Show", "category": "Sports"},
      "EP_STALE": {"md5": "m2", "title": "Expired 15d Ago", "category": "News"},
    },
    updated_at=old_ts,
  )
  assert db.has_sd_cache() is True
  pruned = db.touch_and_prune_sd_programs(["EP_ACTIVE"], now_ts=now_ts)
  assert pruned == 1
  assert set(db.load_sd_programs(["EP_ACTIVE", "EP_STALE"]).keys()) == {
    "EP_ACTIVE"
  }

  db.upsert_sd_schedule_days(
    [
      ("10001", "2026-11-20", "md5-old", [{"programID": "EP_STALE"}]),
      ("10001", "2026-11-25", "md5-cur", [{"programID": "EP_ACTIVE"}]),
    ],
    min_keep_date="2026-11-24",
  )
  assert db.get_sd_schedule_md5s(["10001"], ["2026-11-20", "2026-11-25"]) == {
    ("10001", "2026-11-25"): "md5-cur"
  }

  # Expiring all `guide_airings` clears `fetched_at` and resets cached active count
  snap = _StubSnapshot(
    channels=(
      _StubChannel(
        chno=1108, name="RDS", callsign="RDS", epg_channel_id="RDS.ca"
      ),
    )
  )
  db.replace_guide_schedules(
    snap=snap,
    programs_by_id={
      "RDS.ca": [
        ProgramItem(
          id="p1",
          channel_id="RDS.ca",
          title="Short Show",
          start_timestamp=int(now_ts),
          stop_timestamp=int(now_ts) + 1800,
        )
      ]
    },
    fetched_at=now_ts,
  )
  assert "fetched_at" in db.get_meta()
  assert (
    db.cached_coverage_stats(now_ts=int(now_ts) + 7200).active_programs == 0
  )
  evicted, remaining = db.prune_stale_airings(
    stale_cutoff_ts=int(now_ts) + 3600
  )
  assert (evicted, remaining) == (1, 0)
  assert "fetched_at" not in db.get_meta()
