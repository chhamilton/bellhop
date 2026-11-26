"""Typed configuration dataclasses for Bellhop's two-tier settings model."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bellhop.common.http_utils import DEFAULT_TRUSTED_PROXY_IPS

DEFAULT_BELL_FIBE_MTL_LINEUP_ID = "CAN-BELLFIBE-MTL-X"
DEFAULT_SD_BASE_URL = "https://json.schedulesdirect.org/20141201"
DEFAULT_TVGUIDE_BASE_URL = "https://backend.tvguide.com/tvschedules/tvguide"
DEFAULT_IPTV_ORG_API_URL = "https://iptv-org.github.io/api"


def _mask(secret: str, redact: bool) -> str:
  return "***" if (redact and secret) else secret


@dataclass(slots=True)
class XtreamConfig:
  """Upstream Xtream Codes IPTV account & catalog cache settings."""

  server_url: str = ""
  username: str = ""
  password: str = ""
  stream_format: str = "ts"
  request_timeout_sec: float = 90.0
  cache_dir: Path = Path("cache")
  cache_ttl_hours: float = 24.0
  seed_json: Path | None = None


@dataclass(slots=True)
class ValidationConfig:
  """Phase (4) live stream HTTP/MPEG-TS/HLS/ffprobe validation settings."""

  enabled: bool = True
  only_changed: bool = True
  concurrency: int = 2
  ignore_account_limit: bool = False
  inter_probe_delay_sec: float = 0.35
  http_timeout_sec: float = 8.0
  min_bytes_to_read: int = 8192
  use_ffprobe: bool = True
  ffprobe_timeout_sec: float = 12.0
  detect_black_screen: bool = True
  black_sample_sec: float = 1.5
  check_audio_language: bool = True
  max_candidates_per_channel: int = 5


@dataclass(slots=True)
class SchedulesDirectConfig:
  """Schedules Direct JSON API credentials and lineup auto-add toggle."""

  username: str = ""
  password: str = ""
  auto_add_lineup: bool = False


@dataclass(slots=True)
class IptvOrgConfig:
  """Phase (1) `iptv-org` channel/feed metadata & HTTP ETag cache settings."""

  enabled: bool = True
  recheck_hours: float = 24.0


@dataclass(slots=True)
class LineupConfig:
  """Phase (1) official cable/fiber lineup retrieval and numbering settings."""

  backend: str = "auto"
  lineup_id: str = DEFAULT_BELL_FIBE_MTL_LINEUP_ID
  postal_code: str = ""
  refresh_days: float = 30.0
  file: Path = Path("data/lineups/bell_fibe_montreal.yaml")
  numbering_mode: str = "both"  # "both", "hd", or "sd"
  collapse_sd_dupes: bool = False
  exclude_audio: bool = False
  pinned_streams: dict[int, int] = field(default_factory=dict)
  excluded_channels: set[int] = field(default_factory=set)
  schedules_direct: SchedulesDirectConfig = field(
    default_factory=SchedulesDirectConfig
  )
  iptv_org: IptvOrgConfig = field(default_factory=IptvOrgConfig)


@dataclass(slots=True)
class StateConfig:
  """Phase (3) persistent incremental mapping state & hysteresis settings."""

  state_dir: Path = Path("state")
  mapping_path: Path = Path("state/channel_mapping.json")
  upgrade_hysteresis_score: float = 5.0


@dataclass(slots=True)
class EpgConfig:
  """Passthrough XMLTV EPG verification and Web Guide refresh cadence."""

  verify: bool = True
  refresh_hours: float = 6.0
  xmltv_file: Path | None = None


@dataclass(slots=True)
class OutputConfig:
  """Phase (5) M3U8 and Phase (6) Xtream export output settings."""

  m3u8_enabled: bool = True
  playlist_path: Path = Path("output/bellhop.m3u8")
  report_path: Path = Path("output/lineup_report.json")
  xtream_enabled: bool = True
  xtream_dir: Path = Path("output/xtream")
  include_unmatched_comment: bool = False


@dataclass(slots=True)
class ServeWebConfig:
  """Tier-1 Web TV Guide server, cookie session, and reverse-proxy settings."""

  enabled: bool = True
  host: str | None = None
  port: int | None = None
  settle_ms: int = 180
  username: str = ""
  password: str = ""
  session_ttl_hours: float = 168.0
  session_secret: str = ""
  trust_proxy_headers: bool = False
  trusted_proxy_ips: list[str] = field(
    default_factory=lambda: list(DEFAULT_TRUSTED_PROXY_IPS)
  )


@dataclass(slots=True)
class ServeXtreamConfig:
  """Passthrough Xtream Codes API listener and public URL settings."""

  enabled: bool = True
  host: str | None = None
  port: int | None = None
  username: str = ""
  password: str = ""
  public_host: str | None = None
  public_port: int | None = None
  server_protocol: str = "http"


@dataclass(slots=True)
class ServeConfig:
  """Unified `bellhop serve` bind and co-hosted service settings."""

  host: str = "0.0.0.0"
  port: int = 8080
  web: ServeWebConfig = field(default_factory=ServeWebConfig)
  xtream: ServeXtreamConfig = field(default_factory=ServeXtreamConfig)

  @property
  def web_host(self) -> str:
    return self.web.host or self.host

  @property
  def web_port(self) -> int:
    return self.port if self.web.port is None else self.web.port

  @property
  def xtream_host(self) -> str:
    return self.xtream.host or self.host

  @property
  def xtream_port(self) -> int:
    return self.port if self.xtream.port is None else self.xtream.port

  @property
  def settle_ms(self) -> int:
    return self.web.settle_ms

  @property
  def username(self) -> str:
    return self.xtream.username

  @property
  def password(self) -> str:
    return self.xtream.password

  @property
  def public_host(self) -> str | None:
    return self.xtream.public_host

  @property
  def public_port(self) -> int | None:
    return self.xtream.public_port

  @property
  def server_protocol(self) -> str:
    return self.xtream.server_protocol

  @property
  def trust_proxy_headers(self) -> bool:
    return self.web.trust_proxy_headers

  @property
  def trusted_proxy_ips(self) -> list[str]:
    return self.web.trusted_proxy_ips


@dataclass(slots=True)
class AppConfig:
  """Complete validated application configuration for `bellhop`."""

  project_root: Path = field(default_factory=Path.cwd)
  config_file: Path | None = None
  credentials_file: Path = Path("credentials.yaml")
  cache_dir: Path = Path("cache")
  xtream: XtreamConfig = field(default_factory=XtreamConfig)
  validation: ValidationConfig = field(default_factory=ValidationConfig)
  lineup: LineupConfig = field(default_factory=LineupConfig)
  state: StateConfig = field(default_factory=StateConfig)
  epg: EpgConfig = field(default_factory=EpgConfig)
  output: OutputConfig = field(default_factory=OutputConfig)
  serve: ServeConfig = field(default_factory=ServeConfig)

  def __post_init__(self) -> None:
    if self.cache_dir == Path("cache") and self.xtream.cache_dir != Path(
      "cache"
    ):
      self.cache_dir = self.xtream.cache_dir
    else:
      self.xtream.cache_dir = self.cache_dir

  def resolve_path(self, path: Path | str) -> Path:
    """Resolve `path` relative to `project_root` when not already absolute."""
    p = Path(path)
    return p if p.is_absolute() else (self.project_root / p).resolve()

  def to_dict(self, *, redact_secrets: bool = True) -> dict[str, Any]:
    """Serialize full configuration for diagnostics, redacting secrets by
    default.
    """
    return {
      "credentials_file": str(self.credentials_file),
      "xtream": {
        "server_url": self.xtream.server_url,
        "username": self.xtream.username,
        "password": _mask(self.xtream.password, redact_secrets),
        "stream_format": self.xtream.stream_format,
        "request_timeout_sec": self.xtream.request_timeout_sec,
        "refresh_hours": self.xtream.cache_ttl_hours,
        "cache_dir": str(self.cache_dir),
        "seed_json": str(self.xtream.seed_json)
        if self.xtream.seed_json
        else None,
      },
      "lineup": {
        "backend": self.lineup.backend,
        "lineup_id": self.lineup.lineup_id,
        "postal_code": self.lineup.postal_code,
        "refresh_days": self.lineup.refresh_days,
        "file": str(self.lineup.file),
        "numbering_mode": self.lineup.numbering_mode,
        "collapse_sd_dupes": self.lineup.collapse_sd_dupes,
        "exclude_audio": self.lineup.exclude_audio,
        "pinned_streams": self.lineup.pinned_streams,
        "excluded_channels": sorted(self.lineup.excluded_channels),
        "schedules_direct": {
          "username": self.lineup.schedules_direct.username,
          "password": _mask(
            self.lineup.schedules_direct.password, redact_secrets
          ),
          "auto_add_lineup": self.lineup.schedules_direct.auto_add_lineup,
        },
        "iptv_org": {
          "enabled": self.lineup.iptv_org.enabled,
          "recheck_hours": self.lineup.iptv_org.recheck_hours,
        },
      },
      "state": {
        "state_dir": str(self.state.state_dir),
        "mapping_path": str(self.state.mapping_path),
        "upgrade_hysteresis_score": self.state.upgrade_hysteresis_score,
      },
      "validation": {
        "enabled": self.validation.enabled,
        "only_changed": self.validation.only_changed,
        "concurrency": self.validation.concurrency,
        "ignore_account_limit": self.validation.ignore_account_limit,
        "inter_probe_delay_sec": self.validation.inter_probe_delay_sec,
        "http_timeout_sec": self.validation.http_timeout_sec,
        "min_bytes_to_read": self.validation.min_bytes_to_read,
        "use_ffprobe": self.validation.use_ffprobe,
        "ffprobe_timeout_sec": self.validation.ffprobe_timeout_sec,
        "detect_black_screen": self.validation.detect_black_screen,
        "black_sample_sec": self.validation.black_sample_sec,
        "check_audio_language": self.validation.check_audio_language,
        "max_candidates_per_channel": self.validation.max_candidates_per_channel,
      },
      "epg": {
        "verify": self.epg.verify,
        "refresh_hours": self.epg.refresh_hours,
        "xmltv_file": str(self.epg.xmltv_file) if self.epg.xmltv_file else None,
      },
      "output": {
        "m3u8_enabled": self.output.m3u8_enabled,
        "playlist_path": str(self.output.playlist_path),
        "report_path": str(self.output.report_path),
        "xtream_enabled": self.output.xtream_enabled,
        "xtream_dir": str(self.output.xtream_dir),
        "include_unmatched_comment": self.output.include_unmatched_comment,
      },
      "serve": {
        "host": self.serve.host,
        "port": self.serve.port,
        "web": {
          "enabled": self.serve.web.enabled,
          "host": self.serve.web.host,
          "port": self.serve.web.port,
          "settle_ms": self.serve.web.settle_ms,
          "username": self.serve.web.username,
          "password": _mask(self.serve.web.password, redact_secrets),
          "session_ttl_hours": self.serve.web.session_ttl_hours,
          "session_secret": _mask(
            self.serve.web.session_secret, redact_secrets
          ),
          "trust_proxy_headers": self.serve.web.trust_proxy_headers,
          "trusted_proxy_ips": list(self.serve.web.trusted_proxy_ips),
        },
        "xtream": {
          "enabled": self.serve.xtream.enabled,
          "host": self.serve.xtream.host,
          "port": self.serve.xtream.port,
          "username": self.serve.xtream.username,
          "password": _mask(self.serve.xtream.password, redact_secrets),
          "public_host": self.serve.xtream.public_host,
          "public_port": self.serve.xtream.public_port,
          "server_protocol": self.serve.xtream.server_protocol,
        },
      },
    }
