"""XMLTV stream parser and upstream passthrough endpoint verifier."""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypedDict

import aiohttp

from bellhop.common.http_utils import UPSTREAM_ERRORS, upstream_url
from bellhop.config import AppConfig
from bellhop.epg.models import (
  ProgramItem,
  open_xml_stream,
  parse_xmltv_ts,
  reconcile_non_overlapping_schedule,
)

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


def parse_xmltv_for_channels(
  xml_path: Path,
  channel_id_map: dict[str, str],
  *,
  candidate_chains: Mapping[str, Sequence[str]] | None = None,
  max_per_channel: int = 120,
  now_timestamp: int | None = None,
) -> dict[str, list[ProgramItem]]:
  """Stream-parse an XMLTV file using priority fallback per channel and non-overlapping reconciliation."""
  if not xml_path.is_file() or (not channel_id_map and not candidate_chains):
    return {}

  ref_now = int(time.time()) if now_timestamp is None else now_timestamp
  recent_cutoff = ref_now - 7200

  effective_chains: dict[str, tuple[str, ...]] = {}
  if candidate_chains is not None:
    for canon_id, chain in candidate_chains.items():
      cleaned_chain = tuple(c.strip().lower() for c in chain if c.strip())
      if cleaned_chain:
        effective_chains[canon_id] = cleaned_chain
  else:
    by_canon: dict[str, list[str]] = {}
    for raw_k, canon_id in channel_id_map.items():
      low_k = raw_k.strip().lower()
      if not low_k:
        continue
      bucket = by_canon.setdefault(canon_id, [])
      if low_k == canon_id.lower():
        bucket.insert(0, low_k)
      elif low_k not in bucket:
        bucket.append(low_k)
    effective_chains = {k: tuple(v) for k, v in by_canon.items() if v}

  wanted_raw_keys: set[str] = {
    raw_k for chain in effective_chains.values() for raw_k in chain
  }
  raw_items_by_key: dict[str, list[ProgramItem]] = {}
  seq = 0

  with open_xml_stream(xml_path) as fh:
    try:
      for _event, elem in ET.iterparse(fh, events=("end",)):
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "programme":
          raw_key = (elem.attrib.get("channel") or "").strip().lower()
          if raw_key in wanted_raw_keys:
            title_el = elem.find("title")
            title = (
              (title_el.text or "").strip() if title_el is not None else ""
            )
            if title:
              seq += 1
              sub_el = elem.find("sub-title")
              desc_el = elem.find("desc")
              cat_el = elem.find("category")
              start_ts = parse_xmltv_ts(elem.attrib.get("start") or "")
              stop_ts = parse_xmltv_ts(elem.attrib.get("stop") or "")
              desc_str = (
                (desc_el.text or "").strip() if desc_el is not None else ""
              )

              raw_items_by_key.setdefault(raw_key, []).append(
                ProgramItem(
                  id=str(seq),
                  channel_id=raw_key,
                  title=title,
                  sub_title=(
                    (sub_el.text or "").strip() if sub_el is not None else ""
                  ),
                  description=desc_str,
                  category=(
                    (cat_el.text or "").strip() if cat_el is not None else ""
                  ),
                  start_timestamp=start_ts,
                  stop_timestamp=stop_ts,
                )
              )
          elem.clear()
        elif tag == "channel":
          elem.clear()
    except ET.ParseError:
      pass

  programs_by_id: dict[str, list[ProgramItem]] = {}
  for canonical_id, chain in effective_chains.items():
    selected_raw: list[ProgramItem] | None = None
    fallback_stale: list[ProgramItem] | None = None
    for raw_key in chain:
      cand_list = raw_items_by_key.get(raw_key)
      if not cand_list:
        continue
      reconciled = reconcile_non_overlapping_schedule(cand_list)
      if not reconciled:
        continue
      current_and_upcoming = [
        p
        for p in reconciled
        if p.stop_timestamp == 0 or p.stop_timestamp >= recent_cutoff
      ]
      if current_and_upcoming:
        selected_raw = current_and_upcoming
        break
      if fallback_stale is None:
        fallback_stale = reconciled

    chosen = selected_raw if selected_raw is not None else fallback_stale
    if chosen:
      programs_by_id[canonical_id] = [
        replace(p, channel_id=canonical_id) for p in chosen[:max_per_channel]
      ]

  return programs_by_id
