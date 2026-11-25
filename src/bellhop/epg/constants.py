"""Shared timing, retention, and safety constants for the `bellhop.epg`
domain.
"""

from __future__ import annotations

__all__ = [
  "EPG_MIN_FUTURE_COVERAGE_SEC",
  "EPG_RECENT_FLOOR_SEC",
  "EPG_STALE_ENTRY_GRACE_SEC",
  "MAX_XMLTV_DOWNLOAD_BYTES",
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
