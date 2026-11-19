"""Shared aiohttp server/client helpers and LAN bind-address utilities.

Used by both `bellhop_web` (Web Guide) and `bellhop` (Xtream passthrough
server, pipeline HTTP clients). Provides:

  - Bind-address helpers: `detect_device_ip`, `resolve_bind_host`,
    `build_access_urls`.
  - Server helpers: `create_web_app` (CORS, OPTIONS preflight, JSON error
    middleware, request size limit), `json_response`, `bytes_response`,
    `redirect_response`, and `start_site` (returns the OS-bound port).
  - Client helpers: `ClientSessionFactory` / `default_session_factory` for
    dependency-injected `aiohttp.ClientSession` creation, `upstream_url` for
    verbatim (non-requoted) upstream URLs, and `UPSTREAM_ERRORS`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from typing import Any, Protocol

import aiohttp
from aiohttp import web
from yarl import URL

__all__ = [
  "DEFAULT_CLIENT_MAX_SIZE",
  "UPSTREAM_ERRORS",
  "ClientSessionFactory",
  "build_access_urls",
  "bytes_response",
  "create_web_app",
  "default_session_factory",
  "detect_device_ip",
  "json_response",
  "redirect_response",
  "resolve_bind_host",
  "start_site",
  "upstream_url",
]

logger = logging.getLogger("bellhop_web.http")

# Maximum accepted request body size (login forms & settings JSON are tiny).
DEFAULT_CLIENT_MAX_SIZE = 256 * 1024

# Exceptions raised by aiohttp client requests for network/HTTP failures.
UPSTREAM_ERRORS: tuple[type[BaseException], ...] = (
  aiohttp.ClientError,
  asyncio.TimeoutError,
)

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
_MAX_BODY_KEY: web.AppKey[int] = web.AppKey("max_body_bytes", int)


class ClientSessionFactory(Protocol):
  """Callable creating an `aiohttp.ClientSession` (injectable for tests)."""

  def __call__(
    self,
    *,
    timeout: aiohttp.ClientTimeout,
    headers: Mapping[str, str] | None = None,
  ) -> aiohttp.ClientSession: ...


def default_session_factory(
  *,
  timeout: aiohttp.ClientTimeout,
  headers: Mapping[str, str] | None = None,
) -> aiohttp.ClientSession:
  """Create a production `aiohttp.ClientSession`."""
  return aiohttp.ClientSession(timeout=timeout, headers=headers)


def upstream_url(url: str) -> URL:
  """Wrap an already-encoded upstream URL without re-quoting it.

  Upstream panels and HLS edges frequently embed signed tokens whose exact
  percent-encoding must be preserved byte-for-byte.
  """
  return URL(url, encoded=True)


# -----------------------------------------------------------------------------
# Bind-address & LAN URL helpers
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# Response helpers
# -----------------------------------------------------------------------------
def _with_headers(
  base: dict[str, str], extra: Mapping[str, str] | None
) -> dict[str, str]:
  if extra:
    base.update(extra)
  return base


def json_response(
  payload: Any,
  status: int = HTTPStatus.OK,
  *,
  headers: Mapping[str, str] | None = None,
) -> web.Response:
  """Serialize `payload` as UTF-8 JSON (non-ASCII preserved, never cached)."""
  return web.Response(
    body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    status=int(status),
    content_type="application/json",
    charset="utf-8",
    headers=_with_headers({"Cache-Control": "no-store"}, headers),
  )


def bytes_response(
  body: bytes,
  content_type: str,
  *,
  status: int = HTTPStatus.OK,
  cache_control: str = "no-store",
  headers: Mapping[str, str] | None = None,
) -> web.Response:
  """Return a complete response with an explicit `Content-Type` header."""
  return web.Response(
    body=body,
    status=int(status),
    headers=_with_headers(
      {"Content-Type": content_type, "Cache-Control": cache_control}, headers
    ),
  )


def redirect_response(
  location: str,
  *,
  status: int = HTTPStatus.FOUND,
  headers: Mapping[str, str] | None = None,
) -> web.Response:
  """Return an HTTP redirect (default `302 Found`) to `location`."""
  return web.Response(
    status=int(status),
    headers=_with_headers(
      {"Location": location, "Cache-Control": "no-store"}, headers
    ),
  )


# -----------------------------------------------------------------------------
# Application factory & site startup
# -----------------------------------------------------------------------------
@web.middleware
async def _preflight_and_error_middleware(
  request: web.Request, handler: _Handler
) -> web.StreamResponse:
  """Answer CORS preflights and convert errors into JSON responses."""
  if request.method == "OPTIONS":
    return web.Response(status=HTTPStatus.NO_CONTENT)
  max_bytes = request.app.get(_MAX_BODY_KEY, DEFAULT_CLIENT_MAX_SIZE)
  if request.content_length is not None and request.content_length > max_bytes:
    return json_response(
      {"error": "Request Entity Too Large"},
      status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
      headers={"Connection": "close"},
    )
  try:
    return await handler(request)
  except web.HTTPException as exc:
    if exc.status < 400:
      raise
    message = (
      f"Not found: {request.path}"
      if exc.status == HTTPStatus.NOT_FOUND
      else exc.reason
    )
    return json_response({"error": message}, status=exc.status)
  except asyncio.CancelledError:
    raise
  except Exception as exc:
    logger.exception("Unhandled HTTP server error on %s: %s", request.path, exc)
    return json_response(
      {"error": "Internal server error"},
      status=HTTPStatus.INTERNAL_SERVER_ERROR,
    )


async def _add_cors_headers(
  _request: web.Request, response: web.StreamResponse
) -> None:
  response.headers.setdefault("Access-Control-Allow-Origin", "*")
  response.headers.setdefault(
    "Access-Control-Allow-Methods", "GET, POST, PUT, HEAD, OPTIONS"
  )


def create_web_app(
  *, client_max_size: int = DEFAULT_CLIENT_MAX_SIZE
) -> web.Application:
  """Create an `aiohttp` application with Bellhop's shared middleware."""
  app = web.Application(
    client_max_size=client_max_size,
    middlewares=[_preflight_and_error_middleware],
  )
  app[_MAX_BODY_KEY] = client_max_size
  app.on_response_prepare.append(_add_cors_headers)
  return app


async def start_site(
  app: web.Application, host: str, port: int
) -> tuple[web.AppRunner, int]:
  """Start serving `app` on `host:port`, returning the runner and bound port."""
  runner = web.AppRunner(app, access_log=None, handle_signals=False)
  await runner.setup()
  site = web.TCPSite(runner, host=host, port=port)
  await site.start()
  bound = port
  for addr in runner.addresses:
    if isinstance(addr, tuple) and len(addr) >= 2:
      bound = int(addr[1])
      break
  return runner, bound
