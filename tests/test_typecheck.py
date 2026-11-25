"""Enforces static type checks, Google style formatting, and frontend tests.

Covers:
  - Python `mypy --strict` across `src/` and `tests/`.
  - Python `ruff check` and `ruff format --check` (Google style, 2-space
    indentation, no tabs) plus tab-free verification across source files.
  - TypeScript `tsc --noEmit` strict checking and build-sync verification
    between `src/bellhop/web/frontend/*.ts` and `src/bellhop/web/static/*.js`.
  - Node unit tests for `BellhopSettings` (`tests/frontend/prefs.test.mjs`).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from bellhop.common.api_contracts import render_typescript_api_contracts
from bellhop.common.file_utils import ensure_private_dir, write_private_text
from bellhop.epg.db import EpgDatabase

REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_venv_tool(name: str) -> str | None:
  """Resolve an executable in the active Python environment or PATH."""
  # Do NOT `.resolve()` sys.executable: venv python is a symlink to system python.
  candidate = Path(sys.executable).parent / name
  if candidate.is_file():
    return str(candidate)
  return shutil.which(name)


def test_mypy_strict_passes_with_zero_errors() -> None:
  mypy_api = pytest.importorskip("mypy.api")
  stdout, stderr, exit_code = mypy_api.run(
    [
      "--config-file",
      str(REPO_ROOT / "pyproject.toml"),
      str(REPO_ROOT / "src"),
      str(REPO_ROOT / "tests"),
    ]
  )
  assert exit_code == 0, f"mypy --strict failed:\n{stdout}\n{stderr}"


def test_ruff_google_style_and_no_tabs() -> None:
  ruff_bin = _resolve_venv_tool("ruff")
  if ruff_bin is None:
    pytest.skip("ruff not installed in test environment")

  lint = subprocess.run(
    [ruff_bin, "check", "src", "tests"],
    cwd=REPO_ROOT,
    capture_output=True,
    text=True,
    check=False,
  )
  assert lint.returncode == 0, (
    f"ruff check failed:\n{lint.stdout}\n{lint.stderr}"
  )

  fmt = subprocess.run(
    [ruff_bin, "format", "--check", "src", "tests"],
    cwd=REPO_ROOT,
    capture_output=True,
    text=True,
    check=False,
  )
  assert fmt.returncode == 0, (
    f"ruff format --check failed (run `ruff format src tests`):\n"
    f"{fmt.stdout}\n{fmt.stderr}"
  )

  # Ensure zero tab characters exist in any non-vendored source file.
  tab_files: list[str] = []
  for root in (REPO_ROOT / "src", REPO_ROOT / "tests"):
    for path in root.rglob("*"):
      if (
        path.is_file()
        and path.suffix in {".py", ".ts", ".js", ".mjs", ".css", ".html"}
        and "vendor" not in path.parts
        and "\t" in path.read_text(encoding="utf-8")
      ):
        tab_files.append(str(path.relative_to(REPO_ROOT)))
  assert tab_files == [], f"Tab characters found in: {tab_files}"


def test_typescript_strict_and_compiled_assets_in_sync(tmp_path: Path) -> None:
  contracts_dts = (
    REPO_ROOT / "src" / "bellhop" / "web" / "frontend" / "api_contracts.d.ts"
  )
  assert contracts_dts.is_file(), (
    "Missing generated frontend/api_contracts.d.ts"
  )
  assert contracts_dts.read_text(encoding="utf-8") == (
    render_typescript_api_contracts()
  ), (
    "src/bellhop/web/frontend/api_contracts.d.ts is out of sync with "
    "src/bellhop/common/api_contracts.py"
  )

  node_bin = _resolve_venv_tool("node")
  tsc_js = REPO_ROOT / "node_modules" / "typescript" / "bin" / "tsc"
  if node_bin is None or not tsc_js.is_file():
    pytest.skip("node / typescript toolchain not installed")

  check = subprocess.run(
    [node_bin, str(tsc_js), "--noEmit"],
    cwd=REPO_ROOT,
    capture_output=True,
    text=True,
    check=False,
  )
  assert check.returncode == 0, (
    f"TypeScript strict typecheck failed:\n{check.stdout}\n{check.stderr}"
  )

  out_dir = tmp_path / "compiled_static"
  build = subprocess.run(
    [node_bin, str(tsc_js), "--outDir", str(out_dir)],
    cwd=REPO_ROOT,
    capture_output=True,
    text=True,
    check=False,
  )
  assert build.returncode == 0, (
    f"TypeScript build failed:\n{build.stdout}\n{build.stderr}"
  )

  static_dir = REPO_ROOT / "src" / "bellhop" / "web" / "static"
  compiled_js = (
    "prefs.js",
    "login.js",
    "guide_logic.js",
    "render.js",
    "player.js",
    "settings.js",
    "app.js",
  )
  for filename in compiled_js:
    fresh = (out_dir / filename).read_text(encoding="utf-8")
    committed = (static_dir / filename).read_text(encoding="utf-8")
    assert committed == fresh, (
      f"{static_dir / filename} is out of sync with TypeScript sources; "
      "rebuild via `.venv/bin/node ./node_modules/typescript/bin/tsc`."
    )


def test_frontend_unit_tests_pass() -> None:
  node_bin = _resolve_venv_tool("node")
  if node_bin is None:
    pytest.skip("node not installed in test environment")

  res = subprocess.run(
    [
      node_bin,
      "--test",
      "tests/frontend/prefs.test.mjs",
      "tests/frontend/guide.test.mjs",
    ],
    cwd=REPO_ROOT,
    capture_output=True,
    text=True,
    check=False,
  )
  assert res.returncode == 0, (
    f"Frontend unit tests failed:\n{res.stdout}\n{res.stderr}"
  )


def test_repository_and_private_permissions_hygiene(tmp_path: Path) -> None:
  """Ensure repo trees are never group/world-writable and private files stay 0600/0700."""
  executable_scripts = {
    Path(".agents/scripts/bootstrap.sh"),
    Path("docker/entrypoint.sh"),
    Path("docker/bellhop-shim.sh"),
  }
  checked_trees = (
    REPO_ROOT / "src",
    REPO_ROOT / "tests",
    REPO_ROOT / "data",
    REPO_ROOT / "docker",
    REPO_ROOT / ".agents",
  )

  writable_violations: list[str] = []
  exec_violations: list[str] = []

  root_mode = stat.S_IMODE(REPO_ROOT.stat().st_mode)
  if root_mode & 0o022:
    writable_violations.append(f". ({oct(root_mode)})")

  for item in REPO_ROOT.iterdir():
    if item.is_file() and not item.name.startswith(".git"):
      mode = stat.S_IMODE(item.stat().st_mode)
      if mode & 0o022:
        writable_violations.append(
          f"{item.relative_to(REPO_ROOT)} ({oct(mode)})"
        )

  for tree in checked_trees:
    for entry in (tree, *tree.rglob("*")):
      if entry.is_symlink():
        continue
      rel = entry.relative_to(REPO_ROOT)
      mode = stat.S_IMODE(entry.stat().st_mode)
      if mode & 0o022:
        writable_violations.append(f"{rel} ({oct(mode)})")
      if entry.is_file():
        if rel in executable_scripts:
          if (mode & 0o111) == 0:
            exec_violations.append(f"{rel} missing +x ({oct(mode)})")
        elif mode & 0o111:
          exec_violations.append(f"{rel} unexpected +x ({oct(mode)})")

  assert writable_violations == [], (
    f"Group/world-writable paths found in repository: {writable_violations}"
  )
  assert exec_violations == [], (
    f"Unexpected executable permission bits in repository: {exec_violations}"
  )

  # Even under a permissive `umask(0)`, `ensure_private_dir`, `write_private_text`,
  # and `EpgDatabase` must enforce strict `0o700` / `0o600` on all created paths.
  prev_umask = os.umask(0)
  try:
    nested_dir = tmp_path / "perm_check" / "cache" / "nested"
    ensure_private_dir(nested_dir)
    for ancestor in (
      tmp_path / "perm_check",
      tmp_path / "perm_check" / "cache",
      nested_dir,
    ):
      assert stat.S_IMODE(ancestor.stat().st_mode) == 0o700

    secret_file = nested_dir / "secret.yaml"
    write_private_text(secret_file, "token: secret\n")
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600

    db_path = nested_dir / "epg.sqlite3"
    db = EpgDatabase(db_path)
    db.ensure_initialized()
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
      sidecar = Path(f"{db_path}{suffix}")
      if sidecar.is_file():
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
  finally:
    os.umask(prev_umask)
