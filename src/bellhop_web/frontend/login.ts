/**
 * Bellhop sign-in page controller (`/login`).
 *
 * Binds the theme toggle button to `window.BellhopSettings` and submits
 * credentials asynchronously to `/api/auth/login` without inline scripts so a
 * strict `script-src 'self' blob:` Content-Security-Policy is enforced.
 */
(() => {
  "use strict";

  interface LoginApiResponse {
    authenticated?: boolean;
    error?: string;
  }

  const themeBtn = document.getElementById("theme-toggle-btn");
  if (window.BellhopSettings && themeBtn) {
    window.BellhopSettings.bindThemeToggle(themeBtn);
  }

  const form = document.getElementById("login-form") as HTMLFormElement | null;
  const errEl = document.getElementById("login-error");
  const btn = document.getElementById(
    "login-submit",
  ) as HTMLButtonElement | null;
  const userInput = document.getElementById(
    "username",
  ) as HTMLInputElement | null;
  const passInput = document.getElementById(
    "password",
  ) as HTMLInputElement | null;
  if (!form || !errEl || !btn || !userInput || !passInput) return;

  const nextInput = form.querySelector<HTMLInputElement>('input[name="next"]');
  const nextUrl = nextInput?.value || "/";

  form.addEventListener("submit", async (e: SubmitEvent) => {
    e.preventDefault();
    errEl.hidden = true;
    btn.disabled = true;
    btn.textContent = "Signing in…";
    try {
      const resp = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: userInput.value,
          password: passInput.value,
        }),
      });
      const data = (await resp
        .json()
        .catch(() => ({}))) as LoginApiResponse;
      if (!resp.ok) {
        errEl.textContent = data.error || "Invalid username or password.";
        errEl.hidden = false;
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
      errEl.textContent = "Unable to reach server. Please try again.";
      errEl.hidden = false;
      btn.disabled = false;
      btn.textContent = "Sign In";
    }
  });
})();
