"""Relational SQLite EPG, Schedules Direct SD-JSON, and XMLTV domain package."""

from __future__ import annotations

from bellhop.epg.db import (
  DEFAULT_EPG_DB_FILE,
  SD_PROGRAM_RETENTION_DAYS,
  SD_PROGRAM_RETENTION_SEC,
  EpgCoverageSummary,
  EpgDatabase,
)
from bellhop.epg.models import (
  GuideChannelProto,
  GuideSnapshotProto,
  GuideStoreProto,
  ProgramItem,
  compute_guide_fingerprint,
  open_xml_stream,
  parse_xmltv_ts,
  reconcile_non_overlapping_schedule,
)
from bellhop.epg.refresh import (
  EpgRefreshConfig,
  EpgRefreshService,
  EpgRefreshStatus,
)
from bellhop.epg.sd_client import (
  DEFAULT_SD_BASE_URL,
  SD_USER_AGENT,
  SchedulesDirectApiError,
  SchedulesDirectSession,
  clean_sd_lineup_id,
  hash_sd_password,
  normalize_sd_postal,
)
from bellhop.epg.sd_epg import (
  SchedulesDirectEpgClient,
  SchedulesDirectEpgConfig,
  SchedulesDirectEpgError,
  SdFetchSummary,
)
from bellhop.epg.sync import (
  ExportBundleGuideStore,
  build_epg_refresher_from_config,
)
from bellhop.epg.xmltv import (
  DEFAULT_EPG_USER_AGENT,
  EpgVerificationDict,
  EpgVerificationResult,
  build_passthrough_xmltv_url,
  parse_xmltv_for_channels,
  verify_epg_endpoint,
)

__all__ = [
  "DEFAULT_EPG_DB_FILE",
  "DEFAULT_EPG_USER_AGENT",
  "DEFAULT_SD_BASE_URL",
  "SD_PROGRAM_RETENTION_DAYS",
  "SD_PROGRAM_RETENTION_SEC",
  "SD_USER_AGENT",
  "EpgCoverageSummary",
  "EpgDatabase",
  "EpgRefreshConfig",
  "EpgRefreshService",
  "EpgRefreshStatus",
  "EpgVerificationDict",
  "EpgVerificationResult",
  "ExportBundleGuideStore",
  "GuideChannelProto",
  "GuideSnapshotProto",
  "GuideStoreProto",
  "ProgramItem",
  "SchedulesDirectApiError",
  "SchedulesDirectEpgClient",
  "SchedulesDirectEpgConfig",
  "SchedulesDirectEpgError",
  "SchedulesDirectSession",
  "SdFetchSummary",
  "build_epg_refresher_from_config",
  "build_passthrough_xmltv_url",
  "clean_sd_lineup_id",
  "compute_guide_fingerprint",
  "hash_sd_password",
  "normalize_sd_postal",
  "open_xml_stream",
  "parse_xmltv_for_channels",
  "parse_xmltv_ts",
  "reconcile_non_overlapping_schedule",
  "verify_epg_endpoint",
]
