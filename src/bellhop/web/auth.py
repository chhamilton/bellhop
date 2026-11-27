"""Signed expiring session-cookie auth for the Bellhop Web Guide.

Uses Python standard-library primitives (`hmac`, `hashlib.sha256`, `secrets`,
`http.cookies`)
implementing the same HMAC-SHA256 timed token design as
`itsdangerous.URLSafeTimedSerializer`,
with zero extra runtime dependencies and persistent signing key support across
restarts.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from bellhop.common.accounts import (
  UserAccount,
  normalize_email,
  password_hash_version,
  verify_dummy_web_password,
  verify_web_password,
)
from bellhop.common.file_utils import chmod_private_file, write_private_text
from bellhop.common.http_utils import (
  LoginRateLimiter as LoginRateLimiter,
)

SESSION_COOKIE_NAME = "bellhop_session"
PASSWORD_RESET_TTL_SEC: int = 900
GENERIC_PASSWORD_RESET_MESSAGE: str = (
  "If an account with that email exists, we've sent a password reset link."
)


def _b64u_encode(raw: bytes) -> str:
  return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(token: str) -> bytes:
  pad = "=" * ((4 - len(token) % 4) % 4)
  return base64.urlsafe_b64decode((token + pad).encode("ascii"))


def _safe_compare_str(left: str, right: str) -> bool:
  left_dig = hashlib.sha256(left.encode("utf-8")).digest()
  right_dig = hashlib.sha256(right.encode("utf-8")).digest()
  return hmac.compare_digest(left_dig, right_dig)


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
  if (
    decoded_path == "/login"
    or decoded_path.startswith("/login/")
    or decoded_path == "/reset-password"
    or decoded_path.startswith("/reset-password/")
  ):
    return "/"
  return cleaned


@dataclass(slots=True)
class WebAuthConfig:
  """Configuration for `bellhop_session` HMAC-SHA256 cookie authentication."""

  accounts: Sequence[UserAccount] = ()
  accounts_provider: Callable[[], Sequence[UserAccount]] | None = None
  session_ttl_hours: float = 168.0  # 7 days
  secret_file: Path | None = None
  revocation_file: Path | None = None
  secure_cookie: bool = False


@dataclass(slots=True)
class SessionInfo:
  """Verified browser session extracted from a signed cookie."""

  account_id: str
  email: str
  issued_at: int
  expires_at: int
  jti: str = ""
  pwd_ver: str = ""

  def needs_renewal(self, *, now_ts: int | None = None) -> bool:
    """Sliding expiry: renew cookie once past half its total TTL."""
    now = int(time.time()) if now_ts is None else now_ts
    ttl = max(1, self.expires_at - self.issued_at)
    return (now - self.issued_at) >= (ttl // 2)


@dataclass(slots=True)
class PasswordResetTokenInfo:
  """Verified password-reset token extracted from an emailed reset link."""

  account_id: str
  email: str
  issued_at: int
  expires_at: int
  jti: str = ""
  pwd_ver: str = ""


class WebAuthManager:
  """Manages Web Guide login verification and HMAC-SHA256 session cookies."""

  def __init__(
    self,
    config: WebAuthConfig,
    *,
    clock: Callable[[], float] = time.time,
  ) -> None:
    self.config = config
    self._clock = clock
    self._key: bytes | None = None
    self._secret_sig: tuple[int, int, int] | None = None
    self._revoked_tokens: dict[str, tuple[int, int]] = {}
    self._revoked_sig: tuple[int, int] | None = None
    self._max_revoked = 4096

  @property
  def revocation_path(self) -> Path | None:
    """Path persisting revoked session `jti` entries across restarts."""
    if self.config.revocation_file is not None:
      return self.config.revocation_file
    if self.config.secret_file is not None:
      return self.config.secret_file.with_name("session_revoked.json")
    return None

  @staticmethod
  def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

  def _prune_revoked(self, now: int) -> None:
    for rev_key, (_rev_after, exp) in list(self._revoked_tokens.items()):
      if exp <= now:
        self._revoked_tokens.pop(rev_key, None)
    while len(self._revoked_tokens) >= self._max_revoked:
      oldest = next(iter(self._revoked_tokens))
      self._revoked_tokens.pop(oldest, None)

  def _sync_revoked_from_disk(self, now: int) -> None:
    path = self.revocation_path
    if path is None:
      return
    try:
      st = path.stat()
    except OSError:
      return
    sig = (int(st.st_mtime_ns), int(st.st_size))
    if self._revoked_sig == sig:
      return
    try:
      chmod_private_file(path)
      raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
      self._revoked_sig = sig
      return
    entries = raw.get("revoked") if isinstance(raw, dict) else None
    if isinstance(entries, dict):
      for rev_key, vals in entries.items():
        if not isinstance(rev_key, str) or not rev_key:
          continue
        if isinstance(vals, (list, tuple)) and len(vals) == 2:
          rev_after = int(vals[0])
          exp = int(vals[1])
        elif isinstance(vals, (int, float)):
          rev_after = 0
          exp = int(vals)
        else:
          continue
        if exp <= now:
          continue
        prev = self._revoked_tokens.get(rev_key)
        if prev is None or rev_after < prev[0] or exp > prev[1]:
          self._revoked_tokens[rev_key] = (
            min(prev[0], rev_after) if prev else rev_after,
            max(prev[1], exp) if prev else exp,
          )
    self._prune_revoked(now)
    self._revoked_sig = sig

  def _save_revocations(self) -> None:
    path = self.revocation_path
    if path is None:
      return
    payload = {
      "v": 1,
      "revoked": {
        k: [rev_after, exp]
        for k, (rev_after, exp) in self._revoked_tokens.items()
      },
    }
    with contextlib.suppress(OSError):
      write_private_text(
        path, json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
      )
      st = path.stat()
      self._revoked_sig = (int(st.st_mtime_ns), int(st.st_size))

  def accounts(self) -> tuple[UserAccount, ...]:
    """Return current configured user accounts."""
    if self.config.accounts_provider is not None:
      return tuple(self.config.accounts_provider())
    return tuple(self.config.accounts)

  def account_by_id(self, account_id: str) -> UserAccount | None:
    """Look up an active user account by immutable `account_id`."""
    cleaned = account_id.strip()
    for acct in self.accounts():
      if acct.account_id == cleaned:
        return acct
    return None

  def account_by_email(self, email: str) -> UserAccount | None:
    """Look up an active account by normalized email in constant time."""
    norm_email = normalize_email(email)
    if not norm_email:
      return None
    matched: UserAccount | None = None
    for acct in self.accounts():
      if _safe_compare_str(acct.email, norm_email):
        matched = acct
    return matched

  @property
  def has_credentials(self) -> bool:
    """Return `True` when at least one authenticated user account exists."""
    return bool(self.accounts())

  @property
  def enabled(self) -> bool:
    """Return `True` when account-backed Web Guide login is active."""
    return self.has_credentials

  @property
  def ttl_seconds(self) -> int:
    return max(60, int(round(self.config.session_ttl_hours * 3600.0)))

  def _get_signing_key(self) -> bytes:
    secret_file = self.config.secret_file
    if secret_file is not None:
      try:
        st = secret_file.stat()
      except OSError:
        st = None

      if st is not None:
        sig = (int(st.st_ino), int(st.st_mtime_ns), int(st.st_size))
        if self._key is not None and self._secret_sig == sig:
          return self._key
        try:
          chmod_private_file(secret_file)
          stored = secret_file.read_text(encoding="utf-8").strip()
          if len(stored) >= 32:
            self._key = hashlib.sha256(stored.encode("utf-8")).digest()
            st_after = secret_file.stat()
            self._secret_sig = (
              int(st_after.st_ino),
              int(st_after.st_mtime_ns),
              int(st_after.st_size),
            )
            return self._key
        except OSError:
          pass

      try:
        generated = secrets.token_hex(32)
        write_private_text(secret_file, generated + "\n")
        st_new = secret_file.stat()
        self._secret_sig = (
          int(st_new.st_ino),
          int(st_new.st_mtime_ns),
          int(st_new.st_size),
        )
        self._key = hashlib.sha256(generated.encode("utf-8")).digest()
        return self._key
      except OSError:
        self._secret_sig = None

    if self._key is None:
      self._key = secrets.token_bytes(32)
    return self._key

  def authenticate(self, email: str, password: str) -> UserAccount | None:
    """Authenticate an email + password against configured accounts."""
    norm_email = normalize_email(email)
    accts = self.accounts()
    if not accts or not norm_email or not password:
      if password:
        verify_dummy_web_password(password)
      return None

    candidate: UserAccount | None = None
    for acct in accts:
      if _safe_compare_str(acct.email, norm_email):
        candidate = acct

    if candidate is None:
      verify_dummy_web_password(password)
      return None
    if verify_web_password(password, candidate.web_password_hash):
      return candidate
    return None

  def _sign_payload(self, payload: Mapping[str, object]) -> str:
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64u_encode(payload_bytes)
    sig = hmac.new(
      self._get_signing_key(), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{payload_b64}.{_b64u_encode(sig)}"

  def _verify_signed_payload(
    self,
    token: str,
    *,
    expected_types: tuple[str | None, ...],
    now_ts: int | None = None,
  ) -> tuple[UserAccount, int, int, str, str] | None:
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
    if not isinstance(data, dict) or data.get("v") != 2:
      return None
    raw_typ = data.get("typ")
    typ = str(raw_typ) if raw_typ is not None else None
    if typ not in expected_types:
      return None

    try:
      aid = str(data.get("aid") or "").strip()
      eml = str(data.get("eml") or "").strip().lower()
      iat = int(data.get("iat") or 0)
      exp = int(data.get("exp") or 0)
      pv = str(data.get("pv") or "")
      jti = str(data.get("jti") or "")
    except (TypeError, ValueError):
      return None
    if not aid or not eml:
      return None

    now = int(self._clock()) if now_ts is None else now_ts
    if exp <= now:
      return None
    self._sync_revoked_from_disk(now)
    if self._revoked_tokens:
      self._prune_revoked(now)
      for key in (jti, self._token_digest(token)):
        if key:
          rec = self._revoked_tokens.get(key)
          if rec is not None and now >= rec[0]:
            return None

    acct = self.account_by_id(aid)
    if acct is None:
      return None
    if not _safe_compare_str(eml, acct.email):
      return None
    if not _safe_compare_str(pv, password_hash_version(acct.web_password_hash)):
      return None
    return acct, iat, exp, jti, pv

  def _record_revocation(
    self, rev_key: str, *, now: int, rev_after: int, expires_at: int
  ) -> None:
    self._prune_revoked(now)
    prev = self._revoked_tokens.get(rev_key)
    self._revoked_tokens[rev_key] = (
      min(prev[0], rev_after) if prev is not None else rev_after,
      max(prev[1], expires_at) if prev is not None else expires_at,
    )
    self._save_revocations()

  def create_session_token(
    self, account: UserAccount, *, now_ts: int | None = None
  ) -> tuple[str, SessionInfo]:
    """Mint a signed expiring `bellhop_session` cookie token."""
    now = int(self._clock()) if now_ts is None else now_ts
    exp = now + self.ttl_seconds
    jti = secrets.token_hex(8)
    pwd_ver = password_hash_version(account.web_password_hash)
    token = self._sign_payload(
      {
        "v": 2,
        "typ": "session",
        "aid": account.account_id,
        "eml": account.email,
        "iat": now,
        "exp": exp,
        "pv": pwd_ver,
        "jti": jti,
      }
    )
    return token, SessionInfo(
      account_id=account.account_id,
      email=account.email,
      issued_at=now,
      expires_at=exp,
      jti=jti,
      pwd_ver=pwd_ver,
    )

  def verify_session_token(
    self, token: str, *, now_ts: int | None = None
  ) -> SessionInfo | None:
    """Verify a signed `bellhop_session` cookie token."""
    verified = self._verify_signed_payload(
      token, expected_types=(None, "session"), now_ts=now_ts
    )
    if verified is None:
      return None
    acct, iat, exp, jti, pv = verified
    return SessionInfo(
      account_id=acct.account_id,
      email=acct.email,
      issued_at=iat,
      expires_at=exp,
      jti=jti,
      pwd_ver=pv,
    )

  def create_password_reset_token(
    self,
    account: UserAccount,
    *,
    ttl_sec: int = PASSWORD_RESET_TTL_SEC,
    now_ts: int | None = None,
  ) -> tuple[str, PasswordResetTokenInfo]:
    """Mint a short-lived signed single-use password-reset token."""
    now = int(self._clock()) if now_ts is None else now_ts
    exp = now + max(1, int(ttl_sec))
    jti = secrets.token_hex(12)
    pwd_ver = password_hash_version(account.web_password_hash)
    token = self._sign_payload(
      {
        "v": 2,
        "typ": "pwd_reset",
        "aid": account.account_id,
        "eml": account.email,
        "iat": now,
        "exp": exp,
        "pv": pwd_ver,
        "jti": jti,
      }
    )
    return token, PasswordResetTokenInfo(
      account_id=account.account_id,
      email=account.email,
      issued_at=now,
      expires_at=exp,
      jti=jti,
      pwd_ver=pwd_ver,
    )

  def run_dummy_reset_token_work(self) -> None:
    """Perform equivalent signing CPU work when no reset email is sent."""
    dummy_ver = password_hash_version("scrypt$0$0$0")
    dummy_sig = self._sign_payload(
      {
        "v": 2,
        "typ": "pwd_reset",
        "aid": "dummy",
        "eml": "dummy@example.invalid",
        "iat": 0,
        "exp": PASSWORD_RESET_TTL_SEC,
        "pv": dummy_ver,
        "jti": secrets.token_hex(12),
      }
    )
    _safe_compare_str(dummy_sig, dummy_sig)

  def verify_password_reset_token(
    self, token: str, *, now_ts: int | None = None
  ) -> PasswordResetTokenInfo | None:
    """Verify a password-reset token without consuming or revoking it."""
    verified = self._verify_signed_payload(
      token, expected_types=("pwd_reset",), now_ts=now_ts
    )
    if verified is None:
      return None
    acct, iat, exp, jti, pv = verified
    return PasswordResetTokenInfo(
      account_id=acct.account_id,
      email=acct.email,
      issued_at=iat,
      expires_at=exp,
      jti=jti,
      pwd_ver=pv,
    )

  def revoke_reset_token_info(
    self,
    info: PasswordResetTokenInfo,
    *,
    raw_token: str = "",
    now_ts: int | None = None,
  ) -> None:
    """Persist revocation for an already-verified reset token's `jti`."""
    now = int(self._clock()) if now_ts is None else now_ts
    rev_key = info.jti or (self._token_digest(raw_token) if raw_token else "")
    if not rev_key:
      return
    self._record_revocation(
      rev_key, now=now, rev_after=now, expires_at=info.expires_at
    )

  def revoke_token(
    self, token: str, *, now_ts: int | None = None, grace_sec: int = 0
  ) -> None:
    """Revoke `token` server-side until its natural expiration timestamp."""
    if not token:
      return
    now = int(self._clock()) if now_ts is None else now_ts
    info = self.verify_session_token(token, now_ts=now)
    if info is None:
      return
    rev_key = info.jti or self._token_digest(token)
    self._record_revocation(
      rev_key,
      now=now,
      rev_after=now + max(0, int(grace_sec)),
      expires_at=info.expires_at,
    )

  def revoke_session_from_headers(
    self,
    headers: Mapping[str, str],
    *,
    now_ts: int | None = None,
    grace_sec: int = 0,
  ) -> None:
    """Extract and revoke any `bellhop_session` cookie present in `headers`."""
    raw_cookie = headers.get("cookie") or headers.get("Cookie") or ""
    if not raw_cookie:
      return
    cookie = SimpleCookie()
    try:
      cookie.load(raw_cookie)
    except Exception:
      return
    morsel = cookie.get(SESSION_COOKIE_NAME)
    if morsel is not None and morsel.value:
      self.revoke_token(morsel.value, now_ts=now_ts, grace_sec=grace_sec)

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

  def build_set_cookie_header(
    self, token: str, *, secure: bool | None = None
  ) -> str:
    parts = [
      f"{SESSION_COOKIE_NAME}={token}",
      "Path=/",
      f"Max-Age={self.ttl_seconds}",
      "HttpOnly",
      "SameSite=Lax",
    ]
    if self.config.secure_cookie or bool(secure):
      parts.append("Secure")
    return "; ".join(parts)

  def build_clear_cookie_header(self, *, secure: bool | None = None) -> str:
    parts = [
      f"{SESSION_COOKIE_NAME}=",
      "Path=/",
      "Max-Age=0",
      "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
      "HttpOnly",
      "SameSite=Lax",
    ]
    if self.config.secure_cookie or bool(secure):
      parts.append("Secure")
    return "; ".join(parts)

  def login_redirect_location(self, target_url: str) -> str:
    safe = sanitize_next_url(target_url)
    if safe == "/":
      return "/login"
    return f"/login?next={quote(safe, safe='')}"


