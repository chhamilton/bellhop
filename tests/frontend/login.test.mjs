/**
 * Unit tests for Bellhop's standalone sign-in controller (`login.js`).
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";

import { FakeDocument } from "./_dom_stub.mjs";

const LOGIN_JS = fs.readFileSync(
  path.resolve("src/bellhop/web/static/login.js"),
  "utf8",
);

function buildLoginDom({ nextUrl = "/guide", includeForm = true } = {}) {
  const doc = new FakeDocument();
  const themeBtn = doc.createElement("button");
  themeBtn.id = "theme-toggle-btn";
  doc.register(themeBtn);
  doc.body.appendChild(themeBtn);

  const els = { themeBtn };
  if (includeForm) {
    const form = doc.createElement("form");
    form.id = "login-form";
    const errEl = doc.createElement("div");
    errEl.id = "login-error";
    errEl.hidden = true;
    const btn = doc.createElement("button");
    btn.id = "login-submit";
    btn.textContent = "Sign In";
    const emailInput = doc.createElement("input");
    emailInput.id = "email";
    const passInput = doc.createElement("input");
    passInput.id = "password";
    const nextInput = doc.createElement("input");
    nextInput.name = "next";
    nextInput.value = nextUrl;

    for (const child of [errEl, emailInput, passInput, nextInput, btn]) {
      form.appendChild(child);
      doc.register(child);
    }
    doc.register(form);
    doc.body.appendChild(form);
    Object.assign(els, { form, errEl, btn, emailInput, passInput, nextInput });
  }
  return { doc, els };
}

test("login.js handles missing DOM, theme toggle binding, and successful redirect with hash", async () => {
  // Missing form exits cleanly without throwing.
  const emptySetup = buildLoginDom({ includeForm: false });
  let themeBound = false;
  vm.runInNewContext(LOGIN_JS, {
    document: emptySetup.doc,
    window: {
      location: { hash: "", replace() {} },
      BellhopSettings: {
        bindThemeToggle() {
          themeBound = true;
        },
      },
    },
    fetch: async () => ({ ok: true, json: async () => ({ ok: true }) }),
  });
  assert.equal(themeBound, true);

  // Full login flow preserves hash when nextUrl lacks '#'.
  const { doc, els } = buildLoginDom({ nextUrl: "/?view=grid" });
  const replaced = [];
  let postedBody = null;
  let themeTarget = null;
  vm.runInNewContext(LOGIN_JS, {
    document: doc,
    window: {
      location: {
        hash: "#now",
        replace(url) {
          replaced.push(url);
        },
      },
      BellhopSettings: {
        bindThemeToggle(btn) {
          themeTarget = btn;
        },
      },
    },
    fetch: async (url, init) => {
      assert.equal(url, "/api/auth/login");
      assert.equal(init.method, "POST");
      postedBody = JSON.parse(init.body);
      return {
        ok: true,
        status: 200,
        json: async () => ({ ok: true }),
      };
    },
  });

  assert.equal(themeTarget, els.themeBtn);
  els.emailInput.value = "admin@example.com";
  els.passInput.value = "s3cret";
  await els.form.dispatch("submit");
  assert.deepEqual(postedBody, {
    email: "admin@example.com",
    password: "s3cret",
  });
  assert.deepEqual(replaced, ["/?view=grid#now"]);
});

test("login.js surfaces HTTP error details, invalid JSON fallback, and network errors", async () => {
  // Server 429 JSON error
  {
    const { doc, els } = buildLoginDom({ nextUrl: "/home#sec" });
    const replaced = [];
    vm.runInNewContext(LOGIN_JS, {
      document: doc,
      window: {
        location: {
          hash: "#ignored",
          replace(url) {
            replaced.push(url);
          },
        },
      },
      fetch: async () => ({
        ok: false,
        status: 429,
        json: async () => ({ error: "Rate limited" }),
      }),
    });
    els.emailInput.value = "user@example.com";
    els.passInput.value = "bad";
    await els.form.dispatch("submit");
    assert.equal(els.errEl.hidden, false);
    assert.equal(els.errEl.textContent, "Rate limited");
    assert.equal(els.btn.disabled, false);
    assert.equal(els.btn.textContent, "Sign In");
    assert.deepEqual(replaced, []);
  }

  // Non-JSON error response uses default message
  {
    const { doc, els } = buildLoginDom({ nextUrl: "/" });
    vm.runInNewContext(LOGIN_JS, {
      document: doc,
      window: { location: { hash: "", replace() {} } },
      fetch: async () => ({
        ok: false,
        status: 502,
        json: async () => {
          throw new SyntaxError("bad json");
        },
      }),
    });
    await els.form.dispatch("submit");
    assert.equal(els.errEl.textContent, "Invalid email or password.");
    assert.equal(els.btn.disabled, false);
  }

  // Network exception shows server unreachable message and re-enables button
  {
    const { doc, els } = buildLoginDom({ nextUrl: "/" });
    vm.runInNewContext(LOGIN_JS, {
      document: doc,
      window: { location: { hash: "", replace() {} } },
      fetch: async () => {
        throw new Error("ECONNREFUSED");
      },
    });
    await els.form.dispatch("submit");
    assert.equal(els.errEl.hidden, false);
    assert.equal(
      els.errEl.textContent,
      "Unable to reach server. Please try again.",
    );
    assert.equal(els.btn.disabled, false);
    assert.equal(els.btn.textContent, "Sign In");
  }
});

function buildForgotDom() {
  const { doc, els } = buildLoginDom({ nextUrl: "/" });
  const subtitle = doc.createElement("p");
  subtitle.id = "login-subtitle";
  subtitle.textContent = "Sign in to access your live TV guide & player";
  const noticeEl = doc.createElement("div");
  noticeEl.id = "login-notice";
  noticeEl.hidden = true;
  const forgotLink = doc.createElement("button");
  forgotLink.id = "forgot-password-link";
  const forgotForm = doc.createElement("form");
  forgotForm.id = "forgot-password-form";
  forgotForm.hidden = true;
  const forgotEmail = doc.createElement("input");
  forgotEmail.id = "forgot-email";
  const forgotBtn = doc.createElement("button");
  forgotBtn.id = "forgot-submit";
  forgotBtn.textContent = "Send Reset Link";
  const backLink = doc.createElement("button");
  backLink.id = "back-to-login-link";

  for (const child of [forgotEmail, forgotBtn, backLink]) {
    forgotForm.appendChild(child);
    doc.register(child);
  }
  for (const el of [subtitle, noticeEl, forgotLink, forgotForm]) {
    doc.register(el);
    doc.body.appendChild(el);
  }
  return {
    doc,
    els: {
      ...els,
      subtitle,
      noticeEl,
      forgotLink,
      forgotForm,
      forgotEmail,
      forgotBtn,
      backLink,
    },
  };
}

function buildResetDom(initialToken = "tok-abc") {
  const doc = new FakeDocument();
  const errEl = doc.createElement("div");
  errEl.id = "login-error";
  errEl.hidden = true;
  const noticeEl = doc.createElement("div");
  noticeEl.id = "login-notice";
  noticeEl.hidden = true;
  const resetForm = doc.createElement("form");
  resetForm.id = "reset-password-form";
  const resetToken = doc.createElement("input");
  resetToken.id = "reset-token";
  resetToken.value = initialToken;
  const resetNew = doc.createElement("input");
  resetNew.id = "reset-new-password";
  const resetConfirm = doc.createElement("input");
  resetConfirm.id = "reset-confirm-password";
  const resetBtn = doc.createElement("button");
  resetBtn.id = "reset-submit";
  resetBtn.textContent = "Reset Password";

  for (const child of [resetToken, resetNew, resetConfirm, resetBtn]) {
    resetForm.appendChild(child);
    doc.register(child);
  }
  for (const el of [errEl, noticeEl, resetForm]) {
    doc.register(el);
    doc.body.appendChild(el);
  }
  return {
    doc,
    els: { errEl, noticeEl, resetForm, resetToken, resetNew, resetConfirm, resetBtn },
  };
}

test("login.js forgot-password toggle, submit notice, and error handling", async () => {
  const { doc, els } = buildForgotDom();
  let mode = "ok";
  let postedEmail = null;
  vm.runInNewContext(LOGIN_JS, {
    document: doc,
    window: { location: { hash: "", replace() {} } },
    fetch: async (url, init) => {
      assert.equal(url, "/api/auth/forgot-password");
      postedEmail = JSON.parse(init.body);
      if (mode === "http-err") {
        return { ok: false, status: 429, json: async () => ({ error: "Slow down" }) };
      }
      if (mode === "net-err") {
        throw new Error("offline");
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ ok: true, message: "Generic reset message." }),
      };
    },
  });

  // Prefills email from login box and switches back cleanly.
  els.emailInput.value = "alice@example.com";
  await els.forgotLink.dispatch("click");
  assert.equal(els.form.hidden, true);
  assert.equal(els.forgotForm.hidden, false);
  assert.equal(els.forgotEmail.value, "alice@example.com");
  assert.match(els.subtitle.textContent, /reset link/i);

  await els.backLink.dispatch("click");
  assert.equal(els.form.hidden, false);
  assert.equal(els.forgotForm.hidden, true);

  // HTTP error path
  await els.forgotLink.dispatch("click");
  mode = "http-err";
  await els.forgotForm.dispatch("submit");
  assert.equal(els.errEl.hidden, false);
  assert.equal(els.errEl.textContent, "Slow down");
  assert.equal(els.forgotBtn.disabled, false);

  // Network error path
  mode = "net-err";
  await els.forgotForm.dispatch("submit");
  assert.equal(els.errEl.hidden, false);
  assert.match(els.errEl.textContent, /Unable to reach server/);

  // Success path returns to login view and shows generic notice
  mode = "ok";
  els.forgotEmail.value = "  alice@example.com  ";
  await els.forgotForm.dispatch("submit");
  assert.deepEqual(postedEmail, { email: "alice@example.com" });
  assert.equal(els.form.hidden, false);
  assert.equal(els.forgotForm.hidden, true);
  assert.equal(els.noticeEl.hidden, false);
  assert.equal(els.noticeEl.textContent, "Generic reset message.");
  assert.equal(els.forgotBtn.disabled, false);
  assert.equal(els.forgotBtn.textContent, "Send Reset Link");
});

test("login.js reset-password URL scrub, validation, and auto-login redirect", async () => {
  // Scrubs `?token=` via URLSearchParams & fallback, validates passwords, redirects.
  {
    const { doc, els } = buildResetDom("tok-123");
    const historyCalls = [];
    const replaced = [];
    let fetchCalls = 0;
    let resetPayload = null;
    let failMode = "none";

    vm.runInNewContext(LOGIN_JS, {
      document: doc,
      URLSearchParams,
      window: {
        location: {
          pathname: "/reset-password",
          search: "?token=tok-123&next=%2F",
          hash: "#top",
          replace(url) {
            replaced.push(url);
          },
        },
        history: {
          replaceState(_st, _title, url) {
            historyCalls.push(url);
          },
        },
      },
      fetch: async (url, init) => {
        fetchCalls += 1;
        assert.equal(url, "/api/auth/reset-password");
        resetPayload = JSON.parse(init.body);
        if (failMode === "http-err") {
          return { ok: false, status: 400, json: async () => ({ error: "Expired" }) };
        }
        if (failMode === "net-err") {
          throw new Error("ECONNRESET");
        }
        return { ok: true, status: 200, json: async () => ({ ok: true }) };
      },
    });

    assert.deepEqual(historyCalls, ["/reset-password?next=%2F#top"]);

    // Client-side validation rejects short, oversized, mismatched, & empty token.
    els.resetNew.value = "   abc  ";
    els.resetConfirm.value = "   abc  ";
    await els.resetForm.dispatch("submit");
    assert.match(els.errEl.textContent, /at least 8 non-space/);

    els.resetNew.value = "a".repeat(257);
    els.resetConfirm.value = "a".repeat(257);
    await els.resetForm.dispatch("submit");
    assert.match(els.errEl.textContent, /256 characters/);

    els.resetNew.value = "😀".repeat(257); // > 1024 UTF-8 bytes via fallback loop
    els.resetConfirm.value = els.resetNew.value;
    await els.resetForm.dispatch("submit");
    assert.match(els.errEl.textContent, /256 characters/);

    els.resetNew.value = "valid_pass_123";
    els.resetConfirm.value = "different_pass_123";
    await els.resetForm.dispatch("submit");
    assert.match(els.errEl.textContent, /do not match/);

    els.resetToken.value = "";
    els.resetConfirm.value = "valid_pass_123";
    await els.resetForm.dispatch("submit");
    assert.match(els.errEl.textContent, /invalid or has expired/i);
    assert.equal(fetchCalls, 0);

    // Restore valid token and test HTTP error, network error, and success redirect.
    els.resetToken.value = "tok-123";
    failMode = "http-err";
    await els.resetForm.dispatch("submit");
    assert.equal(els.errEl.textContent, "Expired");
    assert.equal(els.resetBtn.disabled, false);

    failMode = "net-err";
    await els.resetForm.dispatch("submit");
    assert.match(els.errEl.textContent, /Unable to reach server/);
    assert.equal(els.resetBtn.disabled, false);

    failMode = "none";
    await els.resetForm.dispatch("submit");
    assert.deepEqual(resetPayload, {
      token: "tok-123",
      new_password: "valid_pass_123",
    });
    assert.deepEqual(replaced, ["/"]);
  }

  // Fallback path when neither URLSearchParams nor TextEncoder are provided.
  {
    const { doc } = buildResetDom("tok-fallback");
    const historyCalls = [];
    vm.runInNewContext(LOGIN_JS, {
      document: doc,
      window: {
        location: {
          pathname: "/reset-password",
          search: "?token=tok-fallback",
          hash: "",
          replace() {},
        },
        history: {
          replaceState(_st, _title, url) {
            historyCalls.push(url);
          },
        },
      },
      fetch: async () => ({ ok: true, json: async () => ({ ok: true }) }),
    });
    assert.deepEqual(historyCalls, ["/reset-password"]);
  }
});
