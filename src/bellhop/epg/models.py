"""Core EPG schedule data models, interval reconciliation, and guide store
protocols.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import heapq
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from bellhop.common.api_contracts import ProgramItemDict
from bellhop.epg.constants import MAX_XMLTV_DECOMPRESSED_BYTES

_BUMPER_OVERLAP_SEC = 300


def parse_xmltv_ts(raw: str) -> int:
  """Parse an XMLTV `YYYYMMDDHHMMSS [±HHMM]` timestamp into a Unix epoch."""
  cleaned = raw.strip()
  if not cleaned:
    return 0
  parts = cleaned.split()
  dt_part = parts[0][:14]
  tz_part = parts[1] if len(parts) > 1 else "+0000"
  try:
    if len(dt_part) == 14 and len(tz_part) == 5 and tz_part[0] in "+-":
      dt = datetime.strptime(f"{dt_part} {tz_part}", "%Y%m%d%H%M%S %z")
    else:
      dt = datetime.strptime(dt_part.ljust(14, "0"), "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
      )
    return int(dt.timestamp())
  except ValueError:
    return 0


class _BinaryReadable(Protocol):
  def read(self, size: int = -1, /) -> bytes: ...


class _BoundedXmlStream:
  """Bounded binary reader defending `ET.iterparse` against zip/entity bombs."""

  def __init__(
    self,
    raw: _BinaryReadable,
    *,
    max_bytes: int = MAX_XMLTV_DECOMPRESSED_BYTES,
  ) -> None:
    self._raw = raw
    self._max_bytes = max(1, max_bytes)
    self._bytes_read = 0
    self._tail = b""

  def read(self, size: int = -1) -> bytes:
    remaining = self._max_bytes - self._bytes_read
    read_cap = (
      min(65536, remaining + 1) if size < 0 else min(size, remaining + 1)
    )
    if read_cap <= 0:
      raise ValueError(
        f"XMLTV stream exceeded decompressed safety limit ({self._max_bytes} bytes)."
      )
    chunk = self._raw.read(read_cap)
    if not chunk:
      return b""
    self._bytes_read += len(chunk)
    if self._bytes_read > self._max_bytes:
      raise ValueError(
        f"XMLTV stream exceeded decompressed safety limit ({self._max_bytes} bytes)."
      )
    scan_buf = (self._tail + chunk).lower()
    if b"<!entity" in scan_buf:
      raise ValueError(
        "XMLTV stream contains forbidden XML ENTITY declaration."
      )
    self._tail = chunk[-16:]
    return chunk


@contextlib.contextmanager
def open_xml_stream(
  path: Path,
  *,
  max_bytes: int = MAX_XMLTV_DECOMPRESSED_BYTES,
) -> Iterator[_BoundedXmlStream]:
  """Open a plain or gzip-compressed XMLTV file with bounded decompression."""
  with path.open("rb") as probe_fh:
    magic = probe_fh.read(2)
  if magic == b"\x1f\x8b":
    with gzip.open(path, "rb") as gz_fh:
      yield _BoundedXmlStream(gz_fh, max_bytes=max_bytes)
  else:
    with path.open("rb") as raw_fh:
      yield _BoundedXmlStream(raw_fh, max_bytes=max_bytes)


@dataclass(slots=True)
class ProgramItem:
  """Normalized EPG program schedule item."""

  id: str
  channel_id: str
  title: str
  sub_title: str = ""
  description: str = ""
  category: str = ""
  start_timestamp: int = 0
  stop_timestamp: int = 0
  episode_num: str = ""
  air_date: str = ""
  rating: str = ""
  is_new: bool = False
  is_live_airing: bool = False

  def to_dict(self, *, now_ts: int | None = None) -> ProgramItemDict:
    d: ProgramItemDict = {
      "id": self.id,
      "channel_id": self.channel_id,
      "title": self.title,
      "sub_title": self.sub_title,
      "description": self.description,
      "category": self.category,
      "start_timestamp": self.start_timestamp,
      "stop_timestamp": self.stop_timestamp,
    }
    if (
      now_ts is not None
      and self.start_timestamp > 0
      and self.stop_timestamp > self.start_timestamp
    ):
      if self.start_timestamp <= now_ts < self.stop_timestamp:
        pct = (
          (now_ts - self.start_timestamp)
          / (self.stop_timestamp - self.start_timestamp)
          * 100.0
        )
        d["progress_pct"] = round(min(100.0, max(0.0, pct)), 1)
        d["is_live"] = True
      else:
        d["is_live"] = False
    return d


def _program_quality_key(
  item: ProgramItem, seq_idx: int
) -> tuple[int, int, int, int]:
  return (
    1 if "live" in item.title.lower() else 0,
    len(item.description),
    len(item.sub_title),
    seq_idx,
  )


def reconcile_non_overlapping_schedule(
  items: Sequence[ProgramItem],
) -> list[ProgramItem]:
  """Deduplicate and solve 1D weighted interval scheduling without overlaps.

  Args:
    items: Raw unsorted or overlapping `ProgramItem` candidates for one channel.

  Returns:
    Chronological, non-overlapping list of reconciled `ProgramItem` entries.
  """
  if not items:
    return []

  exact_map: dict[tuple[int, int, str], tuple[ProgramItem, int]] = {}
  open_ended: list[ProgramItem] = []
  for idx, p in enumerate(items):
    if p.start_timestamp <= 0 or not p.title:
      continue
    if p.stop_timestamp <= 0:
      open_ended.append(p)
      continue
    if p.stop_timestamp <= p.start_timestamp:
      continue
    key = (p.start_timestamp, p.stop_timestamp, p.title.casefold())
    prev = exact_map.get(key)
    if prev is None or _program_quality_key(p, idx) > _program_quality_key(
      prev[0], prev[1]
    ):
      exact_map[key] = (p, idx)

  if not exact_map:
    open_ended.sort(key=lambda p: (p.start_timestamp, p.id))
    return open_ended

  ordered_pairs = sorted(
    exact_map.values(),
    key=lambda pair: (
      pair[0].start_timestamp,
      pair[0].stop_timestamp,
      pair[1],
    ),
  )
  coalesced: list[tuple[ProgramItem, int]] = []
  for cand, idx in ordered_pairs:
    if coalesced:
      prev_p, prev_idx = coalesced[-1]
      if prev_p.title.casefold() == cand.title.casefold():
        ov = min(prev_p.stop_timestamp, cand.stop_timestamp) - max(
          prev_p.start_timestamp, cand.start_timestamp
        )
        min_dur = min(
          prev_p.stop_timestamp - prev_p.start_timestamp,
          cand.stop_timestamp - cand.start_timestamp,
        )
        if ov > 0 and ov >= 0.40 * min_dur:
          if _program_quality_key(cand, idx) >= _program_quality_key(
            prev_p, prev_idx
          ):
            coalesced[-1] = (cand, idx)
          continue
    coalesced.append((cand, idx))

  has_any_overlap = any(
    coalesced[i][0].stop_timestamp > coalesced[i + 1][0].start_timestamp
    for i in range(len(coalesced) - 1)
  )
  if not has_any_overlap:
    return [p for p, _ in coalesced]

  n = len(coalesced)
  dp = [0.0] * n
  parent = [-1] * n
  near_heap: list[tuple[int, int]] = []
  best_far_dp = -1.0
  best_far_idx = -1

  def _base_weight(p: ProgramItem, seq_idx: int) -> float:
    dur = float(p.stop_timestamp - p.start_timestamp)
    live_bonus = 300.0 if "live" in p.title.lower() else 0.0
    desc_bonus = float(min(180, len(p.description)) + min(60, len(p.sub_title)))
    return dur + live_bonus + desc_bonus + 0.01 * seq_idx

  for j in range(n):
    pj, idx_j = coalesced[j]
    dur_j = pj.stop_timestamp - pj.start_timestamp
    wj = _base_weight(pj, idx_j)
    cutoff_stop = pj.start_timestamp - 60
    while near_heap and near_heap[0][0] < cutoff_stop:
      _stop_k, k = heapq.heappop(near_heap)
      if dp[k] > best_far_dp or (dp[k] == best_far_dp and k > best_far_idx):
        best_far_dp = dp[k]
        best_far_idx = k

    best_val = wj
    best_prev = -1
    if best_far_idx != -1:
      cand_far = best_far_dp + wj
      if cand_far > best_val:
        best_val = cand_far
        best_prev = best_far_idx

    for _stop_i, i in near_heap:
      pi, _ = coalesced[i]
      if pi.start_timestamp >= pj.start_timestamp:
        continue
      if pj.stop_timestamp <= pi.stop_timestamp:
        continue
      ov = pi.stop_timestamp - pj.start_timestamp
      dur_i = pi.stop_timestamp - pi.start_timestamp
      if ov > 0 and (ov > _BUMPER_OVERLAP_SEC or ov > 0.20 * min(dur_i, dur_j)):
        continue
      trimmed_penalty = float(max(0, ov))
      contig_bonus = (
        240.0 if abs(pi.stop_timestamp - pj.start_timestamp) <= 60 else 0.0
      )
      cand_val = dp[i] + wj - trimmed_penalty + contig_bonus
      if cand_val > best_val or (
        cand_val == best_val and best_prev != -1 and i > best_prev
      ):
        best_val = cand_val
        best_prev = i
    dp[j] = best_val
    parent[j] = best_prev
    heapq.heappush(near_heap, (pj.stop_timestamp, j))

  best_end = max(range(n), key=lambda k: dp[k])
  chosen_rev: list[ProgramItem] = []
  cur = best_end
  while cur != -1:
    chosen_rev.append(coalesced[cur][0])
    cur = parent[cur]
  chosen = list(reversed(chosen_rev))

  result: list[ProgramItem] = []
  for idx, item in enumerate(chosen):
    if (
      idx + 1 < len(chosen)
      and item.stop_timestamp > chosen[idx + 1].start_timestamp
    ):
      item = replace(item, stop_timestamp=chosen[idx + 1].start_timestamp)
    if item.stop_timestamp > item.start_timestamp:
      result.append(item)
  return result


class GuideChannelProto(Protocol):
  """Minimal channel interface consumed by `bellhop.epg` without Web UI
  coupling.
  """

  @property
  def chno(self) -> int: ...

  @property
  def sd_chno(self) -> int | None: ...

  @property
  def name(self) -> str: ...

  @property
  def callsign(self) -> str | None: ...

  @property
  def logo(self) -> str: ...

  @property
  def language(self) -> str: ...

  @property
  def epg_channel_id(self) -> str: ...

  @property
  def epg_aliases(self) -> tuple[str, ...]: ...

  @property
  def gracenote_station_id(self) -> str: ...


class GuideSnapshotProto(Protocol):
  """Minimal lineup snapshot interface consumed by `bellhop.epg`."""

  @property
  def lineup_id(self) -> str: ...

  @property
  def provider(self) -> str: ...

  @property
  def market(self) -> str: ...

  @property
  def postal_prefix(self) -> str: ...

  @property
  def xmltv_url(self) -> str: ...

  @property
  def channels(self) -> Sequence[GuideChannelProto]: ...

  def build_epg_channel_lookup(self) -> dict[str, str]: ...

  def build_epg_candidate_chains(self) -> dict[str, tuple[str, ...]]: ...


class GuideStoreProto(Protocol):
  """Minimal hot-reloading guide store interface consumed by
  `EpgRefreshService`.
  """

  def get_snapshot(
    self, *, force_reload: bool = False
  ) -> GuideSnapshotProto: ...

  def touch_epg_revision(self, *, xmltv_url: str = "") -> None: ...


def _compute_raw_guide_fingerprint(snap: GuideSnapshotProto) -> str:
  """Compute uncached SHA-256 fingerprint of channel-to-EPG bindings."""
  rows = [
    (
      ch.chno,
      ch.epg_channel_id.strip().lower(),
      ch.gracenote_station_id.strip(),
      [a.strip().lower() for a in ch.epg_aliases if a.strip()],
    )
    for ch in sorted(snap.channels, key=lambda c: c.chno)
  ]
  payload = json.dumps(rows, separators=(",", ":"), ensure_ascii=True)
  return hashlib.sha256(payload.encode("ascii")).hexdigest()[:24]


def compute_guide_fingerprint(snap: GuideSnapshotProto) -> str:
  """Compute a deterministic SHA-256 fingerprint of channel-to-EPG bindings.

  Unlike file `mtime_ns`, this fingerprint stays identical when `bellhop run`
  re-exports an unchanged lineup, allowing `bellhop run` and `bellhop serve`
  to share SQLite EPG refresh state cleanly.

  Args:
    snap: Active lineup snapshot exposing channels and guide bindings.

  Returns:
    24-character lowercase hex SHA-256 prefix identifying the guide bindings.
  """
  cached = getattr(snap, "_guide_fingerprint", "")
  if isinstance(cached, str) and cached:
    return cached
  return _compute_raw_guide_fingerprint(snap)
