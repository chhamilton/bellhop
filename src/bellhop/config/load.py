"""File-backed YAML loader for `config.yaml` and `credentials.yaml`."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from io import StringIO
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from bellhop.common.accounts import (
  UserAccount,
  generate_stream_credential,
  hash_web_password,
  validate_accounts_collection,
  validate_email,
)
from bellhop.common.file_utils import write_private_text
from bellhop.common.http_utils import (
  DEFAULT_TRUSTED_PROXY_IPS,
  normalize_public_base_urls,
  parse_trusted_proxy_networks,
)
from bellhop.common.mailer import (
  DEFAULT_SMTP_HOST,
  DEFAULT_SMTP_PORT,
  DEFAULT_SMTP_TIMEOUT_SEC,
  VALID_TLS_MODES,
  MailerConfigError,
  SmtpConfig,
)
from bellhop.config.model import (
  DEFAULT_BELL_FIBE_MTL_LINEUP_ID,
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
  ValidationConfig,
  XtreamConfig,
)


def _subdict(mapping: Mapping[str, Any], *keys: str) -> dict[str, Any]:
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


def _make_ruamel() -> YAML:
  ryaml = YAML()
  ryaml.preserve_quotes = True
  ryaml.indent(mapping=2, sequence=4, offset=2)
  ryaml.width = 4096
  return ryaml


def _load_yaml_dict(path: Path) -> dict[str, Any]:
  if not path.is_file():
    raise FileNotFoundError(f"Configuration file not found: {path}")
  with path.open("r", encoding="utf-8") as fh:
    loaded = yaml.safe_load(fh)
  if loaded is None:
    return {}
  if not isinstance(loaded, dict):
    raise ValueError(f"Expected a YAML mapping in {path}.")
  return loaded


def find_project_root(config_path: Path | None = None) -> Path:
  """Locate the workspace/project root directory containing `data/lineups`."""
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


_find_project_root = find_project_root


def resolve_config_targets(
  config_path: Path | str | None = None,
) -> tuple[Path, Path | None, Path, Path]:
  """Resolve `(root, explicit_cfg, yaml_target, base_dir)` for config loading.

  Raises:
    FileNotFoundError: When `config.yaml` (explicit or default) does not exist.
  """
  raw_cfg_path = config_path or os.environ.get("BELLHOP_CONFIG") or None
  explicit_cfg = Path(raw_cfg_path).resolve() if raw_cfg_path else None
  if explicit_cfg is not None and not explicit_cfg.is_file():
    raise FileNotFoundError(f"Configuration file not found: {explicit_cfg}")

  root = _find_project_root(explicit_cfg)
  yaml_target = (
    explicit_cfg if explicit_cfg is not None else (root / "config.yaml")
  )
  if not yaml_target.is_file():
    raise FileNotFoundError(f"Configuration file not found: {yaml_target}")
  base_dir = yaml_target.parent
  return root, explicit_cfg, yaml_target, base_dir


def resolve_credentials_path(
  pre_raw: Mapping[str, Any],
  *,
  root: Path,
  base_dir: Path,
  credentials_path: Path | str | None = None,
) -> Path:
  """Resolve the backing `credentials.yaml` path from `config.yaml`."""
  if credentials_path is not None:
    return Path(credentials_path)
  raw_creds_val = str(
    pre_raw.get("credentials_file") or "credentials.yaml"
  ).strip()
  cfg_creds_ref = Path(raw_creds_val or "credentials.yaml")
  creds_target = (
    cfg_creds_ref if cfg_creds_ref.is_absolute() else (base_dir / cfg_creds_ref)
  )
  if not creds_target.is_file() and not cfg_creds_ref.is_absolute():
    alt_target = root / cfg_creds_ref
    if alt_target.is_file():
      return alt_target
  return creds_target


def _parse_and_normalize_account_entry(
  raw_id: str,
  entry: MutableMapping[str, Any],
  *,
  reserved_xt_users: set[str],
) -> tuple[UserAccount, bool]:
  dirty = False
  aid = str(raw_id).strip()
  if not aid:
    raise ValueError("Account key under `accounts:` cannot be empty.")

  raw_plain = entry.get("web_password")
  if raw_plain is None and "password" in entry:
    raw_plain = entry.get("password")
  plain_pw = str(raw_plain) if raw_plain is not None else ""
  if plain_pw.strip():
    pw_hash = hash_web_password(plain_pw)
    entry["web_password_hash"] = DoubleQuotedScalarString(pw_hash)
    entry.pop("web_password", None)
    entry.pop("password", None)
    dirty = True
  else:
    if "web_password" in entry:
      entry.pop("web_password", None)
      dirty = True
    if "password" in entry:
      entry.pop("password", None)
      dirty = True
    pw_hash = str(entry.get("web_password_hash") or "").strip()

  xt_user = str(entry.get("xtream_username") or "").strip()
  if not xt_user:
    xt_user = generate_stream_credential(exclude=reserved_xt_users)
    reserved_xt_users.add(xt_user)
    entry["xtream_username"] = DoubleQuotedScalarString(xt_user)
    dirty = True

  xt_pass = str(entry.get("xtream_password") or "").strip()
  if not xt_pass:
    xt_pass = generate_stream_credential()
    entry["xtream_password"] = DoubleQuotedScalarString(xt_pass)
    dirty = True

  raw_email = str(entry.get("email") or "").strip()
  norm_email = validate_email(raw_email, account_id=aid)
  if raw_email != norm_email:
    entry["email"] = DoubleQuotedScalarString(norm_email)
    dirty = True

  raw_lineup = entry.get("lineup_id")
  lineup_id = _opt_str(raw_lineup)
  return (
    UserAccount(
      account_id=aid,
      email=norm_email,
      web_password_hash=pw_hash,
      xtream_username=xt_user,
      xtream_password=xt_pass,
      upstream_username=str(entry.get("upstream_username") or "").strip(),
      upstream_password=str(entry.get("upstream_password") or ""),
      pipeline=bool(entry.get("pipeline", False)),
      lineup_id=lineup_id,
    ),
    dirty,
  )


def load_and_normalize_credentials(
  creds_path: Path, *, persist_updates: bool = True
) -> tuple[dict[str, Any], tuple[UserAccount, ...]]:
  """Load `credentials.yaml`, auto-hashing passwords & filling stream creds.

  Any plaintext `web_password` field is hashed into `web_password_hash` and
  deleted cleanly while preserving YAML comments and unrelated keys. Missing
  `xtream_username` or `xtream_password` fields are auto-populated with fresh
  8-character lowercase alphanumeric credentials. All uniqueness/collision
  checks run before persisting updates back to `creds_path` (`0600`).

  Args:
    creds_path: Path to `credentials.yaml`.
    persist_updates: When `True`, rewrite `creds_path` atomically if normalized.

  Returns:
    Tuple `(raw_creds_dict, validated_accounts)`.

  Raises:
    FileNotFoundError: When `creds_path` does not exist.
    ValueError: On malformed YAML, missing required fields, or account
      collisions (`email`, `xtream_username`, `upstream_username`).
  """
  if not creds_path.is_file():
    raise FileNotFoundError(f"Credentials file not found: {creds_path}")

  ryaml = _make_ruamel()
  raw_text = creds_path.read_text(encoding="utf-8")
  doc = ryaml.load(raw_text)
  if doc is None:
    doc = CommentedMap()
  if not isinstance(doc, MutableMapping):
    raise ValueError(f"Expected a YAML mapping in {creds_path}.")

  raw_accounts = doc.get("accounts")
  if not isinstance(raw_accounts, MutableMapping) or not raw_accounts:
    raise ValueError(
      f"{creds_path}: missing required non-empty `accounts:` mapping."
    )

  reserved_xt_users: set[str] = {
    str(entry.get("xtream_username") or "").strip()
    for entry in raw_accounts.values()
    if isinstance(entry, Mapping) and str(entry.get("xtream_username") or "")
  }

  dirty = False
  parsed_accounts: list[UserAccount] = []
  for raw_id, raw_entry in raw_accounts.items():
    if not isinstance(raw_entry, MutableMapping):
      raise ValueError(
        f"{creds_path}: account {raw_id!r} must be a YAML mapping."
      )
    acct, entry_dirty = _parse_and_normalize_account_entry(
      str(raw_id), raw_entry, reserved_xt_users=reserved_xt_users
    )
    dirty = dirty or entry_dirty
    parsed_accounts.append(acct)

  validated = validate_accounts_collection(
    parsed_accounts, require_non_empty=True
  )

  if dirty and persist_updates:
    buf = StringIO()
    ryaml.dump(doc, buf)
    write_private_text(creds_path, buf.getvalue())

  plain_dict = yaml.safe_load(raw_text) if not dirty else None
  if plain_dict is None:
    buf_plain = StringIO()
    ryaml.dump(doc, buf_plain)
    plain_dict = yaml.safe_load(buf_plain.getvalue()) or {}
  if not isinstance(plain_dict, dict):
    plain_dict = {}
  return plain_dict, validated


def _build_xtream_and_validation_cfg(
  paths_raw: Mapping[str, Any],
  xt_raw: Mapping[str, Any],
  val_raw: Mapping[str, Any],
) -> tuple[Path, XtreamConfig, ValidationConfig]:
  cache_dir_path = Path(_first_str(paths_raw.get("cache_dir"), default="cache"))
  seed_json_raw = paths_raw.get("seed_json")
  xtream = XtreamConfig(
    server_url=_first_str(xt_raw.get("server_url")),
    stream_format=_first_str(
      xt_raw.get("stream_format"),
      default="ts",
    ).lstrip("."),
    request_timeout_sec=float(xt_raw.get("request_timeout_sec", 90.0)),
    cache_dir=cache_dir_path,
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
  return cache_dir_path, xtream, validation


def _build_lineup_cfg(
  paths_raw: Mapping[str, Any],
  lu_raw: Mapping[str, Any],
  sd_creds: Mapping[str, Any],
) -> LineupConfig:
  sd_cfg_raw = _subdict(lu_raw, "schedules_direct")
  iptv_org_raw = _subdict(lu_raw, "iptv_org")
  collapse_sd = bool(lu_raw.get("collapse_sd_dupes", False))
  raw_num_mode = str(
    lu_raw.get("numbering_mode") or ("hd" if collapse_sd else "both")
  ).lower()
  return LineupConfig(
    backend=str(lu_raw.get("backend", "auto"))
    .strip()
    .lower()
    .replace("-", "_"),
    lineup_id=str(
      lu_raw.get("lineup_id", DEFAULT_BELL_FIBE_MTL_LINEUP_ID)
    ).strip(),
    postal_code=str(lu_raw.get("postal_code", "")).strip(),
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
        sd_creds.get("username"),
        sd_cfg_raw.get("username"),
      ),
      password=_first_str(
        sd_creds.get("password"),
        sd_cfg_raw.get("password"),
      ),
      auto_add_lineup=bool(
        sd_cfg_raw.get(
          "auto_add_lineup",
          sd_creds.get("auto_add_lineup", False),
        )
      ),
    ),
    iptv_org=IptvOrgConfig(
      enabled=bool(iptv_org_raw.get("enabled", True)),
      recheck_hours=max(0.0, float(iptv_org_raw.get("recheck_hours", 24.0))),
    ),
  )


def _build_serve_cfg(
  web_srv_raw: Mapping[str, Any],
  xt_srv_raw: Mapping[str, Any],
) -> ServeConfig:
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

  if "public_base_url" in web_srv_raw:
    raise ValueError(
      "Use `web_server.public_base_urls` (a YAML list) instead of "
      "`public_base_url`."
    )
  raw_public_base_urls = web_srv_raw.get("public_base_urls")
  if raw_public_base_urls is None:
    public_base_urls: list[str] = []
  elif isinstance(raw_public_base_urls, list):
    for item in raw_public_base_urls:
      if not isinstance(item, str):
        raise ValueError(
          "Each entry in `web_server.public_base_urls` must be a URL string."
        )
    public_base_urls = list(normalize_public_base_urls(raw_public_base_urls))
  else:
    raise ValueError(
      "web_server.public_base_urls must be a list of http(s) base URLs."
    )

  xt_port_raw = xt_srv_raw.get("port")
  pub_port_raw = xt_srv_raw.get("public_port")
  return ServeConfig(
    host=default_serve_host,
    port=default_serve_port,
    web=ServeWebConfig(
      enabled=bool(web_srv_raw.get("enabled", True)),
      host=web_host_raw,
      port=int(web_port_raw) if web_port_raw is not None else None,
      settle_ms=default_settle_ms,
      session_ttl_hours=float(web_srv_raw.get("session_ttl_hours", 168.0)),
      public_base_urls=public_base_urls,
      trust_proxy_headers=bool(web_srv_raw.get("trust_proxy_headers", False)),
      trusted_proxy_ips=list(web_trusted_proxy_ips),
    ),
    xtream=ServeXtreamConfig(
      enabled=bool(xt_srv_raw.get("enabled", True)),
      host=_opt_str(xt_srv_raw.get("host")),
      port=int(xt_port_raw) if xt_port_raw is not None else None,
      public_host=_opt_str(xt_srv_raw.get("public_host")),
      public_port=int(pub_port_raw) if pub_port_raw is not None else None,
      server_protocol=_first_str(
        xt_srv_raw.get("server_protocol"),
        default="http",
      ).lower(),
    ),
  )


def _build_email_cfg(
  email_raw: Mapping[str, Any],
  email_creds: Mapping[str, Any],
) -> SmtpConfig:
  port_raw = email_raw.get("smtp_port", DEFAULT_SMTP_PORT)
  if isinstance(port_raw, bool):
    raise ValueError("`email.smtp_port` must be an integer port.")
  tls_mode = _first_str(email_raw.get("tls_mode"), default="starttls").lower()
  raw_pw = email_creds.get("password")
  if raw_pw is None or str(raw_pw) == "":
    raw_pw = email_creds.get("app_password")
  pw = str(raw_pw) if raw_pw is not None else ""
  raw_from_name = email_raw.get("from_name")
  from_name = "Bellhop" if raw_from_name is None else str(raw_from_name).strip()
  cfg = SmtpConfig(
    enabled=bool(email_raw.get("enabled", False)),
    smtp_host=_first_str(email_raw.get("smtp_host"), default=DEFAULT_SMTP_HOST),
    smtp_port=int(port_raw),
    tls_mode=tls_mode,
    username=_first_str(email_creds.get("username")),
    password=pw,
    from_name=from_name,
    from_address=_first_str(email_raw.get("from_address")),
    timeout_sec=float(email_raw.get("timeout_sec", DEFAULT_SMTP_TIMEOUT_SEC)),
  )
  if cfg.smtp_port < 1 or cfg.smtp_port > 65535:
    raise ValueError("`email.smtp_port` must be between 1 and 65535.")
  if cfg.tls_mode not in VALID_TLS_MODES:
    allowed = ", ".join(sorted(VALID_TLS_MODES))
    raise ValueError(f"`email.tls_mode` must be one of {allowed}.")
  if cfg.enabled:
    try:
      cfg.validate(require_enabled=True)
    except MailerConfigError as exc:
      raise ValueError(str(exc)) from exc
  return cfg


def build_config_from_raw_dict(
  pre_raw: Mapping[str, Any],
  *,
  root: Path,
  base_dir: Path,
  config_file: Path | None,
  is_explicit: bool = False,
  credentials_path: Path | str | None = None,
  persist_credential_updates: bool = True,
) -> AppConfig:
  """Validate and construct an `AppConfig` from a parsed `config.yaml` dict."""
  raw = dict(pre_raw)
  creds_target = resolve_credentials_path(
    raw,
    root=root,
    base_dir=base_dir,
    credentials_path=credentials_path,
  )
  loaded_creds, accounts = load_and_normalize_credentials(
    creds_target, persist_updates=persist_credential_updates
  )
  sd_creds = _subdict(loaded_creds, "schedules_direct")
  email_creds = _subdict(loaded_creds, "email")

  paths_raw = _subdict(raw, "paths")
  web_srv_raw = _subdict(raw, "web_server")
  xt_raw = _subdict(raw, "upstream_xtream")
  xt_srv_raw = _subdict(raw, "xtream_server")
  email_raw = _subdict(raw, "email")
  pipe_raw = _subdict(raw, "pipeline")
  lu_raw = _subdict(pipe_raw, "lineup")
  match_raw = _subdict(pipe_raw, "matching")
  val_raw = _subdict(pipe_raw, "validation")
  epg_raw = _subdict(pipe_raw, "epg")
  out_raw = _subdict(pipe_raw, "output")

  cache_dir_path, xtream, validation = _build_xtream_and_validation_cfg(
    paths_raw, xt_raw, val_raw
  )
  lineup = _build_lineup_cfg(paths_raw, lu_raw, sd_creds)
  serve = _build_serve_cfg(web_srv_raw, xt_srv_raw)
  email = _build_email_cfg(email_raw, email_creds)

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
    report_path=Path(
      _first_str(
        paths_raw.get("report_path"),
        default="output/lineup_report.json",
      )
    ),
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

  pkg_root = Path(__file__).resolve().parents[3]
  effective_root = (
    base_dir if (is_explicit and not (base_dir / "src").is_dir()) else root
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
    config_file=config_file,
    credentials_file=creds_target,
    cache_dir=cache_dir_path,
    accounts=accounts,
    xtream=xtream,
    validation=validation,
    lineup=lineup,
    state=state,
    epg=epg,
    output=output,
    serve=serve,
    email=email,
  )


def load_config(
  config_path: Path | str | None = None,
  *,
  credentials_path: Path | str | None = None,
  persist_credential_updates: bool = True,
) -> AppConfig:
  """Load configuration and accounts from `config.yaml` and `credentials.yaml`.

  Args:
    config_path: Explicit `config.yaml` path (defaults to `$BELLHOP_CONFIG` or
      `<project_root>/config.yaml`).
    credentials_path: Optional explicit `credentials.yaml` override path.
    persist_credential_updates: When `True`, hash any plaintext `web_password`
      entries and generate missing 8-char Xtream credentials on disk.

  Returns:
    Validated `AppConfig` instance.

  Raises:
    FileNotFoundError: When `config.yaml` or `credentials.yaml` does not exist.
    ValueError: On invalid YAML values or account collisions.
  """
  root, explicit_cfg, yaml_target, base_dir = resolve_config_targets(
    config_path
  )
  pre_raw = _load_yaml_dict(yaml_target)
  return build_config_from_raw_dict(
    pre_raw,
    root=root,
    base_dir=base_dir,
    config_file=yaml_target,
    is_explicit=explicit_cfg is not None,
    credentials_path=credentials_path,
    persist_credential_updates=persist_credential_updates,
  )
