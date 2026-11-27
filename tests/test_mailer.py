"""Unit, loopback SMTP, and CLI tests for `bellhop.common.mailer`."""

from __future__ import annotations

import base64
import email
import smtplib
import socket
import socketserver
import ssl
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import cast

import pytest

from bellhop.cli import main as cli_main
from bellhop.common.mailer import (
  DEFAULT_SMTP_HOST,
  DEFAULT_SMTP_PORT,
  MailerAuthError,
  MailerConfigError,
  MailerError,
  SmtpConfig,
  SmtpMailer,
  build_email_message,
  normalize_smtp_password,
)
from bellhop.config import load_config


@dataclass(slots=True)
class CapturedMail:
  """One SMTP transaction recorded by `MockSmtpServer`."""

  mail_from: str
  rcpt_to: list[str]
  raw_data: bytes


class _LoopbackSmtpState:
  def __init__(
    self,
    *,
    expected_user: str = "",
    expected_password: str = "",
    noop_reply: bytes = b"250 2.0.0 OK\r\n",
    refuse_rcpt: bool = False,
  ) -> None:
    self.expected_user = expected_user
    self.expected_password = expected_password
    self.noop_reply = noop_reply
    self.refuse_rcpt = refuse_rcpt
    self.noop_count = 0
    self.auth_attempts: list[tuple[str, str]] = []
    self.messages: list[CapturedMail] = []
    self.lock = threading.Lock()


class _ThreadedTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
  allow_reuse_address = True
  daemon_threads = True

  def __init__(
    self,
    server_address: tuple[str, int],
    handler_cls: type[socketserver.BaseRequestHandler],
    state: _LoopbackSmtpState,
  ) -> None:
    self.smtp_state = state
    super().__init__(server_address, handler_cls)


class _LoopbackSmtpHandler(socketserver.StreamRequestHandler):
  def _send(self, raw: bytes) -> None:
    self.wfile.write(raw)
    self.wfile.flush()

  def _decode_auth_plain(self, b64_token: str) -> tuple[str, str]:
    decoded = base64.b64decode(b64_token.encode("ascii"))
    parts = decoded.split(b"\x00")
    if len(parts) < 3:
      return "", ""
    return parts[1].decode("utf-8"), parts[2].decode("utf-8")

  def handle(self) -> None:
    srv = self.server
    assert isinstance(srv, _ThreadedTcpServer)
    state = srv.smtp_state
    self._send(b"220 127.0.0.1 ESMTP MockSmtpServer\r\n")
    mail_from = ""
    rcpt_to: list[str] = []

    while True:
      line_bytes = self.rfile.readline(65536)
      if not line_bytes:
        break
      line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
      upper = line.upper()

      if upper.startswith("EHLO ") or upper == "EHLO":
        self._send(b"250-127.0.0.1\r\n250 AUTH PLAIN\r\n")
      elif upper.startswith("HELO ") or upper == "HELO":
        self._send(b"250 127.0.0.1\r\n")
      elif upper.startswith("AUTH PLAIN"):
        arg = line[len("AUTH PLAIN") :].strip()
        if not arg:
          self._send(b"334 \r\n")
          arg = self.rfile.readline(65536).decode("utf-8").strip()
        user, pw = self._decode_auth_plain(arg)
        with state.lock:
          state.auth_attempts.append((user, pw))
        if not state.expected_user or (
          user == state.expected_user and pw == state.expected_password
        ):
          self._send(b"235 2.7.0 Authentication successful\r\n")
        else:
          self._send(b"535 5.7.8 Authentication credentials invalid\r\n")
      elif upper == "NOOP":
        with state.lock:
          state.noop_count += 1
        self._send(state.noop_reply)
      elif upper.startswith("MAIL FROM:"):
        mail_from = line[len("MAIL FROM:") :].strip().strip("<>")
        rcpt_to = []
        self._send(b"250 2.1.0 OK\r\n")
      elif upper.startswith("RCPT TO:"):
        if state.refuse_rcpt:
          self._send(b"550 5.1.1 User unknown\r\n")
        else:
          rcpt_to.append(line[len("RCPT TO:") :].strip().strip("<>"))
          self._send(b"250 2.1.5 OK\r\n")
      elif upper == "DATA":
        self._send(b"354 End data with <CR><LF>.<CR><LF>\r\n")
        chunks: list[bytes] = []
        while True:
          data_line = self.rfile.readline(65536)
          if not data_line or data_line == b".\r\n":
            break
          if data_line.startswith(b".."):
            data_line = data_line[1:]
          chunks.append(data_line)
        with state.lock:
          state.messages.append(
            CapturedMail(
              mail_from=mail_from,
              rcpt_to=list(rcpt_to),
              raw_data=b"".join(chunks),
            )
          )
        self._send(b"250 2.0.0 OK queued\r\n")
      elif upper == "RSET":
        mail_from = ""
        rcpt_to = []
        self._send(b"250 2.0.0 OK\r\n")
      elif upper == "QUIT":
        self._send(b"221 2.0.0 Bye\r\n")
        break
      else:
        self._send(b"500 5.5.1 Unrecognized command\r\n")


