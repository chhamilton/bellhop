"""Phase (1): Multi-backend Cable Lineup Retriever (TV Guide JSON, Schedules
Direct SD-JSON, and YAML).

Lineup Snapshot Schema (`state/lineups/<lineup_id>.json`)
---------------------------------------------------------
```json
{
  "saved_timestamp": 1763510400.0,
  "retrieved_at": "2026-11-19T00:00:00+00:00",
  "lineup_id": "CAN-BELLFIBE-MTL-X",
  "backend": "yaml",
  "last_diff": {
    "added_chnos": [],
    "removed_chnos": [],
    "modified_chnos": [],
    "has_changes": false
  },
  "lineup": {
    "lineup_id": "CAN-BELLFIBE-MTL-X",
    "provider": "Bell Fibe TV",
    "market": "Montréal, QC",
    "postal_prefix": "H2Y",
    "retrieved_at": "...",
    "metadata": {},
    "channels": [ /* LineupChannelDict entries */ ]
  }
}
```
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bellhop.common.file_utils import write_private_text
from bellhop.config import AppConfig
from bellhop.pipeline.dial_grouping import assign_dial_contiguous_groups
from bellhop.pipeline.iptv_org import (
  IptvOrgClient,
  IptvOrgFetchResult,
  IptvOrgIndex,
)
from bellhop.pipeline.lineup_loader import (
  DEFAULT_BELL_FIBE_MTL_LINEUP_ID,
  CableLineup,
)
from bellhop.pipeline.models import LineupChannel
from bellhop.pipeline.providers import (
  LineupBackendError,
  LineupProvider,
  ProviderHeadend,
  SchedulesDirectLineupProvider,
  TvGuideLineupProvider,
  YamlLineupProvider,
  parse_tvguide_provider_id,
)
from bellhop.pipeline.providers.normalizer import populate_sibling_exclusions

# Gracenote / Schedules Direct SD-JSON lineupId convention:
# <ISO3_COUNTRY>-<HEADEND_CODE>-<DEVICE_TYPE>
# Examples: "CAN-0005993-X", "CAN-QC67412-X", "CAN-BELLFIBE-MTL-X",
# "CAN-OTA-H2Y1C6"
GRACENOTE_LINEUP_ID_RE = re.compile(
  r"^(?P<country>[A-Z]{3})-(?P<headend>[A-Z0-9][A-Z0-9_-]*?)(?:-(?P<device>[A-Z0-9]+))?$"
)


class LineupRetrieverError(LineupBackendError):
  """Raised when a lineup identifier is invalid or retrieval fails across
  configured backends.
  """


@dataclass(frozen=True, slots=True)
class GracenoteLineupId:
  """Parsed lineup identifier (Gracenote `<ISO3>-<HEADEND>-<DEVICE>` or TVGuide
  `TVG-<id>`).
  """

  raw_id: str
  country: str
  headend: str
  device: str = "X"

  @classmethod
  def parse(cls, value: str) -> GracenoteLineupId:
    cleaned = value.strip().upper()
    for prefix in ("SD:", "YAML:"):
      if cleaned.startswith(prefix):
        cleaned = cleaned[len(prefix) :].strip()
        break

    tvg_id = parse_tvguide_provider_id(cleaned)
    if tvg_id is not None:
      return cls(
        raw_id=f"TVG-{tvg_id}", country="TVG", headend=tvg_id, device="WEB"
      )

    m = GRACENOTE_LINEUP_ID_RE.match(cleaned)
    if not m:
      raise LineupRetrieverError(
        f"Invalid lineup ID {value!r}. Expected Gracenote format '<ISO3>-<HEADEND>-<DEVICE>' "
        f"(e.g. 'CAN-0005993-X' or '{DEFAULT_BELL_FIBE_MTL_LINEUP_ID}') or TV Guide ID "
        f"(e.g. 'TVG-9100000607')."
      )
    country = m.group("country")
    headend = m.group("headend")
    device = m.group("device") or "DEFAULT"
    canonical = (
      f"{country}-{headend}-{device}"
      if m.group("device")
      else f"{country}-{headend}"
    )
    return cls(
      raw_id=canonical, country=country, headend=headend, device=device
    )


@dataclass(slots=True)
class LineupDiff:
  """Changes detected between the previous monthly cable lineup and the newly
  retrieved one.
  """

  added_chnos: list[int] = field(default_factory=list)
  removed_chnos: list[int] = field(default_factory=list)
  modified_chnos: list[int] = field(default_factory=list)

  @property
  def has_changes(self) -> bool:
    return bool(self.added_chnos or self.removed_chnos or self.modified_chnos)

  def to_dict(self) -> dict[str, Any]:
    return {
      "added_chnos": self.added_chnos,
      "removed_chnos": self.removed_chnos,
      "modified_chnos": self.modified_chnos,
      "has_changes": self.has_changes,
    }

  @classmethod
  def from_dict(cls, data: dict[str, Any] | None) -> LineupDiff:
    if not data:
      return cls()
    return cls(
      added_chnos=[int(x) for x in data.get("added_chnos", [])],
      removed_chnos=[int(x) for x in data.get("removed_chnos", [])],
      modified_chnos=[int(x) for x in data.get("modified_chnos", [])],
    )


@dataclass(slots=True)
class LineupRetrievalResult:
  """Result of Phase (1) cable lineup retrieval."""

  lineup_id: GracenoteLineupId
  lineup: CableLineup
  backend: str
  from_cache: bool
  age_days: float
  diff: LineupDiff
  snapshot_path: Path


def build_default_providers(config: AppConfig) -> list[LineupProvider]:
  """Construct the ordered registry of lineup providers from `AppConfig`."""
  cache_dir = config.resolve_path(config.xtream.cache_dir)
  yaml_path = config.resolve_path(config.lineup.file)
  sd_cfg = config.lineup.schedules_direct
  tvg_cfg = config.lineup.tvguide

  return [
    TvGuideLineupProvider(base_url=tvg_cfg.base_url),
    SchedulesDirectLineupProvider(
      username=sd_cfg.username,
      password=sd_cfg.password,
      base_url=sd_cfg.base_url,
      token_cache_path=cache_dir / "sd_token.json",
      auto_add_lineup=sd_cfg.auto_add_lineup,
    ),
    YamlLineupProvider(yaml_path),
  ]


def select_lineup_provider(
  lineup_id: str,
  *,
  backend_hint: str = "auto",
  providers: list[LineupProvider],
) -> LineupProvider:
  """Select the matching `LineupProvider` implementation for `lineup_id` and
  `backend_hint`.
  """
  norm_hint = (backend_hint or "auto").strip().lower().replace("-", "_")
  if norm_hint == "sd":
    norm_hint = "schedules_direct"
  elif norm_hint == "tvg":
    norm_hint = "tvguide"

  # In "auto" mode, prefer curated YAML for the built-in Bell Fibe default ID
  # when YAML exists
  if norm_hint == "auto":
    for p in providers:
      if (
        p.backend_name == "yaml"
        and p.supports(lineup_id, backend_hint="auto")
        and lineup_id.strip().upper()
        in {DEFAULT_BELL_FIBE_MTL_LINEUP_ID, "CAN-BELLFIBE-QC-X"}
      ):
        return p

  for provider in providers:
    if provider.supports(lineup_id, backend_hint=norm_hint):
      return provider

  available = ", ".join(p.backend_name for p in providers)
  raise LineupRetrieverError(
    f"No lineup provider matched lineup_id={lineup_id!r} (backend={norm_hint!r}; "
    f"registered backends: {available})."
  )


async def discover_provider_lineups(
  config: AppConfig,
  *,
  backend: str = "tvguide",
  country: str = "CAN",
  postal_code: str = "",
  query: str = "",
  providers: list[LineupProvider] | None = None,
) -> list[ProviderHeadend]:
  """Query one or all lineup backends for available regional provider
  headends.
  """
  active_providers = (
    providers if providers is not None else build_default_providers(config)
  )
  target_backend = (backend or "tvguide").strip().lower().replace("-", "_")
  if target_backend == "sd":
    target_backend = "schedules_direct"
  elif target_backend == "tvg":
    target_backend = "tvguide"

  effective_postal = postal_code.strip() or config.lineup.postal_code
  matched_backends = [
    p
    for p in active_providers
    if target_backend in {"all", "auto"} or p.backend_name == target_backend
  ]
  if not matched_backends:
    raise LineupRetrieverError(f"Unknown lineup backend {backend!r}")

  out: list[ProviderHeadend] = []
  last_err: Exception | None = None
  for p in matched_backends:
    try:
      items = await p.discover_lineups(
        country=country,
        postal_code=effective_postal,
        query=query,
      )
      out.extend(items)
    except LineupBackendError as exc:
      last_err = exc
      if target_backend not in {"all", "auto"}:
        raise LineupRetrieverError(str(exc)) from exc

  if not out and last_err is not None and target_backend in {"all", "auto"}:
    raise LineupRetrieverError(str(last_err)) from last_err
  return out


def compute_lineup_diff(
  previous: CableLineup | None, current: CableLineup
) -> LineupDiff:
  """Compare two CableLineup snapshots by channel number and metadata."""
  if previous is None:
    return LineupDiff(added_chnos=[ch.chno for ch in current.channels])

  prev_by_no: dict[int, LineupChannel] = {
    ch.chno: ch for ch in previous.channels
  }
  curr_by_no: dict[int, LineupChannel] = {
    ch.chno: ch for ch in current.channels
  }

  added = sorted(set(curr_by_no) - set(prev_by_no))
  removed = sorted(set(prev_by_no) - set(curr_by_no))
  modified: list[int] = []

  for chno in sorted(set(curr_by_no) & set(prev_by_no)):
    p = prev_by_no[chno]
    c = curr_by_no[chno]
    if (
      p.name != c.name
      or p.sd_chno != c.sd_chno
      or p.callsign != c.callsign
      or p.tvg_id != c.tvg_id
      or p.logo != c.logo
      or p.group != c.group
      or p.aliases != c.aliases
      or p.exclude != c.exclude
    ):
      modified.append(chno)

  return LineupDiff(
    added_chnos=added, removed_chnos=removed, modified_chnos=modified
  )


def _snapshot_path_for(state_dir: Path, lineup_id: GracenoteLineupId) -> Path:
  safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", lineup_id.raw_id)
  return state_dir / "lineups" / f"{safe_name}.json"


def _filter_excluded_audio(
  channels: list[LineupChannel],
) -> list[LineupChannel]:
  return [c for c in channels if "music & audio" not in c.group.lower()]


async def retrieve_cable_lineup(
  config: AppConfig,
  *,
  force_refresh: bool = False,
  dry_run: bool = False,
  providers: list[LineupProvider] | None = None,
  iptv_org_client: IptvOrgClient | None = None,
) -> LineupRetrievalResult:
  """Retrieve target cable lineup on a monthly cadence (default 30 days),
  persisting state & diff.
  """
  raw_id_input = config.lineup.lineup_id
  parsed_id = GracenoteLineupId.parse(raw_id_input)
  active_providers = (
    providers if providers is not None else build_default_providers(config)
  )
  chosen_provider = select_lineup_provider(
    raw_id_input,
    backend_hint=config.lineup.backend,
    providers=active_providers,
  )

  iptv_org_res: IptvOrgFetchResult | None = None
  iptv_org_index: IptvOrgIndex | None = None
  if chosen_provider.backend_name != "yaml" and config.lineup.iptv_org.enabled:
    active_iptv_org = iptv_org_client or IptvOrgClient(
      cache_dir=config.resolve_path(config.xtream.cache_dir),
      base_url=config.lineup.iptv_org.base_url,
      recheck_hours=config.lineup.iptv_org.recheck_hours,
    )
    iptv_org_res = await active_iptv_org.fetch_index(
      force_refresh=force_refresh,
      dry_run=dry_run,
    )
    iptv_org_index = iptv_org_res.index

  state_dir = config.resolve_path(config.state.state_dir)
  snap_path = _snapshot_path_for(state_dir, parsed_id)
  refresh_sec = max(0.0, config.lineup.refresh_days) * 86400.0

  yaml_path = config.resolve_path(config.lineup.file)
  logos_path = yaml_path.parent.parent / "official_logos.json"
  if chosen_provider.backend_name == "yaml":
    source_mtime = max(
      yaml_path.stat().st_mtime if yaml_path.is_file() else 0.0,
      logos_path.stat().st_mtime if logos_path.is_file() else 0.0,
    )
  else:
    source_mtime = 0.0

  previous_lineup: CableLineup | None = None
  cached_diff = LineupDiff()

  if snap_path.is_file():
    try:
      raw = json.loads(snap_path.read_text(encoding="utf-8"))
      saved_ts = float(raw.get("saved_timestamp", snap_path.stat().st_mtime))
      saved_backend = str(raw.get("backend") or chosen_provider.backend_name)
      age_sec = max(0.0, time.time() - saved_ts)
      previous_lineup = CableLineup.from_dict(
        raw.get("lineup") or {},
        excluded_channels=config.lineup.excluded_channels,
      )
      cached_diff = LineupDiff.from_dict(raw.get("last_diff"))

      lineup_cache_fresh = (
        not force_refresh
        and refresh_sec > 0
        and age_sec <= refresh_sec
        and saved_ts >= source_mtime
        and saved_backend == chosen_provider.backend_name
      )
      iptv_org_needs_reapply = (
        chosen_provider.backend_name != "yaml"
        and iptv_org_res is not None
        and not iptv_org_res.index.is_empty
        and (
          iptv_org_res.updated
          or not bool(previous_lineup.metadata.get("iptv_org_enriched"))
        )
      )

      if lineup_cache_fresh and not iptv_org_needs_reapply:
        if config.lineup.exclude_audio:
          previous_lineup.channels = _filter_excluded_audio(
            previous_lineup.channels
          )
        return LineupRetrievalResult(
          lineup_id=parsed_id,
          lineup=previous_lineup,
          backend=chosen_provider.backend_name,
          from_cache=True,
          age_days=round(age_sec / 86400.0, 2),
          diff=cached_diff,
          snapshot_path=snap_path,
        )

      if (
        lineup_cache_fresh
        and iptv_org_needs_reapply
        and iptv_org_index is not None
      ):
        re_grouped = CableLineup.from_dict(
          previous_lineup.to_dict(),
          excluded_channels=config.lineup.excluded_channels,
        )
        country_hint = (
          "CAN"
          if parsed_id.country == "TVG" and "QC" in re_grouped.market.upper()
          else (
            parsed_id.country if parsed_id.country in {"CAN", "USA"} else "CAN"
          )
        )
        assign_dial_contiguous_groups(
          re_grouped.channels,
          iptv_org_index=iptv_org_index,
          market=re_grouped.market,
          country=country_hint,
        )
        populate_sibling_exclusions(re_grouped.channels)
        re_grouped.metadata["iptv_org_enriched"] = True
        re_grouped.metadata["iptv_org_station_count"] = (
          iptv_org_index.station_count
        )
        re_diff = compute_lineup_diff(previous_lineup, re_grouped)
        if not dry_run:
          payload = {
            "saved_timestamp": saved_ts,
            "retrieved_at": re_grouped.retrieved_at,
            "lineup_id": parsed_id.raw_id,
            "backend": chosen_provider.backend_name,
            "last_diff": re_diff.to_dict(),
            "lineup": re_grouped.to_dict(),
          }
          write_private_text(
            snap_path, json.dumps(payload, indent=2, ensure_ascii=False)
          )
        if config.lineup.exclude_audio:
          re_grouped.channels = _filter_excluded_audio(re_grouped.channels)
        return LineupRetrievalResult(
          lineup_id=parsed_id,
          lineup=re_grouped,
          backend=chosen_provider.backend_name,
          from_cache=False,
          age_days=round(age_sec / 86400.0, 2),
          diff=re_diff,
          snapshot_path=snap_path,
        )
    except (OSError, ValueError, KeyError, TypeError):
      previous_lineup = None

  try:
    fresh_lineup = await chosen_provider.retrieve_lineup(
      raw_id_input,
      postal_code=config.lineup.postal_code,
      excluded_channels=config.lineup.excluded_channels,
      iptv_org_index=iptv_org_index,
    )
  except LineupBackendError as exc:
    raise LineupRetrieverError(str(exc)) from exc

  diff = compute_lineup_diff(previous_lineup, fresh_lineup)

  if not dry_run:
    payload = {
      "saved_timestamp": time.time(),
      "retrieved_at": fresh_lineup.retrieved_at,
      "lineup_id": parsed_id.raw_id,
      "backend": chosen_provider.backend_name,
      "last_diff": diff.to_dict(),
      "lineup": fresh_lineup.to_dict(),
    }
    write_private_text(
      snap_path, json.dumps(payload, indent=2, ensure_ascii=False)
    )

  if config.lineup.exclude_audio:
    fresh_lineup.channels = _filter_excluded_audio(fresh_lineup.channels)

  return LineupRetrievalResult(
    lineup_id=parsed_id,
    lineup=fresh_lineup,
    backend=chosen_provider.backend_name,
    from_cache=False,
    age_days=0.0,
    diff=diff,
    snapshot_path=snap_path,
  )
