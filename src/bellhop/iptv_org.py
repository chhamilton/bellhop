"""Conditional-cached `iptv-org` channel & feed metadata client (`ETag` / `Last-Modified`).

Fetches `channels.json` and `feeds.json` from `https://iptv-org.github.io/api` (public
domain / The Unlicense), tracks `ETag` and `Last-Modified` validators in
`cache/iptv_org/http_cache_meta.json` so upstream datasets are re-downloaded only when
changed (`HTTP 304 Not Modified` fast path), resolves `closed` / `replaced_by` station
transitions, and indexes station categories, ISO-639-3 languages, XMLTV IDs, and
alternate names for Phase (1) lineup normalization and dial grouping.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

DEFAULT_IPTV_ORG_API_URL = "https://iptv-org.github.io/api"
IPTV_ORG_USER_AGENT = "Bellhop/0.1 (+https://github.com/chhamilton/bellhop)"

COUNTRY_ISO2_MAP: dict[str, str] = {
  "CAN": "CA",
  "CA": "CA",
  "USA": "US",
  "US": "US",
  "GBR": "GB",
  "UK": "GB",
  "GB": "GB",
  "FRA": "FR",
  "FR": "FR",
}

NEIGHBOR_BROADCAST_COUNTRIES: dict[str, tuple[str, ...]] = {
  "CA": ("CA", "US", "FR", "GB"),
  "US": ("US", "CA", "MX", "GB"),
}

COUNTRY_FALLBACK_ISO3_LANG: dict[str, str] = {
  "US": "eng",
  "GB": "eng",
  "AU": "eng",
  "NZ": "eng",
  "IE": "eng",
  "FR": "fra",
  "IT": "ita",
  "ES": "spa",
  "MX": "spa",
  "AR": "spa",
  "CO": "spa",
  "DE": "deu",
  "AT": "deu",
  "PT": "por",
  "BR": "por",
  "GR": "ell",
  "RU": "rus",
  "UA": "ukr",
  "PL": "pol",
  "RO": "ron",
  "TR": "tur",
  "JP": "jpn",
  "KR": "kor",
  "CN": "zho",
  "TW": "zho",
  "HK": "yue",
  "IN": "hin",
  "PK": "urd",
  "PH": "tgl",
  "VN": "vie",
}

PAREN_STRIP_RE = re.compile(r"\([^()]*\)")
LETTER_DIGIT_SPLIT_RE = re.compile(r"^([a-z]{2,})(\d+)$")

LATIN_LIGATURE_TABLE = str.maketrans(
  {
    "œ": "oe",
    "Œ": "Oe",
    "æ": "ae",
    "Æ": "Ae",
    "ß": "ss",
    "ẞ": "SS",
    "ø": "o",
    "Ø": "O",
    "ł": "l",
    "Ł": "L",
    "đ": "d",
    "Đ": "D",
    "ð": "d",
    "Ð": "D",
    "þ": "th",
    "Þ": "Th",
    "’": "'",
    "‘": "'",
  }
)

US_NETWORK_CANONICAL_IDS: dict[str, str] = {
  "ABC": "ABC.us",
  "CBS": "CBS.us",
  "NBC": "NBC.us",
  "FOX": "Fox.us",
  "PBS": "PBS.us",
  "CW": "TheCW.us",
  "THECW": "TheCW.us",
}

LANG_ISO3_QUALIFIER_TOKENS: dict[str, frozenset[str]] = {
  "fra": frozenset({"francais", "french", "fr", "franco"}),
  "eng": frozenset({"english", "anglais", "en"}),
  "spa": frozenset({"espanol", "spanish", "es"}),
  "ita": frozenset({"italiano", "italian"}),
  "ara": frozenset({"arabic", "arabe"}),
  "por": frozenset({"portugues", "portuguese"}),
  "deu": frozenset({"deutsch", "german"}),
  "zho": frozenset({"chinese", "mandarin"}),
  "yue": frozenset({"cantonese", "chinese"}),
}

COUNTRY_QUALIFIER_TOKENS: dict[str, frozenset[str]] = {
  "CA": frozenset({"canada", "canadian", "ca"}),
  "US": frozenset({"usa", "us", "american"}),
  "GB": frozenset({"uk", "british"}),
  "FR": frozenset({"france"}),
}

GENERIC_BROADCAST_TOKENS = frozenset(
  {
    "channel",
    "network",
    "television",
    "tv",
    "chaine",
    "tele",
    "hd",
    "east",
    "west",
    "pacific",
    "atlantic",
  }
)

IPTV_ORG_SPINOFF_TOKENS = frozenset(
  {
    "plus",
    "extra",
    "xtra",
    "info",
    "news",
    "nouvelles",
    "sports",
    "sport",
    "kids",
    "jr",
    "junior",
    "xd",
    "wild",
    "scifi",
    "sci",
    "fi",
    "movies",
    "cinema",
    "action",
    "comedy",
    "drama",
    "life",
  }
)


class IptvOrgError(RuntimeError):
  """Raised when fetching or parsing `iptv-org` metadata fails without a fallback."""


def fold_latin_accents(text: str) -> str:
  """Collapse accented Latin characters and ligatures (`é`->`e`, `œ`->`oe`) to ASCII."""
  translated = text.translate(LATIN_LIGATURE_TABLE)
  nfkd = unicodedata.normalize("NFKD", translated)
  return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def tokenize_station_name(text: str) -> tuple[str, ...]:
  """Tokenize `text` into accent-folded lowercase words and split attached digits."""
  folded = fold_latin_accents(text).lower()
  folded = folded.replace("&", " and ").replace("+", " plus ")
  out: list[str] = []
  for tok in re.findall(r"[a-z0-9]+", folded):
    m = LETTER_DIGIT_SPLIT_RE.match(tok)
    if m:
      out.append(m.group(1))
      out.append(m.group(2))
    else:
      out.append(tok)
  return tuple(out)


def normalize_lookup_key(text: str) -> str:
  """Return lowercase accent-folded ASCII alphanumeric key for station matching."""
  return re.sub(r"[^a-z0-9]+", "", fold_latin_accents(text).lower())


def normalize_country_iso2(country: str) -> str:
  """Normalize 2- or 3-letter country code (`CAN`/`CA`, `USA`/`US`) to ISO-3166 alpha-2."""
  up = country.strip().upper()
  if up in COUNTRY_ISO2_MAP:
    return COUNTRY_ISO2_MAP[up]
  return up[:2] if len(up) >= 2 else "US"


@dataclass(frozen=True, slots=True)
class IptvOrgChannelInfo:
  """Active broadcast/cable station metadata resolved from `iptv-org`."""

  channel_id: str
  name: str
  country: str
  alt_names: tuple[str, ...] = ()
  network: str | None = None
  categories: tuple[str, ...] = ()
  languages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IptvOrgMatch:
  """Result of matching a provider lineup slot against `IptvOrgIndex`."""

  channel: IptvOrgChannelInfo
  was_replaced: bool = False
  matched_closed_id: str | None = None
  similarity: float = 1.0


@dataclass(slots=True)
class HttpCacheEntry:
  """Persisted HTTP conditional-request validators for one remote JSON resource."""

  url: str
  etag: str | None = None
  last_modified: str | None = None
  cache_control: str | None = None
  sha256: str = ""
  size_bytes: int = 0
  checked_at: float = 0.0
  updated_at: float = 0.0

  def to_dict(self) -> dict[str, Any]:
    """Serialize validator entry to JSON-compatible dictionary."""
    return {
      "url": self.url,
      "etag": self.etag,
      "last_modified": self.last_modified,
      "cache_control": self.cache_control,
      "sha256": self.sha256,
      "size_bytes": self.size_bytes,
      "checked_at": self.checked_at,
      "updated_at": self.updated_at,
    }

  @classmethod
  def from_dict(cls, raw: Mapping[str, Any]) -> HttpCacheEntry:
    """Deserialize validator entry from parsed `http_cache_meta.json`."""
    return cls(
      url=str(raw.get("url") or ""),
      etag=str(raw["etag"]) if raw.get("etag") else None,
      last_modified=(
        str(raw["last_modified"]) if raw.get("last_modified") else None
      ),
      cache_control=(
        str(raw["cache_control"]) if raw.get("cache_control") else None
      ),
      sha256=str(raw.get("sha256") or ""),
      size_bytes=int(raw.get("size_bytes", 0) or 0),
      checked_at=float(raw.get("checked_at", 0.0) or 0.0),
      updated_at=float(raw.get("updated_at", 0.0) or 0.0),
    )


class IptvOrgIndex:
  """In-memory lookup index built from `iptv-org` `channels.json` and `feeds.json`."""

  def __init__(self) -> None:
    self._by_id: dict[str, tuple[IptvOrgChannelInfo, bool, str | None]] = {}
    self._by_country_key: dict[
      tuple[str, str], list[tuple[IptvOrgChannelInfo, bool, str | None]]
    ] = defaultdict(list)
    self._by_global_key: dict[
      str, list[tuple[IptvOrgChannelInfo, bool, str | None]]
    ] = defaultdict(list)
    self._entries: list[
      tuple[
        IptvOrgChannelInfo,
        bool,
        str | None,
        tuple[tuple[str, tuple[str, ...]], ...],
      ]
    ] = []
    self._token_postings: dict[str, list[int]] = defaultdict(list)
    self._token_idf: dict[str, float] = {}
    self._active_country_keys: set[tuple[str, str]] = set()
    self._closed_unreplaced_country_keys: set[tuple[str, str]] = set()
    self._closed_unreplaced_ids: set[str] = set()
    self._station_count = 0

  @property
  def is_empty(self) -> bool:
    """Return True when the index holds zero stations."""
    return self._station_count == 0

  @property
  def station_count(self) -> int:
    """Return the number of active stations indexed."""
    return self._station_count

  @classmethod
  def from_json_payloads(
    cls,
    channels_raw: Sequence[Any],
    feeds_raw: Sequence[Any] = (),
  ) -> IptvOrgIndex:
    """Construct `IptvOrgIndex`, resolving `replaced_by` chains and closed stations."""
    idx = cls()

    ota_feed_re = re.compile(
      r"^([CWK][A-Z]{2,4})(?:-?(?:DT|TV|HD))?(?:-\d+|\d)?$"
    )
    langs_by_id: dict[str, list[str]] = defaultdict(list)
    feed_callsigns_by_id: dict[str, list[str]] = defaultdict(list)
    for feed in feeds_raw:
      if not isinstance(feed, Mapping):
        continue
      cid = str(feed.get("channel") or "").strip()
      if not cid:
        continue
      for raw_fc in (feed.get("id"), feed.get("name")):
        fc = str(raw_fc or "").strip().upper()
        m_ota = ota_feed_re.match(fc)
        if m_ota:
          for cand_cs in (fc, m_ota.group(1)):
            if cand_cs not in feed_callsigns_by_id[cid]:
              feed_callsigns_by_id[cid].append(cand_cs)
      raw_langs = feed.get("languages")
      if not isinstance(raw_langs, list):
        continue
      clean_langs = [str(lg).strip().lower() for lg in raw_langs if lg]
      if bool(feed.get("is_main")):
        merged = list(clean_langs)
        for existing in langs_by_id[cid]:
          if existing not in merged:
            merged.append(existing)
        langs_by_id[cid] = merged
      else:
        for lg in clean_langs:
          if lg not in langs_by_id[cid]:
            langs_by_id[cid].append(lg)

    raw_by_id: dict[str, Mapping[str, Any]] = {}
    for entry in channels_raw:
      if not isinstance(entry, Mapping):
        continue
      cid = str(entry.get("id") or "").strip()
      if cid:
        raw_by_id[cid] = entry

    def _build_active_info(raw_ch: Mapping[str, Any]) -> IptvOrgChannelInfo:
      cid = str(raw_ch["id"]).strip()
      country = str(raw_ch.get("country") or "").strip().upper()
      raw_alts = raw_ch.get("alt_names")
      alts = (
        tuple(str(a).strip() for a in raw_alts if str(a).strip())
        if isinstance(raw_alts, list)
        else ()
      )
      raw_cats = raw_ch.get("categories")
      cats = (
        tuple(str(c).strip().lower() for c in raw_cats if str(c).strip())
        if isinstance(raw_cats, list)
        else ()
      )
      flangs = tuple(langs_by_id.get(cid, ()))
      if not flangs and country in COUNTRY_FALLBACK_ISO3_LANG:
        flangs = (COUNTRY_FALLBACK_ISO3_LANG[country],)
      net_raw = raw_ch.get("network")
      net = str(net_raw).strip() if net_raw else None
      return IptvOrgChannelInfo(
        channel_id=cid,
        name=str(raw_ch.get("name") or cid).strip(),
        country=country,
        alt_names=alts,
        network=net,
        categories=cats,
        languages=flangs,
      )

    active_info_by_id: dict[str, IptvOrgChannelInfo] = {}
    for cid, entry in raw_by_id.items():
      if bool(entry.get("is_nsfw")) or entry.get("closed"):
        continue
      active_info_by_id[cid] = _build_active_info(entry)

    idx._station_count = len(active_info_by_id)

    def _extract_forms(
      raw_ch: Mapping[str, Any], cid: str
    ) -> list[tuple[str, tuple[str, ...]]]:
      raw_labels: list[str] = [str(raw_ch.get("name") or "")]
      raw_alts = raw_ch.get("alt_names")
      if isinstance(raw_alts, list):
        for alt in raw_alts:
          raw_labels.append(str(alt))
      raw_labels.extend(feed_callsigns_by_id.get(cid, ()))
      raw_labels.append(cid.split(".", 1)[0])

      seen_compact: set[str] = set()
      forms: list[tuple[str, tuple[str, ...]]] = []
      for label in raw_labels:
        compact = normalize_lookup_key(label)
        toks = tokenize_station_name(label)
        if compact and toks and compact not in seen_compact:
          seen_compact.add(compact)
          forms.append((compact, toks))
      return forms

    df_counter: Counter[str] = Counter()

    def _register_entry(
      info: IptvOrgChannelInfo,
      was_replaced: bool,
      closed_id: str | None,
      forms: list[tuple[str, tuple[str, ...]]],
      index_country: str,
    ) -> None:
      rec = (info, was_replaced, closed_id)
      entry_idx = len(idx._entries)
      idx._entries.append((info, was_replaced, closed_id, tuple(forms)))
      entry_tokens: set[str] = set()
      for compact, toks in forms:
        if not was_replaced:
          idx._active_country_keys.add((index_country, compact))
        idx._by_country_key[(index_country, compact)].append(rec)
        if not was_replaced and len(compact) >= 8:
          idx._by_global_key[compact].append(rec)
        entry_tokens.update(toks)
      for tok in entry_tokens:
        if len(tok) >= 2:
          idx._token_postings[tok].append(entry_idx)
          df_counter[tok] += 1

    # 1. Index all active channels first so active records always rank ahead of redirects
    for cid, info in active_info_by_id.items():
      raw_ch = raw_by_id[cid]
      idx._by_id[cid.lower()] = (info, False, None)
      forms = _extract_forms(raw_ch, cid)
      _register_entry(info, False, None, forms, info.country)

    # 2. Process `closed` channels: follow `replaced_by` chains or mark permanently defunct
    for cid, raw_ch in raw_by_id.items():
      if bool(raw_ch.get("is_nsfw")) or not raw_ch.get("closed"):
        continue

      cur: Mapping[str, Any] | None = raw_ch
      seen: set[str] = {cid}
      resolved_target: IptvOrgChannelInfo | None = None
      for _ in range(5):
        if cur is None:
          break
        rep_raw = str(cur.get("replaced_by") or "").strip()
        if not rep_raw:
          break
        rep_id = rep_raw.split("@", 1)[0].strip()
        if not rep_id or rep_id in seen:
          break
        seen.add(rep_id)
        if rep_id in active_info_by_id:
          resolved_target = active_info_by_id[rep_id]
          break
        cur = raw_by_id.get(rep_id)

      country = str(raw_ch.get("country") or "").strip().upper()
      closed_forms = _extract_forms(raw_ch, cid)

      if resolved_target is not None:
        idx._by_id.setdefault(cid.lower(), (resolved_target, True, cid))
        _register_entry(resolved_target, True, cid, closed_forms, country)
      else:
        idx._closed_unreplaced_ids.add(cid.lower())
        for compact, _ in closed_forms:
          idx._closed_unreplaced_country_keys.add((country, compact))

    total_docs = max(len(idx._entries), 1)
    idx._token_idf = {
      tok: math.log(1.0 + total_docs / (1.0 + df))
      for tok, df in df_counter.items()
    }
    return idx

  def is_permanently_closed_alias(
    self, alias: str, *, country: str = "CA"
  ) -> bool:
    """Return True if `alias` names a shut-down unreplaced channel and no active one."""
    k = normalize_lookup_key(alias)
    if not k:
      return False
    iso2 = normalize_country_iso2(country)
    probe_countries = NEIGHBOR_BROADCAST_COUNTRIES.get(iso2, (iso2,))
    if any((c, k) in self._active_country_keys for c in probe_countries):
      return False
    return any(
      (c, k) in self._closed_unreplaced_country_keys for c in probe_countries
    )

  def has_id(self, tvg_id: str | None) -> bool:
    """Return True if `tvg_id` resolves to an active or replaced station in `iptv-org`."""
    if not tvg_id:
      return False
    return tvg_id.strip().lower() in self._by_id

  def is_permanently_closed_id(self, tvg_id: str | None) -> bool:
    """Return True if `tvg_id` is marked `closed` in `iptv-org` with no `replaced_by`."""
    if not tvg_id:
      return False
    low = tvg_id.strip().lower()
    return low in self._closed_unreplaced_ids and low not in self._by_id

  def filter_active_aliases(
    self,
    aliases: Sequence[str],
    *,
    primary_name: str,
    country: str = "CA",
  ) -> list[str]:
    """Remove permanently closed defunct station aliases while preserving `primary_name`."""
    if self.is_empty:
      return list(aliases)
    primary_norm = normalize_lookup_key(primary_name)
    filtered: list[str] = []
    for alias in aliases:
      if normalize_lookup_key(alias) == primary_norm:
        filtered.append(alias)
        continue
      if self.is_permanently_closed_alias(alias, country=country):
        continue
      filtered.append(alias)
    return filtered or [primary_name]

  def _token_weight(self, tok: str, default_idf: float) -> float:
    if tok in GENERIC_BROADCAST_TOKENS:
      return min(0.75, self._token_idf.get(tok, 0.75))
    return self._token_idf.get(tok, default_idf)

  def _text_similarity(
    self,
    q_compact: str,
    q_tokens: tuple[str, ...],
    c_compact: str,
    c_tokens: tuple[str, ...],
    *,
    meta_qualifiers: frozenset[str] = frozenset(),
  ) -> float:
    if q_compact == c_compact:
      return 1.0
    q_nums = {t for t in q_tokens if t.isdigit()}
    c_nums = {t for t in c_tokens if t.isdigit()}
    if q_nums != c_nums:
      return 0.0

    # Query language/country qualifiers satisfied by station metadata (e.g. "Francais" -> languages=["fra"])
    effective_q_tokens = tuple(
      t for t in q_tokens if (t in c_tokens or t not in meta_qualifiers)
    )
    if not effective_q_tokens:
      effective_q_tokens = q_tokens
    eff_q_compact = "".join(effective_q_tokens)
    if eff_q_compact == c_compact:
      return 0.96

    default_idf = math.log(1.0 + max(len(self._entries), 1))
    q_set = set(effective_q_tokens)
    c_set = set(c_tokens)
    if ((q_set - c_set) | (c_set - q_set)) & IPTV_ORG_SPINOFF_TOKENS:
      return 0.0
    q_weight = sum(self._token_weight(t, default_idf) for t in q_set)
    c_weight = sum(self._token_weight(t, default_idf) for t in c_set)
    overlap_weight = sum(
      self._token_weight(t, default_idf) for t in (q_set & c_set)
    )
    token_dice = (
      (2.0 * overlap_weight) / (q_weight + c_weight)
      if (q_weight + c_weight) > 0.0
      else 0.0
    )
    char_ratio = difflib.SequenceMatcher(None, eff_q_compact, c_compact).ratio()
    if (
      min(len(effective_q_tokens), len(c_tokens)) >= 2
      and (q_set <= c_set or c_set <= q_set)
      and bool((q_set & c_set) - GENERIC_BROADCAST_TOKENS)
      and max(q_weight, c_weight) > 0.0
      and (overlap_weight / max(q_weight, c_weight)) >= 0.45
    ):
      min_cov = overlap_weight / min(q_weight, c_weight)
      return 0.55 * token_dice + 0.25 * min_cov + 0.20 * char_ratio
    return 0.75 * token_dice + 0.25 * char_ratio

  def lookup(
    self,
    name: str,
    *,
    tvg_id: str | None = None,
    callsign: str | None = None,
    aliases: Sequence[str] = (),
    country: str = "CA",
  ) -> IptvOrgMatch | None:
    """Fuzzy-match a lineup slot against `iptv-org` using accent-folded IDF similarity."""
    if self.is_empty:
      return None

    if tvg_id:
      low_id = tvg_id.strip().lower()
      if low_id in self._by_id:
        hit = self._by_id[low_id]
        return IptvOrgMatch(
          channel=hit[0],
          was_replaced=hit[1],
          matched_closed_id=hit[2],
          similarity=1.0,
        )

    no_paren = PAREN_STRIP_RE.sub("", name).strip()
    brand_upper = normalize_lookup_key(no_paren).upper()
    if brand_upper in US_NETWORK_CANONICAL_IDS:
      canon_id = US_NETWORK_CANONICAL_IDS[brand_upper].lower()
      if canon_id in self._by_id:
        hit = self._by_id[canon_id]
        return IptvOrgMatch(
          channel=hit[0],
          was_replaced=hit[1],
          matched_closed_id=hit[2],
          similarity=1.0,
        )

    probe_strings: list[str] = [name, no_paren, *aliases]
    if callsign and callsign.strip():
      probe_strings.append(callsign.strip())

    parsed_probes: list[tuple[str, tuple[str, ...]]] = []
    seen_probes: set[str] = set()
    for raw_probe in probe_strings:
      comp = normalize_lookup_key(raw_probe)
      toks = tokenize_station_name(raw_probe)
      if len(comp) >= 3 and toks and comp not in seen_probes:
        seen_probes.add(comp)
        parsed_probes.append((comp, toks))

    if not parsed_probes:
      return None

    iso2 = normalize_country_iso2(country)
    pref_countries = NEIGHBOR_BROADCAST_COUNTRIES.get(iso2, (iso2, "US", "GB"))
    country_bonus_map: dict[str, float] = {}
    for rank, ctry in enumerate(pref_countries):
      if rank == 0:
        country_bonus_map[ctry] = 0.14
      elif rank == 1:
        country_bonus_map[ctry] = 0.08
      else:
        country_bonus_map[ctry] = 0.0

    # Gather candidate entry indices from highest-IDF tokens across probe strings
    cand_entry_indices: set[int] = set()
    max_posting_len = max(120, len(self._entries) // 25)
    for _comp, toks in parsed_probes:
      ranked_toks = sorted(
        (t for t in toks if t in self._token_postings),
        key=lambda t: self._token_idf.get(t, 0.0),
        reverse=True,
      )
      for tok in ranked_toks[:2]:
        postings = self._token_postings[tok]
        if len(postings) <= max_posting_len:
          cand_entry_indices.update(postings)
        elif len(ranked_toks) >= 2:
          other = self._token_postings[ranked_toks[1]]
          cand_entry_indices.update(set(postings) & set(other))
      if not ranked_toks and len(toks) == 1 and len(toks[0]) >= 5:
        needle = toks[0]
        first_ch = needle[0]
        for vocab_tok, postings in self._token_postings.items():
          if (
            vocab_tok.startswith(first_ch)
            and abs(len(vocab_tok) - len(needle)) <= 2
            and len(postings) <= max_posting_len
            and difflib.SequenceMatcher(None, needle, vocab_tok).ratio() >= 0.82
          ):
            cand_entry_indices.update(postings)

    best_score = -1.0
    best_sim = 0.0
    best_entry: tuple[IptvOrgChannelInfo, bool, str | None] | None = None

    for e_idx in cand_entry_indices:
      info, was_replaced, closed_id, forms = self._entries[e_idx]
      meta_quals: set[str] = set(
        COUNTRY_QUALIFIER_TOKENS.get(info.country, frozenset())
      )
      for lg in info.languages:
        meta_quals.update(LANG_ISO3_QUALIFIER_TOKENS.get(lg, frozenset()))
      meta_frozenset = frozenset(meta_quals)

      max_sim = 0.0
      for q_comp, q_toks in parsed_probes:
        for c_comp, c_toks in forms:
          sim = self._text_similarity(
            q_comp, q_toks, c_comp, c_toks, meta_qualifiers=meta_frozenset
          )
          if sim > max_sim:
            max_sim = sim
            if max_sim >= 1.0:
              break
        if max_sim >= 1.0:
          break

      if max_sim < 0.78:
        continue
      # Foreign non-neighbor stations require near-exact match (>= 0.92) to avoid false hits
      if info.country not in country_bonus_map and max_sim < 0.92:
        continue

      c_bonus = country_bonus_map.get(info.country, -0.05)
      cat_bonus = 0.02 if info.categories else 0.0
      rep_adj = -0.01 if was_replaced else 0.0
      composite = max_sim + c_bonus + cat_bonus + rep_adj
      if composite > best_score:
        best_score = composite
        best_sim = max_sim
        best_entry = (info, was_replaced, closed_id)

    if best_entry is None:
      return None

    return IptvOrgMatch(
      channel=best_entry[0],
      was_replaced=best_entry[1],
      matched_closed_id=best_entry[2],
      similarity=round(best_sim, 4),
    )


@dataclass(slots=True)
class IptvOrgFetchResult:
  """Outcome of fetching or conditionally revalidating `iptv-org` datasets."""

  index: IptvOrgIndex
  updated: bool
  from_cache: bool
  channels_status: str
  feeds_status: str
  metadata: dict[str, HttpCacheEntry] = field(default_factory=dict)


class IptvOrgClient:
  """HTTP client for `iptv-org` JSON API with `ETag` / `Last-Modified` cache tracking."""

  def __init__(
    self,
    *,
    cache_dir: Path,
    base_url: str = DEFAULT_IPTV_ORG_API_URL,
    recheck_hours: float = 24.0,
    timeout_sec: float = 30.0,
    client: aiohttp.ClientSession | None = None,
    clock: Callable[[], float] = time.time,
  ) -> None:
    self.cache_dir = cache_dir
    self.store_dir = cache_dir / "iptv_org"
    self.meta_path = self.store_dir / "http_cache_meta.json"
    self.base_url = base_url.rstrip("/")
    self.recheck_hours = recheck_hours
    self.timeout_sec = timeout_sec
    self._external_client = client
    self._clock = clock

  def _load_meta(self) -> dict[str, HttpCacheEntry]:
    if not self.meta_path.is_file():
      return {}
    try:
      raw = json.loads(self.meta_path.read_text(encoding="utf-8"))
      if not isinstance(raw, Mapping):
        return {}
      out: dict[str, HttpCacheEntry] = {}
      for name, entry in raw.items():
        if isinstance(entry, Mapping):
          out[str(name)] = HttpCacheEntry.from_dict(entry)
      return out
    except (OSError, ValueError, TypeError):
      return {}

  def _save_meta(self, meta: Mapping[str, HttpCacheEntry]) -> None:
    self.store_dir.mkdir(parents=True, exist_ok=True)
    serialized = {k: v.to_dict() for k, v in sorted(meta.items())}
    tmp = self.meta_path.with_suffix(".tmp")
    tmp.write_text(
      json.dumps(serialized, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(self.meta_path)

  async def _fetch_resource(
    self,
    session: aiohttp.ClientSession,
    filename: str,
    meta: dict[str, HttpCacheEntry],
    *,
    force_refresh: bool,
    dry_run: bool,
    allow_offline_empty: bool,
  ) -> tuple[list[Any], str, bool, bool]:
    """Return `(records, status, content_updated, used_cache)` for one JSON resource."""
    url = f"{self.base_url}/{filename}"
    file_path = self.store_dir / filename
    now = self._clock()
    recheck_sec = max(0.0, self.recheck_hours) * 3600.0
    prev = meta.get(filename)
    has_local = file_path.is_file()

    if (
      has_local
      and prev is not None
      and prev.url == url
      and not force_refresh
      and recheck_sec > 0.0
      and max(0.0, now - prev.checked_at) < recheck_sec
    ):
      try:
        cached_list = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(cached_list, list):
          return cached_list, "fresh_local", False, True
      except (OSError, ValueError):
        has_local = False

    req_headers: dict[str, str] = {}
    if has_local and prev is not None and prev.url == url:
      if prev.etag:
        req_headers["If-None-Match"] = prev.etag
      if prev.last_modified:
        req_headers["If-Modified-Since"] = prev.last_modified

    try:
      async with session.get(url, headers=req_headers) as resp:
        if resp.status == 304 and has_local:
          cached_list = json.loads(file_path.read_text(encoding="utf-8"))
          if not isinstance(cached_list, list):
            raise ValueError(f"Cached {filename} is not a JSON array")
          refreshed_etag = resp.headers.get("ETag") or (
            prev.etag if prev else None
          )
          refreshed_lm = resp.headers.get("Last-Modified") or (
            prev.last_modified if prev else None
          )
          refreshed_cc = resp.headers.get("Cache-Control") or (
            prev.cache_control if prev else None
          )
          meta[filename] = HttpCacheEntry(
            url=url,
            etag=refreshed_etag,
            last_modified=refreshed_lm,
            cache_control=refreshed_cc,
            sha256=prev.sha256 if prev else "",
            size_bytes=prev.size_bytes if prev else file_path.stat().st_size,
            checked_at=now,
            updated_at=prev.updated_at if prev else now,
          )
          return cached_list, "not_modified_304", False, True

        resp.raise_for_status()
        raw_bytes = await resp.read()
        parsed = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(parsed, list):
          raise ValueError(f"Expected JSON array from {url}")

        digest = hashlib.sha256(raw_bytes).hexdigest()
        content_changed = not has_local or prev is None or prev.sha256 != digest
        if not dry_run:
          self.store_dir.mkdir(parents=True, exist_ok=True)
          tmp_file = file_path.with_suffix(".tmp")
          tmp_file.write_bytes(raw_bytes)
          tmp_file.replace(file_path)

        meta[filename] = HttpCacheEntry(
          url=url,
          etag=resp.headers.get("ETag"),
          last_modified=resp.headers.get("Last-Modified"),
          cache_control=resp.headers.get("Cache-Control"),
          sha256=digest,
          size_bytes=len(raw_bytes),
          checked_at=now,
          updated_at=(
            now if content_changed else (prev.updated_at if prev else now)
          ),
        )
        return parsed, "fetched_200", content_changed, False
    except (
      aiohttp.ClientError,
      asyncio.TimeoutError,
      OSError,
      ValueError,
    ) as exc:
      if has_local:
        try:
          fallback_list = json.loads(file_path.read_text(encoding="utf-8"))
          if isinstance(fallback_list, list):
            return fallback_list, "stale_cache_fallback", False, True
        except (OSError, ValueError):
          pass
      if allow_offline_empty:
        return [], "offline_empty", False, True
      raise IptvOrgError(f"Failed to retrieve {url}: {exc}") from exc

  async def fetch_index(
    self,
    *,
    force_refresh: bool = False,
    dry_run: bool = False,
    allow_offline_empty: bool = True,
  ) -> IptvOrgFetchResult:
    """Fetch or conditionally revalidate `channels.json` + `feeds.json` via `ETag`."""
    meta = self._load_meta()
    before_meta_dump = {k: v.to_dict() for k, v in meta.items()}

    owns = self._external_client is None
    session = self._external_client or aiohttp.ClientSession(
      headers={
        "User-Agent": IPTV_ORG_USER_AGENT,
        "Accept": "application/json",
      },
      timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
    )
    try:
      (
        channels_data,
        ch_status,
        ch_updated,
        ch_cached,
      ) = await self._fetch_resource(
        session,
        "channels.json",
        meta,
        force_refresh=force_refresh,
        dry_run=dry_run,
        allow_offline_empty=allow_offline_empty,
      )
      feeds_data, fd_status, fd_updated, fd_cached = await self._fetch_resource(
        session,
        "feeds.json",
        meta,
        force_refresh=force_refresh,
        dry_run=dry_run,
        allow_offline_empty=allow_offline_empty,
      )
    finally:
      if owns:
        await session.close()

    after_meta_dump = {k: v.to_dict() for k, v in meta.items()}
    if not dry_run and after_meta_dump != before_meta_dump:
      self._save_meta(meta)

    index = IptvOrgIndex.from_json_payloads(channels_data, feeds_data)
    return IptvOrgFetchResult(
      index=index,
      updated=bool(ch_updated or fd_updated),
      from_cache=bool(ch_cached and fd_cached),
      channels_status=ch_status,
      feeds_status=fd_status,
      metadata=meta,
    )