class MockSmtpServer:
  """Context-managed loopback SMTP server for hermetic mailer tests."""

  def __init__(
    self,
    *,
    expected_user: str = "",
    expected_password: str = "",
    noop_reply: bytes = b"250 2.0.0 OK\r\n",
    refuse_rcpt: bool = False,
  ) -> None:
    self.state = _LoopbackSmtpState(
      expected_user=expected_user,
      expected_password=expected_password,
      noop_reply=noop_reply,
      refuse_rcpt=refuse_rcpt,
    )
    self._server: _ThreadedTcpServer | None = None
    self._thread: threading.Thread | None = None
    self.port: int = 0

  def __enter__(self) -> MockSmtpServer:
    srv = _ThreadedTcpServer(("127.0.0.1", 0), _LoopbackSmtpHandler, self.state)
    self._server = srv
    self.port = int(srv.server_address[1])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    self._thread = t
    return self

  def __exit__(self, *_exc: object) -> None:
    if self._server is not None:
      self._server.shutdown()
      self._server.server_close()
    if self._thread is not None:
      self._thread.join(timeout=2.0)


@dataclass(slots=True)
class _FakeTlsSmtpClient:
  """Deterministic SMTP client double testing STARTTLS and SSL handshakes."""

  events: list[str] = field(default_factory=list)
  advertise_starttls: bool = True
  noop_code: int = 250
  fail_noop: bool = False
  fail_send: bool = False
  refused: dict[str, tuple[int, bytes]] = field(default_factory=dict)

  def ehlo(self) -> tuple[int, bytes]:
    self.events.append("ehlo")
    return 250, b"ok"

  def has_extn(self, name: str) -> bool:
    self.events.append(f"has_extn:{name}")
    return name.lower() == "starttls" and self.advertise_starttls

  def starttls(
    self, context: ssl.SSLContext | None = None
  ) -> tuple[int, bytes]:
    assert context is not None
    self.events.append("starttls")
    return 220, b"ready"

  def login(self, user: str, password: str) -> tuple[int, bytes]:
    self.events.append(f"login:{user}:{password}")
    return 235, b"ok"

  def noop(self) -> tuple[int, bytes]:
    self.events.append("noop")
    if self.fail_noop:
      raise smtplib.SMTPServerDisconnected("boom")
    return self.noop_code, b"pong"

  def send_message(self, msg: EmailMessage) -> dict[str, tuple[int, bytes]]:
    self.events.append(f"send:{msg['To']}")
    if self.fail_send:
      raise OSError("broken pipe")
    return dict(self.refused)

  def quit(self) -> tuple[int, bytes]:
    self.events.append("quit")
    return 221, b"bye"

  def close(self) -> None:
    self.events.append("close")


