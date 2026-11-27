"""Multi-stage async IPTV stream validator.

Runs Stage 1 HTTP/HLS/MPEG-TS checks, Stage 2 ffprobe, and Stage 3 blackdetect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urljoin

import aiohttp

from bellhop.common.http_utils import (
  ClientSessionFactory,
  default_session_factory,
  redact_url_secrets,
  upstream_url,
)
from bellhop.config import AppConfig, UserAccount
from bellhop.pipeline.models import (
  MappedChannel,
  MatchCandidate,
  ProbeResult,
  XtreamAccountInfo,
  XtreamStream,
)

logger = logging.getLogger(__name__)

STREAM_USER_AGENT = "VLC/3.0.20 LibVLC/3.0.20"
FFMPEG_PROTOCOL_WHITELIST = "http,https,tcp,tls,crypto,hls"
TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47
AUDIO_LANG_MISMATCH_PREFIX = "Audio track language mismatch"
BLACK_DURATION_RE = re.compile(r"black_duration:\s*([0-9]+(?:\.[0-9]+)?)")

UNDETERMINED_LANG_CODES = frozenset(
  {
    "und",
    "unk",
    "mul",
    "zxx",
    "mis",
    "nar",
    "qaa",
    "qab",
    "qac",
    "none",
    "unknown",
    "n/a",
  }
)

ISO_LANG_TO_CANONICAL: dict[str, str] = {
  "eng": "en",
  "en": "en",
  "fra": "fr",
  "fre": "fr",
  "fr": "fr",
  "spa": "es",
  "es": "es",
  "ita": "it",
  "it": "it",
  "ara": "ar",
  "ar": "ar",
  "por": "pt",
  "pt": "pt",
  "deu": "de",
  "ger": "de",
  "de": "de",
  "ell": "el",
  "gre": "el",
  "el": "el",
  "zho": "zh",
  "chi": "zh",
  "cmn": "zh",
  "yue": "zh",
  "zh": "zh",
  "hin": "hi",
  "hi": "hi",
  "pan": "pa",
  "pa": "pa",
  "urd": "ur",
  "ur": "ur",
  "tgl": "tl",
  "fil": "tl",
  "tl": "tl",
  "rus": "ru",
  "ru": "ru",
  "ukr": "uk",
  "uk": "uk",
  "pol": "pl",
  "pl": "pl",
  "ron": "ro",
  "rum": "ro",
  "ro": "ro",
  "tur": "tr",
  "tr": "tr",
  "vie": "vi",
  "vi": "vi",
  "jpn": "ja",
  "ja": "ja",
  "kor": "ko",
  "ko": "ko",
  "nld": "nl",
  "dut": "nl",
  "nl": "nl",
  "heb": "he",
  "he": "he",
  "fas": "fa",
  "per": "fa",
  "fa": "fa",
}

ProgressCallback = Callable[[MappedChannel, int, int], None]


def normalize_audio_language_code(raw_lang: str | None) -> str | None:
  """Normalize an ISO-639 audio language tag, ignoring `und` and `mul`."""
  if not raw_lang:
    return None
  token = re.split(r"[-_]", raw_lang.strip().lower(), maxsplit=1)[0].strip()
  if not token or token in UNDETERMINED_LANG_CODES:
    return None
  if token in ISO_LANG_TO_CANONICAL:
    return ISO_LANG_TO_CANONICAL[token]
  if len(token) in (2, 3) and token.isalpha():
    return token
  return None


def check_audio_language_compatibility(
  expected_lang: str | None,
  audio_languages: Sequence[str],
  *,
  allow_audio_only: bool = False,
) -> tuple[bool, bool]:
  """Return `(is_compatible, is_verified_match)` for slot vs probe audio."""
  if allow_audio_only:
    return (True, False)
  expected = normalize_audio_language_code(expected_lang)
  actual = [
    normed
    for raw in audio_languages
    if (normed := normalize_audio_language_code(raw)) is not None
  ]
  if expected is None or not actual:
    return (True, False)
  if expected in actual:
    return (True, True)
  return (False, False)


def clone_probe_with_language_mismatch(
  probe: ProbeResult, expected_lang: str
) -> ProbeResult:
  """Return a slot-scoped demoted `ProbeResult` for audio mismatch."""
  exp = normalize_audio_language_code(expected_lang) or expected_lang
  tracks = ",".join(probe.audio_languages) or "unknown"
  return replace(
    probe,
    ok=True,
    audio_language_mismatch=True,
    error=f"{AUDIO_LANG_MISMATCH_PREFIX} ({tracks} != {exp}); demoted fallback",
  )


def strip_slot_language_mismatch(probe: ProbeResult) -> ProbeResult:
  """Recover stream health if `probe` was flagged only for audio language."""
  if not probe.black_screen and (
    probe.audio_language_mismatch
    or (
      not probe.ok
      and probe.error is not None
      and probe.error.startswith(AUDIO_LANG_MISMATCH_PREFIX)
    )
  ):
    return replace(probe, ok=True, audio_language_mismatch=False, error=None)
  return probe


def parse_blackdetect_duration(stderr_text: str) -> float:
  """Sum `black_duration:<sec>` intervals reported by `ffmpeg`."""
  total = 0.0
  for match in BLACK_DURATION_RE.finditer(stderr_text):
    try:
      total += float(match.group(1))
    except ValueError:
      continue
  return total


def effective_concurrency(
  config: AppConfig, account: XtreamAccountInfo | None = None
) -> int:
  """Compute safe probe concurrency respecting `max_connections`."""
  desired = max(1, config.validation.concurrency)
  if config.validation.ignore_account_limit or account is None:
    return desired
  return max(1, min(desired, max(1, account.max_connections)))


def has_mpegts_sync(data: bytes) -> bool:
  """Verify 188-byte MPEG-TS `0x47` sync cadence in an initial buffer."""
  n = len(data)
  if n < TS_PACKET_SIZE * 2:
    return False
  limit = min(n - TS_PACKET_SIZE * 2, TS_PACKET_SIZE * 3)
  for offset in range(max(1, limit)):
    if (
      data[offset] == TS_SYNC_BYTE
      and data[offset + TS_PACKET_SIZE] == TS_SYNC_BYTE
    ):
      if offset + TS_PACKET_SIZE * 2 < n:
        if data[offset + TS_PACKET_SIZE * 2] == TS_SYNC_BYTE:
          return True
      else:
        return True
  return False


def _looks_like_fmp4_or_flv(data: bytes) -> bool:
  if len(data) >= 8 and data[4:8] in {b"ftyp", b"moof", b"styp"}:
    return True
  return bool(len(data) >= 3 and data[:3] == b"FLV")


def _looks_like_error_payload(data: bytes, content_type: str) -> bool:
  ct = content_type.lower()
  if "text/html" in ct or "application/json" in ct:
    return True
  head = data[:256].lstrip().lower()
  return head.startswith(
    (b"<!doctype", b"<html", b'{"user_info"', b'{"error"', b"error:")
  )


def _extract_first_hls_uri(manifest_text: str) -> tuple[str | None, bool]:
  """Return (first_uri, is_master_playlist) from an M3U8 manifest."""
  is_master = "#EXT-X-STREAM-INF" in manifest_text
  for raw_line in manifest_text.splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
      continue
    return line, is_master
  return None, is_master


def _parse_fps(rate_str: str | None) -> float | None:
  if not rate_str or rate_str in {"0/0", "0"}:
    return None
  if "/" in rate_str:
    num_s, den_s = rate_str.split("/", 1)
    try:
      num, den = float(num_s), float(den_s)
      return round(num / den, 2) if den > 0 else None
    except ValueError:
      return None
  try:
    val = float(rate_str)
    return round(val, 2) if val > 0 else None
  except ValueError:
    return None


class StreamValidator:
  """Validates candidate live streams via HTTP/HLS and ffprobe stages."""

  def __init__(
    self,
    config: AppConfig,
    account: XtreamAccountInfo | None = None,
    *,
    pipeline_account: UserAccount | None = None,
    session_factory: ClientSessionFactory | None = None,
  ) -> None:
    self.config = config
    self.val_cfg = config.validation
    self.pipeline_account: UserAccount | None = (
      pipeline_account or config.pipeline_account
    )
    self._upstream_secret: str = (
      self.pipeline_account.upstream_password
      if self.pipeline_account is not None
      else ""
    )
    self.concurrency = effective_concurrency(config, account)
    self._semaphore = asyncio.Semaphore(self.concurrency)
    self._session_factory: ClientSessionFactory = (
      session_factory or default_session_factory
    )
    self._probe_cache: dict[int, ProbeResult] = {}
    self._inflight: dict[int, asyncio.Future[ProbeResult]] = {}
    self.ffprobe_path: str | None = (
      shutil.which("ffprobe") if self.val_cfg.use_ffprobe else None
    )
    self.ffmpeg_path: str | None = (
      shutil.which("ffmpeg")
      if (self.val_cfg.use_ffprobe and self.val_cfg.detect_black_screen)
      else None
    )
    if self.val_cfg.use_ffprobe and not self.ffprobe_path:
      logger.warning(
        "ffprobe not found on PATH; proceeding with Stage 1 HTTP/MPEG-TS/HLS stream checks only."
      )

  def build_stream_url(self, stream: XtreamStream) -> str:
    xt = self.config.xtream
    acct = self.pipeline_account
    if (
      xt.server_url
      and acct is not None
      and acct.upstream_username
      and acct.upstream_password
    ):
      return stream.build_url(
        xt.server_url,
        acct.upstream_username,
        acct.upstream_password,
        xt.stream_format,
      )
    if stream.direct_source:
      return stream.direct_source
    return stream.build_url(
      xt.server_url or "http://localhost",
      acct.upstream_username if acct is not None else "user",
      acct.upstream_password if acct is not None else "pass",
      xt.stream_format,
    )

  async def validate_mappings(
    self,
    mappings: list[MappedChannel],
    *,
    on_channel_done: ProgressCallback | None = None,
  ) -> list[MappedChannel]:
    """Validate ranked candidates per channel, stopping on first winner."""
    timeout = aiohttp.ClientTimeout(
      total=None,
      connect=min(5.0, self.val_cfg.http_timeout_sec),
      sock_read=self.val_cfg.http_timeout_sec,
    )
    async with self._session_factory(
      timeout=timeout,
      headers={"User-Agent": STREAM_USER_AGENT, "Icy-MetaData": "0"},
    ) as client:
      total = len(mappings)
      completed = 0
      queue: asyncio.Queue[MappedChannel] = asyncio.Queue()
      for item in mappings:
        queue.put_nowait(item)

      async def _worker() -> None:
        nonlocal completed
        while not queue.empty():
          try:
            mapped = queue.get_nowait()
          except asyncio.QueueEmpty:
            return
          try:
            await self._validate_single_channel(mapped, client)
          finally:
            completed += 1
            if on_channel_done is not None:
              on_channel_done(mapped, completed, total)
            queue.task_done()

      worker_count = min(max(1, self.concurrency), max(1, total))
      workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
      await asyncio.gather(*workers)

    return mappings

  async def _validate_single_channel(
    self,
    mapped: MappedChannel,
    client: aiohttp.ClientSession,
  ) -> None:
    if not mapped.candidates:
      return

    max_tries = self.val_cfg.max_candidates_per_channel
    last_probe: ProbeResult | None = None
    fallback_mismatch: tuple[MatchCandidate, ProbeResult] | None = None
    allow_audio_only = (
      "audio" in mapped.channel.group.lower()
      or "music" in mapped.channel.group.lower()
    )

    pinned_id = self.config.lineup.pinned_streams.get(mapped.channel.chno)
    if pinned_id is None and mapped.channel.sd_chno is not None:
      pinned_id = self.config.lineup.pinned_streams.get(mapped.channel.sd_chno)

    for candidate in mapped.candidates[:max_tries]:
      mapped.probes_attempted += 1
      probe = await self.probe_stream(
        candidate.stream,
        client,
        allow_audio_only=allow_audio_only,
      )
      if not probe.ok:
        last_probe = probe
        continue

      if (
        self.val_cfg.check_audio_language
        and candidate.stream.stream_id != pinned_id
      ):
        compat, _ = check_audio_language_compatibility(
          mapped.channel.language,
          probe.audio_languages,
          allow_audio_only=allow_audio_only,
        )
        if not compat:
          demoted_probe = clone_probe_with_language_mismatch(
            probe, mapped.channel.language
          )
          if fallback_mismatch is None:
            fallback_mismatch = (candidate, demoted_probe)
          last_probe = demoted_probe
          continue

      mapped.selected = candidate
      mapped.probe = probe
      return

    # If no language-compatible stream succeeded, fall back to the
    # highest-ranked
    # working stream that was demoted solely for audio-track language metadata.
    if fallback_mismatch is not None:
      mapped.selected, mapped.probe = fallback_mismatch
      return

    # Record last failed probe for diagnostics if all candidates failed
    mapped.probe = last_probe

  async def probe_stream(
    self,
    stream: XtreamStream,
    client: aiohttp.ClientSession,
    *,
    allow_audio_only: bool = False,
  ) -> ProbeResult:
    """Probe one Xtream stream (deduplicated across sharing channels)."""
    sid = stream.stream_id
    if sid in self._probe_cache:
      return self._probe_cache[sid]

    if sid in self._inflight:
      return await self._inflight[sid]

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[ProbeResult] = loop.create_future()
    self._inflight[sid] = fut

    try:
      async with self._semaphore:
        try:
          result = await self._execute_two_stage_probe(
            stream,
            client,
            allow_audio_only=allow_audio_only,
          )
        finally:
          if self.val_cfg.inter_probe_delay_sec > 0:
            await asyncio.sleep(self.val_cfg.inter_probe_delay_sec)
      self._probe_cache[sid] = result
      fut.set_result(result)
      return result
    except Exception as exc:
      fallback = ProbeResult(
        stream_id=sid,
        url=self.build_stream_url(stream),
        ok=False,
        error=redact_url_secrets(
          f"Unexpected probe error: {exc}",
          secret=self._upstream_secret,
        ),
        checked_at=datetime.now(timezone.utc).isoformat(),
      )
      self._probe_cache[sid] = fallback
      if not fut.done():
        fut.set_result(fallback)
      return fallback
    finally:
      self._inflight.pop(sid, None)

  async def _execute_two_stage_probe(
    self,
    stream: XtreamStream,
    client: aiohttp.ClientSession,
    *,
    allow_audio_only: bool = False,
  ) -> ProbeResult:
    url = self.build_stream_url(stream)
    result = await self._stage1_http_check(stream.stream_id, url, client)
    if not result.ok:
      return result

    if self.ffprobe_path:
      # Give max_connections=1 Xtream panels time to release Stage 1's tuner
      # slot
      if self.val_cfg.inter_probe_delay_sec > 0:
        await asyncio.sleep(self.val_cfg.inter_probe_delay_sec)
      await self._stage2_ffprobe_check(
        result, allow_audio_only=allow_audio_only
      )

    if (
      result.ok
      and self.ffmpeg_path
      and self.val_cfg.detect_black_screen
      and not allow_audio_only
      and result.video_codec
    ):
      if self.val_cfg.inter_probe_delay_sec > 0:
        await asyncio.sleep(self.val_cfg.inter_probe_delay_sec)
      await self._stage3_black_screen_check(result)

    return result

  async def _read_initial_stream_response(
    self,
    stream_id: int,
    url: str,
    client: aiohttp.ClientSession,
    *,
    checked_at: str,
    t0: float,
    min_bytes: int,
    _hls_depth: int,
  ) -> tuple[ProbeResult | None, int, str, str, bool, bytes]:
    try:
      async with client.get(upstream_url(url)) as resp:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        status = resp.status
        content_type = resp.headers.get("Content-Type", "")
        final_url = str(resp.url)

        if status not in (200, 206):
          return (
            ProbeResult(
              stream_id=stream_id,
              url=url,
              ok=False,
              http_status=status,
              content_type=content_type,
              latency_ms=latency_ms,
              error=f"HTTP {status}",
              checked_at=checked_at,
            ),
            status,
            content_type,
            final_url,
            False,
            b"",
          )

        path_lower = final_url.split("?", 1)[0].lower()
        # Reject Xtream panel offline placeholder / dummy loop redirects.
        if _hls_depth == 0 and (
          any(
            marker in path_lower
            for marker in (
              "/black.ts",
              "/offline.ts",
              "/dummy.ts",
              "/unavailable.ts",
            )
          )
          or (
            not path_lower.endswith(".m3u8")
            and resp.headers.get("Last-Modified") is not None
            and resp.headers.get("ETag") is not None
            and resp.headers.get("Content-Length") is not None
          )
        ):
          return (
            ProbeResult(
              stream_id=stream_id,
              url=url,
              ok=False,
              http_status=status,
              content_type=content_type,
              latency_ms=latency_ms,
              error=redact_url_secrets(
                f"Redirected to static offline placeholder ({final_url})",
                secret=self._upstream_secret,
              ),
              checked_at=checked_at,
            ),
            status,
            content_type,
            final_url,
            False,
            b"",
          )

        is_hls_hint = "mpegurl" in content_type.lower() or path_lower.endswith(
          ".m3u8"
        )
        buf = bytearray()
        read_limit = (
          65536 if is_hls_hint else max(min_bytes, TS_PACKET_SIZE * 4)
        )

        async def _read_initial() -> None:
          async for chunk in resp.content.iter_chunked(4096):
            if chunk:
              buf.extend(chunk)
              if len(buf) >= read_limit:
                break

        await asyncio.wait_for(
          _read_initial(), timeout=self.val_cfg.http_timeout_sec
        )
        return (None, status, content_type, final_url, is_hls_hint, bytes(buf))
    except asyncio.TimeoutError:
      return (
        ProbeResult(
          stream_id=stream_id,
          url=url,
          ok=False,
          latency_ms=(time.perf_counter() - t0) * 1000.0,
          error="Stream read timed out",
          checked_at=checked_at,
        ),
        0,
        "",
        url,
        False,
        b"",
      )
    except aiohttp.ClientError as exc:
      return (
        ProbeResult(
          stream_id=stream_id,
          url=url,
          ok=False,
          latency_ms=(time.perf_counter() - t0) * 1000.0,
          error=redact_url_secrets(
            f"HTTP error: {exc}",
            secret=self._upstream_secret,
          ),
          checked_at=checked_at,
        ),
        0,
        "",
        url,
        False,
        b"",
      )

  async def _classify_stage1_hls(
    self,
    stream_id: int,
    url: str,
    client: aiohttp.ClientSession,
    *,
    status: int,
    content_type: str,
    final_url: str,
    data: bytes,
    latency_ms: float,
    checked_at: str,
    _hls_depth: int,
  ) -> ProbeResult:
    manifest_text = data.decode("utf-8", errors="replace")
    if "#EXTM3U" not in manifest_text:
      return ProbeResult(
        stream_id=stream_id,
        url=url,
        ok=False,
        http_status=status,
        content_type=content_type,
        bytes_read=len(data),
        latency_ms=latency_ms,
        is_hls=True,
        error="Invalid HLS manifest (missing #EXTM3U)",
        checked_at=checked_at,
      )

    child_uri, _ = _extract_first_hls_uri(manifest_text)
    if not child_uri:
      return ProbeResult(
        stream_id=stream_id,
        url=url,
        ok=False,
        http_status=status,
        content_type=content_type,
        bytes_read=len(data),
        latency_ms=latency_ms,
        is_hls=True,
        error="Empty HLS manifest (no variant or segment URIs)",
        checked_at=checked_at,
      )

    if _hls_depth >= 2:
      return ProbeResult(
        stream_id=stream_id,
        url=url,
        ok=True,
        http_status=status,
        content_type=content_type,
        bytes_read=len(data),
        latency_ms=latency_ms,
        is_hls=True,
        checked_at=checked_at,
      )

    resolved_child = urljoin(final_url, child_uri)
    seg_probe = await self._stage1_http_check(
      stream_id,
      resolved_child,
      client,
      _hls_depth=_hls_depth + 1,
    )
    seg_probe.url = url
    seg_probe.is_hls = True
    seg_probe.latency_ms = latency_ms + seg_probe.latency_ms
    return seg_probe

  async def _stage1_http_check(
    self,
    stream_id: int,
    url: str,
    client: aiohttp.ClientSession,
    *,
    _hls_depth: int = 0,
  ) -> ProbeResult:
    checked_at = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()
    min_bytes = self.val_cfg.min_bytes_to_read

    (
      early,
      status,
      content_type,
      final_url,
      is_hls_hint,
      data,
    ) = await self._read_initial_stream_response(
      stream_id,
      url,
      client,
      checked_at=checked_at,
      t0=t0,
      min_bytes=min_bytes,
      _hls_depth=_hls_depth,
    )
    if early is not None:
      return early

    latency_ms = (time.perf_counter() - t0) * 1000.0
    if not data:
      return ProbeResult(
        stream_id=stream_id,
        url=url,
        ok=False,
        http_status=status,
        content_type=content_type,
        latency_ms=latency_ms,
        error="Empty stream response (0 bytes)",
        checked_at=checked_at,
      )

    if is_hls_hint or data.lstrip().startswith(b"#EXTM3U"):
      return await self._classify_stage1_hls(
        stream_id,
        url,
        client,
        status=status,
        content_type=content_type,
        final_url=final_url,
        data=data,
        latency_ms=latency_ms,
        checked_at=checked_at,
        _hls_depth=_hls_depth,
      )

    if _looks_like_error_payload(data, content_type):
      return ProbeResult(
        stream_id=stream_id,
        url=url,
        ok=False,
        http_status=status,
        content_type=content_type,
        bytes_read=len(data),
        latency_ms=latency_ms,
        error=f"Non-media payload returned ({content_type or 'text/json'})",
        checked_at=checked_at,
      )

    ts_ok = has_mpegts_sync(data)
    container_ok = ts_ok or _looks_like_fmp4_or_flv(data)
    enough_bytes = len(data) >= min_bytes or (_hls_depth > 0 and container_ok)
    if not enough_bytes:
      return ProbeResult(
        stream_id=stream_id,
        url=url,
        ok=False,
        http_status=status,
        content_type=content_type,
        bytes_read=len(data),
        latency_ms=latency_ms,
        ts_sync_verified=ts_ok,
        error=f"Insufficient stream bytes ({len(data)} < {min_bytes})",
        checked_at=checked_at,
      )

    if not container_ok:
      return ProbeResult(
        stream_id=stream_id,
        url=url,
        ok=False,
        http_status=status,
        content_type=content_type,
        bytes_read=len(data),
        latency_ms=latency_ms,
        ts_sync_verified=False,
        error="Unrecognized media stream header (missing MPEG-TS 0x47 sync)",
        checked_at=checked_at,
      )

    return ProbeResult(
      stream_id=stream_id,
      url=url,
      ok=True,
      http_status=status,
      content_type=content_type,
      bytes_read=len(data),
      latency_ms=latency_ms,
      is_hls=_hls_depth > 0,
      ts_sync_verified=ts_ok,
      checked_at=checked_at,
    )

  async def _stage2_ffprobe_check(
    self,
    probe: ProbeResult,
    *,
    allow_audio_only: bool = False,
  ) -> None:
    assert self.ffprobe_path is not None
    cmd = [
      self.ffprobe_path,
      "-v",
      "error",
      "-print_format",
      "json",
      "-show_streams",
      "-analyzeduration",
      "3000000",
      "-probesize",
      "2000000",
      "-protocol_whitelist",
      FFMPEG_PROTOCOL_WHITELIST,
      "-user_agent",
      STREAM_USER_AGENT,
      "-i",
      probe.url,
    ]

    proc: asyncio.subprocess.Process | None = None
    try:
      proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
      )
      stdout, stderr = await asyncio.wait_for(
        proc.communicate(),
        timeout=self.val_cfg.ffprobe_timeout_sec,
      )
    except asyncio.TimeoutError:
      if proc is not None and proc.returncode is None:
        proc.kill()
        await proc.wait()
      probe.ffprobe_checked = True
      probe.ok = False
      probe.error = "ffprobe timed out"
      return
    except OSError as exc:
      logger.warning(
        "Failed to execute ffprobe (%s); keeping Stage 1 result.", exc
      )
      return

    probe.ffprobe_checked = True
    if proc.returncode != 0:
      err_msg = stderr.decode("utf-8", errors="replace").strip().splitlines()
      short_err = err_msg[-1] if err_msg else f"exit code {proc.returncode}"
      probe.ok = False
      probe.error = redact_url_secrets(
        f"ffprobe failed: {short_err}",
        secret=self._upstream_secret,
      )
      return

    try:
      payload = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    except ValueError:
      probe.ok = False
      probe.error = "ffprobe returned invalid JSON"
      return

    streams = payload.get("streams") or []
    video = next(
      (
        s
        for s in streams
        if isinstance(s, Mapping) and s.get("codec_type") == "video"
      ),
      None,
    )
    audio_streams = [
      s
      for s in streams
      if isinstance(s, Mapping) and s.get("codec_type") == "audio"
    ]

    if video:
      probe.video_codec = (
        str(video["codec_name"]) if video.get("codec_name") else None
      )
      if video.get("width"):
        probe.width = int(video["width"])
      if video.get("height"):
        probe.height = int(video["height"])
      probe.fps = _parse_fps(video.get("avg_frame_rate")) or _parse_fps(
        video.get("r_frame_rate")
      )
    if audio_streams:
      first_codec = audio_streams[0].get("codec_name")
      probe.audio_codec = str(first_codec) if first_codec else None
      seen_langs: list[str] = []
      for a_stream in audio_streams:
        tags = a_stream.get("tags")
        if isinstance(tags, Mapping):
          raw_tag_lang: str | None = None
          for k, v in tags.items():
            if str(k).lower() == "language" and v:
              raw_tag_lang = str(v)
              break
          norm_lang = normalize_audio_language_code(raw_tag_lang)
          if norm_lang and norm_lang not in seen_langs:
            seen_langs.append(norm_lang)
      probe.audio_languages = seen_langs

    if not probe.video_codec and not (allow_audio_only and probe.audio_codec):
      probe.ok = False
      probe.error = "ffprobe found no decodable video stream"

  async def _stage3_black_screen_check(self, probe: ProbeResult) -> None:
    """Decode a short video sample via `ffmpeg` and reject black feeds."""
    assert self.ffmpeg_path is not None
    sample_sec = max(0.5, float(self.val_cfg.black_sample_sec))
    min_black_dur = round(sample_sec * 0.85, 2)
    cmd = [
      self.ffmpeg_path,
      "-hide_banner",
      "-nostats",
      "-loglevel",
      "info",
      "-protocol_whitelist",
      FFMPEG_PROTOCOL_WHITELIST,
      "-user_agent",
      STREAM_USER_AGENT,
      "-t",
      f"{sample_sec:.2f}",
      "-i",
      probe.url,
      "-an",
      "-sn",
      "-dn",
      "-vf",
      f"blackdetect=d={min_black_dur:.2f}:pix_th=0.10:pic_th=0.98",
      "-f",
      "null",
      "-",
    ]

    proc: asyncio.subprocess.Process | None = None
    try:
      proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
      )
      _stdout, stderr = await asyncio.wait_for(
        proc.communicate(),
        timeout=self.val_cfg.ffprobe_timeout_sec,
      )
    except asyncio.TimeoutError:
      if proc is not None and proc.returncode is None:
        proc.kill()
        await proc.wait()
      logger.warning(
        "ffmpeg blackdetect timed out on stream #%s; keeping Stage 2 result.",
        probe.stream_id,
      )
      return
    except OSError as exc:
      logger.warning(
        "Failed to execute ffmpeg blackdetect (%s); keeping Stage 2 result.",
        exc,
      )
      return

    stderr_text = stderr.decode("utf-8", errors="replace")
    total_black = parse_blackdetect_duration(stderr_text)
    if total_black >= sample_sec * 0.85:
      probe.black_screen = True
      probe.ok = False
      probe.error = f"Black screen stream detected ({total_black:.2f}s/{sample_sec:.2f}s black)"
