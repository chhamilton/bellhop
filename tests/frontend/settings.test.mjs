/**
 * Unit tests for Bellhop's two-tab Account & Player Setup drawer (`settings.js`).
 */
import assert from "node:assert/strict";
import test from "node:test";

import { FakeDocument } from "./_dom_stub.mjs";
import { createSettingsController } from "../../src/bellhop/web/static/settings.js";

test("createSettingsController manages tabs, stream creds, and 3-box password change flow", async () => {
  const doc = new FakeDocument();
  const elPlayerOverlay = doc.createElement("div");
  elPlayerOverlay.hidden = true;
  const elSettingsBtn = doc.createElement("button");
  elSettingsBtn.id = "settings-btn";
  elSettingsBtn.hidden = true;
  const elSettingsOverlay = doc.createElement("div");
  elSettingsOverlay.id = "settings-overlay";
  elSettingsOverlay.hidden = true;
  const elSettingsBackdrop = doc.createElement("div");
  elSettingsBackdrop.id = "settings-backdrop";
  const elSettingsCloseBtn = doc.createElement("button");
  elSettingsCloseBtn.id = "close-settings-btn";
  const elSettingsEmailBadge = doc.createElement("span");
  elSettingsEmailBadge.id = "settings-email-badge";
  const elSettingsTabs = doc.createElement("div");
  elSettingsTabs.id = "settings-tabs";
  elSettingsTabs.setAttribute("role", "tablist");
  elSettingsTabs.innerHTML = `
    <button
      id="settings-tab-stream"
      class="settings-tab active"
      type="button"
      role="tab"
      aria-selected="true"
      aria-controls="settings-pane-stream"
      data-settings-tab="stream"
    >Stream &amp; Player Setup</button>
    <button
      id="settings-tab-password"
      class="settings-tab"
      type="button"
      role="tab"
      aria-selected="false"
      aria-controls="settings-pane-password"
      tabindex="-1"
      data-settings-tab="password"
    >Change Password</button>
  `;
  const elSettingsBody = doc.createElement("div");
  elSettingsBody.id = "settings-body";
  const elSettingsStatus = doc.createElement("div");
  elSettingsStatus.id = "settings-status";
  const elSettingsReloadBtn = doc.createElement("button");
  elSettingsReloadBtn.id = "settings-reload-btn";

  for (const el of [
    elPlayerOverlay,
    elSettingsBtn,
    elSettingsOverlay,
  ]) {
    doc.body.appendChild(el);
  }
  elSettingsOverlay.appendChild(elSettingsBackdrop);
  elSettingsOverlay.appendChild(elSettingsCloseBtn);
  elSettingsOverlay.appendChild(elSettingsEmailBadge);
  elSettingsOverlay.appendChild(elSettingsTabs);
  elSettingsOverlay.appendChild(elSettingsBody);
  elSettingsOverlay.appendChild(elSettingsStatus);
  elSettingsOverlay.appendChild(elSettingsReloadBtn);

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
  globalThis.document = doc;
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
      elSettingsTabs,
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

    const tabStream = doc.getElementById("settings-tab-stream");
    const tabPassword = doc.getElementById("settings-tab-password");
    let streamPane = doc.getElementById("settings-pane-stream");
    let pwdPane = doc.getElementById("settings-pane-password");

    // Stream tab active initially; Password tabpane & reload button states match.
    assert.equal(tabStream.classList.contains("active"), true);
    assert.equal(tabStream.getAttribute("aria-selected"), "true");
    assert.equal(tabPassword.getAttribute("aria-selected"), "false");
    assert.equal(tabPassword.getAttribute("tabindex"), "-1");
    assert.equal(streamPane.hidden, false);
    assert.equal(pwdPane.hidden, true);
    assert.equal(elSettingsReloadBtn.hidden, false);

    // Readonly Xtream credential inputs opt out of password manager autofill.
    assert.equal(doc.getElementById("acct-xt-user").getAttribute("autocomplete"), "off");
    assert.equal(doc.getElementById("acct-xt-pass").getAttribute("autocomplete"), "off");

    // Password form isolates autofill username and renders 3 vertical password inputs.
    const pwdUsername = doc.getElementById("pwd-username");
    assert.equal(pwdUsername.value, "alice@example.com");
    assert.equal(pwdUsername.getAttribute("autocomplete"), "username");
    assert.equal(pwdUsername.getAttribute("tabindex"), "-1");

    const flush = () => new Promise((r) => setTimeout(r, 0));

    // Show / Hide Xtream password toggle
    const toggleBtn = doc.getElementById("toggle-xt-pass-btn");
    await elSettingsBody.dispatch("click", { target: toggleBtn });
    assert.equal(doc.getElementById("acct-xt-pass").type, "text");
    assert.equal(toggleBtn.textContent, "Hide");
    await elSettingsBody.dispatch("click", { target: toggleBtn });
    assert.equal(doc.getElementById("acct-xt-pass").type, "password");
    assert.equal(toggleBtn.textContent, "Show");

    // Copy button uses clipboard API cleanly
    const copyBtn = elSettingsBody.querySelector('[data-copy-target="player-m3u-ts"]');
    await elSettingsBody.dispatch("click", { target: copyBtn });
    await flush();
    assert.deepEqual(copiedTexts, [accountSample.player_urls.m3u_url]);
    assert.equal(elSettingsStatus.className, "settings-status is-ok");

    // Copy password input without clipboard API falls back to select() safely
    const origClip = globalThis.navigator.clipboard;
    globalThis.navigator.clipboard = undefined;
    const copyPassBtn = elSettingsBody.querySelector('[data-copy-target="acct-xt-pass"]');
    await elSettingsBody.dispatch("click", { target: copyPassBtn });
    await flush();
    assert.equal(doc.getElementById("acct-xt-pass").selected, true);
    assert.equal(doc.getElementById("acct-xt-pass").type, "password");
    globalThis.navigator.clipboard = origClip;

    // Cancelled regeneration does not hit POST
    confirmAnswer = false;
    await elSettingsBody.dispatch("click", {
      target: doc.getElementById("regen-stream-creds-btn"),
    });
    await flush();
    assert.equal(postedRegen, 0);

    // Confirmed regeneration calls POST and re-renders new credentials
    confirmAnswer = true;
    await elSettingsBody.dispatch("click", {
      target: doc.getElementById("regen-stream-creds-btn"),
    });
    await flush();
    assert.equal(postedRegen, 1);
    assert.equal(doc.getElementById("acct-xt-user").value, "zz99yy88");
    assert.equal(elSettingsStatus.className, "settings-status is-ok");

    // Click Password tab: clears transient status, toggles panes & hides Refresh btn.
    await elSettingsTabs.dispatch("click", { target: tabPassword });
    streamPane = doc.getElementById("settings-pane-stream");
    pwdPane = doc.getElementById("settings-pane-password");
    assert.equal(tabPassword.classList.contains("active"), true);
    assert.equal(tabPassword.getAttribute("aria-selected"), "true");
    assert.equal(tabPassword.getAttribute("tabindex"), "0");
    assert.equal(tabStream.getAttribute("aria-selected"), "false");
    assert.equal(tabStream.getAttribute("tabindex"), "-1");
    assert.equal(streamPane.hidden, true);
    assert.equal(pwdPane.hidden, false);
    assert.equal(elSettingsReloadBtn.hidden, true);
    assert.equal(elSettingsStatus.textContent, "");

    // ArrowLeft/Home/End/ArrowRight keyboard tab navigation + unrelated key no-op
    await elSettingsTabs.dispatch("keydown", { key: "Enter" });
    assert.equal(tabPassword.classList.contains("active"), true);
    await elSettingsTabs.dispatch("keydown", { key: "ArrowLeft" });
    assert.equal(tabStream.classList.contains("active"), true);
    assert.equal(doc.activeElement, tabStream);
    await elSettingsTabs.dispatch("keydown", { key: "End" });
    assert.equal(tabPassword.classList.contains("active"), true);
    assert.equal(doc.activeElement, tabPassword);
    await elSettingsTabs.dispatch("keydown", { key: "Home" });
    assert.equal(tabStream.classList.contains("active"), true);
    await elSettingsTabs.dispatch("keydown", { key: "ArrowRight" });
    assert.equal(tabPassword.classList.contains("active"), true);

    // Client-side password validation: empty, short, >256 chars, mismatch, unchanged
    const pwdForm = doc.getElementById("password-change-form");
    const pwdCurrent = doc.getElementById("pwd-current");
    const pwdNew = doc.getElementById("pwd-new");
    const pwdConfirm = doc.getElementById("pwd-confirm");

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

    pwdNew.value = "a".repeat(257);
    pwdConfirm.value = "a".repeat(257);
    await elSettingsBody.dispatch("submit", { target: pwdForm });
    await flush();
    assert.equal(postedPasswordBodies.length, 0);
    assert.match(elSettingsStatus.textContent, /256/);

    pwdNew.value = "newpassword1";
    pwdConfirm.value = "mismatch123";
    await elSettingsBody.dispatch("submit", { target: pwdForm });
    await flush();
    assert.equal(postedPasswordBodies.length, 0);
    assert.equal(elSettingsStatus.className, "settings-status is-error");

    pwdNew.value = "oldpassword1";
    pwdConfirm.value = "oldpassword1";
    await elSettingsBody.dispatch("submit", { target: pwdForm });
    await flush();
    assert.equal(postedPasswordBodies.length, 0);
    assert.match(elSettingsStatus.textContent, /different/);

    // Valid 3-box password update submits POST and clears inputs
    pwdNew.value = "newpassword1";
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

    // Server 403 & 429 surface API error message cleanly
    pwdCurrent.value = "wrongpass";
    pwdNew.value = "anotherpass1";
    pwdConfirm.value = "anotherpass1";
    nextPasswordStatus = 403;
    nextPasswordBody = { error: "Current password is incorrect." };
    await elSettingsBody.dispatch("submit", { target: pwdForm });
    await flush();
    assert.equal(elSettingsStatus.textContent, "Current password is incorrect.");
    assert.equal(elSettingsStatus.className, "settings-status is-error");

    nextPasswordStatus = 429;
    nextPasswordBody = {
      error: "Too many failed password attempts. Retry in 60s.",
    };
    await elSettingsBody.dispatch("submit", { target: pwdForm });
    await flush();
    assert.match(elSettingsStatus.textContent, /Too many failed/);

    // Session expiry (`401`) on password change triggers redirectToLogin()
    nextPasswordStatus = 401;
    await elSettingsBody.dispatch("submit", { target: pwdForm });
    await flush();
    assert.equal(loginRedirects, 1);

    // Clicking Stream tab restores stream pane and reload button; opening with
    // explicit "password" initialTab selects Change Password immediately.
    await elSettingsTabs.dispatch("click", { target: tabStream });
    assert.equal(elSettingsReloadBtn.hidden, false);
    await elSettingsReloadBtn.dispatch("click");
    await flush();
    await elSettingsBackdrop.dispatch("click");
    assert.equal(elSettingsOverlay.hidden, true);
    await ctrl.openSettingsDrawer("password");
    assert.equal(doc.getElementById("settings-pane-password").hidden, false);
    assert.equal(elSettingsReloadBtn.hidden, true);
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
  const doc = new FakeDocument();
  const elPlayerOverlay = doc.createElement("div");
  elPlayerOverlay.hidden = true;
  const elSettingsOverlay = doc.createElement("div");
  elSettingsOverlay.hidden = true;
  const elSettingsBody = doc.createElement("div");
  const elSettingsStatus = doc.createElement("div");
  elSettingsOverlay.appendChild(elSettingsBody);
  elSettingsOverlay.appendChild(elSettingsStatus);
  doc.body.appendChild(elPlayerOverlay);
  doc.body.appendChild(elSettingsOverlay);

  const origDoc = globalThis.document;
  const origFetch = globalThis.fetch;
  globalThis.document = doc;
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
      elSettingsBtn: doc.createElement("button"),
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
    assert.ok(doc.getElementById("player-xtream-disabled-note") !== null);
  } finally {
    globalThis.document = origDoc;
    globalThis.fetch = origFetch;
  }
});
