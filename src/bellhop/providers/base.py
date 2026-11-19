"""Abstract interface and shared types for pluggable cable/OTA lineup backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from bellhop.lineup_loader import CableLineup


class LineupBackendError(RuntimeError):
  """Raised when a lineup backend fails authentication, discovery, or retrieval."""


@dataclass(frozen=True, slots=True)
class ProviderHeadend:
  """A discoverable TV service provider / headend lineup returned by a backend."""

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
  """Interface implemented by all cable/satellite/OTA lineup retrieval backends."""

  backend_name: str = "abstract"

  @abstractmethod
  def supports(self, lineup_id: str, *, backend_hint: str = "auto") -> bool:
    """Return True if this backend can resolve `lineup_id` under `backend_hint`."""

  @abstractmethod
  async def discover_lineups(
    self,
    *,
    country: str = "CAN",
    postal_code: str = "",
    query: str = "",
  ) -> list[ProviderHeadend]:
    """Search available provider headends/lineups for a country, ZIP/postal code, or name query."""

  @abstractmethod
  async def retrieve_lineup(
    self,
    lineup_id: str,
    *,
    postal_code: str = "",
    excluded_channels: set[int] | None = None,
  ) -> CableLineup:
    """Retrieve and normalize a full channel lineup for `lineup_id`."""
