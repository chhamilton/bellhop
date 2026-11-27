"""Shared multi-user account model, scrypt hashing, and stream URL builders.

Placed in `bellhop.common` so `bellhop.config`, `bellhop.web`,
`bellhop.xtream`, `bellhop.epg`, and `bellhop.pipeline` can share account
validation and upstream URL synthesis without violating layer boundaries.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from urllib.parse import quote

__all__ = [
  "SCRYPT_DKLEN",
  "SCRYPT_N",
  "SCRYPT_P",
  "SCRYPT_R",
  "STREAM_CREDENTIAL_ALPHABET",
  "STREAM_CREDENTIAL_LENGTH",
  "UserAccount",
  "build_upstream_stream_url",
  "build_upstream_xmltv_url",
  "generate_stream_credential",
  "hash_web_password",
  "is_valid_password_hash",
  "normalize_email",
  "normalize_upstream_base_url",
  "password_hash_version",
  "select_pipeline_accounts",
  "validate_accounts_collection",
  "validate_email",
  "validate_stream_credential",
  "verify_dummy_web_password",
  "verify_web_password",
]

STREAM_CREDENTIAL_LENGTH: int = 8
STREAM_CREDENTIAL_ALPHABET: str = "abcdefghijklmnopqrstuvwxyz0123456789"

SCRYPT_N: int = 16384
SCRYPT_R: int = 8
SCRYPT_P: int = 1
SCRYPT_DKLEN: int = 32
_SCRYPT_SALT_BYTES: int = 16
_SCRYPT_MAXMEM: int = 64 * 1024 * 1024

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_UNSAFE_CRED_CHARS = frozenset("/\\?#")

# Constant valid scrypt envelope used only for dummy verification on unknown
# emails so failed login latency matches real accounts without running scrypt at
# import time.
_DUMMY_SCRYPT_SALT_B64 = base64.b64encode(b"\x00" * _SCRYPT_SALT_BYTES).decode(
  "ascii"
)
_DUMMY_SCRYPT_DK_B64 = base64.b64encode(b"\x00" * SCRYPT_DKLEN).decode("ascii")
_DUMMY_SCRYPT_HASH = (
  f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}"
  f"${_DUMMY_SCRYPT_SALT_B64}${_DUMMY_SCRYPT_DK_B64}"
)


def normalize_email(raw: str) -> str:
  """Normalize an email login identifier via strip + lowercase."""
  return raw.strip().lower()


def validate_email(raw: str, *, account_id: str = "") -> str:
  """Validate and normalize an account email address.

  Args:
    raw: Candidate email string.
    account_id: Optional account slug included in error messages.

  Returns:
    Normalized lowercase email string.

  Raises:
    ValueError: If `raw` is empty, contains control characters, or lacks a
      valid `local@domain.tld` shape.
  """
  cleaned = normalize_email(raw)
  ctx = f" for account {account_id!r}" if account_id else ""
  if not cleaned:
    raise ValueError(f"Missing required account email{ctx}.")
  if any(ord(c) < 0x20 or ord(c) == 0x7F for c in cleaned):
    raise ValueError(f"Invalid control character in email{ctx}.")
  if not _EMAIL_RE.fullmatch(cleaned):
    raise ValueError(f"Invalid email address {cleaned!r}{ctx}.")
  return cleaned


def validate_stream_credential(
  raw: str,
  *,
  field_name: str = "xtream_credential",
  account_id: str = "",
) -> str:
  """Validate a Bellhop Xtream stream credential string.

  Manually configured legacy/custom credentials of any safe non-empty length are
  accepted as long as they contain no whitespace, control characters, or URL
  path/query delimiters. Generated credentials always use
  `STREAM_CREDENTIAL_LENGTH` lowercase alphanumeric characters.
  """
  cleaned = raw.strip()
  ctx = f" on account {account_id!r}" if account_id else ""
  if not cleaned:
    raise ValueError(f"Missing {field_name}{ctx}.")
  if any(
    ord(c) <= 0x20 or ord(c) == 0x7F or c in _URL_UNSAFE_CRED_CHARS
    for c in cleaned
  ):
    raise ValueError(
      f"Invalid characters in {field_name}{ctx}: "
      "whitespace, control chars, '/', '\\\\', '?', and '#' are forbidden."
    )
  return cleaned


def generate_stream_credential(*, exclude: Collection[str] = ()) -> str:
  """Generate an 8-character lowercase alphanumeric Xtream credential."""
  excluded = set(exclude)
  while True:
    token = "".join(
      secrets.choice(STREAM_CREDENTIAL_ALPHABET)
      for _ in range(STREAM_CREDENTIAL_LENGTH)
    )
    if token not in excluded:
      return token


def hash_web_password(password: str) -> str:
  """Hash a plaintext Web Guide password with `hashlib.scrypt`."""
  if not password or not password.strip():
    raise ValueError("Web password must not be empty or whitespace-only.")
  salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
  dk = hashlib.scrypt(
    password.encode("utf-8"),
    salt=salt,
    n=SCRYPT_N,
    r=SCRYPT_R,
    p=SCRYPT_P,
    maxmem=_SCRYPT_MAXMEM,
    dklen=SCRYPT_DKLEN,
  )
  salt_b64 = base64.b64encode(salt).decode("ascii")
  dk_b64 = base64.b64encode(dk).decode("ascii")
  return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt_b64}${dk_b64}"


def _parse_scrypt_hash(
  encoded: str,
) -> tuple[int, int, int, bytes, bytes] | None:
  parts = encoded.strip().split("$")
  if len(parts) != 6 or parts[0] != "scrypt":
    return None
  try:
    n_val = int(parts[1])
    r_val = int(parts[2])
    p_val = int(parts[3])
    salt = base64.b64decode(parts[4].encode("ascii"), validate=True)
    expected_dk = base64.b64decode(parts[5].encode("ascii"), validate=True)
  except (ValueError, UnicodeEncodeError):
    return None
  if (
    n_val < 2
    or (n_val & (n_val - 1)) != 0
    or r_val < 1
    or p_val < 1
    or len(salt) < 8
    or len(expected_dk) < 16
  ):
    return None
  return n_val, r_val, p_val, salt, expected_dk


def is_valid_password_hash(encoded: str) -> bool:
  """Return `True` when `encoded` is a structurally valid `scrypt$` hash."""
  return bool(encoded) and (_parse_scrypt_hash(encoded) is not None)


def verify_web_password(password: str, encoded_hash: str) -> bool:
  """Verify `password` against a `scrypt$` envelope in constant time."""
  if not password:
    return False
  parsed = _parse_scrypt_hash(encoded_hash)
  if parsed is None:
    return False
  n_val, r_val, p_val, salt, expected_dk = parsed
  try:
    derived = hashlib.scrypt(
      password.encode("utf-8"),
      salt=salt,
      n=n_val,
      r=r_val,
      p=p_val,
      maxmem=_SCRYPT_MAXMEM,
      dklen=len(expected_dk),
    )
  except ValueError:
    return False
  return hmac.compare_digest(derived, expected_dk)


def verify_dummy_web_password(password: str) -> None:
  """Run constant-cost dummy scrypt work when no account matched an email."""
  verify_web_password(password or "dummy", _DUMMY_SCRYPT_HASH)


def password_hash_version(encoded_hash: str) -> str:
  """Return short opaque digest tying sessions to `encoded_hash`."""
  return hashlib.sha256(encoded_hash.encode("utf-8")).hexdigest()[:12]


def normalize_upstream_base_url(server_url: str) -> str:
  """Normalize an Xtream upstream base URL (`http://host[:port]`)."""
  base = server_url.strip().rstrip("/")
  if not base:
    return ""
  if not base.startswith(("http://", "https://")):
    return f"http://{base}"
  return base


def build_upstream_stream_url(
  server_url: str,
  upstream_username: str,
  upstream_password: str,
  stream_id: int,
  ext: str = "ts",
) -> str:
  """Synthesize an upstream Xtream `/live/<user>/<pass>/<id>.<ext>` URL."""
  base = normalize_upstream_base_url(server_url)
  if (
    not base or not upstream_username or not upstream_password or stream_id <= 0
  ):
    return ""
  clean_ext = ext.strip().lstrip(".") or "ts"
  u_q = quote(upstream_username, safe="")
  p_q = quote(upstream_password, safe="")
  return f"{base}/live/{u_q}/{p_q}/{stream_id}.{clean_ext}"


def build_upstream_xmltv_url(
  server_url: str,
  upstream_username: str,
  upstream_password: str,
  *,
  redact_secret: bool = False,
) -> str:
  """Synthesize an upstream Xtream `xmltv.php` passthrough URL."""
  base = normalize_upstream_base_url(server_url)
  if not base or not upstream_username or not upstream_password:
    return ""
  u_q = quote(upstream_username, safe="")
  pw = "***" if redact_secret else quote(upstream_password, safe="")
  return f"{base}/xmltv.php?username={u_q}&password={pw}"


@dataclass(slots=True, frozen=True)
class UserAccount:
  """Multi-credential user account pairing Web, Xtream, & Upstream auth.

  Attributes:
    account_id: Stable YAML profile key under `accounts:`.
    email: Normalized lowercase Web Guide sign-in email address.
    web_password_hash: Salted `scrypt$` hash for Web Guide authentication.
    xtream_username: Per-user Bellhop Xtream server login username.
    xtream_password: Per-user Bellhop Xtream server login password.
    upstream_username: Backend provider Xtream username for this tuner.
    upstream_password: Backend provider Xtream password for this tuner.
    pipeline: Whether `bellhop run` may use this tuner for validation.
    lineup_id: Reserved optional per-user lineup override for future multi-
      lineup customization.
  """

  account_id: str
  email: str
  web_password_hash: str
  xtream_username: str
  xtream_password: str
  upstream_username: str
  upstream_password: str
  pipeline: bool = False
  lineup_id: str | None = None


def validate_accounts_collection(
  accounts: Sequence[UserAccount],
  *,
  require_non_empty: bool = False,
) -> tuple[UserAccount, ...]:
  """Validate account fields and fail fast on any cross-account collisions.

  Args:
    accounts: Account sequence to validate.
    require_non_empty: When `True`, reject an empty collection.

  Returns:
    Normalized tuple of validated `UserAccount` records.

  Raises:
    ValueError: On missing/invalid fields or duplicate `account_id`, `email`,
      `xtream_username`, or `upstream_username`.
  """
  if require_non_empty and not accounts:
    raise ValueError(
      "At least one account must be configured under `accounts:` in credentials.yaml."
    )

  seen_ids: dict[str, str] = {}
  seen_emails: dict[str, str] = {}
  seen_xt_users: dict[str, str] = {}
  seen_up_users: dict[str, str] = {}
  normalized: list[UserAccount] = []

  for acct in accounts:
    aid = acct.account_id.strip()
    if not aid:
      raise ValueError("Account identifier under `accounts:` cannot be empty.")
    aid_key = aid.lower()
    if aid_key in seen_ids:
      raise ValueError(
        f"Duplicate account identifier {aid!r} (collides with {seen_ids[aid_key]!r})."
      )
    seen_ids[aid_key] = aid

    norm_email = validate_email(acct.email, account_id=aid)
    if norm_email in seen_emails:
      raise ValueError(
        f"Account email collision for {norm_email!r}: "
        f"{aid!r} conflicts with {seen_emails[norm_email]!r}."
      )
    seen_emails[norm_email] = aid

    if not is_valid_password_hash(acct.web_password_hash):
      raise ValueError(
        f"Account {aid!r} has missing or invalid `web_password_hash`."
      )

    xt_user = validate_stream_credential(
      acct.xtream_username, field_name="xtream_username", account_id=aid
    )
    xt_pass = validate_stream_credential(
      acct.xtream_password, field_name="xtream_password", account_id=aid
    )
    if xt_user in seen_xt_users:
      raise ValueError(
        f"Bellhop `xtream_username` collision for {xt_user!r}: "
        f"{aid!r} conflicts with {seen_xt_users[xt_user]!r}."
      )
    seen_xt_users[xt_user] = aid

    up_user = acct.upstream_username.strip()
    up_pass = acct.upstream_password
    if not up_user or not up_pass:
      raise ValueError(
        f"Account {aid!r} requires non-empty `upstream_username` and "
        "`upstream_password`."
      )
    if up_user in seen_up_users:
      raise ValueError(
        f"Upstream Xtream credential collision for `upstream_username` "
        f"{up_user!r}: {aid!r} conflicts with {seen_up_users[up_user]!r}."
      )
    seen_up_users[up_user] = aid

    norm_lineup = acct.lineup_id.strip() if acct.lineup_id else None
    normalized.append(
      UserAccount(
        account_id=aid,
        email=norm_email,
        web_password_hash=acct.web_password_hash.strip(),
        xtream_username=xt_user,
        xtream_password=xt_pass,
        upstream_username=up_user,
        upstream_password=up_pass,
        pipeline=bool(acct.pipeline),
        lineup_id=norm_lineup or None,
      )
    )

  return tuple(normalized)


def select_pipeline_accounts(
  accounts: Sequence[UserAccount],
) -> tuple[UserAccount, ...]:
  """Select accounts marked `pipeline: true`, defaulting to the first."""
  marked = tuple(a for a in accounts if a.pipeline)
  if marked:
    return marked
  return (accounts[0],) if accounts else ()
