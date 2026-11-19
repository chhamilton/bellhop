"""Upstream XMLTV passthrough URL builder and lightweight streaming endpoint verifier."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TypedDict

import aiohttp

from bellhop.config import AppConfig
from bellhop_web.http_utils import UPSTREAM_ERRORS, upstream_url

DEFAULT_EPG_USER_AGENT = "Bellhop/0.1 (VLC/3.0.20 LibVLC/3.0.20)"


class EpgVerificationDict(TypedDict):
  """Serialized passthrough XMLTV probe telemetry."""

  url: str
  verified: bool
  skipped: bool
  status_code: int | None
  content_type: str
  format_detected: str
  latency_ms: float
  error: str | None


@dataclass(slots=True)
class EpgVerificationResult:
  """Result of a lightweight passthrough probe against the upstream XMLTV endpoint."""

  url: str = ""
  url_redacted: str = ""
  verified: bool = False
  skipped: bool = False
  status_code: int | None = None
  content_type: str = ""
  format_detected: str = ""
  latency_ms: float = 0.0
  error: str | None = None

  def to_dict(self) -> EpgVerificationDict:
    return {
      "url": self.url_redacted,
      "verified": self.verified,
      "skipped": self.skipped,
      "status_code": self.status_code,
      "content_type": self.content_type,
      "format_detected": self.format_detected,
      "latency_ms": round(self.latency_ms, 1),
      "error": self.error,
    }


def build_passthrough_xmltv_url(
  config: AppConfig, *, redact_secret: bool = False
) -> str:
  """Build the upstream passthrough `xmltv.php` URL from `AppConfig.xtream`."""
  xt = config.xtream
  if not xt.server_url or not xt.username or not xt.password:
    return ""
  base = xt.server_url.strip().rstrip("/")
  if not base.startswith(("http://", "https://")):
    base = f"http://{base}"
  pw = "***" if redact_secret else xt.password
  return f"{base}/xmltv.php?username={xt.username}&password={pw}"


def _detect_xmltv_prefix(sample: bytes) -> str:
  if len(sample) >= 2 and sample[:2] == b"\x1f\x8b":
    return "xmltv_gzip"
  head = (
    sample.lstrip(b"\xef\xbb\xbf \t\r\n")
    .decode("utf-8", errors="ignore")
    .lower()
  )
  if head.startswith("<?xml") or "<tv" in head or "<!doctype tv" in head:
    return "xmltv"
  return ""


async def verify_epg_endpoint(
  config: AppConfig,
  *,
  timeout_sec: float = 15.0,
  client: aiohttp.ClientSession | None = None,
) -> EpgVerificationResult:
  """Verify the upstream XMLTV endpoint is reachable and returns XMLTV without downloading it."""
  if config.epg.xmltv_file is not None:
    resolved = config.resolve_path(config.epg.xmltv_file)
    if not resolved.is_file():
      return EpgVerificationResult(
        url=str(resolved),
        url_redacted=str(resolved),
        verified=False,
        error=f"Local XMLTV file not found: {resolved}",
      )
    sample = resolved.read_bytes()[:4096]
    fmt = _detect_xmltv_prefix(sample)
    return EpgVerificationResult(
      url=str(resolved),
      url_redacted=str(resolved),
      verified=bool(fmt),
      format_detected=fmt or "unknown",
      error=None if fmt else "File does not appear to be valid XMLTV",
    )

  full_url = build_passthrough_xmltv_url(config, redact_secret=False)
  redacted_url = build_passthrough_xmltv_url(config, redact_secret=True)
  if not full_url:
    return EpgVerificationResult(
      verified=False,
      skipped=True,
      error="No upstream Xtream credentials configured for XMLTV passthrough",
    )

  owns_client = client is None
  if owns_client:
    client = aiohttp.ClientSession(
      timeout=aiohttp.ClientTimeout(
        total=timeout_sec, connect=min(10.0, timeout_sec)
      ),
      headers={"User-Agent": DEFAULT_EPG_USER_AGENT},
    )

  assert client is not None
  t0 = time.perf_counter()
  try:
    async with client.get(upstream_url(full_url)) as resp:
      latency_ms = (time.perf_counter() - t0) * 1000.0
      ctype = resp.headers.get("Content-Type", "")
      if resp.status != 200:
        return EpgVerificationResult(
          url=full_url,
          url_redacted=redacted_url,
          verified=False,
          status_code=resp.status,
          content_type=ctype,
          latency_ms=latency_ms,
          error=f"HTTP {resp.status}",
        )

      sample_buf = bytearray()
      async for chunk in resp.content.iter_chunked(4096):
        sample_buf.extend(chunk)
        if len(sample_buf) >= 4096:
          break

      fmt = _detect_xmltv_prefix(bytes(sample_buf))
      if not fmt:
        return EpgVerificationResult(
          url=full_url,
          url_redacted=redacted_url,
          verified=False,
          status_code=resp.status,
          content_type=ctype,
          latency_ms=latency_ms,
          error="Response body is not valid XMLTV or gzipped XMLTV",
        )

      return EpgVerificationResult(
        url=full_url,
        url_redacted=redacted_url,
        verified=True,
        status_code=resp.status,
        content_type=ctype,
        format_detected=fmt,
        latency_ms=latency_ms,
      )
  except UPSTREAM_ERRORS as exc:
    return EpgVerificationResult(
      url=full_url,
      url_redacted=redacted_url,
      verified=False,
      latency_ms=(time.perf_counter() - t0) * 1000.0,
      error=str(exc),
    )
  finally:
    if owns_client:
      await client.close()
