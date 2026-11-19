"""Signed expiring session-cookie authentication for the Bellhop Web Guide (`bellhop serve`).

Uses Python standard-library primitives (`hmac`, `hashlib.sha256`, `secrets`, `http.cookies`)
implementing the same HMAC-SHA256 timed token design as `itsdangerous.URLSafeTimedSerializer`,
with zero extra runtime dependencies and persistent signing key support across restarts.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from math import ceil
from pathlib import Path
from urllib.parse import quote

SESSION_COOKIE_NAME = "bellhop_session"


def _b64u_encode(raw: bytes) -> str:
  return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(token: str) -> bytes:
  pad = "=" * ((4 - len(token) % 4) % 4)
  return base64.urlsafe_b64decode((token + pad).encode("ascii"))


def sanitize_next_url(raw_next: str | None) -> str:
  """Allow only safe relative same-origin redirect targets (preventing open redirects)."""
  if not raw_next:
    return "/"
  cleaned = raw_next.strip()
  if not cleaned.startswith("/") or cleaned.startswith("//") or "\\" in cleaned:
    return "/"
  if cleaned.startswith("/login"):
    return "/"
  return cleaned


@dataclass(slots=True)
class WebAuthConfig:
  username: str = ""
  password: str = ""
  session_ttl_hours: float = 168.0  # 7 days
  session_secret: str = ""
  secret_file: Path | None = None
  secure_cookie: bool = False


@dataclass(slots=True)
class SessionInfo:
  username: str
  issued_at: int
  expires_at: int

  def needs_renewal(self, *, now_ts: int | None = None) -> bool:
    """Sliding expiry: renew cookie once past half its total TTL."""
    now = int(time.time()) if now_ts is None else now_ts
    ttl = max(1, self.expires_at - self.issued_at)
    return (now - self.issued_at) >= (ttl // 2)


class WebAuthManager:
  """Manages Web Guide login verification and HMAC-SHA256 signed session cookies."""

  def __init__(self, config: WebAuthConfig) -> None:
    self.config = config
    self._key: bytes | None = None

  @property
  def enabled(self) -> bool:
    return bool(self.config.username and self.config.password)

  @property
  def ttl_seconds(self) -> int:
    return max(60, int(round(self.config.session_ttl_hours * 3600.0)))

  def _get_signing_key(self) -> bytes:
    if self._key is not None:
      return self._key

    if self.config.session_secret:
      self._key = hashlib.sha256(
        self.config.session_secret.encode("utf-8")
      ).digest()
      return self._key

    secret_file = self.config.secret_file
    if secret_file is not None:
      try:
        if secret_file.is_file():
          stored = secret_file.read_text(encoding="utf-8").strip()
          if len(stored) >= 32:
            self._key = hashlib.sha256(stored.encode("utf-8")).digest()
            return self._key
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_hex(32)
        tmp = secret_file.with_suffix(".key.tmp")
        tmp.write_text(generated + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
          os.chmod(tmp, 0o600)
        tmp.replace(secret_file)
        self._key = hashlib.sha256(generated.encode("utf-8")).digest()
        return self._key
      except OSError:
        pass

    self._key = secrets.token_bytes(32)
    return self._key

  def _credential_fingerprint(self) -> str:
    """Short HMAC binding sessions to current credentials (password change invalidates old cookies)."""
    key = self._get_signing_key()
    material = f"{self.config.username}\0{self.config.password}".encode()
    return hmac.new(key, material, hashlib.sha256).hexdigest()[:12]

  def verify_credentials(self, username: str, password: str) -> bool:
    if not self.enabled:
      return True
    return hmac.compare_digest(
      username, self.config.username
    ) and hmac.compare_digest(password, self.config.password)

  def create_session_token(
    self, username: str, *, now_ts: int | None = None
  ) -> tuple[str, SessionInfo]:
    now = int(time.time()) if now_ts is None else now_ts
    exp = now + self.ttl_seconds
    payload = {
      "u": username,
      "iat": now,
      "exp": exp,
      "v": self._credential_fingerprint(),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64u_encode(payload_bytes)
    sig = hmac.new(
      self._get_signing_key(), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    token = f"{payload_b64}.{_b64u_encode(sig)}"
    return token, SessionInfo(username=username, issued_at=now, expires_at=exp)

  def verify_session_token(
    self, token: str, *, now_ts: int | None = None
  ) -> SessionInfo | None:
    if not self.enabled or not token or "." not in token:
      return None
    parts = token.split(".", 1)
    if len(parts) != 2:
      return None
    payload_b64, sig_b64 = parts
    if not payload_b64 or not sig_b64:
      return None

    expected_sig = _b64u_encode(
      hmac.new(
        self._get_signing_key(), payload_b64.encode("ascii"), hashlib.sha256
      ).digest()
    )
    if not hmac.compare_digest(sig_b64, expected_sig):
      return None

    try:
      data = json.loads(_b64u_decode(payload_b64).decode("utf-8"))
    except Exception:
      return None
    if not isinstance(data, dict):
      return None

    user = str(data.get("u") or "")
    iat = int(data.get("iat") or 0)
    exp = int(data.get("exp") or 0)
    ver = str(data.get("v") or "")

    now = int(time.time()) if now_ts is None else now_ts
    if exp <= now:
      return None
    if not hmac.compare_digest(user, self.config.username):
      return None
    if not hmac.compare_digest(ver, self._credential_fingerprint()):
      return None

    return SessionInfo(username=user, issued_at=iat, expires_at=exp)

  def extract_session_from_headers(
    self,
    headers: dict[str, str],
    *,
    now_ts: int | None = None,
  ) -> SessionInfo | None:
    raw_cookie = headers.get("cookie") or ""
    if not raw_cookie:
      return None
    cookie = SimpleCookie()
    try:
      cookie.load(raw_cookie)
    except Exception:
      return None
    morsel = cookie.get(SESSION_COOKIE_NAME)
    if morsel is None:
      return None
    return self.verify_session_token(morsel.value, now_ts=now_ts)

  def build_set_cookie_header(self, token: str) -> str:
    parts = [
      f"{SESSION_COOKIE_NAME}={token}",
      "Path=/",
      f"Max-Age={self.ttl_seconds}",
      "HttpOnly",
      "SameSite=Lax",
    ]
    if self.config.secure_cookie:
      parts.append("Secure")
    return "; ".join(parts)

  def build_clear_cookie_header(self) -> str:
    parts = [
      f"{SESSION_COOKIE_NAME}=",
      "Path=/",
      "Max-Age=0",
      "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
      "HttpOnly",
      "SameSite=Lax",
    ]
    if self.config.secure_cookie:
      parts.append("Secure")
    return "; ".join(parts)

  def login_redirect_location(self, target_url: str) -> str:
    safe = sanitize_next_url(target_url)
    if safe == "/":
      return "/login"
    return f"/login?next={quote(safe, safe='')}"


@dataclass(slots=True)
class LoginRateLimiter:
  """Bounded per-IP sliding-window failed-login rate limiter."""

  max_failures: int = 8
  window_sec: float = 60.0
  max_tracked_ips: int = 4096
  _failures: dict[str, list[float]] = field(default_factory=dict, repr=False)

  def _prune_ip(self, ip: str, cutoff: float) -> list[float]:
    timestamps = self._failures.get(ip)
    if not timestamps:
      self._failures.pop(ip, None)
      return []
    fresh = [ts for ts in timestamps if ts > cutoff]
    if fresh:
      self._failures[ip] = fresh
    else:
      self._failures.pop(ip, None)
    return fresh

  def check(self, ip: str, *, now: float | None = None) -> int:
    """Return `0` if allowed, or positive `Retry-After` seconds if throttled."""
    if self.max_failures <= 0 or self.window_sec <= 0:
      return 0
    ts_now = time.monotonic() if now is None else now
    fresh = self._prune_ip(ip or "unknown", ts_now - self.window_sec)
    if len(fresh) < self.max_failures:
      return 0
    oldest_active = fresh[0]
    remaining = (oldest_active + self.window_sec) - ts_now
    return max(1, ceil(remaining))

  def record_failure(self, ip: str, *, now: float | None = None) -> None:
    """Record a failed login attempt for `ip`."""
    if self.max_failures <= 0 or self.window_sec <= 0:
      return
    key = ip or "unknown"
    ts_now = time.monotonic() if now is None else now
    cutoff = ts_now - self.window_sec
    if (
      key not in self._failures and len(self._failures) >= self.max_tracked_ips
    ):
      for stale_ip in list(self._failures):
        self._prune_ip(stale_ip, cutoff)
      if len(self._failures) >= self.max_tracked_ips:
        oldest_key = next(iter(self._failures))
        self._failures.pop(oldest_key, None)
    fresh = self._prune_ip(key, cutoff)
    fresh.append(ts_now)
    self._failures[key] = fresh

  def record_success(self, ip: str) -> None:
    """Clear failed login attempts for `ip` after a successful sign-in."""
    self._failures.pop(ip or "unknown", None)


def render_login_page_html(
  *, next_url: str = "/", error_message: str = ""
) -> bytes:
  """Render a clean, standalone dark-themed login page matching the Web Guide UI."""
  safe_next = sanitize_next_url(next_url)
  esc_next = (
    safe_next.replace("&", "&amp;")
    .replace('"', "&quot;")
    .replace("'", "&#39;")
    .replace("<", "&lt;")
    .replace(">", "&gt;")
  )
  esc_err = (
    error_message.replace("&", "&amp;")
    .replace('"', "&quot;")
    .replace("'", "&#39;")
    .replace("<", "&lt;")
    .replace(">", "&gt;")
  )
  err_block = (
    f'<div id="login-error" class="login-error">{esc_err}</div>'
    if esc_err
    else '<div id="login-error" class="login-error" hidden></div>'
  )

  html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Sign In — Bellhop</title>
  <link rel="icon" type="image/svg+xml" href="/static/bellhop-logo.svg" />
  <link rel="alternate icon" href="/favicon.ico" />
  <script src="/static/prefs.js"></script>
  <style>
    :root {{
      --bg: #080d17;
      --bg-glow: #13213c;
      --surface: #0f172a;
      --surface-2: #162038;
      --border: rgba(148, 163, 184, 0.18);
      --text: #f1f5f9;
      --text-strong: #ffffff;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --accent-soft: #7dd3fc;
      --accent-strong: #0284c7;
      --card-shadow: 0 24px 64px rgba(0, 0, 0, 0.55);
      --focus-ring: rgba(56, 189, 248, 0.18);
      --danger-bg: rgba(248, 113, 113, 0.12);
      --danger-border: rgba(248, 113, 113, 0.35);
      --danger-text: #fecaca;
      --toggle-bg: rgba(15, 23, 42, 0.85);
      --toggle-hover: #1e293b;
      --toggle-shadow: 0 6px 20px rgba(0, 0, 0, 0.35);
    }}
    :root[data-theme="light"] {{
      --bg: #eef2f7;
      --bg-glow: #dff0fe;
      --surface: #ffffff;
      --surface-2: #f8fafc;
      --border: rgba(15, 23, 42, 0.13);
      --text: #0f172a;
      --text-strong: #0f172a;
      --muted: #64748b;
      --accent: #0284c7;
      --accent-soft: #0284c7;
      --accent-strong: #0369a1;
      --card-shadow: 0 20px 50px rgba(15, 23, 42, 0.11);
      --focus-ring: rgba(2, 132, 199, 0.16);
      --danger-bg: rgba(220, 38, 38, 0.08);
      --danger-border: rgba(220, 38, 38, 0.32);
      --danger-text: #b91c1c;
      --toggle-bg: rgba(255, 255, 255, 0.92);
      --toggle-hover: #f1f5f9;
      --toggle-shadow: 0 4px 14px rgba(15, 23, 42, 0.09);
    }}
    * {{ box-sizing: border-box; }}
    [hidden] {{ display: none !important; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: radial-gradient(circle at top, var(--bg-glow) 0%, var(--bg) 62%);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      padding: 20px;
      transition: background 0.18s ease, color 0.18s ease;
    }}
    .theme-toggle {{
      position: fixed;
      top: 18px;
      right: 18px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 38px;
      height: 38px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: var(--toggle-bg);
      color: var(--text);
      cursor: pointer;
      box-shadow: var(--toggle-shadow);
      backdrop-filter: blur(8px);
      transition: background 0.15s ease, border-color 0.15s ease, transform 0.1s ease;
    }}
    .theme-toggle:hover {{
      background: var(--toggle-hover);
      border-color: var(--accent);
    }}
    .theme-toggle:active {{ transform: translateY(1px); }}
    :root:not([data-theme="light"]) .theme-toggle .icon-moon {{ display: none; }}
    :root[data-theme="light"] .theme-toggle .icon-sun {{ display: none; }}
    .login-card {{
      width: 100%;
      max-width: 390px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 28px 26px 26px;
      box-shadow: var(--card-shadow);
    }}
    .brand {{
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      margin-bottom: 24px;
    }}
    .brand-logo {{
      display: block;
      width: 76px;
      height: 76px;
      margin-bottom: 14px;
      filter: drop-shadow(0 12px 26px rgba(14, 165, 233, 0.28));
      user-select: none;
    }}
    .brand h1 {{
      margin: 0;
      font-size: 1.48rem;
      font-weight: 800;
      letter-spacing: -0.025em;
      color: var(--text-strong);
    }}
    .brand h1 span {{
      font-weight: 500;
      color: var(--accent-soft);
    }}
    .brand p {{
      margin: 5px 0 0;
      font-size: 0.84rem;
      color: var(--muted);
    }}
    .field {{
      margin-bottom: 14px;
    }}
    label {{
      display: block;
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    input[type="text"],
    input[type="password"] {{
      width: 100%;
      padding: 10px 12px;
      border-radius: 9px;
      border: 1px solid var(--border);
      background: var(--surface-2);
      color: var(--text);
      font-size: 0.95rem;
      outline: none;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }}
    input:focus {{
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--focus-ring);
    }}
    .login-error {{
      margin-bottom: 14px;
      padding: 9px 12px;
      border-radius: 8px;
      background: var(--danger-bg);
      border: 1px solid var(--danger-border);
      color: var(--danger-text);
      font-size: 0.84rem;
    }}
    .submit-btn {{
      width: 100%;
      margin-top: 6px;
      padding: 11px 16px;
      border: none;
      border-radius: 9px;
      background: linear-gradient(135deg, #0ea5e9, #2563eb);
      color: #fff;
      font-size: 0.95rem;
      font-weight: 600;
      cursor: pointer;
      transition: opacity 0.15s ease, transform 0.1s ease;
    }}
    .submit-btn:hover {{ opacity: 0.94; }}
    .submit-btn:active {{ transform: translateY(1px); }}
    .submit-btn:disabled {{ opacity: 0.6; cursor: wait; }}
  </style>
</head>
<body>
  <button
    id="theme-toggle-btn"
    class="theme-toggle"
    type="button"
    aria-label="Switch to light mode"
    aria-pressed="false"
    title="Switch to light mode"
  >
    <svg class="icon-sun" viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4"></circle>
      <line x1="12" y1="2" x2="12" y2="4"></line>
      <line x1="12" y1="20" x2="12" y2="22"></line>
      <line x1="4.93" y1="4.93" x2="6.34" y2="6.34"></line>
      <line x1="17.66" y1="17.66" x2="19.07" y2="19.07"></line>
      <line x1="2" y1="12" x2="4" y2="12"></line>
      <line x1="20" y1="12" x2="22" y2="12"></line>
      <line x1="4.93" y1="19.07" x2="6.34" y2="17.66"></line>
      <line x1="17.66" y1="6.34" x2="19.07" y2="4.93"></line>
    </svg>
    <svg class="icon-moon" viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
    </svg>
  </button>
  <main class="login-card">
    <div class="brand">
      <img class="brand-logo" src="/static/bellhop-logo.svg" alt="Bellhop" width="76" height="76" />
      <h1>Bell<span>hop</span></h1>
      <p>Sign in to access your live TV guide &amp; player</p>
    </div>
    {err_block}
    <form id="login-form" method="post" action="/api/auth/login">
      <input type="hidden" name="next" value="{esc_next}" />
      <div class="field">
        <label for="username">Username</label>
        <input id="username" name="username" type="text" autocomplete="username" required autofocus />
      </div>
      <div class="field">
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required />
      </div>
      <button id="login-submit" class="submit-btn" type="submit">Sign In</button>
    </form>
  </main>
  <script src="/static/login.js"></script>
</body>
</html>
"""
  return html.encode("utf-8")
