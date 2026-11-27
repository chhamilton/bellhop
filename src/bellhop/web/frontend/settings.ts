/**
 * Account & Player Setup drawer controller (`/api/account*`).
 */

import { escapeHtml } from "./guide_logic.js";
import { restoreFocus } from "./render.js";

export interface SettingsControllerDeps {
  elPlayerOverlay: HTMLElement;
  elSettingsBtn: HTMLButtonElement | null;
  elSettingsOverlay: HTMLElement | null;
  elSettingsBackdrop: HTMLElement | null;
  elSettingsCloseBtn: HTMLElement | null;
  elSettingsEmailBadge: HTMLElement | null;
  elSettingsTabs?: HTMLElement | null;
  elSettingsBody: HTMLElement | null;
  elSettingsStatus: HTMLElement | null;
  elSettingsReloadBtn: HTMLElement | null;
  redirectToLogin: () => void;
  confirmRegenerate?: () => boolean;
}

export interface SettingsController {
  openSettingsDrawer(initialTab?: SettingsTabId): Promise<void>;
  closeSettingsDrawer(): void;
  checkSettingsAvailability(): Promise<void>;
  setActiveTab(tab: SettingsTabId): void;
}

const MAX_PASSWORD_CHARS = 256;
const MAX_PASSWORD_BYTES = 1024;

const errorMessage = (err: unknown): string =>
  err instanceof Error ? err.message : String(err);

const defaultConfirmRegenerate = (): boolean =>
  window.confirm(
    "Regenerate both your Bellhop Xtream username and password? " +
      "Any external IPTV players using the old credentials must be updated.",
  );

