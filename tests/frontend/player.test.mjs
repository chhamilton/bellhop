/**
 * Unit tests for Bellhop's single-tuner live player controller (`player.js`).
 *
 * Exercises MPEG-TS absolute URL creation, automatic fallback to HLS when
 * `mpegts.js` is unsupported, autoplay-blocked -> muted retry -> unmute button,
 * fatal HLS error reporting, `stepChannel` wraparound, and `/api/tuner/release`
 * teardown beacons on `closeOverlay()`.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { createPlayerController } from "../../src/bellhop/web/static/player.js";

function makeEl(id = "") {
  const listeners = new Map();
  const classes = new Set();
  return {
    id,
    hidden: false,
    muted: false,
    paused: true,
    src: "",
    textContent: "",
    innerHTML: "",
    className: "",
    dataset: {},
    style: { width: "", overflow: "" },
    classList: {
      add(...items) {
        for (const it of items) classes.add(it);
      },
      remove(...items) {
        for (const it of items) classes.delete(it);
      },
      contains(it) {
        return classes.has(it);
      },
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    dispatch(type, ev = {}) {
      for (const fn of listeners.get(type) || []) {
        fn(ev);
      }
    },
    removeAttribute(attr) {
      if (attr === "src") this.src = "";
    },
    pause() {
      this.paused = true;
    },
    load() {},
    play() {
      this.paused = false;
      return Promise.resolve();
    },
    canPlayType() {
      return "";
    },
    focus() {},
  };
}

test("createPlayerController handles MPEG-TS -> HLS failover, autoplay mute retry, and tuner release", async () => {
  const buttons = new Map();
  for (const id of [
    "close-player-btn",
    "fullscreen-btn",
    "prev-ch-btn",
    "next-ch-btn",
    "pip-btn",
    "retry-stream-btn",
    "switch-mode-btn",
  ]) {
    buttons.set(id, makeEl(id));
  }

  const state = {
    channels: [
      {
        chno: 1101,
        sd_chno: 101,
        name: "ICI RDI",
        callsign: "RDI",
        category_id: "1",
        category_name: "News",
        logo: "",
        quality_tier: "HD",
        epg_id: "ICIRDI.ca",
        stream_url: "/api/stream/1101.ts",
        hls_url: "/api/hls/1101/index.m3u8",
        now_playing: null,
        up_next: null,
        schedule: [],
      },
      {
        chno: 1108,
        sd_chno: 108,
        name: "RDS",
        callsign: "RDS",
        category_id: "2",
        category_name: "Sports",
        logo: "",
        quality_tier: "FHD",
        epg_id: "RDS.ca",
        stream_url: "/api/stream/1108.ts",
        hls_url: "/api/hls/1108/index.m3u8",
        now_playing: null,
        up_next: null,
        schedule: [],
      },
    ],
    filteredChannels: [],
    activeChannel: null,
    streamMode: "mpegts",
    timezone: "UTC",
  };

  const elOverlay = makeEl("player-overlay");
  elOverlay.hidden = true;
  const elVideo = makeEl("video");
  const elUnmuteBtn = makeEl("unmute");
  elUnmuteBtn.hidden = true;
  const elClickPlayBox = makeEl("click-play-box");
  elClickPlayBox.hidden = true;
  const elSpinner = makeEl("spinner");
  const elStatusText = makeEl("status-text");
  const elErrorBox = makeEl("error-box");
  elErrorBox.hidden = true;
  const elErrorText = makeEl("error-text");
  const elPlayerLivePill = makeEl("live-pill");
  const elPlayerChno = makeEl("player-chno");
  const elPlayerName = makeEl("player-name");

  const createdMpegtsUrls = [];
  let mpegtsSupported = true;
  let lastHlsInstance = null;
  const fetchCalls = [];

  class FakeHls {
    static isSupported() {
      return true;
    }
    static Events = {
      MANIFEST_PARSED: "manifestParsed",
      ERROR: "hlsError",
    };
    constructor() {
      this.handlers = new Map();
      this.loadedUrl = "";
      this.destroyed = false;
      lastHlsInstance = this;
    }
    loadSource(url) {
      this.loadedUrl = url;
    }
    attachMedia() {}
    on(ev, cb) {
      this.handlers.set(ev, cb);
    }
    emit(ev, payload) {
      const cb = this.handlers.get(ev);
      if (cb) cb(ev, payload);
    }
    destroy() {
      this.destroyed = true;
    }
  }

  const origWindow = globalThis.window;
  const origDoc = globalThis.document;
  const origFetch = globalThis.fetch;

  globalThis.HTMLElement = class FakeHTMLElement {};
  globalThis.window = {
    location: { origin: "http://bellhop.local:8080" },
    mpegts: {
      Events: { ERROR: "error" },
      isSupported: () => mpegtsSupported,
      createPlayer(mediaDataSource) {
        createdMpegtsUrls.push(mediaDataSource.url);
        return {
          attachMediaElement() {},
          on() {},
          load() {},
          play: () => elVideo.play(),
          pause() {},
          unload() {},
          detachMediaElement() {},
          destroy() {},
        };
      },
    },
    Hls: FakeHls,
  };
  globalThis.document = {
    activeElement: null,
    fullscreenElement: null,
    pictureInPictureElement: null,
    body: { style: { overflow: "" } },
    querySelectorAll: () => [],
  };
  globalThis.fetch = async (url, init = {}) => {
    fetchCalls.push({ url, method: init.method || "GET" });
    return {
      ok: true,
      status: 200,
      json: async () => ({ programs: [] }),
    };
  };

  try {
    let modeSyncCount = 0;
    const ctrl = createPlayerController({
      pageClientId: "tab-abc",
      state,
      elOverlay,
      elBackdrop: makeEl("backdrop"),
      elPlayerStage: makeEl("stage"),
      elVideo,
      elUnmuteBtn,
      elClickPlayBox,
      elClickPlayBtn: makeEl("click-play-btn"),
      elSpinner,
      elStatusText,
      elErrorBox,
      elErrorText,
      elPlayerLogoWrap: makeEl("logo-wrap"),
      elPlayerChno,
      elPlayerName,
      elPlayerQuality: makeEl("quality"),
      elPlayerLivePill,
      elPlayerCategory: makeEl("cat"),
      elPlayerNowTitle: makeEl("now-title"),
      elPlayerNowTime: makeEl("now-time"),
      elPlayerProgressWrap: makeEl("prog-wrap"),
      elPlayerProgressBar: makeEl("prog-bar"),
      elPlayerNowDesc: makeEl("now-desc"),
      elPlayerUpcomingList: makeEl("upcoming"),
      elSettingsOverlay: null,
      requireEl: (id) => buttons.get(id),
      syncUrlFromState: () => {},
      syncModeButtons: () => {
        modeSyncCount += 1;
      },
    });

    // 1. MPEG-TS playback passes an absolute URL (required for blob: Web Workers)
    ctrl.openChannelOverlay(1101);
    assert.equal(elOverlay.hidden, false);
    assert.equal(elPlayerChno.textContent, "1101");
    assert.equal(createdMpegtsUrls.length, 1);
    assert.ok(
      createdMpegtsUrls[0].startsWith(
        "http://bellhop.local:8080/api/stream/1101.ts?",
      ),
    );
    assert.ok(createdMpegtsUrls[0].includes("client=tab-abc"));

    // 2. When mpegts.isSupported() is false, stepChannel(-1) wraps to 1108 and falls back to HLS!
    mpegtsSupported = false;
    let firstPlayAttempt = true;
    elVideo.play = () => {
      if (firstPlayAttempt) {
        firstPlayAttempt = false;
        const err = new Error("Autoplay blocked without user gesture");
        err.name = "NotAllowedError";
        return Promise.reject(err);
      }
      elVideo.paused = false;
      return Promise.resolve();
    };

    ctrl.stepChannel(-1);
    assert.equal(state.activeChannel.chno, 1108);
    assert.equal(state.streamMode, "hls");
    assert.ok(modeSyncCount >= 1);
    assert.ok(lastHlsInstance);
    assert.ok(lastHlsInstance.loadedUrl.startsWith("/api/hls/1108/index.m3u8?"));

    // 3. HLS MANIFEST_PARSED triggers autoplay -> NotAllowedError -> auto-mutes & shows unmute button
    lastHlsInstance.emit(FakeHls.Events.MANIFEST_PARSED);
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(elVideo.muted, true);
    assert.equal(elUnmuteBtn.hidden, false);

    // 4. Fatal HLS error surfaces user-facing mode-switch guidance
    lastHlsInstance.emit(FakeHls.Events.ERROR, {
      fatal: true,
      details: "manifestLoadError",
    });
    assert.equal(elErrorBox.hidden, false);
    assert.ok(elErrorText.textContent.includes("manifestLoadError"));

    // 5. Exercise overlay controls: unmute, click-play, retry, mode switch, PiP, fullscreen
    await elUnmuteBtn.dispatch("click");
    await elClickPlayBox.dispatch("click");
    mpegtsSupported = true;
    buttons.get("retry-stream-btn").dispatch("click");
    buttons.get("switch-mode-btn").dispatch("click");
    assert.equal(state.streamMode, "mpegts");
    await buttons.get("pip-btn").dispatch("click");
    globalThis.document.pictureInPictureElement = elVideo;
    globalThis.document.exitPictureInPicture = async () => {
      globalThis.document.pictureInPictureElement = null;
    };
    await buttons.get("pip-btn").dispatch("click");
    buttons.get("fullscreen-btn").dispatch("click");
    await ctrl.toggleFullscreen();

    // 6. closeOverlay() destroys active players and sends POST /api/tuner/release?client=tab-abc
    await ctrl.closeOverlay();
    assert.equal(elOverlay.hidden, true);
    assert.equal(lastHlsInstance.destroyed, true);
    assert.ok(
      fetchCalls.some(
        (c) =>
          c.method === "POST" &&
          c.url === "/api/tuner/release?client=tab-abc",
      ),
    );
  } finally {
    globalThis.window = origWindow;
    globalThis.document = origDoc;
    globalThis.fetch = origFetch;
  }
});

test("createPlayerController supports native HLS video playback and unknown channel no-op", () => {
  const buttons = new Map();
  for (const id of [
    "close-player-btn",
    "fullscreen-btn",
    "prev-ch-btn",
    "next-ch-btn",
    "pip-btn",
    "retry-stream-btn",
    "switch-mode-btn",
  ]) {
    buttons.set(id, makeEl(id));
  }
  const elOverlay = makeEl("player-overlay");
  elOverlay.hidden = true;
  const elVideo = makeEl("video");
  elVideo.canPlayType = (mime) =>
    mime === "application/vnd.apple.mpegurl" ? "maybe" : "";
  const origWindow = globalThis.window;
  const origDoc = globalThis.document;
  const origFetch = globalThis.fetch;
  globalThis.window = {
    location: { origin: "http://bellhop.local:8080" },
    mpegts: { isSupported: () => false },
    Hls: { isSupported: () => false },
  };
  globalThis.document = {
    activeElement: null,
    fullscreenElement: null,
    body: { style: { overflow: "" } },
    querySelectorAll: () => [],
  };
  globalThis.fetch = async () => ({
    ok: false,
    status: 500,
    json: async () => ({}),
  });
  try {
    const state = {
      channels: [
        {
          chno: 1108,
          sd_chno: null,
          name: "RDS",
          callsign: "",
          category_id: "2",
          category_name: "Sports",
          logo: "https://example.com/rds.png",
          quality_tier: "unknown",
          epg_id: "RDS.ca",
          stream_url: "/api/stream/1108.ts",
          hls_url: "/api/hls/1108/index.m3u8",
          now_playing: {
            title: "Hockey",
            start_timestamp: 1000,
            stop_timestamp: 2000,
            progress_pct: 50,
            description: "Period 2",
          },
          up_next: null,
          schedule: [
            { title: "Future Show", start_timestamp: 3000, stop_timestamp: 4000 },
          ],
        },
      ],
      filteredChannels: [],
      activeChannel: null,
      streamMode: "hls",
      timezone: "UTC",
    };
    const ctrl = createPlayerController({
      pageClientId: "tab-native",
      state,
      elOverlay,
      elBackdrop: makeEl("backdrop"),
      elPlayerStage: makeEl("stage"),
      elVideo,
      elUnmuteBtn: makeEl("unmute"),
      elClickPlayBox: makeEl("click-play"),
      elClickPlayBtn: makeEl("click-play-btn"),
      elSpinner: makeEl("spinner"),
      elStatusText: makeEl("status"),
      elErrorBox: makeEl("err-box"),
      elErrorText: makeEl("err-txt"),
      elPlayerLogoWrap: makeEl("logo"),
      elPlayerChno: makeEl("chno"),
      elPlayerName: makeEl("name"),
      elPlayerQuality: makeEl("qual"),
      elPlayerLivePill: makeEl("live"),
      elPlayerCategory: makeEl("cat"),
      elPlayerNowTitle: makeEl("now-title"),
      elPlayerNowTime: makeEl("now-time"),
      elPlayerProgressWrap: makeEl("prog-wrap"),
      elPlayerProgressBar: makeEl("prog-bar"),
      elPlayerNowDesc: makeEl("now-desc"),
      elPlayerUpcomingList: makeEl("upcoming"),
      elSettingsOverlay: null,
      requireEl: (id) => buttons.get(id),
      syncUrlFromState: () => {},
      syncModeButtons: () => {},
    });
    ctrl.openChannelOverlay(9999);
    assert.equal(elOverlay.hidden, true);
    ctrl.openChannelOverlay(1108);
    assert.equal(elOverlay.hidden, false);
    assert.ok(elVideo.src.startsWith("/api/hls/1108/index.m3u8?"));
    ctrl.destroyActivePlayers(false);
    assert.equal(elVideo.src, "");
  } finally {
    globalThis.window = origWindow;
    globalThis.document = origDoc;
    globalThis.fetch = origFetch;
  }
});
