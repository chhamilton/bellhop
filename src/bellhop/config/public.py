"""Public Tier-2 operational config serializer and comment-preserving
updater.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Mapping, MutableMapping
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import (
  DoubleQuotedScalarString,
  SingleQuotedScalarString,
)

from bellhop.common.api_contracts import (
  PublicConfigPayload,
  PublicEpgDict,
  PublicIptvOrgDict,
  PublicLineupDict,
  PublicMatchingDict,
  PublicOutputDict,
  PublicPipelineDict,
  PublicSchedulesDirectDict,
  PublicUpstreamXtreamDict,
  PublicValidationDict,
)
from bellhop.common.file_utils import PRIVATE_FILE_MODE, ensure_private_dir
from bellhop.config.load import (
  build_config_from_raw_dict,
  find_project_root,
)
from bellhop.config.model import AppConfig

__all__ = [
  "PublicConfigPayload",
  "PublicEpgDict",
  "PublicIptvOrgDict",
  "PublicLineupDict",
  "PublicMatchingDict",
  "PublicOutputDict",
  "PublicPipelineDict",
  "PublicSchedulesDirectDict",
  "PublicUpstreamXtreamDict",
  "PublicValidationDict",
  "apply_public_config_update",
  "export_public_config",
]


def export_public_config(config: AppConfig) -> PublicConfigPayload:
  """Serialize Tier-2 operational settings for the Web UI Settings panel.

  Strictly exposes ONLY:
  - `upstream_xtream` (non-credential upstream IPTV settings)
  - `pipeline` (lineup, matching, validation, EPG, and output toggles)

  Strictly excludes `web_server`, `xtream_server`, `paths`, `credentials_file`,
  and all secrets.

  Args:
    config: Loaded `AppConfig` instance.

  Returns:
    JSON-serializable `PublicConfigPayload`.
  """
  return {
    "upstream_xtream": {
      "server_url": config.xtream.server_url,
      "stream_format": config.xtream.stream_format,
      "request_timeout_sec": config.xtream.request_timeout_sec,
      "refresh_hours": config.xtream.cache_ttl_hours,
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
        "schedules_direct": {
          "auto_add_lineup": config.lineup.schedules_direct.auto_add_lineup,
        },
        "iptv_org": {
          "enabled": config.lineup.iptv_org.enabled,
          "recheck_hours": config.lineup.iptv_org.recheck_hours,
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
        "detect_black_screen": config.validation.detect_black_screen,
        "black_sample_sec": config.validation.black_sample_sec,
        "check_audio_language": config.validation.check_audio_language,
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


def _validate_xtream_server_url(raw_val: Any) -> str:
  raw = str(raw_val or "").strip()
  if not raw:
    return ""
  if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in raw):
    raise ValueError(
      "upstream_xtream.server_url must not contain spaces or control characters."
    )
  candidate = raw if "://" in raw else f"http://{raw}"
  try:
    parsed = urlsplit(candidate)
  except ValueError as exc:
    raise ValueError("upstream_xtream.server_url is not a valid URL.") from exc
  if parsed.scheme.lower() not in {"http", "https"}:
    raise ValueError("upstream_xtream.server_url must use http:// or https://.")
  if "@" in parsed.netloc or parsed.username or parsed.password:
    raise ValueError(
      "upstream_xtream.server_url must not contain embedded credentials."
    )
  if not parsed.hostname:
    raise ValueError(
      "upstream_xtream.server_url must contain a valid hostname or IP."
    )
  if parsed.query or parsed.fragment:
    raise ValueError(
      "upstream_xtream.server_url must not contain a query string or fragment."
    )
  return raw


def _to_plain_data(val: Any) -> Any:
  if isinstance(val, Mapping):
    return {
      (
        k if isinstance(k, int) and not isinstance(k, bool) else str(k)
      ): _to_plain_data(v)
      for k, v in val.items()
    }
  if isinstance(val, list):
    return [_to_plain_data(item) for item in val]
  if isinstance(val, bool) or val is None:
    return val
  if isinstance(val, int):
    return int(val)
  if isinstance(val, float):
    return float(val)
  if isinstance(val, str):
    return str(val)
  return val


_UNSET = object()


def _build_roundtrip_yaml() -> YAML:
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
  *,
  before_config: AppConfig | None = None,
) -> tuple[AppConfig, bool]:
  """Validate and atomically merge operational settings into two-tier
  `config.yaml`.

  Preserves YAML comments, quotes, and `${ENV}` placeholders via `ruamel.yaml`.
  Strictly scopes updates to `upstream_xtream` and `pipeline`.
  Never modifies `web_server`, `xtream_server`, `paths`, `credentials_file`,
  or any credentials.

  Args:
    config_path: Target `config.yaml` path to update.
    updates: Parsed JSON object matching `PublicConfigPayload` (or a subset).
    before_config: Optional pre-loaded `AppConfig` to skip initial YAML parse.

  Returns:
    Tuple of `(reloaded_config, restart_required)`.

  Raises:
    ValueError: When `updates` fails schema or range validation.
  """
  if not isinstance(updates, Mapping):
    raise ValueError("Configuration update payload must be a JSON object.")

  target = config_path.resolve()
  target_exists = target.is_file()
  root = find_project_root(target if target_exists else None)
  rt_yaml, raw = _load_roundtrip_yaml(target)
  before_cfg = (
    before_config
    if before_config is not None
    else (
      build_config_from_raw_dict(
        _to_plain_data(raw),
        root=root,
        base_dir=target.parent,
        config_file=target,
        is_explicit=True,
      )
      if target_exists
      else AppConfig(project_root=target.parent)
    )
  )

  # 1. upstream_xtream (non-secret, non-path fields only)
  u_xt = updates.get("upstream_xtream")
  if isinstance(u_xt, dict):
    xt = _ensure_dict(raw, "upstream_xtream")
    if "server_url" in u_xt:
      _set_rt_value(
        xt,
        "server_url",
        _validate_xtream_server_url(u_xt["server_url"]),
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

  # 2. pipeline (non-secret, non-path settings; provider URLs constant)
  u_pipe = updates.get("pipeline")
  if isinstance(u_pipe, dict):
    pipe = _ensure_dict(raw, "pipeline")

    if isinstance(u_pipe.get("lineup"), dict):
      u_lu = u_pipe["lineup"]
      lu = _ensure_dict(pipe, "lineup")
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
          current_resolved=dict(before_cfg.lineup.pinned_streams),
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
          current_resolved=sorted(before_cfg.lineup.excluded_channels),
        )
      if isinstance(u_lu.get("schedules_direct"), dict):
        sd = _ensure_dict(lu, "schedules_direct")
        if "auto_add_lineup" in u_lu["schedules_direct"]:
          _set_rt_value(
            sd,
            "auto_add_lineup",
            bool(u_lu["schedules_direct"]["auto_add_lineup"]),
            current_resolved=before_cfg.lineup.schedules_direct.auto_add_lineup,
          )
      if isinstance(u_lu.get("iptv_org"), dict):
        u_io = u_lu["iptv_org"]
        io_d = _ensure_dict(lu, "iptv_org")
        if "enabled" in u_io:
          _set_rt_value(
            io_d,
            "enabled",
            bool(u_io["enabled"]),
            current_resolved=before_cfg.lineup.iptv_org.enabled,
          )
        if "recheck_hours" in u_io:
          _set_rt_value(
            io_d,
            "recheck_hours",
            _pos_float(
              u_io["recheck_hours"],
              "pipeline.lineup.iptv_org.recheck_hours",
              allow_zero=True,
            ),
            current_resolved=before_cfg.lineup.iptv_org.recheck_hours,
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
      _set_rt_value(
        _ensure_dict(pipe, "matching"),
        "upgrade_hysteresis_score",
        hyst,
        current_resolved=before_cfg.state.upgrade_hysteresis_score,
      )

    if isinstance(u_pipe.get("validation"), dict):
      u_val = u_pipe["validation"]
      val = _ensure_dict(pipe, "validation")
      for bkey in (
        "enabled",
        "only_changed",
        "ignore_account_limit",
        "use_ffprobe",
        "detect_black_screen",
        "check_audio_language",
      ):
        if bkey in u_val:
          _set_rt_value(
            val,
            bkey,
            bool(u_val[bkey]),
            current_resolved=getattr(before_cfg.validation, bkey),
          )
      if "concurrency" in u_val:
        _set_rt_value(
          val,
          "concurrency",
          _pos_int(
            u_val["concurrency"], "pipeline.validation.concurrency", minimum=1
          ),
          current_resolved=before_cfg.validation.concurrency,
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
          current_resolved=before_cfg.validation.inter_probe_delay_sec,
        )
      if "http_timeout_sec" in u_val:
        _set_rt_value(
          val,
          "http_timeout_sec",
          _pos_float(
            u_val["http_timeout_sec"], "pipeline.validation.http_timeout_sec"
          ),
          current_resolved=before_cfg.validation.http_timeout_sec,
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
          current_resolved=before_cfg.validation.min_bytes_to_read,
        )
      if "ffprobe_timeout_sec" in u_val:
        _set_rt_value(
          val,
          "ffprobe_timeout_sec",
          _pos_float(
            u_val["ffprobe_timeout_sec"],
            "pipeline.validation.ffprobe_timeout_sec",
          ),
          current_resolved=before_cfg.validation.ffprobe_timeout_sec,
        )
      if "black_sample_sec" in u_val:
        bs_sec = _pos_float(
          u_val["black_sample_sec"],
          "pipeline.validation.black_sample_sec",
        )
        if bs_sec < 0.5:
          raise ValueError(
            "pipeline.validation.black_sample_sec must be >= 0.5."
          )
        _set_rt_value(
          val,
          "black_sample_sec",
          bs_sec,
          current_resolved=before_cfg.validation.black_sample_sec,
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
          current_resolved=before_cfg.validation.max_candidates_per_channel,
        )

    if isinstance(u_pipe.get("epg"), dict):
      u_epg = u_pipe["epg"]
      epg_d = _ensure_dict(pipe, "epg")
      if "verify" in u_epg:
        _set_rt_value(
          epg_d,
          "verify",
          bool(u_epg["verify"]),
          current_resolved=before_cfg.epg.verify,
        )
      if "refresh_hours" in u_epg:
        _set_rt_value(
          epg_d,
          "refresh_hours",
          _pos_float(
            u_epg["refresh_hours"],
            "pipeline.epg.refresh_hours",
            allow_zero=True,
          ),
          current_resolved=before_cfg.epg.refresh_hours,
        )

    if isinstance(u_pipe.get("output"), dict):
      u_out = u_pipe["output"]
      out_d = _ensure_dict(pipe, "output")
      if "include_unmatched_comment" in u_out:
        _set_rt_value(
          out_d,
          "include_unmatched_comment",
          bool(u_out["include_unmatched_comment"]),
          current_resolved=before_cfg.output.include_unmatched_comment,
        )
      if "m3u8_enabled" in u_out:
        _set_rt_value(
          out_d,
          "m3u8_enabled",
          bool(u_out["m3u8_enabled"]),
          current_resolved=before_cfg.output.m3u8_enabled,
        )
      if "xtream_enabled" in u_out:
        _set_rt_value(
          out_d,
          "xtream_enabled",
          bool(u_out["xtream_enabled"]),
          current_resolved=before_cfg.output.xtream_enabled,
        )

  validated = build_config_from_raw_dict(
    _to_plain_data(raw),
    root=root,
    base_dir=target.parent,
    config_file=target,
    is_explicit=True,
  )
  if not validated.serve.web.enabled and not validated.serve.xtream.enabled:
    raise ValueError("At least one server must remain enabled.")

  ensure_private_dir(target.parent)
  tmp_path = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
  buf = StringIO()
  rt_yaml.dump(raw, buf)
  fd = os.open(
    str(tmp_path),
    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    PRIVATE_FILE_MODE,
  )
  try:
    with os.fdopen(fd, "wb") as fh:
      fh.write(buf.getvalue().encode("utf-8"))
    with contextlib.suppress(OSError):
      os.chmod(tmp_path, PRIVATE_FILE_MODE)
    os.replace(tmp_path, target)
  except BaseException:
    with contextlib.suppress(OSError):
      tmp_path.unlink(missing_ok=True)
    raise

  restart_required = bool(
    validated.xtream.server_url != before_cfg.xtream.server_url
  )
  return validated, restart_required
