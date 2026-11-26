"""In-process Fake Xtream Codes HTTP/1.1 server for realistic end-to-end tests.

Emulates:
- `player_api.php` (auth, `get_live_categories`, `get_live_streams`,
  `max_connections`)
- `xmltv.php` (XMLTV & optional gzipped XMLTV with configurable live/upcoming
  programmes)
- `/live/<user>/<pass>/<stream_id>.ts` (continuous chunked MPEG-TS streaming)
- `/live/<user>/<pass>/<stream_id>.m3u8` (302 redirect to edge manifest +
  segment delivery)
- Real provider quirks:
  - HTTP 513 on wrong or masked (`***`) passwords
  - 302 redirect to `/video/black.ts` static Apache placeholder for dead streams
  - Strict concurrent live-stream connection tracking (`max_connections=1`
    enforcement)
"""

from __future__ import annotations

import asyncio
import gzip
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


def make_valid_mpegts_chunk(
  packet_count: int = 32, *, marker_byte: int = 0xAA
) -> bytes:
  """Build valid 188-byte MPEG-TS packets with `0x47` sync bytes."""
  pkt = bytes([0x47, 0x40, 0x00, 0x10]) + bytes([marker_byte]) * 184
  return pkt * packet_count


def format_xmltv_ts(epoch: int) -> str:
  return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
    "%Y%m%d%H%M%S +0000"
  )


@dataclass(slots=True)
class FakeProgram:
  channel_id: str
  title: str
  start_ts: int
  stop_ts: int
  description: str = ""
  sub_title: str = ""
  category: str = ""


@dataclass(slots=True)
class FakeStreamDef:
  stream_id: int
  name: str
  category_id: str
  epg_channel_id: str = ""
  stream_icon: str = ""
  num: int = 0
  offline_placeholder: bool = False
  http_error_status: int | None = None
  marker_byte: int = 0xAA


