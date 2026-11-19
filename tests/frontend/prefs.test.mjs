/**
 * Unit tests for Bellhop's compiled client-side settings store (`prefs.js`).
 *
 * Exercises structured JSON persistence (dicts, arrays, strings, numbers,
 * booleans), defensive cloning, corrupt-storage recovery, theme toggling,
 * button binding, and cross-tab `storage` synchronization.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";

const PREFS_JS_PATH = path.resolve("src/bellhop_web/static/prefs.js");
const PREFS_SOURCE = fs.readFileSync(PREFS_JS_PATH, "utf8");

function createBrowserSandbox(initialStorage = {}) {
  const store = new Map(Object.entries(initialStorage));
  const windowListeners = new Map();

  const localStorage = {
    getItem(key) {
      return store.has(key) ? store.get(key) : null;
    },
    setItem(key, value) {
      store.set(key, String(value));
    },
    removeItem(key) {
      store.delete(key);
    },
    clear() {
      store.clear();
    },
  };

  const documentElement = {
    dataset: {},
    style: { colorScheme: "" },
  };

  const windowObj = {
    localStorage,
    addEventListener(type, fn) {
      if (!windowListeners.has(type)) windowListeners.set(type, new Set());
      windowListeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      windowListeners.get(type)?.delete(fn);
    },
    dispatchStorageEvent(key) {
      for (const fn of windowListeners.get("storage") ?? []) {
        fn({ key });
      }
    },
  };

  const context = vm.createContext({
    window: windowObj,
    document: { documentElement },
    Object,
    Array,
    Set,
    JSON,
  });

  vm.runInContext(PREFS_SOURCE, context, { filename: "prefs.js" });
  return {
    settings: windowObj.BellhopSettings,
    documentElement,
    localStorage,
    windowObj,
    store,
  };
}

function createFakeButton() {
  const attrs = new Map();
  const listeners = new Map();
  return {
    dataset: {},
    setAttribute(name, value) {
      attrs.set(name, String(value));
    },
    getAttribute(name) {
      return attrs.get(name) ?? null;
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      listeners.get(type)?.delete(fn);
    },
    click() {
      for (const fn of listeners.get("click") ?? []) {
        fn();
      }
    },
  };
}

test("initializes defaults and applies dark theme before first paint", () => {
  const { settings, documentElement } = createBrowserSandbox();
  assert.equal(settings.STORAGE_KEY, "bellhop.settings");
  assert.equal(settings.SCHEMA_VERSION, 1);
  assert.equal(settings.getTheme(), "dark");
  assert.equal(documentElement.dataset.theme, "dark");
  assert.equal(documentElement.style.colorScheme, "dark");
  assert.deepEqual(structuredClone(settings.read()), { version: 1, ui: { theme: "dark" } });
});

test("supports nested dicts, arrays, strings, numbers, booleans, and defensive clones", () => {
  const { settings, store } = createBrowserSandbox();
  settings.set("guide.favorites", [1101, 1108]);
  settings.set("player.volume", 0.75);
  settings.set("guide.filters.withGuide", true);
  settings.patch({
    profile: { name: "Living Room", tags: ["hd", "sports"], pin: null },
  });

  const persisted = JSON.parse(store.get("bellhop.settings"));
  assert.deepEqual(persisted, {
    version: 1,
    ui: { theme: "dark" },
    guide: { favorites: [1101, 1108], filters: { withGuide: true } },
    player: { volume: 0.75 },
    profile: { name: "Living Room", tags: ["hd", "sports"], pin: null },
  });

  // Returned values are defensively cloned so external mutation cannot corrupt state.
  const favs = settings.get("guide.favorites", []);
  favs.push(9999);
  assert.deepEqual(structuredClone(settings.get("guide.favorites", [])), [1101, 1108]);
  assert.equal(settings.get("missing.path", "fallback"), "fallback");

  settings.reset();
  assert.deepEqual(structuredClone(settings.read()), { version: 1, ui: { theme: "dark" } });
});

test("hydrates existing theme, binds sun/moon toggle, and syncs across tabs", () => {
  const { settings, documentElement, store, windowObj } = createBrowserSandbox({
    "bellhop.settings": JSON.stringify({
      version: 1,
      ui: { theme: "light" },
      guide: { favorites: [1101] },
    }),
  });

  assert.equal(settings.getTheme(), "light");
  assert.equal(documentElement.dataset.theme, "light");

  const btn = createFakeButton();
  const unbind = settings.bindThemeToggle(btn);
  assert.equal(btn.getAttribute("aria-pressed"), "true");
  assert.equal(btn.getAttribute("title"), "Switch to dark mode");

  btn.click();
  assert.equal(settings.getTheme(), "dark");
  assert.equal(documentElement.dataset.theme, "dark");
  assert.equal(btn.getAttribute("aria-pressed"), "false");
  assert.equal(btn.getAttribute("title"), "Switch to light mode");
  // Existing unrelated state is preserved across theme toggles.
  assert.deepEqual(settings.get("guide.favorites", []), [1101]);

  // Simulate another browser tab switching back to light mode.
  const events = [];
  settings.subscribe((next, source) => events.push({ theme: next.ui.theme, source }));
  store.set("bellhop.settings", JSON.stringify({ version: 1, ui: { theme: "light" } }));
  windowObj.dispatchStorageEvent("bellhop.settings");

  assert.equal(settings.getTheme(), "light");
  assert.equal(documentElement.dataset.theme, "light");
  assert.equal(btn.getAttribute("aria-pressed"), "true");
  assert.deepEqual(events, [{ theme: "light", source: "storage" }]);

  unbind();
});

test("recovers cleanly from corrupt localStorage payloads and invalid themes", () => {
  const { settings, documentElement } = createBrowserSandbox({
    "bellhop.settings": "{not-valid-json",
  });
  assert.equal(settings.getTheme(), "dark");
  assert.equal(documentElement.dataset.theme, "dark");
  assert.equal(settings.setTheme("sepia"), "dark");
});