export const createSettingsController = (
  deps: SettingsControllerDeps,
): SettingsController => {
  const {
    elPlayerOverlay,
    elSettingsBtn,
    elSettingsOverlay,
    elSettingsBackdrop,
    elSettingsCloseBtn,
    elSettingsEmailBadge,
    elSettingsTabs = null,
    elSettingsBody,
    elSettingsStatus,
    elSettingsReloadBtn,
    redirectToLogin,
    confirmRegenerate = defaultConfirmRegenerate,
  } = deps;

  let lastFocusedBeforeSettings: HTMLElement | null = null;
  let revealPassword = false;
  let activeTab: SettingsTabId = "stream";

  const setSettingsStatus = (
    msg: string,
    kind: SettingsStatusKind = "",
  ): void => {
    if (!elSettingsStatus) return;
    elSettingsStatus.textContent = msg;
    elSettingsStatus.className = `settings-status${kind ? ` is-${kind}` : ""}`;
  };

  const syncTabDom = (): void => {
    if (elSettingsTabs) {
      elSettingsTabs
        .querySelectorAll<HTMLButtonElement>("[data-settings-tab]")
        .forEach((btn) => {
          const isMatch = btn.dataset["settingsTab"] === activeTab;
          btn.classList.toggle("active", isMatch);
          btn.setAttribute("aria-selected", isMatch ? "true" : "false");
          btn.setAttribute("tabindex", isMatch ? "0" : "-1");
        });
    }
    if (elSettingsBody) {
      const streamPane = elSettingsBody.querySelector<HTMLElement>(
        "#settings-pane-stream",
      );
      const pwdPane = elSettingsBody.querySelector<HTMLElement>(
        "#settings-pane-password",
      );
      if (streamPane) streamPane.hidden = activeTab !== "stream";
      if (pwdPane) pwdPane.hidden = activeTab !== "password";
    }
    if (elSettingsReloadBtn) {
      elSettingsReloadBtn.hidden = activeTab === "password";
    }
  };

  const setActiveTab = (
    nextTab: SettingsTabId,
    opts: { focusTab?: boolean; clearStatus?: boolean } = {},
  ): void => {
    activeTab = nextTab === "password" ? "password" : "stream";
    syncTabDom();
    if (opts.focusTab && elSettingsTabs) {
      const targetBtn = elSettingsTabs.querySelector<HTMLButtonElement>(
        `[data-settings-tab="${activeTab}"]`,
      );
      targetBtn?.focus();
    }
    if (opts.clearStatus !== false) {
      setSettingsStatus("");
    }
  };

  const renderCopyField = (
    id: string,
    label: string,
    value: string,
    {
      inputType = "text",
      hint = "",
      span2 = false,
      toggleReveal = false,
    }: {
      inputType?: "text" | "password";
      hint?: string;
      span2?: boolean;
      toggleReveal?: boolean;
    } = {},
  ): string => `
    <div class="settings-field${span2 ? " span-2" : ""}">
      <label for="${id}">${escapeHtml(label)}</label>
      <div class="copy-row">
        <input id="${id}" type="${inputType}" value="${escapeHtml(value)}" autocomplete="off" spellcheck="false" readonly />
        ${
          toggleReveal
            ? `<button id="toggle-xt-pass-btn" class="modal-btn" type="button">${inputType === "password" ? "Show" : "Hide"}</button>`
            : ""
        }
        <button class="modal-btn" type="button" data-copy-target="${id}"${value ? "" : " disabled"}>Copy</button>
      </div>
      ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
    </div>
  `;

  const renderAccountView = (info: AccountInfoResponse): void => {
    if (!elSettingsBody) return;
    if (elSettingsEmailBadge) {
      elSettingsEmailBadge.textContent = info.email;
    }
    const urls = info.player_urls;
    const xtreamEnabled = Boolean(urls.xtream_server_url);
    const urlsSection = xtreamEnabled
      ? `
        <div class="settings-grid">
          ${renderCopyField("player-xt-server", "Xtream Codes Server URL", urls.xtream_server_url, { span2: true })}
          ${renderCopyField("player-m3u-ts", "M3U8 Playlist URL (MPEG-TS)", urls.m3u_url, { span2: true })}
          ${renderCopyField("player-m3u-hls", "M3U8 Playlist URL (HLS)", urls.m3u_hls_url, { span2: true })}
          ${renderCopyField("player-xmltv-url", "XMLTV EPG Guide URL", urls.xmltv_url, { span2: true })}
        </div>
      `
      : `
        <div id="player-xtream-disabled-note" class="settings-alert-note">
          Xtream API listener is disabled (<code>xtream_server.enabled: false</code> in <code>config.yaml</code>). Enable it to connect external IPTV players.
        </div>
      `;

    elSettingsBody.innerHTML = `
      <section id="settings-pane-stream" class="settings-pane" role="tabpanel" aria-labelledby="settings-tab-stream"${activeTab === "stream" ? "" : " hidden"}>
        <div class="settings-group">
          <h3>Bellhop Xtream Stream Credentials</h3>
          <div class="settings-grid">
            ${renderCopyField("acct-xt-user", "Xtream Username", info.xtream_username)}
            ${renderCopyField("acct-xt-pass", "Xtream Password", info.xtream_password, {
              inputType: revealPassword ? "text" : "password",
              toggleReveal: true,
            })}
          </div>
          <div class="settings-actions-row">
            <span class="field-hint">Used by external IPTV players (TiviMate, VLC, IPTV Smarters) on your dedicated tuner.</span>
            <button id="regen-stream-creds-btn" class="modal-btn" type="button">Regenerate Stream Credentials</button>
          </div>
        </div>

        <div class="settings-group">
          <h3>External Player Setup</h3>
          ${urlsSection}
        </div>
      </section>

      <section id="settings-pane-password" class="settings-pane" role="tabpanel" aria-labelledby="settings-tab-password"${activeTab === "password" ? "" : " hidden"}>
        <form id="password-change-form" class="settings-group">
          <h3>Change Web Password</h3>
          <input
            id="pwd-username"
            class="sr-only"
            type="email"
            name="username"
            value="${escapeHtml(info.email)}"
            autocomplete="username"
            readonly
            tabindex="-1"
            aria-hidden="true"
          />
          <div class="settings-stack">
            <div class="settings-field">
              <label for="pwd-current">Current Password</label>
              <input id="pwd-current" type="password" autocomplete="current-password" maxlength="${MAX_PASSWORD_CHARS}" required />
            </div>
            <div class="settings-field">
              <label for="pwd-new">New Password</label>
              <input id="pwd-new" type="password" autocomplete="new-password" minlength="8" maxlength="${MAX_PASSWORD_CHARS}" required />
            </div>
            <div class="settings-field">
              <label for="pwd-confirm">Confirm New Password</label>
              <input id="pwd-confirm" type="password" autocomplete="new-password" minlength="8" maxlength="${MAX_PASSWORD_CHARS}" required />
            </div>
          </div>
          <div class="settings-actions-row">
            <span class="field-hint">8–${MAX_PASSWORD_CHARS} characters. Other signed-in web browsers will be signed out.</span>
            <button id="pwd-save-btn" class="modal-btn primary-btn" type="submit">Update Password</button>
          </div>
        </form>
      </section>
    `;
    syncTabDom();
  };

  const loadAccountIntoDrawer = async (): Promise<void> => {
    setSettingsStatus("Loading account details…");
    try {
      const resp = await fetch("/api/account");
      if (resp.status === 401) {
        redirectToLogin();
        return;
      }
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = (await resp.json()) as AccountInfoResponse;
      renderAccountView(data);
      setSettingsStatus("");
    } catch (err: unknown) {
      setSettingsStatus(
        `Unable to load account details: ${errorMessage(err)}`,
        "error",
      );
    }
  };

  const openSettingsDrawer = async (
    initialTab: SettingsTabId = "stream",
  ): Promise<void> => {
    if (!elSettingsOverlay) return;
    lastFocusedBeforeSettings = document.activeElement as HTMLElement | null;
    activeTab = initialTab === "password" ? "password" : "stream";
    syncTabDom();
    elSettingsOverlay.hidden = false;
    elSettingsCloseBtn?.focus();
    await loadAccountIntoDrawer();
  };

  const closeSettingsDrawer = (): void => {
    if (!elSettingsOverlay) return;
    elSettingsOverlay.hidden = true;
    restoreFocus(
      lastFocusedBeforeSettings,
      !elPlayerOverlay.hidden ? elPlayerOverlay : elSettingsBtn,
    );
    lastFocusedBeforeSettings = null;
  };

  const checkSettingsAvailability = async (): Promise<void> => {
    if (!elSettingsBtn) return;
    try {
      const resp = await fetch("/api/account");
      if (!resp.ok) return;
      const data = (await resp.json()) as AccountInfoResponse;
      if (elSettingsEmailBadge && data.email) {
        elSettingsEmailBadge.textContent = data.email;
      }
      elSettingsBtn.hidden = false;
    } catch {
      // Keep Account button hidden if account endpoint is unreachable.
    }
  };

  const handleCopyTarget = async (
    targetId: string,
    btn: HTMLButtonElement,
  ): Promise<void> => {
    const input = document.getElementById(targetId) as HTMLInputElement | null;
    if (!input || !input.value) return;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(input.value);
      } else {
        const prevType = input.type;
        if (prevType === "password") input.type = "text";
        input.select();
        if (prevType === "password") input.type = prevType;
      }
      const prevLabel = btn.textContent || "Copy";
      btn.textContent = "Copied";
      setSettingsStatus("Copied to clipboard.", "ok");
      window.setTimeout(() => {
        btn.textContent = prevLabel;
      }, 1500);
    } catch (err: unknown) {
      setSettingsStatus(`Copy failed: ${errorMessage(err)}`, "error");
    }
  };

  const handleRegenerateCredentials = async (
    btn: HTMLButtonElement,
  ): Promise<void> => {
    if (!confirmRegenerate()) return;
    btn.disabled = true;
    setSettingsStatus("Regenerating stream credentials…");
    try {
      const resp = await fetch("/api/account/stream-credentials/regenerate", {
        method: "POST",
      });
      if (resp.status === 401) {
        redirectToLogin();
        return;
      }
      const data = (await resp.json().catch(() => ({}))) as Partial<
        AccountInfoResponse
      > & { error?: string };
      if (!resp.ok || !data.xtream_username) {
        setSettingsStatus(
          data.error || `Regeneration failed (HTTP ${resp.status})`,
          "error",
        );
        return;
      }
      renderAccountView(data as AccountInfoResponse);
      setSettingsStatus(
        "New Bellhop Xtream credentials saved. Update your external IPTV players.",
        "ok",
      );
    } catch (err: unknown) {
      setSettingsStatus(
        `Network error regenerating credentials: ${errorMessage(err)}`,
        "error",
      );
    } finally {
      btn.disabled = false;
    }
  };

  const handlePasswordSubmit = async (
    form: HTMLFormElement,
  ): Promise<void> => {
    const curInput = form.querySelector<HTMLInputElement>("#pwd-current");
    const newInput = form.querySelector<HTMLInputElement>("#pwd-new");
    const confInput = form.querySelector<HTMLInputElement>("#pwd-confirm");
    const saveBtn = form.querySelector<HTMLButtonElement>("#pwd-save-btn");
    if (!curInput || !newInput || !confInput) return;

    const currentPassword = curInput.value;
    const newPassword = newInput.value;
    const confirmPassword = confInput.value;
    if (!currentPassword || !newPassword) {
      setSettingsStatus("Enter current and new passwords.", "error");
      return;
    }
    if (newPassword.trim().length < 8) {
      setSettingsStatus(
        "New password must be at least 8 non-space characters.",
        "error",
      );
      return;
    }
    if (
      newPassword.length > MAX_PASSWORD_CHARS ||
      new TextEncoder().encode(newPassword).length > MAX_PASSWORD_BYTES
    ) {
      setSettingsStatus(
        `New password must not exceed ${MAX_PASSWORD_CHARS} characters.`,
        "error",
      );
      return;
    }
    if (newPassword !== confirmPassword) {
      setSettingsStatus("New password and confirmation do not match.", "error");
      return;
    }
    if (newPassword === currentPassword) {
      setSettingsStatus(
        "New password must be different from your current password.",
        "error",
      );
      return;
    }

    if (saveBtn) saveBtn.disabled = true;
    setSettingsStatus("Updating password…");
    try {
      const resp = await fetch("/api/account/password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });
      if (resp.status === 401) {
        redirectToLogin();
        return;
      }
      const data = (await resp.json().catch(() => ({}))) as Partial<
        PasswordChangeResponse
      > & { error?: string };
      if (!resp.ok || !data.ok) {
        setSettingsStatus(
          data.error || `Password update failed (HTTP ${resp.status})`,
          "error",
        );
        return;
      }
      curInput.value = "";
      newInput.value = "";
      confInput.value = "";
      setSettingsStatus("Web password updated.", "ok");
    } catch (err: unknown) {
      setSettingsStatus(
        `Network error updating password: ${errorMessage(err)}`,
        "error",
      );
    } finally {
      if (saveBtn) saveBtn.disabled = false;
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
  if (elSettingsReloadBtn) {
    elSettingsReloadBtn.addEventListener("click", () => {
      void loadAccountIntoDrawer();
    });
  }
  if (elSettingsTabs) {
    elSettingsTabs.addEventListener("click", (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      const btn = target
        ? target.closest<HTMLButtonElement>("[data-settings-tab]")
        : null;
      const tab = btn?.dataset["settingsTab"];
      if (tab === "stream" || tab === "password") {
        setActiveTab(tab);
      }
    });
    elSettingsTabs.addEventListener("keydown", (e: KeyboardEvent) => {
      if (
        e.key !== "ArrowRight" &&
        e.key !== "ArrowLeft" &&
        e.key !== "Home" &&
        e.key !== "End"
      ) {
        return;
      }
      e.preventDefault();
      if (e.key === "Home") {
        setActiveTab("stream", { focusTab: true });
      } else if (e.key === "End") {
        setActiveTab("password", { focusTab: true });
      } else {
        setActiveTab(activeTab === "stream" ? "password" : "stream", {
          focusTab: true,
        });
      }
    });
  }
  if (elSettingsBody) {
    elSettingsBody.addEventListener("click", (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      const btn = target ? target.closest<HTMLButtonElement>("button") : null;
      if (!btn) return;
      if (btn.id === "toggle-xt-pass-btn") {
        revealPassword = !revealPassword;
        const passEl = document.getElementById(
          "acct-xt-pass",
        ) as HTMLInputElement | null;
        if (passEl) {
          passEl.type = revealPassword ? "text" : "password";
        }
        btn.textContent = revealPassword ? "Hide" : "Show";
        return;
      }
      if (btn.id === "regen-stream-creds-btn") {
        void handleRegenerateCredentials(btn);
        return;
      }
      const copyTarget = btn.dataset["copyTarget"];
      if (copyTarget) {
        void handleCopyTarget(copyTarget, btn);
      }
    });
    elSettingsBody.addEventListener("submit", (e: SubmitEvent) => {
      const form = e.target as HTMLFormElement | null;
      if (form && form.id === "password-change-form") {
        e.preventDefault();
        void handlePasswordSubmit(form);
      }
    });
  }

  return {
    openSettingsDrawer,
    closeSettingsDrawer,
    checkSettingsAvailability,
    setActiveTab,
  };
};
