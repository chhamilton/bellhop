"""Shared Schedules Direct SD-JSON (`20141201`) authentication and HTTP session transport.

Used by both `bellhop.epg.sd_epg` (multi-day EPG schedules & program metadata)
and `bellhop.pipeline.providers.schedules_direct` (lineup headend discovery &
channel retrieval) so SHA-1 password hashing, `0o600` token persistence in
`cache/sd_token.json`, and `4001/4003/4004/4009` token-expiry retry logic live
in one place.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp

from bellhop.common.file_utils import chmod_private_file, write_private_text
from bellhop.common.http_utils import upstream_url

DEFAULT_SD_BASE_URL = "https://json.schedulesdirect.org/20141201"
SD_USER_AGENT = "Bellhop/0.1"
TOKEN_TTL_SEC = 23.0 * 3600.0
REAUTH_ERROR_CODES: frozenset[int] = frozenset({4001, 4003, 4004, 4007, 4009})

SHA1_HEX_RE = re.compile(r"^[0-9a-f]{40}$")
SD_LINEUP_ID_RE = re.compile(
  r"^(?:SD:)?([A-Z]{3}-[A-Z0-9][A-Z0-9_-]*?(?:-[A-Z0-9]+)?)$",
  re.IGNORECASE,
)
_CA_FSA_RE = re.compile(r"^[A-Z]\d[A-Z]$")
_FSA_DEFAULT_SUFFIX: dict[str, str] = {
  "H2Y": "1C6",
  "M5V": "2T6",
  "V6B": "1A1",
  "T2P": "1J9",
  "K1P": "5G4",
  "G1R": "4P5",
  "R3C": "0V8",
  "B3J": "1S9",
}


class SchedulesDirectApiError(RuntimeError):
  """Raised when Schedules Direct authentication or API requests fail."""


def hash_sd_password(password: str) -> str:
  """Return lowercase SHA-1 hex digest required by Schedules Direct `/token`."""
  cleaned = password.strip()
  if SHA1_HEX_RE.match(cleaned.lower()):
    return cleaned.lower()
  return hashlib.sha1(cleaned.encode("utf-8")).hexdigest().lower()


def clean_sd_lineup_id(lineup_id: str) -> str | None:
  """Validate and uppercase a Schedules Direct Gracenote lineup ID."""
  m = SD_LINEUP_ID_RE.match(lineup_id.strip())
  if not m:
    return None
  cand = m.group(1).upper()
  if cand.startswith("TVG-"):
    return None
  return cand


def normalize_sd_postal(
  raw_postal: str, *, explicit_country: str | None = None
) -> tuple[str, str]:
  """Normalize a Canadian postal code / FSA or US 5-digit ZIP into `(postal, ISO3)`."""
  clean = re.sub(r"\s+", "", raw_postal.strip().upper())
  if explicit_country:
    c_up = explicit_country.strip().upper()
    iso3 = {"CA": "CAN", "US": "USA", "GB": "GBR", "UK": "GBR"}.get(c_up, c_up)
  elif clean and clean[0].isalpha():
    iso3 = "CAN"
  else:
    iso3 = "USA"

  if not clean:
    return "", iso3
  if iso3 == "CAN" and _CA_FSA_RE.match(clean):
    suffix = _FSA_DEFAULT_SUFFIX.get(clean, "1A1")
    return f"{clean}{suffix}", iso3
  if iso3 == "USA":
    return clean[:5], iso3
  return clean, iso3


class SchedulesDirectSession:
  """Manages Schedules Direct `/token` caching (`0o600`) and authenticated JSON requests."""

  def __init__(
    self,
    *,
    username: str,
    password: str,
    base_url: str = DEFAULT_SD_BASE_URL,
    token_cache_path: Path | None = Path("cache/sd_token.json"),
    clock: Callable[[], float] = time.time,
  ) -> None:
    self.username = username.strip()
    self.password = password.strip()
    self.base_url = base_url.strip().rstrip("/") or DEFAULT_SD_BASE_URL
    self.token_cache_path = token_cache_path
    self._clock = clock
    self._token: str | None = None

  @property
  def has_credentials(self) -> bool:
    """Return `True` when both Schedules Direct username and password are set."""
    return bool(self.username and self.password)

  def load_cached_token(self) -> str | None:
    """Load a valid (<23h) token matching `(username, base_url)` from disk."""
    if self._token:
      return self._token
    path = self.token_cache_path
    if path is None or not path.is_file():
      return None
    try:
      chmod_private_file(path)
      raw = json.loads(path.read_text(encoding="utf-8"))
      cached_base = str(raw.get("base_url") or "").strip().rstrip("/")
      if (
        isinstance(raw, dict)
        and raw.get("username") == self.username
        and (not cached_base or cached_base == self.base_url)
        and raw.get("token")
        and (float(self._clock()) - float(raw.get("saved_at", 0.0)))
        < TOKEN_TTL_SEC
      ):
        self._token = str(raw["token"]).strip()
        return self._token
    except (OSError, ValueError, TypeError):
      pass
    return None

  def save_cached_token(self, token: str) -> None:
    """Persist `token` atomically (`0o600`) bound to `(username, base_url)`."""
    self._token = token
    if self.token_cache_path is None:
      return
    with contextlib.suppress(OSError):
      write_private_text(
        self.token_cache_path,
        json.dumps(
          {
            "username": self.username,
            "base_url": self.base_url,
            "token": token,
            "saved_at": float(self._clock()),
          },
          indent=2,
        ),
      )

  async def authenticate(self, client: aiohttp.ClientSession) -> str:
    """Exchange `(username, sha1(password))` for a fresh SD API token."""
    if not self.has_credentials:
      raise SchedulesDirectApiError(
        "Schedules Direct credentials missing. Configure `schedules_direct.username` "
        "and `schedules_direct.password` in credentials.yaml or SD_USERNAME / SD_PASSWORD."
      )
    url = f"{self.base_url}/token"
    payload = {
      "username": self.username,
      "password": hash_sd_password(self.password),
    }
    async with client.post(upstream_url(url), json=payload) as resp:
      status = resp.status
      try:
        data = await resp.json(content_type=None)
      except ValueError as exc:
        raise SchedulesDirectApiError(
          f"Schedules Direct /token returned invalid JSON (HTTP {status})"
        ) from exc

    if not isinstance(data, dict):
      raise SchedulesDirectApiError(
        f"Schedules Direct /token returned non-object JSON (HTTP {status})"
      )
    code = int(data.get("code", 0) or 0)
    token = str(data.get("token") or "").strip()
    if status >= 400 or code != 0 or not token:
      msg = data.get("message") or data.get("response") or f"HTTP {status}"
      raise SchedulesDirectApiError(
        f"Schedules Direct authentication failed (code={code}): {msg}"
      )
    self.save_cached_token(token)
    return token

  async def request_json(
    self,
    client: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: Any = None,
    allow_error_codes: frozenset[int] = frozenset(),
  ) -> Any:
    """Execute an authenticated SD-JSON request with 1x automatic token refresh."""
    token = self.load_cached_token() or await self.authenticate(client)
    url = f"{self.base_url}/{path.lstrip('/')}"

    for attempt in range(2):
      async with client.request(
        method,
        upstream_url(url),
        params=params,
        json=json_body,
        headers={"token": token},
      ) as resp:
        need_reauth = resp.status in (401, 403)
        try:
          data = await resp.json(content_type=None)
        except ValueError as exc:
          if need_reauth and attempt == 0:
            data = None
          else:
            raise SchedulesDirectApiError(
              f"Schedules Direct {path} returned invalid JSON (HTTP {resp.status})"
            ) from exc

        if isinstance(data, dict):
          err_code = int(data.get("code", 0) or 0)
          if err_code in REAUTH_ERROR_CODES:
            need_reauth = True
          elif (
            not need_reauth
            and err_code != 0
            and err_code not in allow_error_codes
          ):
            msg = (
              data.get("message")
              or data.get("response")
              or f"SD error {err_code}"
            )
            raise SchedulesDirectApiError(
              f"Schedules Direct {path} error (code={err_code}): {msg}"
            )

        if not need_reauth:
          if resp.status >= 400 and not (
            isinstance(data, dict)
            and int(data.get("code", 0) or 0) in allow_error_codes
          ):
            msg = (
              data.get("message") if isinstance(data, dict) else None
            ) or f"HTTP {resp.status}"
            raise SchedulesDirectApiError(
              f"Schedules Direct {path} failed ({msg})"
            )
          return data

      if attempt == 0:
        self._token = None
        token = await self.authenticate(client)

    raise SchedulesDirectApiError(
      f"Schedules Direct {path} failed after token refresh"
    )
