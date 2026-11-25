"""Shared pytest configuration, deterministic clock fixture, and opt-in hooks."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# Normalize permissive container/CI umasks (e.g. `0000`) so pytest, mypy, ruff,
# and bytecode generation never leave group- or world-writable directories/files.
os.umask(0o022)


def make_mpegts_packet(payload_byte: int = 0xFF, count: int = 8) -> bytes:
  """Build valid `0x47`-synced 188-byte MPEG-TS test packets."""
  pkt = bytes([0x47, 0x40, 0x00, 0x10]) + bytes([payload_byte & 0xFF]) * 184
  return pkt * max(1, count)


async def await_until(
  predicate: Callable[[], Awaitable[bool] | bool],
  *,
  timeout_sec: float = 3.0,
  interval_sec: float = 0.03,
  description: str = "condition",
) -> None:
  """Poll `predicate` until truthy or fail deterministically with `AssertionError`."""
  deadline = time.monotonic() + timeout_sec
  while True:
    res = predicate()
    ok = await res if asyncio.iscoroutine(res) else bool(res)
    if ok:
      return
    if time.monotonic() >= deadline:
      raise AssertionError(
        f"Timed out after {timeout_sec:.2f}s waiting for {description}"
      )
    await asyncio.sleep(interval_sec)


def write_minimal_xtream_export(
  xt_dir: Path,
  *,
  xmltv_url: str = "",
  streams: list[dict[str, Any]] | None = None,
) -> Path:
  """Write a minimal valid Phase 6 Xtream JSON bundle into `xt_dir`."""
  xt_dir.mkdir(parents=True, exist_ok=True)
  categories = [
    {"category_id": "1", "category_name": "News (1000–1099)", "parent_id": 0},
    {"category_id": "2", "category_name": "Sports (1100–1199)", "parent_id": 0},
  ]
  live_streams = (
    streams
    if streams is not None
    else [
      {
        "num": 1101,
        "name": "ICI RDI",
        "stream_type": "live",
        "stream_id": 410101,
        "stream_icon": "https://logos.example/rdi.png",
        "epg_channel_id": "ICIRDI.ca",
        "category_id": "1",
        "direct_source": "http://panel.example/live/alice/secret/410101.ts",
      },
      {
        "num": 1108,
        "name": "RDS",
        "stream_type": "live",
        "stream_id": 410108,
        "stream_icon": "https://logos.example/rds.png",
        "epg_channel_id": "RDS.ca",
        "category_id": "2",
        "direct_source": "http://panel.example/live/alice/secret/410108.ts",
      },
    ]
  )
  (xt_dir / "get_live_categories.json").write_text(
    json.dumps(categories), encoding="utf-8"
  )
  (xt_dir / "get_live_streams.json").write_text(
    json.dumps(live_streams), encoding="utf-8"
  )
  bundle = {
    "user_info": {
      "username": "alice",
      "status": "Active",
      "max_connections": "1",
      "allowed_output_formats": ["ts", "m3u8"],
    },
    "server_info": {
      "timezone": "America/Montreal",
      "xmltv_url": xmltv_url,
    },
    "lineup_info": {
      "lineup_id": "CAN-BELLFIBE-MTL-X",
      "provider": "Bell Fibe TV",
      "market": "Montréal, QC",
    },
    "live_categories": categories,
    "live_streams": live_streams,
  }
  (xt_dir / "bellhop_xtream_playlist.json").write_text(
    json.dumps(bundle), encoding="utf-8"
  )
  return xt_dir


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


@pytest.fixture(autouse=True)
def _hermetic_default_iptv_org(monkeypatch: pytest.MonkeyPatch) -> None:
  """Prevent hermetic tests from hitting live `iptv-org.github.io` unless mocked."""
  from bellhop.pipeline.iptv_org import (
    DEFAULT_IPTV_ORG_API_URL,
    IptvOrgClient,
    IptvOrgFetchResult,
    IptvOrgIndex,
  )

  orig_fetch = IptvOrgClient.fetch_index

  async def _guarded_fetch(
    self: IptvOrgClient,
    *,
    force_refresh: bool = False,
    dry_run: bool = False,
    allow_offline_empty: bool = True,
  ) -> IptvOrgFetchResult:
    if self.base_url.rstrip("/") == DEFAULT_IPTV_ORG_API_URL.rstrip("/"):
      return IptvOrgFetchResult(
        index=IptvOrgIndex(),
        updated=False,
        from_cache=True,
        channels_status="offline_empty",
        feeds_status="offline_empty",
      )
    return await orig_fetch(
      self,
      force_refresh=force_refresh,
      dry_run=dry_run,
      allow_offline_empty=allow_offline_empty,
    )

  monkeypatch.setattr(IptvOrgClient, "fetch_index", _guarded_fetch)


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
  """Relax global `--cov-fail-under` only for targeted file/`-k` or narrow marker runs."""
  keyword = str(config.getoption("keyword", default="") or "").strip()
  markexpr = str(config.getoption("markexpr", default="") or "").strip()
  file_or_dir = list(config.getoption("file_or_dir", default=[]) or [])
  normalized_targets = [str(x).rstrip("/") for x in file_or_dir]
  # Keep the 85% coverage gate enforced on full runs AND `-m "not docker"` CI runs.
  narrow_marker = bool(markexpr) and markexpr not in {
    "not docker",
    "not real_outputs",
  }
  is_subset = (
    bool(keyword)
    or narrow_marker
    or (bool(normalized_targets) and normalized_targets != ["tests"])
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
