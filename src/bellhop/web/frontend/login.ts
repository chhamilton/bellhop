/**
 * Bellhop authentication pages controller (`/login` & `/reset-password`).
 *
 * Binds the theme toggle button to `window.BellhopSettings`, handles email +
 * password login, optional Forgot Password initiation, and token-based
 * password reset submission without inline scripts so a strict
 * `script-src 'self' blob:` Content-Security-Policy is enforced.
 */
(() => {
  "use strict";

  const SIGN_IN_SUBTITLE = "Sign in to access your live TV guide & player";
  const FORGOT_SUBTITLE = "Enter your email for a password reset link";
  const MAX_PASSWORD_CHARS = 256;
  const MAX_PASSWORD_BYTES = 1024;

  interface LoginApiResponse {
    authenticated?: boolean;
    error?: string;
  }

  interface ApiErrorShape {
    error?: string;
  }

  const themeBtn = document.getElementById("theme-toggle-btn");
  if (window.BellhopSettings && themeBtn) {
    window.BellhopSettings.bindThemeToggle(themeBtn);
  }

  const errEl = document.getElementById("login-error");
  const noticeEl = document.getElementById("login-notice");
  const subtitleEl = document.getElementById("login-subtitle");

  const clearAlerts = (): void => {
    if (errEl) errEl.hidden = true;
    if (noticeEl) noticeEl.hidden = true;
  };

  const showError = (msg: string): void => {
    if (noticeEl) noticeEl.hidden = true;
    if (errEl) {
      errEl.textContent = msg;
      errEl.hidden = false;
    }
  };

  const showNotice = (msg: string): void => {
    if (errEl) errEl.hidden = true;
    if (noticeEl) {
      noticeEl.textContent = msg;
      noticeEl.hidden = false;
    }
  };

  const utf8ByteLength = (value: string): number => {
    if (typeof TextEncoder === "function") {
      return new TextEncoder().encode(value).length;
    }
    let bytes = 0;
    for (let i = 0; i < value.length; i += 1) {
      const code = value.charCodeAt(i);
      if (code < 0x80) bytes += 1;
      else if (code < 0x800) bytes += 2;
      else if (code >= 0xd800 && code <= 0xdbff) {
        bytes += 4;
        i += 1;
      } else bytes += 3;
    }
    return bytes;
  };

  const form = document.getElementById("login-form") as HTMLFormElement | null;
  const btn = document.getElementById(
    "login-submit",
  ) as HTMLButtonElement | null;
  const emailInput = document.getElementById(
    "email",
  ) as HTMLInputElement | null;
  const passInput = document.getElementById(
    "password",
  ) as HTMLInputElement | null;

  if (form && errEl && btn && emailInput && passInput) {
    const nextInput = form.querySelector<HTMLInputElement>('input[name="next"]');
    const nextUrl = nextInput?.value || "/";

    const forgotLink = document.getElementById(
      "forgot-password-link",
    ) as HTMLButtonElement | null;
    const forgotForm = document.getElementById(
      "forgot-password-form",
    ) as HTMLFormElement | null;
    const forgotEmail = document.getElementById(
      "forgot-email",
    ) as HTMLInputElement | null;
    const forgotBtn = document.getElementById(
      "forgot-submit",
    ) as HTMLButtonElement | null;
    const backLink = document.getElementById(
      "back-to-login-link",
    ) as HTMLButtonElement | null;

    const showLoginMode = (preserveNotice: boolean): void => {
      if (!preserveNotice) clearAlerts();
      else if (errEl) errEl.hidden = true;
      form.hidden = false;
      if (forgotForm) forgotForm.hidden = true;
      if (subtitleEl) subtitleEl.textContent = SIGN_IN_SUBTITLE;
    };

    if (forgotLink && forgotForm && forgotEmail && forgotBtn && backLink) {
      forgotLink.addEventListener("click", () => {
        clearAlerts();
        if (!forgotEmail.value.trim() && emailInput.value.trim()) {
          forgotEmail.value = emailInput.value.trim();
        }
        form.hidden = true;
        forgotForm.hidden = false;
        if (subtitleEl) subtitleEl.textContent = FORGOT_SUBTITLE;
        forgotEmail.focus();
      });

      backLink.addEventListener("click", () => {
        showLoginMode(false);
        emailInput.focus();
      });

      forgotForm.addEventListener("submit", async (e: SubmitEvent) => {
        e.preventDefault();
        clearAlerts();
        forgotBtn.disabled = true;
        forgotBtn.textContent = "Sending…";
        try {
          const resp = await fetch("/api/auth/forgot-password", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              email: forgotEmail.value.trim(),
            }),
          });
          const data = (await resp.json().catch(() => ({}))) as Partial<
            ForgotPasswordResponse & ApiErrorShape
          >;
          if (!resp.ok) {
            showError(
              data.error || "Unable to send reset link. Please try again.",
            );
            return;
          }
          showLoginMode(true);
          showNotice(
            data.message ||
              "If an account with that email exists, we've sent a password reset link.",
          );
        } catch {
          showError("Unable to reach server. Please try again.");
        } finally {
          forgotBtn.disabled = false;
          forgotBtn.textContent = "Send Reset Link";
        }
      });
    }

    form.addEventListener("submit", async (e: SubmitEvent) => {
      e.preventDefault();
      clearAlerts();
      btn.disabled = true;
      btn.textContent = "Signing in…";
      try {
        const resp = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            email: emailInput.value,
            password: passInput.value,
          }),
        });
        const data = (await resp
          .json()
          .catch(() => ({}))) as LoginApiResponse;
        if (!resp.ok) {
          showError(data.error || "Invalid email or password.");
          btn.disabled = false;
          btn.textContent = "Sign In";
          return;
        }
        const finalNext =
          !nextUrl.includes("#") && window.location.hash
            ? `${nextUrl}${window.location.hash}`
            : nextUrl;
        window.location.replace(finalNext);
      } catch {
        showError("Unable to reach server. Please try again.");
        btn.disabled = false;
        btn.textContent = "Sign In";
      }
    });
  }

  // Strip any emailed reset token from browser history/Referer immediately.
  if (
    typeof window.history?.replaceState === "function" &&
    typeof window.location?.search === "string" &&
    window.location.search.includes("token=")
  ) {
    const pathname = window.location.pathname || "/reset-password";
    if (typeof URLSearchParams === "function") {
      const params = new URLSearchParams(window.location.search);
      params.delete("token");
      const queryStr = params.toString();
      const nextHref = `${pathname}${queryStr ? `?${queryStr}` : ""}${window.location.hash || ""}`;
      window.history.replaceState(null, "", nextHref);
    } else {
      window.history.replaceState(
        null,
        "",
        `${pathname}${window.location.hash || ""}`,
      );
    }
  }

  const resetForm = document.getElementById(
    "reset-password-form",
  ) as HTMLFormElement | null;
  const resetTokenInput = document.getElementById(
    "reset-token",
  ) as HTMLInputElement | null;
  const resetNewInput = document.getElementById(
    "reset-new-password",
  ) as HTMLInputElement | null;
  const resetConfirmInput = document.getElementById(
    "reset-confirm-password",
  ) as HTMLInputElement | null;
  const resetBtn = document.getElementById(
    "reset-submit",
  ) as HTMLButtonElement | null;

  if (
    resetForm &&
    errEl &&
    resetTokenInput &&
    resetNewInput &&
    resetConfirmInput &&
    resetBtn
  ) {
    resetForm.addEventListener("submit", async (e: SubmitEvent) => {
      e.preventDefault();
      clearAlerts();
      const token = resetTokenInput.value;
      const newPassword = resetNewInput.value;
      const confirmPassword = resetConfirmInput.value;

      if (!token) {
        showError("This password reset link is invalid or has expired.");
        return;
      }
      if (newPassword.trim().length < 8) {
        showError("New password must contain at least 8 non-space characters.");
        return;
      }
      if (
        newPassword.length > MAX_PASSWORD_CHARS ||
        utf8ByteLength(newPassword) > MAX_PASSWORD_BYTES
      ) {
        showError(
          `New password must not exceed ${MAX_PASSWORD_CHARS} characters.`,
        );
        return;
      }
      if (newPassword !== confirmPassword) {
        showError("New password and confirmation do not match.");
        return;
      }

      resetBtn.disabled = true;
      resetBtn.textContent = "Resetting…";
      try {
        const resp = await fetch("/api/auth/reset-password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            token,
            new_password: newPassword,
          }),
        });
        const data = (await resp.json().catch(() => ({}))) as Partial<
          ResetPasswordResponse & ApiErrorShape
        >;
        if (!resp.ok) {
          showError(data.error || "Unable to reset password. Please try again.");
          resetBtn.disabled = false;
          resetBtn.textContent = "Reset Password";
          return;
        }
        window.location.replace("/");
      } catch {
        showError("Unable to reach server. Please try again.");
        resetBtn.disabled = false;
        resetBtn.textContent = "Reset Password";
      }
    });
  }
})();
