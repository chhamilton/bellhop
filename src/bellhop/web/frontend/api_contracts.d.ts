/**
 * AUTO-GENERATED FILE — DO NOT EDIT DIRECTLY.
 *
 * Generated from Python `TypedDict` definitions in
 * `src/bellhop/common/api_contracts.py` via `render_typescript_api_contracts()`.
 */

interface LineupMeta {
  lineup_id: string;
  provider: string;
  market: string;
  postal_prefix: string;
  timezone: string;
  lineup_updated_at: number;
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
  lineup_updated_at: number;
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
  account_id: string | null;
  email: string | null;
  expires_at: number | null;
}

interface ChannelEpgResponse {
  chno: number;
  name: string;
  epg_channel_id: string;
  programs: ProgramItem[];
}

interface PlayerUrls {
  xtream_server_url: string;
  m3u_url: string;
  m3u_hls_url: string;
  xmltv_url: string;
}

interface AccountInfoResponse {
  account_id: string;
  email: string;
  xtream_username: string;
  xtream_password: string;
  player_urls: PlayerUrls;
}

interface PasswordChangeResponse {
  ok: boolean;
  email: string;
}
