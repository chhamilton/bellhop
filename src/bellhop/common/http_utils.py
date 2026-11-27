"""Shared aiohttp helpers, rate limiter, and LAN bind-address utilities.

Used across `bellhop.web` (Web Guide), `bellhop.xtream` (Xtream passthrough
server), `bellhop.epg`, and `bellhop.pipeline`.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import socket
import time
from collections.abc import (
  AsyncIterator,
  Awaitable,
  Callable,
  Collection,
  Iterable,
  Mapping,
  Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from math import ceil
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import aiohttp
import aiohttp.abc
import aiohttp.resolver
from aiohttp import web
from yarl import URL

__all__ = [
  "DEFAULT_CLIENT_MAX_SIZE",
  "DEFAULT_TRUSTED_PROXY_IPS",
  "MAX_UPSTREAM_REDIRECTS",
  "UPSTREAM_ERRORS",
  "WEB_CSP_POLICY",
  "ClientSessionFactory",
  "IpNetwork",
  "LoginRateLimiter",
  "SafeUpstreamResolver",
  "UpstreamTargetBlockedError",
  "build_access_urls",
  "bytes_response",
  "constant_time_credential_match",
  "create_web_app",
  "default_session_factory",
  "detect_device_ip",
  "extract_explicit_private_origins",
  "format_advertised_base_url",
  "is_trusted_proxy_peer",
  "json_response",
  "open_upstream_url",
  "parse_trusted_proxy_networks",
  "parse_validated_host_header",
  "redact_url_secrets",
  "redirect_response",
  "resolve_advertised_host_port",
  "resolve_bind_host",
  "resolve_client_ip",
  "resolve_request_client_ip",
  "start_site",
  "upstream_url",
  "validate_upstream_url",
]

logger = logging.getLogger("bellhop.common.http")

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
  "frame-ancestors 'self'; "
  "form-action 'self'"
)

# Bounded HTTP redirect hops followed when opening upstream streams or XMLTV.
MAX_UPSTREAM_REDIRECTS = 5

# Local intranet / mDNS domain suffixes explicitly allowed when configured.
_LOCAL_DOMAIN_SUFFIXES: tuple[str, ...] = (
  ".localhost",
  ".local",
  ".lan",
  ".internal",
  ".home.arpa",
)
_NUMERIC_IPV4_LABEL_RE = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)$")
_ALLOWED_PRIVATE_ORIGINS: contextvars.ContextVar[frozenset[tuple[str, int]]] = (
  contextvars.ContextVar("bellhop_allowed_private_origins", default=frozenset())
)


class UpstreamTargetBlockedError(aiohttp.ClientError):
  """Raised when an upstream URL or DNS result targets a blocked network."""


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


def _is_always_blocked_ip(addr: IpAddress) -> bool:
  return bool(
    addr.is_unspecified
    or addr.is_link_local
    or addr.is_multicast
    or str(addr) == "255.255.255.255"
    or (addr.is_reserved and not addr.is_loopback)
  )


def _looks_like_noncanonical_ipv4(host: str) -> bool:
  labels = host.split(".")
  if not labels or any(not _NUMERIC_IPV4_LABEL_RE.match(lbl) for lbl in labels):
    return False
  try:
    return str(ipaddress.IPv4Address(host)) != host
  except ipaddress.AddressValueError:
    return True


def _normalize_upstream_host(raw_host: str) -> str:
  return raw_host.strip().rstrip(".").lower()


def extract_explicit_private_origins(
  urls: Iterable[str],
) -> frozenset[tuple[str, int]]:
  """Collect `(host, port)` pairs explicitly configured for local origins.

  Link-local/metadata, unspecified, multicast, and reserved IPs are never
  added. Multi-label public domain names (e.g. `panel.example.com`) are never
  added so DNS-rebinding attacks against public upstreams remain blocked.
  """
  origins: set[tuple[str, int]] = set()
  for raw_url in urls:
    text = (raw_url or "").strip()
    if not text:
      continue
    try:
      parsed = urlsplit(text)
    except ValueError:
      continue
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.hostname:
      continue
    host_low = _normalize_upstream_host(parsed.hostname)
    if not host_low:
      continue
    try:
      port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
      continue
    ip = _parse_ip_addr(host_low)
    if ip is not None:
      if not _is_always_blocked_ip(ip) and not ip.is_global:
        origins.add((host_low, port))
        origins.add((str(ip), port))
    elif (
      host_low in {"localhost", "localhost.localdomain"}
      or host_low.endswith(_LOCAL_DOMAIN_SUFFIXES)
      or "." not in host_low
    ):
      origins.add((host_low, port))
  return frozenset(origins)


def validate_upstream_url(
  raw_url: str,
  *,
  allowed_private_origins: Collection[tuple[str, int]] = (),
) -> URL:
  """Validate an upstream HTTP(S) URL against SSRF targets and schemes."""
  cleaned = (raw_url or "").strip()
  if not cleaned:
    raise UpstreamTargetBlockedError("Empty upstream URL")
  if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in cleaned):
    raise UpstreamTargetBlockedError(
      "Blocked upstream URL containing control characters"
    )
  url = upstream_url(cleaned)
  if url.scheme not in ("http", "https") or not url.host:
    raise UpstreamTargetBlockedError(
      f"Blocked non-HTTP(S) upstream URL scheme: {url.scheme or '(none)'}"
    )
  host_low = _normalize_upstream_host(url.host)
  eff_port = url.port or (443 if url.scheme == "https" else 80)
  if not host_low:
    raise UpstreamTargetBlockedError("Blocked empty upstream hostname")
  if ":" not in host_low and _looks_like_noncanonical_ipv4(host_low):
    raise UpstreamTargetBlockedError(
      f"Blocked non-canonical IPv4 upstream host: {host_low}"
    )

  allowed = {(h.rstrip(".").lower(), p) for h, p in allowed_private_origins}
  ip = _parse_ip_addr(host_low)
  if ip is not None:
    if _is_always_blocked_ip(ip):
      raise UpstreamTargetBlockedError(
        f"Blocked restricted IP upstream target: {ip}"
      )
    if (
      not ip.is_global
      and (host_low, eff_port) not in allowed
      and (str(ip), eff_port) not in allowed
    ):
      raise UpstreamTargetBlockedError(
        f"Blocked private upstream IP target: {ip}:{eff_port}"
      )
  elif (
    host_low in {"localhost", "localhost.localdomain"}
    or host_low.endswith(".localhost")
  ) and (host_low, eff_port) not in allowed:
    raise UpstreamTargetBlockedError(
      f"Blocked loopback hostname upstream target: {host_low}:{eff_port}"
    )
  return url


class SafeUpstreamResolver(aiohttp.abc.AbstractResolver):
  """DNS resolver blocking loopback, link-local, and RFC1918 rebinding."""

  def __init__(self, inner: aiohttp.abc.AbstractResolver | None = None) -> None:
    self._resolver: aiohttp.abc.AbstractResolver = (
      inner or aiohttp.resolver.DefaultResolver()
    )

  async def resolve(
    self,
    host: str,
    port: int = 0,
    family: socket.AddressFamily = socket.AF_INET,
  ) -> list[aiohttp.abc.ResolveResult]:
    results = await self._resolver.resolve(host, port, family=family)
    allowed = _ALLOWED_PRIVATE_ORIGINS.get()
    host_low = _normalize_upstream_host(host)
    origin_ok = (host_low, port) in allowed
    for item in results:
      resolved_host = str(item.get("host") or "")
      ip = _parse_ip_addr(resolved_host)
      if ip is None:
        raise UpstreamTargetBlockedError(
          f"Blocked unparseable upstream resolution for {host_low}:{port}"
        )
      if _is_always_blocked_ip(ip) or (not ip.is_global and not origin_ok):
        raise UpstreamTargetBlockedError(
          f"Blocked non-public upstream resolution for {host_low}:{port}"
        )
    return results

  async def close(self) -> None:
    await self._resolver.close()


def default_session_factory(
  *,
  timeout: aiohttp.ClientTimeout,
  headers: Mapping[str, str] | None = None,
) -> aiohttp.ClientSession:
  """Create a production `aiohttp.ClientSession` with SSRF DNS protection."""
  connector = aiohttp.TCPConnector(
    resolver=SafeUpstreamResolver(),
    use_dns_cache=False,
  )
  return aiohttp.ClientSession(
    connector=connector,
    timeout=timeout,
    headers=headers,
  )


@asynccontextmanager
async def open_upstream_url(
  client: aiohttp.ClientSession,
  raw_url: str,
  *,
  allowed_private_origins: Collection[tuple[str, int]] = (),
  max_redirects: int = MAX_UPSTREAM_REDIRECTS,
) -> AsyncIterator[aiohttp.ClientResponse]:
  """Open `raw_url` via `client`, validating every redirect hop for SSRF."""
  normalized_allowed = frozenset(
    (h.rstrip(".").lower(), p) for h, p in allowed_private_origins
  )
  token = _ALLOWED_PRIVATE_ORIGINS.set(normalized_allowed)
  current_url = raw_url
  try:
    for hop in range(max_redirects + 1):
      target = validate_upstream_url(
        current_url,
        allowed_private_origins=normalized_allowed,
      )
      resp = await client.get(target, allow_redirects=False)
      loc = (resp.headers.get("Location") or "").strip()
      if resp.status in (301, 302, 303, 307, 308) and loc:
        base_for_join = str(resp.url)
        resp.release()
        if hop >= max_redirects:
          raise UpstreamTargetBlockedError(
            "Exceeded maximum upstream redirects"
          )
        current_url = urljoin(base_for_join, loc)
        continue
      try:
        yield resp
      finally:
        resp.release()
      return
    raise UpstreamTargetBlockedError("Exceeded maximum upstream redirects")
  finally:
    _ALLOWED_PRIVATE_ORIGINS.reset(token)


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
  """Resolve configured host (`0.0.0.0`, `auto`/`lan`/`device`, or IP)."""
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


_IPV6_HOST_HDR_RE = re.compile(r"^\[([^\]]+)\](?::(\d+))?$")
_REGNAME_HOST_HDR_RE = re.compile(
  r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
)


def parse_validated_host_header(raw_host: str) -> tuple[str, int | None] | None:
  """Parse and validate an incoming HTTP `Host` header into `(host, port)`.

  Rejects control chars, userinfo `@`, path separators, wildcards, and invalid
  IPv6/port values so incoming `Host` headers cannot inject characters into
  generated player or playlist URLs.
  """
  host_hdr = raw_host.strip()
  if not host_hdr or any(ord(c) <= 0x20 or c in "/\\@?#*" for c in host_hdr):
    return None
  if host_hdr.startswith("["):
    m_v6 = _IPV6_HOST_HDR_RE.match(host_hdr)
    if not m_v6 or "%" in m_v6.group(1):
      return None
    try:
      ipaddress.IPv6Address(m_v6.group(1))
    except ipaddress.AddressValueError:
      return None
    if m_v6.group(2) is None:
      return m_v6.group(1), None
    v6_port = int(m_v6.group(2))
    return (m_v6.group(1), v6_port) if 1 <= v6_port <= 65535 else None

  host_part = host_hdr
  port_num: int | None = None
  if ":" in host_hdr:
    h, p_str = host_hdr.rsplit(":", 1)
    if not p_str.isdigit():
      return None
    port_num = int(p_str)
    if not (1 <= port_num <= 65535):
      return None
    host_part = h
  if (
    not host_part
    or ".." in host_part
    or not _REGNAME_HOST_HDR_RE.fullmatch(host_part)
  ):
    return None
  return host_part, port_num


def resolve_advertised_host_port(
  headers: Mapping[str, str],
  *,
  bind_host: str,
  bound_port: int,
  public_host: str | None = None,
  public_port: int | None = None,
  server_protocol: str = "http",
) -> tuple[str, int]:
  """Resolve `(host, port)` to advertise in client-facing player URLs."""
  if public_host:
    return public_host, (public_port or bound_port)

  parsed_host = parse_validated_host_header(headers.get("host") or "")
  if parsed_host is not None:
    h, p = parsed_host
    default_port = 443 if server_protocol == "https" else 80
    return h, (public_port or p or default_port)

  fallback_host = (
    detect_device_ip() or "127.0.0.1"
    if bind_host in ("0.0.0.0", "::", "")
    else bind_host
  )
  return fallback_host, (public_port or bound_port)


def format_advertised_base_url(
  headers: Mapping[str, str],
  *,
  bind_host: str,
  bound_port: int,
  public_host: str | None = None,
  public_port: int | None = None,
  server_protocol: str = "http",
) -> str:
  """Format the scheme/host/port base URL for external IPTV players."""
  adv_host, adv_port = resolve_advertised_host_port(
    headers,
    bind_host=bind_host,
    bound_port=bound_port,
    public_host=public_host,
    public_port=public_port,
    server_protocol=server_protocol,
  )
  host_clean = adv_host.strip("[]")
  host_fmt = f"[{host_clean}]" if ":" in host_clean else host_clean
  return f"{server_protocol}://{host_fmt}:{adv_port}"


# -----------------------------------------------------------------------------
# Trusted reverse-proxy client IP resolution (`X-Forwarded-For` / `X-Real-IP`)
# -----------------------------------------------------------------------------
def _parse_ip_addr(raw: str | None) -> IpAddress | None:
  """Parse an IPv4/IPv6 address, unwrapping `::ffff:x.x.x.x`."""
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


def is_trusted_proxy_peer(
  remote: str | None,
  *,
  trust_proxy_headers: bool = False,
  trusted_networks: Sequence[IpNetwork] = (),
) -> bool:
  """Return `True` when `remote` is an allowlisted reverse-proxy peer."""
  if not trust_proxy_headers or not trusted_networks:
    return False
  peer_ip = _parse_ip_addr(remote)
  return peer_ip is not None and _ip_in_networks(peer_ip, trusted_networks)


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
_STREAM_CRED_PATH_RE = re.compile(
  r"^/(?:(?:live|movie|series|timeshift)/)?([^/]+)/([^/]+)/(\d+(?:\.[A-Za-z0-9]+)?)$"
)


def _redact_request_path(path: str) -> str:
  """Mask embedded credentials inside `/live/<user>/<pass>/<id>` paths."""
  return _STREAM_CRED_PATH_RE.sub(r"/live/\1/***/\3", path)


@web.middleware
async def _preflight_and_error_middleware(
  request: web.Request, handler: _Handler
) -> web.StreamResponse:
  """Convert body-limit errors and HTTP exceptions into JSON responses."""
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
    safe_path = _redact_request_path(request.path)
    message = (
      f"Not found: {safe_path}"
      if exc.status == HTTPStatus.NOT_FOUND
      else exc.reason
    )
    return json_response({"error": message}, status=exc.status)
  except asyncio.CancelledError:
    raise
  except Exception as exc:
    logger.exception(
      "Unhandled HTTP server error on %s: %s",
      _redact_request_path(request.path),
      exc,
    )
    return json_response(
      {"error": "Internal server error"},
      status=HTTPStatus.INTERNAL_SERVER_ERROR,
    )


async def _add_security_headers(
  _request: web.Request, response: web.StreamResponse
) -> None:
  response.headers.setdefault("Content-Security-Policy", WEB_CSP_POLICY)
  response.headers.setdefault("X-Content-Type-Options", "nosniff")
  response.headers.setdefault("Referrer-Policy", "no-referrer")


def create_web_app(
  *, client_max_size: int = DEFAULT_CLIENT_MAX_SIZE
) -> web.Application:
  """Create an `aiohttp` application with Bellhop's shared middleware."""
  app = web.Application(
    client_max_size=client_max_size,
    middlewares=[_preflight_and_error_middleware],
  )
  app[_MAX_BODY_KEY] = client_max_size
  app.on_response_prepare.append(_add_security_headers)
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


