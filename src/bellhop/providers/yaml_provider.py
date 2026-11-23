"""Curated local YAML lineup provider implementing `LineupProvider`."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from bellhop.lineup_loader import (
  DEFAULT_BELL_FIBE_MTL_LINEUP_ID,
  CableLineup,
  load_lineup,
)

if TYPE_CHECKING:
  from bellhop.iptv_org import IptvOrgIndex
from bellhop.providers.base import (
  LineupBackendError,
  LineupProvider,
  ProviderHeadend,
)


class YamlLineupProvider(LineupProvider):
  """Loads cable lineups from a local curated YAML definition (e.g. Bell Fibe Montréal)."""

  backend_name = "yaml"

  SUPPORTED_DEFAULT_IDS = frozenset(
    {
      DEFAULT_BELL_FIBE_MTL_LINEUP_ID,
      "CAN-BELLFIBE-QC-X",
    }
  )

  def __init__(self, yaml_path: Path) -> None:
    self.yaml_path = yaml_path

  def supports(self, lineup_id: str, *, backend_hint: str = "auto") -> bool:
    hint = backend_hint.strip().lower()
    if hint == "yaml":
      return True
    if hint != "auto":
      return False

    cleaned = lineup_id.strip().upper()
    if cleaned.startswith("YAML:"):
      return True
    if cleaned in self.SUPPORTED_DEFAULT_IDS:
      return True
    return self.yaml_path.is_file() and cleaned == "CAN-0005993-X"

  async def discover_lineups(
    self,
    *,
    country: str = "CAN",
    postal_code: str = "",
    query: str = "",
  ) -> list[ProviderHeadend]:
    if not self.yaml_path.is_file():
      return []
    lu = load_lineup(self.yaml_path)
    headend = ProviderHeadend(
      backend=self.backend_name,
      lineup_id=lu.lineup_id,
      name=f"{lu.provider} — {lu.market} (Curated YAML)",
      provider_type="iptv/cable",
      location=lu.market,
      country=country.upper() or "CAN",
    )
    if (
      query
      and query.lower() not in f"{headend.name} {headend.lineup_id}".lower()
    ):
      return []
    return [headend]

  async def retrieve_lineup(
    self,
    lineup_id: str,
    *,
    postal_code: str = "",
    excluded_channels: set[int] | None = None,
    iptv_org_index: IptvOrgIndex | None = None,
  ) -> CableLineup:
    _ = iptv_org_index
    if not self.yaml_path.is_file():
      raise LineupBackendError(f"Lineup YAML file not found: {self.yaml_path}")
    raw_id = lineup_id.strip()
    if raw_id.upper().startswith("YAML:"):
      raw_id = raw_id.split(":", 1)[1].strip()
    lineup = load_lineup(self.yaml_path, excluded_channels=excluded_channels)
    if raw_id:
      lineup.lineup_id = raw_id.upper()
    if postal_code:
      lineup.postal_prefix = postal_code.strip()[:3].upper()
    lineup.retrieved_at = datetime.now(timezone.utc).isoformat()
    lineup.metadata["backend"] = self.backend_name
    lineup.metadata["source_file"] = str(self.yaml_path)
    return lineup
