"""XMLTV stream parser and upstream passthrough endpoint verifier."""

from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypedDict

import aiohttp

from bellhop.common.accounts import UserAccount, build_upstream_xmltv_url
from bellhop.common.http_utils import (
  UPSTREAM_ERRORS,
  default_session_factory,
  extract_explicit_private_origins,
  open_upstream_url,
  redact_url_secrets,
)
from bellhop.config import AppConfig
from bellhop.epg.constants import (
  EPG_STALE_ENTRY_GRACE_SEC,
  MAX_XMLTV_DECOMPRESSED_BYTES,
  MAX_XMLTV_RAW_ITEMS_PER_CHANNEL,
  MAX_XMLTV_TEXT_CHARS,
)
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
  """Result of a passthrough probe against the upstream XMLTV endpoint."""

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
      "error": redact_url_secrets(self.error)
      if self.error is not None
      else None,
    }


def build_passthrough_xmltv_url(
  config: AppConfig,
  *,
  account: UserAccount | None = None,
  redact_secret: bool = False,
) -> str:
  """Build upstream passthrough `xmltv.php` URL via the active account."""
  acct = account or config.pipeline_account
  if acct is None:
    return ""
  return build_upstream_xmltv_url(
    config.xtream.server_url,
    acct.upstream_username,
    acct.upstream_password,
    redact_secret=redact_secret,
  )


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


def _read_file_prefix(path: Path, limit: int = 4096) -> bytes:
  with path.open("rb") as fh:
    return fh.read(limit)


async def verify_epg_endpoint(
  config: AppConfig,
  *,
  timeout_sec: float = 15.0,
  client: aiohttp.ClientSession | None = None,
) -> EpgVerificationResult:
  """Verify the upstream XMLTV endpoint returns valid XMLTV headers.

  Probes reachability without downloading the full XMLTV feed.
  """
  if config.epg.xmltv_file is not None:
    resolved = config.resolve_path(config.epg.xmltv_file)
    if not resolved.is_file():
      return EpgVerificationResult(
        url=str(resolved),
        url_redacted=str(resolved),
        verified=False,
        error=f"Local XMLTV file not found: {resolved}",
      )
    sample = await asyncio.to_thread(_read_file_prefix, resolved, 4096)
    fmt = _detect_xmltv_prefix(sample)
    return EpgVerificationResult(
      url=str(resolved),
      url_redacted=str(resolved),
      verified=bool(fmt),
      format_detected=fmt or "unknown",
      error=None if fmt else "File does not appear to be valid XMLTV",
    )

  pipe_acct = config.pipeline_account
  full_url = build_passthrough_xmltv_url(
    config, account=pipe_acct, redact_secret=False
  )
  redacted_url = build_passthrough_xmltv_url(
    config, account=pipe_acct, redact_secret=True
  )
  secret_pw = pipe_acct.upstream_password if pipe_acct is not None else ""
  if not full_url:
    return EpgVerificationResult(
      verified=False,
      skipped=True,
      error="No upstream Xtream credentials configured for XMLTV passthrough",
    )

  owns_client = client is None
  if owns_client:
    client = default_session_factory(
      timeout=aiohttp.ClientTimeout(
        total=timeout_sec, connect=min(10.0, timeout_sec)
      ),
      headers={"User-Agent": DEFAULT_EPG_USER_AGENT},
    )

  assert client is not None
  t0 = time.perf_counter()
  allowed_origins = extract_explicit_private_origins((full_url,))
  try:
    async with open_upstream_url(
      client,
      full_url,
      allowed_private_origins=allowed_origins,
    ) as resp:
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
      error=redact_url_secrets(str(exc), secret=secret_pw),
    )
  finally:
    if owns_client:
      await client.close()


_XMLTV_NS_EP_RE = re.compile(r"^\s*(\d+)\s*(?:/\d+)?\.\s*(\d+)")


def _clip_xmltv_text(el: ET.Element | None) -> str:
  if el is None or not el.text:
    return ""
  return el.text.strip()[:MAX_XMLTV_TEXT_CHARS]


