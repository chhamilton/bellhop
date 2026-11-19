#!/bin/sh
# Bellhop container entrypoint: initializes mount points, seeds default config,
# applies TZ and PUID/PGID ownership, and execs the Bellhop command cleanly.
set -eu

CONFIG_DIR="${BELLHOP_CONFIG_DIR:-/config}"
STATE_DIR="${BELLHOP_STATE_DIR:-/data/state}"
CACHE_DIR="${BELLHOP_CACHE_DIR:-/data/cache}"
OUTPUT_DIR="${BELLHOP_OUTPUT_DIR:-/data/output}"
CONFIG_FILE="${BELLHOP_CONFIG:-${CONFIG_DIR}/config.yaml}"
DEFAULT_CONFIG="${BELLHOP_DEFAULT_CONFIG:-/app/docker/config.docker.yaml}"

export BELLHOP_CONFIG="${CONFIG_FILE}"

# Ensure internal mount directories exist.
mkdir -p \
  "${CONFIG_DIR}" \
  "${STATE_DIR}" \
  "${CACHE_DIR}" \
  "${OUTPUT_DIR}" \
  "${OUTPUT_DIR}/xtream"

# Seed a container-ready config.yaml on first boot if none was mounted.
if [ ! -f "${CONFIG_FILE}" ] && [ -f "${DEFAULT_CONFIG}" ]; then
  echo "[bellhop] Seeding default configuration at ${CONFIG_FILE}"
  cp "${DEFAULT_CONFIG}" "${CONFIG_FILE}"
fi

# Default to `bellhop serve` when no command arguments are passed.
if [ "$#" -eq 0 ]; then
  set -- bellhop serve
fi

# When running as root, honor PUID/PGID and drop privileges via su-exec.
if [ "$(id -u)" -eq 0 ]; then
  PUID="${PUID:-1000}"
  PGID="${PGID:-1000}"

  if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
    ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime 2>/dev/null || true
    printf '%s\n' "${TZ}" > /etc/timezone 2>/dev/null || true
  fi

  # Create or reuse group for PGID.
  GROUP_NAME="$(awk -F: -v gid="${PGID}" '$3 == gid { print $1; exit }' /etc/group)"
  if [ -z "${GROUP_NAME}" ]; then
    GROUP_NAME="bellhop"
    addgroup -g "${PGID}" "${GROUP_NAME}" >/dev/null 2>&1 || true
  fi

  # Create or reuse user for PUID.
  USER_NAME="$(awk -F: -v uid="${PUID}" '$3 == uid { print $1; exit }' /etc/passwd)"
  if [ -z "${USER_NAME}" ]; then
    USER_NAME="bellhop"
    adduser -D -H -u "${PUID}" -G "${GROUP_NAME}" "${USER_NAME}" >/dev/null 2>&1 || true
  fi

  chown -R "${PUID}:${PGID}" \
    "${CONFIG_DIR}" \
    "${STATE_DIR}" \
    "${CACHE_DIR}" \
    "${OUTPUT_DIR}" 2>/dev/null || true

  exec su-exec "${PUID}:${PGID}" "$@"
fi

# Already running as a non-root user (e.g., `docker run --user` or local test).
exec "$@"
