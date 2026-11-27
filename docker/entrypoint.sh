#!/bin/sh
# Bellhop container entrypoint: initializes mount points, seeds default config,
# applies TZ and PUID/PGID ownership, and execs the Bellhop command cleanly.
set -eu
umask 077

CONFIG_DIR="${BELLHOP_CONFIG_DIR:-/config}"
STATE_DIR="${BELLHOP_STATE_DIR:-/data/state}"
CACHE_DIR="${BELLHOP_CACHE_DIR:-/data/cache}"
OUTPUT_DIR="${BELLHOP_OUTPUT_DIR:-/data/output}"
CONFIG_FILE="${BELLHOP_CONFIG:-${CONFIG_DIR}/config.yaml}"
CREDENTIALS_FILE="$(dirname "${CONFIG_FILE}")/credentials.yaml"
DEFAULT_CONFIG="${BELLHOP_DEFAULT_CONFIG:-/app/docker/config.docker.yaml}"
DEFAULT_CREDENTIALS="${BELLHOP_DEFAULT_CREDENTIALS:-/app/docker/credentials.docker.yaml}"

export BELLHOP_CONFIG="${CONFIG_FILE}"

# Ensure internal mount directories exist.
mkdir -p \
  "${CONFIG_DIR}" \
  "${STATE_DIR}" \
  "${CACHE_DIR}" \
  "${OUTPUT_DIR}" \
  "${OUTPUT_DIR}/xtream"

# Seed container-ready config.yaml & credentials.yaml on first boot if missing.
if [ ! -f "${CONFIG_FILE}" ] && [ -f "${DEFAULT_CONFIG}" ]; then
  echo "[bellhop] Seeding default configuration at ${CONFIG_FILE}"
  cp "${DEFAULT_CONFIG}" "${CONFIG_FILE}"
  chmod 600 "${CONFIG_FILE}" 2>/dev/null || true
fi
if [ ! -f "${CREDENTIALS_FILE}" ] && [ -f "${DEFAULT_CREDENTIALS}" ]; then
  echo "[bellhop] Seeding default credentials at ${CREDENTIALS_FILE}"
  cp "${DEFAULT_CREDENTIALS}" "${CREDENTIALS_FILE}"
  chmod 600 "${CREDENTIALS_FILE}" 2>/dev/null || true
fi

# Default to `bellhop serve` when no command arguments are passed.
if [ "$#" -eq 0 ]; then
  set -- bellhop serve
fi

# When running as root, honor PUID/PGID and drop privileges via su-exec.
if [ "$(id -u)" -eq 0 ]; then
  PUID="${PUID:-1000}"
  PGID="${PGID:-1000}"

  case "${PUID}" in
    ''|*[!0-9]*|0*)
      echo "[bellhop] ERROR: PUID must be a positive integer (>= 1); refusing to run as root (PUID=${PUID})." >&2
      exit 1
      ;;
  esac
  case "${PGID}" in
    ''|*[!0-9]*|0*)
      echo "[bellhop] ERROR: PGID must be a positive integer (>= 1); refusing to run as root (PGID=${PGID})." >&2
      exit 1
      ;;
  esac

  if [ -n "${TZ:-}" ]; then
    case "${TZ}" in
      /*|*..*|*[!A-Za-z0-9_+/-]*)
        echo "[bellhop] WARNING: Ignoring unsafe TZ value: ${TZ}" >&2
        ;;
      *)
        TZ_RESOLVED="$(readlink -f "/usr/share/zoneinfo/${TZ}" 2>/dev/null || true)"
        case "${TZ_RESOLVED}" in
          /usr/share/zoneinfo/*)
            if [ -f "${TZ_RESOLVED}" ]; then
              ln -snf "${TZ_RESOLVED}" /etc/localtime 2>/dev/null || true
              printf '%s\n' "${TZ}" > /etc/timezone 2>/dev/null || true
            fi
            ;;
        esac
        ;;
    esac
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

  # Only chown non-symlink paths that do not already match PUID:PGID.
  for dir in "${CONFIG_DIR}" "${STATE_DIR}" "${CACHE_DIR}" "${OUTPUT_DIR}"; do
    find -P "${dir}" ! -type l \( ! -user "${USER_NAME}" -o ! -group "${GROUP_NAME}" \) \
      -exec chown -h "${PUID}:${PGID}" {} + 2>/dev/null || true
  done

  exec su-exec "${PUID}:${PGID}" "$@"
fi

# Already running as a non-root user (e.g., `docker run --user` or local test).
exec "$@"