@dataclass
class FakeXtreamServer:
  """Async localhost Xtream Codes server for testing `bellhop` end-to-end."""

  username: str = "test_user"
  password: str = "test_pass"
  max_connections: int = 1
  enforce_max_connections: bool = True
  categories: list[dict[str, Any]] = field(default_factory=list)
  streams: list[FakeStreamDef] = field(default_factory=list)
  programs: list[FakeProgram] = field(default_factory=list)
  gzip_xmltv: bool = False
  ts_chunk_interval_sec: float = 0.02
  ts_chunks_per_stream: int | None = (
    None  # None = stream continuously until client disconnects
  )

  # Runtime telemetry
  host: str = "127.0.0.1"
  public_host: str | None = None
  port: int = 0
  _server: asyncio.AbstractServer | None = field(
    default=None, init=False, repr=False
  )
  active_live_streams: int = field(default=0, init=False)
  peak_concurrent_streams: int = field(default=0, init=False)
  connection_limit_violations: int = field(default=0, init=False)
  xmltv_requests: int = field(default=0, init=False)
  stream_requests: list[int] = field(default_factory=list, init=False)
  hls_manifest_requests: list[str] = field(default_factory=list, init=False)
  request_log: list[str] = field(default_factory=list, init=False)
  _edge_seq: int = field(default=0, init=False)

  @property
  def advertised_host(self) -> str:
    return self.public_host or self.host

  @property
  def base_url(self) -> str:
    return f"http://{self.advertised_host}:{self.port}"

  @property
  def xmltv_url(self) -> str:
    return f"{self.base_url}/xmltv.php?username={self.username}&password={self.password}"

  def stream_url(self, stream_id: int, ext: str = "ts") -> str:
    return (
      f"{self.base_url}/live/{self.username}/{self.password}/{stream_id}.{ext}"
    )

  async def start(self) -> FakeXtreamServer:
    self._server = await asyncio.start_server(
      self._handle_connection,
      host=self.host,
      port=self.port,
    )
    sock = self._server.sockets[0]
    self.port = sock.getsockname()[1]
    return self

  async def stop(self) -> None:
    if self._server is not None:
      self._server.close()
      await self._server.wait_closed()
      self._server = None

  async def __aenter__(self) -> FakeXtreamServer:
    return await self.start()

  async def __aexit__(self, *_exc: Any) -> None:
    await self.stop()

  def _find_stream(self, stream_id: int) -> FakeStreamDef | None:
    for s in self.streams:
      if s.stream_id == stream_id:
        return s
    return None

  def _render_xmltv(self) -> bytes:
    lines = [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<tv generator-info-name="FakeXtream">',
    ]
    seen_cids: set[str] = set()
    for s in self.streams:
      if s.epg_channel_id and s.epg_channel_id not in seen_cids:
        seen_cids.add(s.epg_channel_id)
        lines.append(f'  <channel id="{s.epg_channel_id}">')
        lines.append(f"    <display-name>{s.name}</display-name>")
        lines.append("  </channel>")
    for p in self.programs:
      lines.append(
        f'  <programme start="{format_xmltv_ts(p.start_ts)}" '
        f'stop="{format_xmltv_ts(p.stop_ts)}" channel="{p.channel_id}">'
      )
      lines.append(f"    <title>{p.title}</title>")
      if p.sub_title:
        lines.append(f"    <sub-title>{p.sub_title}</sub-title>")
      if p.description:
        lines.append(f"    <desc>{p.description}</desc>")
      if p.category:
        lines.append(f"    <category>{p.category}</category>")
      lines.append("  </programme>")
    lines.append("</tv>\n")
    raw = "\n".join(lines).encode("utf-8")
    return gzip.compress(raw) if self.gzip_xmltv else raw

  async def _handle_connection(
    self,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
  ) -> None:
    try:
      req_line_bytes = await asyncio.wait_for(reader.readline(), timeout=10.0)
      if not req_line_bytes:
        return
      parts = (
        req_line_bytes.decode("latin-1", errors="replace").strip().split(" ")
      )
      if len(parts) < 2:
        return
      method, raw_target = parts[0].upper(), parts[1]

      headers: dict[str, str] = {}
      while True:
        hline = await asyncio.wait_for(reader.readline(), timeout=10.0)
        if not hline or hline in (b"\r\n", b"\n"):
          break
        decoded = hline.decode("latin-1", errors="replace").strip()
        if ":" in decoded:
          k, v = decoded.split(":", 1)
          headers[k.strip().lower()] = v.strip()

      content_len = int(headers.get("content-length", "0") or "0")
      if content_len > 0:
        await asyncio.wait_for(
          reader.readexactly(min(content_len, 65536)), timeout=5.0
        )

      parsed = urlsplit(raw_target)
      path = unquote(parsed.path)
      query = parse_qs(parsed.query)
      self.request_log.append(f"{method} {path}")

      await self._route(method, path, query, writer)
    except (
      asyncio.TimeoutError,
      asyncio.IncompleteReadError,
      ConnectionResetError,
      BrokenPipeError,
      asyncio.CancelledError,
    ):
      pass
    finally:
      try:
        if not writer.is_closing():
          writer.close()
          await writer.wait_closed()
      except Exception:
        pass

  async def _route(
    self,
    method: str,
    path: str,
    query: dict[str, list[str]],
    writer: asyncio.StreamWriter,
  ) -> None:
    if path == "/player_api.php":
      await self._handle_player_api(query, writer)
      return

    if path == "/xmltv.php":
      await self._handle_xmltv(query, writer)
      return

    if path == "/video/black.ts":
      # Static Apache-style offline placeholder matching upstream panel quirks
      body = make_valid_mpegts_chunk(4, marker_byte=0x00)
      await self._send_raw(
        writer,
        200,
        "OK",
        body,
        "video/mp2t",
        extra_headers={
          "ETag": '"dead-black-ts"',
          "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT",
        },
      )
      return

    m_live = re.match(r"^/live/([^/]+)/([^/]+)/(\d+)\.(ts|m3u8)$", path)
    if m_live:
      user, passwd, sid_str, ext = m_live.groups()
      if user != self.username or passwd != self.password:
        await self._send_raw(
          writer, 513, "Invalid Credentials", b"Bad credentials", "text/plain"
        )
        return
      await self._handle_live_stream(int(sid_str), ext, method, writer)
      return

    m_edge_m3u8 = re.match(r"^/edge/([^/]+)/(\d+)\.m3u8$", path)
    if m_edge_m3u8:
      token, sid_str = m_edge_m3u8.groups()
      self.hls_manifest_requests.append(path)
      manifest = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:4\n"
        "#EXT-X-MEDIA-SEQUENCE:1\n"
        "#EXTINF:4.0,\n"
        f"seg_{sid_str}_001.ts\n"
      ).encode()
      await self._send_raw(
        writer,
        200,
        "OK",
        manifest,
        "application/vnd.apple.mpegurl",
      )
      return

    m_edge_seg = re.match(r"^/edge/([^/]+)/seg_(\d+)_\d+\.ts$", path)
    if m_edge_seg:
      _token, sid_str = m_edge_seg.groups()
      sdef = self._find_stream(int(sid_str))
      marker = sdef.marker_byte if sdef else 0xCC
      await self._send_raw(
        writer,
        200,
        "OK",
        make_valid_mpegts_chunk(16, marker_byte=marker),
        "video/mp2t",
      )
      return

    await self._send_raw(writer, 404, "Not Found", b"Not Found", "text/plain")

  async def _handle_player_api(
    self,
    query: dict[str, list[str]],
    writer: asyncio.StreamWriter,
  ) -> None:
    user = (query.get("username") or [""])[0]
    passwd = (query.get("password") or [""])[0]
    if user != self.username or passwd != self.password:
      await self._send_json(writer, 401, {"user_info": {"auth": 0}})
      return

    action = (query.get("action") or [""])[0]
    if action == "get_live_categories":
      await self._send_json(writer, 200, self.categories)
      return
    if action == "get_live_streams":
      payload = [
        {
          "num": s.num or idx + 1,
          "name": s.name,
          "stream_type": "live",
          "stream_id": s.stream_id,
          "stream_icon": s.stream_icon,
          "epg_channel_id": s.epg_channel_id,
          "category_id": s.category_id,
          "tv_archive": 0,
        }
        for idx, s in enumerate(self.streams)
      ]
      await self._send_json(writer, 200, payload)
      return

    await self._send_json(
      writer,
      200,
      {
        "user_info": {
          "username": self.username,
          "auth": 1,
          "status": "Active",
          "is_trial": "0",
          "active_cons": str(self.active_live_streams),
          "max_connections": str(self.max_connections),
          "allowed_output_formats": ["m3u8", "ts"],
        },
        "server_info": {
          "url": self.advertised_host,
          "port": str(self.port),
          "https_port": "443",
          "server_protocol": "http",
          "timezone": "UTC",
          "timestamp_now": int(time.time()),
        },
      },
    )

  async def _handle_xmltv(
    self,
    query: dict[str, list[str]],
    writer: asyncio.StreamWriter,
  ) -> None:
    user = (query.get("username") or [""])[0]
    passwd = (query.get("password") or [""])[0]
    if user != self.username or passwd != self.password:
      await self._send_raw(
        writer, 401, "Unauthorized", b"Unauthorized", "text/plain"
      )
      return
    self.xmltv_requests += 1
    body = self._render_xmltv()
    ctype = (
      "application/gzip"
      if self.gzip_xmltv
      else "application/xml; charset=utf-8"
    )
    await self._send_raw(writer, 200, "OK", body, ctype)

  async def _handle_live_stream(
    self,
    stream_id: int,
    ext: str,
    method: str,
    writer: asyncio.StreamWriter,
  ) -> None:
    sdef = self._find_stream(stream_id)
    if sdef is None:
      await self._send_raw(
        writer, 404, "Not Found", b"Unknown stream", "text/plain"
      )
      return
    if sdef.http_error_status is not None:
      await self._send_raw(
        writer,
        sdef.http_error_status,
        "Upstream Error",
        b"Upstream stream error",
        "text/plain",
      )
      return
    if sdef.offline_placeholder:
      await self._send_raw(
        writer,
        302,
        "Found",
        b"",
        "text/plain",
        extra_headers={"Location": f"{self.base_url}/video/black.ts"},
      )
      return

    if ext == "m3u8":
      self._edge_seq += 1
      loc = f"{self.base_url}/edge/tok{self._edge_seq}/{stream_id}.m3u8"
      await self._send_raw(
        writer,
        302,
        "Found",
        b"",
        "text/plain",
        extra_headers={"Location": loc},
      )
      return

    if method == "HEAD":
      await self._send_raw(writer, 200, "OK", b"", "video/mp2t")
      return

    self.stream_requests.append(stream_id)
    if (
      self.enforce_max_connections
      and self.active_live_streams >= self.max_connections
    ):
      self.connection_limit_violations += 1
      await self._send_raw(
        writer,
        403,
        "Forbidden",
        b"Max concurrent connections exceeded",
        "text/plain",
      )
      return

    self.active_live_streams += 1
    if self.active_live_streams > self.peak_concurrent_streams:
      self.peak_concurrent_streams = self.active_live_streams

    try:
      header = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: video/mp2t\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
      ).encode("latin-1")
      writer.write(header)
      await writer.drain()

      chunk = make_valid_mpegts_chunk(32, marker_byte=sdef.marker_byte)
      sent = 0
      while not writer.is_closing():
        writer.write(f"{len(chunk):X}\r\n".encode("ascii"))
        writer.write(chunk)
        writer.write(b"\r\n")
        await writer.drain()
        sent += 1
        if (
          self.ts_chunks_per_stream is not None
          and sent >= self.ts_chunks_per_stream
        ):
          break
        await asyncio.sleep(self.ts_chunk_interval_sec)

      if not writer.is_closing():
        writer.write(b"0\r\n\r\n")
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
      pass
    finally:
      self.active_live_streams = max(0, self.active_live_streams - 1)

  @staticmethod
  async def _send_json(
    writer: asyncio.StreamWriter, status_code: int, payload: Any
  ) -> None:
    raw = json.dumps(payload).encode("utf-8")
    phrase = (
      HTTPStatus(status_code).phrase
      if status_code in HTTPStatus._value2member_map_
      else "Error"
    )
    await FakeXtreamServer._send_raw(
      writer,
      status_code,
      phrase,
      raw,
      "application/json; charset=utf-8",
    )

  @staticmethod
  async def _send_raw(
    writer: asyncio.StreamWriter,
    status_code: int,
    phrase: str,
    body: bytes,
    content_type: str,
    *,
    extra_headers: dict[str, str] | None = None,
  ) -> None:
    hdrs = [
      f"HTTP/1.1 {status_code} {phrase}",
      f"Content-Type: {content_type}",
      f"Content-Length: {len(body)}",
      "Connection: close",
    ]
    if extra_headers:
      for k, v in extra_headers.items():
        hdrs.append(f"{k}: {v}")
    writer.write(("\r\n".join(hdrs) + "\r\n\r\n").encode("latin-1"))
    if body:
      writer.write(body)
    await writer.drain()
