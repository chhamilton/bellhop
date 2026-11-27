"use strict";
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
    const themeBtn = document.getElementById("theme-toggle-btn");
    if (window.BellhopSettings && themeBtn) {
        window.BellhopSettings.bindThemeToggle(themeBtn);
    }
    const errEl = document.getElementById("login-error");
    const noticeEl = document.getElementById("login-notice");
    const subtitleEl = document.getElementById("login-subtitle");
    const clearAlerts = () => {
        if (errEl)
            errEl.hidden = true;
        if (noticeEl)
            noticeEl.hidden = true;
    };
    const showError = (msg) => {
        if (noticeEl)
            noticeEl.hidden = true;
        if (errEl) {
            errEl.textContent = msg;
            errEl.hidden = false;
        }
    };
    const showNotice = (msg) => {
        if (errEl)
            errEl.hidden = true;
        if (noticeEl) {
            noticeEl.textContent = msg;
            noticeEl.hidden = false;
        }
    };
    const utf8ByteLength = (value) => {
        if (typeof TextEncoder === "function") {
            return new TextEncoder().encode(value).length;
        }
        let bytes = 0;
        for (let i = 0; i < value.length; i += 1) {
            const code = value.charCodeAt(i);
            if (code < 0x80)
                bytes += 1;
            else if (code < 0x800)
                bytes += 2;
            else if (code >= 0xd800 && code <= 0xdbff) {
                bytes += 4;
                i += 1;
            }
            else
                bytes += 3;
        }
        return bytes;
    };
    const form = document.getElementById("login-form");
    const btn = document.getElementById("login-submit");
    const emailInput = document.getElementById("email");
    const passInput = document.getElementById("password");
    if (form && errEl && btn && emailInput && passInput) {
        const nextInput = form.querySelector('input[name="next"]');
        const nextUrl = nextInput?.value || "/";
        const forgotLink = document.getElementById("forgot-password-link");
        const forgotForm = document.getElementById("forgot-password-form");
        const forgotEmail = document.getElementById("forgot-email");
        const forgotBtn = document.getElementById("forgot-submit");
        const backLink = document.getElementById("back-to-login-link");
        const showLoginMode = (preserveNotice) => {
            if (!preserveNotice)
                clearAlerts();
            else if (errEl)
                errEl.hidden = true;
            form.hidden = false;
            if (forgotForm)
                forgotForm.hidden = true;
            if (subtitleEl)
                subtitleEl.textContent = SIGN_IN_SUBTITLE;
        };
        if (forgotLink && forgotForm && forgotEmail && forgotBtn && backLink) {
            forgotLink.addEventListener("click", () => {
                clearAlerts();
                if (!forgotEmail.value.trim() && emailInput.value.trim()) {
                    forgotEmail.value = emailInput.value.trim();
                }
                form.hidden = true;
                forgotForm.hidden = false;
                if (subtitleEl)
                    subtitleEl.textContent = FORGOT_SUBTITLE;
                forgotEmail.focus();
            });
            backLink.addEventListener("click", () => {
                showLoginMode(false);
                emailInput.focus();
            });
            forgotForm.addEventListener("submit", async (e) => {
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
                    const data = (await resp.json().catch(() => ({})));
                    if (!resp.ok) {
                        showError(data.error || "Unable to send reset link. Please try again.");
                        return;
                    }
                    showLoginMode(true);
                    showNotice(data.message ||
                        "If an account with that email exists, we've sent a password reset link.");
                }
                catch {
                    showError("Unable to reach server. Please try again.");
                }
                finally {
                    forgotBtn.disabled = false;
                    forgotBtn.textContent = "Send Reset Link";
                }
            });
        }
        form.addEventListener("submit", async (e) => {
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
                    .catch(() => ({})));
                if (!resp.ok) {
                    showError(data.error || "Invalid email or password.");
                    btn.disabled = false;
                    btn.textContent = "Sign In";
                    return;
                }
                const finalNext = !nextUrl.includes("#") && window.location.hash
                    ? `${nextUrl}${window.location.hash}`
                    : nextUrl;
                window.location.replace(finalNext);
            }
            catch {
                showError("Unable to reach server. Please try again.");
                btn.disabled = false;
                btn.textContent = "Sign In";
            }
        });
    }
    // Strip any emailed reset token from browser history/Referer immediately.
    if (typeof window.history?.replaceState === "function" &&
        typeof window.location?.search === "string" &&
        window.location.search.includes("token=")) {
        const pathname = window.location.pathname || "/reset-password";
        if (typeof URLSearchParams === "function") {
            const params = new URLSearchParams(window.location.search);
            params.delete("token");
            const queryStr = params.toString();
            const nextHref = `${pathname}${queryStr ? `?${queryStr}` : ""}${window.location.hash || ""}`;
            window.history.replaceState(null, "", nextHref);
        }
        else {
            window.history.replaceState(null, "", `${pathname}${window.location.hash || ""}`);
        }
    }
    const resetForm = document.getElementById("reset-password-form");
    const resetTokenInput = document.getElementById("reset-token");
    const resetNewInput = document.getElementById("reset-new-password");
    const resetConfirmInput = document.getElementById("reset-confirm-password");
    const resetBtn = document.getElementById("reset-submit");
    if (resetForm &&
        errEl &&
        resetTokenInput &&
        resetNewInput &&
        resetConfirmInput &&
        resetBtn) {
        resetForm.addEventListener("submit", async (e) => {
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
            if (newPassword.length > MAX_PASSWORD_CHARS ||
                utf8ByteLength(newPassword) > MAX_PASSWORD_BYTES) {
                showError(`New password must not exceed ${MAX_PASSWORD_CHARS} characters.`);
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
                const data = (await resp.json().catch(() => ({})));
                if (!resp.ok) {
                    showError(data.error || "Unable to reset password. Please try again.");
                    resetBtn.disabled = false;
                    resetBtn.textContent = "Reset Password";
                    return;
                }
                window.location.replace("/");
            }
            catch {
                showError("Unable to reach server. Please try again.");
                resetBtn.disabled = false;
                resetBtn.textContent = "Reset Password";
            }
        });
    }
})();
