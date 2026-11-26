/**
 * Bellhop — Live TV Guide & Player Frontend Entry Module (`type="module"`).
 *
 * Coordinates URL/history state, category/search filtering, Cards & 2D
 * Timeline views, in-place 30s live progress updates, keyboard navigation,
 * the single-tuner video player (`player.ts`), and Settings (`settings.ts`).
 */

import {
  DEFAULT_UPDATE_CHECK_INTERVAL_MS,
  buildUrlQueryString,
  computeCategorySummaries,
  createLineupUpdatePoller,
  groupChannelsByCategory,
  matchesCategory,
  matchesSecondaryFilters,
  parseUrlState,
  refreshScheduleWindows,
  resolveSelectedCategory,
} from "./guide_logic.js";
import { createPlayerController } from "./player.js";
import {
  renderCardsSectionsHtml,
  renderCategoriesHtml,
  renderTimelineViewDom,
  trapFocusInContainer,
  updateLiveProgressInPlace,
} from "./render.js";
import { createSettingsController } from "./settings.js";

const PAGE_CLIENT_ID = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

interface AppState {
  lineupMeta: LineupMeta | null;
  categories: GuideCategory[];
  channels: PublicChannel[];
  filteredChannels: PublicChannel[];
  selectedCategory: string;
  hideEmptyGuide: boolean;
  searchRaw: string;
  searchQuery: string;
  viewMode: ViewMode;
  activeChannel: PublicChannel | null;
  pendingChannel: number | null;
  streamMode: StreamMode;
  timezone: string;
  timelineOffsetSec: number;
  lineupUpdatedAt: number | null;
  epgStatus: EpgStatus | null;
  epgPollTimer: number | null;
}

const resolveDefaultTimezone = (): string => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
};

const DEFAULT_TIMEZONE = resolveDefaultTimezone();

export interface GuideAppHandle {
  destroy: () => void;
}

