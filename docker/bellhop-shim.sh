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
  case "${PUID}:${PGID}" in
    0:*|*:0|*[!0-9:]*)
      echo "[bellhop] ERROR: PUID and PGID must be positive integers (>= 1)." >&2
      exit 1
      ;;
  esac
  exec su-exec "${PUID}:${PGID}" "${REAL_BELLHOP}" "$@"
fi

exec "${REAL_BELLHOP}" "$@"
