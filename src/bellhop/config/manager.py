"""Mtime-aware `ConfigManager` caching `config.yaml` & `credentials.yaml`.

Avoids reparsing YAML across HTTP requests while automatically reloading when
either backing file changes on disk, and provides thread-safe self-service
mutations for an account's web password and Bellhop Xtream credentials.
"""

from __future__ import annotations

import hmac
import os
import threading
from collections.abc import MutableMapping
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from bellhop.common.accounts import (
  UserAccount,
  generate_stream_credential,
  hash_web_password,
  validate_new_web_password,
  verify_web_password,
)
from bellhop.common.file_utils import write_private_text
from bellhop.config.load import (
  _load_yaml_dict,
  build_config_from_raw_dict,
  resolve_config_targets,
  resolve_credentials_path,
)
from bellhop.config.model import AppConfig

__all__ = [
  "ConfigManager",
  "clear_config_cache",
  "get_cached_config",
  "get_config_manager",
]


def _stat_sig(path: Path) -> tuple[str, int, int]:
  resolved = path.resolve()
  try:
    st = resolved.stat()
    return (str(resolved), int(st.st_mtime_ns), int(st.st_size))
  except OSError:
    return (str(resolved), -1, -1)


def _make_ruamel() -> YAML:
  ryaml = YAML()
  ryaml.preserve_quotes = True
  ryaml.indent(mapping=2, sequence=4, offset=2)
  ryaml.width = 4096
  return ryaml