def constant_time_credential_match(
  given_user: str,
  given_pass: str,
  expected_user: str,
  expected_pass: str,
) -> bool:
  """Compare credentials in constant time using fixed-length SHA-256 digests.

  Avoids both length side-channels and short-circuiting on username mismatches.
  """
  g_u = hashlib.sha256(given_user.encode("utf-8")).digest()
  e_u = hashlib.sha256(expected_user.encode("utf-8")).digest()
  g_p = hashlib.sha256(given_pass.encode("utf-8")).digest()
  e_p = hashlib.sha256(expected_pass.encode("utf-8")).digest()
  matched = hmac.compare_digest(g_u, e_u) & hmac.compare_digest(g_p, e_p)
  return bool(expected_user and expected_pass and matched)


@dataclass(slots=True)
class LoginRateLimiter:
  """Bounded per-IP sliding-window failed-login rate limiter."""

  max_failures: int = 8
  window_sec: float = 60.0
  max_tracked_ips: int = 4096
  clock: Callable[[], float] = field(default=time.monotonic, repr=False)
  _failures: dict[str, list[float]] = field(default_factory=dict, repr=False)

  @staticmethod
  def _bucket_key(ip: str) -> str:
    """Normalize client IP strings, bucketing IPv6 peers by `/64` prefix."""
    addr = _parse_ip_addr(ip)
    if addr is None:
      return (ip or "").strip() or "unknown"
    if isinstance(addr, ipaddress.IPv6Address):
      return str(ipaddress.IPv6Network((addr, 64), strict=False))
    return str(addr)

  def _prune_ip(self, key: str, cutoff: float) -> list[float]:
    timestamps = self._failures.get(key)
    if not timestamps:
      self._failures.pop(key, None)
      return []
    fresh = [ts for ts in timestamps if ts > cutoff]
    if fresh:
      self._failures[key] = fresh
    else:
      self._failures.pop(key, None)
    return fresh

  def check(self, ip: str, *, now: float | None = None) -> int:
    """Return `0` if allowed, or positive `Retry-After` seconds if throttled."""
    if self.max_failures <= 0 or self.window_sec <= 0:
      return 0
    ts_now = self.clock() if now is None else now
    key = self._bucket_key(ip)
    fresh = self._prune_ip(key, ts_now - self.window_sec)
    if len(fresh) < self.max_failures:
      return 0
    oldest_active = fresh[0]
    remaining = (oldest_active + self.window_sec) - ts_now
    return max(1, ceil(remaining))

  def record_failure(self, ip: str, *, now: float | None = None) -> None:
    """Record a failed login attempt for `ip`."""
    if self.max_failures <= 0 or self.window_sec <= 0:
      return
    key = self._bucket_key(ip)
    ts_now = self.clock() if now is None else now
    cutoff = ts_now - self.window_sec
    if (
      key not in self._failures and len(self._failures) >= self.max_tracked_ips
    ):
      for stale_ip in list(self._failures):
        self._prune_ip(stale_ip, cutoff)
      if len(self._failures) >= self.max_tracked_ips:
        oldest_key = next(iter(self._failures))
        self._failures.pop(oldest_key, None)
    fresh = self._prune_ip(key, cutoff)
    fresh.append(ts_now)
    self._failures[key] = fresh

  def record_success(self, ip: str) -> None:
    """Clear failed login attempts for `ip` after a successful sign-in."""
    self._failures.pop(self._bucket_key(ip), None)


