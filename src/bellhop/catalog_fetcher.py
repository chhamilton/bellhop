"""Phase (2): Daily IPTV Playlist JSON Fetcher with snapshot state and stream diffing.

Catalog Snapshot Schema (`state/catalogs/iptv_catalog_<sha256_16>.json`)
------------------------------------------------------------------------
```json
{
  "saved_timestamp": 1763510400.0,
  "fetched_at": "2026-11-19T00:00:00+00:00",
  "account": { "username": "...", "status": "Active", "max_connections": 1, ... },
  "categories": { "1": "CA: Francophone", ... },
  "stream_count": 16622,
  "streams_with_native_epg": 1156,
  "last_diff": {
    "added_count": 0,
    "removed_count": 0,
    "renamed_count": 0,
    "epg_updated_count": 0,
    "added_stream_ids": [],
    "removed_stream_ids": [],
    "renamed_stream_ids": [],
    "epg_updated_stream_ids": [],
    "has_changes": false
  },
  "streams": [ /* XtreamStream dicts */ ]
}
```
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from bellhop.config import AppConfig
from bellhop.models import XtreamStream
from bellhop.xtream import (
  XtreamCatalog,
  fetch_xtream_catalog,
  load_catalog_from_json,
)


@dataclass(slots=True)
class CatalogDiff:
  """Changes detected between yesterday's IPTV catalog and today's fetched catalog."""

  added_stream_ids: list[int] = field(default_factory=list)
  removed_stream_ids: list[int] = field(default_factory=list)
  renamed_stream_ids: list[int] = field(default_factory=list)
  epg_updated_stream_ids: list[int] = field(default_factory=list)

  @property
  def has_changes(self) -> bool:
    return bool(
      self.added_stream_ids
      or self.removed_stream_ids
      or self.renamed_stream_ids
      or self.epg_updated_stream_ids
    )

  def to_dict(self) -> dict[str, Any]:
    return {
      "added_count": len(self.added_stream_ids),
      "removed_count": len(self.removed_stream_ids),
      "renamed_count": len(self.renamed_stream_ids),
      "epg_updated_count": len(self.epg_updated_stream_ids),
      "added_stream_ids": self.added_stream_ids[:200],
      "removed_stream_ids": self.removed_stream_ids[:200],
      "renamed_stream_ids": self.renamed_stream_ids[:200],
      "epg_updated_stream_ids": self.epg_updated_stream_ids[:200],
      "has_changes": self.has_changes,
    }

  @classmethod
  def from_dict(cls, data: dict[str, Any] | None) -> CatalogDiff:
    if not data:
      return cls()
    return cls(
      added_stream_ids=[int(x) for x in data.get("added_stream_ids", [])],
      removed_stream_ids=[int(x) for x in data.get("removed_stream_ids", [])],
      renamed_stream_ids=[int(x) for x in data.get("renamed_stream_ids", [])],
      epg_updated_stream_ids=[
        int(x) for x in data.get("epg_updated_stream_ids", [])
      ],
    )


@dataclass(slots=True)
class CatalogFetchResult:
  """Result of Phase (2) daily IPTV JSON playlist retrieval."""

  catalog: XtreamCatalog
  from_cache: bool
  age_hours: float
  streams_with_native_epg: int
  diff: CatalogDiff
  snapshot_path: Path


def compute_catalog_diff(
  previous_streams: list[XtreamStream] | None,
  current_streams: list[XtreamStream],
) -> CatalogDiff:
  """Compute stream additions, removals, renames, and native EPG updates."""
  if previous_streams is None:
    return CatalogDiff(added_stream_ids=[s.stream_id for s in current_streams])

  prev_map = {s.stream_id: s for s in previous_streams}
  curr_map = {s.stream_id: s for s in current_streams}

  added = sorted(set(curr_map) - set(prev_map))
  removed = sorted(set(prev_map) - set(curr_map))
  renamed: list[int] = []
  epg_updated: list[int] = []

  for sid in sorted(set(curr_map) & set(prev_map)):
    p = prev_map[sid]
    c = curr_map[sid]
    if p.name != c.name or p.category_name != c.category_name:
      renamed.append(sid)
    if p.epg_channel_id != c.epg_channel_id:
      epg_updated.append(sid)

  return CatalogDiff(
    added_stream_ids=added,
    removed_stream_ids=removed,
    renamed_stream_ids=renamed,
    epg_updated_stream_ids=epg_updated,
  )


def _catalog_snapshot_path(
  state_dir: Path, server_url: str, username: str
) -> Path:
  norm = server_url.strip().rstrip("/").lower()
  for prefix in ("https://", "http://"):
    if norm.startswith(prefix):
      norm = norm[len(prefix) :]
      break
  ident = f"{norm}|{username}" if (norm or username) else "local_catalog"
  digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]
  return state_dir / "catalogs" / f"iptv_catalog_{digest}.json"


def _count_native_epg(streams: list[XtreamStream]) -> int:
  return sum(1 for s in streams if bool(s.epg_channel_id.strip()))


def _save_catalog_state(
  snap_path: Path,
  catalog: XtreamCatalog,
  diff: CatalogDiff,
  *,
  saved_ts: float | None = None,
) -> None:
  snap_path.parent.mkdir(parents=True, exist_ok=True)
  ts = saved_ts if saved_ts is not None else time.time()
  payload = {
    "saved_timestamp": ts,
    "fetched_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
    "account": asdict(catalog.account),
    "categories": catalog.categories,
    "stream_count": len(catalog.streams),
    "streams_with_native_epg": _count_native_epg(catalog.streams),
    "last_diff": diff.to_dict(),
    "streams": [asdict(s) for s in catalog.streams],
  }
  tmp_path = snap_path.with_suffix(".tmp")
  tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
  tmp_path.replace(snap_path)


async def fetch_daily_iptv_catalog(
  config: AppConfig,
  *,
  force_refresh: bool = False,
  dry_run: bool = False,
  input_json: Path | None = None,
  client: aiohttp.ClientSession | None = None,
) -> CatalogFetchResult:
  """Fetch IPTV live channels on a daily cadence (default 24h), tracking catalog diffs."""
  xt = config.xtream
  state_dir = config.resolve_path(config.state.state_dir)
  snap_path = _catalog_snapshot_path(state_dir, xt.server_url, xt.username)
  ttl_sec = max(0.0, xt.cache_ttl_hours) * 3600.0

  previous_catalog: XtreamCatalog | None = None
  cached_diff = CatalogDiff()
  snap_age_sec = 0.0

  if snap_path.is_file():
    try:
      raw_meta = json.loads(snap_path.read_text(encoding="utf-8"))
      saved_ts = float(
        raw_meta.get("saved_timestamp", snap_path.stat().st_mtime)
      )
      snap_age_sec = max(0.0, time.time() - saved_ts)
      previous_catalog = load_catalog_from_json(snap_path)
      cached_diff = CatalogDiff.from_dict(raw_meta.get("last_diff"))
    except (OSError, ValueError, KeyError, TypeError):
      previous_catalog = None

  # Offline JSON seed (via config `xtream.seed_json` or explicit `input_json`)
  effective_seed = input_json or xt.seed_json
  if effective_seed is not None:
    resolved_input = config.resolve_path(effective_seed)
    loaded = load_catalog_from_json(resolved_input)
    diff = compute_catalog_diff(
      previous_catalog.streams if previous_catalog else None,
      loaded.streams,
    )
    if not dry_run:
      _save_catalog_state(snap_path, loaded, diff)
    return CatalogFetchResult(
      catalog=loaded,
      from_cache=True,
      age_hours=0.0,
      streams_with_native_epg=_count_native_epg(loaded.streams),
      diff=diff,
      snapshot_path=snap_path,
    )

  # Daily TTL check: reuse existing state snapshot if younger than 24 hours
  if (
    previous_catalog is not None
    and not force_refresh
    and ttl_sec > 0
    and snap_age_sec <= ttl_sec
  ):
    if not xt.server_url.startswith(("http://", "https://")) and xt.server_url:
      proto = previous_catalog.account.server_protocol or "http"
      xt.server_url = f"{proto}://{xt.server_url.rstrip('/')}"
    return CatalogFetchResult(
      catalog=previous_catalog,
      from_cache=True,
      age_hours=round(snap_age_sec / 3600.0, 2),
      streams_with_native_epg=_count_native_epg(previous_catalog.streams),
      diff=cached_diff,
      snapshot_path=snap_path,
    )

  # Fetch from Xtream client (which will also reuse fresh cache/xtream_catalog_*.json if within 24h!)
  fresh = await fetch_xtream_catalog(
    config,
    force_refresh=force_refresh,
    client=client,
  )
  diff = compute_catalog_diff(
    previous_catalog.streams if previous_catalog else None,
    fresh.streams,
  )
  if not dry_run:
    _save_catalog_state(snap_path, fresh, diff)
  return CatalogFetchResult(
    catalog=fresh,
    from_cache=fresh.from_cache,
    age_hours=0.0,
    streams_with_native_epg=_count_native_epg(fresh.streams),
    diff=diff,
    snapshot_path=snap_path,
  )
