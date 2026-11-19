#!/bin/sh
# Privilege-dropping wrapper for `/usr/local/bin/bellhop` inside the container.
# Ensures `docker exec <container> bellhop ...` (which bypasses entrypoint.sh
# and starts as root) automatically drops to PUID:PGID before writing files.
set -eu

REAL_BELLHOP="/usr/local/libexec/bellhop"

if [ "$(id -u)" -eq 0 ] && [ "${PUID:-1000}" != "0" ]; then
  exec su-exec "${PUID:-1000}:${PGID:-1000}" "${REAL_BELLHOP}" "$@"
fi

exec "${REAL_BELLHOP}" "$@"