class ConfigManager:
  """Thread-safe mtime-cached loader & account mutator for `AppConfig`."""

  def __init__(
    self,
    config_path: Path | str | None = None,
    *,
    credentials_path: Path | str | None = None,
    initial_config: AppConfig | None = None,
    persist_credential_updates: bool = True,
  ) -> None:
    self._config_path = config_path
    self._credentials_path = credentials_path
    self._persist_credential_updates = persist_credential_updates
    self._lock = threading.Lock()
    self._cached_config: AppConfig | None = None
    self._cached_sig: tuple[tuple[str, int, int], ...] = ()
    self._watched_paths: tuple[Path, ...] = ()
    self._target_yaml: Path | None = None
    self._creds_yaml: Path | None = None

    if initial_config is not None:
      self._prime_initial(initial_config)

  def _prime_initial(self, cfg: AppConfig) -> None:
    if cfg.config_file is not None:
      yaml_target = cfg.config_file.resolve()
    elif self._config_path is not None:
      yaml_target = Path(self._config_path).resolve()
    else:
      yaml_target = (cfg.project_root / "config.yaml").resolve()

    creds_target = cfg.resolve_path(cfg.credentials_file)
    watched = (yaml_target, creds_target)
    self._target_yaml = yaml_target
    self._creds_yaml = creds_target
    self._watched_paths = watched
    self._cached_config = cfg
    self._cached_sig = tuple(_stat_sig(p) for p in watched)

  @property
  def target_path(self) -> Path:
    """Return the primary `config.yaml` path managed by this instance."""
    with self._lock:
      if self._target_yaml is not None:
        return self._target_yaml
    _root, _explicit, yaml_target, _base_dir = resolve_config_targets(
      self._config_path
    )
    return yaml_target

  def _reload_locked(self) -> AppConfig:
    root, explicit_cfg, yaml_target, base_dir = resolve_config_targets(
      self._config_path
    )
    pre_raw = _load_yaml_dict(yaml_target)
    creds_target = resolve_credentials_path(
      pre_raw,
      root=root,
      base_dir=base_dir,
      credentials_path=self._credentials_path,
    )
    cfg = build_config_from_raw_dict(
      pre_raw,
      root=root,
      base_dir=base_dir,
      config_file=yaml_target,
      is_explicit=explicit_cfg is not None,
      credentials_path=self._credentials_path,
      persist_credential_updates=self._persist_credential_updates,
    )
    watched = (yaml_target, creds_target)
    self._target_yaml = yaml_target
    self._creds_yaml = creds_target
    self._watched_paths = watched
    self._cached_config = cfg
    self._cached_sig = tuple(_stat_sig(p) for p in watched)
    return cfg

  def _get_locked(self, *, force_reload: bool = False) -> AppConfig:
    if not force_reload and self._cached_config is not None:
      cur_sig = tuple(_stat_sig(p) for p in self._watched_paths)
      if cur_sig == self._cached_sig:
        return self._cached_config
    return self._reload_locked()

  def get(self, *, force_reload: bool = False) -> AppConfig:
    """Return cached `AppConfig`, reloading only when disk files change."""
    with self._lock:
      return self._get_locked(force_reload=force_reload)

  def _require_account_and_creds_path(
    self, account_id: str
  ) -> tuple[AppConfig, UserAccount, Path]:
    cfg = self._get_locked()
    acct = cfg.account_by_id(account_id)
    if acct is None:
      raise KeyError(f"Unknown account {account_id!r}.")
    creds_path = self._creds_yaml or cfg.resolve_path(cfg.credentials_file)
    if not creds_path.is_file():
      raise FileNotFoundError(f"Credentials file not found: {creds_path}")
    return cfg, acct, creds_path

  @staticmethod
  def _load_account_entry(
    creds_path: Path, account_id: str
  ) -> tuple[YAML, MutableMapping[str, object], MutableMapping[str, object]]:
    ryaml = _make_ruamel()
    doc = ryaml.load(creds_path.read_text(encoding="utf-8"))
    if not isinstance(doc, MutableMapping):
      raise ValueError(f"Expected YAML mapping in {creds_path}.")
    accounts_map = doc.get("accounts")
    if not isinstance(accounts_map, MutableMapping):
      raise ValueError(f"{creds_path}: missing `accounts:` mapping.")
    entry = accounts_map.get(account_id)
    if not isinstance(entry, MutableMapping):
      raise KeyError(f"Account {account_id!r} not found in {creds_path}.")
    return ryaml, doc, entry

  @staticmethod
  def _write_ruamel(ryaml: YAML, doc: object, path: Path) -> None:
    buf = StringIO()
    ryaml.dump(doc, buf)
    write_private_text(path, buf.getvalue())

  def _persist_account_web_password_locked(
    self, account_id: str, creds_path: Path, new_password: str
  ) -> UserAccount:
    new_hash = hash_web_password(new_password)
    ryaml, doc, entry = self._load_account_entry(creds_path, account_id)
    entry["web_password_hash"] = DoubleQuotedScalarString(new_hash)
    entry.pop("web_password", None)
    entry.pop("password", None)
    self._write_ruamel(ryaml, doc, creds_path)
    reloaded = self._reload_locked()
    updated = reloaded.account_by_id(account_id)
    assert updated is not None
    return updated

  def change_account_web_password(
    self, account_id: str, current_password: str, new_password: str
  ) -> UserAccount:
    """Verify current password and store a new `scrypt$` hash.

    Checks the current password, validates length, and updates credentials.

    Raises:
      PermissionError: If `current_password` fails verification.
      ValueError: If `new_password` fails length rules or matches current.
      KeyError: If `account_id` does not exist.
    """
    with self._lock:
      _cfg, acct, creds_path = self._require_account_and_creds_path(account_id)
      if not verify_web_password(current_password, acct.web_password_hash):
        raise PermissionError("Current password is incorrect.")
      validate_new_web_password(new_password)
      if hmac.compare_digest(
        new_password.encode("utf-8"), current_password.encode("utf-8")
      ):
        raise ValueError(
          "New password must be different from your current password."
        )
      return self._persist_account_web_password_locked(
        account_id, creds_path, new_password
      )

  def set_account_web_password(
    self, account_id: str, new_password: str
  ) -> UserAccount:
    """Validate and store a new `scrypt$` hash after reset verification.

    Raises:
      ValueError: If `new_password` fails length rules.
      KeyError: If `account_id` does not exist.
    """
    with self._lock:
      _cfg, _acct, creds_path = self._require_account_and_creds_path(account_id)
      validate_new_web_password(new_password)
      return self._persist_account_web_password_locked(
        account_id, creds_path, new_password
      )

  def regenerate_account_stream_credentials(
    self, account_id: str
  ) -> UserAccount:
    """Rotate both 8-char `xtream_username` and `xtream_password` atomically."""
    with self._lock:
      cfg, acct, creds_path = self._require_account_and_creds_path(account_id)
      used_users = {a.xtream_username for a in cfg.accounts}
      new_user = generate_stream_credential(exclude=used_users)
      new_pass = generate_stream_credential(exclude={acct.xtream_password})
      ryaml, doc, entry = self._load_account_entry(creds_path, account_id)
      entry["xtream_username"] = DoubleQuotedScalarString(new_user)
      entry["xtream_password"] = DoubleQuotedScalarString(new_pass)
      self._write_ruamel(ryaml, doc, creds_path)
      reloaded = self._reload_locked()
      updated = reloaded.account_by_id(account_id)
      assert updated is not None
      return updated


_MANAGERS_LOCK = threading.Lock()
_MANAGERS: dict[str, ConfigManager] = {}


def get_config_manager(
  config_path: Path | str | None = None,
) -> ConfigManager:
  """Return a process-wide cached `ConfigManager` for `config_path`."""
  raw_target = config_path or os.environ.get("BELLHOP_CONFIG") or None
  _root, explicit_cfg, yaml_target, _base_dir = resolve_config_targets(
    raw_target
  )
  key = str(yaml_target.resolve())
  with _MANAGERS_LOCK:
    mgr = _MANAGERS.get(key)
    if mgr is None:
      mgr = ConfigManager(
        config_path=yaml_target if explicit_cfg is not None else None
      )
      _MANAGERS[key] = mgr
    return mgr


def get_cached_config(
  config_path: Path | str | None = None,
  *,
  force_reload: bool = False,
) -> AppConfig:
  """Return cached `AppConfig`, reloading only when disk files change."""
  return get_config_manager(config_path).get(force_reload=force_reload)


def clear_config_cache() -> None:
  """Clear the process-wide `ConfigManager` cache (primarily for tests)."""
  with _MANAGERS_LOCK:
    _MANAGERS.clear()
