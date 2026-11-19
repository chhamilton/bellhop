/**
 * Bellhop — Live TV Guide & Player Frontend.
 *
 * Strictly typed browser UI for channel browsing (Cards & 2D Timeline),
 * single-tuner live playback (`mpegts.js` / `hls.js`), and operational
 * configuration editing via `/api/config`.
 */
(() => {
  "use strict";

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
    autoMuted: boolean;
    streamMode: StreamMode;
    mpegtsPlayer: MpegtsPlayer | null;
    hlsPlayer: HlsInstance | null;
    timezone: string;
    timelineOffsetSec: number;
    epgStatus: EpgStatus | null;
    epgPollTimer: number | null;
  }

  interface ChannelCategoryGroup {
    category_id: string;
    category_name: string;
    channels: PublicChannel[];
  }

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
    autoMuted: false,
    streamMode: "mpegts",
    mpegtsPlayer: null,
    hlsPlayer: null,
    timezone: "America/Montreal",
    timelineOffsetSec: 0,
    epgStatus: null,
    epgPollTimer: null,
  };

  /** Require a DOM element by ID so strict null checks stay concise. */
  const requireEl = <T extends HTMLElement>(id: string): T => {
    const el = document.getElementById(id);
    if (!el) {
      throw new Error(`Missing required DOM element #${id}`);
    }
    return el as T;
  };

  const errorMessage = (err: unknown): string =>
    err instanceof Error ? err.message : String(err);

  // Core DOM Elements
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
  const elHideEmptyEpgBtn = document.getElementById("hide-empty-epg-btn") as HTMLButtonElement | null;
  const elRefreshBtn = requireEl<HTMLButtonElement>("refresh-btn");
  const elLogoutBtn = document.getElementById("logout-btn") as HTMLButtonElement | null;
  const elThemeToggleBtn = document.getElementById("theme-toggle-btn") as HTMLButtonElement | null;
  const elSettingsBtn = document.getElementById("settings-btn") as HTMLButtonElement | null;

  // Settings Drawer DOM
  const elSettingsOverlay = document.getElementById("settings-overlay");
  const elSettingsBackdrop = document.getElementById("settings-backdrop");
  const elSettingsCloseBtn = document.getElementById("close-settings-btn");
  const elSettingsFileBadge = document.getElementById("settings-file-badge");
  const elSettingsTabs = document.getElementById("settings-tabs");
  const elSettingsForm = document.getElementById("settings-form") as HTMLFormElement | null;
  const elSettingsBody = document.getElementById("settings-body");
  const elSettingsStatus = document.getElementById("settings-status");
  const elSettingsReloadBtn = document.getElementById("settings-reload-btn");
  const elSettingsSaveBtn = document.getElementById("settings-save-btn") as HTMLButtonElement | null;

  // Player Overlay DOM
  const elOverlay = requireEl<HTMLElement>("player-overlay");
  const elBackdrop = requireEl<HTMLElement>("player-backdrop");
  const elPlayerStage = requireEl<HTMLElement>("player-stage");
  const elVideo = requireEl<WebkitHTMLVideoElement>("video-el");
  const elUnmuteBtn = requireEl<HTMLButtonElement>("video-unmute-btn");
  const elClickPlayBox = requireEl<HTMLElement>("video-click-play");
  const elClickPlayBtn = requireEl<HTMLButtonElement>("video-click-play-btn");
  const elSpinner = requireEl<HTMLElement>("video-spinner");
  const elStatusText = requireEl<HTMLElement>("video-status-text");
  const elErrorBox = requireEl<HTMLElement>("video-error");
  const elErrorText = requireEl<HTMLElement>("video-error-text");
  const elPlayerLogoWrap = requireEl<HTMLElement>("player-logo-wrap");
  const elPlayerChno = requireEl<HTMLElement>("player-chno");
  const elPlayerName = requireEl<HTMLElement>("player-name");
  const elPlayerQuality = requireEl<HTMLElement>("player-quality");
  const elPlayerLivePill = requireEl<HTMLElement>("player-live-pill");
  const elPlayerCategory = requireEl<HTMLElement>("player-category");
  const elPlayerNowTitle = requireEl<HTMLElement>("player-now-title");
  const elPlayerNowTime = requireEl<HTMLElement>("player-now-time");
  const elPlayerProgressWrap = requireEl<HTMLElement>("player-progress-wrap");
  const elPlayerProgressBar = requireEl<HTMLElement>("player-progress-bar");
  const elPlayerNowDesc = requireEl<HTMLElement>("player-now-desc");
  const elPlayerUpcomingList = requireEl<HTMLElement>("player-upcoming-list");

  const timeFmt = (ts: number | null | undefined): string => {
    if (!ts) return "";
    try {
      return new Intl.DateTimeFormat("en-CA", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        timeZone: state.timezone,
      }).format(new Date(ts * 1000));
    } catch {
      const d = new Date(ts * 1000);
      return d.toTimeString().slice(0, 5);
    }
  };

  const escapeHtml = (str: string | number | null | undefined): string => {
    if (str === null || str === undefined || str === "") return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  };

  const makeInitials = (name: string | null | undefined): string => {
    const words = String(name || "TV")
      .replace(/\([^)]*\)/g, "")
      .trim()
      .split(/\s+/)
      .filter(Boolean);
    return words
      .slice(0, 3)
      .map((w) => w.charAt(0))
      .join("")
      .toUpperCase()
      .slice(0, 4);
  };

  const renderLogoHtml = (ch: PublicChannel): string => {
    const fallback = escapeHtml(makeInitials(ch.callsign || ch.name));
    if (!ch.logo) {
      return `<span class="logo-fallback">${fallback}</span>`;
    }
    return `<img src="${escapeHtml(ch.logo)}" alt="${escapeHtml(ch.name)}" loading="lazy" data-logo-fallback="${fallback}" />`;
  };

  const qualityBadgeClass = (tier: string | null | undefined): string => {
    const t = String(tier || "").toLowerCase();
    if (t === "4k") return "q-4k";
    if (t === "fhd") return "q-fhd";
    if (t === "hd") return "q-hd";
    return "";
  };

  // Clock updater
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

  // Recompute live EPG progress dynamically in the browser.
  const refreshClientEpgWindow = (): void => {
    const nowSec = Math.floor(Date.now() / 1000);
    for (const ch of state.channels) {
      const sched = ch.schedule || [];
      let nowP: ProgramItem | null = null;
      let nextP: ProgramItem | null = null;
      for (const p of sched) {
        if (p.stop_timestamp && p.stop_timestamp <= nowSec) continue;
        if (!nowP && p.start_timestamp <= nowSec && nowSec < p.stop_timestamp) {
          const pct =
            ((nowSec - p.start_timestamp) /
              Math.max(1, p.stop_timestamp - p.start_timestamp)) *
            100;
          nowP = { ...p, progress_pct: Math.min(100, Math.max(0, pct)) };
        } else if (!nextP && p.start_timestamp >= nowSec) {
          nextP = p;
        }
      }
      if (sched.length > 0) {
        ch.now_playing = nowP;
        ch.up_next = nextP;
      }
    }
  };

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

  // =========================================================================
  // URL State Persistence (Query Params + History Back/Forward)
  // =========================================================================
  const parseUrlParams = (): URLSearchParams => {
    // Support standard query params (`?cat=...`) and optional hash fallback (`#cat=...`).
    const searchStr = window.location.search.slice(1);
    const hashStr = window.location.hash.replace(/^#\/?/, "");
    return new URLSearchParams(searchStr || (hashStr.includes("=") ? hashStr : ""));
  };

  const resolveSelectedCategory = (rawCat: string): string => {
    if (!rawCat || rawCat === "all") return "all";
    if (state.categories.length === 0) return rawCat;
    const byId = state.categories.find((c) => String(c.category_id) === rawCat);
    if (byId) return String(byId.category_id);
    const byName = state.categories.find(
      (c) => String(c.category_name || "").toLowerCase() === rawCat.toLowerCase(),
    );
    return byName ? String(byName.category_id) : "all";
  };

  const readStateFromUrl = (): number | null => {
    const params = parseUrlParams();

    const rawCat = (params.get("cat") || "").trim();
    state.selectedCategory = rawCat ? resolveSelectedCategory(rawCat) : "all";

    const rawEpg = (params.get("epg") || params.get("hide_empty") || "")
      .trim()
      .toLowerCase();
    state.hideEmptyGuide =
      rawEpg === "1" || rawEpg === "true" || rawEpg === "only";

    const rawQ = params.get("q") || "";
    state.searchRaw = rawQ;
    state.searchQuery = rawQ.trim().toLowerCase();

    const rawView = (params.get("view") || "").trim().toLowerCase();
    state.viewMode =
      rawView === "timeline" || rawView === "grid" ? "grid" : "cards";

    const rawOffset = params.get("offset");
    if (rawOffset !== null && rawOffset !== "") {
      const parsedMin = Number(rawOffset);
      if (Number.isFinite(parsedMin)) {
        state.timelineOffsetSec = Math.max(
          -7200,
          Math.min(8 * 3600, Math.round(parsedMin) * 60),
        );
      } else {
        state.timelineOffsetSec = 0;
      }
    } else {
      state.timelineOffsetSec = 0;
    }

    const rawMode = (params.get("mode") || "").trim().toLowerCase();
    state.streamMode = rawMode === "hls" ? "hls" : "mpegts";

    const rawCh = (params.get("ch") || "").trim();
    const parsedCh = rawCh ? Number(rawCh) : NaN;
    return Number.isInteger(parsedCh) && parsedCh > 0 ? parsedCh : null;
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

  const buildUrlFromState = (): string => {
    const params = new URLSearchParams();
    if (state.selectedCategory && state.selectedCategory !== "all") {
      params.set("cat", state.selectedCategory);
    }
    if (state.hideEmptyGuide) {
      params.set("epg", "1");
    }
    if (state.searchRaw.trim()) {
      params.set("q", state.searchRaw.trim());
    }
    if (state.viewMode === "grid") {
      params.set("view", "timeline");
      if (state.timelineOffsetSec !== 0) {
        params.set("offset", String(Math.round(state.timelineOffsetSec / 60)));
      }
    }
    if (state.streamMode === "hls") {
      params.set("mode", "hls");
    }
    if (state.activeChannel) {
      params.set("ch", String(state.activeChannel.chno));
    }
    const qs = params.toString();
    return `${window.location.pathname}${qs ? `?${qs}` : ""}`;
  };

  const syncUrlFromState = ({ replace = false }: { replace?: boolean } = {}): void => {
    const targetUrl = buildUrlFromState();
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

  // Fetch lineup & EPG from backend.
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
      state.epgStatus = data.epg_status || null;
      state.timezone = state.lineupMeta.timezone || "America/Montreal";
      state.categories = data.categories || [];
      state.channels = data.channels || [];

      // Reconcile category ID or name from URL now that categories are loaded.
      const resolvedCat = resolveSelectedCategory(state.selectedCategory);
      if (resolvedCat !== state.selectedCategory) {
        state.selectedCategory = resolvedCat;
        syncUrlFromState({ replace: true });
      }

      // Keep active channel reference fresh after periodic EPG reloads.
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

      // Open initial deep-linked channel (`?ch=1101`) once channels are available.
      if (state.pendingChannel !== null) {
        const targetCh = state.pendingChannel;
        state.pendingChannel = null;
        if (state.channels.some((c) => c.chno === targetCh)) {
          openChannelOverlay(targetCh, { updateUrl: false, fromDeepLink: true });
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

  // Trigger an on-demand EPG refresh on the server, then reload lineup.
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

  const hasGuideData = (ch: PublicChannel): boolean =>
    Boolean(
      ch.now_playing ||
        ch.up_next ||
        (Array.isArray(ch.schedule) && ch.schedule.length > 0),
    );

  const matchesCategory = (ch: PublicChannel, catFilter: string): boolean =>
    catFilter === "all" || ch.category_id === catFilter;

  const matchesSecondaryFilters = (ch: PublicChannel): boolean => {
    if (state.hideEmptyGuide && !hasGuideData(ch)) {
      return false;
    }
    if (state.searchQuery) {
      const q = state.searchQuery;
      const nowTitle = ch.now_playing ? ch.now_playing.title.toLowerCase() : "";
      const hay = `${ch.chno} ${ch.sd_chno ?? ""} ${ch.name.toLowerCase()} ${(ch.callsign || "").toLowerCase()} ${ch.category_name.toLowerCase()} ${nowTitle}`;
      if (!hay.includes(q)) return false;
    }
    return true;
  };

  const hasActiveSecondaryFilters = (): boolean =>
    Boolean(state.hideEmptyGuide || state.searchQuery);

  const renderCategories = (): void => {
    const filtering = hasActiveSecondaryFilters();
    const countClassFor = (matched: number): string => {
      if (matched === 0) return "cat-count is-empty";
      if (filtering) return "cat-count is-matched";
      return "cat-count";
    };

    const allTotal = state.channels.length;
    const allMatched = filtering
      ? state.channels.filter(matchesSecondaryFilters).length
      : allTotal;
    const allBadge = filtering ? `${allMatched}/${allTotal}` : String(allTotal);

    let html = `
      <button class="cat-item ${state.selectedCategory === "all" ? "active" : ""}${allMatched === 0 ? " is-empty" : ""}" data-cat="all">
        <span>All Channels</span>
        <span class="${countClassFor(allMatched)}">${allBadge}</span>
      </button>
    `;

    for (const cat of state.categories) {
      const inCat = state.channels.filter((ch) =>
        matchesCategory(ch, cat.category_id),
      );
      const catTotal = inCat.length;
      const catMatched = filtering
        ? inCat.filter(matchesSecondaryFilters).length
        : catTotal;
      const badge = filtering ? `${catMatched}/${catTotal}` : String(catTotal);
      const isActive = state.selectedCategory === cat.category_id;
      html += `
        <button class="cat-item ${isActive ? "active" : ""}${catMatched === 0 ? " is-empty" : ""}" data-cat="${escapeHtml(cat.category_id)}">
          <span>${escapeHtml(cat.category_name)}</span>
          <span class="${countClassFor(catMatched)}">${badge}</span>
        </button>
      `;
    }
    elCategoryList.innerHTML = html;
  };

  const groupChannelsByCategory = (
    channels: readonly PublicChannel[],
  ): ChannelCategoryGroup[] => {
    const byCat = new Map<string, ChannelCategoryGroup>();
    for (const ch of channels) {
      const cid = String(ch.category_id || "default");
      let group = byCat.get(cid);
      if (!group) {
        group = {
          category_id: cid,
          category_name: ch.category_name || "General",
          channels: [],
        };
        byCat.set(cid, group);
      }
      group.channels.push(ch);
    }

    const ordered: ChannelCategoryGroup[] = [];
    const seen = new Set<string>();
    for (const cat of state.categories) {
      const cid = String(cat.category_id);
      const grp = byCat.get(cid);
      if (grp && grp.channels.length > 0) {
        ordered.push(grp);
        seen.add(cid);
      }
    }
    for (const [cid, grp] of byCat.entries()) {
      if (!seen.has(cid) && grp.channels.length > 0) {
        ordered.push(grp);
      }
    }
    return ordered;
  };

  const renderCardHtml = (ch: PublicChannel): string => {
    const qTier =
      ch.quality_tier && ch.quality_tier !== "unknown"
        ? ch.quality_tier.toUpperCase()
        : "";
    const qBadge = qTier
      ? `<span class="quality-badge ${qualityBadgeClass(ch.quality_tier)}">${escapeHtml(qTier)}</span>`
      : "";
    const csText = ch.callsign ? `${escapeHtml(ch.callsign)} • ` : "";

    let epgHtml = `<div class="epg-empty">Live stream ready — no schedule listing at this hour</div>`;
    if (ch.now_playing) {
      const np = ch.now_playing;
      const pct = typeof np.progress_pct === "number" ? np.progress_pct : 0;
      const nextRow = ch.up_next
        ? `<div class="epg-next-row"><strong>Next ${timeFmt(ch.up_next.start_timestamp)}:</strong> ${escapeHtml(ch.up_next.title)}</div>`
        : "";
      const descRow = np.description
        ? `<div class="epg-desc-snippet">${escapeHtml(np.description)}</div>`
        : "";
      epgHtml = `
        <div class="epg-now-header">
          <div class="epg-now-title" title="${escapeHtml(np.title)}">${escapeHtml(np.title)}</div>
          <div class="epg-now-time">${timeFmt(np.start_timestamp)}–${timeFmt(np.stop_timestamp)}</div>
        </div>
        <div class="progress-track"><div class="progress-fill" style="width:${pct.toFixed(1)}%"></div></div>
        ${descRow}
        ${nextRow}
      `;
    } else if (ch.up_next) {
      epgHtml = `
        <div class="epg-next-row" style="border-top:none;padding-top:0">
          <strong>Upcoming ${timeFmt(ch.up_next.start_timestamp)}:</strong> ${escapeHtml(ch.up_next.title)}
        </div>
      `;
    }

    return `
      <article class="channel-card" data-chno="${ch.chno}" tabindex="0" role="button" aria-label="Watch ${escapeHtml(ch.name)}">
        <div class="card-top">
          <div class="logo-tile">${renderLogoHtml(ch)}</div>
          <div class="card-ident">
            <div class="card-title-row">
              <span class="chno-pill">${ch.chno}</span>
              <span class="ch-name" title="${escapeHtml(ch.name)}">${escapeHtml(ch.name)}</span>
              ${qBadge}
            </div>
            <div class="card-sub-row">${csText}${escapeHtml(ch.category_name)}</div>
          </div>
          <div class="play-hint" title="Watch Live">&#9654;</div>
        </div>
        <div class="card-epg">${epgHtml}</div>
      </article>
    `;
  };

  const renderCardsView = (): void => {
    const groups = groupChannelsByCategory(state.filteredChannels);
    let html = "";
    for (const grp of groups) {
      const cardsHtml = grp.channels.map(renderCardHtml).join("");
      html += `
        <div class="category-section">
          <div class="category-section-header">
            <span class="category-section-title">${escapeHtml(grp.category_name)}</span>
            <span class="category-section-count">${grp.channels.length} ${grp.channels.length === 1 ? "channel" : "channels"}</span>
          </div>
          <div class="cards-grid">${cardsHtml}</div>
        </div>
      `;
    }
    elCardsView.innerHTML = html;
  };

  const renderTimelineView = (): void => {
    const prevScrollLeft = elTimelineView.scrollLeft;
    const prevScrollTop = elTimelineView.scrollTop;

    const nowSec = Math.floor(Date.now() / 1000);
    const baseHalfHour = Math.floor(nowSec / 1800) * 1800;
    const windowStart = baseHalfHour + state.timelineOffsetSec;
    const slotCount = 8; // 4 hours (8 x 30-min slots)
    const slotWidthPx = 240; // 240px per 30 mins (8px / min) -> 1920px scrollable track
    const trackWidthPx = slotCount * slotWidthPx;
    const windowDuration = slotCount * 1800;
    const windowEnd = windowStart + windowDuration;
    const pxPerSec = trackWidthPx / windowDuration;

    const showNowLine = nowSec >= windowStart && nowSec <= windowEnd;
    const nowOffsetPx = (nowSec - windowStart) * pxPerSec;

    let ticksHtml = "";
    for (let i = 0; i < slotCount; i += 1) {
      const slotTs = windowStart + i * 1800;
      ticksHtml += `<div class="tl-tick">${timeFmt(slotTs)}</div>`;
    }

    const headerNowHtml = showNowLine
      ? `<div class="tl-now-badge" style="left:${nowOffsetPx.toFixed(1)}px">NOW</div>
         <div class="tl-now-line" style="left:${nowOffsetPx.toFixed(1)}px"></div>`
      : "";

    elTimelineHeader.innerHTML = `
      <div class="tl-corner">
        <span>Channel</span>
        <div class="tl-nav-btns">
          <button type="button" class="tl-nav-btn" data-tl-shift="-3600" title="Back 1 hour">&#9664;</button>
          <button type="button" class="tl-nav-btn" data-tl-shift="0" title="Jump to Current Time">Now</button>
          <button type="button" class="tl-nav-btn" data-tl-shift="3600" title="Forward 1 hour">&#9654;</button>
        </div>
      </div>
      <div class="tl-slots-ruler" style="width:${trackWidthPx}px;min-width:${trackWidthPx}px">
        ${ticksHtml}
        ${headerNowHtml}
      </div>
    `;

    const groups = groupChannelsByCategory(state.filteredChannels);
    const totalRowWidth = 255 + trackWidthPx;
    let rowsHtml = "";

    for (const grp of groups) {
      rowsHtml += `
        <div class="tl-category-row" style="width:${totalRowWidth}px;min-width:${totalRowWidth}px">
          <div class="tl-cat-sticky">
            <span>${escapeHtml(grp.category_name)}</span>
            <span class="tl-cat-count">${grp.channels.length}</span>
          </div>
        </div>
      `;

      for (const ch of grp.channels) {
        let blocksHtml = "";
        const sched = ch.schedule || [];
        let renderedAny = false;

        for (const p of sched) {
          if (!p.start_timestamp || !p.stop_timestamp) continue;
          if (p.stop_timestamp <= windowStart || p.start_timestamp >= windowEnd) {
            continue;
          }

          renderedAny = true;
          const clampedStart = Math.max(windowStart, p.start_timestamp);
          const clampedStop = Math.min(windowEnd, p.stop_timestamp);
          const leftPx = (clampedStart - windowStart) * pxPerSec;
          const widthPx = Math.max(28, (clampedStop - clampedStart) * pxPerSec - 3);
          const isLive = p.start_timestamp <= nowSec && nowSec < p.stop_timestamp;
          const tip = `${p.title} (${timeFmt(p.start_timestamp)}–${timeFmt(p.stop_timestamp)})${p.description ? `\n${p.description}` : ""}`;

          blocksHtml += `
            <div class="tl-prog ${isLive ? "is-live" : ""}" style="left:${leftPx.toFixed(1)}px;width:${widthPx.toFixed(1)}px" title="${escapeHtml(tip)}" data-chno="${ch.chno}">
              <div class="tl-prog-title">${escapeHtml(p.title)}</div>
              <div class="tl-prog-time">${timeFmt(p.start_timestamp)}–${timeFmt(p.stop_timestamp)}</div>
            </div>
          `;
        }

        if (!renderedAny) {
          blocksHtml += `<div class="tl-empty-hint" data-chno="${ch.chno}">Live broadcast — click to watch ${escapeHtml(ch.name)}</div>`;
        }

        const subLabel = ch.callsign
          ? escapeHtml(ch.callsign)
          : escapeHtml(
              ch.quality_tier && ch.quality_tier !== "unknown"
                ? ch.quality_tier.toUpperCase()
                : "LIVE",
            );

        rowsHtml += `
          <div class="tl-row">
            <div class="tl-ch-cell" data-chno="${ch.chno}" title="${escapeHtml(`${ch.chno} ${ch.name}`)}">
              <span class="tl-ch-num">${ch.chno}</span>
              <div class="tl-logo">${renderLogoHtml(ch)}</div>
              <div class="tl-ch-meta">
                <div class="tl-ch-name">${escapeHtml(ch.name)}</div>
                <div class="tl-ch-sub">${subLabel}</div>
              </div>
            </div>
            <div class="tl-track" style="width:${trackWidthPx}px;min-width:${trackWidthPx}px" data-chno="${ch.chno}">${blocksHtml}</div>
          </div>
        `;
      }
    }

    const bodyNowHtml = showNowLine
      ? `<div class="tl-now-line" style="left:${(255 + nowOffsetPx).toFixed(1)}px"></div>`
      : "";
    elTimelineBody.innerHTML = `${rowsHtml}${bodyNowHtml}`;
    elTimelineView.scrollLeft = prevScrollLeft;
    elTimelineView.scrollTop = prevScrollTop;
  };

  const applyFiltersAndRender = (): void => {
    refreshClientEpgWindow();
    const categoryChannels = state.channels.filter((ch) =>
      matchesCategory(ch, state.selectedCategory),
    );
    state.filteredChannels = categoryChannels.filter(matchesSecondaryFilters);

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
    renderCategories();

    const isEmpty = state.filteredChannels.length === 0;
    elEmptyState.hidden = !isEmpty;

    const isTimeline = state.viewMode === "grid";
    elGuideMain.classList.toggle("is-timeline", isTimeline && !isEmpty);

    if (!isTimeline) {
      elCardsView.hidden = isEmpty;
      elTimelineView.hidden = true;
      if (!isEmpty) renderCardsView();
    } else {
      elCardsView.hidden = true;
      elTimelineView.hidden = isEmpty;
      if (!isEmpty) renderTimelineView();
    }
  };

  // =========================================================================
  // Video Player Overlay & Single-Tuner Lifecycle
  // =========================================================================
  const destroyActivePlayers = (notifyServerRelease = false): void => {
    if (state.mpegtsPlayer) {
      try {
        state.mpegtsPlayer.pause();
        state.mpegtsPlayer.unload();
        state.mpegtsPlayer.detachMediaElement();
        state.mpegtsPlayer.destroy();
      } catch {
        // Ignore media teardown errors on rapid channel switches.
      }
      state.mpegtsPlayer = null;
    }
    if (state.hlsPlayer) {
      try {
        state.hlsPlayer.destroy();
      } catch {
        // Ignore HLS teardown errors.
      }
      state.hlsPlayer = null;
    }
    try {
      elVideo.pause();
      elVideo.removeAttribute("src");
      elVideo.load();
    } catch {
      // Ignore video reset errors.
    }

    if (notifyServerRelease) {
      fetch(`/api/tuner/release?client=${encodeURIComponent(PAGE_CLIENT_ID)}`, {
        method: "POST",
        keepalive: true,
      }).catch(() => {});
    }
    elUnmuteBtn.hidden = true;
    elClickPlayBox.hidden = true;
  };

  const setPlayerStatus = (
    status: "connecting" | "live" | "error",
    message = "",
  ): void => {
    elPlayerLivePill.classList.remove("is-live", "is-error");
    if (status === "connecting") {
      elPlayerLivePill.textContent = "TUNING…";
      elStatusText.textContent = message || "Connecting to live stream…";
      elSpinner.hidden = false;
      elErrorBox.hidden = true;
      elClickPlayBox.hidden = true;
    } else if (status === "live") {
      elPlayerLivePill.textContent = `LIVE • ${state.streamMode === "mpegts" ? "TS" : "HLS"}`;
      elPlayerLivePill.classList.add("is-live");
      elSpinner.hidden = true;
      elErrorBox.hidden = true;
      elClickPlayBox.hidden = true;
    } else {
      elPlayerLivePill.textContent = "OFFLINE / ERROR";
      elPlayerLivePill.classList.add("is-error");
      elSpinner.hidden = true;
      elClickPlayBox.hidden = true;
      elUnmuteBtn.hidden = true;
      elErrorText.textContent =
        message || "Unable to play stream in current mode.";
      elErrorBox.hidden = false;
    }
  };

  const populateOverlayEpg = async (ch: PublicChannel): Promise<void> => {
    const renderProgramPanel = (programs: readonly ProgramItem[]): void => {
      const nowSec = Math.floor(Date.now() / 1000);
      const nowProg =
        programs.find(
          (p) => p.start_timestamp <= nowSec && nowSec < p.stop_timestamp,
        ) || ch.now_playing;
      const upcoming = programs
        .filter((p) => p.start_timestamp > nowSec)
        .slice(0, 6);

      if (nowProg) {
        elPlayerNowTitle.textContent = nowProg.title;
        elPlayerNowTime.textContent = `${timeFmt(nowProg.start_timestamp)} – ${timeFmt(nowProg.stop_timestamp)}`;
        const pct =
          ((nowSec - nowProg.start_timestamp) /
            Math.max(1, nowProg.stop_timestamp - nowProg.start_timestamp)) *
          100;
        elPlayerProgressWrap.hidden = false;
        elPlayerProgressBar.style.width = `${Math.min(100, Math.max(0, pct)).toFixed(1)}%`;
        elPlayerNowDesc.textContent =
          nowProg.description || "Live broadcast in progress.";
      } else {
        elPlayerNowTitle.textContent = ch.name;
        elPlayerNowTime.textContent = "Live Broadcast";
        elPlayerProgressWrap.hidden = true;
        elPlayerNowDesc.textContent =
          "No program description listed for this time slot.";
      }

      if (upcoming.length > 0) {
        elPlayerUpcomingList.innerHTML = upcoming
          .map(
            (p) => `
            <div class="upcoming-item">
              <span class="upcoming-time">${timeFmt(p.start_timestamp)}–${timeFmt(p.stop_timestamp)}</span>
              <span class="upcoming-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title)}</span>
            </div>`,
          )
          .join("");
      } else {
        elPlayerUpcomingList.innerHTML = `<div class="epg-empty">No upcoming listings available.</div>`;
      }
    };

    renderProgramPanel(ch.schedule || []);
    try {
      const resp = await fetch(`/api/epg/${ch.chno}`);
      if (resp.ok) {
        const data = (await resp.json()) as ChannelEpgResponse;
        if (
          state.activeChannel &&
          state.activeChannel.chno === ch.chno &&
          Array.isArray(data.programs)
        ) {
          renderProgramPanel(data.programs);
        }
      }
    } catch {
      // Keep locally cached schedule if EPG detail fetch fails.
    }
  };

  const attemptPlayback = async (
    playFn: () => Promise<void> | void,
  ): Promise<void> => {
    try {
      const p = playFn();
      if (p && typeof p.then === "function") {
        await p;
      }
      elClickPlayBox.hidden = true;
      if (state.autoMuted && elVideo.muted) {
        elUnmuteBtn.hidden = false;
      }
    } catch (err: unknown) {
      if (!state.activeChannel) return;
      const errName = err instanceof Error ? err.name : "";
      const errMsg = errorMessage(err);
      const isAutoplayBlocked =
        errName === "NotAllowedError" ||
        /autoplay|user gesture|didn't interact|not allowed/i.test(errMsg);
      if (!isAutoplayBlocked) return;

      // Browser blocked unmuted autoplay (e.g. page reload with `?ch=...`):
      // retry muted so live video starts immediately, with a one-click Unmute pill.
      if (!elVideo.muted) {
        elVideo.muted = true;
        state.autoMuted = true;
        try {
          const p2 = playFn();
          if (p2 && typeof p2.then === "function") {
            await p2;
          }
          elClickPlayBox.hidden = true;
          elUnmuteBtn.hidden = false;
          return;
        } catch {
          // Fall through to explicit click-to-play button if even muted autoplay is blocked.
        }
      }
      elSpinner.hidden = true;
      elClickPlayBox.hidden = false;
    }
  };

  const startPlayback = (
    ch: PublicChannel,
    { preferMuted = false }: { preferMuted?: boolean } = {},
  ): void => {
    // Server's /api/stream/<chno>.ts preempts any prior session automatically.
    destroyActivePlayers(false);
    setPlayerStatus("connecting", `Tuning ${ch.chno} ${ch.name}…`);

    if (preferMuted) {
      elVideo.muted = true;
      state.autoMuted = true;
      elUnmuteBtn.hidden = false;
    } else if (state.autoMuted) {
      elVideo.muted = false;
      state.autoMuted = false;
      elUnmuteBtn.hidden = true;
    }

    const cacheBuster = Date.now();
    const clientParam = encodeURIComponent(PAGE_CLIENT_ID);

    if (state.streamMode === "mpegts") {
      if (window.mpegts && window.mpegts.isSupported()) {
        const absStreamUrl = new URL(
          `${ch.stream_url}?t=${cacheBuster}&client=${clientParam}`,
          window.location.origin,
        ).href;
        const player = window.mpegts.createPlayer(
          {
            type: "mpegts",
            isLive: true,
            url: absStreamUrl,
          },
          {
            enableWorker: true,
            enableStashBuffer: false,
            stashInitialSize: 128 * 1024,
            liveBufferLatencyChasing: true,
            liveBufferLatencyMaxLatency: 4.0,
            liveBufferLatencyMinRemain: 0.8,
          },
        );
        state.mpegtsPlayer = player;
        player.attachMediaElement(elVideo);
        player.on(window.mpegts.Events.ERROR, (_errType, errDetail) => {
          if (state.mpegtsPlayer !== player) return;
          setPlayerStatus(
            "error",
            `MPEG-TS playback error (${errDetail || "stream interrupted"}). Try switching to HLS mode.`,
          );
        });
        player.load();
        void attemptPlayback(() => player.play());
      } else {
        state.streamMode = "hls";
        syncModeButtons();
        syncUrlFromState({ replace: true });
        startPlayback(ch, { preferMuted });
        return;
      }
    } else {
      const hlsUrl = `${ch.hls_url}?t=${cacheBuster}&client=${clientParam}`;
      if (window.Hls && window.Hls.isSupported()) {
        const hls = new window.Hls({
          lowLatencyMode: true,
          maxBufferLength: 15,
        });
        state.hlsPlayer = hls;
        hls.loadSource(hlsUrl);
        hls.attachMedia(elVideo);
        hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
          void attemptPlayback(() => elVideo.play());
        });
        hls.on(window.Hls.Events.ERROR, (_ev, data) => {
          if (data && data.fatal && state.hlsPlayer === hls) {
            setPlayerStatus(
              "error",
              `HLS error: ${data.details || "manifest/network failure"}. Try MPEG-TS mode.`,
            );
          }
        });
      } else if (elVideo.canPlayType("application/vnd.apple.mpegurl")) {
        elVideo.src = hlsUrl;
        void attemptPlayback(() => elVideo.play());
      } else {
        setPlayerStatus("error", "HLS playback is not supported in this browser.");
      }
    }
  };

  elVideo.addEventListener("canplay", () => {
    if (state.activeChannel && elVideo.paused && elErrorBox.hidden) {
      void attemptPlayback(() => elVideo.play());
    }
  });

  elVideo.addEventListener("playing", () => {
    if (state.activeChannel) {
      setPlayerStatus("live");
      if (state.autoMuted && elVideo.muted) {
        elUnmuteBtn.hidden = false;
      }
    }
  });

  elVideo.addEventListener("volumechange", () => {
    if (!elVideo.muted) {
      state.autoMuted = false;
      elUnmuteBtn.hidden = true;
    }
  });

  elVideo.addEventListener("waiting", () => {
    if (state.activeChannel && elErrorBox.hidden && elClickPlayBox.hidden) {
      elSpinner.hidden = false;
      elStatusText.textContent = "Buffering live stream…";
    }
  });

  const openChannelOverlay = (
    chno: number | string | undefined,
    {
      updateUrl = true,
      replaceUrl = false,
      fromDeepLink = false,
    }: { updateUrl?: boolean; replaceUrl?: boolean; fromDeepLink?: boolean } = {},
  ): void => {
    const ch = state.channels.find((c) => c.chno === Number(chno));
    if (!ch) return;
    state.activeChannel = ch;

    elPlayerLogoWrap.innerHTML = renderLogoHtml(ch);
    elPlayerChno.textContent = String(ch.chno);
    elPlayerName.textContent = ch.name;
    elPlayerCategory.textContent = `${ch.callsign ? `${ch.callsign} • ` : ""}${ch.category_name}`;

    if (ch.quality_tier && ch.quality_tier !== "unknown") {
      elPlayerQuality.hidden = false;
      elPlayerQuality.className = `quality-badge ${qualityBadgeClass(ch.quality_tier)}`;
      elPlayerQuality.textContent = ch.quality_tier.toUpperCase();
    } else {
      elPlayerQuality.hidden = true;
    }

    elOverlay.hidden = false;
    document.body.style.overflow = "hidden";

    if (updateUrl) {
      syncUrlFromState({ replace: replaceUrl });
    }

    void populateOverlayEpg(ch);
    startPlayback(ch, { preferMuted: fromDeepLink });
  };

  const closeOverlay = async ({
    updateUrl = true,
    replaceUrl = false,
  }: { updateUrl?: boolean; replaceUrl?: boolean } = {}): Promise<void> => {
    if (document.fullscreenElement) {
      try {
        await document.exitFullscreen();
      } catch {
        // Ignore fullscreen exit errors.
      }
    }
    state.activeChannel = null;
    elOverlay.hidden = true;
    document.body.style.overflow = "";
    destroyActivePlayers(true);
    if (updateUrl) {
      syncUrlFromState({ replace: replaceUrl });
    }
  };

  const toggleFullscreen = async (): Promise<void> => {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else if (elPlayerStage.requestFullscreen) {
        await elPlayerStage.requestFullscreen();
      } else if (elVideo.webkitEnterFullscreen) {
        elVideo.webkitEnterFullscreen();
      }
    } catch {
      // Ignore fullscreen permission errors.
    }
  };

  const stepChannel = (delta: number): void => {
    if (!state.activeChannel) return;
    const activeChno = state.activeChannel.chno;
    const pool =
      state.filteredChannels.length > 0 ? state.filteredChannels : state.channels;
    if (pool.length === 0) return;
    const idx = pool.findIndex((c) => c.chno === activeChno);
    const nextIdx = idx === -1 ? 0 : (idx + delta + pool.length) % pool.length;
    const target = pool[nextIdx];
    if (target) {
      openChannelOverlay(target.chno);
    }
  };

  // =========================================================================
  // Event Listeners
  // =========================================================================
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
      destroyActivePlayers(true);
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

  // Delegate clicks on channel cards & timeline rows.
  const handleChannelClick = (e: MouseEvent): void => {
    const el = e.target as HTMLElement | null;
    const target = el ? el.closest<HTMLElement>("[data-chno]") : null;
    if (target) {
      openChannelOverlay(target.dataset["chno"]);
    }
  };
  elCardsView.addEventListener("click", handleChannelClick);
  elCardsView.addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      const el = e.target as HTMLElement | null;
      const card = el ? el.closest<HTMLElement>("[data-chno]") : null;
      if (card) {
        e.preventDefault();
        openChannelOverlay(card.dataset["chno"]);
      }
    }
  });
  elTimelineBody.addEventListener("click", handleChannelClick);
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
    renderTimelineView();
  });

  // Overlay controls
  requireEl<HTMLElement>("close-player-btn").addEventListener("click", () => {
    void closeOverlay();
  });
  elBackdrop.addEventListener("click", () => {
    void closeOverlay();
  });
  elUnmuteBtn.addEventListener("click", () => {
    elVideo.muted = false;
    state.autoMuted = false;
    elUnmuteBtn.hidden = true;
    if (elVideo.paused) {
      elVideo.play().catch(() => {});
    }
  });
  elClickPlayBtn.addEventListener("click", () => {
    elVideo.muted = false;
    state.autoMuted = false;
    elUnmuteBtn.hidden = true;
    elClickPlayBox.hidden = true;
    elVideo.play().catch(() => {});
  });
  requireEl<HTMLElement>("fullscreen-btn").addEventListener("click", () => {
    void toggleFullscreen();
  });
  elPlayerStage.addEventListener("dblclick", () => {
    void toggleFullscreen();
  });
  requireEl<HTMLElement>("prev-ch-btn").addEventListener("click", () =>
    stepChannel(-1),
  );
  requireEl<HTMLElement>("next-ch-btn").addEventListener("click", () =>
    stepChannel(1),
  );

  requireEl<HTMLElement>("pip-btn").addEventListener("click", async () => {
    try {
      if (document.pictureInPictureElement) {
        await document.exitPictureInPicture();
      } else if (elVideo.requestPictureInPicture) {
        await elVideo.requestPictureInPicture();
      }
    } catch {
      // Ignore PiP rejection errors.
    }
  });

  document.querySelectorAll<HTMLElement>("[data-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.streamMode = btn.dataset["mode"] === "hls" ? "hls" : "mpegts";
      syncModeButtons();
      syncUrlFromState({ replace: true });
      if (state.activeChannel) startPlayback(state.activeChannel);
    });
  });

  requireEl<HTMLElement>("retry-stream-btn").addEventListener("click", () => {
    if (state.activeChannel) startPlayback(state.activeChannel);
  });

  requireEl<HTMLElement>("switch-mode-btn").addEventListener("click", () => {
    state.streamMode = state.streamMode === "mpegts" ? "hls" : "mpegts";
    syncModeButtons();
    syncUrlFromState({ replace: true });
    if (state.activeChannel) startPlayback(state.activeChannel);
  });

  // Restore UI state on browser Back / Forward navigation.
  window.addEventListener("popstate", () => {
    const targetChno = readStateFromUrl();
    syncControlsFromState();
    applyFiltersAndRender();

    if (targetChno !== null) {
      if (!state.activeChannel || state.activeChannel.chno !== targetChno) {
        openChannelOverlay(targetChno, { updateUrl: false });
      }
    } else if (state.activeChannel) {
      void closeOverlay({ updateUrl: false });
    }
  });

  // =========================================================================
  // Pipeline & Xtream Settings Drawer (Operational Config Only)
  // =========================================================================
  let activeSettingsTab = "xtream_server";

  const setSettingsStatus = (
    msg: string,
    kind: SettingsStatusKind = "",
  ): void => {
    if (!elSettingsStatus) return;
    elSettingsStatus.textContent = msg;
    elSettingsStatus.className = `settings-status${kind ? ` is-${kind}` : ""}`;
  };

  const renderTextField = (
    id: string,
    label: string,
    value: string | null | undefined,
    {
      placeholder = "",
      hint = "",
      span2 = false,
    }: { placeholder?: string; hint?: string; span2?: boolean } = {},
  ): string => `
    <div class="settings-field${span2 ? " span-2" : ""}">
      <label for="${id}">${escapeHtml(label)}</label>
      <input id="${id}" type="text" value="${escapeHtml(value ?? "")}" placeholder="${escapeHtml(placeholder)}" />
      ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
    </div>
  `;

  const renderNumberField = (
    id: string,
    label: string,
    value: number | null | undefined,
    {
      step = "any",
      min = "",
      placeholder = "",
      hint = "",
    }: { step?: string; min?: string; placeholder?: string; hint?: string } = {},
  ): string => `
    <div class="settings-field">
      <label for="${id}">${escapeHtml(label)}</label>
      <input id="${id}" type="number" step="${step}" ${min !== "" ? `min="${min}"` : ""} value="${value === null || value === undefined ? "" : escapeHtml(String(value))}" placeholder="${escapeHtml(placeholder)}" />
      ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
    </div>
  `;

  const renderSelectField = (
    id: string,
    label: string,
    value: string | undefined,
    options: readonly (readonly [string, string])[],
    { hint = "" }: { hint?: string } = {},
  ): string => `
    <div class="settings-field">
      <label for="${id}">${escapeHtml(label)}</label>
      <select id="${id}">
        ${options
          .map(
            ([optVal, optLabel]) =>
              `<option value="${escapeHtml(optVal)}"${String(value) === String(optVal) ? " selected" : ""}>${escapeHtml(optLabel)}</option>`,
          )
          .join("")}
      </select>
      ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
    </div>
  `;

  const renderCheckboxField = (
    id: string,
    label: string,
    checked: boolean,
  ): string => `
    <label class="settings-check" for="${id}">
      <input id="${id}" type="checkbox"${checked ? " checked" : ""} />
      <span>${escapeHtml(label)}</span>
    </label>
  `;

  const syncSettingsTabVisibility = (): void => {
    if (!elSettingsTabs || !elSettingsBody) return;
    elSettingsTabs
      .querySelectorAll<HTMLElement>("[data-settings-tab]")
      .forEach((btn) => {
        btn.classList.toggle(
          "active",
          btn.dataset["settingsTab"] === activeSettingsTab,
        );
      });
    elSettingsBody
      .querySelectorAll<HTMLElement>("[data-settings-pane]")
      .forEach((pane) => {
        pane.hidden = pane.dataset["settingsPane"] !== activeSettingsTab;
      });
  };

  const renderSettingsForm = (cfg: PublicConfig): void => {
    if (!elSettingsBody) return;
    const sx = cfg.xtream_server || {};
    const xt = cfg.upstream_xtream || {};
    const pipe = cfg.pipeline || {};
    const lu = pipe.lineup || {};
    const tvg = lu.tvguide || {};
    const sd = lu.schedules_direct || {};
    const match = pipe.matching || {};
    const val = pipe.validation || {};
    const epg = pipe.epg || {};
    const out = pipe.output || {};

    const pinsText = Object.entries(lu.pinned_streams || {})
      .map(([k, v]) => `${k}=${v}`)
      .join("\n");
    const exclText = (lu.excluded_channels || []).join(", ");

    elSettingsBody.innerHTML = `
      <div class="settings-pane" data-settings-pane="xtream_server">
        <div class="settings-group">
          <h3>Xtream Codes Passthrough API Server</h3>
          ${renderCheckboxField("cfg-sx-enabled", "Enable passthrough Xtream Codes API (/player_api.php, /get.php, /xmltv.php)", sx.enabled !== false)}
          <div class="settings-grid">
            ${renderTextField("cfg-sx-host", "Bind Host Override (optional)", sx.host, { placeholder: "Share Web Guide host", hint: "Leave blank to share the Web Guide listener host" })}
            ${renderNumberField("cfg-sx-port", "Bind Port Override (optional)", sx.port, { step: "1", min: "1", placeholder: "Share Web Guide port", hint: "Leave blank to share the Web Guide port, or set a dedicated port" })}
            ${renderTextField("cfg-sx-pub-host", "Advertised Public Host (optional)", sx.public_host, { placeholder: "Auto from Host header / LAN IP" })}
            ${renderNumberField("cfg-sx-pub-port", "Advertised Public Port (optional)", sx.public_port, { step: "1", min: "1", placeholder: "Auto from Host header" })}
            ${renderSelectField("cfg-sx-proto", "Advertised Protocol", sx.server_protocol || "http", [["http", "http"], ["https", "https"]])}
          </div>
        </div>
      </div>

      <div class="settings-pane" data-settings-pane="upstream_xtream" hidden>
        <div class="settings-group">
          <h3>Upstream Xtream IPTV Source</h3>
          <div class="settings-grid">
            ${renderTextField("cfg-xt-server", "Upstream Server URL", xt.server_url, { span2: true, placeholder: "http://provider.example" })}
            ${renderSelectField("cfg-xt-format", "Preferred Stream Format", xt.stream_format || "ts", [["ts", "ts (MPEG-TS)"], ["m3u8", "m3u8 (HLS)"]])}
            ${renderNumberField("cfg-xt-timeout", "API Request Timeout (sec)", xt.request_timeout_sec, { step: "1", min: "1" })}
            ${renderNumberField("cfg-xt-refresh", "Catalog Refresh TTL (hours)", xt.refresh_hours, { step: "1", min: "0", hint: "How often Phase (2) refreshes the upstream catalog" })}
          </div>
        </div>
      </div>

      <div class="settings-pane" data-settings-pane="lineup" hidden>
        <div class="settings-group">
          <h3>Lineup Source &amp; Numbering</h3>
          <div class="settings-grid">
            ${renderSelectField("cfg-lu-backend", "Lineup Backend", lu.backend || "auto", [
              ["auto", "auto"],
              ["yaml", "yaml (Curated File)"],
              ["tvguide", "tvguide (Free US/CA)"],
              ["schedules_direct", "schedules_direct (SD-JSON)"],
            ])}
            ${renderTextField("cfg-lu-id", "Lineup ID", lu.lineup_id)}
            ${renderTextField("cfg-lu-postal", "Postal / ZIP Code", lu.postal_code)}
            ${renderNumberField("cfg-lu-refresh", "Lineup Refresh Cadence (days)", lu.refresh_days, { step: "1", min: "0" })}
            ${renderSelectField("cfg-lu-numbering", "Channel Numbering Mode", lu.numbering_mode || "both", [
              ["both", "both (SD + HD)"],
              ["hd", "hd (HD preferred)"],
              ["sd", "sd (SD only)"],
            ])}
            ${renderNumberField("cfg-match-hysteresis", "Upgrade Hysteresis Score", match.upgrade_hysteresis_score, { step: "0.5", min: "0", hint: "Score margin required before replacing an existing validated stream" })}
          </div>
          ${renderCheckboxField("cfg-lu-collapse", "Collapse SD duplicates into HD slots", Boolean(lu.collapse_sd_dupes))}
          ${renderCheckboxField("cfg-lu-excl-audio", "Exclude Music & Audio channels", Boolean(lu.exclude_audio))}
        </div>

        <div class="settings-group">
          <h3>Channel Overrides &amp; Provider Endpoints</h3>
          <div class="settings-grid">
            <div class="settings-field span-2">
              <label for="cfg-lu-pins">Pinned Streams (one <code>chno=stream_id</code> per line)</label>
              <textarea id="cfg-lu-pins" placeholder="1108=410108">${escapeHtml(pinsText)}</textarea>
            </div>
            ${renderTextField("cfg-lu-excl", "Excluded Channel Numbers (comma-separated)", exclText, { placeholder: "1107, 1424", span2: true })}
            ${renderTextField("cfg-lu-tvg-url", "TV Guide Base URL", tvg.base_url, { span2: true })}
            ${renderTextField("cfg-lu-sd-url", "Schedules Direct Base URL", sd.base_url, { span2: true })}
          </div>
          ${renderCheckboxField("cfg-lu-sd-autoadd", "Auto-add Schedules Direct lineup to account if unsubscribed", Boolean(sd.auto_add_lineup))}
        </div>
      </div>

      <div class="settings-pane" data-settings-pane="validation" hidden>
        <div class="settings-group">
          <h3>Candidate Stream Validation</h3>
          ${renderCheckboxField("cfg-val-enabled", "Enable Phase (4) live stream validation", val.enabled !== false)}
          ${renderCheckboxField("cfg-val-only-changed", "Only probe changed or previously unvalidated slots", val.only_changed !== false)}
          ${renderCheckboxField("cfg-val-ignore-limit", "Ignore upstream max_connections account limit (use cautiously)", Boolean(val.ignore_account_limit))}
          ${renderCheckboxField("cfg-val-ffprobe", "Use ffprobe codec/stream verification", val.use_ffprobe !== false)}
          <div class="settings-grid">
            ${renderNumberField("cfg-val-concurrency", "Probe Concurrency", val.concurrency, { step: "1", min: "1" })}
            ${renderNumberField("cfg-val-delay", "Inter-Probe Delay (sec)", val.inter_probe_delay_sec, { step: "0.05", min: "0" })}
            ${renderNumberField("cfg-val-http-timeout", "HTTP Probe Timeout (sec)", val.http_timeout_sec, { step: "0.5", min: "1" })}
            ${renderNumberField("cfg-val-min-bytes", "Minimum Bytes to Read", val.min_bytes_to_read, { step: "512", min: "512" })}
            ${renderNumberField("cfg-val-ffprobe-timeout", "ffprobe Timeout (sec)", val.ffprobe_timeout_sec, { step: "0.5", min: "1" })}
            ${renderNumberField("cfg-val-max-cands", "Max Candidates Per Channel", val.max_candidates_per_channel, { step: "1", min: "1" })}
          </div>
        </div>
      </div>

      <div class="settings-pane" data-settings-pane="epg_output" hidden>
        <div class="settings-group">
          <h3>EPG Guide Passthrough &amp; Refresh</h3>
          ${renderCheckboxField("cfg-epg-verify", "Verify upstream XMLTV passthrough during pipeline export", epg.verify !== false)}
          <div class="settings-grid">
            ${renderNumberField("cfg-epg-refresh", "Web Guide EPG Cache TTL (hours)", epg.refresh_hours, { step: "0.5", min: "0", hint: "How often bellhop serve refreshes cached EPG guide data" })}
          </div>
        </div>

        <div class="settings-group">
          <h3>Pipeline Export Toggles</h3>
          ${renderCheckboxField("cfg-out-m3u8-en", "Enable M3U8 playlist & diagnostics report export", out.m3u8_enabled !== false)}
          ${renderCheckboxField("cfg-out-xt-en", "Enable Xtream JSON playlist export", out.xtream_enabled !== false)}
          ${renderCheckboxField("cfg-out-unmatched", "Include unmatched channel comments in M3U8", Boolean(out.include_unmatched_comment))}
        </div>
      </div>
    `;

    syncSettingsTabVisibility();
  };

  const collectSettingsPayload = (): PublicConfig => {
    const valStr = (id: string): string => {
      const el = document.getElementById(id) as
        | HTMLInputElement
        | HTMLSelectElement
        | HTMLTextAreaElement
        | null;
      return (el?.value ?? "").trim();
    };
    const optStr = (id: string): string | null => {
      const s = valStr(id);
      return s ? s : null;
    };
    const reqNum = (id: string): number => Number(valStr(id));
    const optInt = (id: string): number | null => {
      const s = valStr(id);
      return s ? Number(s) : null;
    };
    const isChecked = (id: string): boolean => {
      const el = document.getElementById(id) as HTMLInputElement | null;
      return Boolean(el?.checked);
    };

    // Parse pinned streams (`chno=stream_id` lines or JSON object).
    const rawPins = valStr("cfg-lu-pins");
    const pinnedStreams: Record<string, number> = {};
    if (rawPins) {
      if (rawPins.startsWith("{")) {
        const parsed = JSON.parse(rawPins) as Record<string, unknown>;
        for (const [k, v] of Object.entries(parsed)) {
          pinnedStreams[String(k).trim()] = Number(v);
        }
      } else {
        for (const line of rawPins.split(/\r?\n/)) {
          const clean = line.trim();
          if (!clean || clean.startsWith("#")) continue;
          const sep = clean.includes("=") ? "=" : ":";
          const parts = clean.split(sep);
          const left = parts[0];
          const right = parts[1];
          if (parts.length !== 2 || left === undefined || right === undefined) {
            throw new Error(
              `Invalid pinned stream entry: "${clean}" (use chno=stream_id)`,
            );
          }
          pinnedStreams[left.trim()] = Number(right.trim());
        }
      }
    }

    const rawExcl = valStr("cfg-lu-excl");
    const excludedChannels: number[] = rawExcl
      ? rawExcl
          .split(/[,\s]+/)
          .map((t) => t.trim())
          .filter(Boolean)
          .map((t) => Number(t))
      : [];

    return {
      upstream_xtream: {
        server_url: valStr("cfg-xt-server"),
        stream_format: valStr("cfg-xt-format"),
        request_timeout_sec: reqNum("cfg-xt-timeout"),
        refresh_hours: reqNum("cfg-xt-refresh"),
      },
      xtream_server: {
        enabled: isChecked("cfg-sx-enabled"),
        host: optStr("cfg-sx-host"),
        port: optInt("cfg-sx-port"),
        public_host: optStr("cfg-sx-pub-host"),
        public_port: optInt("cfg-sx-pub-port"),
        server_protocol: valStr("cfg-sx-proto"),
      },
      pipeline: {
        lineup: {
          backend: valStr("cfg-lu-backend"),
          lineup_id: valStr("cfg-lu-id"),
          postal_code: valStr("cfg-lu-postal"),
          refresh_days: reqNum("cfg-lu-refresh"),
          numbering_mode: valStr("cfg-lu-numbering"),
          collapse_sd_dupes: isChecked("cfg-lu-collapse"),
          exclude_audio: isChecked("cfg-lu-excl-audio"),
          pinned_streams: pinnedStreams,
          excluded_channels: excludedChannels,
          tvguide: { base_url: valStr("cfg-lu-tvg-url") },
          schedules_direct: {
            base_url: valStr("cfg-lu-sd-url"),
            auto_add_lineup: isChecked("cfg-lu-sd-autoadd"),
          },
        },
        matching: {
          upgrade_hysteresis_score: reqNum("cfg-match-hysteresis"),
        },
        validation: {
          enabled: isChecked("cfg-val-enabled"),
          only_changed: isChecked("cfg-val-only-changed"),
          concurrency: reqNum("cfg-val-concurrency"),
          ignore_account_limit: isChecked("cfg-val-ignore-limit"),
          inter_probe_delay_sec: reqNum("cfg-val-delay"),
          http_timeout_sec: reqNum("cfg-val-http-timeout"),
          min_bytes_to_read: reqNum("cfg-val-min-bytes"),
          use_ffprobe: isChecked("cfg-val-ffprobe"),
          ffprobe_timeout_sec: reqNum("cfg-val-ffprobe-timeout"),
          max_candidates_per_channel: reqNum("cfg-val-max-cands"),
        },
        epg: {
          verify: isChecked("cfg-epg-verify"),
          refresh_hours: reqNum("cfg-epg-refresh"),
        },
        output: {
          m3u8_enabled: isChecked("cfg-out-m3u8-en"),
          xtream_enabled: isChecked("cfg-out-xt-en"),
          include_unmatched_comment: isChecked("cfg-out-unmatched"),
        },
      },
    };
  };

  const loadSettingsIntoDrawer = async (): Promise<void> => {
    setSettingsStatus("Loading configuration…");
    try {
      const resp = await fetch("/api/config");
      if (resp.status === 401) {
        redirectToLogin();
        return;
      }
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = (await resp.json()) as PublicConfigResponse;
      if (!data.available || !data.config) {
        setSettingsStatus("Configuration API unavailable in this mode.", "warn");
        return;
      }
      if (elSettingsFileBadge && data.config_file) {
        const parts = String(data.config_file).split("/");
        elSettingsFileBadge.textContent =
          parts[parts.length - 1] || data.config_file;
        elSettingsFileBadge.title = data.config_file;
      }
      renderSettingsForm(data.config);
      setSettingsStatus("Loaded from disk.");
    } catch (err: unknown) {
      setSettingsStatus(`Failed to load settings: ${errorMessage(err)}`, "error");
    }
  };

  const openSettingsDrawer = async (): Promise<void> => {
    if (!elSettingsOverlay) return;
    elSettingsOverlay.hidden = false;
    document.body.style.overflow = "hidden";
    await loadSettingsIntoDrawer();
  };

  const closeSettingsDrawer = (): void => {
    if (!elSettingsOverlay) return;
    elSettingsOverlay.hidden = true;
    if (elOverlay.hidden) {
      document.body.style.overflow = "";
    }
  };

  const checkSettingsAvailability = async (): Promise<void> => {
    if (!elSettingsBtn) return;
    try {
      const resp = await fetch("/api/config");
      if (!resp.ok) return;
      const data = (await resp.json()) as PublicConfigResponse;
      elSettingsBtn.hidden = !data.available;
    } catch {
      // Keep Settings button hidden if config API is unreachable.
    }
  };

  if (elSettingsBtn) {
    elSettingsBtn.addEventListener("click", () => {
      void openSettingsDrawer();
    });
  }
  if (elSettingsBackdrop) {
    elSettingsBackdrop.addEventListener("click", closeSettingsDrawer);
  }
  if (elSettingsCloseBtn) {
    elSettingsCloseBtn.addEventListener("click", closeSettingsDrawer);
  }
  if (elSettingsTabs) {
    elSettingsTabs.addEventListener("click", (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      const tabBtn = target
        ? target.closest<HTMLElement>("[data-settings-tab]")
        : null;
      const tabName = tabBtn?.dataset["settingsTab"];
      if (!tabName) return;
      activeSettingsTab = tabName;
      syncSettingsTabVisibility();
    });
  }
  if (elSettingsReloadBtn) {
    elSettingsReloadBtn.addEventListener("click", () => {
      void loadSettingsIntoDrawer();
    });
  }
  if (elSettingsForm) {
    elSettingsForm.addEventListener("submit", async (e: SubmitEvent) => {
      e.preventDefault();
      let payload: PublicConfig;
      try {
        payload = collectSettingsPayload();
      } catch (err: unknown) {
        setSettingsStatus(errorMessage(err), "error");
        return;
      }
      if (elSettingsSaveBtn) elSettingsSaveBtn.disabled = true;
      setSettingsStatus("Saving settings…");
      try {
        const resp = await fetch("/api/config", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (resp.status === 401) {
          redirectToLogin();
          return;
        }
        const data = (await resp.json().catch(() => ({}))) as ConfigSaveResponse;
        if (!resp.ok) {
          setSettingsStatus(
            data.error || `Save failed (HTTP ${resp.status})`,
            "error",
          );
          return;
        }
        if (data.config) renderSettingsForm(data.config);
        if (data.restart_required) {
          setSettingsStatus(
            "Saved to config.yaml — restart `bellhop serve` to apply Xtream server listener changes.",
            "warn",
          );
        } else {
          setSettingsStatus("Saved to config.yaml.", "ok");
        }
      } catch (err: unknown) {
        setSettingsStatus(
          `Network error saving settings: ${errorMessage(err)}`,
          "error",
        );
      } finally {
        if (elSettingsSaveBtn) elSettingsSaveBtn.disabled = false;
      }
    });
  }

  // Delegated capture-phase fallback for broken channel logo images (CSP-safe, no inline onerror).
  document.addEventListener(
    "error",
    (event: Event) => {
      const target = event.target;
      if (!(target instanceof HTMLImageElement)) return;
      const fallback = target.dataset["logoFallback"];
      if (fallback === undefined) return;
      const span = document.createElement("span");
      span.className = "logo-fallback";
      span.textContent = fallback || "TV";
      target.replaceWith(span);
    },
    true,
  );

  // Global keyboard shortcuts
  window.addEventListener("keydown", (e: KeyboardEvent) => {
    const target = e.target as HTMLElement | null;
    const tag = target?.tagName || "";
    const inInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

    if (elSettingsOverlay && !elSettingsOverlay.hidden) {
      if (e.key === "Escape") {
        e.preventDefault();
        closeSettingsDrawer();
      }
      return;
    }

    if (!elOverlay.hidden) {
      if (e.key === "Escape") {
        e.preventDefault();
        void closeOverlay();
      } else if ((e.key === "f" || e.key === "F") && !inInput) {
        e.preventDefault();
        void toggleFullscreen();
      } else if (e.key === "ArrowUp" && !inInput) {
        e.preventDefault();
        stepChannel(-1);
      } else if (e.key === "ArrowDown" && !inInput) {
        e.preventDefault();
        stepChannel(1);
      }
      return;
    }

    if (e.key === "/" && !inInput) {
      e.preventDefault();
      elSearchInput.focus();
    }
  });

  // Release tuner cleanly if browser tab closes (scoped to this page's client ID
  // so reload beacons never kill the new tab's stream).
  window.addEventListener("pagehide", () => {
    if (state.activeChannel) {
      navigator.sendBeacon(
        `/api/tuner/release?client=${encodeURIComponent(PAGE_CLIENT_ID)}`,
      );
    }
  });

  // Periodic clock & EPG progress bar refresh every 30s.
  window.setInterval(() => {
    updateClock();
    if (state.channels.length === 0) return;
    applyFiltersAndRender();
  }, 30000);

  // Periodically sync fresh EPG data from the backend every 10 minutes.
  window.setInterval(() => {
    void loadLineup();
  }, 600000);

  // Boot: bind theme toggle and hydrate UI state from URL query params.
  if (window.BellhopSettings && elThemeToggleBtn) {
    window.BellhopSettings.bindThemeToggle(elThemeToggleBtn);
  }
  state.pendingChannel = readStateFromUrl();
  syncControlsFromState();
  updateClock();
  void checkAuthStatus();
  void checkSettingsAvailability();
  void loadLineup();
})();