def test_normalize_smtp_password_and_config_validation(tmp_path: Path) -> None:
  # Google Workspace / Gmail App Passwords strip spaces automatically.
  assert (
    normalize_smtp_password("abcd efgh ijkl mnop", smtp_host="smtp.gmail.com")
    == "abcdefghijklmnop"
  )
  assert (
    normalize_smtp_password(
      "  wxyz 1234 abcd 5678 ", smtp_host="smtp-relay.gmail.com."
    )
    == "wxyz1234abcd5678"
  )
  # Non-Google hosts and regular passphrases stay untouched.
  assert (
    normalize_smtp_password("abcd efgh ijkl mnop", smtp_host="mail.example.com")
    == "abcd efgh ijkl mnop"
  )
  assert (
    normalize_smtp_password("not an app pass!", smtp_host="smtp.gmail.com")
    == "not an app pass!"
  )

  # Validation catches disabled, bad ports/modes/timeouts, half creds,
  # cleartext creds on non-loopback hosts, and header injections.
  disabled = SmtpConfig(enabled=False, smtp_host=DEFAULT_SMTP_HOST)
  assert disabled.is_configured is False
  with pytest.raises(MailerConfigError, match="disabled"):
    disabled.validate(require_enabled=True)

  for bad_cfg in (
    SmtpConfig(
      enabled=True, smtp_host="", username="a@example.com", password="p"
    ),
    SmtpConfig(
      enabled=True,
      smtp_host="bad\nhost",
      username="a@example.com",
      password="p",
    ),
    SmtpConfig(
      enabled=True, smtp_port=0, username="a@example.com", password="p"
    ),
    SmtpConfig(
      enabled=True,
      smtp_port=cast(int, True),
      username="a@example.com",
      password="p",
    ),
    SmtpConfig(
      enabled=True, tls_mode="bogus", username="a@example.com", password="p"
    ),
    SmtpConfig(
      enabled=True, timeout_sec=0, username="a@example.com", password="p"
    ),
    SmtpConfig(enabled=True, username="a@example.com", password=""),
    SmtpConfig(enabled=True, username="", from_address=""),
    SmtpConfig(enabled=True, from_address="not_an_email"),
    SmtpConfig(
      enabled=True,
      smtp_host="smtp.example.com",
      tls_mode="none",
      username="a@example.com",
      password="secret",
    ),
  ):
    with pytest.raises(MailerConfigError):
      bad_cfg.validate(require_enabled=True)

  # `none` is allowed for loopback relays even with credentials.
  loopback_ok = SmtpConfig(
    enabled=True,
    smtp_host="127.0.0.1",
    smtp_port=2525,
    tls_mode="none",
    username="mailer@example.com",
    password="pw",
  )
  assert loopback_ok.is_configured is True
  loopback_ok.validate(require_enabled=True)

  # Config loading supports `app_password` alias and redacts `to_dict`.
  creds = tmp_path / "credentials.yaml"
  creds.write_text(
    """\
accounts:
  admin:
    email: "admin@example.com"
    web_password: "secret_web_password"
    upstream_username: "up_u"
    upstream_password: "up_p"
email:
  username: "bellhop@example.com"
  app_password: "abcd efgh ijkl mnop"
""",
    encoding="utf-8",
  )
  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"""\
credentials_file: "{creds}"
email:
  enabled: true
  smtp_host: "{DEFAULT_SMTP_HOST}"
  smtp_port: {DEFAULT_SMTP_PORT}
  tls_mode: "starttls"
  from_name: "Bellhop Mailer"
""",
    encoding="utf-8",
  )
  loaded = load_config(cfg_path)
  assert loaded.email.enabled is True
  assert loaded.email.effective_password == "abcdefghijklmnop"
  assert loaded.email.effective_from_address == "bellhop@example.com"
  redacted = loaded.to_dict(redact_secrets=True)
  assert redacted["email"]["password"] == "***"
  assert loaded.to_dict(redact_secrets=False)["email"]["password"] == (
    "abcd efgh ijkl mnop"
  )


def test_load_config_email_validation_errors(tmp_path: Path) -> None:
  creds = tmp_path / "credentials.yaml"
  creds.write_text(
    """\
accounts:
  admin:
    email: "admin@example.com"
    web_password: "secret_web_password"
    upstream_username: "up_u"
    upstream_password: "up_p"
email:
  username: "only_user@example.com"
  password: ""
""",
    encoding="utf-8",
  )
  cfg_path = tmp_path / "config.yaml"
  cfg_path.write_text(
    f"""\
credentials_file: "{creds}"
email:
  enabled: true
""",
    encoding="utf-8",
  )
  with pytest.raises(ValueError, match="email.username"):
    load_config(cfg_path)


