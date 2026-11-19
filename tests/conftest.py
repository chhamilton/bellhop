"""Shared pytest configuration, deterministic clock fixture, and opt-in hooks."""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest


@dataclass(slots=True)
class FakeClock:
  """Deterministic controllable clock replacing `time.time` / `time.monotonic`."""

  now: float = 1_763_510_400.0

  def __call__(self) -> float:
    return self.now

  def advance(self, seconds: float) -> float:
    """Advance clock by `seconds` and return the new timestamp."""
    self.now += seconds
    return self.now


@pytest.fixture
def fake_clock() -> FakeClock:
  """Provide a fresh deterministic `FakeClock` starting at a fixed epoch."""
  return FakeClock()


def pytest_addoption(parser: pytest.Parser) -> None:
  """Register `--run-real-outputs` for local non-hermetic output inspection."""
  parser.addoption(
    "--run-real-outputs",
    action="store_true",
    default=False,
    help="Run opt-in tests that inspect local gitignored output/xtream files.",
  )


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
  """Relax global `--cov-fail-under` when running a targeted subset of tests."""
  keyword = str(config.getoption("keyword", default="") or "").strip()
  markexpr = str(config.getoption("markexpr", default="") or "").strip()
  file_or_dir = list(config.getoption("file_or_dir", default=[]) or [])
  normalized_targets = [str(x).rstrip("/") for x in file_or_dir]
  is_subset = bool(keyword or markexpr) or (
    bool(normalized_targets) and normalized_targets != ["tests"]
  )
  if is_subset:
    if hasattr(config.option, "cov_fail_under"):
      config.option.cov_fail_under = 0
    cov_plugin = config.pluginmanager.getplugin("_cov")
    if cov_plugin is not None and hasattr(cov_plugin, "options"):
      cov_plugin.options.cov_fail_under = 0


def pytest_collection_modifyitems(
  config: pytest.Config, items: list[pytest.Item]
) -> None:
  """Deselect `@pytest.mark.real_outputs` unless explicitly opted in."""
  run_real = bool(config.getoption("--run-real-outputs")) or (
    os.environ.get("BELLHOP_TEST_REAL_OUTPUTS") == "1"
  )
  markexpr = str(config.getoption("markexpr", default="") or "")
  if run_real or "real_outputs" in markexpr:
    return

  kept: list[pytest.Item] = []
  deselected: list[pytest.Item] = []
  for item in items:
    if "real_outputs" in item.keywords:
      deselected.append(item)
    else:
      kept.append(item)
  if deselected:
    config.hook.pytest_deselected(items=deselected)
    items[:] = kept
