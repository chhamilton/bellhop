"""Loopback aiohttp mock server and test client helpers."""

from __future__ import annotations

import inspect
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import aiohttp
import aiohttp.abc
from aiohttp import web
from yarl import URL

from bellhop.common.http_utils import start_site

MockHandler = Callable[
  [web.Request], web.StreamResponse | Awaitable[web.StreamResponse]
]


class _LoopbackResolver(aiohttp.abc.AbstractResolver):
  """Resolve any hostname/port in unit tests to a local loopback port."""

  def __init__(self, port: int) -> None:
    self._port = port

  async def resolve(
    self,
    host: str,
    port: int = 0,
    family: socket.AddressFamily = socket.AF_INET,
  ) -> list[aiohttp.abc.ResolveResult]:
    _ = (port, family)
    return [
      {
        "hostname": host,
        "host": "127.0.0.1",
        "port": self._port,
        "family": socket.AF_INET,
        "proto": socket.IPPROTO_TCP,
        "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
      }
    ]

  async def close(self) -> None:
    return None


class MockHttpServer:
  """Ephemeral `127.0.0.1:0` `aiohttp.web` server for upstream mocks."""

  def __init__(self, handler: MockHandler) -> None:
    self._handler = handler
    self._runner: web.AppRunner | None = None
    self.port: int = 0

  @property
  def base_url(self) -> str:
    return f"http://127.0.0.1:{self.port}"

  async def _dispatch(self, request: web.Request) -> web.StreamResponse:
    res = self._handler(request)
    if inspect.isawaitable(res):
      return await res
    return res

  async def start(self) -> MockHttpServer:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", self._dispatch)
    self._runner, self.port = await start_site(app, "127.0.0.1", 0)
    return self

  async def stop(self) -> None:
    if self._runner is not None:
      await self._runner.cleanup()
      self._runner = None

  async def __aenter__(self) -> MockHttpServer:
    return await self.start()

  async def __aexit__(
    self,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
  ) -> None:
    _ = (exc_type, exc, tb)
    await self.stop()

  def session_factory(
    self,
    *,
    timeout: aiohttp.ClientTimeout,
    headers: Mapping[str, str] | None = None,
  ) -> aiohttp.ClientSession:
    """Create a `ClientSession` routing all HTTP hosts to this mock server."""
    connector = aiohttp.TCPConnector(resolver=_LoopbackResolver(self.port))
    return aiohttp.ClientSession(
      connector=connector,
      timeout=timeout,
      headers=headers,
    )

  def new_session(
    self,
    *,
    timeout_sec: float = 10.0,
    headers: Mapping[str, str] | None = None,
  ) -> aiohttp.ClientSession:
    """Create a standalone `ClientSession` wired to this mock server."""
    return self.session_factory(
      timeout=aiohttp.ClientTimeout(total=timeout_sec),
      headers=headers,
    )


def new_test_session(
  base_url: str | None = None,
  *,
  timeout: float = 10.0,
) -> aiohttp.ClientSession:
  """Create a test `aiohttp.ClientSession` accepting cookies on `127.0.0.1`."""
  return aiohttp.ClientSession(
    base_url=base_url,
    timeout=aiohttp.ClientTimeout(total=timeout),
    cookie_jar=aiohttp.CookieJar(unsafe=True),
  )


@dataclass(slots=True)
class TestHttpResponse:
  """Buffered `aiohttp` response snapshot for concise test assertions."""

  __test__ = False

  status_code: int
  headers: Mapping[str, str]
  content: bytes
  charset: str = "utf-8"

  @property
  def text(self) -> str:
    return self.content.decode(self.charset, errors="replace")

  def json(self) -> Any:
    return json.loads(self.text)


class TestStreamResponse:
  """Streaming `aiohttp.ClientResponse` wrapper for MPEG-TS assertions."""

  __test__ = False

  def __init__(self, resp: aiohttp.ClientResponse) -> None:
    self._resp = resp

  @property
  def status_code(self) -> int:
    return self._resp.status

  @property
  def headers(self) -> Mapping[str, str]:
    return self._resp.headers

  async def aiter_bytes(self) -> AsyncIterator[bytes]:
    async for chunk in self._resp.content.iter_any():
      yield chunk


class TestCookieHelper:
  """Cookie setter attached to a `TestHttpClient`'s `aiohttp.CookieJar`."""

  __test__ = False

  def __init__(
    self, session: aiohttp.ClientSession, base_url: str | None
  ) -> None:
    self._session = session
    self._origin = URL(base_url or "http://127.0.0.1/")

  def set(self, name: str, value: str) -> None:
    self._session.cookie_jar.update_cookies({name: value}, self._origin)


class TestHttpClient:
  """Thin `aiohttp.ClientSession` wrapper with buffered & stream reads."""

  __test__ = False

  def __init__(
    self,
    *,
    base_url: str | None = None,
    timeout: float = 10.0,
    follow_redirects: bool = True,
  ) -> None:
    self._base_url = base_url
    self._timeout = timeout
    self._follow_redirects = follow_redirects
    self._session: aiohttp.ClientSession | None = None

  async def __aenter__(self) -> TestHttpClient:
    self._session = new_test_session(self._base_url, timeout=self._timeout)
    return self

  async def __aexit__(
    self,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
  ) -> None:
    _ = (exc_type, exc, tb)
    if self._session is not None:
      await self._session.close()
      self._session = None

  @property
  def cookies(self) -> TestCookieHelper:
    assert self._session is not None
    return TestCookieHelper(self._session, self._base_url)

  async def request(
    self,
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    json: Any = None,
    data: Any = None,
    headers: Mapping[str, str] | None = None,
  ) -> TestHttpResponse:
    assert self._session is not None
    async with self._session.request(
      method,
      url,
      params=params,
      json=json,
      data=data,
      headers=headers,
      allow_redirects=self._follow_redirects,
    ) as resp:
      body = await resp.read()
      return TestHttpResponse(
        status_code=resp.status,
        headers=resp.headers,
        content=body,
        charset=resp.charset or "utf-8",
      )

  async def get(
    self,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
  ) -> TestHttpResponse:
    return await self.request("GET", url, params=params, headers=headers)

  async def head(
    self,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
  ) -> TestHttpResponse:
    return await self.request("HEAD", url, params=params, headers=headers)

  async def post(
    self,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    json: Any = None,
    data: Any = None,
    headers: Mapping[str, str] | None = None,
  ) -> TestHttpResponse:
    return await self.request(
      "POST", url, params=params, json=json, data=data, headers=headers
    )

  async def put(
    self,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    json: Any = None,
    data: Any = None,
    headers: Mapping[str, str] | None = None,
  ) -> TestHttpResponse:
    return await self.request(
      "PUT", url, params=params, json=json, data=data, headers=headers
    )

  @asynccontextmanager
  async def stream(
    self,
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
  ) -> AsyncIterator[TestStreamResponse]:
    assert self._session is not None
    async with self._session.request(
      method,
      url,
      params=params,
      headers=headers,
      allow_redirects=self._follow_redirects,
    ) as resp:
      yield TestStreamResponse(resp)