def test_build_email_message_headers_and_injection_guards() -> None:
  msg = build_email_message(
    from_address="bellhop@example.com",
    from_name="Bellhop Server",
    to_address="USER@Example.com",
    subject="Hello Bellhop",
    text_body="Plain body",
    html_body="<p>HTML body</p>",
  )
  assert msg["To"] == "user@example.com"
  assert "Bellhop Server" in str(msg["From"])
  assert msg["Auto-Submitted"] == "auto-generated"
  assert msg["Message-ID"] is not None
  payload = msg.get_payload()
  assert isinstance(payload, list) and len(payload) == 2

  for kwargs in (
    {
      "from_address": "a@example.com",
      "to_address": "b@example.com",
      "subject": "Bad\r\nBcc: evil@example.com",
      "text_body": "Hi",
    },
    {
      "from_address": "a@example.com",
      "to_address": "bad-recipient",
      "subject": "Ok",
      "text_body": "Hi",
    },
    {
      "from_address": "a@example.com",
      "to_address": "b@example.com",
      "subject": "   ",
      "text_body": "Hi",
    },
    {
      "from_address": "a@example.com",
      "to_address": "b@example.com",
      "subject": "Ok",
      "text_body": "   ",
    },
  ):
    with pytest.raises(MailerConfigError):
      build_email_message(**kwargs)


@pytest.mark.asyncio
async def test_smtp_mailer_verify_send_and_auth_against_loopback_server() -> (
  None
):
  with MockSmtpServer(
    expected_user="mailer@example.com",
    expected_password="app_secret_password",
  ) as smtp:
    cfg = SmtpConfig(
      enabled=True,
      smtp_host="127.0.0.1",
      smtp_port=smtp.port,
      tls_mode="none",
      username="mailer@example.com",
      password="app_secret_password",
      from_name="Bellhop Test",
    )
    mailer = SmtpMailer(cfg)

    # 1. Verify connects + authenticates + runs NOOP without sending mail.
    verified = await mailer.verify()
    assert verified.noop_code == 250
    assert verified.authenticated_user == "mailer@example.com"
    assert smtp.state.noop_count == 1
    assert smtp.state.messages == []

    # 2. Send delivers valid multipart plain+HTML message over loopback TCP.
    sent = await mailer.send(
      to_address="viewer@example.com",
      subject="Reset Check",
      text_body="Hello viewer",
      html_body="<p>Hello viewer</p>",
    )
    assert sent.to_address == "viewer@example.com"
    assert len(smtp.state.messages) == 1
    captured = smtp.state.messages[0]
    assert captured.mail_from == "mailer@example.com"
    assert captured.rcpt_to == ["viewer@example.com"]
    parsed = email.message_from_bytes(captured.raw_data)
    assert parsed["Subject"] == "Reset Check"
    assert parsed["Auto-Submitted"] == "auto-generated"

    # 3. Wrong password raises MailerAuthError and scrubs secret.
    bad_cfg = SmtpConfig(
      enabled=True,
      smtp_host="127.0.0.1",
      smtp_port=smtp.port,
      tls_mode="none",
      username="mailer@example.com",
      password="wrong_app_password_xyz",
    )
    with pytest.raises(MailerAuthError) as auth_exc:
      await SmtpMailer(bad_cfg).verify()
    assert "wrong_app_password_xyz" not in str(auth_exc.value)

  # 4. Non-OK NOOP and refused recipient raise MailerError cleanly.
  with (
    MockSmtpServer(noop_reply=b"451 Requested action aborted\r\n") as s_noop,
    pytest.raises(MailerError, match="NOOP"),
  ):
    SmtpMailer(
      SmtpConfig(
        enabled=True,
        smtp_host="127.0.0.1",
        smtp_port=s_noop.port,
        tls_mode="none",
        from_address="bellhop@example.com",
      )
    ).verify_sync()

  with (
    MockSmtpServer(refuse_rcpt=True) as s_rcpt,
    pytest.raises(MailerError),
  ):
    SmtpMailer(
      SmtpConfig(
        enabled=True,
        smtp_host="127.0.0.1",
        smtp_port=s_rcpt.port,
        tls_mode="none",
        from_address="bellhop@example.com",
      )
    ).send_sync(
      to_address="nobody@example.com",
      subject="Bounce",
      text_body="Body",
    )

  # 5. Unreachable port raises MailerError without leaking secret.
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    closed_port = int(sock.getsockname()[1])
  with pytest.raises(MailerError):
    SmtpMailer(
      SmtpConfig(
        enabled=True,
        smtp_host="127.0.0.1",
        smtp_port=closed_port,
        tls_mode="none",
        username="u@example.com",
        password="super_secret_password",
        timeout_sec=1.0,
      )
    ).verify_sync()