_STREAM_PATH_SECRET_RE = re.compile(
  r"(/(?:live|movie|series|timeshift)/[^/\s'\"\\]+/)([^/\s'\"\\]+)(/)",
  re.IGNORECASE,
)
_SHORTHAND_STREAM_SECRET_RE = re.compile(
  r"(://[^/\s'\"\\]+/)(?!(?:live|movie|series|timeshift)/)"
  r"([^/\s'\"\\]+/)([^/\s'\"\\]+)(/\d+(?:\.[A-Za-z0-9]+)?\b)",
  re.IGNORECASE,
)
_QUERY_SECRET_RE = re.compile(
  r"((?:password|passwd|pass|token)=)([^&\s'\"\\]+)",
  re.IGNORECASE,
)
_AUTH_USERINFO_RE = re.compile(r"(://[^:/@\s'\"\\]+:)([^@\s'\"\\]+)(@)")


def redact_url_secrets(text: str, *, secret: str = "") -> str:
  """Mask embedded stream/query/userinfo credentials in logged error strings.

  Args:
    text: Raw error or URL text that may embed upstream credentials.
    secret: Explicit literal password/secret to scrub if present anywhere.

  Returns:
    Sanitized string with passwords and tokens replaced by `***`.
  """
  masked = _STREAM_PATH_SECRET_RE.sub(r"\1***\3", text)
  masked = _SHORTHAND_STREAM_SECRET_RE.sub(r"\1\2***\4", masked)
  masked = _AUTH_USERINFO_RE.sub(r"\1***\3", masked)
  masked = _QUERY_SECRET_RE.sub(r"\1***", masked)
  if secret:
    masked = masked.replace(secret, "***")
  return masked
