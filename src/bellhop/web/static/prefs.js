"use strict";
/**
 * Bellhop client-side settings store (localStorage JSON) & theme bootstrapper.
 *
 * Persists a single structured, versioned JSON object under `bellhop.settings`
 * supporting arbitrarily nested dicts, arrays, strings, numbers, and booleans:
 *
 *   {
 *     "version": 1,
 *     "ui": {
 *       "theme": "dark"
 *     }
 *   }
 *
 * Loaded synchronously in `<head>` on both `/login` and `/` so the saved theme
 * applies before first paint with zero flash.
 */
(() => {
    "use strict";
    const STORAGE_KEY = "bellhop.settings";
    const SCHEMA_VERSION = 1;
    const DEFAULTS = Object.freeze({
        version: SCHEMA_VERSION,
        ui: Object.freeze({
            theme: "dark",
        }),
    });
    const listeners = new Set();
    const isValidTheme = (val) => val === "dark" || val === "light";
    const isPlainObject = (val) => val !== null && typeof val === "object" && !Array.isArray(val);
    const cloneJson = (val) => {
        if (Array.isArray(val)) {
            return val.map((item) => cloneJson(item));
        }
        if (isPlainObject(val)) {
            const out = {};
            for (const [k, v] of Object.entries(val)) {
                out[k] = cloneJson(v);
            }
            return out;
        }
        return val;
    };
    const deepMerge = (base, override) => {
        const result = cloneJson(base);
        for (const [key, val] of Object.entries(override)) {
            const existing = result[key];
            if (isPlainObject(val) && isPlainObject(existing)) {
                result[key] = deepMerge(existing, val);
            }
            else if (val !== undefined) {
                result[key] = cloneJson(val);
            }
        }
        return result;
    };
    const normalizeState = (raw) => {
        const merged = deepMerge(DEFAULTS, isPlainObject(raw) ? raw : {});
        if (typeof merged["version"] !== "number" || merged["version"] < 1) {
            merged["version"] = SCHEMA_VERSION;
        }
        const uiRaw = merged["ui"];
        const uiObj = isPlainObject(uiRaw) ? uiRaw : {};
        uiObj["theme"] = isValidTheme(uiObj["theme"]) ? uiObj["theme"] : "dark";
        merged["ui"] = uiObj;
        return merged;
    };
    const readState = () => {
        try {
            const raw = window.localStorage.getItem(STORAGE_KEY);
            if (!raw)
                return normalizeState({});
            return normalizeState(JSON.parse(raw));
        }
        catch {
            return normalizeState({});
        }
    };
    let cache = readState();
    const notify = (nextState, source) => {
        const snap = cloneJson(nextState);
        for (const fn of listeners) {
            try {
                fn(snap, source);
            }
            catch {
                // Ignore individual subscriber errors so one callback never breaks others.
            }
        }
    };
    const writeState = (nextState) => {
        cache = normalizeState(nextState);
        try {
            window.localStorage.setItem(STORAGE_KEY, JSON.stringify(cache));
        }
        catch {
            // Private browsing or storage quota exceeded: keep in-memory state.
        }
        notify(cache, "local");
        return cloneJson(cache);
    };
    const parsePath = (path) => {
        if (Array.isArray(path)) {
            return path.filter((p) => typeof p === "string" && p.length > 0);
        }
        if (typeof path === "string") {
            return path.split(".").filter(Boolean);
        }
        return [];
    };
    function getValue(path, fallback) {
        const parts = parsePath(path);
        let cur = cache;
        for (const key of parts) {
            if (!isPlainObject(cur) || !(key in cur))
                return fallback;
            const next = cur[key];
            if (next === undefined)
                return fallback;
            cur = next;
        }
        return cloneJson(cur);
    }
    const setValue = (path, value) => {
        const parts = parsePath(path);
        if (parts.length === 0)
            return cloneJson(cache);
        const next = cloneJson(cache);
        let cur = next;
        for (let i = 0; i < parts.length - 1; i += 1) {
            const key = parts[i];
            if (key === undefined)
                continue;
            const existing = cur[key];
            if (!isPlainObject(existing)) {
                const created = {};
                cur[key] = created;
                cur = created;
            }
            else {
                cur = existing;
            }
        }
        const leafKey = parts[parts.length - 1];
        if (leafKey !== undefined) {
            cur[leafKey] = cloneJson(value);
        }
        return writeState(next);
    };
    const patchState = (partial) => {
        if (!isPlainObject(partial))
            return cloneJson(cache);
        return writeState(deepMerge(cache, partial));
    };
    const resetState = () => writeState(DEFAULTS);
    const subscribe = (listener) => {
        listeners.add(listener);
        return () => {
            listeners.delete(listener);
        };
    };
    // Theme application & toggle helpers backed by `ui.theme`.
    const applyTheme = (theme) => {
        const resolved = isValidTheme(theme) ? theme : "dark";
        const root = document.documentElement;
        if (root) {
            root.dataset["theme"] = resolved;
            root.style.colorScheme = resolved;
        }
        return resolved;
    };
    const getTheme = () => {
        const t = getValue("ui.theme", "dark");
        return isValidTheme(t) ? t : "dark";
    };
    const setTheme = (theme) => {
        const resolved = isValidTheme(theme) ? theme : "dark";
        applyTheme(resolved);
        setValue("ui.theme", resolved);
        return resolved;
    };
    const toggleTheme = () => setTheme(getTheme() === "light" ? "dark" : "light");
    const syncToggleButton = (btn, theme) => {
        if (!btn)
            return;
        const isLight = theme === "light";
        const label = isLight ? "Switch to dark mode" : "Switch to light mode";
        btn.setAttribute("aria-pressed", isLight ? "true" : "false");
        btn.setAttribute("aria-label", label);
        btn.setAttribute("title", label);
        btn.dataset["themeState"] = theme;
    };
    const bindThemeToggle = (btn) => {
        if (!btn)
            return () => { };
        syncToggleButton(btn, getTheme());
        const onClick = () => {
            toggleTheme();
        };
        btn.addEventListener("click", onClick);
        const unsub = subscribe((next) => {
            const theme = isValidTheme(next.ui.theme)
                ? next.ui.theme
                : "dark";
            applyTheme(theme);
            syncToggleButton(btn, theme);
        });
        return () => {
            btn.removeEventListener("click", onClick);
            unsub();
        };
    };
    // Sync across open tabs when localStorage changes.
    window.addEventListener("storage", (event) => {
        if (event.key !== STORAGE_KEY)
            return;
        cache = readState();
        applyTheme(getTheme());
        notify(cache, "storage");
    });
    // Apply persisted theme immediately before first paint.
    applyTheme(getTheme());
    window.BellhopSettings = Object.freeze({
        STORAGE_KEY,
        SCHEMA_VERSION,
        DEFAULTS,
        read: () => cloneJson(cache),
        write: writeState,
        get: getValue,
        set: setValue,
        patch: patchState,
        reset: resetState,
        subscribe,
        getTheme,
        setTheme,
        toggleTheme,
        applyTheme,
        bindThemeToggle,
    });
})();
