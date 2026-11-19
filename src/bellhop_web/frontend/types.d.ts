/**
 * Shared TypeScript contracts for the Bellhop Web Guide frontend.
 *
 * Mirrors the JSON response schemas emitted by `bellhop_web.server` and
 * `bellhop.config.export_public_config`, plus ambient declarations for the
 * vendored `mpegts.js` and `hls.js` browser libraries.
 */

type JsonPrimitive = string | number | boolean | null;
type JsonArray = JsonValue[];
type JsonObject = { [key: string]: JsonValue };
type JsonValue = JsonPrimitive | JsonObject | JsonArray;

type ThemeMode = "dark" | "light";
type ViewMode = "cards" | "grid";
type StreamMode = "mpegts" | "hls";
type SettingsStatusKind = "" | "ok" | "warn" | "error";

interface BellhopUiSettings extends JsonObject {
  theme: ThemeMode;
}

interface BellhopClientSettings extends JsonObject {
  version: number;
  ui: BellhopUiSettings;
}

type SettingsChangeSource = "local" | "storage";
type SettingsListener = (
  state: BellhopClientSettings,
  source: SettingsChangeSource,
) => void;

interface BellhopSettingsApi {
  readonly STORAGE_KEY: string;
  readonly SCHEMA_VERSION: number;
  readonly DEFAULTS: Readonly<BellhopClientSettings>;
  read(): BellhopClientSettings;
  write(nextState: JsonObject): BellhopClientSettings;
  get<T extends JsonValue = JsonValue>(
    path: string | readonly string[],
    fallback: T,
  ): T;
  get(path: string | readonly string[]): JsonValue | undefined;
  set(path: string | readonly string[], value: JsonValue): BellhopClientSettings;
  patch(partial: JsonObject): BellhopClientSettings;
  reset(): BellhopClientSettings;
  subscribe(listener: SettingsListener): () => void;
  getTheme(): ThemeMode;
  setTheme(theme: string): ThemeMode;
  toggleTheme(): ThemeMode;
  applyTheme(theme: string): ThemeMode;
  bindThemeToggle(btn: HTMLElement | null): () => void;
}

interface LineupMeta {
  lineup_id?: string;
  provider?: string;
  market?: string;
  timezone?: string;
}

interface GuideCategory {
  category_id: string;
  category_name: string;
  channel_count?: number;
}

interface ProgramItem {
  id?: string;
  title: string;
  sub_title?: string;
  description?: string;
  category?: string;
  start_timestamp: number;
  stop_timestamp: number;
  progress_pct?: number;
}

interface PublicChannel {
  chno: number;
  sd_chno: number | null;
  name: string;
  callsign: string;
  logo: string;
  category_id: string;
  category_name: string;
  language: string;
  quality_tier: string;
  epg_channel_id: string;
  stream_url: string;
  hls_url: string;
  now_playing: ProgramItem | null;
  up_next: ProgramItem | null;
  schedule: ProgramItem[];
}

interface EpgStatus {
  enabled: boolean;
  refreshing: boolean;
  program_count?: number;
  active_program_count?: number;
  last_refresh_ts?: number | null;
  last_error?: string | null;
}

interface LineupApiResponse {
  lineup?: LineupMeta;
  categories?: GuideCategory[];
  channels?: PublicChannel[];
  total_channels?: number;
  category_channels?: number;
  matched_channels?: number;
  epg_status?: EpgStatus | null;
}

interface HealthApiResponse {
  status: string;
  epg_status?: EpgStatus | null;
}

interface AuthStatusResponse {
  auth_required: boolean;
  authenticated: boolean;
  username: string | null;
  expires_at: number | null;
}

interface ChannelEpgResponse {
  chno: number;
  programs?: ProgramItem[];
}

interface XtreamServerPublicConfig {
  enabled?: boolean;
  host?: string | null;
  port?: number | null;
  public_host?: string | null;
  public_port?: number | null;
  server_protocol?: string;
}

