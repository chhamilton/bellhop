"""Generic stdlib SMTP client and config model for Bellhop outbound email.

Supports Google Workspace / Gmail App Passwords (`smtp.gmail.com:587` STARTTLS
or `:465` SSL) as well as any standard SMTP relay while keeping `bellhop.common`
free of third-party network dependencies.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Protocol

from bellhop.common.accounts import validate_email

__all__ = [
  "DEFAULT_SMTP_HOST",
  "DEFAULT_SMTP_PORT",
  "DEFAULT_SMTP_TIMEOUT_SEC",
  "VALID_TLS_MODES",
  "EmailSenderProtocol",
  "MailerAuthError",
  "MailerConfigError",
  "MailerError",
  "SentEmailResult",
  "SmtpConfig",
  "SmtpMailer",
  "SmtpVerifyResult",
  "build_email_message",
  "normalize_smtp_password",
]

DEFAULT_SMTP_HOST: str = "smtp.gmail.com"
DEFAULT_SMTP_PORT: int = 587
DEFAULT_SMTP_TIMEOUT_SEC: float = 15.0
VALID_TLS_MODES: frozenset[str] = frozenset({"starttls", "ssl", "none"})

_GOOGLE_APP_PW_RE = re.compile(r"^[A-Za-z0-9]{4}(?:\s+[A-Za-z0-9]{4}){3}$")


class MailerError(RuntimeError):
  """Base error raised for outbound SMTP configuration or transport failures."""


class MailerConfigError(MailerError):
  """Raised when outbound email configuration or headers are invalid."""


class MailerAuthError(MailerError):
  """Raised when SMTP authentication fails."""


def normalize_smtp_password(password: str, *, smtp_host: str) -> str:
  """Strip Google App Password spaces for Gmail/Workspace SMTP hosts.

  Google Workspace and Gmail display 16-character App Passwords in four
  4-character groups (`abcd efgh ijkl mnop`), whereas SMTP `AUTH` expects the
  16 characters without spaces. Passwords for non-Google hosts and other
  formats are returned unchanged.
  """
  host_low = smtp_host.strip().rstrip(".").lower()
  is_google_host = host_low in {
    "gmail.com",
    "googlemail.com",
  } or host_low.endswith((".gmail.com", ".googlemail.com"))
  stripped = password.strip()
  if is_google_host and _GOOGLE_APP_PW_RE.fullmatch(stripped):
    return "".join(stripped.split())
  return password


def _is_loopback_host(host: str) -> bool:
  cleaned = host.strip().rstrip(".").lower()
  if cleaned.startswith("[") and cleaned.endswith("]"):
    cleaned = cleaned[1:-1]
  if cleaned in {"localhost", "localhost.localdomain"} or cleaned.endswith(
    ".localhost"
  ):
    return True
  try:
    addr = ipaddress.ip_address(cleaned)
  except ValueError:
    return False
  return addr.is_loopback


def _sanitize_header_text(value: str, *, field_name: str) -> str:
  if "\r" in value or "\n" in value or "\x00" in value:
    raise MailerConfigError(
      f"{field_name} must not contain newline or NUL characters."
    )
  return value.strip()


@dataclass(slots=True)
class SmtpConfig:
  """Outbound SMTP transport and Google Workspace App Password settings."""

  enabled: bool = False
  smtp_host: str = DEFAULT_SMTP_HOST
  smtp_port: int = DEFAULT_SMTP_PORT
  tls_mode: str = "starttls"
  username: str = ""
  password: str = ""
  from_name: str = "Bellhop"
  from_address: str = ""
  timeout_sec: float = DEFAULT_SMTP_TIMEOUT_SEC

  @property
  def effective_from_address(self) -> str:
    """Return explicit `from_address` or fall back to SMTP `username`."""
    return self.from_address.strip() or self.username.strip()

  @property
  def effective_password(self) -> str:
    """Return normalized SMTP password or App-Specific Password."""
    return normalize_smtp_password(self.password, smtp_host=self.smtp_host)

  @property
  def has_credentials(self) -> bool:
    """Return `True` when both SMTP username and password are present."""
    return bool(self.username.strip() and self.effective_password)

  @property
  def is_configured(self) -> bool:
    """Return `True` when email is enabled with host and sender address."""
    return bool(
      self.enabled and self.smtp_host.strip() and self.effective_from_address
    )

  def validate(self, *, require_enabled: bool = True) -> None:
    """Validate SMTP host, port, TLS mode, sender email, and credentials."""
    if require_enabled and not self.enabled:
      raise MailerConfigError(
        "Outbound email is disabled (`email.enabled: false` in config.yaml)."
      )
    host = _sanitize_header_text(self.smtp_host, field_name="email.smtp_host")
    if not host:
      raise MailerConfigError("`email.smtp_host` must not be empty.")
    if isinstance(self.smtp_port, bool):
      raise MailerConfigError("`email.smtp_port` must be an integer port.")
    try:
      port = int(self.smtp_port)
    except (TypeError, ValueError) as exc:
      raise MailerConfigError(
        "`email.smtp_port` must be an integer between 1 and 65535."
      ) from exc
    if port < 1 or port > 65535:
      raise MailerConfigError(
        "`email.smtp_port` must be an integer between 1 and 65535."
      )
    mode = self.tls_mode.strip().lower()
    if mode not in VALID_TLS_MODES:
      allowed = ", ".join(sorted(VALID_TLS_MODES))
      raise MailerConfigError(
        f"`email.tls_mode` must be one of {allowed} (got {self.tls_mode!r})."
      )
    try:
      timeout = float(self.timeout_sec)
    except (TypeError, ValueError) as exc:
      raise MailerConfigError(
        "`email.timeout_sec` must be a positive number."
      ) from exc
    if timeout <= 0:
      raise MailerConfigError("`email.timeout_sec` must be positive.")

    _sanitize_header_text(self.from_name, field_name="email.from_name")
    username = _sanitize_header_text(self.username, field_name="email.username")
    password = self.effective_password
    if bool(username) != bool(password):
      raise MailerConfigError(
        "Configure both `email.username` and `email.password` in "
        "credentials.yaml (or leave both empty for unauthenticated local relay)."
      )

    raw_from = _sanitize_header_text(
      self.effective_from_address, field_name="email.from_address"
    )
    if not raw_from:
      raise MailerConfigError(
        "Configure `email.from_address` in config.yaml or `email.username` "
        "in credentials.yaml."
      )
    try:
      validate_email(raw_from, account_id="email.from_address")
    except ValueError as exc:
      raise MailerConfigError(str(exc)) from exc

    if mode == "none" and self.has_credentials and not _is_loopback_host(host):
      raise MailerConfigError(
        "Refusing `email.tls_mode: none` with SMTP credentials on non-loopback "
        f"host {host!r}; use `starttls` or `ssl`."
      )


@dataclass(slots=True, frozen=True)
class SmtpVerifyResult:
  """Diagnostic result from non-sending SMTP TLS + AUTH verification."""

  smtp_host: str
  smtp_port: int
  tls_mode: str
  authenticated_user: str
  from_address: str
  noop_code: int


@dataclass(slots=True, frozen=True)
class SentEmailResult:
  """Confirmation metadata returned after SMTP delivery."""

  message_id: str
  from_header: str
  to_address: str
  subject: str
  smtp_host: str
  smtp_port: int
  tls_mode: str


class EmailSenderProtocol(Protocol):
  """Structural protocol for outbound email senders (`SmtpMailer`, fakes)."""

  async def send(
    self,
    *,
    to_address: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
  ) -> SentEmailResult: ...


def build_email_message(
  *,
  from_address: str,
  to_address: str,
  subject: str,
  text_body: str,
  from_name: str = "",
  html_body: str | None = None,
) -> EmailMessage:
  """Construct a validated MIME `EmailMessage` (plain text + optional HTML)."""
  clean_from_raw = _sanitize_header_text(
    from_address, field_name="email.from_address"
  )
  clean_to_raw = _sanitize_header_text(to_address, field_name="to_address")
  try:
    clean_from = validate_email(clean_from_raw, account_id="email.from_address")
    clean_to = validate_email(clean_to_raw, account_id="to_address")
  except ValueError as exc:
    raise MailerConfigError(str(exc)) from exc

  clean_subject = _sanitize_header_text(subject, field_name="subject")
  if not clean_subject:
    raise MailerConfigError("Email subject must not be empty.")
  clean_name = _sanitize_header_text(from_name, field_name="email.from_name")
  if not text_body or not text_body.strip():
    raise MailerConfigError("Email plain-text body must not be empty.")

  from_header = (
    formataddr((clean_name, clean_from)) if clean_name else clean_from
  )
  domain = clean_from.rsplit("@", 1)[-1] or "bellhop.local"
  message_id = make_msgid(domain=domain)

  msg = EmailMessage()
  msg["From"] = from_header
  msg["To"] = clean_to
  msg["Subject"] = clean_subject
  msg["Date"] = formatdate(localtime=True)
  msg["Message-ID"] = message_id
  msg["Auto-Submitted"] = "auto-generated"
  msg.set_content(text_body, subtype="plain", charset="utf-8")
  if html_body and html_body.strip():
    msg.add_alternative(html_body, subtype="html", charset="utf-8")
  return msg


class _SmtpClientProtocol(Protocol):
  """Minimal structural subset of `smtplib.SMTP` used by `SmtpMailer`."""

  def ehlo(self) -> tuple[int, bytes]: ...

  def has_extn(self, opt: str) -> bool: ...

  def starttls(
    self, *, context: ssl.SSLContext | None = None
  ) -> tuple[int, bytes]: ...

  def login(self, user: str, password: str) -> tuple[int, bytes]: ...

  def noop(self) -> tuple[int, bytes]: ...

  def send_message(self, msg: EmailMessage) -> dict[str, tuple[int, bytes]]: ...

  def quit(self) -> tuple[int, bytes]: ...

  def close(self) -> None: ...


class SmtpMailer:
  """Async-friendly wrapper over `smtplib` for transactional Bellhop emails."""

  def __init__(
    self,
    config: SmtpConfig,
    *,
    ssl_context: ssl.SSLContext | None = None,
    smtp_client_factory: Callable[..., _SmtpClientProtocol] | None = None,
    smtp_ssl_client_factory: Callable[..., _SmtpClientProtocol] | None = None,
  ) -> None:
    self.config = config
    self._ssl_context = ssl_context or ssl.create_default_context()
    self._smtp_client_factory = smtp_client_factory or smtplib.SMTP
    self._smtp_ssl_client_factory = smtp_ssl_client_factory or smtplib.SMTP_SSL

  def _scrub_error(self, text: str) -> str:
    cleaned = text
    for secret in {self.config.password, self.config.effective_password}:
      if secret:
        cleaned = cleaned.replace(secret, "***")
    return cleaned

  def _open_and_authenticate_sync(self) -> _SmtpClientProtocol:
    self.config.validate(require_enabled=True)
    host = self.config.smtp_host.strip()
    port = int(self.config.smtp_port)
    mode = self.config.tls_mode.strip().lower()
    timeout = max(1.0, min(120.0, float(self.config.timeout_sec)))

    client: _SmtpClientProtocol | None = None
    try:
      if mode == "ssl":
        client = self._smtp_ssl_client_factory(
          host, port, timeout=timeout, context=self._ssl_context
        )
      else:
        client = self._smtp_client_factory(host, port, timeout=timeout)

      client.ehlo()
      if mode == "starttls":
        if not client.has_extn("starttls"):
          raise MailerError(
            f"SMTP server {host}:{port} does not advertise STARTTLS."
          )
        client.starttls(context=self._ssl_context)
        client.ehlo()

      if self.config.has_credentials:
        client.login(
          self.config.username.strip(), self.config.effective_password
        )
      return client
    except MailerError:
      if client is not None:
        with contextlib.suppress(Exception):
          client.close()
      raise
    except smtplib.SMTPAuthenticationError as exc:
      if client is not None:
        with contextlib.suppress(Exception):
          client.close()
      raise MailerAuthError(
        f"SMTP authentication failed for {host}:{port}: "
        f"{self._scrub_error(str(exc))}"
      ) from exc
    except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
      if client is not None:
        with contextlib.suppress(Exception):
          client.close()
      raise MailerError(
        f"SMTP connection/delivery error on {host}:{port}: "
        f"{self._scrub_error(str(exc))}"
      ) from exc

  @staticmethod
  def _close_quietly(client: _SmtpClientProtocol) -> None:
    try:
      client.quit()
    except Exception:
      with contextlib.suppress(Exception):
        client.close()

  def verify_sync(self) -> SmtpVerifyResult:
    """Verify SMTP connection, TLS, and credentials without sending mail."""
    client = self._open_and_authenticate_sync()
    try:
      noop_code, noop_msg = client.noop()
      if noop_code >= 400:
        raise MailerError(
          f"SMTP server rejected NOOP probe ({noop_code}): "
          f"{noop_msg.decode('utf-8', errors='replace')}"
        )
    except MailerError:
      raise
    except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
      raise MailerError(
        f"SMTP verification error: {self._scrub_error(str(exc))}"
      ) from exc
    finally:
      self._close_quietly(client)

    return SmtpVerifyResult(
      smtp_host=self.config.smtp_host.strip(),
      smtp_port=int(self.config.smtp_port),
      tls_mode=self.config.tls_mode.strip().lower(),
      authenticated_user=self.config.username.strip(),
      from_address=self.config.effective_from_address,
      noop_code=int(noop_code),
    )

  async def verify(self) -> SmtpVerifyResult:
    """Verify SMTP connection, TLS, and credentials off the event loop."""
    return await asyncio.to_thread(self.verify_sync)

  def send_sync(
    self,
    *,
    to_address: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
  ) -> SentEmailResult:
    """Send an email synchronously via configured SMTP transport."""
    self.config.validate(require_enabled=True)
    msg = build_email_message(
      from_address=self.config.effective_from_address,
      from_name=self.config.from_name,
      to_address=to_address,
      subject=subject,
      text_body=text_body,
      html_body=html_body,
    )
    client = self._open_and_authenticate_sync()
    try:
      refused = client.send_message(msg)
      if refused:
        raise MailerError(
          f"SMTP server refused recipient(s): {', '.join(sorted(refused))}"
        )
    except MailerError:
      raise
    except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
      raise MailerError(
        f"SMTP send failed: {self._scrub_error(str(exc))}"
      ) from exc
    finally:
      self._close_quietly(client)

    return SentEmailResult(
      message_id=str(msg["Message-ID"] or ""),
      from_header=str(msg["From"] or ""),
      to_address=str(msg["To"] or ""),
      subject=str(msg["Subject"] or ""),
      smtp_host=self.config.smtp_host.strip(),
      smtp_port=int(self.config.smtp_port),
      tls_mode=self.config.tls_mode.strip().lower(),
    )

  async def send(
    self,
    *,
    to_address: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
  ) -> SentEmailResult:
    """Send an email asynchronously off the asyncio event loop."""
    return await asyncio.to_thread(
      self.send_sync,
      to_address=to_address,
      subject=subject,
      text_body=text_body,
      html_body=html_body,
    )
