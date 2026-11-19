"""Two-stage async IPTV stream validator (HTTP/HLS/MPEG-TS check + optional ffprobe)."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urljoin

import aiohttp

from bellhop.config import AppConfig
from bellhop.models import (
  MappedChannel,
  ProbeResult,
  XtreamAccountInfo,
  XtreamStream,
)
from bellhop_web.http_utils import (
  ClientSessionFactory,
  default_session_factory,
  upstream_url,
)

logger = logging.getLogger(__name__)

STREAM_USER_AGENT = "VLC/3.0.20 LibVLC/3.0.20"
TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47

ProgressCallback = Callable[[MappedChannel, int, int], None]


def effective_concurrency(
  config: AppConfig, account: XtreamAccountInfo | None = None
) -> int:
  """Compute safe stream probe concurrency respecting Xtream account max_connections."""
  desired = max(1, config.validation.concurrency)
  if config.validation.ignore_account_limit or account is None:
    return desired
  return max(1, min(desired, max(1, account.max_connections)))


def has_mpegts_sync(data: bytes) -> bool:
  """Verify 188-byte MPEG-TS 0x47 sync byte cadence inside an initial byte buffer."""
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
  """Validates candidate Xtream live streams using Stage 1 HTTP/HLS + Stage 2 ffprobe."""

  def __init__(
    self,
    config: AppConfig,
    account: XtreamAccountInfo | None = None,
    *,
    session_factory: ClientSessionFactory | None = None,
  ) -> None:
    self.config = config
    self.val_cfg = config.validation
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
    if self.val_cfg.use_ffprobe and not self.ffprobe_path:
      logger.warning(
        "ffprobe not found on PATH; proceeding with Stage 1 HTTP/MPEG-TS/HLS stream checks only."
      )

  def build_stream_url(self, stream: XtreamStream) -> str:
    xt = self.config.xtream
    if xt.server_url and xt.username and xt.password:
      return stream.build_url(
        xt.server_url,
        xt.username,
        xt.password,
        xt.stream_format,
      )
    if stream.direct_source:
      return stream.direct_source
    return stream.build_url(
      xt.server_url or "http://localhost",
      xt.username or "user",
      xt.password or "pass",
      xt.stream_format,
    )

  async def validate_mappings(
    self,
    mappings: list[MappedChannel],
    *,
    on_channel_done: ProgressCallback | None = None,
  ) -> list[MappedChannel]:
    """Validate ranked candidates per channel, stopping at the first working stream."""
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
    allow_audio_only = (
      "audio" in mapped.channel.group.lower()
      or "music" in mapped.channel.group.lower()
    )

    for candidate in mapped.candidates[:max_tries]:
      mapped.probes_attempted += 1
      probe = await self.probe_stream(
        candidate.stream,
        client,
        allow_audio_only=allow_audio_only,
      )
      last_probe = probe
      if probe.ok:
        mapped.selected = candidate
        mapped.probe = probe
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
    """Probe a single Xtream stream (deduplicated across channels sharing a candidate)."""
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
        error=f"Unexpected probe error: {exc}",
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
      # Give max_connections=1 Xtream panels time to release Stage 1's tuner slot
      if self.val_cfg.inter_probe_delay_sec > 0:
        await asyncio.sleep(self.val_cfg.inter_probe_delay_sec)
      await self._stage2_ffprobe_check(
        result, allow_audio_only=allow_audio_only
      )

    return result

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

    try:
      async with client.get(upstream_url(url)) as resp:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        status = resp.status
        content_type = resp.headers.get("Content-Type", "")
        final_url = str(resp.url)

        if status not in (200, 206):
          return ProbeResult(
            stream_id=stream_id,
            url=url,
            ok=False,
            http_status=status,
            content_type=content_type,
            latency_ms=latency_ms,
            error=f"HTTP {status}",
            checked_at=checked_at,
          )

        path_lower = final_url.split("?", 1)[0].lower()
        # Detect Xtream panel offline placeholder / dummy loop redirects (e.g. /video/black.ts)
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
          return ProbeResult(
            stream_id=stream_id,
            url=url,
            ok=False,
            http_status=status,
            content_type=content_type,
            latency_ms=latency_ms,
            error=f"Redirected to static offline placeholder ({final_url})",
            checked_at=checked_at,
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
        data = bytes(buf)

    except asyncio.TimeoutError:
      return ProbeResult(
        stream_id=stream_id,
        url=url,
        ok=False,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        error="Stream read timed out",
        checked_at=checked_at,
      )
    except aiohttp.ClientError as exc:
      return ProbeResult(
        stream_id=stream_id,
        url=url,
        ok=False,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        error=f"HTTP error: {exc}",
        checked_at=checked_at,
      )

    # Notice: `async with client.stream(...)` has exited cleanly here, closing the connection
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

    # Handle HLS playlists (#EXTM3U) by following variant/segment up to depth 2
    if is_hls_hint or data.lstrip().startswith(b"#EXTM3U"):
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

    # HLS segments might theoretically be small in mock tests, while live continuous streams
    # must deliver at least min_bytes_to_read
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
      "-user_agent",
      STREAM_USER_AGENT,
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
      probe.error = f"ffprobe failed: {short_err}"
      return

    try:
      payload = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    except ValueError:
      probe.ok = False
      probe.error = "ffprobe returned invalid JSON"
      return

    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video:
      probe.video_codec = video.get("codec_name")
      if video.get("width"):
        probe.width = int(video["width"])
      if video.get("height"):
        probe.height = int(video["height"])
      probe.fps = _parse_fps(video.get("avg_frame_rate")) or _parse_fps(
        video.get("r_frame_rate")
      )
    if audio:
      probe.audio_codec = audio.get("codec_name")

    if not probe.video_codec and not (allow_audio_only and probe.audio_codec):
      probe.ok = False
      probe.error = "ffprobe found no decodable video stream"
