/**
 * Unit tests for Bellhop's Settings drawer controller (`settings.js`).
 *
 * Exercises `/api/config` availability checks, form population, tab switching,
 * `PUT /api/config` payload assembly (including `pinned_streams` &
 * `excluded_channels`), validation error reporting, `401` login redirects, and
 * `restart_required` warnings using lightweight DOM stubs.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { createSettingsController } from "../../src/bellhop/web/static/settings.js";

function makeFakeElement(tag = "div", id = "") {
  const listeners = new Map();
  const classes = new Set();
  const attrs = new Map();
  return {
    tagName: tag.toUpperCase(),
    id,
    hidden: false,
    disabled: false,
    checked: false,
    value: "",
    textContent: "",
    title: "",
    innerHTML: "",
    dataset: {},
    style: { overflow: "" },
    className: "",
    classList: {
      toggle(cls, force) {
        if (force === undefined) {
          if (classes.has(cls)) classes.delete(cls);
          else classes.add(cls);
        } else if (force) {
          classes.add(cls);
        } else {
          classes.delete(cls);
        }
      },
      contains(cls) {
        return classes.has(cls);
      },
    },
    setAttribute(k, v) {
      attrs.set(k, String(v));
    },
    getAttribute(k) {
      return attrs.get(k) ?? null;
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    async dispatch(type, ev = {}) {
      const fns = listeners.get(type) || [];
      for (const fn of fns) {
        await fn({ preventDefault() {}, ...ev });
      }
    },
    focus() {},
    querySelectorAll() {
      return [];
    },
  };
}

test("createSettingsController loads config, switches tabs, and submits PUT /api/config", async () => {
  const elementsById = new Map();
  const registerInput = (id, { value = "", checked = false } = {}) => {
    const el = makeFakeElement("input", id);
    el.value = value;
    el.checked = checked;
    elementsById.set(id, el);
    return el;
  };

  const tabUpstream = makeFakeElement("button");
  tabUpstream.dataset.settingsTab = "upstream_xtream";
  const tabLineup = makeFakeElement("button");
  tabLineup.dataset.settingsTab = "lineup";
  tabLineup.closest = () => tabLineup;

  const paneUpstream = makeFakeElement("div");
  paneUpstream.dataset.settingsPane = "upstream_xtream";
  const paneLineup = makeFakeElement("div");
  paneLineup.dataset.settingsPane = "lineup";

  const elPlayerOverlay = makeFakeElement("div");
  elPlayerOverlay.hidden = true;
  const elSettingsBtn = makeFakeElement("button");
  elSettingsBtn.hidden = true;
  const elSettingsOverlay = makeFakeElement("div");
  elSettingsOverlay.hidden = true;
  const elSettingsBackdrop = makeFakeElement("div");
  const elSettingsCloseBtn = makeFakeElement("button");
  const elSettingsFileBadge = makeFakeElement("span");
  const elSettingsTabs = makeFakeElement("div");
  elSettingsTabs.querySelectorAll = () => [tabUpstream, tabLineup];
  const elSettingsForm = makeFakeElement("form");
  const elSettingsBody = makeFakeElement("div");
  elSettingsBody.querySelectorAll = () => [paneUpstream, paneLineup];
  const elSettingsStatus = makeFakeElement("div");
  const elSettingsReloadBtn = makeFakeElement("button");
  const elSettingsSaveBtn = makeFakeElement("button");

  const sampleConfig = {
    upstream_xtream: {
      server_url: "http://provider.example",
      stream_format: "ts",
      request_timeout_sec: 15,
      refresh_hours: 24,
    },
    pipeline: {
      lineup: {
        backend: "schedules_direct",
        lineup_id: "CAN-0000001-X",
        postal_code: "H3B",
        refresh_days: 30,
        numbering_mode: "both",
        collapse_sd_dupes: true,
        exclude_audio: false,
        pinned_streams: { "1108": 410108 },
        excluded_channels: [1424],
        iptv_org: {
          enabled: true,
          recheck_hours: 24,
        },
        schedules_direct: {
          auto_add_lineup: true,
        },
      },
      matching: { upgrade_hysteresis_score: 5.0 },
      validation: {
        enabled: true,
        only_changed: true,
        concurrency: 1,
        ignore_account_limit: false,
        inter_probe_delay_sec: 0.35,
        http_timeout_sec: 8,
        min_bytes_to_read: 8192,
        use_ffprobe: true,
        ffprobe_timeout_sec: 12,
        max_candidates_per_channel: 5,
        detect_black_screen: true,
        black_sample_sec: 1.5,
        check_audio_language: true,
      },
      epg: { verify: true, refresh_hours: 6 },
      output: {
        m3u8_enabled: true,
        xtream_enabled: true,
        include_unmatched_comment: false,
      },
    },
  };

  // Populate simulated form inputs read by collectSettingsPayload()
  registerInput("cfg-xt-server", { value: " http://provider.example " });
  registerInput("cfg-xt-format", { value: "ts" });
  registerInput("cfg-xt-timeout", { value: "15" });
  registerInput("cfg-xt-refresh", { value: "24" });
  registerInput("cfg-lu-backend", { value: "schedules_direct" });
  registerInput("cfg-lu-id", { value: "CAN-0000001-X" });
  registerInput("cfg-lu-postal", { value: "H3B" });
  registerInput("cfg-lu-refresh", { value: "30" });
  registerInput("cfg-lu-numbering", { value: "both" });
  registerInput("cfg-lu-collapse", { checked: true });
  registerInput("cfg-lu-excl-audio", { checked: false });
  registerInput("cfg-lu-pins", { value: "1108 = 410108\n1205:520105" });
  registerInput("cfg-lu-excl", { value: "1424, 1425" });
  registerInput("cfg-lu-iptv-en", { checked: false });
  registerInput("cfg-lu-iptv-recheck", { value: "48" });
  registerInput("cfg-lu-sd-autoadd", { checked: true });
  registerInput("cfg-match-hysteresis", { value: "5.5" });
  registerInput("cfg-val-enabled", { checked: true });
  registerInput("cfg-val-only-changed", { checked: true });
  registerInput("cfg-val-concurrency", { value: "1" });
  registerInput("cfg-val-ignore-limit", { checked: false });
  registerInput("cfg-val-delay", { value: "0.35" });
  registerInput("cfg-val-http-timeout", { value: "8" });
  registerInput("cfg-val-min-bytes", { value: "8192" });
  registerInput("cfg-val-ffprobe", { checked: true });
  registerInput("cfg-val-ffprobe-timeout", { value: "12" });
  registerInput("cfg-val-max-cands", { value: "5" });
  registerInput("cfg-val-black", { checked: false });
  registerInput("cfg-val-black-sec", { value: "2.5" });
  registerInput("cfg-val-audio-lang", { checked: false });
  registerInput("cfg-epg-verify", { checked: true });
  registerInput("cfg-epg-refresh", { value: "6" });
  registerInput("cfg-out-m3u8-en", { checked: true });
  registerInput("cfg-out-xt-en", { checked: true });
  registerInput("cfg-out-unmatched", { checked: false });

  let loginRedirects = 0;
  let putRequests = [];
  let nextPutStatus = 200;
  let nextPutBody = {
    saved: true,
    restart_required: true,
    config_file: "config.yaml",
    config: sampleConfig,
  };

  const origDoc = globalThis.document;
  const origFetch = globalThis.fetch;
  globalThis.HTMLElement = class FakeHTMLElement {};
  globalThis.document = {
    activeElement: null,
    body: { style: { overflow: "" } },
    getElementById: (id) => elementsById.get(id) ?? null,
  };
  globalThis.fetch = async (url, init = {}) => {
    const method = init.method || "GET";
    if (url === "/api/config" && method === "GET") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          available: true,
          config_file: "/config/config.yaml",
          config: sampleConfig,
        }),
      };
    }
    if (url === "/api/config" && method === "PUT") {
      putRequests.push(JSON.parse(init.body));
      return {
        ok: nextPutStatus >= 200 && nextPutStatus < 300,
        status: nextPutStatus,
        json: async () => nextPutBody,
      };
    }
    throw new Error(`Unexpected fetch: ${method} ${url}`);
  };

  try {
    const ctrl = createSettingsController({
      elPlayerOverlay,
      elSettingsBtn,
      elSettingsOverlay,
      elSettingsBackdrop,
      elSettingsCloseBtn,
      elSettingsFileBadge,
      elSettingsTabs,
      elSettingsForm,
      elSettingsBody,
      elSettingsStatus,
      elSettingsReloadBtn,
      elSettingsSaveBtn,
      redirectToLogin: () => {
        loginRedirects += 1;
      },
    });

    await ctrl.checkSettingsAvailability();
    assert.equal(elSettingsBtn.hidden, false);

    await ctrl.openSettingsDrawer();
    assert.equal(elSettingsOverlay.hidden, false);
    assert.equal(elSettingsFileBadge.textContent, "config.yaml");
    assert.ok(elSettingsBody.innerHTML.includes("CAN-0000001-X"));
    assert.equal(elSettingsStatus.textContent, "Loaded from disk.");

    // Switch active tab to "lineup"
    await elSettingsTabs.dispatch("click", { target: tabLineup });
    assert.equal(paneUpstream.hidden, true);
    assert.equal(paneLineup.hidden, false);

    // Valid PUT submission serializes typed payload and reports restart_required
    await elSettingsForm.dispatch("submit");
    assert.equal(putRequests.length, 1);
    assert.equal(
      putRequests[0].upstream_xtream.server_url,
      "http://provider.example",
    );
    assert.deepEqual(putRequests[0].pipeline.lineup.pinned_streams, {
      "1108": 410108,
      "1205": 520105,
    });
    assert.deepEqual(putRequests[0].pipeline.lineup.excluded_channels, [
      1424,
      1425,
    ]);
    assert.equal(
      putRequests[0].pipeline.matching.upgrade_hysteresis_score,
      5.5,
    );
    assert.deepEqual(putRequests[0].pipeline.lineup.iptv_org, {
      enabled: false,
      recheck_hours: 48,
    });
    assert.equal(putRequests[0].pipeline.validation.detect_black_screen, false);
    assert.equal(putRequests[0].pipeline.validation.black_sample_sec, 2.5);
    assert.equal(putRequests[0].pipeline.validation.check_audio_language, false);
    assert.equal(elSettingsStatus.className, "settings-status is-warn");

    // Malformed pinned stream syntax aborts client-side before sending PUT
    elementsById.get("cfg-lu-pins").value = "bad_pin_entry";
    await elSettingsForm.dispatch("submit");
    assert.equal(putRequests.length, 1);
    assert.equal(elSettingsStatus.className, "settings-status is-error");

    // Session expiry (`401`) on PUT triggers redirectToLogin()
    elementsById.get("cfg-lu-pins").value = "1108=410108";
    nextPutStatus = 401;
    await elSettingsForm.dispatch("submit");
    assert.equal(loginRedirects, 1);

    ctrl.closeSettingsDrawer();
    assert.equal(elSettingsOverlay.hidden, true);
  } finally {
    globalThis.document = origDoc;
    globalThis.fetch = origFetch;
  }
});