export const initGuideApp = (): GuideAppHandle => {
  const state: AppState = {
    lineupMeta: null,
    categories: [],
    channels: [],
    filteredChannels: [],
    selectedCategory: "all",
    hideEmptyGuide: false,
    searchRaw: "",
    searchQuery: "",
    viewMode: "cards",
    activeChannel: null,
    pendingChannel: null,
    streamMode: "mpegts",
    timezone: DEFAULT_TIMEZONE,
    timelineOffsetSec: 0,
    lineupUpdatedAt: null,
    epgStatus: null,
    epgPollTimer: null,
  };

  const requireEl = <T extends HTMLElement>(id: string): T => {
    const el = document.getElementById(id);
    if (!el) {
      throw new Error(`Missing required DOM element #${id}`);
    }
    return el as T;
  };

  const errorMessage = (err: unknown): string =>
    err instanceof Error ? err.message : String(err);

  // Core Guide DOM
  const elGuideMain = requireEl<HTMLElement>("guide-main");
  const elLineupTitle = requireEl<HTMLElement>("lineup-title");
  const elLineupBadge = requireEl<HTMLElement>("lineup-id-badge");
  const elChannelCounter = requireEl<HTMLElement>("channel-counter");
  const elClock = requireEl<HTMLElement>("live-clock");
  const elSearchInput = requireEl<HTMLInputElement>("search-input");
  const elSearchClear = requireEl<HTMLButtonElement>("search-clear");
  const elCategoryList = requireEl<HTMLElement>("category-list");
  const elCardsView = requireEl<HTMLElement>("cards-view");
  const elTimelineView = requireEl<HTMLElement>("timeline-view");
  const elTimelineHeader = requireEl<HTMLElement>("timeline-header");
  const elTimelineBody = requireEl<HTMLElement>("timeline-body");
  const elEmptyState = requireEl<HTMLElement>("empty-state");
  const elResetFiltersBtn = requireEl<HTMLButtonElement>("reset-filters-btn");
  const elHideEmptyEpgBtn = document.getElementById(
    "hide-empty-epg-btn",
  ) as HTMLButtonElement | null;
  const elRefreshBtn = requireEl<HTMLButtonElement>("refresh-btn");
  const elLogoutBtn = document.getElementById(
    "logout-btn",
  ) as HTMLButtonElement | null;
  const elThemeToggleBtn = document.getElementById(
    "theme-toggle-btn",
  ) as HTMLButtonElement | null;
  const elSettingsBtn = document.getElementById(
    "settings-btn",
  ) as HTMLButtonElement | null;
  const elSettingsOverlay = document.getElementById("settings-overlay");
  const elOverlay = requireEl<HTMLElement>("player-overlay");

  const updateClock = (): void => {
    try {
      elClock.textContent = new Intl.DateTimeFormat("en-CA", {
        weekday: "short",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        timeZone: state.timezone,
      }).format(new Date());
    } catch {
      elClock.textContent = new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
    }
  };

  const readStateFromUrl = (): number | null => {
    const parsed = parseUrlState(
      window.location.search,
      window.location.hash,
      state.categories,
    );
    state.selectedCategory = parsed.selectedCategory;
    state.hideEmptyGuide = parsed.hideEmptyGuide;
    state.searchRaw = parsed.searchRaw;
    state.searchQuery = parsed.searchQuery;
    state.viewMode = parsed.viewMode;
    state.timelineOffsetSec = parsed.timelineOffsetSec;
    state.streamMode = parsed.streamMode;
    return parsed.pendingChannel;
  };

  const syncModeButtons = (): void => {
    requireEl<HTMLElement>("mode-ts-btn").classList.toggle(
      "active",
      state.streamMode === "mpegts",
    );
    requireEl<HTMLElement>("mode-hls-btn").classList.toggle(
      "active",
      state.streamMode === "hls",
    );
  };

  const syncControlsFromState = (): void => {
    elSearchInput.value = state.searchRaw;
    elSearchClear.hidden = !state.searchQuery;

    if (elHideEmptyEpgBtn) {
      elHideEmptyEpgBtn.classList.toggle("active", state.hideEmptyGuide);
      elHideEmptyEpgBtn.setAttribute(
        "aria-pressed",
        state.hideEmptyGuide ? "true" : "false",
      );
    }
    document.querySelectorAll<HTMLElement>("[data-view]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset["view"] === state.viewMode);
    });
    syncModeButtons();
  };

  const syncUrlFromState = ({
    replace = false,
  }: { replace?: boolean } = {}): void => {
    const qs = buildUrlQueryString({
      selectedCategory: state.selectedCategory,
      hideEmptyGuide: state.hideEmptyGuide,
      searchRaw: state.searchRaw,
      viewMode: state.viewMode,
      timelineOffsetSec: state.timelineOffsetSec,
      streamMode: state.streamMode,
      activeChno: state.activeChannel ? state.activeChannel.chno : null,
    });
    const targetUrl = `${window.location.pathname}${qs ? `?${qs}` : ""}`;
    const currentUrl = `${window.location.pathname}${window.location.search}`;
    if (targetUrl === currentUrl && !window.location.hash) return;
    try {
      if (replace) {
        window.history.replaceState(null, "", targetUrl);
      } else {
        window.history.pushState(null, "", targetUrl);
      }
    } catch {
      // Ignore history errors in restricted frames.
    }
  };

  const redirectToLogin = (): void => {
    const nextTarget = `${window.location.pathname}${window.location.search}`;
    const q =
      nextTarget && nextTarget !== "/"
        ? `?next=${encodeURIComponent(nextTarget)}`
        : "";
    window.location.replace(`/login${q}`);
  };

  const renderTimeline = (): void => {
    const groups = groupChannelsByCategory(
      state.filteredChannels,
      state.categories,
    );
    renderTimelineViewDom({
      elTimelineView,
      elTimelineHeader,
      elTimelineBody,
      groups,
      timezone: state.timezone,
      timelineOffsetSec: state.timelineOffsetSec,
      nowSec: Math.floor(Date.now() / 1000),
    });
  };

  const applyFiltersAndRender = (): void => {
    const nowSec = Math.floor(Date.now() / 1000);
    refreshScheduleWindows(state.channels, nowSec);

    // Preserve focused channel item across full re-renders when possible.
    const activeEl = document.activeElement as HTMLElement | null;
    const focusedChno =
      activeEl &&
      (elCardsView.contains(activeEl) || elTimelineBody.contains(activeEl))
        ? activeEl.closest<HTMLElement>("[data-chno]")?.dataset["chno"] || null
        : null;

    const categoryChannels = state.channels.filter((ch) =>
      matchesCategory(ch, state.selectedCategory),
    );
    state.filteredChannels = categoryChannels.filter((ch) =>
      matchesSecondaryFilters(ch, state.hideEmptyGuide, state.searchQuery),
    );

    const epgSuffix =
      state.epgStatus && state.epgStatus.refreshing ? " • Updating guide…" : "";
    elChannelCounter.textContent = `${state.filteredChannels.length} of ${categoryChannels.length} channels${epgSuffix}`;
    const activeCatObj =
      state.selectedCategory !== "all"
        ? state.categories.find((c) => c.category_id === state.selectedCategory)
        : null;
    elChannelCounter.title = activeCatObj
      ? `${state.filteredChannels.length} of ${categoryChannels.length} channels in ${activeCatObj.category_name}`
      : `${state.filteredChannels.length} of ${categoryChannels.length} channels`;

    elCategoryList.innerHTML = renderCategoriesHtml(
      computeCategorySummaries(
        state.categories,
        state.channels,
        state.selectedCategory,
        state.hideEmptyGuide,
        state.searchQuery,
      ),
    );

    const isEmpty = state.filteredChannels.length === 0;
    elEmptyState.hidden = !isEmpty;

    const isTimeline = state.viewMode === "grid";
    elGuideMain.classList.toggle("is-timeline", isTimeline && !isEmpty);

    if (!isTimeline) {
      elCardsView.hidden = isEmpty;
      elTimelineView.hidden = true;
      if (!isEmpty) {
        elCardsView.innerHTML = renderCardsSectionsHtml(
          groupChannelsByCategory(state.filteredChannels, state.categories),
          state.timezone,
        );
        if (focusedChno) {
          elCardsView
            .querySelector<HTMLElement>(
              `.channel-card[data-chno="${focusedChno}"]`,
            )
            ?.focus();
        }
      }
    } else {
      elCardsView.hidden = true;
      elTimelineView.hidden = isEmpty;
      if (!isEmpty) {
        renderTimeline();
        if (focusedChno) {
          elTimelineBody
            .querySelector<HTMLElement>(`.tl-ch-cell[data-chno="${focusedChno}"]`)
            ?.focus();
        }
      }
    }
  };

  // Instantiate Player & Settings controllers
  const player = createPlayerController({
    pageClientId: PAGE_CLIENT_ID,
    state,
    elOverlay,
    elBackdrop: requireEl<HTMLElement>("player-backdrop"),
    elPlayerStage: requireEl<HTMLElement>("player-stage"),
    elVideo: requireEl<WebkitHTMLVideoElement>("video-el"),
    elUnmuteBtn: requireEl<HTMLButtonElement>("video-unmute-btn"),
    elClickPlayBox: requireEl<HTMLElement>("video-click-play"),
    elClickPlayBtn: requireEl<HTMLButtonElement>("video-click-play-btn"),
    elSpinner: requireEl<HTMLElement>("video-spinner"),
    elStatusText: requireEl<HTMLElement>("video-status-text"),
    elErrorBox: requireEl<HTMLElement>("video-error"),
    elErrorText: requireEl<HTMLElement>("video-error-text"),
    elPlayerLogoWrap: requireEl<HTMLElement>("player-logo-wrap"),
    elPlayerChno: requireEl<HTMLElement>("player-chno"),
    elPlayerName: requireEl<HTMLElement>("player-name"),
    elPlayerQuality: requireEl<HTMLElement>("player-quality"),
    elPlayerLivePill: requireEl<HTMLElement>("player-live-pill"),
    elPlayerCategory: requireEl<HTMLElement>("player-category"),
    elPlayerNowTitle: requireEl<HTMLElement>("player-now-title"),
    elPlayerNowTime: requireEl<HTMLElement>("player-now-time"),
    elPlayerProgressWrap: requireEl<HTMLElement>("player-progress-wrap"),
    elPlayerProgressBar: requireEl<HTMLElement>("player-progress-bar"),
    elPlayerNowDesc: requireEl<HTMLElement>("player-now-desc"),
    elPlayerUpcomingList: requireEl<HTMLElement>("player-upcoming-list"),
    elSettingsOverlay,
    requireEl,
    syncUrlFromState,
    syncModeButtons,
  });

  const settings = createSettingsController({
    elPlayerOverlay: elOverlay,
    elSettingsBtn,
    elSettingsOverlay,
    elSettingsBackdrop: document.getElementById("settings-backdrop"),
    elSettingsCloseBtn: document.getElementById("close-settings-btn"),
    elSettingsFileBadge: document.getElementById("settings-file-badge"),
    elSettingsTabs: document.getElementById("settings-tabs"),
    elSettingsForm: document.getElementById(
      "settings-form",
    ) as HTMLFormElement | null,
    elSettingsBody: document.getElementById("settings-body"),
    elSettingsStatus: document.getElementById("settings-status"),
    elSettingsReloadBtn: document.getElementById("settings-reload-btn"),
    elSettingsSaveBtn: document.getElementById(
      "settings-save-btn",
    ) as HTMLButtonElement | null,
    redirectToLogin,
  });

  const scheduleEpgCompletionPoll = (): void => {
    if (state.epgPollTimer !== null) return;
    state.epgPollTimer = window.setInterval(async () => {
      try {
        const resp = await fetch("/api/health");
        if (!resp.ok) return;
        const health = (await resp.json()) as HealthApiResponse;
        state.epgStatus = health.epg_status || null;
        if (!state.epgStatus || !state.epgStatus.refreshing) {
          if (state.epgPollTimer !== null) {
            window.clearInterval(state.epgPollTimer);
            state.epgPollTimer = null;
          }
          await loadLineup();
        }
      } catch {
        // Retry on next tick.
      }
    }, 2500);
  };

  const checkAuthStatus = async (): Promise<void> => {
    try {
      const resp = await fetch("/api/auth/status");
      if (!resp.ok) return;
      const data = (await resp.json()) as AuthStatusResponse;
      if (elLogoutBtn) {
        elLogoutBtn.hidden = !data.auth_required;
      }
      if (data.auth_required && !data.authenticated) {
        redirectToLogin();
      }
    } catch {
      // Ignore transient offline status checks.
    }
  };

  const loadLineup = async (): Promise<void> => {
    elRefreshBtn.disabled = true;
    try {
      const resp = await fetch("/api/lineup");
      if (resp.status === 401) {
        redirectToLogin();
        return;
      }
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = (await resp.json()) as LineupApiResponse;
      state.lineupMeta = data.lineup || {};
      state.lineupUpdatedAt =
        typeof state.lineupMeta.lineup_updated_at === "number"
          ? state.lineupMeta.lineup_updated_at
          : null;
      state.epgStatus = data.epg_status || null;
      state.timezone = state.lineupMeta.timezone || DEFAULT_TIMEZONE;
      state.categories = data.categories || [];
      state.channels = data.channels || [];

      const resolvedCat = resolveSelectedCategory(
        state.selectedCategory,
        state.categories,
      );
      if (resolvedCat !== state.selectedCategory) {
        state.selectedCategory = resolvedCat;
        syncUrlFromState({ replace: true });
      }

      if (state.activeChannel) {
        const activeChno = state.activeChannel.chno;
        const refreshedCh = state.channels.find((c) => c.chno === activeChno);
        if (refreshedCh) state.activeChannel = refreshedCh;
      }

      const providerLabel = state.lineupMeta.provider || "Bellhop";
      const marketLabel = state.lineupMeta.market || "";
      elLineupTitle.textContent = marketLabel
        ? `${providerLabel} — ${marketLabel}`
        : providerLabel;
      elLineupBadge.textContent = state.lineupMeta.lineup_id || "LIVE GUIDE";
      document.title = marketLabel
        ? `Bellhop — ${providerLabel} (${marketLabel})`
        : `Bellhop — ${providerLabel}`;

      updateClock();
      syncControlsFromState();
      applyFiltersAndRender();

      if (state.pendingChannel !== null) {
        const targetCh = state.pendingChannel;
        state.pendingChannel = null;
        if (state.channels.some((c) => c.chno === targetCh)) {
          player.openChannelOverlay(targetCh, {
            updateUrl: false,
            fromDeepLink: true,
          });
        } else {
          syncUrlFromState({ replace: true });
        }
      }

      if (state.epgStatus && state.epgStatus.refreshing) {
        scheduleEpgCompletionPoll();
      } else if (state.epgPollTimer !== null) {
        window.clearInterval(state.epgPollTimer);
        state.epgPollTimer = null;
      }
    } catch (err: unknown) {
      elChannelCounter.textContent = `Error loading lineup: ${errorMessage(err)}`;
    } finally {
      elRefreshBtn.disabled = false;
    }
  };

  const triggerEpgRefresh = async (): Promise<void> => {
    const labelSpan = elRefreshBtn.querySelector("span");
    elRefreshBtn.disabled = true;
    if (labelSpan) labelSpan.textContent = "Updating…";
    try {
      await fetch("/api/epg/refresh", { method: "POST" });
    } catch {
      // Still reload lineup below even if remote refresh fails.
    } finally {
      if (labelSpan) labelSpan.textContent = "Refresh";
    }
    await loadLineup();
  };

  // Guide filter & view listeners
  elSearchInput.addEventListener("input", () => {
    state.searchRaw = elSearchInput.value;
    state.searchQuery = state.searchRaw.trim().toLowerCase();
    elSearchClear.hidden = !state.searchQuery;
    syncUrlFromState({ replace: true });
    applyFiltersAndRender();
  });

  elSearchClear.addEventListener("click", () => {
    elSearchInput.value = "";
    state.searchRaw = "";
    state.searchQuery = "";
    elSearchClear.hidden = true;
    syncUrlFromState({ replace: true });
    applyFiltersAndRender();
    elSearchInput.focus();
  });

  elCategoryList.addEventListener("click", (e: MouseEvent) => {
    const target = e.target as HTMLElement | null;
    const btn = target ? target.closest<HTMLElement>(".cat-item") : null;
    const cat = btn?.dataset["cat"];
    if (!cat) return;
    state.selectedCategory = cat;
    syncUrlFromState();
    applyFiltersAndRender();
  });

  if (elHideEmptyEpgBtn) {
    elHideEmptyEpgBtn.addEventListener("click", () => {
      state.hideEmptyGuide = !state.hideEmptyGuide;
      syncControlsFromState();
      syncUrlFromState();
      applyFiltersAndRender();
    });
  }

  document.querySelectorAll<HTMLElement>("[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document
        .querySelectorAll<HTMLElement>("[data-view]")
        .forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.viewMode = btn.dataset["view"] === "grid" ? "grid" : "cards";
      syncUrlFromState();
      applyFiltersAndRender();
    });
  });

  elResetFiltersBtn.addEventListener("click", () => {
    state.selectedCategory = "all";
    state.hideEmptyGuide = false;
    state.searchRaw = "";
    state.searchQuery = "";
    syncControlsFromState();
    syncUrlFromState();
    applyFiltersAndRender();
  });

  elRefreshBtn.addEventListener("click", () => {
    void triggerEpgRefresh();
  });

  if (elLogoutBtn) {
    elLogoutBtn.addEventListener("click", async () => {
      player.destroyActivePlayers(true);
      try {
        await fetch(
          `/api/auth/logout?client=${encodeURIComponent(PAGE_CLIENT_ID)}`,
          { method: "POST" },
        );
      } catch {
        // Proceed to login screen even if logout beacon fails.
      }
      window.location.replace("/login");
    });
  }

  const handleChannelClick = (e: MouseEvent): void => {
    const el = e.target as HTMLElement | null;
    const target = el ? el.closest<HTMLElement>("[data-chno]") : null;
    if (target) {
      player.openChannelOverlay(target.dataset["chno"]);
    }
  };
  elCardsView.addEventListener("click", handleChannelClick);
  elCardsView.addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      const el = e.target as HTMLElement | null;
      const card = el ? el.closest<HTMLElement>("[data-chno]") : null;
      if (card) {
        e.preventDefault();
        player.openChannelOverlay(card.dataset["chno"]);
      }
    }
  });

  elTimelineBody.addEventListener("click", handleChannelClick);
  elTimelineBody.addEventListener("keydown", (e: KeyboardEvent) => {
    const el = e.target as HTMLElement | null;
    const item = el ? el.closest<HTMLElement>("[data-chno]") : null;
    if (!item) return;

    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      player.openChannelOverlay(item.dataset["chno"]);
      return;
    }

    const row = item.closest<HTMLElement>(".tl-row");
    if (!row) return;

    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const rowItems = Array.from(
        row.querySelectorAll<HTMLElement>(
          ".tl-ch-cell, .tl-prog, .tl-empty-hint",
        ),
      );
      const idx = rowItems.indexOf(item);
      if (idx === -1) return;
      const next = rowItems[idx + (e.key === "ArrowRight" ? 1 : -1)];
      if (next) {
        e.preventDefault();
        next.focus();
      }
      return;
    }

    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const allRows = Array.from(
        elTimelineBody.querySelectorAll<HTMLElement>(".tl-row"),
      );
      const rIdx = allRows.indexOf(row);
      if (rIdx === -1) return;
      const targetRow = allRows[rIdx + (e.key === "ArrowDown" ? 1 : -1)];
      const targetCell = targetRow?.querySelector<HTMLElement>(".tl-ch-cell");
      if (targetCell) {
        e.preventDefault();
        targetCell.focus();
      }
    }
  });

  elTimelineHeader.addEventListener("click", (e: MouseEvent) => {
    const el = e.target as HTMLElement | null;
    const shiftBtn = el ? el.closest<HTMLElement>("[data-tl-shift]") : null;
    if (!shiftBtn) return;
    const val = Number(shiftBtn.dataset["tlShift"]);
    if (val === 0) {
      state.timelineOffsetSec = 0;
      elTimelineView.scrollLeft = 0;
    } else {
      state.timelineOffsetSec = Math.max(
        -7200,
        Math.min(8 * 3600, state.timelineOffsetSec + val),
      );
    }
    syncUrlFromState({ replace: true });
    renderTimeline();
  });

  const onPopState = (): void => {
    const targetChno = readStateFromUrl();
    syncControlsFromState();
    applyFiltersAndRender();

    if (targetChno !== null) {
      if (!state.activeChannel || state.activeChannel.chno !== targetChno) {
        player.openChannelOverlay(targetChno, { updateUrl: false });
      }
    } else if (state.activeChannel) {
      void player.closeOverlay({ updateUrl: false });
    }
  };
  window.addEventListener("popstate", onPopState);

  // Delegated capture-phase fallback for broken channel logo images (CSP-safe).
  const onDocumentImageError = (event: Event): void => {
      const target = event.target;
      if (!(target instanceof HTMLImageElement)) return;
      const fallback = target.dataset["logoFallback"];
      if (fallback === undefined) return;
      const span = document.createElement("span");
      span.className = "logo-fallback";
      span.textContent = fallback || "TV";
      target.replaceWith(span);
    };
  document.addEventListener("error", onDocumentImageError, true);

  // Global keyboard shortcuts & modal focus traps.
  const onWindowKeydown = (e: KeyboardEvent): void => {
    const target = e.target as HTMLElement | null;
    const tag = target?.tagName || "";
    const inInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

    if (elSettingsOverlay && !elSettingsOverlay.hidden) {
      if (e.key === "Escape") {
        e.preventDefault();
        settings.closeSettingsDrawer();
      } else if (e.key === "Tab") {
        trapFocusInContainer(elSettingsOverlay, e);
      }
      return;
    }

    if (!elOverlay.hidden) {
      if (e.key === "Escape") {
        e.preventDefault();
        void player.closeOverlay();
      } else if (e.key === "Tab") {
        trapFocusInContainer(elOverlay, e);
      } else if ((e.key === "f" || e.key === "F") && !inInput) {
        e.preventDefault();
        void player.toggleFullscreen();
      } else if (e.key === "ArrowUp" && !inInput) {
        e.preventDefault();
        player.stepChannel(-1);
      } else if (e.key === "ArrowDown" && !inInput) {
        e.preventDefault();
        player.stepChannel(1);
      }
      return;
    }

    if (e.key === "/" && !inInput) {
      e.preventDefault();
      elSearchInput.focus();
    }
  };
  window.addEventListener("keydown", onWindowKeydown);

  const onPageHide = (): void => {
    if (state.activeChannel) {
      navigator.sendBeacon(
        `/api/tuner/release?client=${encodeURIComponent(PAGE_CLIENT_ID)}`,
      );
    }
  };
  window.addEventListener("pagehide", onPageHide);

  // 30s tick: update clock + progress bars in place (only full re-render on show boundary).
  const tickTimerId = window.setInterval(() => {
    updateClock();
    if (state.channels.length === 0) return;
    const nowSec = Math.floor(Date.now() / 1000);
    const boundaryCrossed = refreshScheduleWindows(state.channels, nowSec);
    if (boundaryCrossed) {
      applyFiltersAndRender();
    } else {
      updateLiveProgressInPlace({
        viewMode: state.viewMode,
        elCardsView,
        elTimelineHeader,
        elTimelineBody,
        channels: state.filteredChannels,
        timelineOffsetSec: state.timelineOffsetSec,
        nowSec,
      });
    }
  }, 30000);

  const updatePoller = createLineupUpdatePoller({
    intervalMs:
      window.BellhopUpdateCheckIntervalMs ?? DEFAULT_UPDATE_CHECK_INTERVAL_MS,
    getKnownUpdatedAt: () => state.lineupUpdatedAt,
    fetchHealth: async () => {
      const resp = await fetch("/api/health");
      if (resp.status === 401) {
        redirectToLogin();
        return null;
      }
      if (!resp.ok) return null;
      return (await resp.json()) as HealthApiResponse;
    },
    onHealthChecked: (health) => {
      if (health.epg_status) {
        state.epgStatus = health.epg_status;
        if (state.epgStatus.refreshing) {
          scheduleEpgCompletionPoll();
        }
      }
    },
    onUpdateDetected: async () => {
      await loadLineup();
    },
  });
  updatePoller.start();

  const onVisibilityChange = (): void => {
    if (document.visibilityState === "visible") {
      void updatePoller.checkNow();
    }
  };
  document.addEventListener("visibilitychange", onVisibilityChange);

  if (window.BellhopSettings && elThemeToggleBtn) {
    window.BellhopSettings.bindThemeToggle(elThemeToggleBtn);
  }
  state.pendingChannel = readStateFromUrl();
  syncControlsFromState();
  updateClock();
  void checkAuthStatus();
  void settings.checkSettingsAvailability();
  void loadLineup();

  const destroy = (): void => {
    window.clearInterval(tickTimerId);
    if (state.epgPollTimer !== null) {
      window.clearInterval(state.epgPollTimer);
      state.epgPollTimer = null;
    }
    updatePoller.stop();
    window.removeEventListener("popstate", onPopState);
    document.removeEventListener("error", onDocumentImageError, true);
    window.removeEventListener("keydown", onWindowKeydown);
    window.removeEventListener("pagehide", onPageHide);
    document.removeEventListener("visibilitychange", onVisibilityChange);
  };

  return { destroy };
};

if (typeof document !== "undefined" && document.getElementById("guide-main")) {
  initGuideApp();
}