def test_smtp_mailer_starttls_and_ssl_handshake_paths() -> None:
  fake_tls = _FakeTlsSmtpClient(advertise_starttls=True)
  cfg_tls = SmtpConfig(
    enabled=True,
    smtp_host="smtp.gmail.com",
    smtp_port=587,
    tls_mode="starttls",
    username="workspace@example.com",
    password="abcd efgh ijkl mnop",
  )
  mailer_tls = SmtpMailer(
    cfg_tls,
    smtp_client_factory=lambda *_a, **_kw: fake_tls,
  )
  mailer_tls.verify_sync()
  assert fake_tls.events == [
    "ehlo",
    "has_extn:starttls",
    "starttls",
    "ehlo",
    "login:workspace@example.com:abcdefghijklmnop",
    "noop",
    "quit",
  ]

  # Missing STARTTLS extension fails cleanly before sending credentials.
  no_tls = _FakeTlsSmtpClient(advertise_starttls=False)
  with pytest.raises(MailerError, match="STARTTLS"):
    SmtpMailer(
      cfg_tls,
      smtp_client_factory=lambda *_a, **_kw: no_tls,
    ).verify_sync()
  assert "close" in no_tls.events
  assert not any(ev.startswith("login:") for ev in no_tls.events)

  # Implicit SSL mode uses `SMTP_SSL` factory and skips STARTTLS.
  fake_ssl = _FakeTlsSmtpClient()
  cfg_ssl = SmtpConfig(
    enabled=True,
    smtp_host="smtp.gmail.com",
    smtp_port=465,
    tls_mode="ssl",
    username="workspace@example.com",
    password="abcd efgh ijkl mnop",
  )
  res = SmtpMailer(
    cfg_ssl,
    smtp_ssl_client_factory=lambda *_a, **_kw: fake_ssl,
  ).send_sync(
    to_address="dest@example.com",
    subject="SSL Ok",
    text_body="Works",
  )
  assert res.tls_mode == "ssl"
  assert fake_ssl.events == [
    "ehlo",
    "login:workspace@example.com:abcdefghijklmnop",
    "send:dest@example.com",
    "quit",
  ]


def test_cli_test_email_dry_run_send_and_errors(tmp_path: Path) -> None:
  creds = tmp_path / "credentials.yaml"
  creds.write_text(
    """\
accounts:
  admin:
    email: "admin@example.com"
    web_password: "secret_web_password"
    upstream_username: "up_u"
    upstream_password: "up_p"
email:
  username: "bot@example.com"
  password: "smtp_app_password"
""",
    encoding="utf-8",
  )
  cfg_path = tmp_path / "config.yaml"

  with MockSmtpServer(
    expected_user="bot@example.com",
    expected_password="smtp_app_password",
  ) as smtp:
    cfg_path.write_text(
      f"""\
credentials_file: "{creds}"
email:
  enabled: true
  smtp_host: "127.0.0.1"
  smtp_port: {smtp.port}
  tls_mode: "none"
  from_name: "Bellhop"
  from_address: "bot@example.com"
""",
      encoding="utf-8",
    )

    assert cli_main(["test-email", "--dry-run", "-c", str(cfg_path)]) == 0
    assert smtp.state.noop_count == 1
    assert len(smtp.state.messages) == 0

    # Omitting recipient defaults to primary account email.
    assert cli_main(["test-email", "-c", str(cfg_path)]) == 0
    assert len(smtp.state.messages) == 1
    assert smtp.state.messages[0].rcpt_to == ["admin@example.com"]

    # Explicit recipient + --quiet returns 0 cleanly.
    assert (
      cli_main(["test-email", "other@example.com", "-c", str(cfg_path), "-q"])
      == 0
    )
    assert smtp.state.messages[1].rcpt_to == ["other@example.com"]

  # Disabled email config returns exit code 2 from CLI cleanly.
  cfg_disabled = tmp_path / "config_disabled.yaml"
  cfg_disabled.write_text(
    f"""\
credentials_file: "{creds}"
email:
  enabled: false
""",
    encoding="utf-8",
  )
  assert cli_main(["test-email", "--dry-run", "-c", str(cfg_disabled)]) == 2
