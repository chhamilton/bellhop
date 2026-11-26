"""Abstract interface and shared types for cable/OTA lineup backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bellhop.pipeline.lineup_loader import CableLineup

if TYPE_CHECKING:
  from bellhop.pipeline.iptv_org import IptvOrgIndex


class LineupBackendError(RuntimeError):
  """Raised when a lineup backend fails auth, discovery, or retrieval."""


@dataclass(frozen=True, slots=True)
class ProviderHeadend:
  """Discoverable TV provider / headend lineup returned by a backend."""

  backend: str
  lineup_id: str
  name: str
  provider_type: str = "cable"
  location: str = ""
  country: str = ""

  def to_dict(self) -> dict[str, Any]:
    return {
      "backend": self.backend,
      "lineup_id": self.lineup_id,
      "name": self.name,
      "provider_type": self.provider_type,
      "location": self.location,
      "country": self.country,
    }


class LineupProvider(ABC):
  """Interface implemented by all cable/satellite/OTA lineup backends."""

  backend_name: str = "abstract"

  @abstractmethod
  def supports(self, lineup_id: str, *, backend_hint: str = "auto") -> bool:
    """Return True if this backend handles `lineup_id` under `backend_hint`."""

  @abstractmethod
  async def discover_lineups(
    self,
    *,
    country: str = "CAN",
    postal_code: str = "",
    query: str = "",
  ) -> list[ProviderHeadend]:
    """Search available headend lineups by country, postal code, or query."""

  @abstractmethod
  async def retrieve_lineup(
    self,
    lineup_id: str,
    *,
    postal_code: str = "",
    excluded_channels: set[int] | None = None,
    iptv_org_index: IptvOrgIndex | None = None,
  ) -> CableLineup:
    """Retrieve and normalize a full channel lineup for `lineup_id`."""
