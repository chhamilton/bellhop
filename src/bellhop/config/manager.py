"""Mtime-aware `ConfigManager` caching YAML config and `.env` files.

Avoids reparsing YAML or re-interpolating environment variables across HTTP
requests while automatically reloading whenever any backing file changes on
disk.
"""

from __future__ import annotations

import copy
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bellhop.common.api_contracts import PublicConfigPayload
from bellhop.config.load import (
  _load_yaml_dict,
  build_config_from_raw_dict,
  resolve_config_targets,
  resolve_dependency_paths,
)
from bellhop.config.model import AppConfig
from bellhop.config.public import (
  apply_public_config_update,
  export_public_config,
)

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


class ConfigManager:
  """Thread-safe mtime-cached loader & updater for `AppConfig`."""

  def __init__(
    self,
    config_path: Path | str | None = None,
    *,
    env_path: Path | str | None = None,
    credentials_path: Path | str | None = None,
    initial_config: AppConfig | None = None,
  ) -> None:
    self._config_path = config_path
    self._env_path = env_path
    self._credentials_path = credentials_path
    self._lock = threading.Lock()
    self._cached_config: AppConfig | None = None
    self._cached_public: PublicConfigPayload | None = None
    self._cached_sig: tuple[tuple[str, int, int], ...] = ()
    self._watched_paths: tuple[Path, ...] = ()
    self._target_yaml: Path | None = None

    if initial_config is not None:
      self._prime_initial(initial_config)

  def _prime_initial(self, cfg: AppConfig) -> None:
    if self._config_path is not None:
      root, _explicit, yaml_target, _base_dir = resolve_config_targets(
        self._config_path
      )
    elif cfg.config_file is not None:
      yaml_target = cfg.config_file.resolve()
      root = cfg.project_root
    else:
      root = cfg.project_root
      yaml_target = root / "config.yaml"

    dotenv_target = Path(self._env_path) if self._env_path else (root / ".env")
    creds_target = cfg.resolve_path(cfg.credentials_file)
    watched = (yaml_target, dotenv_target, creds_target)
    self._target_yaml = yaml_target
    self._watched_paths = watched
    self._cached_config = cfg
    self._cached_public = export_public_config(cfg)
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
    pre_raw = _load_yaml_dict(yaml_target, interpolate=False)
    dotenv_target, creds_target = resolve_dependency_paths(
      pre_raw,
      root=root,
      base_dir=base_dir,
      env_path=self._env_path,
      credentials_path=self._credentials_path,
    )
    cfg = build_config_from_raw_dict(
      pre_raw,
      root=root,
      base_dir=base_dir,
      config_file=yaml_target if yaml_target.is_file() else None,
      is_explicit=explicit_cfg is not None,
      env_path=self._env_path,
      credentials_path=self._credentials_path,
    )
    watched = (yaml_target, dotenv_target, creds_target)
    self._target_yaml = yaml_target
    self._watched_paths = watched
    self._cached_config = cfg
    self._cached_public = export_public_config(cfg)
    self._cached_sig = tuple(_stat_sig(p) for p in watched)
    return cfg

  def _get_locked(self, *, force_reload: bool = False) -> AppConfig:
    if not force_reload and self._cached_config is not None:
      cur_sig = tuple(_stat_sig(p) for p in self._watched_paths)
      if cur_sig == self._cached_sig:
        return self._cached_config
    return self._reload_locked()

  def get(self, *, force_reload: bool = False) -> AppConfig:
    """Return cached `AppConfig`, reloading only if watched files changed."""
    with self._lock:
      return self._get_locked(force_reload=force_reload)

  def get_public(self, *, force_reload: bool = False) -> PublicConfigPayload:
    """Return cached Tier-2 `PublicConfigPayload` (copy-safe)."""
    with self._lock:
      cfg = self._get_locked(force_reload=force_reload)
      if self._cached_public is None:
        self._cached_public = export_public_config(cfg)
      return copy.deepcopy(self._cached_public)

  def update_public(self, updates: Mapping[str, Any]) -> tuple[AppConfig, bool]:
    """Validate and persist Tier-2 updates, refreshing the in-memory cache."""
    with self._lock:
      before_cfg = self._get_locked()
      target = self._target_yaml
      if target is None:
        _r, _e, target, _b = resolve_config_targets(self._config_path)
      new_cfg, restart_required = apply_public_config_update(
        target, updates, before_config=before_cfg
      )
      dotenv_target = (
        Path(self._env_path)
        if self._env_path
        else (new_cfg.project_root / ".env")
      )
      creds_target = new_cfg.resolve_path(new_cfg.credentials_file)
      watched = (target, dotenv_target, creds_target)
      self._target_yaml = target
      self._watched_paths = watched
      self._cached_config = new_cfg
      self._cached_public = export_public_config(new_cfg)
      self._cached_sig = tuple(_stat_sig(p) for p in watched)
      return new_cfg, restart_required


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