def _escape_html(raw: str) -> str:
  return (
    raw.replace("&", "&amp;")
    .replace('"', "&quot;")
    .replace("'", "&#39;")
    .replace("<", "&lt;")
    .replace(">", "&gt;")
  )


def _render_auth_page_shell(
  *,
  title: str,
  subtitle: str,
  body_html: str,
) -> bytes:
  esc_title = _escape_html(title)
  esc_subtitle = _escape_html(subtitle)
  html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{esc_title}</title>
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
      <p id="login-subtitle">{esc_subtitle}</p>
    </div>
    {body_html}
  </main>
  <script src="/static/login.js"></script>
</body>
</html>
"""
  return html.encode("utf-8")


def render_login_page_html(
  *,
  next_url: str = "/",
  error_message: str = "",
  password_reset_enabled: bool = False,
) -> bytes:
  """Render a standalone themed login page matching the Web Guide."""
  esc_next = _escape_html(sanitize_next_url(next_url))
  esc_err = _escape_html(error_message)
  err_block = (
    f'<div id="login-error" class="login-error">{esc_err}</div>'
    if esc_err
    else '<div id="login-error" class="login-error" hidden></div>'
  )
  forgot_trigger = (
    '<div class="login-aux-row">'
    '<button id="forgot-password-link" class="login-link-btn" type="button">'
    "Forgot password?</button></div>"
    if password_reset_enabled
    else ""
  )
  forgot_form = (
    """
    <form id="forgot-password-form" method="post" action="/api/auth/forgot-password" hidden>
      <div class="field">
        <label for="forgot-email">Email</label>
        <input id="forgot-email" name="email" type="email" autocomplete="email" required />
      </div>
      <button id="forgot-submit" class="submit-btn" type="submit">Send Reset Link</button>
      <div class="login-aux-row">
        <button id="back-to-login-link" class="login-link-btn" type="button">Back to sign in</button>
      </div>
    </form>"""
    if password_reset_enabled
    else ""
  )
  body_html = f"""{err_block}
    <div id="login-notice" class="login-notice" hidden></div>
    <form id="login-form" method="post" action="/api/auth/login">
      <input type="hidden" name="next" value="{esc_next}" />
      <div class="field">
        <label for="email">Email</label>
        <input id="email" name="email" type="email" autocomplete="username" required autofocus />
      </div>
      <div class="field">
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required />
      </div>
      <button id="login-submit" class="submit-btn" type="submit">Sign In</button>
      {forgot_trigger}
    </form>{forgot_form}"""
  return _render_auth_page_shell(
    title="Sign In — Bellhop",
    subtitle="Sign in to access your live TV guide & player",
    body_html=body_html,
  )


def render_reset_password_page_html(
  *,
  token: str = "",
  email: str = "",
  error_message: str = "",
) -> bytes:
  """Render the password-reset form page (`GET /reset-password`)."""
  esc_err = _escape_html(error_message)
  err_block = (
    f'<div id="login-error" class="login-error">{esc_err}</div>'
    if esc_err
    else '<div id="login-error" class="login-error" hidden></div>'
  )
  if token and not error_message:
    esc_token = _escape_html(token)
    esc_email = _escape_html(email)
    form_block = f"""
    <form id="reset-password-form" method="post" action="/api/auth/reset-password">
      <input type="hidden" id="reset-token" name="token" value="{esc_token}" />
      <input id="reset-username" class="sr-only" type="email" name="username" value="{esc_email}" autocomplete="username" readonly tabindex="-1" aria-hidden="true" />
      <div class="field">
        <label for="reset-new-password">New Password</label>
        <input id="reset-new-password" name="new_password" type="password" autocomplete="new-password" minlength="8" maxlength="256" required autofocus />
      </div>
      <div class="field">
        <label for="reset-confirm-password">Confirm New Password</label>
        <input id="reset-confirm-password" name="confirm_password" type="password" autocomplete="new-password" minlength="8" maxlength="256" required />
      </div>
      <button id="reset-submit" class="submit-btn" type="submit">Reset Password</button>
      <div class="login-aux-row">
        <a class="login-link-btn" href="/login">Back to sign in</a>
      </div>
    </form>"""
  else:
    form_block = """
    <div class="login-aux-row">
      <a class="login-link-btn" href="/login">Back to sign in</a>
    </div>"""
  body_html = f"""{err_block}
    <div id="login-notice" class="login-notice" hidden></div>{form_block}"""
  return _render_auth_page_shell(
    title="Reset Password — Bellhop",
    subtitle="Choose a new password for your Bellhop account",
    body_html=body_html,
  )
