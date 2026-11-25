"""Schedules Direct SD-JSON (`20141201`) EPG client backed by relational SQLite.

Implements multi-day schedule & program retrieval from Schedules Direct without
importing `bellhop.*` (preserving the strict `bellhop_web` package boundary):
1. Authenticates via `POST /token` (SHA-1 password digest + 23-hour token cache).
2. Resolves `WebChannel` slots to Gracenote `stationID`s via native IDs or the
   account's subscribed Schedules Direct lineups (`GET /status`, `GET /lineups/{id}`,
   and optional `GET /headends` + `PUT /lineups/{id}` auto-subscription) cached
   in `sd_lineups`, `sd_stations`, and `sd_channel_map`.
3. Checks `POST /schedules/md5` against `sd_schedule_days` and fetches
   `POST /schedules` + `POST /programs` only for changed `(stationID, date)`
   schedules and unseen/updated `programID`s in `sd_programs`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp

from bellhop_web.epg_db import (
  DEFAULT_EPG_DB_FILE,
  EpgDatabase,
  SdLineupRecord,
  SdStationRow,
)
from bellhop_web.file_utils import chmod_private_file, write_private_text
from bellhop_web.http_utils import (
  ClientSessionFactory,
  default_session_factory,
  upstream_url,
)
from bellhop_web.store import (
  LineupSnapshot,
  ProgramItem,
  WebChannel,
  reconcile_non_overlapping_schedule,
)

logger = logging.getLogger("bellhop_web.sd_epg")

DEFAULT_SD_BASE_URL = "https://json.schedulesdirect.org/20141201"
SD_USER_AGENT = "Bellhop/0.1"
TOKEN_TTL_SEC = 23 * 3600
LINEUP_MAP_TTL_SEC = 24 * 3600
MAX_SCHEDULE_BATCH = 500
MAX_PROGRAM_BATCH = 500

_SHA1_HEX_RE = re.compile(r"^[0-9a-f]{40}$")
_SD_LINEUP_ID_RE = re.compile(
  r"^(?:SD:)?([A-Z]{3}-[A-Z0-9][A-Z0-9_-]*?(?:-[A-Z0-9]+)?)$",
  re.IGNORECASE,
)
_CA_FSA_RE = re.compile(r"^[A-Z]\d[A-Z]$")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_CALLSIGN_SUFFIX_RE = re.compile(r"(?:[-_ ]?(?:DT|HD|TV|FM|CA|CAN|\d+))+$")
_REAUTH_CODES = frozenset({4001, 4003, 4004, 4007})
_SKIP_STATION_CODES = frozenset({7010, 7020, 7030, 7100, 2201})

_FSA_DEFAULT_SUFFIX: dict[str, str] = {
  "H2Y": "1C6",
  "M5V": "2T6",
  "V6B": "1A1",
  "T2P": "1J9",
  "K1P": "5G4",
  "G1R": "4P5",
  "R3C": "0V8",
  "B3J": "1S9",
}


class SchedulesDirectEpgError(RuntimeError):
  """Raised when Schedules Direct authentication or EPG retrieval fails."""


def hash_sd_password(password: str) -> str:
  """Return lowercase SHA-1 hex digest required by Schedules Direct `/token`."""
  cleaned = password.strip()
  if _SHA1_HEX_RE.match(cleaned.lower()):
    return cleaned.lower()
  return hashlib.sha1(cleaned.encode("utf-8")).hexdigest().lower()


def clean_sd_lineup_id(lineup_id: str) -> str | None:
  """Return canonical uppercase Gracenote lineup ID if `lineup_id` matches SD format."""
  m = _SD_LINEUP_ID_RE.match(lineup_id.strip())
  if not m:
    return None
  cand = m.group(1).upper()
  if cand.startswith("TVG-"):
    return None
  return cand


def _norm_key(text: str) -> str:
  return _NON_ALNUM_RE.sub("", text.strip().lower())


def _callsign_stem(callsign: str) -> str:
  up = callsign.strip().upper()
  stemmed = _CALLSIGN_SUFFIX_RE.sub("", up)
  return stemmed if len(stemmed) >= 3 else up


def _parse_air_ts(raw_iso: str) -> int:
  cleaned = raw_iso.strip()
  if not cleaned:
    return 0
  if cleaned.endswith("Z"):
    cleaned = f"{cleaned[:-1]}+00:00"
  try:
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
      dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())
  except ValueError:
    return 0


def _tokens(text: str) -> set[str]:
  stop = {"tv", "hd", "sd", "channel", "network", "the", "la", "le", "de"}
  return {
    tok
    for tok in re.findall(r"[a-z0-9]+", text.lower())
    if len(tok) >= 2 and tok not in stop
  }


@dataclass(slots=True)
class SchedulesDirectEpgConfig:
  """Settings for Schedules Direct SD-JSON EPG augmentation."""

  username: str = ""
  password: str = ""
  base_url: str = DEFAULT_SD_BASE_URL
  auto_add_lineup: bool = True
  preferred_lineup_id: str = ""
  postal_code: str = ""
  days: int = 3
  timeout_sec: float = 45.0
  token_cache_file: Path = Path("cache/sd_token.json")
  db_file: Path = DEFAULT_EPG_DB_FILE

  @property
  def enabled(self) -> bool:
    """Return `True` when both Schedules Direct username and password are set."""
    return bool(self.username.strip() and self.password.strip())


@dataclass(slots=True)
class SdFetchSummary:
  """Result and telemetry from a Schedules Direct EPG refresh pass."""

  programs_by_chno: dict[int, list[ProgramItem]]
  matched_channels: int = 0
  fetched_schedule_days: int = 0
  fetched_programs: int = 0


class SchedulesDirectEpgClient:
  """SQLite-backed SD-JSON (`20141201`) EPG fetcher with MD5 differential sync."""

  def __init__(
    self,
    config: SchedulesDirectEpgConfig,
    *,
    db: EpgDatabase | None = None,
    session_factory: ClientSessionFactory | None = None,
    clock: Any = time.time,
  ) -> None:
    self.config = config
    self.db = db or EpgDatabase(config.db_file)
    self._session_factory: ClientSessionFactory = (
      session_factory or default_session_factory
    )
    self._clock = clock
    self._token: str | None = None

  @property
  def enabled(self) -> bool:
    """Return `True` when Schedules Direct credentials are configured."""
    return self.config.enabled

  # ---------------------------------------------------------------------------
  # Token Persistence & HTTP Transport
  # ---------------------------------------------------------------------------
  def _load_token(self) -> str | None:
    if self._token:
      return self._token
    path = self.config.token_cache_file
    if not path.is_file():
      return None
    try:
      chmod_private_file(path)
      raw = json.loads(path.read_text(encoding="utf-8"))
      expected_base = self.config.base_url.strip().rstrip("/")
      cached_base = str(raw.get("base_url") or "").strip().rstrip("/")
      if (
        isinstance(raw, dict)
        and raw.get("username") == self.config.username.strip()
        and (not cached_base or cached_base == expected_base)
        and raw.get("token")
        and (float(self._clock()) - float(raw.get("saved_at", 0.0)))
        < TOKEN_TTL_SEC
      ):
        self._token = str(raw["token"]).strip()
        return self._token
    except (OSError, ValueError, TypeError):
      pass
    return None

  def _save_token(self, token: str) -> None:
    self._token = token
    path = self.config.token_cache_file
    with contextlib.suppress(OSError):
      write_private_text(
        path,
        json.dumps(
          {
            "username": self.config.username.strip(),
            "base_url": self.config.base_url.strip().rstrip("/"),
            "token": token,
            "saved_at": float(self._clock()),
          },
          indent=2,
        ),
      )

  async def _authenticate(self, session: aiohttp.ClientSession) -> str:
    url = f"{self.config.base_url.rstrip('/')}/token"
    payload = {
      "username": self.config.username.strip(),
      "password": hash_sd_password(self.config.password),
    }
    async with session.post(upstream_url(url), json=payload) as resp:
      status = resp.status
      data = await resp.json(content_type=None)
    if not isinstance(data, dict):
      raise SchedulesDirectEpgError(
        f"Schedules Direct /token returned non-object JSON (HTTP {status})"
      )
    code = int(data.get("code", 0) or 0)
    token = str(data.get("token") or "").strip()
    if status >= 400 or code != 0 or not token:
      msg = data.get("message") or data.get("response") or f"HTTP {status}"
      raise SchedulesDirectEpgError(
        f"Schedules Direct authentication failed (code={code}): {msg}"
      )
    self._save_token(token)
    return token

  async def _request_json(
    self,
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: Any = None,
  ) -> Any:
    token = self._load_token() or await self._authenticate(session)
    url = f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"

    for attempt in range(2):
      async with session.request(
        method,
        upstream_url(url),
        params=params,
        json=json_body,
        headers={"token": token},
      ) as resp:
        need_reauth = resp.status in (401, 403)
        data = await resp.json(content_type=None)
        if isinstance(data, dict):
          err_code = int(data.get("code", 0) or 0)
          if err_code in _REAUTH_CODES:
            need_reauth = True
          elif not need_reauth and err_code != 0:
            msg = (
              data.get("message")
              or data.get("response")
              or f"SD error {err_code}"
            )
            raise SchedulesDirectEpgError(
              f"Schedules Direct {path} error (code={err_code}): {msg}"
            )
        if not need_reauth:
          if resp.status >= 400:
            raise SchedulesDirectEpgError(
              f"Schedules Direct {path} failed with HTTP {resp.status}"
            )
          return data

      if attempt == 0:
        self._token = None
        token = await self._authenticate(session)
    raise SchedulesDirectEpgError(
      f"Schedules Direct {path} failed after token refresh"
    )

  # ---------------------------------------------------------------------------
  # Lineup & Station Mapping (SQLite-backed)
  # ---------------------------------------------------------------------------
  def _normalize_discovery_postal(self, raw_postal: str) -> tuple[str, str]:
    clean = re.sub(r"\s+", "", raw_postal.strip().upper())
    if not clean:
      return "", "CAN"
    if clean[0].isalpha():
      if _CA_FSA_RE.match(clean):
        suffix = _FSA_DEFAULT_SUFFIX.get(clean, "1A1")
        return f"{clean}{suffix}", "CAN"
      return clean, "CAN"
    return clean[:5], "USA"

  async def _discover_and_add_lineup(
    self, session: aiohttp.ClientSession, snap: LineupSnapshot
  ) -> list[dict[str, str]]:
    preferred = clean_sd_lineup_id(
      self.config.preferred_lineup_id or snap.lineup_id
    )
    if preferred and not preferred.endswith("BELLFIBE-MTL-X"):
      await self._request_json(session, "PUT", f"lineups/{preferred}")
      return [{"lineup": preferred, "modified": ""}]

    postal, country = self._normalize_discovery_postal(
      self.config.postal_code or snap.postal_prefix
    )
    if not postal:
      return []

    headends = await self._request_json(
      session,
      "GET",
      "headends",
      params={"country": country, "postalcode": postal},
    )
    if not isinstance(headends, list):
      return []

    prov_tokens = _tokens(snap.provider)
    best_lid = ""
    best_score = -1
    best_name = ""
    best_loc = ""
    for he in headends:
      if not isinstance(he, dict):
        continue
      loc = str(he.get("location") or "")
      for lu in he.get("lineups") or []:
        if not isinstance(lu, dict) or not lu.get("lineup"):
          continue
        lid = str(lu["lineup"]).strip()
        lname = str(lu.get("name") or "")
        cand_tokens = _tokens(f"{lname} {loc}")
        score = len(prov_tokens & cand_tokens) * 10
        if "fibe" in lname.lower() and "fibe" in snap.provider.lower():
          score += 25
        if score > best_score:
          best_score = score
          best_lid = lid
          best_name = lname
          best_loc = loc

    if not best_lid:
      return []

    logger.info(
      "Auto-subscribing Schedules Direct account to lineup %s for %s",
      best_lid,
      snap.provider,
    )
    await self._request_json(session, "PUT", f"lineups/{best_lid}")
    return [
      {
        "lineup": best_lid,
        "name": best_name,
        "location": best_loc,
        "modified": "",
      }
    ]

  async def _refresh_lineup_maps(
    self, session: aiohttp.ClientSession, snap: LineupSnapshot
  ) -> list[SdLineupRecord]:
    now = float(self._clock())
    cached_map = await asyncio.to_thread(self.db.load_sd_lineups)
    if cached_map and all(
      (now - lu.fetched_at) < LINEUP_MAP_TTL_SEC for lu in cached_map.values()
    ):
      return list(cached_map.values())

    status_data = await self._request_json(session, "GET", "status")
    acct_lineups: list[dict[str, str]] = []
    if isinstance(status_data, dict):
      for item in status_data.get("lineups") or []:
        if isinstance(item, dict) and item.get("lineup"):
          acct_lineups.append(
            {
              "lineup": str(item["lineup"]).strip(),
              "name": str(item.get("name") or "").strip(),
              "location": str(item.get("location") or "").strip(),
              "modified": str(item.get("modified") or ""),
            }
          )

    if not acct_lineups and self.config.auto_add_lineup:
      acct_lineups = await self._discover_and_add_lineup(session, snap)

    explicit_sd = clean_sd_lineup_id(
      self.config.preferred_lineup_id or snap.lineup_id
    )
    if (
      explicit_sd
      and not explicit_sd.endswith("BELLFIBE-MTL-X")
      and self.config.auto_add_lineup
      and all(x["lineup"].upper() != explicit_sd for x in acct_lineups)
    ):
      try:
        await self._request_json(session, "PUT", f"lineups/{explicit_sd}")
        acct_lineups.insert(0, {"lineup": explicit_sd, "modified": ""})
      except SchedulesDirectEpgError as exc:
        logger.warning("Could not auto-add SD lineup %s: %s", explicit_sd, exc)

    entries: list[SdLineupRecord] = []
    for lu_meta in acct_lineups:
      lid = lu_meta["lineup"]
      remote_mod = lu_meta["modified"]
      cached = cached_map.get(lid)
      if (
        cached is not None
        and cached.stations
        and remote_mod
        and cached.modified == remote_mod
      ):
        cached.fetched_at = now
        await asyncio.to_thread(self.db.touch_sd_lineup_fetched_at, lid, now)
        entries.append(cached)
        continue

      payload = await self._request_json(session, "GET", f"lineups/{lid}")
      if not isinstance(payload, dict):
        continue
      stations: dict[str, SdStationRow] = {}
      for st_raw in payload.get("stations") or []:
        if not isinstance(st_raw, dict) or not st_raw.get("stationID"):
          continue
        sid = str(st_raw["stationID"]).strip()
        langs = st_raw.get("broadcastLanguage")
        lang = (
          str(langs[0]).lower()[:2] if isinstance(langs, list) and langs else ""
        )
        stations[sid] = SdStationRow(
          station_id=sid,
          name=str(st_raw.get("name") or st_raw.get("callsign") or sid).strip(),
          callsign=str(st_raw.get("callsign") or "").strip(),
          language=lang,
        )

      chno_map: dict[int, str] = {}
      for m_raw in payload.get("map") or []:
        if not isinstance(m_raw, dict):
          continue
        sid = str(m_raw.get("stationID") or "").strip()
        raw_ch = str(
          m_raw.get("channel")
          or m_raw.get("atscMajor")
          or m_raw.get("uhfVhf")
          or ""
        ).strip()
        digits = re.findall(r"\d+", raw_ch)
        if sid and digits:
          chno_val = int(digits[0])
          chno_map.setdefault(chno_val, sid)

      meta_info = payload.get("metadata")
      meta_dict = meta_info if isinstance(meta_info, Mapping) else {}
      raw_meta_name = str(meta_dict.get("lineup") or "").strip()
      resolved_name = (
        lu_meta.get("name")
        if (not raw_meta_name or raw_meta_name.upper() == lid.upper())
        and lu_meta.get("name")
        else (raw_meta_name or lid)
      )
      entry = SdLineupRecord(
        lineup_id=lid,
        name=str(resolved_name or lid),
        location=str(
          meta_dict.get("location") or lu_meta.get("location") or ""
        ),
        modified=str(meta_dict.get("modified") or remote_mod),
        fetched_at=now,
        stations=stations,
        chno_to_station=chno_map,
      )
      await asyncio.to_thread(self.db.save_sd_lineup, entry)
      entries.append(entry)

    return entries

  @staticmethod
  def _match_channels_to_stations(
    snap: LineupSnapshot,
    lineups: Sequence[SdLineupRecord],
  ) -> dict[int, str]:
    """Map `snap.channels` (`chno`) -> Gracenote `stationID`."""
    all_stations: dict[str, SdStationRow] = {}
    chno_to_sid: dict[int, str] = {}
    by_callsign: dict[str, list[SdStationRow]] = {}
    by_stem: dict[str, list[SdStationRow]] = {}
    by_name: dict[str, list[SdStationRow]] = {}

    prov_tokens = _tokens(snap.provider)
    ordered_lus = sorted(
      lineups,
      key=lambda lu: (
        1 if lu.lineup_id.upper() == snap.lineup_id.upper() else 0,
        len(prov_tokens & _tokens(f"{lu.name} {lu.location}")),
        len(lu.stations),
      ),
      reverse=True,
    )
    same_provider_headend = bool(
      ordered_lus
      and (
        ordered_lus[0].lineup_id.upper() == snap.lineup_id.upper()
        or bool(prov_tokens & _tokens(ordered_lus[0].name))
      )
    )

    for lu in ordered_lus:
      for ch_num, sid in lu.chno_to_station.items():
        chno_to_sid.setdefault(ch_num, sid)
      for sid, st in lu.stations.items():
        if sid in all_stations:
          continue
        all_stations[sid] = st
        if st.callsign:
          cs_up = st.callsign.upper()
          by_callsign.setdefault(cs_up, []).append(st)
          by_stem.setdefault(_callsign_stem(cs_up), []).append(st)
        nk = _norm_key(st.name)
        if nk:
          by_name.setdefault(nk, []).append(st)

    def _compatible(ch: WebChannel, st: SdStationRow) -> bool:
      if (
        ch.callsign
        and st.callsign
        and _callsign_stem(ch.callsign) == _callsign_stem(st.callsign)
      ):
        return True
      ch_toks = _tokens(f"{ch.name} {ch.callsign or ''}")
      st_toks = _tokens(f"{st.name} {st.callsign}")
      if ch_toks & st_toks:
        return True
      ch_nk = _norm_key(ch.name)
      st_nk = _norm_key(st.name)
      return bool(ch_nk and st_nk and (ch_nk in st_nk or st_nk in ch_nk))

    resolved: dict[int, str] = {}
    for ch in snap.channels:
      gid = ch.gracenote_station_id.strip()
      if gid and (
        gid in all_stations
        or (gid.isdigit() and 4 <= len(gid) <= 7 and not all_stations)
      ):
        resolved[ch.chno] = gid
        continue

      dial_sid = chno_to_sid.get(ch.chno) or (
        chno_to_sid.get(ch.sd_chno) if ch.sd_chno else None
      )
      if (
        dial_sid
        and dial_sid in all_stations
        and _compatible(ch, all_stations[dial_sid])
      ):
        resolved[ch.chno] = dial_sid
        continue

      if ch.callsign:
        cs_up = ch.callsign.strip().upper()
        exact_cs = by_callsign.get(cs_up)
        if exact_cs:
          resolved[ch.chno] = exact_cs[0].station_id
          continue
        stem_cs = by_stem.get(_callsign_stem(cs_up))
        if stem_cs and len(stem_cs) == 1:
          resolved[ch.chno] = stem_cs[0].station_id
          continue

      name_hits = by_name.get(_norm_key(ch.name))
      if name_hits:
        lang_low = ch.language.lower()[:2]
        lang_matched = [
          st for st in name_hits if st.language and st.language == lang_low
        ]
        chosen_st = lang_matched[0] if lang_matched else name_hits[0]
        resolved[ch.chno] = chosen_st.station_id
        continue

      if same_provider_headend and dial_sid and dial_sid in all_stations:
        resolved[ch.chno] = dial_sid

    return resolved

  # ---------------------------------------------------------------------------
  # Program Metadata Formatting
  # ---------------------------------------------------------------------------
  @staticmethod
  def _extract_program_fields(raw_prog: Mapping[str, Any]) -> dict[str, str]:
    title = ""
    titles = raw_prog.get("titles")
    if isinstance(titles, list):
      for t_obj in titles:
        if isinstance(t_obj, Mapping) and t_obj.get("title120"):
          title = str(t_obj["title120"]).strip()
          break

    ep_title = str(raw_prog.get("episodeTitle150") or "").strip()
    season_ep = ""
    meta_list = raw_prog.get("metadata")
    if isinstance(meta_list, list):
      for m_entry in meta_list:
        if not isinstance(m_entry, Mapping):
          continue
        gn = m_entry.get("Gracenote")
        if isinstance(gn, Mapping):
          s_num = gn.get("season")
          e_num = gn.get("episode")
          if (
            isinstance(s_num, int)
            and isinstance(e_num, int)
            and s_num > 0
            and e_num > 0
          ):
            season_ep = f"S{s_num:02d}E{e_num:02d}"
            break

    if not ep_title:
      ev = raw_prog.get("eventDetails")
      if isinstance(ev, Mapping):
        teams = ev.get("teams")
        if isinstance(teams, list) and len(teams) >= 2:
          names = [
            str(t.get("name") or "").strip()
            for t in teams
            if isinstance(t, Mapping) and t.get("name")
          ]
          if len(names) >= 2:
            ep_title = f"{names[0]} vs. {names[1]}"
        if not ep_title and ev.get("subType"):
          ep_title = str(ev["subType"]).strip()

    if not ep_title:
      mv = raw_prog.get("movie")
      if isinstance(mv, Mapping) and mv.get("year"):
        ep_title = str(mv["year"]).strip()

    sub_title = (
      f"{season_ep} · {ep_title}"
      if (season_ep and ep_title)
      else (season_ep or ep_title)
    )

    desc = ""
    descs = raw_prog.get("descriptions")
    if isinstance(descs, Mapping):
      for bucket_key in ("description1000", "description100"):
        arr = descs.get(bucket_key)
        if isinstance(arr, list):
          for d_obj in arr:
            if isinstance(d_obj, Mapping) and d_obj.get("description"):
              desc = str(d_obj["description"]).strip()
              break
        if desc:
          break

    category = ""
    genres = raw_prog.get("genres")
    if isinstance(genres, list) and genres:
      category = str(genres[0]).strip()
    elif raw_prog.get("entityType"):
      category = str(raw_prog["entityType"]).strip()

    return {
      "md5": str(raw_prog.get("md5") or raw_prog.get("hash") or ""),
      "title": title,
      "sub_title": sub_title,
      "description": desc,
      "category": category,
    }

  # ---------------------------------------------------------------------------
  # Main EPG Fetch Entrypoint (SQLite MD5 Differential Sync)
  # ---------------------------------------------------------------------------
  async def fetch_for_snapshot(
    self,
    snap: LineupSnapshot,
    *,
    now_ts: int | None = None,
    max_per_channel: int = 120,
  ) -> SdFetchSummary:
    """Fetch Schedules Direct listings for `snap` using SQLite MD5 differential caching."""
    if not self.enabled or not snap.channels:
      return SdFetchSummary(programs_by_chno={})

    await asyncio.to_thread(self.db.ensure_initialized)
    ref_now = int(self._clock()) if now_ts is None else now_ts
    recent_cutoff = ref_now - 7200
    base_date = datetime.fromtimestamp(recent_cutoff, tz=timezone.utc).date()
    num_days = max(1, min(14, self.config.days))
    dates = [
      (base_date + timedelta(days=offset)).strftime("%Y-%m-%d")
      for offset in range(num_days)
    ]

    session = self._session_factory(
      timeout=aiohttp.ClientTimeout(
        total=max(25.0, self.config.timeout_sec),
        connect=15.0,
      ),
      headers={
        "User-Agent": SD_USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "deflate, gzip",
      },
    )
    try:
      lineups = await self._refresh_lineup_maps(session, snap)
      chno_to_sid = self._match_channels_to_stations(snap, lineups)
      if not chno_to_sid:
        return SdFetchSummary(programs_by_chno={})

      unique_sids = sorted(set(chno_to_sid.values()))
      cached_sched_md5s = await asyncio.to_thread(
        self.db.get_sd_schedule_md5s, unique_sids, dates
      )

      # 1. Differential MD5 check (`POST /schedules/md5`)
      stale_requests: list[dict[str, Any]] = []
      for start_idx in range(0, len(unique_sids), MAX_SCHEDULE_BATCH):
        batch_sids = unique_sids[start_idx : start_idx + MAX_SCHEDULE_BATCH]
        md5_req = [{"stationID": sid, "date": dates} for sid in batch_sids]
        md5_resp = await self._request_json(
          session, "POST", "schedules/md5", json_body=md5_req
        )
        md5_map = md5_resp if isinstance(md5_resp, Mapping) else {}
        for sid in batch_sids:
          remote_dates = md5_map.get(sid)
          remote_by_dt = (
            remote_dates if isinstance(remote_dates, Mapping) else {}
          )
          if int(remote_by_dt.get("code", 0) or 0) != 0:
            continue
          needed_dts: list[str] = []
          for dt_str in dates:
            r_info = remote_by_dt.get(dt_str)
            if isinstance(r_info, Mapping):
              r_code = int(r_info.get("code", 0) or 0)
              if r_code in _SKIP_STATION_CODES:
                continue
              r_md5 = str(r_info.get("md5") or r_info.get("hash") or "")
              if r_md5 and cached_sched_md5s.get((sid, dt_str)) == r_md5:
                continue
            needed_dts.append(dt_str)
          if needed_dts:
            stale_requests.append({"stationID": sid, "date": needed_dts})

      # 2. Fetch changed station-day schedules (`POST /schedules`) & commit to SQLite
      fetched_sched_days = 0
      new_sched_rows: list[tuple[str, str, str, list[dict[str, Any]]]] = []
      for start_idx in range(0, len(stale_requests), MAX_SCHEDULE_BATCH):
        sched_batch = stale_requests[start_idx : start_idx + MAX_SCHEDULE_BATCH]
        sched_resp = await self._request_json(
          session, "POST", "schedules", json_body=sched_batch
        )
        if not isinstance(sched_resp, list):
          continue
        for item in sched_resp:
          if not isinstance(item, Mapping):
            continue
          if int(item.get("code", 0) or 0) != 0:
            continue
          sid = str(item.get("stationID") or "").strip()
          meta = item.get("metadata")
          meta_map = meta if isinstance(meta, Mapping) else {}
          start_dt = str(meta_map.get("startDate") or "").strip()
          s_md5 = str(meta_map.get("md5") or meta_map.get("hash") or "")
          raw_progs = item.get("programs")
          if not sid or not start_dt or not isinstance(raw_progs, list):
            continue
          compact_airings: list[dict[str, Any]] = []
          for air in raw_progs:
            if not isinstance(air, Mapping):
              continue
            pid = str(air.get("programID") or "").strip()
            air_ts = _parse_air_ts(str(air.get("airDateTime") or ""))
            dur = int(air.get("duration") or 0)
            if not pid or air_ts <= 0 or dur <= 0:
              continue
            compact_airings.append(
              {
                "pid": pid,
                "md5": str(air.get("md5") or air.get("hash") or ""),
                "start": air_ts,
                "dur": dur,
                "live": str(air.get("liveTapeDelay") or "").lower() == "live",
              }
            )
          new_sched_rows.append((sid, start_dt, s_md5, compact_airings))
          fetched_sched_days += 1

      min_keep = (base_date - timedelta(days=1)).strftime("%Y-%m-%d")
      await asyncio.to_thread(
        self.db.upsert_sd_schedule_days,
        new_sched_rows,
        min_keep_date=min_keep,
      )

      # 3. Load active airings from SQLite and diff `programID` MD5s against `sd_programs`
      airings_by_sid = await asyncio.to_thread(
        self.db.load_sd_airings, unique_sids, dates
      )
      wanted_pids: dict[str, str] = {}
      for sid in unique_sids:
        for air in airings_by_sid.get(sid, ()):
          stop_ts = int(air.get("start") or 0) + int(air.get("dur") or 0)
          if stop_ts >= recent_cutoff:
            pid = str(air.get("pid") or "")
            if pid:
              wanted_pids[pid] = str(air.get("md5") or "")

      cached_prog_md5s = await asyncio.to_thread(
        self.db.get_sd_program_md5s, list(wanted_pids)
      )
      missing_pids = [
        pid
        for pid, p_md5 in wanted_pids.items()
        if pid not in cached_prog_md5s
        or (p_md5 and cached_prog_md5s[pid] != p_md5)
      ]

      fetched_progs = 0
      for start_idx in range(0, len(missing_pids), MAX_PROGRAM_BATCH):
        pid_batch = missing_pids[start_idx : start_idx + MAX_PROGRAM_BATCH]
        prog_resp = await self._request_json(
          session, "POST", "programs", json_body=pid_batch
        )
        if not isinstance(prog_resp, list):
          continue
        batch_extracted: dict[str, dict[str, str]] = {}
        for raw_p in prog_resp:
          if not isinstance(raw_p, Mapping):
            continue
          if int(raw_p.get("code", 0) or 0) != 0:
            continue
          pid = str(raw_p.get("programID") or "").strip()
          if not pid:
            continue
          extracted = self._extract_program_fields(raw_p)
          if extracted["title"]:
            batch_extracted[pid] = extracted
            fetched_progs += 1
        if batch_extracted:
          await asyncio.to_thread(
            self.db.upsert_sd_programs,
            batch_extracted,
            updated_at=float(self._clock()),
          )

      # 4. Query only the active `programID` metadata needed for this window
      active_prog_meta = await asyncio.to_thread(
        self.db.load_sd_programs, list(wanted_pids)
      )

      station_programs: dict[str, list[ProgramItem]] = {}
      seq = 0
      for sid in unique_sids:
        raw_items: list[ProgramItem] = []
        for air in airings_by_sid.get(sid, ()):
          start_ts = int(air.get("start") or 0)
          stop_ts = start_ts + int(air.get("dur") or 0)
          if stop_ts < recent_cutoff:
            continue
          pid = str(air.get("pid") or "")
          p_meta = active_prog_meta.get(pid)
          if not p_meta or not p_meta.get("title"):
            continue
          seq += 1
          title = str(p_meta["title"])
          if air.get("live") and "live" not in title.lower():
            title = f"Live: {title}"
          raw_items.append(
            ProgramItem(
              id=f"sd-{seq}",
              channel_id=sid,
              title=title,
              sub_title=str(p_meta.get("sub_title") or ""),
              description=str(p_meta.get("description") or ""),
              category=str(p_meta.get("category") or ""),
              start_timestamp=start_ts,
              stop_timestamp=stop_ts,
            )
          )
        reconciled = reconcile_non_overlapping_schedule(raw_items)
        if reconciled:
          station_programs[sid] = reconciled[:max_per_channel]

      by_chno: dict[int, list[ProgramItem]] = {}
      for ch in snap.channels:
        matched_sid = chno_to_sid.get(ch.chno)
        if not matched_sid or matched_sid not in station_programs:
          continue
        cid = ch.epg_channel_id or f"sd.{matched_sid}"
        by_chno[ch.chno] = [
          ProgramItem(
            id=p.id,
            channel_id=cid,
            title=p.title,
            sub_title=p.sub_title,
            description=p.description,
            category=p.category,
            start_timestamp=p.start_timestamp,
            stop_timestamp=p.stop_timestamp,
          )
          for p in station_programs[matched_sid]
        ]

      logger.info(
        "Schedules Direct EPG resolved %d/%d channels (%d schedule-days fetched, %d programs fetched)",
        len(by_chno),
        len(snap.channels),
        fetched_sched_days,
        fetched_progs,
      )
      return SdFetchSummary(
        programs_by_chno=by_chno,
        matched_channels=len(chno_to_sid),
        fetched_schedule_days=fetched_sched_days,
        fetched_programs=fetched_progs,
      )
    except (
      aiohttp.ClientError,
      asyncio.TimeoutError,
      OSError,
      ValueError,
    ) as exc:
      raise SchedulesDirectEpgError(
        f"Schedules Direct HTTP error: {exc}"
      ) from exc
    finally:
      await session.close()
