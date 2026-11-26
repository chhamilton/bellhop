/**
 * Operational Settings drawer controller (`GET/PUT /api/config`).
 */
import { escapeHtml, parseExcludedChannelsText, parsePinnedStreamsText, } from "./guide_logic.js";
import { restoreFocus } from "./render.js";
const errorMessage = (err) => err instanceof Error ? err.message : String(err);
export const createSettingsController = (deps) => {
    const { elPlayerOverlay, elSettingsBtn, elSettingsOverlay, elSettingsBackdrop, elSettingsCloseBtn, elSettingsFileBadge, elSettingsTabs, elSettingsForm, elSettingsBody, elSettingsStatus, elSettingsReloadBtn, elSettingsSaveBtn, redirectToLogin, } = deps;
    let activeSettingsTab = "upstream_xtream";
    let lastFocusedBeforeSettings = null;
    const setSettingsStatus = (msg, kind = "") => {
        if (!elSettingsStatus)
            return;
        elSettingsStatus.textContent = msg;
        elSettingsStatus.className = `settings-status${kind ? ` is-${kind}` : ""}`;
    };
    const renderTextField = (id, label, value, { placeholder = "", hint = "", span2 = false, } = {}) => `
    <div class="settings-field${span2 ? " span-2" : ""}">
      <label for="${id}">${escapeHtml(label)}</label>
      <input id="${id}" type="text" value="${escapeHtml(value ?? "")}" placeholder="${escapeHtml(placeholder)}" />
      ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
    </div>
  `;
    const renderNumberField = (id, label, value, { step = "any", min = "", placeholder = "", hint = "", } = {}) => `
    <div class="settings-field">
      <label for="${id}">${escapeHtml(label)}</label>
      <input id="${id}" type="number" step="${step}" ${min !== "" ? `min="${min}"` : ""} value="${value === null || value === undefined ? "" : escapeHtml(String(value))}" placeholder="${escapeHtml(placeholder)}" />
      ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
    </div>
  `;
    const renderSelectField = (id, label, value, options, { hint = "" } = {}) => `
    <div class="settings-field">
      <label for="${id}">${escapeHtml(label)}</label>
      <select id="${id}">
        ${options
        .map(([val, text]) => `<option value="${escapeHtml(val)}"${val === value ? " selected" : ""}>${escapeHtml(text)}</option>`)
        .join("")}
      </select>
      ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
    </div>
  `;
    const renderCheckboxField = (id, label, checked) => `
    <label class="settings-check" for="${id}">
      <input id="${id}" type="checkbox"${checked ? " checked" : ""} />
      <span>${escapeHtml(label)}</span>
    </label>
  `;
    const syncSettingsTabVisibility = () => {
        if (!elSettingsTabs || !elSettingsBody)
            return;
        elSettingsTabs
            .querySelectorAll("[data-settings-tab]")
            .forEach((btn) => {
            const isActive = btn.dataset["settingsTab"] === activeSettingsTab;
            btn.classList.toggle("active", isActive);
            btn.setAttribute("aria-selected", isActive ? "true" : "false");
        });
        elSettingsBody
            .querySelectorAll("[data-settings-pane]")
            .forEach((pane) => {
            pane.setAttribute("role", "tabpanel");
            pane.hidden = pane.dataset["settingsPane"] !== activeSettingsTab;
        });
    };
    const renderSettingsForm = (cfg) => {
        if (!elSettingsBody)
            return;
        const xt = cfg.upstream_xtream || {};
        const pipe = cfg.pipeline || {};
        const lu = pipe.lineup || {};
        const sd = lu.schedules_direct || {};
        const iptvOrg = lu.iptv_org || {};
        const match = pipe.matching || {};
        const val = pipe.validation || {};
        const epg = pipe.epg || {};
        const out = pipe.output || {};
        const pinsText = Object.entries(lu.pinned_streams || {})
            .map(([k, v]) => `${k}=${v}`)
            .join("\n");
        const exclText = (lu.excluded_channels || []).join(", ");
        elSettingsBody.innerHTML = `
      <div class="settings-pane" data-settings-pane="upstream_xtream">
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
          <h3>Channel Overrides &amp; Provider Options</h3>
          <div class="settings-grid">
            <div class="settings-field span-2">
              <label for="cfg-lu-pins">Pinned Streams (one <code>chno=stream_id</code> per line)</label>
              <textarea id="cfg-lu-pins" placeholder="1108=410108">${escapeHtml(pinsText)}</textarea>
            </div>
            ${renderTextField("cfg-lu-excl", "Excluded Channel Numbers (comma-separated)", exclText, { placeholder: "1107, 1424", span2: true })}
            ${renderNumberField("cfg-lu-iptv-recheck", "iptv-org Recheck Interval (hours)", iptvOrg.recheck_hours, { step: "1", min: "0" })}
          </div>
          ${renderCheckboxField("cfg-lu-iptv-en", "Enrich dynamic lineups with iptv-org genres & languages", iptvOrg.enabled !== false)}
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
          ${renderCheckboxField("cfg-val-black", "Detect black/frozen video frames via ffmpeg", val.detect_black_screen !== false)}
          ${renderCheckboxField("cfg-val-audio-lang", "Verify audio track language matches channel", val.check_audio_language !== false)}
          <div class="settings-grid">
            ${renderNumberField("cfg-val-concurrency", "Probe Concurrency", val.concurrency, { step: "1", min: "1" })}
            ${renderNumberField("cfg-val-delay", "Inter-Probe Delay (sec)", val.inter_probe_delay_sec, { step: "0.05", min: "0" })}
            ${renderNumberField("cfg-val-http-timeout", "HTTP Probe Timeout (sec)", val.http_timeout_sec, { step: "0.5", min: "1" })}
            ${renderNumberField("cfg-val-min-bytes", "Minimum Bytes to Read", val.min_bytes_to_read, { step: "512", min: "512" })}
            ${renderNumberField("cfg-val-ffprobe-timeout", "ffprobe Timeout (sec)", val.ffprobe_timeout_sec, { step: "0.5", min: "1" })}
            ${renderNumberField("cfg-val-black-sec", "Black-Frame Sample Duration (sec)", val.black_sample_sec, { step: "0.25", min: "0.5" })}
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
    const collectSettingsPayload = () => {
        const valStr = (id) => {
            const el = document.getElementById(id);
            return (el?.value ?? "").trim();
        };
        const reqNum = (id) => Number(valStr(id));
        const isChecked = (id) => {
            const el = document.getElementById(id);
            return Boolean(el?.checked);
        };
        const pinnedStreams = parsePinnedStreamsText(valStr("cfg-lu-pins"));
        const excludedChannels = parseExcludedChannelsText(valStr("cfg-lu-excl"));
        return {
            upstream_xtream: {
                server_url: valStr("cfg-xt-server"),
                stream_format: valStr("cfg-xt-format"),
                request_timeout_sec: reqNum("cfg-xt-timeout"),
                refresh_hours: reqNum("cfg-xt-refresh"),
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
                    schedules_direct: {
                        auto_add_lineup: isChecked("cfg-lu-sd-autoadd"),
                    },
                    iptv_org: {
                        enabled: isChecked("cfg-lu-iptv-en"),
                        recheck_hours: reqNum("cfg-lu-iptv-recheck"),
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
                    detect_black_screen: isChecked("cfg-val-black"),
                    black_sample_sec: reqNum("cfg-val-black-sec"),
                    check_audio_language: isChecked("cfg-val-audio-lang"),
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
    const loadSettingsIntoDrawer = async () => {
        setSettingsStatus("Loading configuration…");
        try {
            const resp = await fetch("/api/config");
            if (resp.status === 401) {
                redirectToLogin();
                return;
            }
            if (!resp.ok)
                throw new Error(`HTTP ${resp.status}`);
            const data = (await resp.json());
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
        }
        catch (err) {
            setSettingsStatus(`Failed to load settings: ${errorMessage(err)}`, "error");
        }
    };
    const openSettingsDrawer = async () => {
        if (!elSettingsOverlay)
            return;
        if (elSettingsOverlay.hidden &&
            document.activeElement instanceof HTMLElement) {
            lastFocusedBeforeSettings = document.activeElement;
        }
        elSettingsOverlay.hidden = false;
        document.body.style.overflow = "hidden";
        elSettingsCloseBtn?.focus();
        await loadSettingsIntoDrawer();
    };
    const closeSettingsDrawer = () => {
        if (!elSettingsOverlay)
            return;
        elSettingsOverlay.hidden = true;
        if (elPlayerOverlay.hidden) {
            document.body.style.overflow = "";
        }
        const toRestore = lastFocusedBeforeSettings;
        lastFocusedBeforeSettings = null;
        restoreFocus(toRestore, elSettingsBtn);
    };
    const checkSettingsAvailability = async () => {
        if (!elSettingsBtn)
            return;
        try {
            const resp = await fetch("/api/config");
            if (!resp.ok)
                return;
            const data = (await resp.json());
            elSettingsBtn.hidden = !data.available;
        }
        catch {
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
        elSettingsTabs.addEventListener("click", (e) => {
            const target = e.target;
            const tabBtn = target
                ? target.closest("[data-settings-tab]")
                : null;
            const tabName = tabBtn?.dataset["settingsTab"];
            if (!tabName)
                return;
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
        elSettingsForm.addEventListener("submit", async (e) => {
            e.preventDefault();
            let payload;
            try {
                payload = collectSettingsPayload();
            }
            catch (err) {
                setSettingsStatus(errorMessage(err), "error");
                return;
            }
            if (elSettingsSaveBtn)
                elSettingsSaveBtn.disabled = true;
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
                const data = (await resp
                    .json()
                    .catch(() => ({})));
                if (!resp.ok) {
                    setSettingsStatus(data.error || `Save failed (HTTP ${resp.status})`, "error");
                    return;
                }
                if (data.config)
                    renderSettingsForm(data.config);
                if (data.restart_required) {
                    setSettingsStatus("Saved to config.yaml — restart bellhop serve to apply provider changes.", "warn");
                }
                else {
                    setSettingsStatus("Saved to config.yaml.", "ok");
                }
            }
            catch (err) {
                setSettingsStatus(`Network error saving settings: ${errorMessage(err)}`, "error");
            }
            finally {
                if (elSettingsSaveBtn)
                    elSettingsSaveBtn.disabled = false;
            }
        });
    }
    return {
        openSettingsDrawer,
        closeSettingsDrawer,
        checkSettingsAvailability,
    };
};
