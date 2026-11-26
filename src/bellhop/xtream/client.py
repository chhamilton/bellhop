"""Async Xtream `player_api.php` client, loader, and catalog cache."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from bellhop.common.file_utils import ensure_private_dir, write_private_text
from bellhop.common.http_utils import redact_url_secrets
from bellhop.config import AppConfig, XtreamConfig

DEFAULT_USER_AGENT = "Bellhop/0.1 (VLC/3.0.20 LibVLC/3.0.20)"


def _opt_int(val: object) -> int | None:
  if val is None or isinstance(val, bool):
    return None
  if isinstance(val, (int, float, str)):
    try:
      return int(val)
    except ValueError:
      return None
  return None


@dataclass(slots=True)
class XtreamAccountInfo:
  """Xtream Codes account & server metadata returned by `player_api.php`."""

  username: str
  status: str = "Unknown"
  exp_date: str | None = None
  is_trial: bool = False
  active_connections: int = 0
  max_connections: int = 1
  allowed_output_formats: list[str] = field(
    default_factory=lambda: ["ts", "m3u8"]
  )
  server_protocol: str = "http"
  server_port: str | None = None
  timezone: str | None = None

  @classmethod
  def from_api_payload(cls, data: Mapping[str, Any]) -> XtreamAccountInfo:
    user_raw = data.get("user_info")
    server_raw = data.get("server_info")
    user: Mapping[str, Any] = user_raw if isinstance(user_raw, Mapping) else {}
    server: Mapping[str, Any] = (
      server_raw if isinstance(server_raw, Mapping) else {}
    )

    formats_raw = user.get("allowed_output_formats") or ["ts", "m3u8"]
    if isinstance(formats_raw, str):
      formats = [f.strip() for f in formats_raw.split(",") if f.strip()]
    elif isinstance(formats_raw, list):
      formats = [str(f).strip() for f in formats_raw if str(f).strip()]
    else:
      formats = ["ts", "m3u8"]

    active_cons = _opt_int(user.get("active_cons"))
    max_cons = _opt_int(user.get("max_connections"))
    port_val = server.get("port")

    return cls(
      username=str(user.get("username", "")),
      status=str(user.get("status", "Unknown")),
      exp_date=str(user["exp_date"]) if user.get("exp_date") else None,
      is_trial=str(user.get("is_trial", "0")) == "1",
      active_connections=0 if active_cons is None else active_cons,
      max_connections=max(1, 1 if max_cons is None else max_cons),
      allowed_output_formats=formats,
      server_protocol=str(server.get("server_protocol", "http")),
      server_port=str(port_val)
      if port_val is not None and str(port_val)
      else None,
      timezone=str(server["timezone"]) if server.get("timezone") else None,
    )


@dataclass(slots=True)
class XtreamStream:
  """A single live stream entry from an upstream Xtream JSON catalog."""

  stream_id: int
  name: str
  category_id: str = ""
  category_name: str = ""
  num: int = 0
  stream_type: str = "live"
  stream_icon: str = ""
  epg_channel_id: str = ""
  tv_archive: bool = False
  direct_source: str = ""

  @classmethod
  def from_dict(
    cls,
    raw: Mapping[str, Any],
    categories_by_id: Mapping[str, str] | None = None,
  ) -> XtreamStream:
    cat_id = str(raw.get("category_id") or "")
    cat_name = str(raw.get("category_name") or "")
    if not cat_name and categories_by_id and cat_id in categories_by_id:
      cat_name = categories_by_id[cat_id]

    stream_id_raw = raw.get("stream_id") or raw.get("id") or 0
    num_raw = raw.get("num") or 0

    return cls(
      stream_id=int(stream_id_raw),
      name=str(raw.get("name") or "").strip(),
      category_id=cat_id,
      category_name=cat_name.strip(),
      num=int(num_raw) if str(num_raw).isdigit() else 0,
      stream_type=str(raw.get("stream_type") or "live"),
      stream_icon=str(raw.get("stream_icon") or "").strip(),
      epg_channel_id=str(raw.get("epg_channel_id") or "").strip(),
      tv_archive=str(raw.get("tv_archive") or "0") == "1",
      direct_source=str(raw.get("direct_source") or "").strip(),
    )

  def build_url(
    self,
    server_url: str,
    username: str,
    password: str,
    stream_format: str = "ts",
  ) -> str:
    """Construct the playback URL for this Xtream live stream."""
    if self.direct_source and not server_url:
      return self.direct_source
    base = server_url.strip().rstrip("/")
    if base and not base.startswith(("http://", "https://")):
      base = f"http://{base}"
    ext = stream_format.lstrip(".")
    return f"{base}/live/{username}/{password}/{self.stream_id}.{ext}"


class XtreamError(RuntimeError):
  """Raised when Xtream Codes API authentication or fetching fails."""


@dataclass(slots=True)
class XtreamCatalog:
  """Complete Xtream live TV catalog (account info, categories, and streams)."""

  account: XtreamAccountInfo
  categories: dict[str, str]
  streams: list[XtreamStream]
  from_cache: bool = False


def _candidate_base_urls(raw_url: str) -> list[str]:
  cleaned = raw_url.strip().rstrip("/")
  if cleaned.startswith(("http://", "https://")):
    return [cleaned]
  return [f"http://{cleaned}", f"https://{cleaned}"]


def _cache_file_for(cache_dir: Path, server_url: str, username: str) -> Path:
  normalized = server_url.strip().rstrip("/").lower()
  for prefix in ("https://", "http://"):
    if normalized.startswith(prefix):
      normalized = normalized[len(prefix) :]
      break
  key = hashlib.sha256(f"{normalized}|{username}".encode()).hexdigest()[:16]
  return cache_dir / f"xtream_catalog_{key}.json"


def _load_cached_catalog(
  cache_file: Path, ttl_hours: float
) -> XtreamCatalog | None:
  if not cache_file.is_file() or ttl_hours <= 0:
    return None
  try:
    age_seconds = time.time() - cache_file.stat().st_mtime
    if age_seconds > ttl_hours * 3600.0:
      return None
    with cache_file.open("r", encoding="utf-8") as fh:
      payload = json.load(fh)
    account_raw = payload.get("account_payload") or {}
    account = XtreamAccountInfo.from_api_payload(account_raw)
    categories = {
      str(k): str(v) for k, v in (payload.get("categories") or {}).items()
    }
    streams = [
      XtreamStream.from_dict(item, categories_by_id=categories)
      for item in (payload.get("streams") or [])
    ]
    return XtreamCatalog(
      account=account,
      categories=categories,
      streams=streams,
      from_cache=True,
    )
  except (OSError, ValueError, KeyError, TypeError):
    return None


def _save_cached_catalog(
  cache_file: Path,
  account_payload: Mapping[str, Any],
  categories: dict[str, str],
  streams_raw: list[dict[str, Any]],
) -> None:
  ensure_private_dir(cache_file.parent)
  # Strip password if present in raw user_info before persisting to disk.
  sanitized_account: dict[str, Any] = dict(account_payload)
  user_raw = sanitized_account.get("user_info")
  if isinstance(user_raw, Mapping):
    user_copy = dict(user_raw)
    user_copy.pop("password", None)
    sanitized_account["user_info"] = user_copy

  data = {
    "cached_at": time.time(),
    "account_payload": sanitized_account,
    "categories": categories,
    "streams": streams_raw,
  }
  write_private_text(cache_file, json.dumps(data, ensure_ascii=False))


async def _negotiate_xtream_handshake(
  client: aiohttp.ClientSession,
  candidates: Sequence[str],
  base_params: Mapping[str, str],
  xt: XtreamConfig,
) -> tuple[str, dict[str, Any]]:
  """Authenticate with candidate Xtream URLs; return `(base_url, payload)`."""
  base_url = candidates[0]
  account_payload: Any = None
  last_exc: Exception | None = None
  for cand_base in candidates:
    try:
      async with client.get(
        f"{cand_base}/player_api.php", params=base_params
      ) as resp:
        resp.raise_for_status()
        account_payload = await resp.json(content_type=None)
        base_url = str(resp.url.origin())
        xt.server_url = base_url
        break
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
      last_exc = exc

  if account_payload is None:
    if isinstance(last_exc, aiohttp.ClientResponseError):
      raise XtreamError(
        f"Xtream API HTTP {last_exc.status} error from {base_url}"
      ) from last_exc
    raise XtreamError(
      f"Network error contacting Xtream server {xt.server_url}: "
      f"{redact_url_secrets(str(last_exc))}"
    ) from last_exc

  if not isinstance(account_payload, dict):
    raise XtreamError("Unexpected non-JSON-object response from player_api.php")

  user_info = account_payload.get("user_info") or {}
  if str(user_info.get("auth", "1")) == "0":
    raise XtreamError(
      "Xtream authentication failed (invalid username or password)."
    )
  return base_url, account_payload


async def fetch_xtream_account_info(
  config: AppConfig,
  *,
  client: aiohttp.ClientSession | None = None,
) -> XtreamAccountInfo:
  """Verify live `player_api.php` account status without using a tuner slot."""
  xt = config.xtream
  if not xt.server_url or not xt.username or not xt.password:
    raise XtreamError(
      "Missing Xtream credentials. Configure upstream_xtream credentials in "
      "credentials.yaml, config.yaml, or .env."
    )

  candidates = _candidate_base_urls(xt.server_url)
  base_params = {"username": xt.username, "password": xt.password}

  owns_client = client is None
  if owns_client:
    client = aiohttp.ClientSession(
      timeout=aiohttp.ClientTimeout(total=min(xt.request_timeout_sec, 15.0)),
      headers={"User-Agent": DEFAULT_USER_AGENT},
    )

  assert client is not None
  try:
    _base_url, account_payload = await _negotiate_xtream_handshake(
      client, candidates, base_params, xt
    )
    return XtreamAccountInfo.from_api_payload(account_payload)
  finally:
    if owns_client:
      await client.close()


async def fetch_xtream_catalog(
  config: AppConfig,
  *,
  force_refresh: bool = False,
  persist_cache: bool = True,
  client: aiohttp.ClientSession | None = None,
) -> XtreamCatalog:
  """Fetch live categories and streams from upstream Xtream Codes API."""
  xt = config.xtream
  if not xt.server_url or not xt.username or not xt.password:
    raise XtreamError(
      "Missing Xtream credentials. Configure upstream_xtream credentials in "
      "credentials.yaml, config.yaml, or .env (or set paths.seed_json for offline files)."
    )

  cache_dir = config.resolve_path(xt.cache_dir)
  cache_file = _cache_file_for(cache_dir, xt.server_url, xt.username)

  if persist_cache and not force_refresh:
    cached = _load_cached_catalog(cache_file, xt.cache_ttl_hours)
    if cached is not None:
      if not xt.server_url.startswith(("http://", "https://")):
        proto = cached.account.server_protocol or "http"
        xt.server_url = f"{proto}://{xt.server_url.rstrip('/')}"
      return cached

  candidates = _candidate_base_urls(xt.server_url)
  base_url = candidates[0]
  base_params = {"username": xt.username, "password": xt.password}

  owns_client = client is None
  if owns_client:
    client = aiohttp.ClientSession(
      timeout=aiohttp.ClientTimeout(total=xt.request_timeout_sec),
      headers={"User-Agent": DEFAULT_USER_AGENT},
    )

  assert client is not None
  try:
    base_url, account_payload = await _negotiate_xtream_handshake(
      client, candidates, base_params, xt
    )
    api_url = f"{base_url}/player_api.php"

    account = XtreamAccountInfo.from_api_payload(account_payload)
    account_inactive = account.status.strip().lower() not in {
      "active",
      "unknown",
      "",
    }

    # 2. Fetch live categories.
    async with client.get(
      api_url,
      params={**base_params, "action": "get_live_categories"},
    ) as cat_resp:
      cat_resp.raise_for_status()
      cat_raw = await cat_resp.json(content_type=None)
    categories: dict[str, str] = {}
    if isinstance(cat_raw, list):
      for entry in cat_raw:
        if isinstance(entry, dict) and entry.get("category_id") is not None:
          categories[str(entry["category_id"])] = str(
            entry.get("category_name") or ""
          ).strip()

    # 3. Fetch live streams.
    async with client.get(
      api_url,
      params={**base_params, "action": "get_live_streams"},
    ) as streams_resp:
      streams_resp.raise_for_status()
      streams_raw = await streams_resp.json(content_type=None)
    if not isinstance(streams_raw, list):
      if account_inactive:
        raise XtreamError(
          f"Xtream account is not active (status={account.status!r}) and returned no stream list."
        )
      raise XtreamError(
        "Unexpected response for action=get_live_streams (expected JSON list)."
      )

    streams: list[XtreamStream] = []
    valid_raw_items: list[dict[str, Any]] = []
    for item in streams_raw:
      if not isinstance(item, dict):
        continue
      stream = XtreamStream.from_dict(item, categories_by_id=categories)
      if stream.stream_id and stream.name:
        streams.append(stream)
        valid_raw_items.append(item)

    if account_inactive and not streams:
      raise XtreamError(
        f"Xtream account is not active (status={account.status!r}) and returned 0 streams."
      )

    if persist_cache and streams:
      _save_cached_catalog(
        cache_file, account_payload, categories, valid_raw_items
      )
    return XtreamCatalog(
      account=account,
      categories=categories,
      streams=streams,
      from_cache=False,
    )
  except aiohttp.ClientResponseError as exc:
    raise XtreamError(
      f"Xtream API HTTP {exc.status} error from {base_url}"
    ) from exc
  except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
    raise XtreamError(
      f"Network error contacting Xtream server {base_url}: "
      f"{redact_url_secrets(str(exc))}"
    ) from exc
  except ValueError as exc:
    raise XtreamError(
      f"Invalid JSON returned by Xtream server {base_url}: "
      f"{redact_url_secrets(str(exc))}"
    ) from exc
  finally:
    if owns_client:
      await client.close()


def load_catalog_from_data(
  data: Any, *, source_name: str = "<memory>"
) -> XtreamCatalog:
  """Parse an Xtream catalog from an in-memory JSON-decoded object or list."""
  categories: dict[str, str] = {}
  account = XtreamAccountInfo(
    username="local_json", status="Active", max_connections=2
  )
  raw_streams: list[Any] = []

  if isinstance(data, list):
    raw_streams = data
  elif isinstance(data, dict):
    if "user_info" in data or "server_info" in data:
      account = XtreamAccountInfo.from_api_payload(data)
    elif isinstance(data.get("account_payload"), dict):
      account = XtreamAccountInfo.from_api_payload(data["account_payload"])
    elif isinstance(data.get("account"), dict):
      acc_d = data["account"]
      account = XtreamAccountInfo(
        username=str(acc_d.get("username", "local_json")),
        status=str(acc_d.get("status", "Active")),
        exp_date=acc_d.get("exp_date"),
        is_trial=bool(acc_d.get("is_trial", False)),
        active_connections=int(acc_d.get("active_connections", 0)),
        max_connections=max(1, int(acc_d.get("max_connections", 1))),
        allowed_output_formats=list(
          acc_d.get("allowed_output_formats") or ["ts", "m3u8"]
        ),
        server_protocol=str(acc_d.get("server_protocol", "http")),
        server_port=acc_d.get("server_port"),
        timezone=acc_d.get("timezone"),
      )

    raw_cats = data.get("live_categories") or data.get("categories") or {}
    if isinstance(raw_cats, dict) and isinstance(raw_cats.get("live"), list):
      raw_cats = raw_cats["live"]
    if isinstance(raw_cats, dict):
      categories = {str(k): str(v) for k, v in raw_cats.items()}
    elif isinstance(raw_cats, list):
      for entry in raw_cats:
        if isinstance(entry, dict) and entry.get("category_id") is not None:
          categories[str(entry["category_id"])] = str(
            entry.get("category_name") or ""
          ).strip()

    raw_streams = (
      data.get("streams")
      or data.get("live_streams")
      or data.get("available_channels")
      or []
    )
    if isinstance(raw_streams, dict):
      raw_streams = list(raw_streams.values())
  else:
    raise XtreamError(f"Unsupported JSON format in {source_name}")

  streams: list[XtreamStream] = []
  for item in raw_streams:
    if isinstance(item, dict):
      stream = XtreamStream.from_dict(item, categories_by_id=categories)
      if stream.stream_id and stream.name:
        streams.append(stream)

  return XtreamCatalog(
    account=account,
    categories=categories,
    streams=streams,
    from_cache=True,
  )


def load_catalog_from_json(json_path: Path) -> XtreamCatalog:
  """Load an Xtream JSON playlist from a local file."""
  with json_path.open("r", encoding="utf-8") as fh:
    data = json.load(fh)
  return load_catalog_from_data(data, source_name=str(json_path))
