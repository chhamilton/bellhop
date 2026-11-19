"""Shared async HTTP/1.1 connection, request/response, and bind-address utilities."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

__all__ = [
  "HttpRequest",
  "bound_server_port",
  "build_access_urls",
  "detect_device_ip",
  "read_http_request",
  "resolve_bind_host",
  "send_bytes",
  "send_json",
  "send_redirect",
  "serve_http_connection",
  "write_chunk",
]


def bound_server_port(server: asyncio.Server | None, fallback: int) -> int:
  """Return the OS-assigned TCP port for a started `asyncio.Server`, or `fallback`."""
  if server is not None and server.sockets:
    sockname = server.sockets[0].getsockname()
    if isinstance(sockname, tuple) and len(sockname) >= 2:
      return int(sockname[1])
  return fallback


@dataclass(slots=True)
class HttpRequest:
  """Parsed HTTP/1.1 request head and bounded request body."""

  method: str
  raw_target: str
  path: str
  query: dict[str, list[str]]
  headers: dict[str, str]
  body: bytes


def detect_device_ip() -> str | None:
  """Return the device's primary non-loopback IPv4 address, if available."""
  for target in (("10.255.255.255", 1), ("192.0.2.1", 80), ("8.8.8.8", 80)):
    try:
      with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(target)
        sockname = sock.getsockname()
        if isinstance(sockname, tuple) and sockname:
          ip = str(sockname[0])
          if ip and not ip.startswith("127.") and ip != "0.0.0.0":
            return ip
    except OSError:
      continue

  try:
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
      raw_ip = info[4][0]
      if (
        isinstance(raw_ip, str)
        and raw_ip
        and not raw_ip.startswith("127.")
        and raw_ip != "0.0.0.0"
      ):
        return raw_ip
  except OSError:
    pass
  return None


def resolve_bind_host(
  configured_host: str | None,
  *,
  ip_detector: Callable[[], str | None] = detect_device_ip,
) -> str:
  """Resolve a configured host (`0.0.0.0`, `auto`/`lan`/`device`, or explicit IP)."""
  raw = (configured_host or "").strip()
  if not raw:
    return "0.0.0.0"
  if raw.lower() in {"auto", "lan", "device"}:
    return ip_detector() or "0.0.0.0"
  return raw


def build_access_urls(
  bind_host: str,
  port: int,
  *,
  protocol: str = "http",
  device_ip: str | None = None,
  ip_detector: Callable[[], str | None] = detect_device_ip,
) -> list[str]:
  """Build user-facing base URLs for a bound HTTP server."""
  if bind_host in ("0.0.0.0", "::", ""):
    lan_ip = device_ip if device_ip is not None else ip_detector()
    urls: list[str] = []
    if lan_ip:
      urls.append(f"{protocol}://{lan_ip}:{port}")
    urls.append(f"{protocol}://localhost:{port}")
    return urls
  if bind_host in ("127.0.0.1", "::1", "localhost"):
    return [f"{protocol}://localhost:{port}"]
  formatted = (
    f"[{bind_host}]"
    if ":" in bind_host and not bind_host.startswith("[")
    else bind_host
  )
  return [f"{protocol}://{formatted}:{port}"]


