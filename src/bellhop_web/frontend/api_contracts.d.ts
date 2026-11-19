/**
 * AUTO-GENERATED FILE — DO NOT EDIT DIRECTLY.
 *
 * Generated from Python `TypedDict` definitions in
 * `src/bellhop_web/api_contracts.py` via `render_typescript_api_contracts()`.
 */

interface LineupMeta {
  lineup_id: string;
  provider: string;
  market: string;
  postal_prefix: string;
  timezone: string;
  total_channels: number;
  category_channels: number;
  filtered_channels: number;
}

interface GuideCategory {
  category_id: string;
  category_name: string;
  channel_count: number;
}

interface ProgramItem {
  title: string;
  start_timestamp: number;
  stop_timestamp: number;
  id?: string;
  channel_id?: string;
  sub_title?: string;
  description?: string;
  category?: string;
  progress_pct?: number;
  is_live?: boolean;
}

interface PublicChannel {
  chno: number;
  sd_chno: number | null;
  name: string;
  callsign: string | null;
  logo: string;
  category_id: string;
  category_name: string;
  language: string;
  quality_tier: string;
  epg_channel_id: string;
  has_native_epg: boolean;
  stream_id: number;
  stream_url: string;
  hls_url: string;
  now_playing: ProgramItem | null;
  up_next: ProgramItem | null;
  schedule: ProgramItem[];
}

interface EpgStatus {
  enabled: boolean;
  configured: boolean;
  refreshing: boolean;
  state: string;
  last_updated: number | null;
  last_attempt: number | null;
  last_reason: string;
  last_error: string | null;
  program_count: number;
  active_program_count: number;
  channels_with_epg: number;
  max_stop_timestamp: number;
  refresh_hours: number | null;
}

interface TunerActiveInfo {
  session_id: number;
  chno: number;
  channel_name: string;
  mode: string;
  uptime_sec: number;
}

interface LineupApiResponse {
  server_timestamp: number;
  lineup: LineupMeta;
  epg_status: EpgStatus;
  categories: GuideCategory[];
  channels: PublicChannel[];
}

interface HealthApiResponse {
  status: string;
  lineup_id: string;
  provider: string;
  market: string;
  channel_count: number;
  category_count: number;
  epg_program_count: number;
  epg_active_program_count: number;
  epg_status: EpgStatus;
  active_tuner: TunerActiveInfo | null;
  auth_required: boolean;
}

interface AuthStatusResponse {
  auth_required: boolean;
  authenticated: boolean;
  username: string | null;
  expires_at: number | null;
}

interface ChannelEpgResponse {
  chno: number;
  name: string;
  epg_channel_id: string;
  programs: ProgramItem[];
}

interface XtreamServerPublicConfig {
  enabled: boolean;
  host: string | null;
  port: number | null;
  public_host: string | null;
  public_port: number | null;
  server_protocol: string;
}

interface UpstreamXtreamPublicConfig {
  server_url: string;
  stream_format: string;
  request_timeout_sec: number;
  refresh_hours: number;
}

interface TvGuidePublicConfig {
  base_url: string;
}

interface SchedulesDirectPublicConfig {
  base_url: string;
  auto_add_lineup: boolean;
}

interface LineupPublicConfig {
  backend: string;
  lineup_id: string;
  postal_code: string;
  refresh_days: number;
  numbering_mode: string;
  collapse_sd_dupes: boolean;
  exclude_audio: boolean;
  pinned_streams: Record<string, number>;
  excluded_channels: number[];
  tvguide: TvGuidePublicConfig;
  schedules_direct: SchedulesDirectPublicConfig;
}

interface MatchingPublicConfig {
  upgrade_hysteresis_score: number;
}

interface ValidationPublicConfig {
  enabled: boolean;
  only_changed: boolean;
  concurrency: number;
  ignore_account_limit: boolean;
  inter_probe_delay_sec: number;
  http_timeout_sec: number;
  min_bytes_to_read: number;
  use_ffprobe: boolean;
  ffprobe_timeout_sec: number;
  max_candidates_per_channel: number;
}

interface EpgPublicConfig {
  verify: boolean;
  refresh_hours: number;
}

interface OutputPublicConfig {
  m3u8_enabled: boolean;
  xtream_enabled: boolean;
  include_unmatched_comment: boolean;
}

interface PipelinePublicConfig {
  lineup: LineupPublicConfig;
  matching: MatchingPublicConfig;
  validation: ValidationPublicConfig;
  epg: EpgPublicConfig;
  output: OutputPublicConfig;
}

interface PublicConfig {
  upstream_xtream: UpstreamXtreamPublicConfig;
  xtream_server: XtreamServerPublicConfig;
  pipeline: PipelinePublicConfig;
}

interface PublicConfigResponse {
  available: boolean;
  config: PublicConfig | null;
  config_file: string | null;
}

interface ConfigSaveResponse {
  saved?: boolean;
  restart_required?: boolean;
  config_file?: string;
  config?: PublicConfig;
  error?: string;
}
