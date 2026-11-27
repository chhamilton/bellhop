/**
 * Unit tests for Bellhop's Account & Player Setup drawer (`settings.js`).
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
    type: "text",
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
    selected: false,
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
    select() {
      this.selected = true;
    },
    querySelectorAll() {
      return [];
    },
  };
}

test("createSettingsController loads /api/account, toggles password, copies URLs, regenerates creds, and updates web password", async () => {
  const elementsById = new Map();
  const regInput = (id, value = "", type = "text") => {
    const el = makeFakeElement("input", id);
    el.value = value;
    el.type = type;
    elementsById.set(id, el);
    return el;
  };

  const elPlayerOverlay = makeFakeElement("div");
  elPlayerOverlay.hidden = true;
  const elSettingsBtn = makeFakeElement("button");
  elSettingsBtn.hidden = true;
  const elSettingsOverlay = makeFakeElement("div");
  elSettingsOverlay.hidden = true;
  const elSettingsBackdrop = makeFakeElement("div");
  const elSettingsCloseBtn = makeFakeElement("button");
  const elSettingsEmailBadge = makeFakeElement("span");
  const elSettingsBody = makeFakeElement("div");
  const elSettingsStatus = makeFakeElement("div");
  const elSettingsReloadBtn = makeFakeElement("button");

  const accountSample = {
    account_id: "alice",
    email: "alice@example.com",
    xtream_username: "ab12cd34",
    xtream_password: "ef56gh78",
    player_urls: {
      xtream_server_url: "http://127.0.0.1:8080",
      m3u_url: "http://127.0.0.1:8080/get.php?username=ab12cd34&password=ef56gh78&type=m3u_plus&output=ts",
      m3u_hls_url: "http://127.0.0.1:8080/get.php?username=ab12cd34&password=ef56gh78&type=m3u_plus&output=hls",
      xmltv_url: "http://127.0.0.1:8080/xmltv.php?username=ab12cd34&password=ef56gh78",
    },
  };

  const xtPassInput = regInput("acct-xt-pass", accountSample.xtream_password, "password");
  const m3uInput = regInput("player-m3u-ts", accountSample.player_urls.m3u_url);
  const pwdCurrent = regInput("pwd-current", "", "password");
  const pwdNew = regInput("pwd-new", "", "password");
  const pwdConfirm = regInput("pwd-confirm", "", "password");
  const pwdSaveBtn = makeFakeElement("button", "pwd-save-btn");

  const pwdForm = makeFakeElement("form", "password-change-form");
  pwdForm.querySelector = (sel) => {
    if (sel === "#pwd-current") return pwdCurrent;
    if (sel === "#pwd-new") return pwdNew;
    if (sel === "#pwd-confirm") return pwdConfirm;
    if (sel === "#pwd-save-btn") return pwdSaveBtn;
    return null;
  };

  let confirmAnswer = true;
  let loginRedirects = 0;
  let copiedTexts = [];
  let postedRegen = 0;
  let postedPasswordBodies = [];
  let nextPasswordStatus = 200;
  let nextPasswordBody = { ok: true, email: "alice@example.com" };

  const origDoc = globalThis.document;
  const origWin = globalThis.window;
  const origNav = globalThis.navigator;
  const origFetch = globalThis.fetch;
  globalThis.HTMLElement = class FakeHTMLElement {};
  globalThis.document = {
    activeElement: null,
    body: { style: { overflow: "" } },
    getElementById: (id) => elementsById.get(id) ?? null,
  };
  globalThis.window = {
    setTimeout(fn) {
      fn();
      return 1;
    },
    confirm() {
      return confirmAnswer;
    },
  };
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      clipboard: {
        async writeText(text) {
          copiedTexts.push(text);
        },
      },
    },
  });
  globalThis.fetch = async (url, init = {}) => {
    const method = init.method || "GET";
    if (url === "/api/account" && method === "GET") {
      return { ok: true, status: 200, json: async () => accountSample };
    }
    if (
      url === "/api/account/stream-credentials/regenerate" &&
      method === "POST"
    ) {
      postedRegen += 1;
      return {
        ok: true,
        status: 200,
        json: async () => ({
          ...accountSample,
          xtream_username: "zz99yy88",
          xtream_password: "ww77vv66",
          player_urls: {
            ...accountSample.player_urls,
            m3u_url: "http://127.0.0.1:8080/get.php?username=zz99yy88&password=ww77vv66&type=m3u_plus&output=ts",
          },
        }),
      };
    }
    if (url === "/api/account/password" && method === "POST") {
      postedPasswordBodies.push(JSON.parse(init.body));
      return {
        ok: nextPasswordStatus >= 200 && nextPasswordStatus < 300,
        status: nextPasswordStatus,
        json: async () => nextPasswordBody,
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
      elSettingsEmailBadge,
      elSettingsBody,
      elSettingsStatus,
      elSettingsReloadBtn,
      redirectToLogin: () => {
        loginRedirects += 1;
      },
      confirmRegenerate: () => confirmAnswer,
    });

    await ctrl.checkSettingsAvailability();
    assert.equal(elSettingsBtn.hidden, false);
    assert.equal(elSettingsEmailBadge.textContent, "alice@example.com");

    await ctrl.openSettingsDrawer();
    assert.equal(elSettingsOverlay.hidden, false);
    assert.ok(elSettingsBody.innerHTML.includes("ab12cd34"));
    assert.ok(elSettingsBody.innerHTML.includes("External Player Setup"));

    const flush = () => new Promise((r) => setTimeout(r, 0));

    // Show / Hide Xtream password toggle
    const toggleBtn = makeFakeElement("button", "toggle-xt-pass-btn");
    toggleBtn.closest = () => toggleBtn;
    await elSettingsBody.dispatch("click", { target: toggleBtn });
    assert.equal(xtPassInput.type, "text");
    assert.equal(toggleBtn.textContent, "Hide");
    await elSettingsBody.dispatch("click", { target: toggleBtn });
    assert.equal(xtPassInput.type, "password");
    assert.equal(toggleBtn.textContent, "Show");

    // Copy button uses clipboard API cleanly
    const copyBtn = makeFakeElement("button");
    copyBtn.textContent = "Copy";
    copyBtn.dataset.copyTarget = "player-m3u-ts";
    copyBtn.closest = () => copyBtn;
    await elSettingsBody.dispatch("click", { target: copyBtn });
    await flush();
    assert.deepEqual(copiedTexts, [m3uInput.value]);
    assert.equal(elSettingsStatus.className, "settings-status is-ok");

    // Copy password input without clipboard API falls back to select() safely
    const origClip = globalThis.navigator.clipboard;
    globalThis.navigator.clipboard = undefined;
    const copyPassBtn = makeFakeElement("button");
    copyPassBtn.textContent = "Copy";
    copyPassBtn.dataset.copyTarget = "acct-xt-pass";
    copyPassBtn.closest = () => copyPassBtn;
    await elSettingsBody.dispatch("click", { target: copyPassBtn });
    await flush();
    assert.equal(xtPassInput.selected, true);
    assert.equal(xtPassInput.type, "password");
    globalThis.navigator.clipboard = origClip;

    // Cancelled regeneration does not hit POST
    confirmAnswer = false;
    const regenBtn = makeFakeElement("button", "regen-stream-creds-btn");
    regenBtn.closest = () => regenBtn;
    await elSettingsBody.dispatch("click", { target: regenBtn });
    await flush();
    assert.equal(postedRegen, 0);

    // Confirmed regeneration calls POST and re-renders new credentials
    confirmAnswer = true;
    await elSettingsBody.dispatch("click", { target: regenBtn });
    await flush();
    assert.equal(postedRegen, 1);
    assert.ok(elSettingsBody.innerHTML.includes("zz99yy88"));
    assert.equal(elSettingsStatus.className, "settings-status is-ok");

    // Client-side password validation (empty / short / mismatch) aborts before fetch
    pwdCurrent.value = "";
    pwdNew.value = "";
    pwdConfirm.value = "";
    await elSettingsBody.dispatch("submit", { target: pwdForm });
    await flush();
    assert.equal(postedPasswordBodies.length, 0);
    assert.equal(elSettingsStatus.className, "settings-status is-error");

    pwdCurrent.value = "oldpassword1";
    pwdNew.value = "short";
    pwdConfirm.value = "short";
    await elSettingsBody.dispatch("submit", { target: pwdForm });
    await flush();
    assert.equal(postedPasswordBodies.length, 0);
    assert.equal(elSettingsStatus.className, "settings-status is-error");

    pwdNew.value = "newpassword1";
    pwdConfirm.value = "mismatch123";
    await elSettingsBody.dispatch("submit", { target: pwdForm });
    await flush();
    assert.equal(postedPasswordBodies.length, 0);
    assert.equal(elSettingsStatus.className, "settings-status is-error");

    // Valid password update submits POST and clears inputs
    pwdConfirm.value = "newpassword1";
    await elSettingsBody.dispatch("submit", { target: pwdForm });
    await flush();
    assert.equal(postedPasswordBodies.length, 1);
    assert.deepEqual(postedPasswordBodies[0], {
      current_password: "oldpassword1",
      new_password: "newpassword1",
    });
    assert.equal(pwdCurrent.value, "");
    assert.equal(pwdNew.value, "");
    assert.equal(pwdConfirm.value, "");
    assert.equal(elSettingsStatus.className, "settings-status is-ok");

    // Server 403 on wrong password surfaces API error message
    pwdCurrent.value = "wrongpass";
    pwdNew.value = "anotherpass1";
    pwdConfirm.value = "anotherpass1";
    nextPasswordStatus = 403;
    nextPasswordBody = { error: "Current password is incorrect." };
    await elSettingsBody.dispatch("submit", { target: pwdForm });
    await flush();
    assert.equal(elSettingsStatus.textContent, "Current password is incorrect.");
    assert.equal(elSettingsStatus.className, "settings-status is-error");

    // Session expiry (`401`) on password change triggers redirectToLogin()
    pwdCurrent.value = "newpassword1";
    pwdNew.value = "anotherpass1";
    pwdConfirm.value = "anotherpass1";
    nextPasswordStatus = 401;
    await elSettingsBody.dispatch("submit", { target: pwdForm });
    await flush();
    assert.equal(loginRedirects, 1);

    // Reload button & close/backdrop actions work cleanly
    await elSettingsReloadBtn.dispatch("click");
    await flush();
    await elSettingsBackdrop.dispatch("click");
    assert.equal(elSettingsOverlay.hidden, true);
    await ctrl.openSettingsDrawer();
    await elSettingsCloseBtn.dispatch("click");
    assert.equal(elSettingsOverlay.hidden, true);
  } finally {
    globalThis.document = origDoc;
    globalThis.window = origWin;
    if (origNav !== undefined) {
      Object.defineProperty(globalThis, "navigator", {
        configurable: true,
        value: origNav,
      });
    }
    globalThis.fetch = origFetch;
  }
});

test("createSettingsController renders disabled notice when xtream_server_url is empty", async () => {
  const elPlayerOverlay = makeFakeElement("div");
  elPlayerOverlay.hidden = true;
  const elSettingsOverlay = makeFakeElement("div");
  elSettingsOverlay.hidden = true;
  const elSettingsBody = makeFakeElement("div");
  const elSettingsStatus = makeFakeElement("div");

  const origDoc = globalThis.document;
  const origFetch = globalThis.fetch;
  globalThis.HTMLElement = class FakeHTMLElement {};
  globalThis.document = {
    activeElement: null,
    body: { style: { overflow: "" } },
    getElementById: () => null,
  };
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      account_id: "bob",
      email: "bob@example.com",
      xtream_username: "aa11bb22",
      xtream_password: "cc33dd44",
      player_urls: {
        xtream_server_url: "",
        m3u_url: "",
        m3u_hls_url: "",
        xmltv_url: "",
      },
    }),
  });

  try {
    const ctrl = createSettingsController({
      elPlayerOverlay,
      elSettingsBtn: makeFakeElement("button"),
      elSettingsOverlay,
      elSettingsBackdrop: null,
      elSettingsCloseBtn: null,
      elSettingsEmailBadge: null,
      elSettingsBody,
      elSettingsStatus,
      elSettingsReloadBtn: null,
      redirectToLogin: () => {},
    });
    await ctrl.openSettingsDrawer();
    assert.ok(elSettingsBody.innerHTML.includes("player-xtream-disabled-note"));
  } finally {
    globalThis.document = origDoc;
    globalThis.fetch = origFetch;
  }
});
