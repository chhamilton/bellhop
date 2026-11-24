"""Dynamic passthrough XMLTV EPG fetcher & persistent local cache for Bellhop Web Guide.

Persistent EPG Cache Layout (`cache/bellhop_web/epg_cache.json`, `version = 1`)
----------------------------------------------------------------------------
```json
{
  "version": 1,
  "fetched_at": 1763510400.0,
  "channel_count": 235,
  "program_count": 14820,
  "channels": {
    "RDS.ca": [
      {
        "id": "1",
        "title": "Hockey Canadien",
        "sub_title": "",
        "description": "...",
        "category": "Sports",
        "start_timestamp": 1763510400,
        "stop_timestamp": 1763519400
      }
    ]
  }
}
```
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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import aiohttp

from bellhop_web.api_contracts import EpgStatusDict as EpgStatusDict
from bellhop_web.http_utils import (
  ClientSessionFactory,
  default_session_factory,
  upstream_url,
)
from bellhop_web.store import (
  LineupGuideStore,
  LineupSnapshot,
  ProgramItem,
  open_xml_stream,
  parse_xmltv_ts,
)

logger = logging.getLogger("bellhop_web.epg")


class CachedProgramDict(TypedDict):
  """Compact program entry stored inside `cache/bellhop_web/epg_cache.json`."""

  id: str
  title: str
  sub_title: str
  description: str
  category: str
  start_timestamp: int
  stop_timestamp: int


class _EpgCacheRequiredDict(TypedDict):
  version: int
  fetched_at: float
  channel_count: int
  program_count: int
  channels: dict[str, list[CachedProgramDict]]


class EpgCacheDocumentDict(_EpgCacheRequiredDict, total=False):
  """Top-level schema for `cache/bellhop_web/epg_cache.json`."""

  covered_channel_ids: list[str]


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
  def disabled_for_snapshot(
    cls,
    snap: LineupSnapshot,
    *,
    now_ts: int | None = None,
  ) -> EpgRefreshStatus:
    total_progs, active_progs, max_stop = snap.epg_coverage_stats(now_ts=now_ts)
    return cls(
      enabled=False,
      configured=False,
      refreshing=False,
      state="disabled",
      program_count=total_progs,
      active_program_count=active_progs,
      channels_with_epg=sum(1 for v in snap.programs_by_epg_id.values() if v),
      max_stop_timestamp=max_stop,
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
CACHE_SCHEMA_VERSION = 1
STALE_ENTRY_GRACE_SEC = 7200  # keep shows ending within the last 2 hours
_PASSWORD_QS_RE = re.compile(r"(password=)([^&\s'\"\\]+)", re.IGNORECASE)


def _format_safe_error(exc: Exception) -> str:
  raw = str(exc).strip()
  msg = f"{type(exc).__name__}: {raw}" if raw else type(exc).__name__
  return _PASSWORD_QS_RE.sub(r"\1***", msg)


def build_xmltv_url(server_url: str, username: str, password: str) -> str:
  """Construct the provider's passthrough `xmltv.php` URL from Xtream credentials."""
  base = server_url.strip().rstrip("/")
  if not base or not username.strip() or not password.strip():
    return ""
  if not base.startswith(("http://", "https://")):
    base = f"http://{base}"
  return (
    f"{base}/xmltv.php?username={username.strip()}&password={password.strip()}"
  )