def _extract_xmltv_categories(elem: ET.Element) -> str:
  cats: list[str] = []
  for cat_el in elem.findall("category"):
    val = _clip_xmltv_text(cat_el)
    if val and val not in cats:
      cats.append(val)
      if len(cats) >= 3:
        break
  return ", ".join(cats)


def _extract_xmltv_episode_num(elem: ET.Element) -> str:
  ns_fallback = ""
  for ep_el in elem.findall("episode-num"):
    raw_val = _clip_xmltv_text(ep_el)[:32]
    if not raw_val:
      continue
    system = (ep_el.attrib.get("system") or "").strip().lower()
    if system in {"onscreen", ""}:
      return raw_val
    if system == "xmltv_ns" and not ns_fallback:
      ns_fallback = raw_val
  if ns_fallback:
    m = _XMLTV_NS_EP_RE.match(ns_fallback)
    if m:
      return f"S{int(m.group(1)) + 1:02d}E{int(m.group(2)) + 1:02d}"
  return ""


def _extract_xmltv_rating(elem: ET.Element) -> str:
  rating_el = elem.find("rating")
  if rating_el is None:
    return ""
  return _clip_xmltv_text(rating_el.find("value"))[:24]


def parse_xmltv_for_channels(
  xml_path: Path,
  channel_id_map: dict[str, str],
  *,
  candidate_chains: Mapping[str, Sequence[str]] | None = None,
  max_per_channel: int = 120,
  now_timestamp: int | None = None,
  max_decompressed_bytes: int = MAX_XMLTV_DECOMPRESSED_BYTES,
  max_raw_per_channel: int = MAX_XMLTV_RAW_ITEMS_PER_CHANNEL,
) -> dict[str, list[ProgramItem]]:
  """Stream-parse an XMLTV file with priority fallback per channel.

  Applies non-overlapping interval reconciliation across matched programs.
  """
  if not xml_path.is_file() or (not channel_id_map and not candidate_chains):
    return {}

  ref_now = int(time.time()) if now_timestamp is None else now_timestamp
  recent_cutoff = ref_now - EPG_STALE_ENTRY_GRACE_SEC

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
  pruned_raw_keys: set[str] = set()
  seq = 0
  raw_cap = max(max_per_channel, max_raw_per_channel)

  with open_xml_stream(xml_path, max_bytes=max_decompressed_bytes) as fh:
    try:
      root: ET.Element | None = None
      depth = 0
      for event, elem in ET.iterparse(fh, events=("start", "end")):
        if event == "start":
          if depth == 0:
            root = elem
          depth += 1
          continue
        depth -= 1
        if depth == 1:
          tag = elem.tag.rsplit("}", 1)[-1]
          if tag == "programme":
            raw_key = (elem.attrib.get("channel") or "").strip().lower()
            if raw_key in wanted_raw_keys:
              title = _clip_xmltv_text(elem.find("title"))
              if title:
                seq += 1
                start_ts = parse_xmltv_ts(elem.attrib.get("start") or "")
                stop_ts = parse_xmltv_ts(elem.attrib.get("stop") or "")
                chan_bucket = raw_items_by_key.setdefault(raw_key, [])
                if (
                  len(chan_bucket) >= raw_cap and raw_key not in pruned_raw_keys
                ):
                  pruned_raw_keys.add(raw_key)
                  chan_bucket[:] = [
                    p
                    for p in chan_bucket
                    if p.stop_timestamp == 0
                    or p.stop_timestamp >= recent_cutoff
                  ]
                if len(chan_bucket) < raw_cap:
                  chan_bucket.append(
                    ProgramItem(
                      id=str(seq),
                      channel_id=raw_key,
                      title=title,
                      sub_title=_clip_xmltv_text(elem.find("sub-title")),
                      description=_clip_xmltv_text(elem.find("desc")),
                      category=_extract_xmltv_categories(elem),
                      start_timestamp=start_ts,
                      stop_timestamp=stop_ts,
                      episode_num=_extract_xmltv_episode_num(elem),
                      air_date=_clip_xmltv_text(elem.find("date"))[:10],
                      rating=_extract_xmltv_rating(elem),
                      is_new=elem.find("new") is not None,
                      is_live_airing=elem.find("live") is not None,
                    )
                  )
          elem.clear()
          if root is not None:
            root.clear()
        elif depth == 0:
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
