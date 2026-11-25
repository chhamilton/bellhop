#!/usr/bin/env bash
set -euo pipefail
umask 022

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

echo "==> Bootstrapping Bellhop workspace in $ROOT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "==> Creating Python virtual environment (.venv)..."
  python3 -m venv .venv
fi

echo "==> Installing editable package (bellhop) + dev dependencies (mypy, ruff, pytest, nodejs-wheel)..."
.venv/bin/pip install --upgrade pip setuptools wheel >/dev/null
.venv/bin/pip install -e ".[dev]"

if [[ -f "package.json" && -x ".venv/bin/npm" ]]; then
  echo "==> Installing TypeScript frontend toolchain..."
  if [[ -f "package-lock.json" ]]; then
    PATH="$ROOT_DIR/.venv/bin:$PATH" .venv/bin/npm ci --no-audit --no-fund
  else
    PATH="$ROOT_DIR/.venv/bin:$PATH" .venv/bin/npm install --no-audit --no-fund
  fi
  .venv/bin/node ./node_modules/typescript/bin/tsc --noEmit
  .venv/bin/node ./node_modules/typescript/bin/tsc
fi

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "NOTE: ffprobe (ffmpeg) not found on PATH; Phase (4) codec validation falls back to HTTP/TS header checks."
fi

if [[ ! -f "config.yaml" && -f "config.example.yaml" ]]; then
  echo "==> Initializing config.yaml from config.example.yaml..."
  cp config.example.yaml config.yaml
  chmod 600 config.yaml
fi

if [[ ! -f "credentials.yaml" ]]; then
  echo "WARNING: credentials.yaml not found. Copy credentials.example.yaml to credentials.yaml and add your Xtream credentials."
else
  echo "==> Found credentials.yaml"
fi

echo "==> Running strict type check & unit test suite..."
.venv/bin/pytest -q

echo ""
echo "Bellhop workspace ready! Activate with: source .venv/bin/activate"
echo "  - Run pipeline preview: bellhop run --dry-run"
echo "  - Start unified server: bellhop serve"
