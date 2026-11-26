"""TV Guide (`backend.tvguide.com`) lineup provider supporting US & Canadian
headends.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiohttp

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

DEFAULT_TVGUIDE_BASE_URL = "https://backend.tvguide.com/tvschedules/tvguide"
TVGUIDE_LOGO_BASE_URL = "https://www.tvguide.com/a/img/catalog"
TVGUIDE_HEADERS = {
  "Referer": "https://www.tvguide.com/",
  "User-Agent": (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
  ),
  "Accept": "application/json",
}

TVGUIDE_ID_RE = re.compile(r"^(?:TVG-|TVGUIDE[:-])?(\d{6,14})$", re.IGNORECASE)
US_ZIP_RE = re.compile(r"^(\d{5})(?:-\d{4})?$")
CA_POSTAL_RE = re.compile(r"^([A-Z]\d[A-Z])\s*(\d[A-Z]\d)?$", re.IGNORECASE)

# Map Canadian FSA first letter to province code (& major metro hint for H/M)
CANADA_FSA_PROVINCE: dict[str, tuple[str, str]] = {
  "A": ("NL", ""),
  "B": ("NS", ""),
  "C": ("PE", ""),
  "E": ("NB", ""),
  "G": ("QC", ""),
  "H": ("QC", "montreal"),
  "J": ("QC", ""),
  "K": ("ON", ""),
  "L": ("ON", ""),
  "M": ("ON", "toronto"),
  "N": ("ON", ""),
  "P": ("ON", ""),
  "R": ("MB", "winnipeg"),
  "S": ("SK", ""),
  "T": ("AB", ""),
  "V": ("BC", ""),
  "X": ("NT", ""),
  "Y": ("YT", ""),
}


def parse_tvguide_provider_id(lineup_id: str) -> str | None:
  """Extract the numeric TV Guide `providerId` from `TVG-<id>` or `<id>`."""
  m = TVGUIDE_ID_RE.match(lineup_id.strip())
  return m.group(1) if m else None


def _parse_channel_number(raw_num: Any, fallback_seq: int) -> int:
  """Convert TVGuide channel number ('101', '1108', '2.1', '4-1', or None) into
  an int dial slot.
  """
  if raw_num is None:
    return fallback_seq
  s = str(raw_num).strip()
  if not s:
    return fallback_seq
  if s.isdigit():
    return int(s)
  # OTA subchannel like "2.1" or "17-1" -> encode major*10 + minor or major if
  # .1
  m_sub = re.match(r"^(\d+)[.-](\d+)$", s)
  if m_sub:
    major, minor = int(m_sub.group(1)), int(m_sub.group(2))
    return major if minor == 1 else (major * 100 + minor)
  digits = re.findall(r"\d+", s)
  return int(digits[0]) if digits else fallback_seq


class TvGuideLineupProvider(LineupProvider):
  """Free multi-provider US & Canadian lineup backend powered by
  `backend.tvguide.com`.
  """

  backend_name = "tvguide"

  def __init__(
    self,
    *,
    base_url: str = DEFAULT_TVGUIDE_BASE_URL,
    timeout_sec: float = 25.0,
    client: aiohttp.ClientSession | None = None,
  ) -> None:
    self.base_url = base_url.rstrip("/")
    self.timeout_sec = timeout_sec
    self._external_client = client

  def supports(self, lineup_id: str, *, backend_hint: str = "auto") -> bool:
    hint = backend_hint.strip().lower()
    if hint in {"tvguide", "tvg"}:
      return parse_tvguide_provider_id(lineup_id) is not None
    if hint != "auto":
      return False
    return parse_tvguide_provider_id(lineup_id) is not None

  async def _get_json(self, path: str) -> dict[str, Any]:
    url = f"{self.base_url}/{path.lstrip('/')}"
    owns = self._external_client is None
    client = self._external_client or aiohttp.ClientSession(
      headers=TVGUIDE_HEADERS,
      timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
    )
    try:
      async with client.get(url) as resp:
        if resp.status == 404:
          raise LineupBackendError(
            f"TV Guide resource not found (HTTP 404): {url}"
          )
        resp.raise_for_status()
        data = await resp.json(content_type=None)
      if not isinstance(data, dict):
        raise LineupBackendError(f"Unexpected non-object JSON from {url}")
      return data
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
      raise LineupBackendError(
        f"TV Guide API request failed ({url}): {exc}"
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
    postal_clean = postal_code.strip()
    country_up = (country or "CAN").strip().upper()
    if country_up == "CA":
      country_up = "CAN"
    elif country_up == "US":
      country_up = "USA"

    zip_match = US_ZIP_RE.match(postal_clean)
    ca_match = CA_POSTAL_RE.match(postal_clean)

    province_filter = ""
    city_hint = ""
    if zip_match:
      zip5 = zip_match.group(1)
      payload = await self._get_json(f"serviceproviders/zipcode/{zip5}/web")
      country_up = "USA"
    else:
      if ca_match:
        country_up = "CAN"
        fsa_letter = ca_match.group(1)[0].upper()
        province_filter, city_hint = CANADA_FSA_PROVINCE.get(
          fsa_letter, ("", "")
        )
      payload = await self._get_json(
        f"serviceproviders/country/{country_up}/web"
      )

    raw_items = (payload.get("data") or {}).get("items") or []
    q_low = query.strip().lower()

    results: list[ProviderHeadend] = []
    city_matched_results: list[ProviderHeadend] = []

    for item in raw_items:
      if not isinstance(item, dict) or item.get("id") is None:
        continue
      pid = str(item["id"])
      pname = str(item.get("name") or f"Provider {pid}").strip()
      city = str(item.get("city") or "").strip()
      state = str(item.get("state") or "").strip()
      ptype = str(item.get("type") or "cable").strip().lower()
      loc = ", ".join(p for p in (city, state) if p)

      if province_filter and state and state.upper() != province_filter:
        continue

      if q_low and q_low not in f"{pname} {loc} {pid} {ptype}".lower():
        continue

      headend = ProviderHeadend(
        backend=self.backend_name,
        lineup_id=f"TVG-{pid}",
        name=pname,
        provider_type=ptype,
        location=loc,
        country=country_up,
      )
      results.append(headend)
      if city_hint and city_hint in city.lower():
        city_matched_results.append(headend)

    # If user gave a known Canadian city FSA (e.g. M5V/H3B) without an explicit
    # city query, prioritize that metro!
    final_list = (
      city_matched_results if (city_matched_results and not q_low) else results
    )
    return final_list

  async def retrieve_lineup(
    self,
    lineup_id: str,
    *,
    postal_code: str = "",
    excluded_channels: set[int] | None = None,
    iptv_org_index: IptvOrgIndex | None = None,
  ) -> CableLineup:
    provider_id = parse_tvguide_provider_id(lineup_id)
    if not provider_id:
      raise LineupBackendError(
        f"Invalid TV Guide lineup ID {lineup_id!r}. Expected 'TVG-<providerId>' or numeric ID "
        f"(e.g. 'TVG-9100000001')."
      )

    meta_payload = await self._get_json(f"serviceprovider/{provider_id}/web")
    meta_item = (meta_payload.get("data") or {}).get("item") or {}
    sources_payload = await self._get_json(
      f"serviceprovider/{provider_id}/sources/web"
    )
    raw_sources = (sources_payload.get("data") or {}).get("items") or []

    if not raw_sources:
      raise LineupBackendError(
        f"TV Guide returned 0 channels for service provider {provider_id}."
      )

    provider_name = str(
      meta_item.get("name") or f"TVGuide Provider {provider_id}"
    ).strip()
    city = str(meta_item.get("city") or "").strip()
    state = str(meta_item.get("state") or "").strip()
    market = ", ".join(p for p in (city, state) if p) or "National"
    ca_provinces = {
      "QC",
      "ON",
      "BC",
      "AB",
      "MB",
      "SK",
      "NS",
      "NB",
      "PE",
      "NL",
      "NT",
      "YT",
      "NU",
    }
    country = "CAN" if state.upper() in ca_provinces else "USA"

    raw_slots: list[RawProviderSlot] = []
    for idx, item in enumerate(raw_sources, start=1):
      if not isinstance(item, dict):
        continue
      chno = _parse_channel_number(item.get("number"), fallback_seq=idx)
      full_name = str(
        item.get("fullName") or item.get("name") or f"Channel {chno}"
      ).strip()
      short_name = str(item.get("name") or "").strip() or None
      net_name = str(item.get("networkName") or "").strip() or None
      src_id = (
        str(item["sourceId"]) if item.get("sourceId") is not None else None
      )
      raw_logo = str(item.get("logo") or "").strip()
      if raw_logo.startswith("/"):
        logo_url: str | None = f"{TVGUIDE_LOGO_BASE_URL}{raw_logo}"
      elif raw_logo.startswith(("http://", "https://")):
        logo_url = raw_logo
      else:
        logo_url = None

      raw_slots.append(
        RawProviderSlot(
          chno=chno,
          raw_name=full_name,
          callsign=short_name,
          network_name=net_name,
          station_id=src_id,
          logo_url=logo_url,
          country=country,
        )
      )

    canonical_id = f"TVG-{provider_id}"
    channels = normalize_provider_slots(
      raw_slots,
      postal_code=postal_code,
      market=market,
      provider_name=provider_name,
      lineup_id=canonical_id,
      excluded_channels=excluded_channels,
      pair_sd_hd=True,
      iptv_org_index=iptv_org_index,
    )

    postal_prefix = (
      postal_code.strip()[:3].upper()
      if postal_code.strip()
      else (str(meta_item.get("zipcode") or "")[:5])
    )

    meta_dict: dict[str, Any] = {
      "backend": self.backend_name,
      "tvguide_provider_id": int(provider_id),
      "provider_type": meta_item.get("type", "cable"),
      "raw_source_count": len(raw_slots),
    }
    if iptv_org_index is not None and not iptv_org_index.is_empty:
      meta_dict["iptv_org_enriched"] = True
      meta_dict["iptv_org_station_count"] = iptv_org_index.station_count

    return CableLineup(
      lineup_id=canonical_id,
      provider=provider_name,
      market=market,
      postal_prefix=postal_prefix,
      retrieved_at=datetime.now(timezone.utc).isoformat(),
      metadata=meta_dict,
      channels=channels,
    )
