"""Dynamic Schedules Direct & XMLTV EPG fetcher backed by WAL-mode SQLite (`epg.sqlite3`).

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
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import aiohttp

from bellhop_web.api_contracts import EpgStatusDict as EpgStatusDict
from bellhop_web.epg_db import (
  DEFAULT_EPG_DB_FILE,
  EpgDatabase,
)
from bellhop_web.file_utils import ensure_private_dir
from bellhop_web.http_utils import (
  ClientSessionFactory,
  default_session_factory,
  upstream_url,
)
from bellhop_web.sd_epg import (
  SchedulesDirectEpgClient,
  SchedulesDirectEpgConfig,
)
from bellhop_web.store import (
  LineupGuideStore,
  ProgramItem,
  open_xml_stream,
  parse_xmltv_ts,
  reconcile_non_overlapping_schedule,
)

logger = logging.getLogger("bellhop_web.epg")


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


DEFAULT_EPG_USER_AGENT = "Bellhop/0.1 (VLC/3.0.20 LibVLC/3.0.20)"
STALE_ENTRY_GRACE_SEC = 7200  # keep shows ending within the last 2 hours
_PASSWORD_QS_RE = re.compile(r"(password=)([^&\s'\"\\]+)", re.IGNORECASE)


def _format_safe_error(exc: Exception) -> str:
  raw = str(exc).strip()
  msg = f"{type(exc).__name__}: {raw}" if raw else type(exc).__name__
  return _PASSWORD_QS_RE.sub(r"\1***", msg)


def parse_xmltv_for_channels(
  xml_path: Path,
  channel_id_map: dict[str, str],
  *,
  candidate_chains: Mapping[str, Sequence[str]] | None = None,
  max_per_channel: int = 120,
  now_timestamp: int | None = None,
) -> dict[str, list[ProgramItem]]:
  """Stream-parse an XMLTV file using priority fallback per channel and non-overlapping reconciliation."""
  if not xml_path.is_file() or (not channel_id_map and not candidate_chains):
    return {}

  ref_now = int(time.time()) if now_timestamp is None else now_timestamp
  recent_cutoff = (
    ref_now - 7200
  )  # keep shows ending within the last 2 hours + all upcoming

  effective_chains: dict[str, tuple[str, ...]] = {}
  if candidate_chains is not None:
    for canon_id, chain in candidate_chains.items():
      cleaned_chain = tuple(c.strip().lower() for c in chain if c.strip())
      if cleaned_chain:
        effective_chains[canon_id] = cleaned_chain
  else:
    by_canon: dict[str, list[str]] = {}
    for raw_k, canon_id in channel_id_map.items():
      low_k = raw_k.strip().lower()
      if not low_k:
        continue
      bucket = by_canon.setdefault(canon_id, [])
      if low_k == canon_id.lower():
        bucket.insert(0, low_k)
      elif low_k not in bucket:
        bucket.append(low_k)
    effective_chains = {k: tuple(v) for k, v in by_canon.items() if v}

  wanted_raw_keys: set[str] = {
    raw_k for chain in effective_chains.values() for raw_k in chain
  }
  raw_items_by_key: dict[str, list[ProgramItem]] = {}
  seq = 0

  with open_xml_stream(xml_path) as fh:
    try:
      for _event, elem in ET.iterparse(fh, events=("end",)):
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "programme":
          raw_key = (elem.attrib.get("channel") or "").strip().lower()
          if raw_key in wanted_raw_keys:
            title_el = elem.find("title")
            title = (
              (title_el.text or "").strip() if title_el is not None else ""
            )
            if title:
              seq += 1
              sub_el = elem.find("sub-title")
              desc_el = elem.find("desc")
              cat_el = elem.find("category")
              start_ts = parse_xmltv_ts(elem.attrib.get("start") or "")
              stop_ts = parse_xmltv_ts(elem.attrib.get("stop") or "")
              desc_str = (
                (desc_el.text or "").strip() if desc_el is not None else ""
              )

              raw_items_by_key.setdefault(raw_key, []).append(
                ProgramItem(
                  id=str(seq),
                  channel_id=raw_key,
                  title=title,
                  sub_title=(
                    (sub_el.text or "").strip() if sub_el is not None else ""
                  ),
                  description=desc_str,
                  category=(
                    (cat_el.text or "").strip() if cat_el is not None else ""
                  ),
                  start_timestamp=start_ts,
                  stop_timestamp=stop_ts,
                )
              )
          elem.clear()
        elif tag == "channel":
          elem.clear()
    except ET.ParseError:
      pass

  programs_by_id: dict[str, list[ProgramItem]] = {}
  for canonical_id, chain in effective_chains.items():
    selected_raw: list[ProgramItem] | None = None
    fallback_stale: list[ProgramItem] | None = None
    for raw_key in chain:
      cand_list = raw_items_by_key.get(raw_key)
      if not cand_list:
        continue
      reconciled = reconcile_non_overlapping_schedule(cand_list)
      if not reconciled:
        continue
      current_and_upcoming = [
        p
        for p in reconciled
        if p.stop_timestamp == 0 or p.stop_timestamp >= recent_cutoff
      ]
      if current_and_upcoming:
        selected_raw = current_and_upcoming
        break
      if fallback_stale is None:
        fallback_stale = reconciled

    chosen = selected_raw if selected_raw is not None else fallback_stale
    if chosen:
      programs_by_id[canonical_id] = [
        replace(p, channel_id=canonical_id) for p in chosen[:max_per_channel]
      ]

  return programs_by_id


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
    store: LineupGuideStore,
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
    self._covered_bundle_sig: tuple[float, ...] = ()
    self._seen_db_mtime_ns: int = -1

  def _resolve_effective_xmltv_url(self) -> str:
    # Prefer the passthrough URL embedded in the generated playlist, falling back to config
    try:
      snap = self.store.get_snapshot()
      if snap.xmltv_url:
        return snap.xmltv_url
    except Exception:
      pass
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

    restored_sig: tuple[float, ...] = ()
    raw_sig_json = meta.get("covered_bundle_sig", "")
    if raw_sig_json:
      with contextlib.suppress(ValueError, TypeError):
        parsed_sig = json.loads(raw_sig_json)
        if isinstance(parsed_sig, list) and parsed_sig:
          restored_sig = tuple(float(v) for v in parsed_sig)

    self._covered_channel_ids = frozenset(covered_ids)
    self._covered_bundle_sig = restored_sig or self.store.bundle_signature
    if fetched_at > 0.0:
      self._last_updated = fetched_at

  def _sync_disk_state(self, now_ts: int) -> None:
    """Worker-thread helper: refresh cached coverage and detect external DB writes."""
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
    except Exception as exc:
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
    except Exception:
      return False, "no_snapshot"

    cov = self.db.cached_coverage_stats(now_ts=now)
    active_progs = cov.active_programs
    max_stop_ts = cov.max_stop_timestamp
    current_epg_ids = frozenset(
      cid.strip().lower()
      for cid in snap.build_epg_channel_lookup().values()
      if cid.strip()
    )
    lineup_changed = bool(
      current_epg_ids
      and self._last_updated is not None
      and (
        (
          bool(self._covered_bundle_sig)
          and self.store.bundle_signature != self._covered_bundle_sig
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
      name="bellhop-web-epg-ondemand",
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
        self._run_loop(), name="bellhop-web-epg-refresher"
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
      name="bellhop-web-epg-refresh-once",
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
        logger.warning("Background EPG check error: %s", exc)

      try:
        await asyncio.wait_for(
          self._stop_event.wait(),
          timeout=max(0.02, self.config.check_interval_sec),
        )
      except asyncio.TimeoutError:
        continue

  async def _execute_refresh(self, *, reason: str) -> EpgStatusDict:
    async with self._refresh_lock:
      self._refreshing = True
      self._last_attempt = self._clock()
      self._last_reason = reason
      self._last_error = None
      tmp_xml_path: Path | None = None
      try:
        snap = self.store.get_snapshot()
        bundle_sig = self.store.bundle_signature
        channel_id_map = snap.build_epg_channel_lookup()
        candidate_chains = snap.build_epg_candidate_chains()
        covered_ids = frozenset(
          cid.strip().lower() for cid in candidate_chains if cid.strip()
        )
        if not candidate_chains and not snap.channels:
          self._covered_channel_ids = covered_ids
          self._covered_bundle_sig = bundle_sig
          self._state = "ok"
          return self.status_dict()

        effective_url = self._resolve_effective_xmltv_url()
        has_xmltv_source = self.config.xmltv_file is not None or bool(
          effective_url
        )

        # 1. Primary EPG source: Schedules Direct SD-JSON (when configured)
        sd_by_chno: dict[int, list[ProgramItem]] = {}
        sd_error: Exception | None = None
        if self._sd_client is not None and self._sd_client.enabled:
          try:
            sd_summary = await self._sd_client.fetch_for_snapshot(
              snap,
              now_ts=int(self._clock()),
              max_per_channel=self.config.max_per_channel,
            )
            sd_by_chno = sd_summary.programs_by_chno
          except Exception as exc:
            if not has_xmltv_source:
              raise
            sd_error = exc
            logger.warning(
              "Schedules Direct EPG failed (%s); falling back to XMLTV.",
              _format_safe_error(exc),
            )

        # 2. Secondary / backfill EPG source: XMLTV (fills channels missing from SD)
        need_xmltv = (not sd_by_chno) or (len(sd_by_chno) < len(snap.channels))
        xmltv_programs: dict[str, list[ProgramItem]] = {}
        if need_xmltv and has_xmltv_source and candidate_chains:
          try:
            if self.config.xmltv_file is not None:
              xml_path = self.config.xmltv_file
              if not xml_path.is_file():
                raise FileNotFoundError(
                  f"Configured XMLTV file not found: {xml_path}"
                )
              xmltv_programs = await asyncio.to_thread(
                parse_xmltv_for_channels,
                xml_path,
                channel_id_map,
                candidate_chains=candidate_chains,
                max_per_channel=self.config.max_per_channel,
                now_timestamp=int(self._clock()),
              )
            elif effective_url:
              ensure_private_dir(self.db.db_path.parent)
              fd, tmp_name = tempfile.mkstemp(
                prefix="xmltv_fetch_",
                suffix=".xml",
                dir=str(self.db.db_path.parent),
              )
              tmp_xml_path = Path(tmp_name)
              logger.info(
                "Fetching live XMLTV guide from upstream passthrough (reason=%s)...",
                reason,
              )
              owns_client = self._client is None
              client = (
                self._client
                if self._client is not None
                else self._new_session()
              )
              try:
                with open(fd, "wb") as out_fh:
                  async with client.get(upstream_url(effective_url)) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.content.iter_chunked(65536):
                      out_fh.write(chunk)
              finally:
                if owns_client:
                  await client.close()

              xmltv_programs = await asyncio.to_thread(
                parse_xmltv_for_channels,
                tmp_xml_path,
                channel_id_map,
                candidate_chains=candidate_chains,
                max_per_channel=self.config.max_per_channel,
                now_timestamp=int(self._clock()),
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

        # 3. Merge schedules (Schedules Direct primary, XMLTV backfill) & commit to SQLite
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
            bundle_sig=bundle_sig,
            xmltv_url_redacted=redacted_url,
          )
          self.store.touch_epg_revision(xmltv_url=effective_url)
          covered_total = len(merged_by_chno)
          logger.info(
            "EPG guide refreshed (%d listings across %d/%d channels: %d Schedules Direct + %d XMLTV backfill; saved to %s).",
            total_loaded,
            covered_total,
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
            bundle_sig=bundle_sig,
            xmltv_url_redacted=redacted_url,
          )
          self.store.touch_epg_revision(xmltv_url=effective_url)
          logger.info(
            "EPG guide refreshed (%d listings across %d channels; saved to %s).",
            total_loaded,
            len(xmltv_programs),
            self.db.db_path,
          )

        self._seen_db_mtime_ns = await asyncio.to_thread(
          self._current_db_mtime_ns
        )
        self._covered_channel_ids = covered_ids
        self._covered_bundle_sig = bundle_sig
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
        if tmp_xml_path is not None:
          with contextlib.suppress(OSError):
            tmp_xml_path.unlink(missing_ok=True)
        self._refreshing = False

      return self.status_dict()
