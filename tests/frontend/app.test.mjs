/**
 * Integration-style Node unit tests for Bellhop's guide entry module (`app.js`).
 */
import assert from "node:assert/strict";
import test from "node:test";

import { FakeDocument, FakeElement, FakeImageElement } from "./_dom_stub.mjs";
import { initGuideApp } from "../../src/bellhop/web/static/app.js";

function flushMicrotasks() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function buildAppHarness({ search = "", fetchImpl } = {}) {
  const doc = new FakeDocument();
  const reg = (tag, id, attrs = {}) => {
    const el = doc.createElement(tag);
    el.id = id;
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "hidden" || k === "disabled") el[k] = v;
      else if (k === "textContent") el.textContent = v;
      else if (k === "className") el.className = v;
      else el.setAttribute(k, v);
    }
    doc.register(el);
    return el;
  };

  const guideMain = reg("main", "guide-main");
  const lineupTitle = reg("h1", "lineup-title");
  const lineupBadge = reg("span", "lineup-id-badge");
  const channelCounter = reg("span", "channel-counter");
  const liveClock = reg("span", "live-clock");
  const searchInput = reg("input", "search-input");
  const searchClear = reg("button", "search-clear", { hidden: true });
  const categoryList = reg("aside", "category-list");
  const cardsView = reg("div", "cards-view");
  const timelineView = reg("div", "timeline-view", { hidden: true });
  const timelineHeader = reg("div", "timeline-header");
  const timelineBody = reg("div", "timeline-body");
  const emptyState = reg("div", "empty-state", { hidden: true });
  const resetFiltersBtn = reg("button", "reset-filters-btn");
  const hideEmptyBtn = reg("button", "hide-empty-epg-btn");
  const refreshBtn = reg("button", "refresh-btn");
  const refreshSpan = doc.createElement("span");
  refreshSpan.textContent = "Refresh";
  refreshBtn.appendChild(refreshSpan);
  const logoutBtn = reg("button", "logout-btn", { hidden: true });
  const themeBtn = reg("button", "theme-toggle-btn");

  const viewCardsBtn = doc.createElement("button");
  viewCardsBtn.setAttribute("data-view", "cards");
  const viewGridBtn = doc.createElement("button");
  viewGridBtn.setAttribute("data-view", "grid");

  // Player DOM
  const overlay = reg("div", "player-overlay", { hidden: true });
  const playerBackdrop = reg("div", "player-backdrop");
  const playerStage = reg("div", "player-stage");
  const videoEl = reg("video", "video-el");
  const unmuteBtn = reg("button", "video-unmute-btn", { hidden: true });
  const clickPlayBox = reg("div", "video-click-play", { hidden: true });
  const clickPlayBtn = reg("button", "video-click-play-btn");
  const spinner = reg("div", "video-spinner", { hidden: true });
  const statusText = reg("span", "video-status-text");
  const errorBox = reg("div", "video-error", { hidden: true });
  const errorText = reg("span", "video-error-text");
  const playerLogoWrap = reg("div", "player-logo-wrap");
  const playerChno = reg("span", "player-chno");
  const playerName = reg("span", "player-name");
  const playerQuality = reg("span", "player-quality");
  const playerLivePill = reg("span", "player-live-pill");
  const playerCategory = reg("span", "player-category");
  const playerNowTitle = reg("span", "player-now-title");
  const playerNowTime = reg("span", "player-now-time");
  const playerProgressWrap = reg("div", "player-progress-wrap");
  const playerProgressBar = reg("div", "player-progress-bar");
  const playerNowDesc = reg("div", "player-now-desc");
  const playerUpcoming = reg("div", "player-upcoming-list");
  const closePlayerBtn = reg("button", "close-player-btn");
  const fullscreenBtn = reg("button", "fullscreen-btn");
  const prevChBtn = reg("button", "prev-ch-btn");
  const nextChBtn = reg("button", "next-ch-btn");
  const pipBtn = reg("button", "pip-btn");
  const retryStreamBtn = reg("button", "retry-stream-btn");
  const switchModeBtn = reg("button", "switch-mode-btn");
  const modeTsBtn = reg("button", "mode-ts-btn", { "data-mode": "mpegts" });
  const modeHlsBtn = reg("button", "mode-hls-btn", { "data-mode": "hls" });

  overlay.appendChild(playerBackdrop);
  overlay.appendChild(playerStage);
  playerStage.appendChild(videoEl);
  for (const child of [
    closePlayerBtn,
    fullscreenBtn,
    prevChBtn,
    nextChBtn,
    pipBtn,
    retryStreamBtn,
    switchModeBtn,
    modeTsBtn,
    modeHlsBtn,
    unmuteBtn,
    clickPlayBox,
    clickPlayBtn,
    spinner,
    statusText,
    errorBox,
    errorText,
    playerLogoWrap,
    playerChno,
    playerName,
    playerQuality,
    playerLivePill,
    playerCategory,
    playerNowTitle,
    playerNowTime,
    playerProgressWrap,
    playerProgressBar,
    playerNowDesc,
    playerUpcoming,
  ]) {
    overlay.appendChild(child);
  }

  // Settings DOM
  const settingsBtn = reg("button", "settings-btn", { hidden: true });
  const settingsOverlay = reg("div", "settings-overlay", { hidden: true });
  const settingsCloseBtn = reg("button", "close-settings-btn");
  settingsOverlay.appendChild(settingsCloseBtn);

  for (const el of [
    lineupTitle,
    lineupBadge,
    channelCounter,
    liveClock,
    searchInput,
    searchClear,
    categoryList,
    cardsView,
    timelineView,
    timelineHeader,
    timelineBody,
    emptyState,
    resetFiltersBtn,
    hideEmptyBtn,
    refreshBtn,
    logoutBtn,
    themeBtn,
    viewCardsBtn,
    viewGridBtn,
    settingsBtn,
  ]) {
    guideMain.appendChild(el);
  }
  timelineView.appendChild(timelineHeader);
  timelineView.appendChild(timelineBody);
  doc.body.appendChild(guideMain);
  doc.body.appendChild(overlay);
  doc.body.appendChild(settingsOverlay);

  const windowListeners = new Map();
  const intervals = new Map();
  let nextTimerId = 1;
  const beacons = [];
  const locationsReplaced = [];
  const historyCalls = [];
  let themeBound = null;

  const win = {
    location: {
      origin: "http://127.0.0.1:8080",
      pathname: "/",
      search,
      hash: "",
      replace(url) {
        locationsReplaced.push(url);
      },
    },
    history: {
      replaceState(_s, _t, url) {
        const [p, s = ""] = String(url).split("?");
        win.location.pathname = p || "/";
        win.location.search = s ? `?${s}` : "";
        historyCalls.push(["replace", url]);
      },
      pushState(_s, _t, url) {
        const [p, s = ""] = String(url).split("?");
        win.location.pathname = p || "/";
        win.location.search = s ? `?${s}` : "";
        historyCalls.push(["push", url]);
      },
    },
    BellhopUpdateCheckIntervalMs: 60000,
    BellhopSettings: {
      bindThemeToggle(btn) {
        themeBound = btn;
      },
    },
    getComputedStyle: () => ({
      getPropertyValue: () => "255px",
    }),
    setInterval(fn, ms) {
      const id = nextTimerId++;
      intervals.set(id, { fn, ms });
      return id;
    },
    clearInterval(id) {
      intervals.delete(id);
    },
    addEventListener(type, fn) {
      if (!windowListeners.has(type)) windowListeners.set(type, []);
      windowListeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      if (!windowListeners.has(type)) return;
      windowListeners.set(
        type,
        windowListeners.get(type).filter((f) => f !== fn),
      );
    },
    async dispatch(type, ev = {}) {
      const event = {
        preventDefault() {
          this.defaultPrevented = true;
        },
        defaultPrevented: false,
        ...ev,
      };
      for (const fn of [...(windowListeners.get(type) || [])]) {
        await fn(event);
      }
      return event;
    },
  };

  return {
    doc,
    win,
    intervals,
    beacons,
    locationsReplaced,
    historyCalls,
    getThemeBound: () => themeBound,
    els: {
      guideMain,
      lineupTitle,
      lineupBadge,
      channelCounter,
      liveClock,
      searchInput,
      searchClear,
      categoryList,
      cardsView,
      timelineView,
      timelineHeader,
      timelineBody,
      emptyState,
      resetFiltersBtn,
      hideEmptyBtn,
      refreshBtn,
      refreshSpan,
      logoutBtn,
      themeBtn,
      viewCardsBtn,
      viewGridBtn,
      overlay,
      modeTsBtn,
      modeHlsBtn,
      settingsOverlay,
      settingsCloseBtn,
    },
    async runWithGlobals(fn) {
      const prevDoc = globalThis.document;
      const prevWin = globalThis.window;
      const prevFetch = globalThis.fetch;
      const prevNav = globalThis.navigator;
      const prevHtmlEl = globalThis.HTMLElement;
      const prevImg = globalThis.HTMLImageElement;
      const prevMpegts = globalThis.mpegts;
      const prevHls = globalThis.Hls;
      globalThis.document = doc;
      globalThis.window = win;
      globalThis.fetch = fetchImpl;
      Object.defineProperty(globalThis, "navigator", {
        value: {
          sendBeacon(url) {
            beacons.push(url);
            return true;
          },
        },
        configurable: true,
      });
      globalThis.HTMLElement = FakeElement;
      globalThis.HTMLImageElement = FakeImageElement;
      globalThis.mpegts = { isSupported: () => false };
      globalThis.Hls = { isSupported: () => false };
      try {
        return await fn();
      } finally {
        globalThis.document = prevDoc;
        globalThis.window = prevWin;
        globalThis.fetch = prevFetch;
        if (prevNav !== undefined) {
          Object.defineProperty(globalThis, "navigator", {
            value: prevNav,
            configurable: true,
          });
        }
        globalThis.HTMLImageElement = prevImg;
        globalThis.HTMLElement = prevHtmlEl;
        globalThis.mpegts = prevMpegts;
        globalThis.Hls = prevHls;
      }
    },
  };
}

