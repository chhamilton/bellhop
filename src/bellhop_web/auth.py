"""Signed expiring session-cookie authentication for the Bellhop Web Guide (`bellhop serve`).

Uses Python standard-library primitives (`hmac`, `hashlib.sha256`, `secrets`, `http.cookies`)
implementing the same HMAC-SHA256 timed token design as `itsdangerous.URLSafeTimedSerializer`,
with zero extra runtime dependencies and persistent signing key support across restarts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from math import ceil
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from bellhop_web.file_utils import chmod_private_file, write_private_text

SESSION_COOKIE_NAME = "bellhop_session"


def _b64u_encode(raw: bytes) -> str:
  return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(token: str) -> bytes:
  pad = "=" * ((4 - len(token) % 4) % 4)
  return base64.urlsafe_b64decode((token + pad).encode("ascii"))


def _safe_compare_str(left: str, right: str) -> bool:
  return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def sanitize_next_url(raw_next: str | None) -> str:
  """Allow only safe internal relative paths (preventing open redirects)."""
  if not raw_next:
    return "/"
  cleaned = raw_next.strip()
  if not cleaned.startswith("/") or cleaned.startswith("//") or "\\" in cleaned:
    return "/"
  if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in cleaned):
    return "/"
  parsed = urlsplit(cleaned)
  if parsed.scheme or parsed.netloc:
    return "/"
  decoded_path = unquote(parsed.path)
  if (
    not decoded_path.startswith("/")
    or decoded_path.startswith("//")
    or "\\" in decoded_path
    or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in decoded_path)
  ):
    return "/"
  if decoded_path == "/login" or decoded_path.startswith("/login/"):
    return "/"
  return cleaned


@dataclass(slots=True)
class WebAuthConfig:
  """Configuration for `bellhop_session` HMAC-SHA256 cookie authentication."""

  username: str = ""
  password: str = ""
  session_ttl_hours: float = 168.0  # 7 days
  session_secret: str = ""
  secret_file: Path | None = None
  secure_cookie: bool = False


@dataclass(slots=True)
class SessionInfo:
  """Verified browser session extracted from a signed `bellhop_session` cookie."""

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

  def __init__(
    self,
    config: WebAuthConfig,
    *,
    clock: Callable[[], float] = time.time,
  ) -> None:
    self.config = config
    self._clock = clock
    self._key: bytes | None = None

  @property
  def has_credentials(self) -> bool:
    """Return `True` when both login username and password are non-empty."""
    return bool(self.config.username.strip() and self.config.password.strip())

  @property
  def enabled(self) -> bool:
    """Login is mandatory for the Web Guide and cannot be disabled."""
    return True

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
          chmod_private_file(secret_file)
          stored = secret_file.read_text(encoding="utf-8").strip()
          if len(stored) >= 32:
            self._key = hashlib.sha256(stored.encode("utf-8")).digest()
            return self._key
        generated = secrets.token_hex(32)
        write_private_text(secret_file, generated + "\n")
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
    if not self.has_credentials:
      return False
    return _safe_compare_str(username, self.config.username) and (
      _safe_compare_str(password, self.config.password)
    )

  def create_session_token(
    self, username: str, *, now_ts: int | None = None
  ) -> tuple[str, SessionInfo]:
    now = int(self._clock()) if now_ts is None else now_ts
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
    if not self.has_credentials or not token or "." not in token:
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
    if not _safe_compare_str(sig_b64, expected_sig):
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

    now = int(self._clock()) if now_ts is None else now_ts
    if exp <= now:
      return None
    if not _safe_compare_str(user, self.config.username):
      return None
    if not _safe_compare_str(ver, self._credential_fingerprint()):
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
  clock: Callable[[], float] = field(default=time.monotonic, repr=False)
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
    ts_now = self.clock() if now is None else now
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
    ts_now = self.clock() if now is None else now
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
  <link rel="stylesheet" href="/static/styles.css" />
</head>
<body class="login-page">
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
