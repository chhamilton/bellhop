"""Single-source-of-truth HTTP JSON contracts, JSON Schema, and TS codegen.

Defines the wire `TypedDict` schemas shared across `bellhop.web`, `bellhop.epg`,
and `bellhop.pipeline`, generates
`src/bellhop/web/frontend/api_contracts.d.ts`, emits Draft 2020-12 JSON
Schemas, and validates live HTTP responses against those schemas in tests.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from typing import Any, TypedDict, Union, get_args, get_origin, get_type_hints


class _ProgramItemRequiredDict(TypedDict):
  title: str
  start_timestamp: int
  stop_timestamp: int


class ProgramItemDict(_ProgramItemRequiredDict, total=False):
  """Program schedule entry in `/api/lineup` and `/api/epg/<chno>`."""

  id: str
  channel_id: str
  sub_title: str
  description: str
  category: str
  progress_pct: float
  is_live: bool


class GuideCategoryDict(TypedDict):
  """Serialized lineup category returned in `/api/lineup`."""

  category_id: str
  category_name: str
  channel_count: int


class PublicChannelDict(TypedDict):
  """Browser-safe channel payload returned in `/api/lineup`."""

  chno: int
  sd_chno: int | None
  name: str
  callsign: str | None
  logo: str
  category_id: str
  category_name: str
  language: str
  quality_tier: str
  epg_channel_id: str
  has_native_epg: bool
  stream_id: int
  stream_url: str
  hls_url: str
  now_playing: ProgramItemDict | None
  up_next: ProgramItemDict | None
  schedule: list[ProgramItemDict]


class EpgStatusDict(TypedDict):
  """EPG refresh and coverage telemetry in `/api/lineup` and `/api/health`."""

  enabled: bool
  configured: bool
  refreshing: bool
  state: str
  last_updated: float | None
  last_attempt: float | None
  last_reason: str
  last_error: str | None
  program_count: int
  active_program_count: int
  channels_with_epg: int
  max_stop_timestamp: int
  refresh_hours: float | None


class LineupMetaDict(TypedDict):
  """Lineup summary block returned inside `GET /api/lineup`."""

  lineup_id: str
  provider: str
  market: str
  postal_prefix: str
  timezone: str
  lineup_updated_at: float
  total_channels: int
  category_channels: int
  filtered_channels: int


class LineupApiResponseDict(TypedDict):
  """Top-level response payload for `GET /api/lineup`."""

  server_timestamp: int
  lineup: LineupMetaDict
  epg_status: EpgStatusDict
  categories: list[GuideCategoryDict]
  channels: list[PublicChannelDict]


class TunerActiveInfoDict(TypedDict):
  """Active single-tuner relay session telemetry exposed on `/api/health`."""

  session_id: int
  chno: int
  channel_name: str
  mode: str
  uptime_sec: float


class HealthApiResponseDict(TypedDict):
  """Response payload for `GET /api/health`."""

  status: str
  lineup_id: str
  provider: str
  market: str
  lineup_updated_at: float
  channel_count: int
  category_count: int
  epg_program_count: int
  epg_active_program_count: int
  epg_status: EpgStatusDict
  active_tuner: TunerActiveInfoDict | None
  auth_required: bool


class AuthStatusResponseDict(TypedDict):
  """Response payload for `GET /api/auth/status`."""

  auth_required: bool
  authenticated: bool
  account_id: str | None
  email: str | None
  expires_at: float | None


class ChannelEpgResponseDict(TypedDict):
  """Response payload for `GET /api/epg/<chno>`."""

  chno: int
  name: str
  epg_channel_id: str
  programs: list[ProgramItemDict]


class PlayerUrlsDict(TypedDict):
  """Ready-to-paste external IPTV player URLs scoped to the signed-in user."""

  xtream_server_url: str
  m3u_url: str
  m3u_hls_url: str
  xmltv_url: str


class AccountInfoResponseDict(TypedDict):
  """Self-service account & external player setup payload (`/api/account`)."""

  account_id: str
  email: str
  xtream_username: str
  xtream_password: str
  player_urls: PlayerUrlsDict


class PasswordChangeResponseDict(TypedDict):
  """Response payload for `POST /api/account/password`."""

  ok: bool
  email: str


# Ordered mapping from Python TypedDict class -> TypeScript interface name.
API_CONTRACT_TYPES: tuple[tuple[type[Any], str], ...] = (
  (LineupMetaDict, "LineupMeta"),
  (GuideCategoryDict, "GuideCategory"),
  (ProgramItemDict, "ProgramItem"),
  (PublicChannelDict, "PublicChannel"),
  (EpgStatusDict, "EpgStatus"),
  (TunerActiveInfoDict, "TunerActiveInfo"),
  (LineupApiResponseDict, "LineupApiResponse"),
  (HealthApiResponseDict, "HealthApiResponse"),
  (AuthStatusResponseDict, "AuthStatusResponse"),
  (ChannelEpgResponseDict, "ChannelEpgResponse"),
  (PlayerUrlsDict, "PlayerUrls"),
  (AccountInfoResponseDict, "AccountInfoResponse"),
  (PasswordChangeResponseDict, "PasswordChangeResponse"),
)

_TS_NAME_BY_CLASS: dict[type[Any], str] = dict(API_CONTRACT_TYPES)


def _is_union_origin(origin: Any) -> bool:
  return origin is Union or origin is types.UnionType


def _is_typed_dict_cls(tp: Any) -> bool:
  return isinstance(tp, type) and hasattr(tp, "__required_keys__")


def _type_to_ts(tp: Any) -> str:
  if tp is type(None):
    return "null"
  if tp is str:
    return "string"
  if tp in (int, float):
    return "number"
  if tp is bool:
    return "boolean"
  if tp in _TS_NAME_BY_CLASS:
    return _TS_NAME_BY_CLASS[tp]

  origin = get_origin(tp)
  args = get_args(tp)
  if _is_union_origin(origin):
    return " | ".join(_type_to_ts(arg) for arg in args)
  if origin is list and len(args) == 1:
    inner = _type_to_ts(args[0])
    return f"({inner})[]" if " | " in inner else f"{inner}[]"
  if origin is dict and len(args) == 2:
    return f"Record<{_type_to_ts(args[0])}, {_type_to_ts(args[1])}>"
  raise TypeError(f"Unsupported type annotation for TS codegen: {tp!r}")


def _type_to_json_schema(tp: Any) -> dict[str, Any]:
  if tp is type(None):
    return {"type": "null"}
  if tp is str:
    return {"type": "string"}
  if tp is bool:
    return {"type": "boolean"}
  if tp is int:
    return {"type": "integer"}
  if tp is float:
    return {"type": "number"}
  if tp in _TS_NAME_BY_CLASS:
    return {"$ref": f"#/$defs/{_TS_NAME_BY_CLASS[tp]}"}

  origin = get_origin(tp)
  args = get_args(tp)
  if _is_union_origin(origin):
    return {"anyOf": [_type_to_json_schema(arg) for arg in args]}
  if origin is list and len(args) == 1:
    return {"type": "array", "items": _type_to_json_schema(args[0])}
  if origin is dict and len(args) == 2 and args[0] is str:
    return {
      "type": "object",
      "additionalProperties": _type_to_json_schema(args[1]),
    }
  raise TypeError(f"Unsupported type annotation for JSON Schema: {tp!r}")


def build_api_json_schemas() -> dict[str, Any]:
  """Build a Draft 2020-12 JSON Schema `$defs` bundle for API contracts."""
  defs: dict[str, Any] = {}
  for td_cls, ts_name in API_CONTRACT_TYPES:
    hints = get_type_hints(td_cls)
    req_keys: frozenset[str] = getattr(td_cls, "__required_keys__", frozenset())
    props = {
      field_name: _type_to_json_schema(field_tp)
      for field_name, field_tp in hints.items()
    }
    schema_obj: dict[str, Any] = {
      "type": "object",
      "properties": props,
      "additionalProperties": False,
    }
    required_sorted = [k for k in hints if k in req_keys]
    if required_sorted:
      schema_obj["required"] = required_sorted
    defs[ts_name] = schema_obj
  return {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": defs,
  }


def render_typescript_api_contracts() -> str:
  """Render `src/bellhop/web/frontend/api_contracts.d.ts` from `TypedDict`s."""
  lines = [
    "/**",
    " * AUTO-GENERATED FILE — DO NOT EDIT DIRECTLY.",
    " *",
    " * Generated from Python `TypedDict` definitions in",
    " * `src/bellhop/common/api_contracts.py` via `render_typescript_api_contracts()`.",
    " */",
    "",
  ]
  for td_cls, ts_name in API_CONTRACT_TYPES:
    hints = get_type_hints(td_cls)
    opt_keys: frozenset[str] = getattr(td_cls, "__optional_keys__", frozenset())
    lines.append(f"interface {ts_name} {{")
    for field_name, field_tp in hints.items():
      opt_suffix = "?" if field_name in opt_keys else ""
      ts_type = _type_to_ts(field_tp)
      lines.append(f"  {field_name}{opt_suffix}: {ts_type};")
    lines.append("}")
    lines.append("")
  return "\n".join(lines)


def validate_typed_dict_payload(
  value: object,
  expected_type: Any,
  *,
  path: str = "$",
) -> None:
  """Validate a JSON-decoded payload recursively against a `TypedDict` type.

  Args:
    value: Decoded JSON value to validate.
    expected_type: Target `TypedDict` or field annotation.
    path: Dot-path prefix used in `AssertionError` diagnostics.

  Raises:
    AssertionError: If `value` violates any key or type constraint.
  """
  if expected_type is type(None):
    assert value is None, f"{path}: expected None, got {value!r}"
    return
  if expected_type is bool:
    assert isinstance(value, bool), f"{path}: expected bool, got {value!r}"
    return
  if expected_type is int:
    assert isinstance(value, int) and not isinstance(value, bool), (
      f"{path}: expected int, got {value!r}"
    )
    return
  if expected_type is float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool), (
      f"{path}: expected float, got {value!r}"
    )
    return
  if expected_type is str:
    assert isinstance(value, str), f"{path}: expected str, got {value!r}"
    return

  origin = get_origin(expected_type)
  args = get_args(expected_type)
  if _is_union_origin(origin):
    errors: list[str] = []
    for branch in args:
      try:
        validate_typed_dict_payload(value, branch, path=path)
        return
      except AssertionError as exc:
        errors.append(str(exc))
    raise AssertionError(
      f"{path}: value {value!r} matched no union member ({'; '.join(errors)})"
    )

  if origin is list and len(args) == 1:
    assert isinstance(value, list), f"{path}: expected list, got {value!r}"
    for idx, item in enumerate(value):
      validate_typed_dict_payload(item, args[0], path=f"{path}[{idx}]")
    return

  if origin is dict and len(args) == 2:
    assert isinstance(value, Mapping), f"{path}: expected dict, got {value!r}"
    for k, v in value.items():
      validate_typed_dict_payload(k, args[0], path=f"{path}.<key:{k!r}>")
      validate_typed_dict_payload(v, args[1], path=f"{path}[{k!r}]")
    return

  if _is_typed_dict_cls(expected_type):
    assert isinstance(value, Mapping), (
      f"{path}: expected mapping for {expected_type.__name__}, got {value!r}"
    )
    hints = get_type_hints(expected_type)
    req_keys: frozenset[str] = getattr(
      expected_type, "__required_keys__", frozenset()
    )
    missing = sorted(set(req_keys) - set(value.keys()))
    extra = sorted(set(value.keys()) - set(hints.keys()))
    assert not missing, (
      f"{path} ({expected_type.__name__}): missing required keys {missing}"
    )
    assert not extra, (
      f"{path} ({expected_type.__name__}): unexpected keys {extra}"
    )
    for k, v in value.items():
      validate_typed_dict_payload(v, hints[k], path=f"{path}.{k}")
    return

  raise TypeError(f"Unsupported schema type at {path}: {expected_type!r}")