function sampleLineupPayload(nowSec, { refreshing = false, updatedAt = 100 } = {}) {
  return {
    lineup: {
      provider: "Bell Fibe",
      market: "Montreal",
      lineup_id: "CAN-BELLFIBE-MTL-X",
      timezone: "UTC",
      lineup_updated_at: updatedAt,
    },
    epg_status: {
      refreshing,
    },
    categories: [
      { category_id: "1", category_name: "News" },
      { category_id: "2", category_name: "Sports" },
    ],
    channels: [
      {
        chno: 1101,
        sd_chno: 101,
        name: "ICI RDI",
        callsign: "RDI",
        category_id: "1",
        category_name: "News",
        logo: "https://example.com/rdi.png",
        quality_tier: "hd",
        epg_id: "rdi.ca",
        stream_url: "/api/stream/1101.ts",
        hls_url: "/api/hls/1101/index.m3u8",
        now_playing: {
          title: "Le Journal",
          start_timestamp: nowSec - 600,
          stop_timestamp: nowSec + 1200,
          progress_pct: 33.3,
        },
        up_next: null,
        schedule: [
          {
            title: "Le Journal",
            start_timestamp: nowSec - 600,
            stop_timestamp: nowSec + 1200,
          },
        ],
      },
      {
        chno: 1108,
        sd_chno: 108,
        name: "RDS",
        callsign: "RDS",
        category_id: "2",
        category_name: "Sports",
        logo: "https://example.com/rds.png",
        quality_tier: "fhd",
        epg_id: "rds.ca",
        stream_url: "/api/stream/1108.ts",
        hls_url: "/api/hls/1108/index.m3u8",
        now_playing: {
          title: "Hockey",
          start_timestamp: nowSec - 600,
          stop_timestamp: nowSec + 1200,
          progress_pct: 33.3,
        },
        up_next: {
          title: "Sports 30",
          start_timestamp: nowSec + 1200,
          stop_timestamp: nowSec + 3000,
        },
        schedule: [
          {
            title: "Hockey",
            start_timestamp: nowSec - 600,
            stop_timestamp: nowSec + 1200,
          },
          {
            title: "Sports 30",
            start_timestamp: nowSec + 1200,
            stop_timestamp: nowSec + 3000,
          },
        ],
      },
      {
        chno: 1120,
        sd_chno: 120,
        name: "No Guide TV",
        callsign: "NGTV",
        category_id: "2",
        category_name: "Sports",
        logo: "",
        quality_tier: "sd",
        epg_id: "",
        stream_url: "/api/stream/1120.ts",
        hls_url: "/api/hls/1120/index.m3u8",
        now_playing: null,
        up_next: null,
        schedule: [],
      },
    ],
  };
}