async def read_http_request(
  reader: asyncio.StreamReader,
  *,
  max_body_bytes: int = 65536,
  merge_form_query: bool = False,
) -> HttpRequest | None:
  """Read and parse a single HTTP/1.1 request from `reader` (`None` on clean EOF)."""
  req_line_bytes = await asyncio.wait_for(reader.readline(), timeout=15.0)
  if not req_line_bytes:
    return None
  parts = req_line_bytes.decode("latin-1", errors="replace").strip().split(" ")
  if len(parts) < 2:
    raise ValueError("Malformed HTTP request line")
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

  body = b""
  content_len = int(headers.get("content-length", "0") or "0")
  if content_len > 0:
    body = await asyncio.wait_for(
      reader.readexactly(min(content_len, max_body_bytes)),
      timeout=5.0,
    )

  parsed = urlsplit(raw_target)
  path = unquote(parsed.path)
  query = parse_qs(parsed.query)
  if merge_form_query and method == "POST" and body:
    ctype = (headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in ctype or (
      "application/json" not in ctype and b"=" in body
    ):
      for k, vals in parse_qs(body.decode("utf-8", errors="replace")).items():
        query.setdefault(k, vals)

  return HttpRequest(
    method=method,
    raw_target=raw_target,
    path=path,
    query=query,
    headers=headers,
    body=body,
  )


async def send_bytes(
  writer: asyncio.StreamWriter,
  status: HTTPStatus,
  body: bytes,
  content_type: str,
  *,
  content_length: int | None = None,
  cache_control: str = "no-store",
  extra_headers: dict[str, str] | None = None,
) -> None:
  """Write a complete HTTP/1.1 response with `Content-Length`."""
  clen = len(body) if content_length is None else content_length
  hdr_lines = [
    f"HTTP/1.1 {status.value} {status.phrase}",
    f"Content-Type: {content_type}",
    f"Content-Length: {clen}",
    f"Cache-Control: {cache_control}",
    "Access-Control-Allow-Origin: *",
    "Access-Control-Allow-Methods: GET, POST, PUT, HEAD, OPTIONS",
    "Connection: close",
  ]
  if extra_headers:
    for k, v in extra_headers.items():
      hdr_lines.append(f"{k}: {v}")
  writer.write(("\r\n".join(hdr_lines) + "\r\n\r\n").encode("latin-1"))
  if body:
    writer.write(body)
  await writer.drain()


async def send_json(
  writer: asyncio.StreamWriter,
  status: HTTPStatus,
  payload: Any,
  *,
  extra_headers: dict[str, str] | None = None,
) -> None:
  """Serialize `payload` as UTF-8 JSON and write an HTTP/1.1 response."""
  raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
  await send_bytes(
    writer,
    status,
    raw,
    "application/json; charset=utf-8",
    extra_headers=extra_headers,
  )


async def send_redirect(
  writer: asyncio.StreamWriter,
  location: str,
  *,
  status: HTTPStatus = HTTPStatus.FOUND,
  extra_headers: dict[str, str] | None = None,
) -> None:
  """Write an HTTP 302 redirect response."""
  hdr_lines = [
    f"HTTP/1.1 {status.value} {status.phrase}",
    f"Location: {location}",
    "Cache-Control: no-store",
    "Access-Control-Allow-Origin: *",
    "Content-Length: 0",
    "Connection: close",
  ]
  if extra_headers:
    for k, v in extra_headers.items():
      hdr_lines.append(f"{k}: {v}")
  writer.write(("\r\n".join(hdr_lines) + "\r\n\r\n").encode("latin-1"))
  await writer.drain()


async def write_chunk(writer: asyncio.StreamWriter, data: bytes) -> None:
  """Write a single HTTP/1.1 `Transfer-Encoding: chunked` frame."""
  if not data:
    return
  writer.write(f"{len(data):X}\r\n".encode("ascii"))
  writer.write(data)
  writer.write(b"\r\n")
  await writer.drain()


async def serve_http_connection(
  reader: asyncio.StreamReader,
  writer: asyncio.StreamWriter,
  handler: Callable[[HttpRequest, asyncio.StreamWriter], Awaitable[None]],
  *,
  logger: logging.Logger,
  merge_form_query: bool = False,
) -> None:
  """Handle one HTTP/1.1 socket lifecycle, CORS preflight, error trapping, and teardown."""
  try:
    try:
      req = await read_http_request(reader, merge_form_query=merge_form_query)
    except ValueError:
      await send_json(writer, HTTPStatus.BAD_REQUEST, {"error": "Bad request"})
      return
    if req is None:
      return
    if req.method == "OPTIONS":
      await send_bytes(writer, HTTPStatus.NO_CONTENT, b"", "text/plain")
      return
    await handler(req, writer)
  except (
    asyncio.TimeoutError,
    asyncio.IncompleteReadError,
    ConnectionResetError,
    BrokenPipeError,
  ):
    pass
  except asyncio.CancelledError:
    raise
  except Exception as exc:
    logger.exception("Unhandled HTTP server error: %s", exc)
    with contextlib.suppress(Exception):
      await send_json(
        writer, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
      )
  finally:
    try:
      if not writer.is_closing():
        writer.close()
        await writer.wait_closed()
    except Exception:
      pass
