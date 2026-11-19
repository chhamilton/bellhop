"""Load cable provider lineup definitions from YAML or JSON snapshots, enriched with official logos."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bellhop.models import LineupChannel

DEFAULT_BELL_FIBE_MTL_LINEUP_ID = "CAN-BELLFIBE-MTL-X"


def _load_official_logo_index(base_dir: Path | None = None) -> dict[int, str]:
  """Load verified official network logo URLs keyed by Bell Fibe channel number."""
  candidates: list[Path] = []
  if base_dir is not None:
    candidates.append(base_dir / "official_logos.json")
    candidates.append(base_dir.parent / "official_logos.json")
  pkg_data = (
    Path(__file__).resolve().parents[2] / "data" / "official_logos.json"
  )
  candidates.append(pkg_data)
  candidates.append(Path.cwd().resolve() / "data" / "official_logos.json")

  for path in candidates:
    if path.is_file():
      try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {int(k): str(v) for k, v in raw.items() if v}
      except (OSError, ValueError, TypeError):
        continue
  return {}


@dataclass(slots=True)
class CableLineup:
  provider: str
  market: str
  postal_prefix: str
  channels: list[LineupChannel]
  lineup_id: str = DEFAULT_BELL_FIBE_MTL_LINEUP_ID
  retrieved_at: str = ""
  metadata: dict[str, Any] = field(default_factory=dict)

  @property
  def total_dial_positions(self) -> int:
    """Total channel numbers on the Bell Fibe dial (counting both SD and HD positions)."""
    return sum(
      2 if (ch.sd_chno is not None and ch.sd_chno != ch.chno) else 1
      for ch in self.channels
    )

  def to_dict(self) -> dict[str, Any]:
    return {
      "lineup_id": self.lineup_id,
      "provider": self.provider,
      "market": self.market,
      "postal_prefix": self.postal_prefix,
      "retrieved_at": self.retrieved_at,
      "metadata": self.metadata,
      "channels": [ch.to_dict() for ch in self.channels],
    }

  @classmethod
  def from_dict(
    cls,
    data: dict[str, Any],
    excluded_channels: set[int] | None = None,
    official_logos: dict[int, str] | None = None,
  ) -> CableLineup:
    excluded = excluded_channels or set()
    logos = (
      official_logos
      if official_logos is not None
      else _load_official_logo_index()
    )
    channels: list[LineupChannel] = []
    for entry in data.get("channels", []):
      ch = LineupChannel.from_dict(entry)
      if ch.chno in excluded or (
        ch.sd_chno is not None and ch.sd_chno in excluded
      ):
        continue
      if not ch.logo and ch.chno in logos:
        ch.logo = logos[ch.chno]
      channels.append(ch)
    channels.sort(key=lambda c: c.chno)
    return cls(
      lineup_id=str(data.get("lineup_id") or DEFAULT_BELL_FIBE_MTL_LINEUP_ID),
      provider=str(data.get("provider", "Cable Provider")),
      market=str(data.get("market", "")),
      postal_prefix=str(data.get("postal_prefix", "")),
      retrieved_at=str(data.get("retrieved_at", "")),
      metadata=dict(data.get("metadata") or {}),
      channels=channels,
    )


def load_lineup(
  yaml_path: Path, excluded_channels: set[int] | None = None
) -> CableLineup:
  """Parse a YAML cable lineup definition into structured LineupChannel entries."""
  with yaml_path.open("r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}
  logos = _load_official_logo_index(yaml_path.parent)
  return CableLineup.from_dict(
    data, excluded_channels=excluded_channels, official_logos=logos
  )