def parse_xmltv_for_channels(
  xml_path: Path,
  channel_id_map: dict[str, str],
  *,
  max_per_channel: int = 120,
  now_timestamp: int | None = None,
) -> dict[str, list[ProgramItem]]:
  """Stream-parse an XMLTV file, deduplicating and windowing programs for mapped channels."""
  if not xml_path.is_file() or not channel_id_map:
    return {}

  ref_now = int(time.time()) if now_timestamp is None else now_timestamp
  recent_cutoff = (
    ref_now - 7200
  )  # keep shows ending within the last 2 hours + all upcoming

  dedup_by_id: dict[str, dict[tuple[int, int, str], ProgramItem]] = {}
  seq = 0

  with open_xml_stream(xml_path) as fh:
    try:
      for _event, elem in ET.iterparse(fh, events=("end",)):
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "programme":
          raw_ch = (elem.attrib.get("channel") or "").strip()
          canonical_id = channel_id_map.get(raw_ch.lower())
          if canonical_id is not None:
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

              item = ProgramItem(
                id=str(seq),
                channel_id=canonical_id,
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
              bucket = dedup_by_id.setdefault(canonical_id, {})
              key = (start_ts, stop_ts, title.casefold())
              existing = bucket.get(key)
              if existing is None or len(desc_str) > len(existing.description):
                bucket[key] = item
          elem.clear()
        elif tag == "channel":
          elem.clear()
    except ET.ParseError:
      pass

  programs_by_id: dict[str, list[ProgramItem]] = {}
  for cid, bucket in dedup_by_id.items():
    ordered = sorted(
      bucket.values(),
      key=lambda p: (p.start_timestamp, p.stop_timestamp, p.id),
    )
    current_and_upcoming = [
      p
      for p in ordered
      if p.stop_timestamp == 0 or p.stop_timestamp >= recent_cutoff
    ]
    chosen = current_and_upcoming if current_and_upcoming else ordered
    programs_by_id[cid] = chosen[:max_per_channel]

  return programs_by_id


@dataclass(slots=True)
class EpgRefreshConfig:
  """Configuration for background and on-demand XMLTV EPG caching."""

  enabled: bool = True
  refresh_hours: float = 6.0
  request_timeout_sec: float = 90.0
  xmltv_url: str = ""
  xmltv_file: Path | None = None
  cache_file: Path = Path("cache/bellhop_web/epg_cache.json")
  check_interval_sec: float = 900.0
  min_future_coverage_sec: float = 7200.0
  min_retry_sec: float = 300.0
  max_per_channel: int = 120


class EpgRefreshService:
  """Dynamically fetches passthrough XMLTV as needed and persists a compact JSON cache."""

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
  def is_configured(self) -> bool:
    if self.config.xmltv_file is not None:
      return True
    return bool(self._resolve_effective_xmltv_url())

  def hydrate_from_cache(self, *, now_ts: int | None = None) -> int:
    """Load the Web Guide's persistent JSON EPG cache on startup, evicting stale entries."""
    cache_path = self.config.cache_file
    if not cache_path.is_file():
      return 0
    try:
      raw = json.loads(cache_path.read_text(encoding="utf-8"))
      if not isinstance(raw, dict):
        return 0
      channels_map = raw.get("channels")
      if not isinstance(channels_map, dict):
        return 0

      ref_now = int(self._clock()) if now_ts is None else now_ts
      stale_cutoff = ref_now - STALE_ENTRY_GRACE_SEC
      programs_by_id: dict[str, list[ProgramItem]] = {}
      evicted_count = 0

      for cid, items in channels_map.items():
        if not isinstance(items, list):
          continue
        parsed_items: list[ProgramItem] = []
        for p in items:
          if not isinstance(p, dict) or not p.get("title"):
            continue
          stop_ts = int(p.get("stop_timestamp") or 0)
          if stop_ts > 0 and stop_ts < stale_cutoff:
            evicted_count += 1
            continue
          parsed_items.append(
            ProgramItem(
              id=str(p.get("id") or ""),
              channel_id=str(cid),
              title=str(p.get("title") or ""),
              sub_title=str(p.get("sub_title") or ""),
              description=str(p.get("description") or ""),
              category=str(p.get("category") or ""),
              start_timestamp=int(p.get("start_timestamp") or 0),
              stop_timestamp=stop_ts,
            )
          )
        if parsed_items:
          programs_by_id[str(cid)] = parsed_items

      if not programs_by_id:
        if evicted_count > 0:
          with contextlib.suppress(OSError):
            cache_path.unlink(missing_ok=True)
          logger.info(
            "Evicted %d stale EPG listings from %s (cache empty; will download fresh guide).",
            evicted_count,
            cache_path,
          )
        return 0

      fetched_at_raw = raw.get("fetched_at")
      fetched_at = (
        float(fetched_at_raw)
        if isinstance(fetched_at_raw, (int, float)) and fetched_at_raw > 0
        else cache_path.stat().st_mtime
      )

      loaded = self.store.apply_epg_programs(programs_by_id)
      raw_covered = raw.get("covered_channel_ids")
      if isinstance(raw_covered, list) and raw_covered:
        self._covered_channel_ids = frozenset(
          str(cid).strip().lower() for cid in raw_covered if str(cid).strip()
        )
      else:
        self._covered_channel_ids = frozenset(
          cid.strip().lower() for cid in programs_by_id if cid.strip()
        )
      self._covered_bundle_sig = self.store.bundle_signature
      self._last_updated = fetched_at
      if self._state == "idle":
        self._state = "ok"

      if evicted_count > 0:
        try:
          self._save_cache(
            programs_by_id, fetched_at, self._covered_channel_ids
          )
        except OSError as exc:
          logger.warning(
            "Could not prune stale entries in %s: %s", cache_path, exc
          )

      logger.info(
        "Loaded %d cached EPG listings across %d channels from %s%s.",
        loaded,
        len(programs_by_id),
        cache_path,
        f" (evicted {evicted_count} stale)" if evicted_count > 0 else "",
      )
      return loaded
    except Exception as exc:
      logger.warning("Ignoring unreadable EPG cache %s: %s", cache_path, exc)
      return 0

  def _save_cache(
    self,
    programs_by_id: dict[str, list[ProgramItem]],
    fetched_at: float,
    covered_channel_ids: frozenset[str] = frozenset(),
  ) -> None:
    cache_path = self.config.cache_file
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    channels_payload: dict[str, list[CachedProgramDict]] = {}
    total_programs = 0
    for cid, items in programs_by_id.items():
      channels_payload[cid] = [
        {
          "id": p.id,
          "title": p.title,
          "sub_title": p.sub_title,
          "description": p.description,
          "category": p.category,
          "start_timestamp": p.start_timestamp,
          "stop_timestamp": p.stop_timestamp,
        }
        for p in items
      ]
      total_programs += len(items)

    effective_covered = sorted(
      covered_channel_ids
      or {cid.strip().lower() for cid in channels_payload if cid.strip()}
    )
    payload: EpgCacheDocumentDict = {
      "version": CACHE_SCHEMA_VERSION,
      "fetched_at": fetched_at,
      "channel_count": len(channels_payload),
      "program_count": total_programs,
      "covered_channel_ids": effective_covered,
      "channels": channels_payload,
    }
    tmp_path = cache_path.with_suffix(".json.tmp")
    tmp_path.write_text(
      json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    tmp_path.replace(cache_path)

  def status_dict(self, *, now_ts: int | None = None) -> EpgStatusDict:
    effective_now = int(self._clock()) if now_ts is None else now_ts
    total_progs = 0
    active_progs = 0
    max_stop_ts = 0
    channels_with_epg = 0
    try:
      snap = self.store.get_snapshot()
      total_progs, active_progs, max_stop_ts = snap.epg_coverage_stats(
        now_ts=effective_now
      )
      channels_with_epg = sum(1 for v in snap.programs_by_epg_id.values() if v)
    except Exception:
      pass

    return EpgRefreshStatus(
      enabled=self.config.enabled,
      configured=self.is_configured,
      refreshing=self._refreshing,
      state="refreshing" if self._refreshing else self._state,
      last_updated=self._last_updated,
      last_attempt=self._last_attempt,
      last_reason=self._last_reason,
      last_error=self._last_error,
      program_count=total_progs,
      active_program_count=active_progs,
      channels_with_epg=channels_with_epg,
      max_stop_timestamp=max_stop_ts,
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

    _total, active_progs, max_stop_ts = snap.epg_coverage_stats(now_ts=now)
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

    if self.config.xmltv_file is not None:
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
      self.hydrate_from_cache()
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
        covered_ids = frozenset(
          cid.strip().lower() for cid in channel_id_map.values() if cid.strip()
        )
        if not channel_id_map:
          self._covered_channel_ids = covered_ids
          self._covered_bundle_sig = bundle_sig
          self._state = "ok"
          return self.status_dict()

        if self.config.xmltv_file is not None:
          xml_path = self.config.xmltv_file
          if not xml_path.is_file():
            raise FileNotFoundError(
              f"Configured XMLTV file not found: {xml_path}"
            )
          programs = await asyncio.to_thread(
            parse_xmltv_for_channels,
            xml_path,
            channel_id_map,
            max_per_channel=self.config.max_per_channel,
            now_timestamp=int(self._clock()),
          )
          now_epoch = self._clock()
          self.store.apply_epg_programs(programs)
          await asyncio.to_thread(
            self._save_cache, programs, now_epoch, covered_ids
          )
          self._covered_channel_ids = covered_ids
          self._covered_bundle_sig = bundle_sig
          self._last_updated = now_epoch
          self._state = "ok"
          return self.status_dict()

        effective_url = self._resolve_effective_xmltv_url()
        if not effective_url:
          raise RuntimeError("No upstream XMLTV passthrough URL available.")

        # Stream into a short-lived temp file, extract only mapped channels, then delete it
        self.config.cache_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
          prefix="xmltv_fetch_",
          suffix=".xml",
          dir=str(self.config.cache_file.parent),
        )
        tmp_xml_path = Path(tmp_name)
        logger.info(
          "Fetching live XMLTV guide from upstream passthrough (reason=%s)...",
          reason,
        )
        owns_client = self._client is None
        client = (
          self._client if self._client is not None else self._new_session()
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

        programs = await asyncio.to_thread(
          parse_xmltv_for_channels,
          tmp_xml_path,
          channel_id_map,
          max_per_channel=self.config.max_per_channel,
          now_timestamp=int(self._clock()),
        )
        now_epoch = self._clock()
        total_loaded = self.store.apply_epg_programs(
          programs, xmltv_url=effective_url
        )
        await asyncio.to_thread(
          self._save_cache, programs, now_epoch, covered_ids
        )
        self._covered_channel_ids = covered_ids
        self._covered_bundle_sig = bundle_sig
        self._last_updated = now_epoch
        self._state = "ok"
        logger.info(
          "EPG guide refreshed (%d listings across %d channels; cached to %s).",
          total_loaded,
          len(programs),
          self.config.cache_file,
        )
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
