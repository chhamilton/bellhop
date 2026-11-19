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
import ipaddress
import json
import logging
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from http import HTTPStatus
from typing import Any, Protocol

import aiohttp
from aiohttp import web
from yarl import URL

__all__ = [
  "DEFAULT_CLIENT_MAX_SIZE",
  "DEFAULT_TRUSTED_PROXY_IPS",
  "UPSTREAM_ERRORS",
  "WEB_CSP_POLICY",
  "ClientSessionFactory",
  "IpNetwork",
  "build_access_urls",
  "bytes_response",
  "create_web_app",
  "default_session_factory",
  "detect_device_ip",
  "json_response",
  "parse_trusted_proxy_networks",
  "redirect_response",
  "resolve_bind_host",
  "resolve_client_ip",
  "resolve_request_client_ip",
  "start_site",
  "upstream_url",
]

logger = logging.getLogger("bellhop_web.http")

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Default reverse-proxy allowlist when `trust_proxy_headers: true` is enabled.
DEFAULT_TRUSTED_PROXY_IPS: tuple[str, ...] = ("127.0.0.1", "::1")

# Maximum accepted request body size (login forms & settings JSON are tiny).
DEFAULT_CLIENT_MAX_SIZE = 256 * 1024

# Content-Security-Policy disallowing inline scripts/eval while permitting
# `mpegts.js`/`hls.js` blob: workers & MSE streams and external channel logos.
WEB_CSP_POLICY = (
  "default-src 'self'; "
  "script-src 'self' blob:; "
  "worker-src 'self' blob:; "
  "media-src 'self' blob:; "
  "img-src 'self' https: http: data: blob:; "
  "style-src 'self' 'unsafe-inline'; "
  "connect-src 'self'; "
  "object-src 'none'; "
  "base-uri 'self'; "
  "frame-ancestors 'self'"
)

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
# Trusted reverse-proxy client IP resolution (`X-Forwarded-For` / `X-Real-IP`)
# -----------------------------------------------------------------------------
def _parse_ip_addr(raw: str | None) -> IpAddress | None:
  """Parse an IPv4/IPv6 address, unwrapping IPv4-mapped IPv6 (`::ffff:x.x.x.x`)."""
  if not raw:
    return None
  cleaned = raw.strip()
  if not cleaned:
    return None
  try:
    addr = ipaddress.ip_address(cleaned)
  except ValueError:
    return None
  if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
    return addr.ipv4_mapped
  return addr


def parse_trusted_proxy_networks(
  entries: Sequence[str],
) -> tuple[IpNetwork, ...]:
  """Validate and parse trusted reverse-proxy IPs or CIDR ranges."""
  networks: list[IpNetwork] = []
  for raw in entries:
    item = str(raw).strip()
    if not item:
      continue
    try:
      net = ipaddress.ip_network(item, strict=False)
    except ValueError as exc:
      raise ValueError(
        f"Invalid IP address or CIDR in trusted_proxy_ips: {item!r}"
      ) from exc
    if (
      isinstance(net, ipaddress.IPv6Network)
      and net.prefixlen == 128
      and net.network_address.ipv4_mapped is not None
    ):
      net = ipaddress.ip_network(net.network_address.ipv4_mapped, strict=False)
    networks.append(net)
  return tuple(networks)


def _ip_in_networks(addr: IpAddress, networks: Sequence[IpNetwork]) -> bool:
  return any(addr.version == net.version and addr in net for net in networks)


def resolve_client_ip(
  remote: str | None,
  *,
  xff_headers: Sequence[str] = (),
  real_ip_header: str | None = None,
  trust_proxy_headers: bool = False,
  trusted_networks: Sequence[IpNetwork] = (),
) -> str:
  """Resolve the effective client IP for rate limiting and audit logging.

  When `trust_proxy_headers` is False or `remote` is not inside
  `trusted_networks`, forwarding headers are ignored. When `remote` is a
  trusted reverse proxy, walks `X-Forwarded-For` right-to-left and returns the
  rightmost valid IP that is not itself in `trusted_networks` (preventing
  leftmost XFF spoofing).
  """
  peer_ip = _parse_ip_addr(remote)
  peer_str = (
    str(peer_ip)
    if peer_ip is not None
    else ((remote or "").strip() or "unknown")
  )
  if (
    not trust_proxy_headers
    or peer_ip is None
    or not trusted_networks
    or not _ip_in_networks(peer_ip, trusted_networks)
  ):
    return peer_str

  valid_hops: list[IpAddress] = []
  for header_val in xff_headers:
    for token in header_val.split(","):
      parsed = _parse_ip_addr(token)
      if parsed is not None:
        valid_hops.append(parsed)

  if valid_hops:
    for hop in reversed(valid_hops):
      if not _ip_in_networks(hop, trusted_networks):
        return str(hop)
    return str(valid_hops[0])

  real_ip = _parse_ip_addr(real_ip_header)
  if real_ip is not None:
    return str(real_ip)

  return peer_str


def resolve_request_client_ip(
  request: web.Request,
  *,
  trust_proxy_headers: bool = False,
  trusted_networks: Sequence[IpNetwork] = (),
) -> str:
  """Resolve the client IP of an `aiohttp.web.Request`."""
  return resolve_client_ip(
    request.remote,
    xff_headers=request.headers.getall("X-Forwarded-For", ()),
    real_ip_header=request.headers.get("X-Real-IP"),
    trust_proxy_headers=trust_proxy_headers,
    trusted_networks=trusted_networks,
  )


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
  response.headers.setdefault("Content-Security-Policy", WEB_CSP_POLICY)
  response.headers.setdefault("X-Content-Type-Options", "nosniff")


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
