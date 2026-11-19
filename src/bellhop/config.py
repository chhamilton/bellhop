"""Declarative YAML configuration loader (`config.yaml` + `credentials.yaml` + `.env`).

Two-Tier Configuration Layout (`config.yaml`)
---------------------------------------------
Tier 1 — Host Infrastructure & Filesystem Paths (config-file only, NEVER exposed to `/api/config`):
```yaml
credentials_file: "credentials.yaml"
paths:
  cache_dir: "cache"
  state_dir: "state"
  mapping_path: "state/channel_mapping.json"
  lineup_file: "data/lineups/bell_fibe_montreal.yaml"
  m3u8_playlist_path: "output/bellhop.m3u8"
  m3u8_report_path: "output/lineup_report.json"
  xtream_output_dir: "output/xtream"
  seed_json: null
  xmltv_file: null
web_server:
  enabled: true
  host: "0.0.0.0"
  port: 8080
  settle_ms: 180
  session_ttl_hours: 168.0
  username: ""
  password: ""
```

Tier 2 — Operational Settings (exposed via `PublicConfigPayload` in `GET/PUT /api/config`,
strictly excluding all usernames, passwords, and secrets):
```yaml
upstream_xtream:
  server_url: ""
  stream_format: "ts"          # "ts" | "m3u8"
  request_timeout_sec: 90.0
  refresh_hours: 24.0
xtream_server:
  enabled: true
  host: null                   # null shares web_server.host
  port: null                   # null shares web_server.port
  public_host: null
  public_port: null
  server_protocol: "http"      # "http" | "https"
pipeline:
  lineup: { ... }
  matching: { upgrade_hysteresis_score: 5.0 }
  validation: { ... }
  epg: { verify: true, refresh_hours: 6.0 }
  output: { m3u8_enabled: true, xtream_enabled: true, include_unmatched_comment: false }
```
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, TypedDict

import yaml
from dotenv import load_dotenv
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import (
  DoubleQuotedScalarString,
  SingleQuotedScalarString,
)

from bellhop.lineup_loader import DEFAULT_BELL_FIBE_MTL_LINEUP_ID
from bellhop_web.http_utils import (
  DEFAULT_TRUSTED_PROXY_IPS,
  parse_trusted_proxy_networks,
)


class PublicUpstreamXtreamDict(TypedDict):
  server_url: str
  stream_format: str
  request_timeout_sec: float
  refresh_hours: float


class PublicXtreamServerDict(TypedDict):
  enabled: bool
  host: str | None
  port: int | None
  public_host: str | None
  public_port: int | None
  server_protocol: str


class PublicTvGuideDict(TypedDict):
  base_url: str


class PublicSchedulesDirectDict(TypedDict):
  base_url: str
  auto_add_lineup: bool


class PublicLineupDict(TypedDict):
  backend: str
  lineup_id: str
  postal_code: str
  refresh_days: float
  numbering_mode: str
  collapse_sd_dupes: bool
  exclude_audio: bool
  pinned_streams: dict[str, int]
  excluded_channels: list[int]
  tvguide: PublicTvGuideDict
  schedules_direct: PublicSchedulesDirectDict


class PublicMatchingDict(TypedDict):
  upgrade_hysteresis_score: float


class PublicValidationDict(TypedDict):
  enabled: bool
  only_changed: bool
  concurrency: int
  ignore_account_limit: bool
  inter_probe_delay_sec: float
  http_timeout_sec: float
  min_bytes_to_read: int
  use_ffprobe: bool
  ffprobe_timeout_sec: float
  max_candidates_per_channel: int


class PublicEpgDict(TypedDict):
  verify: bool
  refresh_hours: float


class PublicOutputDict(TypedDict):
  m3u8_enabled: bool
  xtream_enabled: bool
  include_unmatched_comment: bool


class PublicPipelineDict(TypedDict):
  lineup: PublicLineupDict
  matching: PublicMatchingDict
  validation: PublicValidationDict
  epg: PublicEpgDict
  output: PublicOutputDict


class PublicConfigPayload(TypedDict):
  """Scoped operational settings payload exposed by `GET /api/config`."""

  upstream_xtream: PublicUpstreamXtreamDict
  xtream_server: PublicXtreamServerDict
  pipeline: PublicPipelineDict


ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(value: Any) -> Any:
  if isinstance(value, str):
    return ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
  if isinstance(value, dict):
    return {k: _interpolate_env(v) for k, v in value.items()}
  if isinstance(value, list):
    return [_interpolate_env(v) for v in value]
  return value


def _subdict(mapping: dict[str, Any], *keys: str) -> dict[str, Any]:
  """Return the first present dict value among `keys` in `mapping`, or `{}`."""
  for key in keys:
    val = mapping.get(key)
    if isinstance(val, dict):
      return val
  return {}


def _first_str(*values: Any, default: str = "") -> str:
  """Return the first non-empty stripped string from `values`, or `default`."""
  for val in values:
    if val is not None:
      s = str(val).strip()
      if s:
        return s
  return default


def _first_defined(*values: Any, default: Any = None) -> Any:
  """Return the first non-None value from `values`, or `default`."""
  for val in values:
    if val is not None:
      return val
  return default


def _mask(secret: str, redact: bool) -> str:
  return "***" if (redact and secret) else secret


def _load_yaml_dict(path: Path, *, interpolate: bool = False) -> dict[str, Any]:
  if not path.is_file():
    return {}
  with path.open("r", encoding="utf-8") as fh:
    loaded = yaml.safe_load(fh) or {}
  if not isinstance(loaded, dict):
    return {}
  return _interpolate_env(loaded) if interpolate else loaded


@dataclass(slots=True)
class XtreamConfig:
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
  enabled: bool = True
  only_changed: bool = True
  concurrency: int = 2
  ignore_account_limit: bool = False
  inter_probe_delay_sec: float = 0.35
  http_timeout_sec: float = 8.0
  min_bytes_to_read: int = 8192
  use_ffprobe: bool = True
  ffprobe_timeout_sec: float = 12.0
  max_candidates_per_channel: int = 5


@dataclass(slots=True)
class SchedulesDirectConfig:
  username: str = ""
  password: str = ""
  base_url: str = "https://json.schedulesdirect.org/20141201"
  auto_add_lineup: bool = False


@dataclass(slots=True)
class TvGuideConfig:
  base_url: str = "https://backend.tvguide.com/tvschedules/tvguide"


@dataclass(slots=True)
class LineupConfig:
  backend: str = "auto"
  lineup_id: str = DEFAULT_BELL_FIBE_MTL_LINEUP_ID
  postal_code: str = "H2Y"
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
  tvguide: TvGuideConfig = field(default_factory=TvGuideConfig)


@dataclass(slots=True)
class StateConfig:
  state_dir: Path = Path("state")
  mapping_path: Path = Path("state/channel_mapping.json")
  upgrade_hysteresis_score: float = 5.0


@dataclass(slots=True)
class EpgConfig:
  verify: bool = True
  refresh_hours: float = 6.0
  xmltv_file: Path | None = None

  @property
  def fetch(self) -> bool:
    """Backwards-compatible alias for `verify`."""
    return self.verify


@dataclass(slots=True)
class OutputConfig:
  m3u8_enabled: bool = True
  playlist_path: Path = Path("output/bellhop.m3u8")
  report_path: Path = Path("output/lineup_report.json")
  xtream_enabled: bool = True
  xtream_dir: Path = Path("output/xtream")
  include_unmatched_comment: bool = False


@dataclass(slots=True)
class WebConfig:
  host: str = "0.0.0.0"
  port: int = 8080
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
class ServeWebConfig:
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


@dataclass(slots=True)
class AppConfig:
  project_root: Path = field(default_factory=Path.cwd)
  config_file: Path | None = None
  credentials_file: Path = Path("credentials.yaml")
  xtream: XtreamConfig = field(default_factory=XtreamConfig)
  validation: ValidationConfig = field(default_factory=ValidationConfig)
  lineup: LineupConfig = field(default_factory=LineupConfig)
  state: StateConfig = field(default_factory=StateConfig)
  epg: EpgConfig = field(default_factory=EpgConfig)
  output: OutputConfig = field(default_factory=OutputConfig)
  web: WebConfig = field(default_factory=WebConfig)
  serve: ServeConfig = field(default_factory=ServeConfig)

  def resolve_path(self, path: Path | str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (self.project_root / p).resolve()

  def to_dict(self, *, redact_secrets: bool = True) -> dict[str, Any]:
    """Serialize full configuration for diagnostics, redacting passwords when requested."""
    return {
      "credentials_file": str(self.credentials_file),
      "xtream": {
        "server_url": self.xtream.server_url,
        "username": self.xtream.username,
        "password": _mask(self.xtream.password, redact_secrets),
        "stream_format": self.xtream.stream_format,
        "request_timeout_sec": self.xtream.request_timeout_sec,
        "refresh_hours": self.xtream.cache_ttl_hours,
        "cache_dir": str(self.xtream.cache_dir),
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
          "base_url": self.lineup.schedules_direct.base_url,
          "auto_add_lineup": self.lineup.schedules_direct.auto_add_lineup,
        },
        "tvguide": {"base_url": self.lineup.tvguide.base_url},
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
      "web": {
        "host": self.web.host,
        "port": self.web.port,
        "settle_ms": self.web.settle_ms,
        "username": self.web.username,
        "password": _mask(self.web.password, redact_secrets),
        "session_ttl_hours": self.web.session_ttl_hours,
        "session_secret": _mask(self.web.session_secret, redact_secrets),
        "trust_proxy_headers": self.web.trust_proxy_headers,
        "trusted_proxy_ips": list(self.web.trusted_proxy_ips),
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


def _find_project_root(config_path: Path | None = None) -> Path:
  """Locate project root from explicit config path, CWD, or package installation."""
  if config_path is not None:
    cfg_dir = config_path.resolve().parent
    for candidate in (cfg_dir, *cfg_dir.parents):
      if (candidate / "data" / "lineups").is_dir() or (
        candidate / "pyproject.toml"
      ).is_file():
        return candidate
    return cfg_dir

  cwd = Path.cwd().resolve()
  for candidate in (cwd, *cwd.parents):
    if (candidate / "data" / "lineups").is_dir() or (
      candidate / "pyproject.toml"
    ).is_file():
      return candidate
  pkg_root = Path(__file__).resolve().parents[2]
  return pkg_root if (pkg_root / "data" / "lineups").is_dir() else cwd


def load_config(
  config_path: Path | str | None = None,
  env_path: Path | str | None = None,
  credentials_path: Path | str | None = None,
) -> AppConfig:
  """Load declarative configuration from `config.yaml` (+ referenced `credentials.yaml` & `.env`)."""
  raw_cfg_path = config_path or os.environ.get("BELLHOP_CONFIG") or None
  explicit_cfg = Path(raw_cfg_path).resolve() if raw_cfg_path else None
  if explicit_cfg is not None and not explicit_cfg.is_file():
    raise FileNotFoundError(f"Configuration file not found: {explicit_cfg}")

  root = _find_project_root(explicit_cfg)
  yaml_target = (
    explicit_cfg if explicit_cfg is not None else (root / "config.yaml")
  )
  base_dir = yaml_target.parent if yaml_target.is_file() else root

  pre_raw = _load_yaml_dict(yaml_target, interpolate=False)
  cfg_env_file = pre_raw.get("env_file")
  dotenv_target = (
    Path(env_path)
    if env_path
    else ((base_dir / str(cfg_env_file)) if cfg_env_file else (root / ".env"))
  )
  if dotenv_target.is_file():
    load_dotenv(dotenv_target, override=False)
  else:
    load_dotenv(override=False)

  raw = _interpolate_env(pre_raw)

  cfg_creds_ref = Path(str(raw.get("credentials_file") or "credentials.yaml"))
  if credentials_path:
    creds_target = Path(credentials_path)
  else:
    creds_target = (
      cfg_creds_ref
      if cfg_creds_ref.is_absolute()
      else (base_dir / cfg_creds_ref)
    )
    if not creds_target.is_file():
      creds_target = root / cfg_creds_ref

  loaded_creds = _load_yaml_dict(creds_target, interpolate=True)
  sd_creds = _subdict(loaded_creds, "schedules_direct")
  serve_creds = _subdict(loaded_creds, "serve")
  web_srv_creds = _subdict(loaded_creds, "web_server") or _subdict(
    serve_creds, "web"
  )
  xt_srv_creds = (
    _subdict(loaded_creds, "xtream_server")
    or _subdict(serve_creds, "xtream")
    or serve_creds
  )
  xt_creds = _subdict(loaded_creds, "upstream_xtream", "xtream") or loaded_creds

  # Support 2-tier layout (`paths`, `web_server`, `upstream_xtream`, `xtream_server`,
  # `pipeline: {lineup, matching, validation, epg, output}`) alongside legacy top-level sections.
  paths_raw = _subdict(raw, "paths")
  pipe_raw = _subdict(raw, "pipeline")
  xt_raw = _subdict(raw, "upstream_xtream", "xtream")
  lu_raw = _subdict(pipe_raw, "lineup") or _subdict(raw, "lineup")
  match_raw = _subdict(pipe_raw, "matching")
  val_raw = _subdict(pipe_raw, "validation") or _subdict(raw, "validation")
  st_raw = _subdict(raw, "state")
  epg_raw = _subdict(pipe_raw, "epg") or _subdict(raw, "epg")
  out_raw = _subdict(pipe_raw, "output") or _subdict(raw, "output")
  web_raw = _subdict(raw, "web")
  serve_raw = _subdict(raw, "serve")
  serve_web_raw = _subdict(raw, "web_server") or _subdict(serve_raw, "web")
  serve_xt_raw = _subdict(raw, "xtream_server") or _subdict(serve_raw, "xtream")

  seed_json_raw = _first_defined(
    paths_raw.get("seed_json"),
    xt_raw.get("seed_json"),
    xt_raw.get("input_json"),
  )
  xtream = XtreamConfig(
    server_url=_first_str(
      xt_raw.get("server_url"),
      xt_raw.get("url"),
      xt_creds.get("server_url"),
      xt_creds.get("url"),
      os.environ.get("XTREAM_SERVER_URL"),
    ),
    username=_first_str(
      xt_raw.get("username"),
      xt_creds.get("username"),
      os.environ.get("XTREAM_USERNAME"),
    ),
    password=_first_str(
      xt_raw.get("password"),
      xt_creds.get("password"),
      os.environ.get("XTREAM_PASSWORD"),
    ),
    stream_format=_first_str(
      xt_raw.get("stream_format"),
      xt_creds.get("stream_format"),
      os.environ.get("XTREAM_STREAM_FORMAT"),
      default="ts",
    ).lstrip("."),
    request_timeout_sec=float(xt_raw.get("request_timeout_sec", 90.0)),
    cache_dir=Path(
      _first_str(
        paths_raw.get("cache_dir"), xt_raw.get("cache_dir"), default="cache"
      )
    ),
    cache_ttl_hours=float(
      xt_raw.get("refresh_hours", xt_raw.get("cache_ttl_hours", 24.0))
    ),
    seed_json=Path(str(seed_json_raw)) if seed_json_raw else None,
  )

  validation = ValidationConfig(
    enabled=bool(val_raw.get("enabled", True)),
    only_changed=bool(val_raw.get("only_changed", True)),
    concurrency=max(1, int(val_raw.get("concurrency", 2))),
    ignore_account_limit=bool(val_raw.get("ignore_account_limit", False)),
    inter_probe_delay_sec=max(
      0.0, float(val_raw.get("inter_probe_delay_sec", 0.35))
    ),
    http_timeout_sec=float(val_raw.get("http_timeout_sec", 8.0)),
    min_bytes_to_read=max(512, int(val_raw.get("min_bytes_to_read", 8192))),
    use_ffprobe=bool(val_raw.get("use_ffprobe", True)),
    ffprobe_timeout_sec=float(val_raw.get("ffprobe_timeout_sec", 12.0)),
    max_candidates_per_channel=max(
      1, int(val_raw.get("max_candidates_per_channel", 5))
    ),
  )

  sd_cfg_raw = _subdict(lu_raw, "schedules_direct") or _subdict(
    raw, "schedules_direct"
  )
  tvg_cfg_raw = _subdict(lu_raw, "tvguide") or _subdict(raw, "tvguide")
  collapse_sd = bool(lu_raw.get("collapse_sd_dupes", False))
  raw_num_mode = str(
    lu_raw.get("numbering_mode", "hd" if collapse_sd else "both")
  ).lower()
  if collapse_sd:
    raw_num_mode = "hd"

  lineup = LineupConfig(
    backend=str(lu_raw.get("backend", "auto"))
    .strip()
    .lower()
    .replace("-", "_"),
    lineup_id=str(
      lu_raw.get("lineup_id", DEFAULT_BELL_FIBE_MTL_LINEUP_ID)
    ).strip(),
    postal_code=str(lu_raw.get("postal_code", "H2Y")).strip(),
    refresh_days=float(lu_raw.get("refresh_days", 30.0)),
    file=Path(
      _first_str(
        paths_raw.get("lineup_file"),
        lu_raw.get("file"),
        default="data/lineups/bell_fibe_montreal.yaml",
      )
    ),
    numbering_mode=raw_num_mode,
    collapse_sd_dupes=collapse_sd,
    exclude_audio=bool(lu_raw.get("exclude_audio", False)),
    pinned_streams={
      int(k): int(v) for k, v in (lu_raw.get("pinned_streams") or {}).items()
    },
    excluded_channels={
      int(ch) for ch in (lu_raw.get("excluded_channels") or [])
    },
    schedules_direct=SchedulesDirectConfig(
      username=_first_str(
        sd_cfg_raw.get("username"),
        sd_creds.get("username"),
        os.environ.get("SD_USERNAME"),
      ),
      password=_first_str(
        sd_cfg_raw.get("password"),
        sd_creds.get("password"),
        os.environ.get("SD_PASSWORD"),
      ),
      base_url=_first_str(
        sd_cfg_raw.get("base_url"),
        sd_creds.get("base_url"),
        default="https://json.schedulesdirect.org/20141201",
      ),
      auto_add_lineup=bool(
        sd_cfg_raw.get(
          "auto_add_lineup", sd_creds.get("auto_add_lineup", False)
        )
      ),
    ),
    tvguide=TvGuideConfig(
      base_url=_first_str(
        tvg_cfg_raw.get("base_url"),
        default="https://backend.tvguide.com/tvschedules/tvguide",
      )
    ),
  )

  state_dir_path = Path(
    _first_str(
      paths_raw.get("state_dir"), st_raw.get("state_dir"), default="state"
    )
  )
  state = StateConfig(
    state_dir=state_dir_path,
    mapping_path=Path(
      _first_str(
        paths_raw.get("mapping_path"),
        st_raw.get("mapping_path"),
        default=str(state_dir_path / "channel_mapping.json"),
      )
    ),
    upgrade_hysteresis_score=float(
      match_raw.get(
        "upgrade_hysteresis_score", st_raw.get("upgrade_hysteresis_score", 5.0)
      )
    ),
  )

  epg_xml_raw = _first_defined(
    paths_raw.get("xmltv_file"), epg_raw.get("xmltv_file")
  )
  epg = EpgConfig(
    verify=bool(epg_raw.get("verify", epg_raw.get("fetch", True))),
    refresh_hours=float(epg_raw.get("refresh_hours", 6.0)),
    xmltv_file=Path(str(epg_xml_raw)) if epg_xml_raw else None,
  )

  m3u8_sub = _subdict(out_raw, "m3u8")
  xt_sub = _subdict(out_raw, "xtream")
  output = OutputConfig(
    m3u8_enabled=bool(
      m3u8_sub.get("enabled", out_raw.get("m3u8_enabled", True))
    ),
    playlist_path=Path(
      _first_str(
        paths_raw.get("m3u8_playlist_path"),
        m3u8_sub.get("playlist_path"),
        out_raw.get("playlist_path"),
        default="output/bellhop.m3u8",
      )
    ),
    report_path=Path(
      _first_str(
        paths_raw.get("m3u8_report_path"),
        m3u8_sub.get("report_path"),
        out_raw.get("report_path"),
        default="output/lineup_report.json",
      )
    ),
    xtream_enabled=bool(
      xt_sub.get("enabled", out_raw.get("xtream_enabled", True))
    ),
    xtream_dir=Path(
      _first_str(
        paths_raw.get("xtream_output_dir"),
        xt_sub.get("dir"),
        xt_sub.get("xtream_dir"),
        out_raw.get("xtream_dir"),
        default="output/xtream",
      )
    ),
    include_unmatched_comment=bool(
      out_raw.get("include_unmatched_comment", False)
    ),
  )

  default_serve_host = _first_str(
    serve_web_raw.get("host"),
    serve_raw.get("host"),
    web_raw.get("host"),
    default="0.0.0.0",
  )
  default_serve_port = int(
    _first_defined(
      serve_web_raw.get("port"),
      serve_raw.get("port"),
      web_raw.get("port"),
      default=8080,
    )
  )
  default_settle_ms = max(
    0,
    int(
      _first_defined(
        serve_web_raw.get("settle_ms"),
        serve_raw.get("settle_ms"),
        web_raw.get("settle_ms"),
        default=180,
      )
    ),
  )

  web_username = _first_str(
    serve_web_raw.get("username"),
    web_raw.get("username"),
    web_srv_creds.get("username"),
    os.environ.get("BELLHOP_WEB_USERNAME"),
    os.environ.get("IPTV_WEB_USERNAME"),
  )
  web_password = _first_str(
    serve_web_raw.get("password"),
    web_raw.get("password"),
    web_srv_creds.get("password"),
    os.environ.get("BELLHOP_WEB_PASSWORD"),
    os.environ.get("IPTV_WEB_PASSWORD"),
  )
  web_session_ttl_hours = float(
    _first_defined(
      serve_web_raw.get("session_ttl_hours"),
      web_raw.get("session_ttl_hours"),
      default=168.0,
    )
  )
  web_session_secret = _first_str(
    serve_web_raw.get("session_secret"),
    web_raw.get("session_secret"),
    web_srv_creds.get("session_secret"),
    os.environ.get("BELLHOP_WEB_SESSION_SECRET"),
    os.environ.get("IPTV_WEB_SESSION_SECRET"),
  )
  web_trust_proxy = bool(
    _first_defined(
      serve_web_raw.get("trust_proxy_headers"),
      web_raw.get("trust_proxy_headers"),
      default=False,
    )
  )
  raw_proxy_ips = _first_defined(
    serve_web_raw.get("trusted_proxy_ips"),
    web_raw.get("trusted_proxy_ips"),
  )
  if raw_proxy_ips is None:
    web_trusted_proxy_ips = list(DEFAULT_TRUSTED_PROXY_IPS)
  elif isinstance(raw_proxy_ips, list):
    web_trusted_proxy_ips = [
      str(item).strip() for item in raw_proxy_ips if str(item).strip()
    ]
  elif isinstance(raw_proxy_ips, str):
    web_trusted_proxy_ips = [
      part.strip() for part in raw_proxy_ips.split(",") if part.strip()
    ]
  else:
    raise ValueError(
      "web_server.trusted_proxy_ips must be a list of IP addresses or CIDRs."
    )
  # Validate all configured reverse-proxy IPs/CIDRs eagerly at config load.
  parse_trusted_proxy_networks(web_trusted_proxy_ips)

  serve_web_port_raw = serve_web_raw.get("port")
  serve_xt_port_raw = serve_xt_raw.get("port")
  pub_host_raw = _first_defined(
    serve_xt_raw.get("public_host"), serve_raw.get("public_host")
  )
  pub_port_raw = _first_defined(
    serve_xt_raw.get("public_port"), serve_raw.get("public_port")
  )

  serve = ServeConfig(
    host=default_serve_host,
    port=default_serve_port,
    web=ServeWebConfig(
      enabled=bool(serve_web_raw.get("enabled", True)),
      host=_opt_str(serve_web_raw.get("host")),
      port=int(serve_web_port_raw) if serve_web_port_raw is not None else None,
      settle_ms=default_settle_ms,
      username=web_username,
      password=web_password,
      session_ttl_hours=web_session_ttl_hours,
      session_secret=web_session_secret,
      trust_proxy_headers=web_trust_proxy,
      trusted_proxy_ips=list(web_trusted_proxy_ips),
    ),
    xtream=ServeXtreamConfig(
      enabled=bool(serve_xt_raw.get("enabled", True)),
      host=_opt_str(serve_xt_raw.get("host")),
      port=int(serve_xt_port_raw) if serve_xt_port_raw is not None else None,
      username=_first_str(
        serve_xt_raw.get("username"),
        serve_raw.get("username"),
        xt_srv_creds.get("username"),
        os.environ.get("BELLHOP_SERVE_USERNAME"),
        os.environ.get("IPTV_SERVE_USERNAME"),
      ),
      password=_first_str(
        serve_xt_raw.get("password"),
        serve_raw.get("password"),
        xt_srv_creds.get("password"),
        os.environ.get("BELLHOP_SERVE_PASSWORD"),
        os.environ.get("IPTV_SERVE_PASSWORD"),
      ),
      public_host=_opt_str(pub_host_raw),
      public_port=int(pub_port_raw) if pub_port_raw is not None else None,
      server_protocol=_first_str(
        serve_xt_raw.get("server_protocol"),
        serve_raw.get("server_protocol"),
        default="http",
      ).lower(),
    ),
  )

  web = WebConfig(
    host=_first_str(web_raw.get("host"), serve.web_host, default="0.0.0.0"),
    port=int(_first_defined(web_raw.get("port"), default=serve.web_port)),
    settle_ms=max(
      0,
      int(
        _first_defined(web_raw.get("settle_ms"), default=serve.web.settle_ms)
      ),
    ),
    username=web_username,
    password=web_password,
    session_ttl_hours=web_session_ttl_hours,
    session_secret=web_session_secret,
    trust_proxy_headers=web_trust_proxy,
    trusted_proxy_ips=list(web_trusted_proxy_ips),
  )

  pkg_root = Path(__file__).resolve().parents[2]
  effective_root = (
    base_dir
    if (explicit_cfg is not None and not (base_dir / "src").is_dir())
    else root
  )
  if (
    not lineup.file.is_absolute()
    and not (effective_root / lineup.file).is_file()
  ):
    for fallback_root in (root, Path.cwd().resolve(), pkg_root):
      if (fallback_root / lineup.file).is_file():
        lineup.file = (fallback_root / lineup.file).resolve()
        break

  return AppConfig(
    project_root=effective_root,
    config_file=yaml_target if yaml_target.is_file() else None,
    credentials_file=creds_target,
    xtream=xtream,
    validation=validation,
    lineup=lineup,
    state=state,
    epg=epg,
    output=output,
    web=web,
    serve=serve,
  )


def export_public_config(config: AppConfig) -> PublicConfigPayload:
  """Serialize operational settings for the Web UI Settings panel.

  Strictly exposes ONLY the three operational domains:
  - `upstream_xtream` (non-credential upstream IPTV settings)
  - `xtream_server` (passthrough Xtream Codes API server settings; non-credential)
  - `pipeline` (lineup, matching, validation, EPG, and output toggles)

  Strictly excludes:
  - Web Guide host/port/auth/session settings (`web_server`)
  - All filesystem directories & file paths (`paths`)
  - All usernames, passwords, and secrets
  """
  return {
    "upstream_xtream": {
      "server_url": config.xtream.server_url,
      "stream_format": config.xtream.stream_format,
      "request_timeout_sec": config.xtream.request_timeout_sec,
      "refresh_hours": config.xtream.cache_ttl_hours,
    },
    "xtream_server": {
      "enabled": config.serve.xtream.enabled,
      "host": config.serve.xtream.host,
      "port": config.serve.xtream.port,
      "public_host": config.serve.xtream.public_host,
      "public_port": config.serve.xtream.public_port,
      "server_protocol": config.serve.xtream.server_protocol,
    },
    "pipeline": {
      "lineup": {
        "backend": config.lineup.backend,
        "lineup_id": config.lineup.lineup_id,
        "postal_code": config.lineup.postal_code,
        "refresh_days": config.lineup.refresh_days,
        "numbering_mode": config.lineup.numbering_mode,
        "collapse_sd_dupes": config.lineup.collapse_sd_dupes,
        "exclude_audio": config.lineup.exclude_audio,
        "pinned_streams": {
          str(k): int(v) for k, v in config.lineup.pinned_streams.items()
        },
        "excluded_channels": sorted(config.lineup.excluded_channels),
        "tvguide": {"base_url": config.lineup.tvguide.base_url},
        "schedules_direct": {
          "base_url": config.lineup.schedules_direct.base_url,
          "auto_add_lineup": config.lineup.schedules_direct.auto_add_lineup,
        },
      },
      "matching": {
        "upgrade_hysteresis_score": config.state.upgrade_hysteresis_score,
      },
      "validation": {
        "enabled": config.validation.enabled,
        "only_changed": config.validation.only_changed,
        "concurrency": config.validation.concurrency,
        "ignore_account_limit": config.validation.ignore_account_limit,
        "inter_probe_delay_sec": config.validation.inter_probe_delay_sec,
        "http_timeout_sec": config.validation.http_timeout_sec,
        "min_bytes_to_read": config.validation.min_bytes_to_read,
        "use_ffprobe": config.validation.use_ffprobe,
        "ffprobe_timeout_sec": config.validation.ffprobe_timeout_sec,
        "max_candidates_per_channel": config.validation.max_candidates_per_channel,
      },
      "epg": {
        "verify": config.epg.verify,
        "refresh_hours": config.epg.refresh_hours,
      },
      "output": {
        "m3u8_enabled": config.output.m3u8_enabled,
        "xtream_enabled": config.output.xtream_enabled,
        "include_unmatched_comment": config.output.include_unmatched_comment,
      },
    },
  }


def _opt_str(val: Any) -> str | None:
  if val is None:
    return None
  s = str(val).strip()
  return s if s else None


def _req_port(
  val: Any, field_name: str, *, allow_none: bool = False
) -> int | None:
  if val is None or (isinstance(val, str) and not val.strip()):
    if allow_none:
      return None
    raise ValueError(f"{field_name} is required.")
  try:
    port = int(val)
  except (TypeError, ValueError) as exc:
    raise ValueError(f"{field_name} must be an integer.") from exc
  if port < 1 or port > 65535:
    raise ValueError(f"{field_name} must be between 1 and 65535.")
  return port


def _pos_float(val: Any, field_name: str, *, allow_zero: bool = False) -> float:
  try:
    num = float(val)
  except (TypeError, ValueError) as exc:
    raise ValueError(f"{field_name} must be a number.") from exc
  if (allow_zero and num < 0) or (not allow_zero and num <= 0):
    op = ">= 0" if allow_zero else "> 0"
    raise ValueError(f"{field_name} must be {op}.")
  return num


def _pos_int(val: Any, field_name: str, *, minimum: int = 1) -> int:
  try:
    num = int(val)
  except (TypeError, ValueError) as exc:
    raise ValueError(f"{field_name} must be an integer.") from exc
  if num < minimum:
    raise ValueError(f"{field_name} must be >= {minimum}.")
  return num


_UNSET = object()


def _build_roundtrip_yaml() -> YAML:
  """Build a `ruamel.yaml` round-trip loader/dumper preserving comments & quotes."""
  rt = YAML(typ="rt")
  rt.preserve_quotes = True
  rt.width = 4096
  rt.indent(mapping=2, sequence=4, offset=2)
  rt.representer.add_representer(
    type(None),
    lambda rep, _data: rep.represent_scalar("tag:yaml.org,2002:null", "null"),
  )
  return rt


def _load_roundtrip_yaml(path: Path) -> tuple[YAML, CommentedMap]:
  """Load `path` as a comment-preserving `CommentedMap` without interpolation."""
  rt = _build_roundtrip_yaml()
  if not path.is_file():
    return rt, CommentedMap()
  loaded: Any = rt.load(path.read_text(encoding="utf-8"))
  if isinstance(loaded, CommentedMap):
    return rt, loaded
  cm = CommentedMap()
  if isinstance(loaded, Mapping):
    for k, v in loaded.items():
      cm[str(k)] = v
  return rt, cm


def _ensure_dict(
  parent: MutableMapping[str, Any], key: str
) -> MutableMapping[str, Any]:
  sub = parent.get(key)
  if not isinstance(sub, MutableMapping):
    sub = CommentedMap()
    parent[key] = sub
  return sub


def _set_rt_value(
  parent: MutableMapping[str, Any],
  key: str,
  value: Any,
  *,
  current_resolved: Any = _UNSET,
) -> None:
  """Update `parent[key]` while preserving comments, quotes, and `${ENV}` placeholders."""
  if key in parent:
    existing = parent[key]
    if (
      isinstance(existing, str)
      and "${" in existing
      and current_resolved is not _UNSET
      and value == current_resolved
    ):
      return
    if isinstance(existing, str) and isinstance(value, str):
      if str(existing) == value:
        return
      if isinstance(existing, DoubleQuotedScalarString):
        parent[key] = DoubleQuotedScalarString(value)
        return
      if isinstance(existing, SingleQuotedScalarString):
        parent[key] = SingleQuotedScalarString(value)
        return
    if type(existing) is type(value) and existing == value:
      return
    if (
      isinstance(existing, Mapping)
      and isinstance(value, Mapping)
      and not existing
      and not value
    ):
      return
    if (
      isinstance(existing, list)
      and isinstance(value, list)
      and existing == value
    ):
      return
  parent[key] = value


def apply_public_config_update(
  config_path: Path,
  updates: Mapping[str, Any],
) -> tuple[AppConfig, bool]:
  """Validate and atomically merge operational settings into `config.yaml`.

  Preserves YAML comments, quotes, and `${ENV}` placeholders via `ruamel.yaml`.
  Strictly scopes updates to `upstream_xtream`, `xtream_server`, and `pipeline`.
  Never modifies `web_server` / `serve.web`, filesystem paths/directories (`paths`),
  or any `username`, `password`, `session_secret`, or `credentials_file` values on disk.
  Returns `(reloaded_config, restart_required)`.
  """
  if not isinstance(updates, Mapping):
    raise ValueError("Configuration update payload must be a JSON object.")

  target = config_path.resolve()
  before_cfg = (
    load_config(target)
    if target.is_file()
    else AppConfig(project_root=target.parent)
  )
  rt_yaml, raw = _load_roundtrip_yaml(target)
  use_structured = any(
    k in raw
    for k in (
      "upstream_xtream",
      "xtream_server",
      "pipeline",
      "web_server",
      "paths",
    )
  )

  # 1. upstream_xtream (non-secret, non-path fields only)
  u_xt = updates.get("upstream_xtream")
  if isinstance(u_xt, dict):
    xt = _ensure_dict(
      raw,
      "upstream_xtream"
      if (use_structured or "xtream" not in raw)
      else "xtream",
    )
    if "server_url" in u_xt:
      _set_rt_value(
        xt,
        "server_url",
        str(u_xt["server_url"] or "").strip(),
        current_resolved=before_cfg.xtream.server_url,
      )
    if "stream_format" in u_xt:
      fmt = str(u_xt["stream_format"] or "").strip().lower().lstrip(".")
      if fmt not in {"ts", "m3u8"}:
        raise ValueError(
          "upstream_xtream.stream_format must be 'ts' or 'm3u8'."
        )
      _set_rt_value(
        xt,
        "stream_format",
        fmt,
        current_resolved=before_cfg.xtream.stream_format,
      )
    if "request_timeout_sec" in u_xt:
      _set_rt_value(
        xt,
        "request_timeout_sec",
        _pos_float(
          u_xt["request_timeout_sec"], "upstream_xtream.request_timeout_sec"
        ),
        current_resolved=before_cfg.xtream.request_timeout_sec,
      )
    if "refresh_hours" in u_xt:
      _set_rt_value(
        xt,
        "refresh_hours",
        _pos_float(
          u_xt["refresh_hours"],
          "upstream_xtream.refresh_hours",
          allow_zero=True,
        ),
        current_resolved=before_cfg.xtream.cache_ttl_hours,
      )

  # 2. xtream_server (passthrough Xtream API only; web_server host/port/auth never touched)
  u_sx = updates.get("xtream_server")
  if isinstance(u_sx, dict):
    sx = (
      _ensure_dict(raw, "xtream_server")
      if (use_structured or "serve" not in raw)
      else _ensure_dict(_ensure_dict(raw, "serve"), "xtream")
    )
    if "enabled" in u_sx:
      _set_rt_value(
        sx,
        "enabled",
        bool(u_sx["enabled"]),
        current_resolved=before_cfg.serve.xtream.enabled,
      )
    if "host" in u_sx:
      _set_rt_value(
        sx,
        "host",
        _opt_str(u_sx["host"]),
        current_resolved=before_cfg.serve.xtream.host,
      )
    if "port" in u_sx:
      _set_rt_value(
        sx,
        "port",
        _req_port(u_sx["port"], "xtream_server.port", allow_none=True),
        current_resolved=before_cfg.serve.xtream.port,
      )
    if "public_host" in u_sx:
      _set_rt_value(
        sx,
        "public_host",
        _opt_str(u_sx["public_host"]),
        current_resolved=before_cfg.serve.xtream.public_host,
      )
    if "public_port" in u_sx:
      _set_rt_value(
        sx,
        "public_port",
        _req_port(
          u_sx["public_port"], "xtream_server.public_port", allow_none=True
        ),
        current_resolved=before_cfg.serve.xtream.public_port,
      )
    if "server_protocol" in u_sx:
      proto = str(u_sx["server_protocol"] or "").strip().lower()
      if proto not in {"http", "https"}:
        raise ValueError(
          "xtream_server.server_protocol must be 'http' or 'https'."
        )
      _set_rt_value(
        sx,
        "server_protocol",
        proto,
        current_resolved=before_cfg.serve.xtream.server_protocol,
      )

  # 3. pipeline (non-secret, non-path settings only)
  u_pipe = updates.get("pipeline")
  if isinstance(u_pipe, dict):
    pipe_parent = _ensure_dict(raw, "pipeline") if use_structured else raw

    if isinstance(u_pipe.get("lineup"), dict):
      u_lu = u_pipe["lineup"]
      lu = _ensure_dict(pipe_parent, "lineup")
      if "backend" in u_lu:
        bk = str(u_lu["backend"] or "").strip().lower()
        if bk not in {"auto", "yaml", "tvguide", "schedules_direct"}:
          raise ValueError(
            "pipeline.lineup.backend must be one of: auto, yaml, tvguide, schedules_direct."
          )
        _set_rt_value(
          lu, "backend", bk, current_resolved=before_cfg.lineup.backend
        )
      if "lineup_id" in u_lu:
        lid = str(u_lu["lineup_id"] or "").strip()
        if not lid:
          raise ValueError("pipeline.lineup.lineup_id cannot be empty.")
        _set_rt_value(
          lu, "lineup_id", lid, current_resolved=before_cfg.lineup.lineup_id
        )
      if "postal_code" in u_lu:
        _set_rt_value(
          lu,
          "postal_code",
          str(u_lu["postal_code"] or "").strip(),
          current_resolved=before_cfg.lineup.postal_code,
        )
      if "refresh_days" in u_lu:
        _set_rt_value(
          lu,
          "refresh_days",
          _pos_float(
            u_lu["refresh_days"],
            "pipeline.lineup.refresh_days",
            allow_zero=True,
          ),
          current_resolved=before_cfg.lineup.refresh_days,
        )
      if "numbering_mode" in u_lu:
        nm = str(u_lu["numbering_mode"] or "").strip().lower()
        if nm not in {"both", "hd", "sd"}:
          raise ValueError(
            "pipeline.lineup.numbering_mode must be 'both', 'hd', or 'sd'."
          )
        _set_rt_value(
          lu,
          "numbering_mode",
          nm,
          current_resolved=before_cfg.lineup.numbering_mode,
        )
      if "collapse_sd_dupes" in u_lu:
        _set_rt_value(
          lu,
          "collapse_sd_dupes",
          bool(u_lu["collapse_sd_dupes"]),
          current_resolved=before_cfg.lineup.collapse_sd_dupes,
        )
      if "exclude_audio" in u_lu:
        _set_rt_value(
          lu,
          "exclude_audio",
          bool(u_lu["exclude_audio"]),
          current_resolved=before_cfg.lineup.exclude_audio,
        )
      if "pinned_streams" in u_lu:
        raw_pins = u_lu["pinned_streams"]
        if not isinstance(raw_pins, dict):
          raise ValueError(
            "pipeline.lineup.pinned_streams must be a mapping of channel number to stream ID."
          )
        _set_rt_value(
          lu,
          "pinned_streams",
          {
            _pos_int(k, "pipeline.lineup.pinned_streams key"): _pos_int(
              v, "pipeline.lineup.pinned_streams value"
            )
            for k, v in raw_pins.items()
          },
        )
      if "excluded_channels" in u_lu:
        raw_excl = u_lu["excluded_channels"]
        if not isinstance(raw_excl, list):
          raise ValueError(
            "pipeline.lineup.excluded_channels must be a list of channel numbers."
          )
        _set_rt_value(
          lu,
          "excluded_channels",
          sorted(
            {
              _pos_int(c, "pipeline.lineup.excluded_channels item")
              for c in raw_excl
            }
          ),
        )
      if (
        isinstance(u_lu.get("tvguide"), dict) and "base_url" in u_lu["tvguide"]
      ):
        _set_rt_value(
          _ensure_dict(lu, "tvguide"),
          "base_url",
          str(u_lu["tvguide"]["base_url"] or "").strip(),
          current_resolved=before_cfg.lineup.tvguide.base_url,
        )
      if isinstance(u_lu.get("schedules_direct"), dict):
        sd = _ensure_dict(lu, "schedules_direct")
        if "base_url" in u_lu["schedules_direct"]:
          _set_rt_value(
            sd,
            "base_url",
            str(u_lu["schedules_direct"]["base_url"] or "").strip(),
            current_resolved=before_cfg.lineup.schedules_direct.base_url,
          )
        if "auto_add_lineup" in u_lu["schedules_direct"]:
          _set_rt_value(
            sd,
            "auto_add_lineup",
            bool(u_lu["schedules_direct"]["auto_add_lineup"]),
            current_resolved=before_cfg.lineup.schedules_direct.auto_add_lineup,
          )

    if (
      isinstance(u_pipe.get("matching"), dict)
      and "upgrade_hysteresis_score" in u_pipe["matching"]
    ):
      hyst = _pos_float(
        u_pipe["matching"]["upgrade_hysteresis_score"],
        "pipeline.matching.upgrade_hysteresis_score",
        allow_zero=True,
      )
      target_match = (
        _ensure_dict(pipe_parent, "matching")
        if use_structured
        else _ensure_dict(raw, "state")
      )
      _set_rt_value(
        target_match,
        "upgrade_hysteresis_score",
        hyst,
        current_resolved=before_cfg.state.upgrade_hysteresis_score,
      )

    if isinstance(u_pipe.get("validation"), dict):
      u_val = u_pipe["validation"]
      val = _ensure_dict(pipe_parent, "validation")
      for bkey in (
        "enabled",
        "only_changed",
        "ignore_account_limit",
        "use_ffprobe",
      ):
        if bkey in u_val:
          _set_rt_value(val, bkey, bool(u_val[bkey]))
      if "concurrency" in u_val:
        _set_rt_value(
          val,
          "concurrency",
          _pos_int(
            u_val["concurrency"], "pipeline.validation.concurrency", minimum=1
          ),
        )
      if "inter_probe_delay_sec" in u_val:
        _set_rt_value(
          val,
          "inter_probe_delay_sec",
          _pos_float(
            u_val["inter_probe_delay_sec"],
            "pipeline.validation.inter_probe_delay_sec",
            allow_zero=True,
          ),
        )
      if "http_timeout_sec" in u_val:
        _set_rt_value(
          val,
          "http_timeout_sec",
          _pos_float(
            u_val["http_timeout_sec"], "pipeline.validation.http_timeout_sec"
          ),
        )
      if "min_bytes_to_read" in u_val:
        _set_rt_value(
          val,
          "min_bytes_to_read",
          _pos_int(
            u_val["min_bytes_to_read"],
            "pipeline.validation.min_bytes_to_read",
            minimum=512,
          ),
        )
      if "ffprobe_timeout_sec" in u_val:
        _set_rt_value(
          val,
          "ffprobe_timeout_sec",
          _pos_float(
            u_val["ffprobe_timeout_sec"],
            "pipeline.validation.ffprobe_timeout_sec",
          ),
        )
      if "max_candidates_per_channel" in u_val:
        _set_rt_value(
          val,
          "max_candidates_per_channel",
          _pos_int(
            u_val["max_candidates_per_channel"],
            "pipeline.validation.max_candidates_per_channel",
            minimum=1,
          ),
        )

    if isinstance(u_pipe.get("epg"), dict):
      u_epg = u_pipe["epg"]
      epg_d = _ensure_dict(pipe_parent, "epg")
      if "verify" in u_epg:
        _set_rt_value(epg_d, "verify", bool(u_epg["verify"]))
        epg_d.pop("fetch", None)
      if "refresh_hours" in u_epg:
        _set_rt_value(
          epg_d,
          "refresh_hours",
          _pos_float(
            u_epg["refresh_hours"],
            "pipeline.epg.refresh_hours",
            allow_zero=True,
          ),
        )

    if isinstance(u_pipe.get("output"), dict):
      u_out = u_pipe["output"]
      out_d = _ensure_dict(pipe_parent, "output")
      if "include_unmatched_comment" in u_out:
        _set_rt_value(
          out_d,
          "include_unmatched_comment",
          bool(u_out["include_unmatched_comment"]),
        )
      if "m3u8_enabled" in u_out:
        if isinstance(out_d.get("m3u8"), MutableMapping):
          _set_rt_value(out_d["m3u8"], "enabled", bool(u_out["m3u8_enabled"]))
        else:
          _set_rt_value(out_d, "m3u8_enabled", bool(u_out["m3u8_enabled"]))
      if "xtream_enabled" in u_out:
        if isinstance(out_d.get("xtream"), MutableMapping):
          _set_rt_value(
            out_d["xtream"], "enabled", bool(u_out["xtream_enabled"])
          )
        else:
          _set_rt_value(out_d, "xtream_enabled", bool(u_out["xtream_enabled"]))

  target.parent.mkdir(parents=True, exist_ok=True)
  tmp_path = target.with_suffix(".yaml.tmp")
  buf = StringIO()
  rt_yaml.dump(raw, buf)
  tmp_path.write_text(buf.getvalue(), encoding="utf-8")
  try:
    reloaded = load_config(tmp_path)
  except Exception:
    tmp_path.unlink(missing_ok=True)
    raise

  if not reloaded.serve.web.enabled and not reloaded.serve.xtream.enabled:
    tmp_path.unlink(missing_ok=True)
    raise ValueError("At least one server must remain enabled.")

  tmp_path.replace(target)
  final_cfg = load_config(target)

  restart_required = (
    before_cfg.serve.xtream.enabled != final_cfg.serve.xtream.enabled
    or before_cfg.serve.xtream_host != final_cfg.serve.xtream_host
    or before_cfg.serve.xtream_port != final_cfg.serve.xtream_port
  )
  return final_cfg, restart_required
