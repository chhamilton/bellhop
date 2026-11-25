"""Schedules Direct SD-JSON (`20141201`) lineup provider backed by Gracenote TMS."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from bellhop.epg.sd_client import (
  DEFAULT_SD_BASE_URL as DEFAULT_SD_BASE_URL,
)
from bellhop.epg.sd_client import (
  SD_USER_AGENT as SD_USER_AGENT,
)
from bellhop.epg.sd_client import (
  SchedulesDirectApiError,
  SchedulesDirectSession,
)
from bellhop.epg.sd_client import (
  clean_sd_lineup_id as clean_sd_lineup_id,
)
from bellhop.epg.sd_client import (
  hash_sd_password as hash_sd_password,
)
from bellhop.pipeline.lineup_loader import CableLineup

if TYPE_CHECKING:
  from bellhop.pipeline.iptv_org import IptvOrgIndex
from bellhop.pipeline.providers.base import (
  LineupBackendError,
  LineupProvider,
  ProviderHeadend,
)
from bellhop.pipeline.providers.normalizer import (
  RawProviderSlot,
  normalize_provider_slots,
)


@dataclass(slots=True)
class SdStationInfo:
  """Normalized station metadata parsed from an SD-JSON `stations[]` entry."""

  station_id: str
  name: str
  callsign: str | None = None
  affiliate: str | None = None
  primary_language: str | None = None
  logo_url: str | None = None
  is_hd_hint: bool | None = None

  @classmethod
  def from_raw(
    cls, raw: Mapping[str, Any], *, fallback_id: str = ""
  ) -> SdStationInfo:
    sid = str(raw.get("stationID") or fallback_id).strip()
    name = str(
      raw.get("name") or raw.get("callsign") or f"Station {sid}"
    ).strip()
    callsign = str(raw.get("callsign") or "").strip() or None
    affiliate = str(raw.get("affiliate") or "").strip() or None
    langs = raw.get("broadcastLanguage")
    primary_lang = str(langs[0]) if isinstance(langs, list) and langs else None

    logo_url: str | None = None
    station_logos = raw.get("stationLogo")
    if isinstance(station_logos, list) and station_logos:
      first_logo = station_logos[0]
      if isinstance(first_logo, Mapping) and first_logo.get("URL"):
        logo_url = str(first_logo["URL"])
    raw_logo = raw.get("logo")
    if not logo_url and isinstance(raw_logo, Mapping) and raw_logo.get("URL"):
      logo_url = str(raw_logo["URL"])

    is_hd_hint = bool(raw["isHD"]) if "isHD" in raw else None
    return cls(
      station_id=sid,
      name=name,
      callsign=callsign,
      affiliate=affiliate,
      primary_language=primary_lang,
      logo_url=logo_url,
      is_hd_hint=is_hd_hint,
    )


class SchedulesDirectLineupProvider(LineupProvider):
  """Official Schedules Direct SD-JSON API lineup backend (`json.schedulesdirect.org/20141201`)."""

  backend_name = "schedules_direct"

  def __init__(
    self,
    *,
    username: str = "",
    password: str = "",
    base_url: str = DEFAULT_SD_BASE_URL,
    token_cache_path: Path | None = None,
    auto_add_lineup: bool = False,
    timeout_sec: float = 25.0,
    client: aiohttp.ClientSession | None = None,
  ) -> None:
    self.username = username.strip()
    self.password = password.strip()
    self.base_url = base_url.rstrip("/")
    self.token_cache_path = token_cache_path
    self.auto_add_lineup = auto_add_lineup
    self.timeout_sec = timeout_sec
    self._external_client = client
    self._sd = SchedulesDirectSession(
      username=self.username,
      password=self.password,
      base_url=self.base_url,
      token_cache_path=self.token_cache_path,
    )

  @property
  def has_credentials(self) -> bool:
    return self._sd.has_credentials

  def supports(self, lineup_id: str, *, backend_hint: str = "auto") -> bool:
    hint = backend_hint.strip().lower().replace("-", "_")
    if hint in {"schedules_direct", "sd"}:
      return clean_sd_lineup_id(lineup_id) is not None
    if hint != "auto":
      return False

    upper = lineup_id.strip().upper()
    if upper.startswith("SD:"):
      return True
    if not self.has_credentials:
      return False
    return clean_sd_lineup_id(lineup_id) is not None

  async def _request(
    self,
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
  ) -> Any:
    owns = self._external_client is None
    client = self._external_client or aiohttp.ClientSession(
      headers={"User-Agent": SD_USER_AGENT, "Accept": "application/json"},
      timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
    )
    try:
      return await self._sd.request_json(client, method, path, params=params)
    except SchedulesDirectApiError as exc:
      raise LineupBackendError(str(exc)) from exc
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
      raise LineupBackendError(
        f"Schedules Direct HTTP error ({path}): {exc}"
      ) from exc
    finally:
      if owns:
        await client.close()

  async def discover_lineups(
    self,
    *,
    country: str = "CAN",
    postal_code: str = "",
    query: str = "",
  ) -> list[ProviderHeadend]:
    country_iso = (country or "CAN").strip().upper()
    if country_iso == "CA":
      country_iso = "CAN"
    elif country_iso == "US":
      country_iso = "USA"

    # Schedules Direct expects Canadian postal codes without spaces (e.g. "H2Y1C6")
    clean_pc = re.sub(r"\s+", "", postal_code.strip().upper())
    if not clean_pc:
      raise LineupBackendError(
        "Schedules Direct headend discovery requires `--postal-code` (e.g. 'H2Y1C6' or '10001')."
      )

    raw_headends = await self._request(
      "GET",
      "headends",
      params={"country": country_iso, "postalcode": clean_pc},
    )
    if not isinstance(raw_headends, list):
      return []

    q_low = query.strip().lower()
    results: list[ProviderHeadend] = []
    for he in raw_headends:
      if not isinstance(he, dict):
        continue
      transport = str(he.get("transport") or "cable").strip().lower()
      location = str(he.get("location") or "").strip()
      for lu in he.get("lineups") or []:
        if not isinstance(lu, dict) or not lu.get("lineup"):
          continue
        lid = str(lu["lineup"]).strip()
        lname = str(lu.get("name") or lid).strip()
        if (
          q_low and q_low not in f"{lname} {lid} {location} {transport}".lower()
        ):
          continue
        results.append(
          ProviderHeadend(
            backend=self.backend_name,
            lineup_id=lid,
            name=lname,
            provider_type=transport,
            location=location,
            country=country_iso,
          )
        )
    return results

  async def retrieve_lineup(
    self,
    lineup_id: str,
    *,
    postal_code: str = "",
    excluded_channels: set[int] | None = None,
    iptv_org_index: IptvOrgIndex | None = None,
  ) -> CableLineup:
    canonical_id = clean_sd_lineup_id(lineup_id)
    if not canonical_id:
      raise LineupBackendError(
        f"Invalid Schedules Direct Gracenote lineup ID {lineup_id!r} "
        f"(expected e.g. 'CAN-0005993-X' or 'USA-NY12345-X')."
      )

    try:
      payload = await self._request("GET", f"lineups/{canonical_id}")
    except LineupBackendError as exc:
      # Code 2100 / not in account lineups: optionally subscribe via PUT /lineups/{id}
      if self.auto_add_lineup and (
        "2100" in str(exc) or "not in" in str(exc).lower()
      ):
        await self._request("PUT", f"lineups/{canonical_id}")
        payload = await self._request("GET", f"lineups/{canonical_id}")
      else:
        raise

    if not isinstance(payload, dict):
      raise LineupBackendError(
        f"Unexpected Schedules Direct payload for {canonical_id}"
      )

    stations_by_id: dict[str, SdStationInfo] = {}
    for st_raw in payload.get("stations") or []:
      if isinstance(st_raw, dict) and st_raw.get("stationID"):
        parsed_st = SdStationInfo.from_raw(st_raw)
        stations_by_id[parsed_st.station_id] = parsed_st

    channel_map = payload.get("map") or []
    meta_raw = payload.get("metadata")
    meta: Mapping[str, Any] = meta_raw if isinstance(meta_raw, Mapping) else {}
    country_prefix = canonical_id.split("-", 1)[0].upper()

    raw_slots: list[RawProviderSlot] = []
    for idx, entry in enumerate(channel_map, start=1):
      if not isinstance(entry, dict):
        continue
      sid = str(entry.get("stationID") or "").strip()
      st = stations_by_id.get(sid) or SdStationInfo(
        station_id=sid,
        name=f"Station {sid}",
      )

      raw_ch = (
        entry.get("channel") or entry.get("atscMajor") or entry.get("uhfVhf")
      )
      chno_str = str(raw_ch or "").strip()
      if chno_str.isdigit():
        chno = int(chno_str)
      else:
        digits = re.findall(r"\d+", chno_str)
        chno = int(digits[0]) if digits else idx

      raw_slots.append(
        RawProviderSlot(
          chno=chno,
          raw_name=st.name,
          callsign=st.callsign,
          network_name=st.affiliate,
          station_id=sid or None,
          logo_url=st.logo_url,
          language=st.primary_language,
          country=country_prefix,
          is_hd_hint=st.is_hd_hint,
        )
      )

    provider_label = str(meta.get("lineup") or canonical_id).strip()
    transport = str(meta.get("transport") or "Cable").strip()
    sd_location = str(meta.get("location") or "").strip()
    if provider_label.upper() == canonical_id or not sd_location:
      try:
        acct_lus = await self._request("GET", "lineups")
        if isinstance(acct_lus, Mapping):
          for item in acct_lus.get("lineups") or []:
            if (
              isinstance(item, Mapping)
              and str(item.get("lineup") or "").strip().upper() == canonical_id
            ):
              if provider_label.upper() == canonical_id and item.get("name"):
                provider_label = str(item["name"]).strip()
              if not sd_location and item.get("location"):
                sd_location = str(item["location"]).strip()
              break
      except LineupBackendError:
        pass

    channels = normalize_provider_slots(
      raw_slots,
      postal_code=postal_code,
      market=sd_location,
      provider_name=provider_label,
      lineup_id=canonical_id,
      excluded_channels=excluded_channels,
      pair_sd_hd=True,
      iptv_org_index=iptv_org_index,
    )

    postal_prefix = (
      postal_code.strip()[:3].upper() if postal_code.strip() else ""
    )
    market_label = (
      f"{sd_location} ({transport})"
      if sd_location
      else f"{country_prefix} ({transport})"
    )

    meta_dict: dict[str, Any] = {
      "backend": self.backend_name,
      "transport": transport,
      "modified": meta.get("modified", ""),
      "raw_station_count": len(stations_by_id),
    }
    if iptv_org_index is not None and not iptv_org_index.is_empty:
      meta_dict["iptv_org_enriched"] = True
      meta_dict["iptv_org_station_count"] = iptv_org_index.station_count

    return CableLineup(
      lineup_id=canonical_id,
      provider=provider_label,
      market=market_label,
      postal_prefix=postal_prefix,
      retrieved_at=datetime.now(timezone.utc).isoformat(),
      metadata=meta_dict,
      channels=channels,
    )
