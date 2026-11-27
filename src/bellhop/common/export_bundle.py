"""Typed reader and hot-reload cache for Phase (5) `output/xtream/` bundles.

Consumed by `bellhop.web.store.LineupGuideStore` (Web TV Guide UI),
`bellhop.xtream.server.XtreamExportStore` (Passthrough Xtream Codes API server),
and `bellhop.epg.sync` so bundle discovery, split-JSON fallbacks,
category synthesis, and mtime caching live in one place.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BUNDLE_FILENAMES: tuple[str, str] = (
  "bellhop_xtream_playlist.json",
  "bell_fibe_xtream_playlist.json",
)


def as_str_dict(val: object) -> dict[str, Any]:
  """Return a shallow `dict[str, Any]` copy when `val` is a `Mapping`."""
  return dict(val) if isinstance(val, Mapping) else {}


def resolve_preferred_epg_base_id(
  *,
  chno: int,
  gracenote_station_id: str | None = "",
  has_native_epg: bool = False,
  upstream_epg_channel_id: str | None = "",
  official_tvg_id: str | None = "",
  raw_epg_channel_id: str | None = "",
) -> str:
  """Choose canonical XMLTV channel ID before collision disambiguation."""
  gn_id = (gracenote_station_id or "").strip()
  if gn_id:
    return f"sd.{gn_id}"
  raw_id = (raw_epg_channel_id or "").strip()
  if raw_id:
    return raw_id
  up_id = (upstream_epg_channel_id or "").strip()
  if has_native_epg and up_id:
    return up_id
  off_id = (official_tvg_id or "").strip()
  if off_id:
    return off_id
  if up_id:
    return up_id
  return f"bellhop.{chno}.tv"


def assign_unique_slot_epg_ids(
  slots: Sequence[tuple[int, int | None, str]],
) -> dict[int, str]:
  """Assign unique case-insensitive XMLTV channel IDs across all channel slots.

  Primary/HD slots keep the unsuffixed base ID while secondary or colliding
  slots receive a `.{chno}` suffix so `/xmltv.php` never drops duplicates.
  """
  ordered = sorted(
    slots,
    key=lambda item: (
      1 if (item[1] is not None and item[0] == item[1]) else 0,
      item[0],
    ),
  )
  claimed_lower: set[str] = set()
  assigned: dict[int, str] = {}
  for chno, _sd_chno, base_id in ordered:
    base = base_id.strip() or f"bellhop.{chno}.tv"
    cand = base
    if cand.lower() in claimed_lower:
      cand = f"{base}.{chno}"
      suffix = 2
      while cand.lower() in claimed_lower:
        cand = f"{base}.{chno}.{suffix}"
        suffix += 1
    claimed_lower.add(cand.lower())
    assigned[chno] = cand
  return assigned


def build_epg_lookup_from_pairs(
  pairs: Iterable[tuple[str, Sequence[str]]],
) -> dict[str, str]:
  """Build lowercase channel ID -> canonical `epg_channel_id` lookup map."""
  lookup: dict[str, str] = {}
  cleaned_rows: list[tuple[str, Sequence[str]]] = []
  for raw_canonical, aliases in pairs:
    canonical = raw_canonical.strip()
    if not canonical:
      continue
    lookup[canonical.lower()] = canonical
    cleaned_rows.append((canonical, aliases))
  for canonical, aliases in cleaned_rows:
    for alias in aliases:
      cleaned = alias.strip()
      if cleaned and cleaned.lower() not in lookup:
        lookup[cleaned.lower()] = canonical
  return lookup


def build_epg_chains_from_pairs(
  pairs: Iterable[tuple[str, Sequence[str]]],
) -> dict[str, tuple[str, ...]]:
  """Build ordered `canonical_epg_id -> (primary, aliases...)` lookup map."""
  chains: dict[str, list[str]] = {}
  for raw_canonical, aliases in pairs:
    canonical = raw_canonical.strip()
    if not canonical:
      continue
    bucket = chains.setdefault(canonical, [])
    for cand in (canonical, *aliases):
      low = cand.strip().lower()
      if low and low not in bucket:
        bucket.append(low)
  return {k: tuple(v) for k, v in chains.items()}


@dataclass(slots=True)
class ExportCategoryRecord:
  """Normalized category parsed from an Xtream bundle."""

  category_id: str
  category_name: str
  parent_id: int = 0
  channel_count: int = 0


@dataclass(slots=True)
class ExportStreamRecord:
  """Normalized mapped channel record parsed from Phase (5) export artifacts."""

  num: int
  sd_chno: int | None
  name: str
  callsign: str | None
  stream_id: int
  stream_type: str
  stream_icon: str
  epg_channel_id: str
  has_native_epg: bool
  epg_aliases: tuple[str, ...]
  added: str
  is_adult: int
  category_id: str
  category_ids: tuple[int, ...]
  category_name: str
  custom_sid: str
  tv_archive: int
  tv_archive_duration: int
  language: str
  quality_tier: str
  gracenote_station_id: str
  direct_source: str
  fallback_sources: tuple[str, ...] = ()
  fallback_stream_ids: tuple[int, ...] = ()


@dataclass(slots=True)
class ExportBundleData:
  """Typed in-memory representation of `output/xtream/` export files."""

  initialized: bool
  lineup_id: str = "UNINITIALIZED"
  provider: str = "Bellhop"
  market: str = ""
  postal_prefix: str = ""
  timezone: str = "UTC"
  max_connections: str = "1"
  allowed_output_formats: tuple[str, ...] = ("m3u8", "ts")
  exp_date: str | None = None
  is_trial: str = "0"
  xmltv_url: str = ""
  categories: list[ExportCategoryRecord] = field(default_factory=list)
  streams: list[ExportStreamRecord] = field(default_factory=list)
  loaded_at: float = 0.0


class ExportBundleReader:
  """Mtime-aware shared cache for `output/xtream/` export bundles."""

  def __init__(
    self,
    xtream_dir: Path,
    *,
    fallback_xmltv_url: str = "",
    fallback_xmltv_url_provider: Callable[[], str] | None = None,
    extra_watched_files: Sequence[Path] = (),
  ) -> None:
    self.xtream_dir = xtream_dir.resolve()
    self.fallback_xmltv_url = fallback_xmltv_url.strip()
    self._fallback_xmltv_url_provider = fallback_xmltv_url_provider
    self._extra_watched_files = tuple(p.resolve() for p in extra_watched_files)
    self._cached_sig: tuple[float, ...] = ()
    self._cached_bundle: ExportBundleData | None = None
    self._cached_fallback: str = ""

  def _resolve_fallback_xmltv_url(self, explicit: str = "") -> str:
    direct = explicit.strip()
    if direct:
      return direct
    if self._fallback_xmltv_url_provider is not None:
      return self._fallback_xmltv_url_provider().strip()
    return self.fallback_xmltv_url

  def read_cached(
    self,
    *,
    force_reload: bool = False,
    fallback_xmltv_url: str = "",
  ) -> tuple[tuple[float, ...], ExportBundleData]:
    """Return `(signature, bundle_data)`, reloading on mtime changes."""
    sig = self.compute_signature()
    eff_fallback = self._resolve_fallback_xmltv_url(fallback_xmltv_url)
    if (
      not force_reload
      and self._cached_bundle is not None
      and sig == self._cached_sig
      and self._cached_fallback == eff_fallback
      and (not eff_fallback or bool(self._cached_bundle.xmltv_url))
    ):
      return sig, self._cached_bundle
    bundle = self.read(fallback_xmltv_url=eff_fallback)
    self._cached_sig = sig
    self._cached_bundle = bundle
    self._cached_fallback = eff_fallback
    return sig, bundle

  def watched_files(self) -> list[Path]:
    """Return ordered list of files whose mtimes control hot-reload caching."""
    files = [
      self.xtream_dir / BUNDLE_FILENAMES[0],
      self.xtream_dir / BUNDLE_FILENAMES[1],
      self.xtream_dir / "get_live_streams.json",
      self.xtream_dir / "get_live_categories.json",
    ]
    files.extend(self._extra_watched_files)
    return files

  def compute_signature(self) -> tuple[float, ...]:
    """Compute `(mtime_ns, size)` signature across all watched exports."""
    sig: list[float] = []
    for path in self.watched_files():
      try:
        if path.is_file():
          st = path.stat()
          sig.extend((float(st.st_mtime_ns), float(st.st_size)))
        else:
          sig.extend((0.0, 0.0))
      except OSError:
        sig.extend((0.0, 0.0))
    return tuple(sig)

  def read(self, *, fallback_xmltv_url: str = "") -> ExportBundleData:
    """Load and normalize the Phase (5) export bundle from disk.

    Args:
      fallback_xmltv_url: Default XMLTV URL used when the bundle omits one.

    Returns:
      Normalized `ExportBundleData` (`initialized=False` on an empty volume).
    """
    now = time.time()
    primary_bundle = self.xtream_dir / BUNDLE_FILENAMES[0]
    legacy_bundle = self.xtream_dir / BUNDLE_FILENAMES[1]
    bundle_path = primary_bundle if primary_bundle.is_file() else legacy_bundle
    streams_path = self.xtream_dir / "get_live_streams.json"
    cats_path = self.xtream_dir / "get_live_categories.json"

    lineup_info: dict[str, Any] = {}
    server_info: dict[str, Any] = {}
    user_info: dict[str, Any] = {}
    epg_guide: dict[str, Any] = {}
    raw_categories: list[Any] = []
    raw_streams: list[Any] = []
    bundle_xmltv_url = ""

    if bundle_path.is_file():
      bundle = as_str_dict(json.loads(bundle_path.read_text(encoding="utf-8")))
      lineup_info = as_str_dict(bundle.get("lineup_info"))
      server_info = as_str_dict(bundle.get("server_info"))
      user_info = as_str_dict(bundle.get("user_info"))
      epg_guide = as_str_dict(bundle.get("epg_guide"))
      bundle_xmltv_url = str(
        server_info.get("xmltv_url") or epg_guide.get("xmltv_url") or ""
      ).strip()
      raw_categories = list(bundle.get("live_categories") or [])
      raw_streams = list(bundle.get("live_streams") or [])
    else:
      if cats_path.is_file():
        loaded_cats = json.loads(cats_path.read_text(encoding="utf-8"))
        if isinstance(loaded_cats, list):
          raw_categories = loaded_cats
      if streams_path.is_file():
        loaded_streams = json.loads(streams_path.read_text(encoding="utf-8"))
        if isinstance(loaded_streams, list):
          raw_streams = loaded_streams

    resolved_xmltv_url = bundle_xmltv_url or self._resolve_fallback_xmltv_url(
      fallback_xmltv_url
    )

    if not raw_streams:
      return ExportBundleData(
        initialized=False,
        lineup_id=str(lineup_info.get("lineup_id") or "UNINITIALIZED"),
        provider=str(lineup_info.get("provider") or "Bellhop"),
        market=str(lineup_info.get("market") or ""),
        postal_prefix=str(lineup_info.get("postal_prefix") or ""),
        timezone=str(server_info.get("timezone") or "UTC"),
        xmltv_url=resolved_xmltv_url,
        loaded_at=now,
      )

    cat_by_id: dict[str, ExportCategoryRecord] = {}
    categories: list[ExportCategoryRecord] = []
    for raw_cat in raw_categories:
      item = as_str_dict(raw_cat)
      cid = str(item.get("category_id") or "").strip()
      cname = str(item.get("category_name") or "General").strip() or "General"
      if cid and cid not in cat_by_id:
        cat = ExportCategoryRecord(
          category_id=cid,
          category_name=cname,
          parent_id=int(item.get("parent_id") or 0),
        )
        cat_by_id[cid] = cat
        categories.append(cat)

    streams: list[ExportStreamRecord] = []
    default_added = str(int(now))
    parsed_rows: list[
      tuple[int, dict[str, Any], dict[str, Any], int | None, str, str, bool]
    ] = []
    slot_specs: list[tuple[int, int | None, str]] = []

    for idx, raw_entry in enumerate(raw_streams):
      entry = as_str_dict(raw_entry)
      if not entry:
        continue
      num = int(entry.get("num") or (idx + 1))
      guide = as_str_dict(entry.get("guide"))
      sd_raw = guide.get("sd_chno")
      sd_chno = int(sd_raw) if sd_raw is not None else None
      raw_epg_id = str(
        entry.get("epg_channel_id") or guide.get("epg_channel_id") or ""
      ).strip()
      gn_sid = str(
        guide.get("gracenote_station_id")
        or entry.get("gracenote_station_id")
        or ""
      ).strip()
      has_native_epg = bool(guide.get("has_native_epg", bool(raw_epg_id)))
      official_tvg_id = str(guide.get("official_tvg_id") or "").strip()
      upstream_epg_id = str(guide.get("upstream_epg_channel_id") or "").strip()
      base_epg_id = resolve_preferred_epg_base_id(
        chno=num,
        gracenote_station_id=gn_sid,
        has_native_epg=has_native_epg,
        upstream_epg_channel_id=upstream_epg_id,
        official_tvg_id=official_tvg_id,
        raw_epg_channel_id=raw_epg_id,
      )
      slot_specs.append((num, sd_chno, base_epg_id))
      parsed_rows.append(
        (num, entry, guide, sd_chno, gn_sid, raw_epg_id, has_native_epg)
      )

    unique_epg_by_chno = assign_unique_slot_epg_ids(slot_specs)

    for (
      num,
      entry,
      guide,
      sd_chno,
      gn_sid,
      raw_epg_id,
      has_native_epg,
    ) in parsed_rows:
      cat_id = str(entry.get("category_id") or "1")
      existing_cat = cat_by_id.get(cat_id)
      cat_name = str(
        entry.get("category_name")
        or guide.get("group_title")
        or (existing_cat.category_name if existing_cat is not None else None)
        or "General"
      )
      if existing_cat is None:
        existing_cat = ExportCategoryRecord(
          category_id=cat_id,
          category_name=cat_name,
        )
        cat_by_id[cat_id] = existing_cat
        categories.append(existing_cat)
      existing_cat.channel_count += 1

      epg_id = unique_epg_by_chno.get(num, raw_epg_id or f"bellhop.{num}.tv")
      aliases: list[str] = [epg_id]
      if raw_epg_id and raw_epg_id not in aliases:
        aliases.append(raw_epg_id)
      official_tvg_id = str(guide.get("official_tvg_id") or "").strip()
      if official_tvg_id and official_tvg_id not in aliases:
        aliases.append(official_tvg_id)
      upstream_epg_id = str(guide.get("upstream_epg_channel_id") or "").strip()
      if has_native_epg and upstream_epg_id and upstream_epg_id not in aliases:
        aliases.append(upstream_epg_id)

      callsign_raw = entry.get("callsign") or guide.get("callsign")
      raw_fallbacks = guide.get("fallback_sources")
      fallbacks = (
        tuple(str(u) for u in raw_fallbacks if u and "***" not in str(u))
        if isinstance(raw_fallbacks, list)
        else ()
      )

      raw_stream_id = entry.get("stream_id")
      stream_id = int(raw_stream_id) if raw_stream_id is not None else 0

      raw_fallback_ids = guide.get("fallback_stream_ids")
      parsed_fallback_ids: list[int] = []
      if isinstance(raw_fallback_ids, list):
        for raw_fid in raw_fallback_ids:
          if isinstance(raw_fid, bool):
            continue
          try:
            fid = int(raw_fid)
          except (TypeError, ValueError):
            continue
          if fid > 0 and fid not in parsed_fallback_ids:
            parsed_fallback_ids.append(fid)
      fallback_ids = tuple(parsed_fallback_ids)

      raw_cat_ids = entry.get("category_ids")
      if isinstance(raw_cat_ids, list) and raw_cat_ids:
        category_ids = tuple(int(x) for x in raw_cat_ids)
      elif cat_id.isdigit():
        category_ids = (int(cat_id),)
      else:
        category_ids = (1,)

      streams.append(
        ExportStreamRecord(
          num=num,
          sd_chno=sd_chno,
          name=str(entry.get("name") or f"Channel {num}"),
          callsign=str(callsign_raw) if callsign_raw else None,
          stream_id=stream_id,
          stream_type=str(entry.get("stream_type") or "live"),
          stream_icon=str(
            entry.get("stream_icon") or guide.get("official_logo") or ""
          ),
          epg_channel_id=epg_id,
          has_native_epg=has_native_epg,
          epg_aliases=tuple(aliases),
          added=str(entry.get("added") or default_added),
          is_adult=int(entry.get("is_adult") or 0),
          category_id=cat_id,
          category_ids=category_ids,
          category_name=cat_name,
          custom_sid=str(entry.get("custom_sid") or num),
          tv_archive=int(entry.get("tv_archive") or 0),
          tv_archive_duration=int(entry.get("tv_archive_duration") or 0),
          language=str(
            guide.get("language") or entry.get("language") or ""
          ).strip(),
          quality_tier=str(guide.get("quality_tier") or "unknown"),
          gracenote_station_id=gn_sid,
          direct_source=str(entry.get("direct_source") or "").strip(),
          fallback_sources=fallbacks,
          fallback_stream_ids=fallback_ids,
        )
      )

    raw_fmts = user_info.get("allowed_output_formats")
    allowed_formats = (
      tuple(str(f) for f in raw_fmts if f)
      if isinstance(raw_fmts, list) and raw_fmts
      else ("m3u8", "ts")
    )
    exp_raw = user_info.get("exp_date")

    return ExportBundleData(
      initialized=True,
      lineup_id=str(lineup_info.get("lineup_id") or "UNINITIALIZED"),
      provider=str(lineup_info.get("provider") or "Bellhop"),
      market=str(lineup_info.get("market") or ""),
      postal_prefix=str(lineup_info.get("postal_prefix") or ""),
      timezone=str(server_info.get("timezone") or "UTC"),
      max_connections=str(user_info.get("max_connections") or "1"),
      allowed_output_formats=allowed_formats,
      exp_date=str(exp_raw) if exp_raw is not None else None,
      is_trial=str(user_info.get("is_trial") or "0"),
      xmltv_url=resolved_xmltv_url,
      categories=categories,
      streams=streams,
      loaded_at=now,
    )