interface UpstreamXtreamPublicConfig {
  server_url?: string;
  stream_format?: string;
  request_timeout_sec?: number;
  refresh_hours?: number;
}

interface TvGuidePublicConfig {
  base_url?: string;
}

interface SchedulesDirectPublicConfig {
  base_url?: string;
  auto_add_lineup?: boolean;
}

interface LineupPublicConfig {
  backend?: string;
  lineup_id?: string;
  postal_code?: string;
  refresh_days?: number;
  numbering_mode?: string;
  collapse_sd_dupes?: boolean;
  exclude_audio?: boolean;
  pinned_streams?: Record<string, number>;
  excluded_channels?: number[];
  tvguide?: TvGuidePublicConfig;
  schedules_direct?: SchedulesDirectPublicConfig;
}

interface MatchingPublicConfig {
  upgrade_hysteresis_score?: number;
}

interface ValidationPublicConfig {
  enabled?: boolean;
  only_changed?: boolean;
  concurrency?: number;
  ignore_account_limit?: boolean;
  inter_probe_delay_sec?: number;
  http_timeout_sec?: number;
  min_bytes_to_read?: number;
  use_ffprobe?: boolean;
  ffprobe_timeout_sec?: number;
  max_candidates_per_channel?: number;
}

interface EpgPublicConfig {
  verify?: boolean;
  refresh_hours?: number;
}

interface OutputPublicConfig {
  m3u8_enabled?: boolean;
  xtream_enabled?: boolean;
  include_unmatched_comment?: boolean;
}

interface PipelinePublicConfig {
  lineup?: LineupPublicConfig;
  matching?: MatchingPublicConfig;
  validation?: ValidationPublicConfig;
  epg?: EpgPublicConfig;
  output?: OutputPublicConfig;
}

interface PublicConfig {
  upstream_xtream?: UpstreamXtreamPublicConfig;
  xtream_server?: XtreamServerPublicConfig;
  pipeline?: PipelinePublicConfig;
}

interface PublicConfigResponse {
  available: boolean;
  config: PublicConfig | null;
  config_file: string | null;
}

interface ConfigSaveResponse {
  saved?: boolean;
  restart_required?: boolean;
  config?: PublicConfig;
  error?: string;
}

interface MpegtsPlayer {
  attachMediaElement(mediaElement: HTMLMediaElement): void;
  detachMediaElement(): void;
  load(): void;
  unload(): void;
  play(): Promise<void> | void;
  pause(): void;
  destroy(): void;
  on(event: string, listener: (errType: string, errDetail: string) => void): void;
}

interface MpegtsStatic {
  readonly Events: {
    readonly ERROR: string;
  };
  isSupported(): boolean;
  createPlayer(
    mediaDataSource: { type: string; isLive: boolean; url: string },
    config: {
      enableWorker: boolean;
      enableStashBuffer: boolean;
      stashInitialSize: number;
      liveBufferLatencyChasing: boolean;
      liveBufferLatencyMaxLatency: number;
      liveBufferLatencyMinRemain: number;
    },
  ): MpegtsPlayer;
}

interface HlsErrorData {
  fatal?: boolean;
  details?: string;
}

interface HlsInstance {
  loadSource(url: string): void;
  attachMedia(mediaElement: HTMLMediaElement): void;
  destroy(): void;
  on(event: string, listener: (ev: string, data?: HlsErrorData) => void): void;
}

interface HlsConstructor {
  readonly Events: {
    readonly MANIFEST_PARSED: string;
    readonly ERROR: string;
  };
  isSupported(): boolean;
  new (config: { lowLatencyMode: boolean; maxBufferLength: number }): HlsInstance;
}

interface WebkitHTMLVideoElement extends HTMLVideoElement {
  webkitEnterFullscreen?: () => void;
}

interface Window {
  BellhopSettings?: BellhopSettingsApi;
  mpegts?: MpegtsStatic;
  Hls?: HlsConstructor;
}
