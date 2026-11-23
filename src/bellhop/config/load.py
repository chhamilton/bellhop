"""Two-tier YAML configuration loader (`config.yaml` + `credentials.yaml` + `.env`)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from bellhop.config.model import (
  AppConfig,
  EpgConfig,
  IptvOrgConfig,
  LineupConfig,
  OutputConfig,
  SchedulesDirectConfig,
  ServeConfig,
  ServeWebConfig,
  ServeXtreamConfig,
  StateConfig,
  TvGuideConfig,
  ValidationConfig,
  XtreamConfig,
)
from bellhop.iptv_org import DEFAULT_IPTV_ORG_API_URL
from bellhop.lineup_loader import DEFAULT_BELL_FIBE_MTL_LINEUP_ID
from bellhop_web.http_utils import (
  DEFAULT_TRUSTED_PROXY_IPS,
  parse_trusted_proxy_networks,
)

ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def interpolate_env(value: Any) -> Any:
  """Recursively substitute `${VAR}` placeholders from `os.environ`."""
  if isinstance(value, str):
    return ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
  if isinstance(value, dict):
    return {k: interpolate_env(v) for k, v in value.items()}
  if isinstance(value, list):
    return [interpolate_env(v) for v in value]
  return value


def _subdict(mapping: dict[str, Any], *keys: str) -> dict[str, Any]:
  for key in keys:
    val = mapping.get(key)
    if isinstance(val, dict):
      return val
  return {}


def _first_str(*values: Any, default: str = "") -> str:
  for val in values:
    if val is not None:
      s = str(val).strip()
      if s:
        return s
  return default


def _opt_str(val: Any) -> str | None:
  if val is None:
    return None
  s = str(val).strip()
  return s if s else None


def _load_yaml_dict(path: Path, *, interpolate: bool = False) -> dict[str, Any]:
  if not path.is_file():
    return {}
  with path.open("r", encoding="utf-8") as fh:
    loaded = yaml.safe_load(fh) or {}
  if not isinstance(loaded, dict):
    return {}
  return interpolate_env(loaded) if interpolate else loaded


def _find_project_root(config_path: Path | None = None) -> Path:
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
  pkg_root = Path(__file__).resolve().parents[3]
  return pkg_root if (pkg_root / "data" / "lineups").is_dir() else cwd


def load_config(
  config_path: Path | str | None = None,
  env_path: Path | str | None = None,
  credentials_path: Path | str | None = None,
) -> AppConfig:
  """Load declarative two-tier settings from `config.yaml` (+ `credentials.yaml` & `.env`).

  Args:
    config_path: Explicit `config.yaml` path (defaults to `$BELLHOP_CONFIG` or
      `<project_root>/config.yaml`).
    env_path: Optional explicit `.env` file path.
    credentials_path: Optional explicit `credentials.yaml` path override.

  Returns:
    Validated `AppConfig` instance.

  Raises:
    FileNotFoundError: When an explicit `config_path` does not exist.
    ValueError: When `trusted_proxy_ips` or numeric fields are invalid.
  """
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

  raw = interpolate_env(pre_raw)

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
  web_srv_creds = _subdict(loaded_creds, "web_server")
  xt_srv_creds = _subdict(loaded_creds, "xtream_server")
  xt_creds = _subdict(loaded_creds, "upstream_xtream", "xtream") or loaded_creds

  # Canonical two-tier `config.yaml` sections:
  # Tier 1: `paths`, `web_server`
  # Tier 2: `upstream_xtream`, `xtream_server`, `pipeline`
  paths_raw = _subdict(raw, "paths")
  web_srv_raw = _subdict(raw, "web_server")
  xt_raw = _subdict(raw, "upstream_xtream")
  xt_srv_raw = _subdict(raw, "xtream_server")
  pipe_raw = _subdict(raw, "pipeline")
  lu_raw = _subdict(pipe_raw, "lineup")
  match_raw = _subdict(pipe_raw, "matching")
  val_raw = _subdict(pipe_raw, "validation")
  epg_raw = _subdict(pipe_raw, "epg")
  out_raw = _subdict(pipe_raw, "output")

  seed_json_raw = paths_raw.get("seed_json")
  xtream = XtreamConfig(
    server_url=_first_str(
      xt_raw.get("server_url"),
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
    cache_dir=Path(_first_str(paths_raw.get("cache_dir"), default="cache")),
    cache_ttl_hours=float(xt_raw.get("refresh_hours", 24.0)),
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
    detect_black_screen=bool(val_raw.get("detect_black_screen", True)),
    black_sample_sec=max(0.5, float(val_raw.get("black_sample_sec", 1.5))),
    check_audio_language=bool(val_raw.get("check_audio_language", True)),
    max_candidates_per_channel=max(
      1, int(val_raw.get("max_candidates_per_channel", 5))
    ),
  )

  sd_cfg_raw = _subdict(lu_raw, "schedules_direct")
  tvg_cfg_raw = _subdict(lu_raw, "tvguide")
  iptv_org_raw = _subdict(lu_raw, "iptv_org")
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
    iptv_org=IptvOrgConfig(
      enabled=bool(iptv_org_raw.get("enabled", True)),
      base_url=_first_str(
        iptv_org_raw.get("base_url"),
        default=DEFAULT_IPTV_ORG_API_URL,
      ),
      recheck_hours=max(0.0, float(iptv_org_raw.get("recheck_hours", 24.0))),
    ),
  )

  state_dir_path = Path(_first_str(paths_raw.get("state_dir"), default="state"))
  state = StateConfig(
    state_dir=state_dir_path,
    mapping_path=Path(
      _first_str(
        paths_raw.get("mapping_path"),
        default=str(state_dir_path / "channel_mapping.json"),
      )
    ),
    upgrade_hysteresis_score=float(
      match_raw.get("upgrade_hysteresis_score", 5.0)
    ),
  )

  epg_xml_raw = paths_raw.get("xmltv_file")
  epg = EpgConfig(
    verify=bool(epg_raw.get("verify", True)),
    refresh_hours=float(epg_raw.get("refresh_hours", 6.0)),
    xmltv_file=Path(str(epg_xml_raw)) if epg_xml_raw else None,
  )

  output = OutputConfig(
    m3u8_enabled=bool(out_raw.get("m3u8_enabled", True)),
    playlist_path=Path(
      _first_str(
        paths_raw.get("m3u8_playlist_path"),
        default="output/bellhop.m3u8",
      )
    ),
    report_path=Path(
      _first_str(
        paths_raw.get("m3u8_report_path"),
        default="output/lineup_report.json",
      )
    ),
    xtream_enabled=bool(out_raw.get("xtream_enabled", True)),
    xtream_dir=Path(
      _first_str(
        paths_raw.get("xtream_output_dir"),
        default="output/xtream",
      )
    ),
    include_unmatched_comment=bool(
      out_raw.get("include_unmatched_comment", False)
    ),
  )

  web_host_raw = _opt_str(web_srv_raw.get("host"))
  web_port_raw = web_srv_raw.get("port")
  default_serve_host = web_host_raw or "0.0.0.0"
  default_serve_port = int(web_port_raw) if web_port_raw is not None else 8080
  default_settle_ms = max(0, int(web_srv_raw.get("settle_ms", 180)))

  raw_proxy_ips = web_srv_raw.get("trusted_proxy_ips")
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
  parse_trusted_proxy_networks(web_trusted_proxy_ips)

  xt_port_raw = xt_srv_raw.get("port")
  pub_port_raw = xt_srv_raw.get("public_port")

  serve = ServeConfig(
    host=default_serve_host,
    port=default_serve_port,
    web=ServeWebConfig(
      enabled=bool(web_srv_raw.get("enabled", True)),
      host=web_host_raw,
      port=int(web_port_raw) if web_port_raw is not None else None,
      settle_ms=default_settle_ms,
      username=_first_str(
        web_srv_raw.get("username"),
        web_srv_creds.get("username"),
        os.environ.get("BELLHOP_WEB_USERNAME"),
      ),
      password=_first_str(
        web_srv_raw.get("password"),
        web_srv_creds.get("password"),
        os.environ.get("BELLHOP_WEB_PASSWORD"),
      ),
      session_ttl_hours=float(web_srv_raw.get("session_ttl_hours", 168.0)),
      session_secret=_first_str(
        web_srv_raw.get("session_secret"),
        web_srv_creds.get("session_secret"),
        os.environ.get("BELLHOP_WEB_SESSION_SECRET"),
      ),
      trust_proxy_headers=bool(web_srv_raw.get("trust_proxy_headers", False)),
      trusted_proxy_ips=list(web_trusted_proxy_ips),
    ),
    xtream=ServeXtreamConfig(
      enabled=bool(xt_srv_raw.get("enabled", True)),
      host=_opt_str(xt_srv_raw.get("host")),
      port=int(xt_port_raw) if xt_port_raw is not None else None,
      username=_first_str(
        xt_srv_raw.get("username"),
        xt_srv_creds.get("username"),
        os.environ.get("BELLHOP_SERVE_USERNAME"),
      ),
      password=_first_str(
        xt_srv_raw.get("password"),
        xt_srv_creds.get("password"),
        os.environ.get("BELLHOP_SERVE_PASSWORD"),
      ),
      public_host=_opt_str(xt_srv_raw.get("public_host")),
      public_port=int(pub_port_raw) if pub_port_raw is not None else None,
      server_protocol=_first_str(
        xt_srv_raw.get("server_protocol"),
        default="http",
      ).lower(),
    ),
  )

  pkg_root = Path(__file__).resolve().parents[3]
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
    serve=serve,
  )
