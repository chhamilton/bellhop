"""Relational SQLite 3 (WAL mode) backing store for EPG schedules and SD-JSON
MD5 caches.

Manages the embedded SQLite database (`cache/epg.sqlite3`):
- Safe concurrent access (`PRAGMA journal_mode=WAL`, `busy_timeout=5000`) across
  `bellhop serve` HTTP readers and `bellhop run --phase epg` batch writers.
- Sub-millisecond indexed range queries for `/api/lineup`, `/api/epg/<chno>`,
  and Xtream `get_short_epg` / `get_simple_data_table`.
- Constant-memory (`~64 KB` chunked) cursor streaming for `/xmltv.php`.
- Persistent MD5 differential tables (`sd_schedule_days`, `sd_programs`)
  for Schedules Direct.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from bellhop.common.file_utils import (
  PRIVATE_FILE_MODE,
  chmod_private_file,
  ensure_private_dir,
)
from bellhop.epg.constants import EPG_RECENT_FLOOR_SEC
from bellhop.epg.constants import (
  SD_PROGRAM_RETENTION_DAYS as SD_PROGRAM_RETENTION_DAYS,
)
from bellhop.epg.constants import (
  SD_PROGRAM_RETENTION_SEC as SD_PROGRAM_RETENTION_SEC,
)
from bellhop.epg.models import (
  GuideSnapshotProto,
  ProgramItem,
  reconcile_non_overlapping_schedule,
)

logger = logging.getLogger("bellhop.epg.db")

EPG_DB_SCHEMA_VERSION = 1
DEFAULT_EPG_DB_FILE = Path("cache/epg.sqlite3")
_SQL_CHUNK = 450
_XMLTV_PAGE_ROWS = 2000
_INCREMENTAL_VACUUM_PAGES = 256
_MAX_POOL_SIZE = 8

_DDL_STATEMENTS: tuple[str, ...] = (
  """
  CREATE TABLE IF NOT EXISTS epg_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
  )
  """,
  """
  CREATE TABLE IF NOT EXISTS sd_lineups (
    lineup_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    location TEXT NOT NULL,
    modified TEXT NOT NULL,
    fetched_at REAL NOT NULL
  )
  """,
  """
  CREATE TABLE IF NOT EXISTS sd_stations (
    lineup_id TEXT NOT NULL,
    station_id TEXT NOT NULL,
    name TEXT NOT NULL,
    callsign TEXT NOT NULL,
    language TEXT NOT NULL,
    PRIMARY KEY (lineup_id, station_id)
  )
  """,
  """
  CREATE TABLE IF NOT EXISTS sd_channel_map (
    lineup_id TEXT NOT NULL,
    chno INTEGER NOT NULL,
    station_id TEXT NOT NULL,
    PRIMARY KEY (lineup_id, chno)
  )
  """,
  """
  CREATE TABLE IF NOT EXISTS sd_schedule_days (
    station_id TEXT NOT NULL,
    schedule_date TEXT NOT NULL,
    md5 TEXT NOT NULL,
    airings_json TEXT NOT NULL,
    PRIMARY KEY (station_id, schedule_date)
  )
  """,
  """
  CREATE TABLE IF NOT EXISTS sd_programs (
    program_id TEXT PRIMARY KEY,
    md5 TEXT NOT NULL,
    title TEXT NOT NULL,
    sub_title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    updated_at REAL NOT NULL
  )
  """,
  """
  CREATE TABLE IF NOT EXISTS guide_airings (
    chno INTEGER NOT NULL,
    epg_channel_id TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    stop_ts INTEGER NOT NULL,
    program_uid TEXT NOT NULL,
    title TEXT NOT NULL,
    sub_title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (chno, epg_channel_id, start_ts)
  )
  """,
  """
  CREATE INDEX IF NOT EXISTS idx_guide_chno_stop
    ON guide_airings (chno, stop_ts, start_ts)
  """,
  """
  CREATE INDEX IF NOT EXISTS idx_guide_epg_stop
    ON guide_airings (epg_channel_id, stop_ts, start_ts)
  """,
  """
  CREATE INDEX IF NOT EXISTS idx_guide_stop
    ON guide_airings (stop_ts)
  """,
  """
  CREATE INDEX IF NOT EXISTS idx_sd_programs_updated
    ON sd_programs (updated_at)
  """,
)


@dataclass(slots=True)
class SdStationRow:
  """Normalized Schedules Direct station row stored in `sd_stations`."""

  station_id: str
  name: str
  callsign: str = ""
  language: str = ""


@dataclass(slots=True)
class SdLineupRecord:
  """Cached Schedules Direct lineup mapping loaded from SQLite."""

  lineup_id: str
  name: str
  location: str
  modified: str
  fetched_at: float
  stations: dict[str, SdStationRow] = field(default_factory=dict)
  chno_to_station: dict[int, str] = field(default_factory=dict)


@dataclass(slots=True)
class EpgCoverageSummary:
  """Aggregate coverage counts computed directly by SQLite."""

  total_programs: int = 0
  active_programs: int = 0
  max_stop_timestamp: int = 0
  channels_with_epg: int = 0


def _format_xmltv_ts(epoch_ts: int) -> str:
  return datetime.fromtimestamp(epoch_ts, tz=timezone.utc).strftime(
    "%Y%m%d%H%M%S +0000"
  )


def _format_xtream_utc(epoch_ts: int) -> str:
  return datetime.fromtimestamp(epoch_ts, tz=timezone.utc).strftime(
    "%Y-%m-%d %H:%M:%S"
  )


def _b64_utf8(text: str) -> str:
  return base64.b64encode(text.encode("utf-8")).decode("ascii")


class EpgDatabase:
  """Embedded WAL-mode SQLite 3 repository for EPG listings and SD MD5 state."""

  def __init__(self, db_path: Path = DEFAULT_EPG_DB_FILE) -> None:
    self.db_path = db_path
    self._initialized = False
    self._init_lock = threading.Lock()
    self._summary_lock = threading.Lock()
    self._pool_lock = threading.Lock()
    self._idle_conns: list[tuple[tuple[int, int], sqlite3.Connection]] = []
    self._cached_summary: EpgCoverageSummary | None = None

  def __del__(self) -> None:
    with contextlib.suppress(Exception):
      self.close()

  def close(self) -> None:
    """Close any pooled SQLite connections held by this repository."""
    with self._pool_lock:
      conns = list(self._idle_conns)
      self._idle_conns.clear()
    for _cid, conn in conns:
      with contextlib.suppress(sqlite3.Error):
        conn.close()

  def _set_cached_summary(self, summary: EpgCoverageSummary | None) -> None:
    with self._summary_lock:
      self._cached_summary = summary

  def cached_coverage_stats(
    self, *, now_ts: int | None = None
  ) -> EpgCoverageSummary:
    """Return the latest in-memory `EpgCoverageSummary` without touching
    SQLite.
    """
    with self._summary_lock:
      summary = self._cached_summary
    if summary is None:
      return EpgCoverageSummary()
    if (
      now_ts is not None
      and summary.max_stop_timestamp > 0
      and (now_ts - EPG_RECENT_FLOOR_SEC) >= summary.max_stop_timestamp
    ):
      return EpgCoverageSummary(
        total_programs=summary.total_programs,
        active_programs=0,
        max_stop_timestamp=summary.max_stop_timestamp,
        channels_with_epg=summary.channels_with_epg,
      )
    return summary

  @staticmethod
  def _query_coverage_on_conn(
    conn: sqlite3.Connection, *, now_ts: int
  ) -> EpgCoverageSummary:
    recent_floor = now_ts - EPG_RECENT_FLOOR_SEC
    row = conn.execute(
      """
      SELECT
        COUNT(*),
        COALESCE(SUM(CASE WHEN stop_ts = 0 OR stop_ts > ? THEN 1 ELSE 0 END), 0),
        COALESCE(MAX(stop_ts), 0),
        COUNT(DISTINCT CASE WHEN chno > 0 THEN 'c:' || chno ELSE 'e:' || epg_channel_id END)
      FROM guide_airings
      """,
      (recent_floor,),
    ).fetchone()
    if row is None:
      return EpgCoverageSummary()
    return EpgCoverageSummary(
      total_programs=int(row[0] or 0),
      active_programs=int(row[1] or 0),
      max_stop_timestamp=int(row[2] or 0),
      channels_with_epg=int(row[3] or 0),
    )

  def _enforce_private_permissions(self) -> None:
    for suffix in ("", "-wal", "-shm"):
      chmod_private_file(Path(f"{self.db_path}{suffix}"))

  def _borrow_connection(
    self, cur_id: tuple[int, int]
  ) -> sqlite3.Connection | None:
    stale: list[sqlite3.Connection] = []
    borrowed: sqlite3.Connection | None = None
    with self._pool_lock:
      while self._idle_conns:
        cid, conn = self._idle_conns.pop()
        if cid == cur_id:
          borrowed = conn
          break
        stale.append(conn)
    for dead in stale:
      with contextlib.suppress(sqlite3.Error):
        dead.close()
    return borrowed

  def _return_connection(
    self, cur_id: tuple[int, int], conn: sqlite3.Connection
  ) -> None:
    evict: sqlite3.Connection | None = None
    with self._pool_lock:
      if len(self._idle_conns) < _MAX_POOL_SIZE:
        self._idle_conns.append((cur_id, conn))
      else:
        evict = conn
    if evict is not None:
      with contextlib.suppress(sqlite3.Error):
        evict.close()

  @contextlib.contextmanager
  def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    if write:
      ensure_private_dir(self.db_path.parent)
      if not self.db_path.exists():
        with contextlib.suppress(OSError):
          fd = os.open(
            str(self.db_path),
            os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            PRIVATE_FILE_MODE,
          )
          os.close(fd)
    st = self.db_path.stat()
    cur_id = (int(st.st_dev), int(st.st_ino))
    conn = self._borrow_connection(cur_id)
    if conn is None:
      conn = sqlite3.connect(
        str(self.db_path),
        timeout=5.0,
        isolation_level=None,
        check_same_thread=False,
      )
      try:
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA temp_store=MEMORY")
      except Exception:
        with contextlib.suppress(sqlite3.Error):
          conn.close()
        raise

    healthy = True
    try:
      yield conn
    except Exception:
      healthy = False
      raise
    finally:
      if not healthy or conn.in_transaction:
        with contextlib.suppress(sqlite3.Error):
          if conn.in_transaction:
            conn.execute("ROLLBACK")
          conn.close()
      else:
        self._return_connection(cur_id, conn)
      if write:
        self._enforce_private_permissions()

  def delete_database(self) -> None:
    """Remove the SQLite database file and any `-wal` / `-shm` sidecars."""
    self._initialized = False
    self.close()
    self._set_cached_summary(EpgCoverageSummary())
    for suffix in ("", "-wal", "-shm"):
      cand = Path(f"{self.db_path}{suffix}")
      with contextlib.suppress(OSError):
        cand.unlink(missing_ok=True)

  def ensure_initialized(self) -> None:
    """Create or migrate SQLite tables under a thread-safe initialization
    lock.
    """
    if self._initialized and self.db_path.is_file():
      return
    with self._init_lock:
      if self._initialized and self.db_path.is_file():
        return
      if self.db_path.is_file():
        try:
          with self.db_path.open("rb") as fh:
            header = fh.read(16)
          if header and header != b"SQLite format 3\x00":
            logger.warning(
              "Replacing non-SQLite file at %s with fresh EPG database.",
              self.db_path,
            )
            self.delete_database()
        except OSError:
          pass
      try:
        self._init_schema()
      except sqlite3.DatabaseError as exc:
        logger.warning(
          "Recreating unreadable SQLite EPG database %s (%s).",
          self.db_path,
          exc,
        )
        self.delete_database()
        self._init_schema()

      self._enforce_private_permissions()
      self._initialized = True

  def _init_schema(self) -> None:
    self.close()
    with self._connect(write=True) as conn:
      auto_vac = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
      cur_ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
      if auto_vac != 2:
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        if cur_ver != 0:
          conn.execute("VACUUM")
      conn.execute("PRAGMA journal_mode=WAL")
      conn.execute("BEGIN IMMEDIATE")
      try:
        if cur_ver != EPG_DB_SCHEMA_VERSION:
          if cur_ver != 0:
            existing_tables = [
              str(r[0])
              for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
              ).fetchall()
            ]
            for tbl in existing_tables:
              conn.execute(f'DROP TABLE IF EXISTS "{tbl}"')
          for stmt in _DDL_STATEMENTS:
            conn.execute(stmt)
          conn.execute(f"PRAGMA user_version = {EPG_DB_SCHEMA_VERSION}")
        else:
          for stmt in _DDL_STATEMENTS:
            conn.execute(stmt)
        conn.execute("COMMIT")
      except Exception:
        conn.execute("ROLLBACK")
        raise

  # ---------------------------------------------------------------------------
  # Metadata & Guide Schedule Persistence
  # ---------------------------------------------------------------------------
  def get_meta(self) -> dict[str, str]:
    """Return all key/value metadata pairs stored in `epg_meta`."""
    if not self.db_path.is_file():
      return {}
    self.ensure_initialized()
    with self._connect() as conn:
      return {
        str(k): str(v)
        for k, v in conn.execute("SELECT key, value FROM epg_meta").fetchall()
      }

  def has_sd_cache(self) -> bool:
    """Return `True` if `sd_programs` or `sd_schedule_days` holds cached SD
    data.
    """
    if not self.db_path.is_file():
      return False
    self.ensure_initialized()
    with self._connect() as conn:
      row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM sd_programs LIMIT 1)"
      ).fetchone()
      return bool(row and row[0])

  def prune_stale_airings(self, *, stale_cutoff_ts: int) -> tuple[int, int]:
    """Delete `guide_airings` ending before `stale_cutoff_ts`; return `(evicted,
    remaining)`.
    """
    if not self.db_path.is_file():
      self._set_cached_summary(EpgCoverageSummary())
      return 0, 0
    self.ensure_initialized()
    with self._connect(write=True) as conn:
      conn.execute("BEGIN IMMEDIATE")
      try:
        cur = conn.execute(
          "DELETE FROM guide_airings WHERE stop_ts > 0 AND stop_ts < ?",
          (stale_cutoff_ts,),
        )
        evicted = max(0, cur.rowcount)
        summary = self._query_coverage_on_conn(
          conn, now_ts=stale_cutoff_ts + EPG_RECENT_FLOOR_SEC
        )
        remaining = summary.total_programs
        if remaining == 0:
          conn.execute("DELETE FROM epg_meta WHERE key = 'fetched_at'")
        conn.execute("COMMIT")
      except Exception:
        conn.execute("ROLLBACK")
        raise
      if evicted > 0:
        conn.execute(f"PRAGMA incremental_vacuum({_INCREMENTAL_VACUUM_PAGES})")
    self._set_cached_summary(summary)
    return evicted, remaining

  def replace_guide_schedules(
    self,
    *,
    snap: GuideSnapshotProto,
    programs_by_id: Mapping[str, Sequence[ProgramItem]],
    programs_by_chno: Mapping[int, Sequence[ProgramItem]] | None = None,
    fetched_at: float,
    covered_channel_ids: frozenset[str] = frozenset(),
    guide_fingerprint: str = "",
    xmltv_url_redacted: str = "",
  ) -> int:
    """Atomically replace all `guide_airings` and metadata inside one
    transaction.
    """
    self.ensure_initialized()
    rows: list[tuple[int, str, int, int, str, str, str, str, str, str]] = []
    emitted_epg_ids: set[str] = set()

    if snap.channels:
      for ch in snap.channels:
        raw_list: Sequence[ProgramItem] = ()
        if programs_by_chno is not None and ch.chno in programs_by_chno:
          raw_list = programs_by_chno[ch.chno]
        elif ch.epg_channel_id in programs_by_id:
          raw_list = programs_by_id[ch.epg_channel_id]
        if not raw_list:
          continue
        reconciled = reconcile_non_overlapping_schedule(raw_list)
        cid = ch.epg_channel_id or f"ch.{ch.chno}"
        emitted_epg_ids.add(cid)
        for p in reconciled:
          src = "sd" if p.id.startswith("sd-") else "xmltv"
          rows.append(
            (
              ch.chno,
              cid,
              p.start_timestamp,
              p.stop_timestamp,
              p.id,
              p.title,
              p.sub_title,
              p.description,
              p.category,
              src,
            )
          )

    # Also store any unattached `programs_by_id` entries (`chno = 0`)
    for cid, plist in programs_by_id.items():
      if cid in emitted_epg_ids or not plist:
        continue
      reconciled = reconcile_non_overlapping_schedule(plist)
      for p in reconciled:
        src = "sd" if p.id.startswith("sd-") else "xmltv"
        rows.append(
          (
            0,
            cid,
            p.start_timestamp,
            p.stop_timestamp,
            p.id,
            p.title,
            p.sub_title,
            p.description,
            p.category,
            src,
          )
        )

    effective_covered = sorted(
      covered_channel_ids
      or {cid.strip().lower() for cid in programs_by_id if cid.strip()}
    )

    with self._connect(write=True) as conn:
      conn.execute("BEGIN IMMEDIATE")
      try:
        conn.execute("DELETE FROM guide_airings")
        if rows:
          conn.executemany(
            """
            INSERT OR REPLACE INTO guide_airings
              (chno, epg_channel_id, start_ts, stop_ts, program_uid,
               title, sub_title, description, category, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
          )
        meta_pairs = [
          ("fetched_at", str(fetched_at)),
          ("covered_channel_ids", json.dumps(effective_covered)),
          ("guide_fingerprint", guide_fingerprint),
        ]
        if xmltv_url_redacted:
          meta_pairs.append(("xmltv_url", xmltv_url_redacted))
        conn.executemany(
          "INSERT OR REPLACE INTO epg_meta (key, value) VALUES (?, ?)",
          meta_pairs,
        )
        summary = self._query_coverage_on_conn(conn, now_ts=int(fetched_at))
        conn.execute("COMMIT")
      except Exception:
        conn.execute("ROLLBACK")
        raise
      conn.execute(f"PRAGMA incremental_vacuum({_INCREMENTAL_VACUUM_PAGES})")

    self._set_cached_summary(summary)
    return len(rows)

  # ---------------------------------------------------------------------------
  # Fast Indexed Read Queries (Web Guide, Health, XMLTV, & Xtream API)
  # ---------------------------------------------------------------------------
  def coverage_stats(self, *, now_ts: int | None = None) -> EpgCoverageSummary:
    """Compute `(total_programs, active_programs, max_stop_ts,
    channels_with_epg)` in SQL.
    """
    if not self.db_path.is_file():
      empty = EpgCoverageSummary()
      self._set_cached_summary(empty)
      return empty
    self.ensure_initialized()
    now = int(time.time()) if now_ts is None else now_ts
    with self._connect() as conn:
      summary = self._query_coverage_on_conn(conn, now_ts=now)
    self._set_cached_summary(summary)
    return summary

  def query_channel_schedule(
    self,
    chno: int,
    epg_channel_id: str = "",
    *,
    min_stop_ts: int = 0,
    limit: int = 120,
  ) -> list[ProgramItem]:
    """Query ordered schedule items for one channel (`chno` primary,
    `epg_channel_id` fallback).
    """
    if not self.db_path.is_file():
      return []
    self.ensure_initialized()
    with self._connect() as conn:
      rows = conn.execute(
        """
        SELECT program_uid, epg_channel_id, title, sub_title, description,
               category, start_ts, stop_ts
        FROM guide_airings
        WHERE chno = ? AND (stop_ts = 0 OR stop_ts > ?)
        ORDER BY start_ts ASC
        LIMIT ?
        """,
        (chno, min_stop_ts, limit),
      ).fetchall()
      if not rows and epg_channel_id:
        rows = conn.execute(
          """
          SELECT program_uid, epg_channel_id, title, sub_title, description,
                 category, start_ts, stop_ts
          FROM guide_airings
          WHERE chno = 0 AND epg_channel_id = ? AND (stop_ts = 0 OR stop_ts > ?)
          ORDER BY start_ts ASC
          LIMIT ?
          """,
          (epg_channel_id, min_stop_ts, limit),
        ).fetchall()
    return [
      ProgramItem(
        id=str(r[0]),
        channel_id=str(r[1]),
        title=str(r[2]),
        sub_title=str(r[3]),
        description=str(r[4]),
        category=str(r[5]),
        start_timestamp=int(r[6]),
        stop_timestamp=int(r[7]),
      )
      for r in rows
    ]

  def query_lineup_window(
    self,
    *,
    min_stop_ts: int,
    max_start_ts: int,
    per_channel_limit: int = 24,
  ) -> tuple[dict[int, list[ProgramItem]], dict[str, list[ProgramItem]]]:
    """Query bounded timeline window across all channels in a single indexed
    scan.
    """
    if not self.db_path.is_file():
      return {}, {}
    self.ensure_initialized()
    by_chno: dict[int, list[ProgramItem]] = {}
    by_epg_id: dict[str, list[ProgramItem]] = {}
    with self._connect() as conn:
      cur = conn.execute(
        """
        SELECT chno, epg_channel_id, program_uid, title, sub_title,
               description, category, start_ts, stop_ts
        FROM guide_airings
        WHERE (stop_ts = 0 OR stop_ts > ?) AND start_ts <= ?
        ORDER BY chno ASC, start_ts ASC
        """,
        (min_stop_ts, max_start_ts),
      )
      for r in cur:
        chno = int(r[0])
        cid = str(r[1])
        item = ProgramItem(
          id=str(r[2]),
          channel_id=cid,
          title=str(r[3]),
          sub_title=str(r[4]),
          description=str(r[5]),
          category=str(r[6]),
          start_timestamp=int(r[7]),
          stop_timestamp=int(r[8]),
        )
        if chno > 0:
          bucket = by_chno.setdefault(chno, [])
          if len(bucket) < per_channel_limit:
            bucket.append(item)
        elif cid:
          ebucket = by_epg_id.setdefault(cid, [])
          if len(ebucket) < per_channel_limit:
            ebucket.append(item)
    return by_chno, by_epg_id

  def query_xtream_epg_listings(
    self,
    *,
    chno: int,
    epg_channel_id: str,
    language: str = "en",
    now_ts: int | None = None,
    limit: int = 4,
  ) -> list[dict[str, Any]]:
    """Return Xtream `player_api.php` `epg_listings` (`get_short_epg` /
    `get_simple_data_table`).
    """
    now = int(time.time()) if now_ts is None else now_ts
    items = self.query_channel_schedule(
      chno,
      epg_channel_id,
      min_stop_ts=now - 60,
      limit=max(1, min(200, limit)),
    )
    listings: list[dict[str, Any]] = []
    for idx, p in enumerate(items, start=1):
      is_live = 1 if (p.start_timestamp <= now < p.stop_timestamp) else 0
      desc_full = (
        f"{p.sub_title} — {p.description}"
        if (p.sub_title and p.description)
        else (p.description or p.sub_title)
      )
      listings.append(
        {
          "id": str(idx),
          "epg_id": p.id or str(idx),
          "title": _b64_utf8(p.title),
          "lang": language or "en",
          "start": _format_xtream_utc(p.start_timestamp),
          "end": _format_xtream_utc(p.stop_timestamp),
          "description": _b64_utf8(desc_full),
          "channel_id": epg_channel_id,
          "start_timestamp": str(p.start_timestamp),
          "stop_timestamp": str(p.stop_timestamp),
          "now_playing": is_live,
          "has_archive": 0,
        }
      )
    return listings

  def iter_xmltv_chunks(
    self,
    snap: GuideSnapshotProto,
    *,
    chunk_target_bytes: int = 65536,
    page_rows: int = _XMLTV_PAGE_ROWS,
  ) -> Iterator[bytes]:
    """Stream UTF-8 XMLTV chunks via short-lived keyset-paginated SQLite
    reads.
    """
    self.ensure_initialized()
    buf: list[str] = [
      '<?xml version="1.0" encoding="UTF-8"?>\n',
      '<tv generator-info-name="Bellhop">\n',
    ]
    buf_bytes = sum(len(s) for s in buf)

    primary_chno_for_cid: dict[str, int] = {}
    chno_to_cid: dict[int, str] = {}
    for ch in snap.channels:
      cid = ch.epg_channel_id.strip()
      if not cid:
        continue
      chno_to_cid[ch.chno] = cid
      if cid in primary_chno_for_cid:
        continue
      primary_chno_for_cid[cid] = ch.chno
      esc_cid = xml_escape(cid, {'"': "&quot;"})
      esc_name = xml_escape(ch.name)
      entry = f'  <channel id="{esc_cid}">\n    <display-name>{esc_name}</display-name>\n'
      if ch.logo:
        esc_logo = xml_escape(ch.logo, {'"': "&quot;"})
        entry += f'    <icon src="{esc_logo}"/>\n'
      entry += "  </channel>\n"
      buf.append(entry)
      buf_bytes += len(entry)
      if buf_bytes >= chunk_target_bytes:
        yield "".join(buf).encode("utf-8")
        buf.clear()
        buf_bytes = 0

    eff_page_rows = max(1, page_rows)
    last_chno = -1
    last_epg_id = ""
    last_start_ts = -1

    while True:
      with self._connect() as conn:
        rows = conn.execute(
          """
          SELECT chno, epg_channel_id, start_ts, stop_ts,
                 title, sub_title, description, category
          FROM guide_airings
          WHERE (chno, epg_channel_id, start_ts) > (?, ?, ?)
          ORDER BY chno ASC, epg_channel_id ASC, start_ts ASC
          LIMIT ?
          """,
          (
            last_chno,
            last_epg_id,
            last_start_ts,
            eff_page_rows,
          ),
        ).fetchall()
      if not rows:
        break

      tail = rows[-1]
      last_chno = int(tail[0])
      last_epg_id = str(tail[1])
      last_start_ts = int(tail[2])

      for r in rows:
        chno = int(r[0])
        cid = chno_to_cid.get(chno, "") if chno > 0 else str(r[1]).strip()
        if not cid or cid not in primary_chno_for_cid:
          continue
        if chno > 0 and primary_chno_for_cid.get(cid) != chno:
          continue
        start_ts = int(r[2])
        stop_ts = int(r[3])
        if start_ts <= 0 or stop_ts <= start_ts:
          continue
        esc_cid = xml_escape(cid, {'"': "&quot;"})
        prog = (
          f'  <programme start="{_format_xmltv_ts(start_ts)}" '
          f'stop="{_format_xmltv_ts(stop_ts)}" channel="{esc_cid}">\n'
          f"    <title>{xml_escape(str(r[4]))}</title>\n"
        )
        if r[5]:
          prog += f"    <sub-title>{xml_escape(str(r[5]))}</sub-title>\n"
        if r[6]:
          prog += f"    <desc>{xml_escape(str(r[6]))}</desc>\n"
        if r[7]:
          prog += f"    <category>{xml_escape(str(r[7]))}</category>\n"
        prog += "  </programme>\n"
        buf.append(prog)
        buf_bytes += len(prog)
        if buf_bytes >= chunk_target_bytes:
          yield "".join(buf).encode("utf-8")
          buf.clear()
          buf_bytes = 0

    buf.append("</tv>\n")
    yield "".join(buf).encode("utf-8")

  # ---------------------------------------------------------------------------
  # Schedules Direct Relational Cache Operations
  # ---------------------------------------------------------------------------
  def load_sd_lineups(self) -> dict[str, SdLineupRecord]:
    """Load cached Schedules Direct lineups, stations, and dial maps from
    SQLite.
    """
    if not self.db_path.is_file():
      return {}
    self.ensure_initialized()
    out: dict[str, SdLineupRecord] = {}
    with self._connect() as conn:
      for r in conn.execute(
        "SELECT lineup_id, name, location, modified, fetched_at FROM sd_lineups"
      ):
        lid = str(r[0])
        out[lid] = SdLineupRecord(
          lineup_id=lid,
          name=str(r[1]),
          location=str(r[2]),
          modified=str(r[3]),
          fetched_at=float(r[4]),
        )
      for r in conn.execute(
        "SELECT lineup_id, station_id, name, callsign, language FROM sd_stations"
      ):
        rec = out.get(str(r[0]))
        if rec is not None:
          sid = str(r[1])
          rec.stations[sid] = SdStationRow(
            station_id=sid,
            name=str(r[2]),
            callsign=str(r[3]),
            language=str(r[4]),
          )
      for r in conn.execute(
        "SELECT lineup_id, chno, station_id FROM sd_channel_map"
      ):
        rec = out.get(str(r[0]))
        if rec is not None:
          rec.chno_to_station[int(r[1])] = str(r[2])
    return out

  def save_sd_lineup(self, entry: SdLineupRecord) -> None:
    """Persist one Schedules Direct lineup's metadata, stations, and channel
    map.
    """
    self.ensure_initialized()
    with self._connect(write=True) as conn:
      conn.execute("BEGIN IMMEDIATE")
      try:
        conn.execute(
          """
          INSERT OR REPLACE INTO sd_lineups
            (lineup_id, name, location, modified, fetched_at)
          VALUES (?, ?, ?, ?, ?)
          """,
          (
            entry.lineup_id,
            entry.name,
            entry.location,
            entry.modified,
            entry.fetched_at,
          ),
        )
        conn.execute(
          "DELETE FROM sd_stations WHERE lineup_id = ?", (entry.lineup_id,)
        )
        conn.execute(
          "DELETE FROM sd_channel_map WHERE lineup_id = ?", (entry.lineup_id,)
        )
        conn.executemany(
          """
          INSERT INTO sd_stations (lineup_id, station_id, name, callsign, language)
          VALUES (?, ?, ?, ?, ?)
          """,
          [
            (entry.lineup_id, st.station_id, st.name, st.callsign, st.language)
            for st in entry.stations.values()
          ],
        )
        conn.executemany(
          """
          INSERT INTO sd_channel_map (lineup_id, chno, station_id)
          VALUES (?, ?, ?)
          """,
          [
            (entry.lineup_id, chno, sid)
            for chno, sid in entry.chno_to_station.items()
          ],
        )
        conn.execute("COMMIT")
      except Exception:
        conn.execute("ROLLBACK")
        raise

  def touch_sd_lineup_fetched_at(
    self, lineup_id: str, fetched_at: float
  ) -> None:
    """Update `fetched_at` after verifying `modified` is unchanged on SD
    `/status`.
    """
    self.ensure_initialized()
    with self._connect(write=True) as conn:
      conn.execute(
        "UPDATE sd_lineups SET fetched_at = ? WHERE lineup_id = ?",
        (fetched_at, lineup_id),
      )

  def get_sd_schedule_md5s(
    self, station_ids: Sequence[str], dates: Sequence[str]
  ) -> dict[tuple[str, str], str]:
    """Return `(station_id, schedule_date) -> md5` for cached SD schedule
    days.
    """
    if not station_ids or not dates or not self.db_path.is_file():
      return {}
    self.ensure_initialized()
    out: dict[tuple[str, str], str] = {}
    dt_placeholders = ",".join("?" for _ in dates)
    with self._connect() as conn:
      for idx in range(0, len(station_ids), _SQL_CHUNK):
        chunk = station_ids[idx : idx + _SQL_CHUNK]
        sid_placeholders = ",".join("?" for _ in chunk)
        sql = (
          f"SELECT station_id, schedule_date, md5 FROM sd_schedule_days "
          f"WHERE station_id IN ({sid_placeholders}) "
          f"AND schedule_date IN ({dt_placeholders})"
        )
        for r in conn.execute(sql, [*chunk, *dates]):
          out[(str(r[0]), str(r[1]))] = str(r[2])
    return out

  def upsert_sd_schedule_days(
    self,
    rows: Sequence[tuple[str, str, str, list[dict[str, Any]]]],
    *,
    min_keep_date: str,
  ) -> None:
    """Upsert `(station_id, schedule_date, md5, airings)` rows and prune old
    dates.
    """
    self.ensure_initialized()
    with self._connect(write=True) as conn:
      conn.execute("BEGIN IMMEDIATE")
      try:
        if rows:
          conn.executemany(
            """
            INSERT OR REPLACE INTO sd_schedule_days
              (station_id, schedule_date, md5, airings_json)
            VALUES (?, ?, ?, ?)
            """,
            [
              (
                sid,
                dt,
                md5,
                json.dumps(airings, ensure_ascii=False, separators=(",", ":")),
              )
              for sid, dt, md5, airings in rows
            ],
          )
        conn.execute(
          "DELETE FROM sd_schedule_days WHERE schedule_date < ?",
          (min_keep_date,),
        )
        conn.execute("COMMIT")
      except Exception:
        conn.execute("ROLLBACK")
        raise

  def load_sd_airings(
    self, station_ids: Sequence[str], dates: Sequence[str]
  ) -> dict[str, list[dict[str, Any]]]:
    """Load cached airing lists grouped by `station_id` for `dates`."""
    if not station_ids or not dates or not self.db_path.is_file():
      return {}
    self.ensure_initialized()
    out: dict[str, list[dict[str, Any]]] = {}
    dt_placeholders = ",".join("?" for _ in dates)
    with self._connect() as conn:
      for idx in range(0, len(station_ids), _SQL_CHUNK):
        chunk = station_ids[idx : idx + _SQL_CHUNK]
        sid_placeholders = ",".join("?" for _ in chunk)
        sql = (
          f"SELECT station_id, airings_json FROM sd_schedule_days "
          f"WHERE station_id IN ({sid_placeholders}) "
          f"AND schedule_date IN ({dt_placeholders}) "
          f"ORDER BY schedule_date ASC"
        )
        for sid_raw, raw_json in conn.execute(sql, [*chunk, *dates]):
          with contextlib.suppress(ValueError, TypeError):
            parsed = json.loads(str(raw_json))
            if isinstance(parsed, list):
              out.setdefault(str(sid_raw), []).extend(
                x for x in parsed if isinstance(x, dict)
              )
    return out

  def get_sd_program_md5s(self, program_ids: Sequence[str]) -> dict[str, str]:
    """Return `program_id -> md5` for already-cached Gracenote programs."""
    if not program_ids or not self.db_path.is_file():
      return {}
    self.ensure_initialized()
    out: dict[str, str] = {}
    with self._connect() as conn:
      for idx in range(0, len(program_ids), _SQL_CHUNK):
        chunk = program_ids[idx : idx + _SQL_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        for r in conn.execute(
          f"SELECT program_id, md5 FROM sd_programs WHERE program_id IN ({placeholders})",
          list(chunk),
        ):
          out[str(r[0])] = str(r[1])
    return out

  def upsert_sd_programs(
    self, programs: Mapping[str, Mapping[str, str]], *, updated_at: float
  ) -> None:
    """Upsert Gracenote program metadata rows into `sd_programs`."""
    if not programs:
      return
    self.ensure_initialized()
    with self._connect(write=True) as conn:
      conn.execute("BEGIN IMMEDIATE")
      try:
        conn.executemany(
          """
          INSERT OR REPLACE INTO sd_programs
            (program_id, md5, title, sub_title, description, category, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, ?)
          """,
          [
            (
              pid,
              str(p.get("md5") or ""),
              str(p.get("title") or ""),
              str(p.get("sub_title") or ""),
              str(p.get("description") or ""),
              str(p.get("category") or ""),
              updated_at,
            )
            for pid, p in programs.items()
            if p.get("title")
          ],
        )
        conn.execute("COMMIT")
      except Exception:
        conn.execute("ROLLBACK")
        raise

  def touch_and_prune_sd_programs(
    self,
    active_program_ids: Sequence[str],
    *,
    now_ts: float,
    retention_sec: float = SD_PROGRAM_RETENTION_SEC,
  ) -> int:
    """Refresh `updated_at` for active `program_id`s and prune unreferenced rows
    older than `retention_sec`.
    """
    if not self.db_path.is_file():
      return 0
    self.ensure_initialized()
    cutoff = now_ts - max(0.0, retention_sec)
    with self._connect(write=True) as conn:
      conn.execute("BEGIN IMMEDIATE")
      try:
        for idx in range(0, len(active_program_ids), _SQL_CHUNK):
          chunk = active_program_ids[idx : idx + _SQL_CHUNK]
          placeholders = ",".join("?" for _ in chunk)
          conn.execute(
            f"UPDATE sd_programs SET updated_at = ? WHERE program_id IN ({placeholders})",
            [now_ts, *chunk],
          )
        cur = conn.execute(
          "DELETE FROM sd_programs WHERE updated_at < ?",
          (cutoff,),
        )
        pruned = max(0, cur.rowcount)
        conn.execute("COMMIT")
      except Exception:
        conn.execute("ROLLBACK")
        raise
      if pruned > 0:
        conn.execute(f"PRAGMA incremental_vacuum({_INCREMENTAL_VACUUM_PAGES})")
    return pruned

  def load_sd_programs(
    self, program_ids: Sequence[str]
  ) -> dict[str, dict[str, str]]:
    """Fetch `(title, sub_title, description, category)` for `program_ids`."""
    if not program_ids or not self.db_path.is_file():
      return {}
    self.ensure_initialized()
    out: dict[str, dict[str, str]] = {}
    with self._connect() as conn:
      for idx in range(0, len(program_ids), _SQL_CHUNK):
        chunk = program_ids[idx : idx + _SQL_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        for r in conn.execute(
          f"SELECT program_id, title, sub_title, description, category "
          f"FROM sd_programs WHERE program_id IN ({placeholders})",
          list(chunk),
        ):
          out[str(r[0])] = {
            "title": str(r[1]),
            "sub_title": str(r[2]),
            "description": str(r[3]),
            "category": str(r[4]),
          }
    return out
