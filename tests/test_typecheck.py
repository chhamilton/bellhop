"""Enforces static type checks, Google style formatting, and frontend tests.

Covers:
  - Python `mypy --strict` across `src/` and `tests/`.
  - Python `ruff check` and `ruff format --check` (Google style, 2-space
    indentation, no tabs) plus tab-free verification across source files.
  - TypeScript `tsc --noEmit` strict checking and build-sync verification
    between `src/bellhop_web/frontend/*.ts` and `src/bellhop_web/static/*.js`.
  - Node unit tests for `BellhopSettings` (`tests/frontend/prefs.test.mjs`).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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

  static_dir = REPO_ROOT / "src" / "bellhop_web" / "static"
  for filename in ("prefs.js", "app.js"):
    fresh = (out_dir / filename).read_text(encoding="utf-8")
    committed = (static_dir / filename).read_text(encoding="utf-8")
    assert committed == fresh, (
      f"{static_dir / filename} is out of sync with TypeScript sources; "
      "rebuild via `.venv/bin/node ./node_modules/typescript/bin/tsc`."
    )


def test_frontend_prefs_unit_tests_pass() -> None:
  node_bin = _resolve_venv_tool("node")
  if node_bin is None:
    pytest.skip("node not installed in test environment")

  res = subprocess.run(
    [node_bin, "--test", "tests/frontend/prefs.test.mjs"],
    cwd=REPO_ROOT,
    capture_output=True,
    text=True,
    check=False,
  )
  assert res.returncode == 0, (
    f"Frontend unit tests failed:\n{res.stdout}\n{res.stderr}"
  )
