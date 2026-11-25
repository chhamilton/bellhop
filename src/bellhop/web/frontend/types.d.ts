/**
 * Browser-local UI, client preferences, and vendored player declarations.
 *
 * All HTTP JSON wire interfaces (`PublicChannel`, `LineupApiResponse`,
 * `PublicConfig`, etc.) are generated directly from Python `TypedDict`s into
 * `./api_contracts.d.ts` by `bellhop.common.api_contracts`.
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
  BellhopUpdateCheckIntervalMs?: number;
  mpegts?: MpegtsStatic;
  Hls?: HlsConstructor;
}
