"""Core typed domain models for Xtream streams, cable lineup slots, matching, and stream probes.

Serialized JSON Layouts
-----------------------
1. `LineupChannelDict` (entries inside `state/lineups/<lineup_id>.json` -> `lineup.channels[]`):
   ```json
   {
     "chno": 1108,
     "sd_chno": 108,
     "name": "RDS",
     "group": "Sports",
     "language": "fr",
     "callsign": "RDS",
     "tvg_id": "RDS.ca",
     "gracenote_station_id": "14873",
     "logo": "https://.../rds-ca.png",
     "regions": ["qc", "ca"],
     "aliases": ["RDS", "Reseau des sports"],
     "exclude": ["RDS 2", "RDS Info"]
   }
   ```
2. `ProbeResultDict` (stored under `probe` on `CandidateRecord` in `state/channel_mapping.json`):
   ```json
   {
     "stream_id": 410108,
     "url": "http://provider/live/user/pass/410108.ts",
     "ok": true,
     "http_status": 200,
     "content_type": "video/mp2t",
     "bytes_read": 8192,
     "latency_ms": 385.2,
     "is_hls": false,
     "ts_sync_verified": true,
     "ffprobe_checked": true,
     "video_codec": "h264",
     "audio_codec": "aac",
     "width": 1920,
     "height": 1080,
     "fps": 59.94,
     "error": null,
     "checked_at": "2026-11-18T04:12:00+00:00"
   }
   ```
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from bellhop.xtream.client import (
  XtreamAccountInfo as XtreamAccountInfo,
)
from bellhop.xtream.client import (
  XtreamStream as XtreamStream,
)

QualityTier = Literal["4k", "fhd", "hd", "sd", "unknown"]
ValidationStatus = Literal["unvalidated", "valid", "invalid"]
PreferredSource = Literal["pinned", "validated", "rough_match", "none"]


class LineupChannelDict(TypedDict):
  """Serialized dictionary representation of a `LineupChannel`."""

  chno: int
  sd_chno: int | None
  name: str
  group: str
  language: str
  callsign: str | None
  tvg_id: str | None
  gracenote_station_id: str | None
  logo: str | None
  regions: list[str]
  aliases: list[str]
  exclude: list[str]


class ProbeResultDict(TypedDict):
  """Serialized dictionary representation of a `ProbeResult`."""

  stream_id: int
  url: str
  ok: bool
  http_status: int | None
  content_type: str | None
  bytes_read: int
  latency_ms: float
  is_hls: bool
  ts_sync_verified: bool
  ffprobe_checked: bool
  video_codec: str | None
  audio_codec: str | None
  audio_languages: list[str]
  audio_language_mismatch: bool
  black_screen: bool
  width: int | None
  height: int | None
  fps: float | None
  error: str | None
  checked_at: str


def _opt_int(val: object) -> int | None:
  if val is None or isinstance(val, bool):
    return None
  if isinstance(val, (int, float, str)):
    try:
      return int(val)
    except ValueError:
      return None
  return None


def _opt_float(val: object) -> float | None:
  if val is None or isinstance(val, bool):
    return None
  if isinstance(val, (int, float, str)):
    try:
      return float(val)
    except ValueError:
      return None
  return None


@dataclass(slots=True)
class LineupChannel:
  """A target slot in the emulated cable provider lineup (e.g. Bell Fibe Montréal)."""

  chno: int
  name: str
  group: str
  language: str = "en"
  sd_chno: int | None = None
  callsign: str | None = None
  tvg_id: str | None = None
  gracenote_station_id: str | None = None
  logo: str | None = None
  regions: list[str] = field(default_factory=lambda: ["ca"])
  aliases: list[str] = field(default_factory=list)
  exclude: list[str] = field(default_factory=list)

  def to_dict(self) -> LineupChannelDict:
    return {
      "chno": self.chno,
      "sd_chno": self.sd_chno,
      "name": self.name,
      "group": self.group,
      "language": self.language,
      "callsign": self.callsign,
      "tvg_id": self.tvg_id,
      "gracenote_station_id": self.gracenote_station_id,
      "logo": self.logo,
      "regions": list(self.regions),
      "aliases": list(self.aliases),
      "exclude": list(self.exclude),
    }

  @classmethod
  def from_dict(cls, entry: Mapping[str, Any]) -> LineupChannel:
    return cls(
      chno=int(entry["chno"]),
      sd_chno=int(entry["sd_chno"])
      if entry.get("sd_chno") is not None
      else None,
      name=str(entry["name"]),
      group=str(entry.get("group", "General")),
      language=str(entry.get("language", "en")).lower(),
      callsign=str(entry["callsign"]).upper()
      if entry.get("callsign")
      else None,
      tvg_id=str(entry["tvg_id"]) if entry.get("tvg_id") else None,
      gracenote_station_id=(
        str(entry["gracenote_station_id"])
        if entry.get("gracenote_station_id")
        else None
      ),
      logo=str(entry["logo"]).strip() if entry.get("logo") else None,
      regions=[str(r).lower() for r in entry.get("regions", ["ca"])],
      aliases=[str(a) for a in entry.get("aliases", [entry["name"]])],
      exclude=[str(x) for x in entry.get("exclude", [])],
    )


@dataclass(slots=True)
class MatchCandidate:
  """A scored candidate Xtream stream for a target `LineupChannel`."""

  stream: XtreamStream
  score: float
  reasons: list[str] = field(default_factory=list)
  quality_tier: str = "unknown"
  has_native_epg: bool = False


@dataclass(slots=True)
class ProbeResult:
  """Validation telemetry for a single Xtream stream."""

  stream_id: int
  url: str
  ok: bool
  http_status: int | None = None
  content_type: str | None = None
  bytes_read: int = 0
  latency_ms: float = 0.0
  is_hls: bool = False
  ts_sync_verified: bool = False
  ffprobe_checked: bool = False
  video_codec: str | None = None
  audio_codec: str | None = None
  audio_languages: list[str] = field(default_factory=list)
  audio_language_mismatch: bool = False
  black_screen: bool = False
  width: int | None = None
  height: int | None = None
  fps: float | None = None
  error: str | None = None
  checked_at: str = ""

  @property
  def resolution_label(self) -> str:
    if self.height:
      if self.height >= 2160:
        return "4K"
      if self.height >= 1080:
        return "1080p"
      if self.height >= 720:
        return "720p"
      return f"{self.height}p"
    return ""

  def to_dict(self, *, redact_secret: str = "") -> ProbeResultDict:
    safe_url = (
      self.url.replace(redact_secret, "***")
      if (redact_secret and self.url)
      else self.url
    )
    return {
      "stream_id": self.stream_id,
      "url": safe_url,
      "ok": self.ok,
      "http_status": self.http_status,
      "content_type": self.content_type,
      "bytes_read": self.bytes_read,
      "latency_ms": round(self.latency_ms, 1),
      "is_hls": self.is_hls,
      "ts_sync_verified": self.ts_sync_verified,
      "ffprobe_checked": self.ffprobe_checked,
      "video_codec": self.video_codec,
      "audio_codec": self.audio_codec,
      "audio_languages": list(self.audio_languages),
      "audio_language_mismatch": self.audio_language_mismatch,
      "black_screen": self.black_screen,
      "width": self.width,
      "height": self.height,
      "fps": self.fps,
      "error": self.error,
      "checked_at": self.checked_at,
    }

  @classmethod
  def from_dict(cls, data: Mapping[str, Any]) -> ProbeResult:
    return cls(
      stream_id=int(data["stream_id"]),
      url=str(data.get("url", "")),
      ok=bool(data.get("ok", False)),
      http_status=_opt_int(data.get("http_status")),
      content_type=str(data["content_type"])
      if data.get("content_type") is not None
      else None,
      bytes_read=int(data.get("bytes_read", 0)),
      latency_ms=float(data.get("latency_ms", 0.0)),
      is_hls=bool(data.get("is_hls", False)),
      ts_sync_verified=bool(data.get("ts_sync_verified", False)),
      ffprobe_checked=bool(data.get("ffprobe_checked", False)),
      video_codec=str(data["video_codec"])
      if data.get("video_codec") is not None
      else None,
      audio_codec=str(data["audio_codec"])
      if data.get("audio_codec") is not None
      else None,
      audio_languages=[
        str(lg).lower()
        for lg in (data.get("audio_languages") or [])
        if str(lg).strip()
      ],
      audio_language_mismatch=bool(data.get("audio_language_mismatch", False)),
      black_screen=bool(data.get("black_screen", False)),
      width=_opt_int(data.get("width")),
      height=_opt_int(data.get("height")),
      fps=_opt_float(data.get("fps")),
      error=str(data["error"]) if data.get("error") is not None else None,
      checked_at=str(data.get("checked_at", "")),
    )


@dataclass(slots=True)
class MappedChannel:
  """Final mapping decision for a target lineup slot."""

  channel: LineupChannel
  selected: MatchCandidate | None = None
  probe: ProbeResult | None = None
  candidates: list[MatchCandidate] = field(default_factory=list)
  probes_attempted: int = 0
