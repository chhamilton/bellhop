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