test("app.js initializes state from URL, supports filtering, timeline nav, shortcuts, ticks, and logout", async () => {
  const nowSec = Math.floor(Date.now() / 1000);
  const fetchCalls = [];
  let epgPollCount = 0;

  const harness = buildAppHarness({
    search: "?cat=2&epg=1&q=rds&view=grid&offset=3600&mode=hls&ch=1108",
    fetchImpl: async (url, init = {}) => {
      fetchCalls.push(`${init.method || "GET"} ${url}`);
      if (url === "/api/auth/status") {
        return {
          ok: true,
          status: 200,
          json: async () => ({ auth_required: true, authenticated: true }),
        };
      }
      if (url === "/api/lineup") {
        return {
          ok: true,
          status: 200,
          json: async () => sampleLineupPayload(nowSec, { refreshing: true, updatedAt: 100 }),
        };
      }
      if (url === "/api/health") {
        epgPollCount += 1;
        return {
          ok: true,
          status: 200,
          json: async () => ({
            lineup_updated_at: epgPollCount >= 2 ? 200 : 100,
            epg_status: { refreshing: epgPollCount < 2 },
          }),
        };
      }
      if (url === "/api/epg/refresh") {
        return { ok: true, status: 200, json: async () => ({ ok: true }) };
      }
      if (url.startsWith("/api/auth/logout")) {
        return { ok: true, status: 200, json: async () => ({ ok: true }) };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    },
  });

  await harness.runWithGlobals(async () => {
    const appHandle = initGuideApp();
    try {
      await flushMicrotasks();
      await flushMicrotasks();

      assert.equal(harness.getThemeBound(), harness.els.themeBtn);
      assert.equal(harness.els.logoutBtn.hidden, false);
      assert.equal(harness.els.lineupTitle.textContent, "Bell Fibe — Montreal");
      assert.equal(harness.els.lineupBadge.textContent, "CAN-BELLFIBE-MTL-X");
      // Deep-link ch=1108 auto-opened the player overlay!
      assert.equal(harness.els.overlay.hidden, false);

      // Modal keyboard shortcuts while player overlay is open: Tab, f, ArrowDown, ArrowUp, Escape.
      await harness.win.dispatch("keydown", { key: "Tab", target: harness.els.overlay });
      await harness.win.dispatch("keydown", { key: "f", target: harness.els.overlay });
      await harness.win.dispatch("keydown", { key: "ArrowDown", target: harness.els.overlay });
      await harness.win.dispatch("keydown", { key: "ArrowUp", target: harness.els.overlay });
      const escEv = await harness.win.dispatch("keydown", { key: "Escape", target: harness.els.overlay });
      assert.equal(escEv.defaultPrevented, true);
      assert.equal(harness.els.overlay.hidden, true);

      // Trigger broken image fallback on document capture 'error' listener.
      const brokenImg = harness.doc.createElement("img");
      brokenImg.dataset.logoFallback = "RD";
      harness.els.cardsView.appendChild(brokenImg);
      await harness.doc.dispatch("error", { target: brokenImg }, true);
      assert.equal(harness.els.cardsView.children[0].className, "logo-fallback");
      assert.equal(harness.els.cardsView.children[0].textContent, "RD");

      // Settings overlay Escape & Tab traps when settingsOverlay is visible.
      harness.els.settingsOverlay.hidden = false;
      await harness.win.dispatch("keydown", { key: "Tab", target: harness.els.settingsOverlay });
      await harness.win.dispatch("keydown", { key: "Escape", target: harness.els.settingsOverlay });
      assert.equal(harness.els.settingsOverlay.hidden, true);

      // Timeline shift buttons (-3600, 0, +3600) & timeline keyboard navigation.
      harness.els.timelineView.scrollLeft = 90;
      const nowBtn = harness.els.timelineHeader.querySelector('[data-tl-shift="0"]');
      await harness.els.timelineHeader.dispatch("click", { target: nowBtn });
      assert.equal(harness.els.timelineView.scrollLeft, 0);

      const shiftPlus = harness.els.timelineHeader.querySelector('[data-tl-shift="3600"]');
      await harness.els.timelineHeader.dispatch("click", { target: shiftPlus });

      // Reset filters and switch to Cards view.
      await harness.els.resetFiltersBtn.dispatch("click");
      assert.equal(harness.els.searchInput.value, "");

      // Switch to grid and exercise ArrowRight/ArrowLeft/ArrowUp/ArrowDown/Enter on timeline rows.
      await harness.els.viewGridBtn.dispatch("click");
      const firstCell = harness.els.timelineBody.querySelector(".tl-ch-cell");
      firstCell.focus();
      await harness.els.timelineBody.dispatch("keydown", { key: "ArrowRight", target: firstCell });
      assert.notEqual(harness.doc.activeElement, firstCell);
      await harness.els.timelineBody.dispatch("keydown", { key: "ArrowLeft", target: harness.doc.activeElement });
      assert.equal(harness.doc.activeElement, firstCell);
      await harness.els.timelineBody.dispatch("keydown", { key: "ArrowDown", target: firstCell });
      await harness.els.timelineBody.dispatch("keydown", { key: "ArrowUp", target: harness.doc.activeElement });

      // Switch to cards, search, clear, toggle hideEmpty, click category, open via card Enter.
      await harness.els.viewCardsBtn.dispatch("click");
      await harness.win.dispatch("keydown", { key: "/", target: harness.doc.body });
      assert.equal(harness.doc.activeElement, harness.els.searchInput);

      harness.els.searchInput.value = "RDI";
      await harness.els.searchInput.dispatch("input");
      assert.equal(harness.els.searchClear.hidden, false);
      await harness.els.searchClear.dispatch("click");
      assert.equal(harness.els.searchClear.hidden, true);

      await harness.els.hideEmptyBtn.dispatch("click");
      assert.equal(harness.els.hideEmptyBtn.classList.contains("active"), true);

      const catBtn = harness.els.categoryList.querySelector('[data-cat="1"]');
      await harness.els.categoryList.dispatch("click", { target: catBtn });
      const cardEl = harness.els.cardsView.querySelector(".channel-card");
      await harness.els.cardsView.dispatch("keydown", { key: "Enter", target: cardEl });
      assert.equal(harness.els.overlay.hidden, false);

      // pagehide while player open fires tuner release beacon.
      await harness.win.dispatch("pagehide");
      assert.ok(harness.beacons.some((u) => u.startsWith("/api/tuner/release?client=")));

      // popstate without ?ch closes overlay, with ?ch re-opens it.
      harness.win.history.replaceState(null, "", "/?view=cards");
      await harness.win.dispatch("popstate");
      assert.equal(harness.els.overlay.hidden, true);
      harness.win.history.replaceState(null, "", "/?ch=1101");
      await harness.win.dispatch("popstate");
      assert.equal(harness.els.overlay.hidden, false);

      // Exercise registered intervals: 30s tick + EPG health poll + visibilitychange.
      for (const { fn } of [...harness.intervals.values()]) {
        await fn();
      }
      for (const { fn } of [...harness.intervals.values()]) {
        await fn();
      }
      await flushMicrotasks();
      assert.ok(fetchCalls.some((c) => c === "GET /api/health"));

      await harness.doc.dispatch("visibilitychange");
      await flushMicrotasks();

      // Refresh button triggers POST /api/epg/refresh + reload lineup.
      await harness.els.refreshBtn.dispatch("click");
      await flushMicrotasks();
      assert.ok(fetchCalls.includes("POST /api/epg/refresh"));

      // Logout triggers POST /api/auth/logout and redirects to /login.
      await harness.els.logoutBtn.dispatch("click");
      assert.ok(fetchCalls.some((c) => c.startsWith("POST /api/auth/logout")));
      assert.ok(harness.locationsReplaced.includes("/login"));
    } finally {
      appHandle.destroy();
    }
  });
});

test("app.js redirects to /login on 401 lineup responses and unauthenticated auth status", async () => {
  const harness = buildAppHarness({
    search: "?q=live",
    fetchImpl: async (url) => {
      if (url === "/api/auth/status") {
        return {
          ok: true,
          status: 200,
          json: async () => ({ auth_required: true, authenticated: false }),
        };
      }
      if (url === "/api/lineup") {
        return { ok: false, status: 401, json: async () => ({}) };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    },
  });

  await harness.runWithGlobals(async () => {
    const appHandle = initGuideApp();
    try {
      await flushMicrotasks();
      await flushMicrotasks();
      assert.ok(
        harness.locationsReplaced.some((u) => u.startsWith("/login?next=")),
      );
    } finally {
      appHandle.destroy();
    }
  });
});
