#!/bin/sh
# Privilege-dropping wrapper for `/usr/local/bin/bellhop` inside the container.
# Ensures `docker exec <container> bellhop ...` (which bypasses entrypoint.sh
# and starts as root) automatically drops to PUID:PGID before writing files.
set -eu
umask 077

REAL_BELLHOP="/usr/local/libexec/bellhop"

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
  exec su-exec "${PUID}:${PGID}" "${REAL_BELLHOP}" "$@"
fi

exec "${REAL_BELLHOP}" "$@"
