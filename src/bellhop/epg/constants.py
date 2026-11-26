"""Shared timing, retention, and safety constants for `bellhop.epg`."""

from __future__ import annotations

__all__ = [
  "EPG_MIN_FUTURE_COVERAGE_SEC",
  "EPG_RECENT_FLOOR_SEC",
  "EPG_STALE_ENTRY_GRACE_SEC",
  "MAX_XMLTV_DECOMPRESSED_BYTES",
  "MAX_XMLTV_DOWNLOAD_BYTES",
  "MAX_XMLTV_RAW_ITEMS_PER_CHANNEL",
  "MAX_XMLTV_TEXT_CHARS",
  "SD_LINEUP_MAP_TTL_SEC",
  "SD_PROGRAM_RETENTION_DAYS",
  "SD_PROGRAM_RETENTION_SEC",
  "SD_TOKEN_TTL_SEC",
]

# Retain recently ended shows (`<= 1h` ago) in active coverage & guide views.
EPG_RECENT_FLOOR_SEC = 3600

# Keep shows ending within the past `2h` during XMLTV / SD-JSON ingestion.
EPG_STALE_ENTRY_GRACE_SEC = 7200

# Trigger a fresh EPG sync when remaining forward schedule coverage drops `<
# 2h`.
EPG_MIN_FUTURE_COVERAGE_SEC = 7200.0

# Schedules Direct `/token` lifetime margin (`23h`, inside SD's 24h expiry).
SD_TOKEN_TTL_SEC = 23.0 * 3600.0

# Schedules Direct `/lineups/{id}` station-map cache TTL (`24h`).
SD_LINEUP_MAP_TTL_SEC = 24 * 3600

# Retain unreferenced Gracenote `sd_programs` rows for `14` days before pruning.
SD_PROGRAM_RETENTION_DAYS = 14
SD_PROGRAM_RETENTION_SEC = SD_PROGRAM_RETENTION_DAYS * 86400.0

# Safety ceiling (`256 MiB`) on streamed upstream XMLTV downloads.
MAX_XMLTV_DOWNLOAD_BYTES = 256 * 1024 * 1024

# Safety ceiling (`512 MiB`) on decompressed XMLTV stream parsing.
MAX_XMLTV_DECOMPRESSED_BYTES = 512 * 1024 * 1024

# Maximum raw `<programme>` entries buffered per channel before pruning stale.
MAX_XMLTV_RAW_ITEMS_PER_CHANNEL = 2000

# Maximum characters retained for XMLTV text elements (`title`, `desc`, etc.).
MAX_XMLTV_TEXT_CHARS = 8192
