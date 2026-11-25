"""Dynamic Schedules Direct & XMLTV EPG refresher backed by WAL-mode SQLite (`epg.sqlite3`).

Fetches multi-day Schedules Direct (`20141201`) and passthrough XMLTV guide data
in the background, reconciles non-overlapping per-slot schedules, and commits
them to `cache/bellhop_web/epg.sqlite3` without retaining schedule objects in
Python heap memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from bellhop.common.api_contracts import EpgStatusDict as EpgStatusDict
from bellhop.common.file_utils import ensure_private_dir
from bellhop.common.http_utils import (
  ClientSessionFactory,
  default_session_factory,
  upstream_url,
)
from bellhop.epg.db import DEFAULT_EPG_DB_FILE, EpgDatabase
from bellhop.epg.models import (
  GuideSnapshotProto,
  GuideStoreProto,
  ProgramItem,
  compute_guide_fingerprint,
)
from bellhop.epg.sd_epg import (
  SchedulesDirectEpgClient,
  SchedulesDirectEpgConfig,
)
from bellhop.epg.xmltv import (
  DEFAULT_EPG_USER_AGENT,
  parse_xmltv_for_channels,
)

__all__ = [
  "DEFAULT_EPG_USER_AGENT",
  "STALE_ENTRY_GRACE_SEC",
  "EpgRefreshConfig",
  "EpgRefreshService",
  "EpgRefreshStatus",
  "parse_xmltv_for_channels",
]

logger = logging.getLogger("bellhop.epg")

STALE_ENTRY_GRACE_SEC = 7200  # keep shows ending within the last 2 hours
_PASSWORD_QS_RE = re.compile(r"(password=)([^&\s'\"\\]+)", re.IGNORECASE)


def _format_safe_error(exc: Exception) -> str:
  raw = str(exc).strip()
  msg = f"{type(exc).__name__}: {raw}" if raw else type(exc).__name__
  return _PASSWORD_QS_RE.sub(r"\1***", msg)


@dataclass(slots=True)
class EpgRefreshStatus:
  """Structured runtime EPG refresh state and schedule coverage summary."""

  enabled: bool
  configured: bool
  refreshing: bool
  state: str
  last_updated: float | None = None
  last_attempt: float | None = None
  last_reason: str = ""
  last_error: str | None = None
  program_count: int = 0
  active_program_count: int = 0
  channels_with_epg: int = 0
  max_stop_timestamp: int = 0
  refresh_hours: float | None = None

  @classmethod
  def disabled(cls) -> EpgRefreshStatus:
    """Return a disabled EPG status record when no refresher is configured."""
    return cls(
      enabled=False,
      configured=False,
      refreshing=False,
      state="disabled",
    )

  def to_dict(self) -> EpgStatusDict:
    return {
      "enabled": self.enabled,
      "configured": self.configured,
      "refreshing": self.refreshing,
      "state": self.state,
      "last_updated": self.last_updated,
      "last_attempt": self.last_attempt,
      "last_reason": self.last_reason,
      "last_error": self.last_error,
      "program_count": self.program_count,
      "active_program_count": self.active_program_count,
      "channels_with_epg": self.channels_with_epg,
      "max_stop_timestamp": self.max_stop_timestamp,
      "refresh_hours": self.refresh_hours,
    }


@dataclass(slots=True)
class EpgRefreshConfig:
  """Configuration for background and on-demand SQLite EPG caching."""

  enabled: bool = True
  refresh_hours: float = 6.0
  request_timeout_sec: float = 90.0
  xmltv_url: str = ""
  xmltv_file: Path | None = None
  cache_file: Path = DEFAULT_EPG_DB_FILE
  check_interval_sec: float = 900.0
  min_future_coverage_sec: float = 7200.0
  min_retry_sec: float = 300.0
  max_per_channel: int = 120
  sd_epg: SchedulesDirectEpgConfig | None = None


class EpgRefreshService:
  """Dynamically fetches Schedules Direct & XMLTV EPG into SQLite (`epg.sqlite3`)."""

  def __init__(
    self,
    store: GuideStoreProto,
    config: EpgRefreshConfig,
    *,
    session_factory: ClientSessionFactory | None = None,
    clock: Callable[[], float] = time.time,
  ) -> None:
    self.store = store
    self.config = config
    self._clock = clock
    self._session_factory: ClientSessionFactory = (
      session_factory or default_session_factory
    )
    self.db = EpgDatabase(config.cache_file)
    self._sd_client: SchedulesDirectEpgClient | None = (
      SchedulesDirectEpgClient(
        config.sd_epg,
        db=self.db,
        session_factory=self._session_factory,
        clock=self._clock,
      )
      if (config.sd_epg is not None and config.sd_epg.enabled)
      else None
    )
    self._client: aiohttp.ClientSession | None = None
    self._task: asyncio.Task[None] | None = None
    self._stop_event = asyncio.Event()
    self._refresh_lock = asyncio.Lock()
    self._in_flight: asyncio.Task[EpgStatusDict] | None = None

    self._refreshing: bool = False
    self._state: str = "idle" if config.enabled else "disabled"
    self._last_updated: float | None = None
    self._last_attempt: float | None = None
    self._last_reason: str = ""
    self._last_error: str | None = None
    self._covered_channel_ids: frozenset[str] = frozenset()
    self._covered_guide_fingerprint: str = ""
    self._seen_db_mtime_ns: int = -1

  def _resolve_effective_xmltv_url(self) -> str:
    try:
      snap = self.store.get_snapshot()
      if snap.xmltv_url:
        return snap.xmltv_url
    except (OSError, ValueError) as exc:
      logger.debug("Could not read XMLTV URL from snapshot: %s", exc)
    return self.config.xmltv_url

  @property
  def sd_enabled(self) -> bool:
    """Return `True` when Schedules Direct JSON EPG is configured and active."""
    return self._sd_client is not None and self._sd_client.enabled

  def current_epoch(self) -> int:
    """Return current integer UNIX timestamp from the configured clock."""
    return int(self._clock())

  @property
  def is_configured(self) -> bool:
    if self.sd_enabled:
      return True
    if self.config.xmltv_file is not None:
      return True
    return bool(self._resolve_effective_xmltv_url())

  def _current_db_mtime_ns(self) -> int:
    best = -1
    for suffix in ("", "-wal"):
      cand = Path(f"{self.db.db_path}{suffix}")
      with contextlib.suppress(OSError):
        best = max(best, cand.stat().st_mtime_ns)
    return best

  def _apply_meta_dict(
    self, meta: Mapping[str, str], *, fallback_mtime: float = 0.0
  ) -> None:
    fetched_at_raw = meta.get("fetched_at", "")
    try:
      fetched_at = float(fetched_at_raw) if fetched_at_raw else 0.0
    except ValueError:
      fetched_at = 0.0
    if fetched_at <= 0.0:
      fetched_at = fallback_mtime

    covered_ids: set[str] = set()
    raw_cov_json = meta.get("covered_channel_ids", "")
    if raw_cov_json:
      with contextlib.suppress(ValueError, TypeError):
        parsed_cov = json.loads(raw_cov_json)
        if isinstance(parsed_cov, list):
          covered_ids = {
            str(cid).strip().lower() for cid in parsed_cov if str(cid).strip()
          }

    self._covered_channel_ids = frozenset(covered_ids)
    self._covered_guide_fingerprint = meta.get("guide_fingerprint", "").strip()
    if fetched_at > 0.0:
      self._last_updated = fetched_at

  def sync_external_if_needed(self, now_ts: int) -> None:
    """Worker-thread helper: refresh cached coverage and detect external DB writes."""
    if not self.db.db_path.is_file():
      return
    cur_mtime = self._current_db_mtime_ns()
    if cur_mtime != self._seen_db_mtime_ns and not self._refreshing:
      self._seen_db_mtime_ns = cur_mtime
      cov = self.db.coverage_stats(now_ts=now_ts)
      if cov.total_programs > 0:
        meta = self.db.get_meta()
        self._apply_meta_dict(meta)
        if self._state == "idle":
          self._state = "ok"

  def _sync_disk_state(self, now_ts: int) -> None:
    """Periodic worker-thread check: recompute active coverage and sync external writes."""
    if not self.db.db_path.is_file():
      return
    cur_mtime = self._current_db_mtime_ns()
    cov = self.db.coverage_stats(now_ts=now_ts)
    if cur_mtime != self._seen_db_mtime_ns and not self._refreshing:
      self._seen_db_mtime_ns = cur_mtime
      if cov.total_programs > 0:
        meta = self.db.get_meta()
        self._apply_meta_dict(meta)
        if self._state == "idle":
          self._state = "ok"

  def hydrate_from_cache(self, *, now_ts: int | None = None) -> int:
    """Validate and prune the persistent SQLite EPG database on startup."""
    cache_path = self.db.db_path
    if not cache_path.is_file():
      self.db.coverage_stats(now_ts=now_ts)
      return 0
    try:
      with cache_path.open("rb") as fh:
        header = fh.read(16)
      if header != b"SQLite format 3\x00":
        logger.warning("Ignoring non-SQLite EPG cache file at %s.", cache_path)
        return 0
    except OSError as exc:
      logger.warning("Ignoring unreadable EPG cache %s: %s", cache_path, exc)
      return 0

    try:
      self.db.ensure_initialized()
      ref_now = int(self._clock()) if now_ts is None else now_ts
      stale_cutoff = ref_now - STALE_ENTRY_GRACE_SEC
      evicted_count, remaining = self.db.prune_stale_airings(
        stale_cutoff_ts=stale_cutoff
      )
      if remaining == 0:
        if not self.db.has_sd_cache():
          self.db.delete_database()
        self._seen_db_mtime_ns = self._current_db_mtime_ns()
        if evicted_count > 0:
          logger.info(
            "Evicted %d stale EPG listings from %s (guide airings empty; will refresh).",
            evicted_count,
            cache_path,
          )
        return 0

      meta = self.db.get_meta()
      self._apply_meta_dict(meta, fallback_mtime=cache_path.stat().st_mtime)
      if not self._covered_guide_fingerprint:
        with contextlib.suppress(OSError, ValueError):
          self._covered_guide_fingerprint = compute_guide_fingerprint(
            self.store.get_snapshot()
          )
      self.store.touch_epg_revision()
      if self._state == "idle":
        self._state = "ok"

      cov = self.db.coverage_stats(now_ts=ref_now)
      self._seen_db_mtime_ns = self._current_db_mtime_ns()
      logger.info(
        "Hydrated %d SQLite EPG listings across %d channels from %s%s.",
        remaining,
        cov.channels_with_epg,
        cache_path,
        f" (evicted {evicted_count} stale)" if evicted_count > 0 else "",
      )
      return remaining
    except (OSError, sqlite3.Error, ValueError) as exc:
      logger.warning("Ignoring unreadable EPG database %s: %s", cache_path, exc)
      return 0

  def status_dict(self, *, now_ts: int | None = None) -> EpgStatusDict:
    effective_now = int(self._clock()) if now_ts is None else now_ts
    cov = self.db.cached_coverage_stats(now_ts=effective_now)
    return EpgRefreshStatus(
      enabled=self.config.enabled,
      configured=self.is_configured,
      refreshing=self._refreshing,
      state="refreshing" if self._refreshing else self._state,
      last_updated=self._last_updated,
      last_attempt=self._last_attempt,
      last_reason=self._last_reason,
      last_error=self._last_error,
      program_count=cov.total_programs,
      active_program_count=cov.active_programs,
      channels_with_epg=cov.channels_with_epg,
      max_stop_timestamp=cov.max_stop_timestamp,
      refresh_hours=self.config.refresh_hours,
    ).to_dict()

  def needs_refresh(self, *, now_ts: int | None = None) -> tuple[bool, str]:
    if not self.config.enabled:
      return False, "disabled"
    if not self.is_configured:
      return False, "unconfigured"

    now = int(self._clock()) if now_ts is None else now_ts
    try:
      snap = self.store.get_snapshot()
    except (OSError, ValueError) as exc:
      logger.warning("Failed to load lineup snapshot for EPG check: %s", exc)
      return False, "no_snapshot"

    cov = self.db.cached_coverage_stats(now_ts=now)
    active_progs = cov.active_programs
    max_stop_ts = cov.max_stop_timestamp
    current_epg_ids = frozenset(
      cid.strip().lower()
      for cid in snap.build_epg_channel_lookup().values()
      if cid.strip()
    )
    cur_fp = compute_guide_fingerprint(snap)
    lineup_changed = bool(
      (current_epg_ids or snap.channels)
      and self._last_updated is not None
      and (
        (
          bool(self._covered_guide_fingerprint)
          and cur_fp != self._covered_guide_fingerprint
        )
        or bool(current_epg_ids - self._covered_channel_ids)
      )
    )

    if self.config.xmltv_file is not None and not self.sd_enabled:
      if not self.config.xmltv_file.is_file():
        return False, "xmltv_file_missing"
      if active_progs == 0:
        return True, "no_active_programs"
      if lineup_changed:
        return True, "lineup_changed"
      if (
        self._last_updated is None
        or self.config.xmltv_file.stat().st_mtime > self._last_updated
      ):
        return True, "xmltv_file_updated"
      return False, "fresh"

    if (
      self.config.xmltv_file is not None
      and self.config.xmltv_file.is_file()
      and self._last_updated is not None
      and self.config.xmltv_file.stat().st_mtime > self._last_updated
    ):
      return True, "xmltv_file_updated"

    ttl_sec = max(60.0, self.config.refresh_hours * 3600.0)
    if active_progs == 0:
      return True, "no_active_programs"
    if lineup_changed:
      return True, "lineup_changed"
    if max_stop_ts > 0 and max_stop_ts < (
      now + int(self.config.min_future_coverage_sec)
    ):
      return True, "low_coverage"
    if self._last_updated is None:
      return True, "cache_missing"
    if (now - self._last_updated) >= ttl_sec:
      return True, "ttl_expired"
    return False, "fresh"

  def trigger_background_if_needed(
    self, *, reason_prefix: str = "on_demand"
  ) -> bool:
    """Non-blocking check: kick off a background refresh if EPG is stale or empty."""
    if not self.config.enabled or not self.is_configured:
      return False
    if self._in_flight is not None and not self._in_flight.done():
      return False
    should_refresh, reason = self.needs_refresh()
    if not should_refresh:
      return False
    bypass_retry_backoff = reason == "lineup_changed" and self._state != "error"
    if (
      not bypass_retry_backoff
      and self._last_attempt is not None
      and (self._clock() - self._last_attempt) < self.config.min_retry_sec
    ):
      return False

    task = asyncio.create_task(
      self._execute_refresh(reason=f"{reason_prefix}:{reason}"),
      name="bellhop-epg-ondemand",
    )
    self._in_flight = task

    def _clear_done(done_task: asyncio.Task[EpgStatusDict]) -> None:
      if self._in_flight is done_task:
        self._in_flight = None

    task.add_done_callback(_clear_done)
    return True

  def _new_session(self) -> aiohttp.ClientSession:
    return self._session_factory(
      timeout=aiohttp.ClientTimeout(
        total=max(30.0, self.config.request_timeout_sec),
        connect=15.0,
        sock_read=max(30.0, self.config.request_timeout_sec),
      ),
      headers={"User-Agent": DEFAULT_EPG_USER_AGENT},
    )

  async def start(self) -> None:
    if self._client is None:
      self._client = self._new_session()
    if self._last_updated is None:
      await asyncio.to_thread(self.hydrate_from_cache)
    if not self.config.enabled:
      self._state = "disabled"
      return
    if self._task is None or self._task.done():
      self._stop_event.clear()
      self._task = asyncio.create_task(
        self._run_loop(), name="bellhop-epg-refresher"
      )

  async def stop(self) -> None:
    self._stop_event.set()
    if self._task is not None and not self._task.done():
      self._task.cancel()
      with contextlib.suppress(asyncio.CancelledError):
        await self._task
    self._task = None
    if self._in_flight is not None and not self._in_flight.done():
      self._in_flight.cancel()
      with contextlib.suppress(asyncio.CancelledError):
        await self._in_flight
    self._in_flight = None
    if self._client is not None:
      await self._client.close()
      self._client = None

  async def refresh_now(
    self, *, force: bool = True, reason: str = "manual"
  ) -> EpgStatusDict:
    """Run or join an EPG refresh immediately and return the updated status payload."""
    if not self.is_configured:
      self._state = "error"
      self._last_error = (
        "No upstream XMLTV passthrough URL configured "
        "(check generated playlist or credentials.yaml)."
      )
      return self.status_dict()

    if self._in_flight is not None and not self._in_flight.done():
      return await asyncio.shield(self._in_flight)

    if not force:
      should_refresh, _ = self.needs_refresh()
      if not should_refresh:
        return self.status_dict()

    task = asyncio.create_task(
      self._execute_refresh(reason=reason),
      name="bellhop-epg-refresh-once",
    )
    self._in_flight = task
    try:
      return await asyncio.shield(task)
    finally:
      if self._in_flight is task:
        self._in_flight = None

  async def _run_loop(self) -> None:
    while not self._stop_event.is_set():
      try:
        await asyncio.to_thread(self._sync_disk_state, int(self._clock()))
        should_refresh, reason = self.needs_refresh()
        if should_refresh:
          now = self._clock()
          bypass_retry = reason == "lineup_changed" and self._state != "error"
          if (
            bypass_retry
            or self._last_attempt is None
            or (now - self._last_attempt) >= self.config.min_retry_sec
          ):
            await self.refresh_now(force=True, reason=reason)
        elif self._state == "idle":
          self._state = "ok"
      except asyncio.CancelledError:
        raise
      except Exception as exc:
        logger.warning("Background EPG check error: %s", exc, exc_info=True)

      try:
        await asyncio.wait_for(
          self._stop_event.wait(),
          timeout=max(0.02, self.config.check_interval_sec),
        )
      except asyncio.TimeoutError:
        continue

  async def _fetch_sd_schedules(
    self,
    snap: GuideSnapshotProto,
    *,
    has_xmltv_source: bool,
  ) -> tuple[dict[int, list[ProgramItem]], Exception | None]:
    """Fetch primary Schedules Direct schedules when configured."""
    if self._sd_client is None or not self._sd_client.enabled:
      return {}, None
    try:
      sd_summary = await self._sd_client.fetch_for_snapshot(
        snap,
        now_ts=int(self._clock()),
        max_per_channel=self.config.max_per_channel,
      )
      return sd_summary.programs_by_chno, None
    except Exception as exc:
      if not has_xmltv_source:
        raise
      logger.warning(
        "Schedules Direct EPG failed (%s); falling back to XMLTV.",
        _format_safe_error(exc),
      )
      return {}, exc

  async def _fetch_xmltv_schedules(
    self,
    *,
    effective_url: str,
    channel_id_map: dict[str, str],
    candidate_chains: dict[str, tuple[str, ...]],
    reason: str,
  ) -> dict[str, list[ProgramItem]]:
    """Fetch and stream-parse secondary/backfill XMLTV listings."""
    if self.config.xmltv_file is not None:
      xml_path = self.config.xmltv_file
      if not xml_path.is_file():
        raise FileNotFoundError(f"Configured XMLTV file not found: {xml_path}")
      return await asyncio.to_thread(
        parse_xmltv_for_channels,
        xml_path,
        channel_id_map,
        candidate_chains=candidate_chains,
        max_per_channel=self.config.max_per_channel,
        now_timestamp=int(self._clock()),
      )

    ensure_private_dir(self.db.db_path.parent)
    fd, tmp_name = tempfile.mkstemp(
      prefix="xmltv_fetch_",
      suffix=".xml",
      dir=str(self.db.db_path.parent),
    )
    tmp_xml_path = Path(tmp_name)
    try:
      logger.info(
        "Fetching live XMLTV guide from upstream passthrough (reason=%s)...",
        reason,
      )
      owns_client = self._client is None
      client = self._client if self._client is not None else self._new_session()
      try:
        with open(fd, "wb") as out_fh:
          async with client.get(upstream_url(effective_url)) as resp:
            resp.raise_for_status()
            async for chunk in resp.content.iter_chunked(65536):
              out_fh.write(chunk)
      finally:
        if owns_client:
          await client.close()

      return await asyncio.to_thread(
        parse_xmltv_for_channels,
        tmp_xml_path,
        channel_id_map,
        candidate_chains=candidate_chains,
        max_per_channel=self.config.max_per_channel,
        now_timestamp=int(self._clock()),
      )
    finally:
      with contextlib.suppress(OSError):
        tmp_xml_path.unlink(missing_ok=True)

  async def _merge_and_commit_schedules(
    self,
    *,
    snap: GuideSnapshotProto,
    sd_by_chno: dict[int, list[ProgramItem]],
    xmltv_programs: dict[str, list[ProgramItem]],
    covered_ids: frozenset[str],
    guide_fp: str,
    effective_url: str,
  ) -> float:
    """Merge SD primary + XMLTV backfill schedules and commit atomically to SQLite."""
    now_epoch = self._clock()
    redacted_url = (
      _PASSWORD_QS_RE.sub(r"\1***", effective_url) if effective_url else ""
    )
    if sd_by_chno:
      merged_by_chno: dict[int, list[ProgramItem]] = {}
      merged_by_id: dict[str, list[ProgramItem]] = dict(xmltv_programs)
      sd_claimed_ids: set[str] = set()
      xmltv_backfilled = 0
      for ch in snap.channels:
        if ch.chno in sd_by_chno:
          merged_by_chno[ch.chno] = sd_by_chno[ch.chno]
          if ch.epg_channel_id and ch.epg_channel_id not in sd_claimed_ids:
            merged_by_id[ch.epg_channel_id] = sd_by_chno[ch.chno]
            sd_claimed_ids.add(ch.epg_channel_id)
        elif ch.epg_channel_id in xmltv_programs:
          merged_by_chno[ch.chno] = xmltv_programs[ch.epg_channel_id]
          xmltv_backfilled += 1

      total_loaded = await asyncio.to_thread(
        self.db.replace_guide_schedules,
        snap=snap,
        programs_by_id=merged_by_id,
        programs_by_chno=merged_by_chno,
        fetched_at=now_epoch,
        covered_channel_ids=covered_ids,
        guide_fingerprint=guide_fp,
        xmltv_url_redacted=redacted_url,
      )
      self.store.touch_epg_revision(xmltv_url=effective_url)
      logger.info(
        "EPG guide refreshed (%d listings across %d/%d channels: %d Schedules Direct + %d XMLTV backfill; saved to %s).",
        total_loaded,
        len(merged_by_chno),
        len(snap.channels),
        len(sd_by_chno),
        xmltv_backfilled,
        self.db.db_path,
      )
    else:
      total_loaded = await asyncio.to_thread(
        self.db.replace_guide_schedules,
        snap=snap,
        programs_by_id=xmltv_programs,
        fetched_at=now_epoch,
        covered_channel_ids=covered_ids,
        guide_fingerprint=guide_fp,
        xmltv_url_redacted=redacted_url,
      )
      self.store.touch_epg_revision(xmltv_url=effective_url)
      logger.info(
        "EPG guide refreshed (%d listings across %d channels; saved to %s).",
        total_loaded,
        len(xmltv_programs),
        self.db.db_path,
      )
    return now_epoch

  async def _execute_refresh(self, *, reason: str) -> EpgStatusDict:
    async with self._refresh_lock:
      self._refreshing = True
      self._last_attempt = self._clock()
      self._last_reason = reason
      self._last_error = None
      try:
        snap = self.store.get_snapshot()
        guide_fp = compute_guide_fingerprint(snap)
        channel_id_map = snap.build_epg_channel_lookup()
        candidate_chains = snap.build_epg_candidate_chains()
        covered_ids = frozenset(
          cid.strip().lower() for cid in candidate_chains if cid.strip()
        )
        if not candidate_chains and not snap.channels:
          self._covered_channel_ids = covered_ids
          self._covered_guide_fingerprint = guide_fp
          self._state = "ok"
          return self.status_dict()

        effective_url = self._resolve_effective_xmltv_url()
        has_xmltv_source = self.config.xmltv_file is not None or bool(
          effective_url
        )

        sd_by_chno, sd_error = await self._fetch_sd_schedules(
          snap, has_xmltv_source=has_xmltv_source
        )

        need_xmltv = (not sd_by_chno) or (len(sd_by_chno) < len(snap.channels))
        xmltv_programs: dict[str, list[ProgramItem]] = {}
        if need_xmltv and has_xmltv_source and candidate_chains:
          try:
            xmltv_programs = await self._fetch_xmltv_schedules(
              effective_url=effective_url,
              channel_id_map=channel_id_map,
              candidate_chains=candidate_chains,
              reason=reason,
            )
          except Exception as exc:
            if not sd_by_chno:
              if sd_error is not None:
                raise sd_error from exc
              raise
            logger.warning(
              "XMLTV backfill failed (%s); retaining %d Schedules Direct channels.",
              _format_safe_error(exc),
              len(sd_by_chno),
            )
        elif not sd_by_chno and not has_xmltv_source:
          raise RuntimeError("No upstream XMLTV passthrough URL available.")

        now_epoch = await self._merge_and_commit_schedules(
          snap=snap,
          sd_by_chno=sd_by_chno,
          xmltv_programs=xmltv_programs,
          covered_ids=covered_ids,
          guide_fp=guide_fp,
          effective_url=effective_url,
        )

        self._seen_db_mtime_ns = await asyncio.to_thread(
          self._current_db_mtime_ns
        )
        self._covered_channel_ids = covered_ids
        self._covered_guide_fingerprint = guide_fp
        self._last_updated = now_epoch
        self._state = "ok"
      except asyncio.CancelledError:
        raise
      except Exception as exc:
        safe_err = _format_safe_error(exc)
        self._state = "error"
        self._last_error = safe_err
        logger.warning("EPG fetch failed (%s): %s", reason, safe_err)
      finally:
        self._refreshing = False

      return self.status_dict()
