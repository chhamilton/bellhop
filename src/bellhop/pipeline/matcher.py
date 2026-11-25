"""Multi-signal channel matcher mapping Xtream streams to cable/OTA lineup
slots.
"""

from __future__ import annotations

import difflib
import math
import re
from collections import defaultdict
from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from bellhop.pipeline.iptv_org import fold_latin_accents, normalize_lookup_key
from bellhop.pipeline.matcher_vocab import (
  BENIGN_QUALIFIER_TOKENS,
  BROADCAST_WRAPPER_TOKENS,
  CA_METRO_MARKERS,
  CA_PROVINCE_MARKERS,
  CALLSIGN_ROOT_RE,
  CANADA_MARKERS,
  CANADA_PRIMARY_REGIONS,
  CARDINAL_AFTER_BRAND_RE,
  CARDINAL_WORD_MAP,
  CODEC_CLEAN_RE,
  DECADE_OR_NUM_RE,
  DELIMITER_SPLIT_RE,
  E_EXCLAMATION_RE,
  FOREIGN_PREFIX_MARKERS,
  GLUED_HD_SUFFIX_RE,
  INFIX_PLUS_RE,
  LEADING_BRACKET_RE,
  LEADING_COUNTRY_CODES,
  LETTER_DIGIT_RE,
  MAJOR_SPINOFF_UMBRELLA_BRANDS,
  MONTREAL_MARKERS,
  NON_LINEAR_RE,
  OTA_CALLSIGN_RE,
  PAREN_CITY_RE,
  QUALITY_AND_TECH_TOKENS,
  QUEBEC_MARKERS,
  SEPARATOR_RE,
  SPINOFF_MODIFIER_TOKENS,
  TARGET_REGION_QUALIFIER_TOKENS,
  TIMESHIFT_RE,
  UK_MARKERS,
  US_MARKERS,
  US_METRO_MARKERS,
  US_OTA_CALLSIGN_RE,
  US_PRIMARY_REGIONS,
  US_STATE_MARKERS,
)
from bellhop.pipeline.models import (
  LineupChannel,
  MappedChannel,
  MatchCandidate,
  XtreamStream,
)


def strip_accents(text: str) -> str:
  """Convert accented Latin characters and ligatures (Montréal, Cœur, Écran) to
  ASCII.
  """
  return fold_latin_accents(text)


def _normalize_symbols(text: str) -> str:
  s = strip_accents(text)
  s = re.sub(r"\bat&t\b", "att", s, flags=re.IGNORECASE)
  s = E_EXCLAMATION_RE.sub(" eexclamation ", s)
  # Spaced infix " + " between words acts as conjunction "&" ("Crime +
  # Investigation")
  s = INFIX_PLUS_RE.sub(" & ", s)
  # Attached/trailing "+" stays "+" unless it is a timeshift like +1 / +2
  s = re.sub(r"\+(?![123]\b)", " + ", s)
  s = s.replace("&", " & ")
  s = s.lower()
  s = re.sub(r"\band\b", "&", s)
  s = re.sub(r"\bplus\b", "+", s)
  s = re.sub(r"\b2000s\b", "00s", s)
  s = re.sub(r"\b19([6789]0s)\b", r"\1", s)
  s = CARDINAL_AFTER_BRAND_RE.sub(
    lambda m: f" {CARDINAL_WORD_MAP[m.group(1).lower()]}", s
  )
  return s


def _tokenize_raw(text: str, *, split_alnum: bool = True) -> list[str]:
  s = _normalize_symbols(text)
  raw_tokens = re.findall(r"[a-z0-9]+|[+&]", s)
  if not split_alnum:
    return raw_tokens
  out: list[str] = []
  for tok in raw_tokens:
    if tok in {"+", "&"}:
      out.append(tok)
      continue
    m = LETTER_DIGIT_RE.match(tok)
    if m:
      out.append(m.group(1))
      out.append(m.group(2))
      continue
    m_hd = GLUED_HD_SUFFIX_RE.match(tok)
    if m_hd:
      out.append(m_hd.group(1))
      out.append(m_hd.group(2))
    else:
      out.append(tok)
  return out


def _is_ordered_subsequence(
  sub: tuple[str, ...], full: tuple[str, ...]
) -> bool:
  """Return True if all tokens of `sub` appear in order inside `full`."""
  it = iter(full)
  return all(tok in it for tok in sub)


def _strip_quality_tokens(tokens: Iterable[str]) -> list[str]:
  return [t for t in tokens if t not in QUALITY_AND_TECH_TOKENS]


def _score_quality_tags(
  raw_name: str, category_name: str = ""
) -> tuple[float, str, list[str]]:
  # Normalize Unicode superscripts (e.g. ᴴᴰ -> HD, ᴿᴬᵂ -> RAW, ⁶⁰ᶠᵖˢ -> 60FPS,
  # ⁽ᴮᴷ⁾ -> (BK))
  upper = strip_accents(raw_name).upper()
  cat_upper = strip_accents(category_name).upper()
  score = 0.0
  tier = "unknown"
  notes: list[str] = []

  if re.search(r"\b(?:4K|UHD|2160P)\b", upper):
    score += 15.0
    tier = "4k"
    notes.append("quality:4K")
  elif re.search(r"\b(?:FHD|1080P|1080I)\b", upper):
    score += 14.0
    tier = "fhd"
    notes.append("quality:1080p/FHD")
  elif re.search(r"\b(?:HD|720P)\b", upper):
    score += 10.0
    tier = "hd"
    notes.append("quality:HD")
  elif re.search(r"\b(?:SD|480P|540P|LQ)\b", upper):
    score -= 8.0
    tier = "sd"
    notes.append("quality:SD")
  elif re.search(r"\b(?:60FPS|59FPS|RAW)\b", upper):
    score += 11.0
    tier = "fhd"
    notes.append("quality:RAW")
  elif re.search(r"\b(?:4K|UHD|FHD|HD|RAW|60FPS)\b", cat_upper):
    # Category-level HD/RAW indicator when stream name omits explicit SD/HD tag
    score += 5.5
    tier = "hd"
    notes.append("quality:cat_HD")

  if (
    re.search(r"\b(?:60FPS|59FPS|RAW)\b", upper) and "quality:RAW" not in notes
  ):
    score += 4.0
    notes.append("60fps/raw")
  elif re.search(r"\b(?:60FPS|59FPS)\b", cat_upper):
    score += 2.0
    notes.append("cat:60fps")

  if re.search(r"\b(?:HEVC|H\.?265)\b", upper):
    score += 2.0
    notes.append("codec:HEVC")
  if re.search(r"(?:\b(?:BACKUP|BKP|BK|LOW)\b|\(BK\))", upper):
    score -= 10.0
    notes.append("backup_feed")

  return score, tier, notes


def _is_plausible_native_epg(
  epg_id_lower: str,
  *,
  tvg_id_lower: str | None,
  callsign_tok: str | None,
  alias_specs: list[tuple[int, str, tuple[str, ...]]],
) -> bool:
  """Verify a provider's epg_channel_id plausibly belongs to this channel
  (filters panel typos).
  """
  if not epg_id_lower:
    return False
  if tvg_id_lower and epg_id_lower == tvg_id_lower:
    return True
  epg_compact = re.sub(
    r"[^a-z0-9]+", "", strip_accents(epg_id_lower.rsplit(".", 1)[0])
  )
  if not epg_compact:
    return False
  if callsign_tok and len(callsign_tok) >= 3 and callsign_tok in epg_compact:
    return True
  if tvg_id_lower:
    tvg_compact = re.sub(
      r"[^a-z0-9]+", "", strip_accents(tvg_id_lower.rsplit(".", 1)[0])
    )
    if len(tvg_compact) >= 3 and (
      tvg_compact in epg_compact or epg_compact in tvg_compact
    ):
      return True

  stopwords = {
    "the",
    "and",
    "plus",
    "tele",
    "ici",
    "radio",
    "channel",
    "network",
    "canada",
    "montreal",
    "quebec",
    "east",
    "francais",
    "french",
    "english",
  }
  distinctive_found_in_aliases = False
  for _, _, a_toks in alias_specs:
    joined_alias = "".join(a_toks)
    if len(joined_alias) >= 2 and (
      joined_alias in epg_compact or epg_compact in joined_alias
    ):
      return True
    for tok in a_toks:
      if tok in stopwords:
        continue
      distinctive_found_in_aliases = True
      if len(tok) >= 3 and tok in epg_compact:
        return True

  # If all tokens of every alias are common network words (e.g. "ICI Montreal" /
  # "Radio-Canada"),
  # require all tokens of at least one multi-word alias to appear in
  # epg_compact.
  if not distinctive_found_in_aliases:
    for _, _, a_toks in alias_specs:
      if len(a_toks) >= 2 and all(tok in epg_compact for tok in a_toks):
        return True
  return False


@dataclass(slots=True)
class _PreparedStream:
  stream: XtreamStream
  core_tokens: tuple[str, ...]
  trimmed_core_tokens: tuple[str, ...]
  full_tokens: frozenset[str]
  full_str: str
  regions: frozenset[str]
  prefix_regions: frozenset[str]
  quality_score: float
  quality_tier: str
  quality_notes: tuple[str, ...]
  is_nonlinear: bool
  is_timeshift: bool
  epg_id_lower: str


def _prepare_stream(stream: XtreamStream) -> _PreparedStream | None:
  raw_name = stream.name.strip()
  if not raw_name or SEPARATOR_RE.match(raw_name):
    return None

  is_nonlinear = bool(
    NON_LINEAR_RE.search(raw_name) or NON_LINEAR_RE.search(stream.category_name)
  )
  is_timeshift = bool(TIMESHIFT_RE.search(raw_name))

  prefix_chunks: list[str] = []
  working = raw_name

  # Extract leading [QC] / (CA-FR) bracket prefixes
  m_bracket = LEADING_BRACKET_RE.match(working)
  if m_bracket:
    prefix_chunks.append(m_bracket.group(0))
    working = working[m_bracket.end() :].strip()

  # Split on explicit prefix delimiters like "CA | RDS HD" or "VIP | QC | TVA"
  parts = [p.strip() for p in DELIMITER_SPLIT_RE.split(working) if p.strip()]
  if len(parts) >= 2:
    # Treat short leading segments (e.g. "CA", "QC", "VIP", "CA-FR", "CANADA")
    # as prefix metadata
    idx = 0
    while idx < len(parts) - 1:
      seg_tokens = _tokenize_raw(parts[idx], split_alnum=False)
      if len(seg_tokens) <= 3 and any(
        t
        in (
          LEADING_COUNTRY_CODES
          | MONTREAL_MARKERS
          | QUEBEC_MARKERS
          | CANADA_MARKERS
          | US_MARKERS
          | UK_MARKERS
          | FOREIGN_PREFIX_MARKERS
          | {
            "vip",
            "en",
            "english",
            "french",
            "francais",
            "france",
            "euro",
            "latam",
          }
        )
        for t in seg_tokens
      ):
        prefix_chunks.append(parts[idx])
        idx += 1
      else:
        break
    core_source = " ".join(parts[idx:])
  else:
    core_source = working

  # Remove parenthetical codec/resolution suffixes while preserving
  # city/callsign hints in full_tokens
  core_cleaned = CODEC_CLEAN_RE.sub(" ", core_source)
  raw_core_tokens = _strip_quality_tokens(
    _tokenize_raw(core_cleaned, split_alnum=True)
  )
  if not raw_core_tokens:
    raw_core_tokens = _tokenize_raw(core_source, split_alnum=True)
  if not raw_core_tokens:
    return None

  core_tokens = tuple(raw_core_tokens)
  if len(core_tokens) >= 2 and core_tokens[0] in LEADING_COUNTRY_CODES:
    trimmed_core_tokens = core_tokens[1:]
  elif len(core_tokens) >= 3 and f"{core_tokens[0]}{core_tokens[1]}" in {
    "cafr",
    "frca",
    "enca",
    "caen",
  }:
    trimmed_core_tokens = core_tokens[2:]
  else:
    trimmed_core_tokens = core_tokens

  # Build full token set (without codec noise like h264/mpeg2 so "2" won't
  # false-trigger exclusions)
  combined_meta = f"{' '.join(prefix_chunks)} {stream.category_name} {CODEC_CLEAN_RE.sub(' ', raw_name)}"
  full_token_list = _strip_quality_tokens(
    _tokenize_raw(combined_meta, split_alnum=True)
  )
  full_tokens = frozenset(full_token_list)
  full_str = f" {' '.join(full_token_list)} "

  # Region signal detection
  prefix_meta_str = f"{' '.join(prefix_chunks)} {stream.category_name}"
  prefix_toks = frozenset(_tokenize_raw(prefix_meta_str, split_alnum=False))
  epg_lower = stream.epg_channel_id.strip().lower()

  regions: set[str] = set()
  prefix_regions: set[str] = set()

  if full_tokens & MONTREAL_MARKERS:
    regions.add("montreal")
    regions.add("qc")
    regions.add("ca")
  for metro, markers in CA_METRO_MARKERS.items():
    if metro != "montreal" and (full_tokens & markers):
      regions.add(metro)
      regions.add("ca")
  for prov, markers in CA_PROVINCE_MARKERS.items():
    if prov != "qc" and (full_tokens & markers):
      regions.add(prov)
      regions.add("ca")
  for metro, markers in US_METRO_MARKERS.items():
    if (
      (full_tokens & markers)
      or (metro == "newyork" and "new york" in full_str)
      or (metro == "losangeles" and "los angeles" in full_str)
    ):
      regions.add(metro)
      regions.add("us")
  for st_code, markers in US_STATE_MARKERS.items():
    if full_tokens & markers:
      regions.add(st_code)
      regions.add("us")
  if (
    (full_tokens & QUEBEC_MARKERS)
    or (
      "ca" in prefix_toks
      and (
        "fr" in prefix_toks
        or "french" in prefix_toks
        or "francais" in prefix_toks
      )
    )
    or (
      "canada" in prefix_toks
      and (
        "fr" in prefix_toks
        or "french" in prefix_toks
        or "francais" in prefix_toks
      )
    )
  ):
    regions.add("qc")
    regions.add("ca")
    prefix_regions.add("qc")
  if (full_tokens & CANADA_MARKERS) or epg_lower.endswith(".ca"):
    regions.add("ca")
  if prefix_toks & CANADA_MARKERS:
    prefix_regions.add("ca")
  if (full_tokens & US_MARKERS) or epg_lower.endswith(".us"):
    regions.add("us")
  if prefix_toks & US_MARKERS:
    prefix_regions.add("us")
  if (prefix_toks & {"uk", "gb", "british"}) or epg_lower.endswith(".uk"):
    regions.add("uk")
    prefix_regions.add("uk")
  if (
    "france" in prefix_toks
    or (
      "fr" in prefix_toks
      and not (prefix_toks & {"ca", "can", "canada", "qc", "quebec"})
    )
  ) or epg_lower.endswith(".fr"):
    regions.add("fr")
    prefix_regions.add("fr")
  if prefix_toks & FOREIGN_PREFIX_MARKERS:
    regions.add("foreign")
    prefix_regions.add("foreign")

  q_score, q_tier, q_notes = _score_quality_tags(raw_name, stream.category_name)

  return _PreparedStream(
    stream=stream,
    core_tokens=core_tokens,
    trimmed_core_tokens=trimmed_core_tokens,
    full_tokens=full_tokens,
    full_str=full_str,
    regions=frozenset(regions),
    prefix_regions=frozenset(prefix_regions),
    quality_score=q_score,
    quality_tier=q_tier,
    quality_notes=tuple(q_notes),
    is_nonlinear=is_nonlinear,
    is_timeshift=is_timeshift,
    epg_id_lower=epg_lower,
  )


def _contains_token_sequence(
  haystack: tuple[str, ...], needle: tuple[str, ...]
) -> bool:
  n = len(needle)
  if n == 0 or n > len(haystack):
    return False
  for idx in range(len(haystack) - n + 1):
    if haystack[idx : idx + n] == needle:
      return True
  return False


def _weighted_levenshtein(
  s_chars: str,
  s_weights: list[float],
  t_chars: str,
  t_weights: list[float],
) -> float:
  """Compute weighted Levenshtein distance with cheap separator & `+`↔`&`
  costs.
  """
  m = len(s_chars)
  n = len(t_chars)
  prev = [0.0] * (n + 1)
  for j in range(1, n + 1):
    prev[j] = prev[j - 1] + t_weights[j - 1]

  for i in range(1, m + 1):
    sc = s_chars[i - 1]
    sw = s_weights[i - 1]
    curr = [prev[0] + sw] + [0.0] * n
    for j in range(1, n + 1):
      tc = t_chars[j - 1]
      tw = t_weights[j - 1]
      if sc == tc:
        sub_cost = 0.0
      elif (sc == "+" and tc == "&") or (sc == "&" and tc == "+"):
        sub_cost = 0.12
      else:
        sub_cost = 0.88 * (sw + tw)
      del_cost = prev[j] + sw
      ins_cost = curr[j - 1] + tw
      rep_cost = prev[j - 1] + sub_cost
      best = del_cost if del_cost < ins_cost else ins_cost
      curr[j] = rep_cost if rep_cost < best else best
    prev = curr
  return prev[n]


def _station_group_key(ch: LineupChannel) -> str:
  """Group identical stations placed at multiple dial slots (scoped by
  language).
  """
  if ch.gracenote_station_id and ch.gracenote_station_id.strip():
    return f"gn:{ch.language}:{ch.gracenote_station_id.strip()}"
  cs = (ch.callsign or "").strip().upper()
  nm = normalize_lookup_key(ch.name)
  if cs:
    return f"cs:{ch.language}:{cs}:{nm}"
  return f"nm:{ch.language}:{nm}"


def _is_high_confidence_claim(cand: MatchCandidate, ch: LineupChannel) -> bool:
  """Return True when `cand` matches `ch`'s primary brand strongly enough to
  claim in Pass 1.
  """
  if cand.score >= 1000.0:
    return True
  if cand.score < 76.0:
    return False
  if any(
    r in cand.reasons
    for r in (
      "penalty:foreign_region",
      "penalty:uk_feed",
      "penalty:france_feed",
    )
  ):
    return False
  primary_toks = (
    set(_strip_quality_tokens(_tokenize_raw(ch.name)))
    - BENIGN_QUALIFIER_TOKENS
    - {"+", "&"}
  )
  cand_toks = set(_strip_quality_tokens(_tokenize_raw(cand.stream.name)))
  cand_joined = "".join(t for t in cand_toks if t not in {"+", "&"})
  if primary_toks and not (
    (primary_toks & cand_toks)
    or any(len(t) >= 4 and t in cand_joined for t in primary_toks)
  ):
    return False
  for r in cand.reasons:
    if r.startswith(("exact_alias:", "compound_alias:")):
      return True
    if r.startswith("edit_alias:") and "(" in r:
      try:
        sim_val = float(r.rsplit("(", 1)[1].rstrip(")"))
        if sim_val >= 0.90:
          return True
      except ValueError:
        continue
  return False


class ChannelMatcher:
  """Indexes an Xtream catalog and ranks candidate streams for each cable lineup
  channel.
  """

  def __init__(
    self,
    streams: Iterable[XtreamStream],
    *,
    pinned_streams: dict[int, int] | None = None,
    min_score: float = 45.0,
  ) -> None:
    self.pinned_streams = pinned_streams or {}
    self.min_score = min_score
    self._streams_by_id: dict[int, XtreamStream] = {}
    self._prepared: list[_PreparedStream] = []
    self._token_index: dict[str, set[int]] = defaultdict(set)
    self._compound_index: dict[str, set[int]] = defaultdict(set)
    self._epg_index: dict[str, list[int]] = defaultdict(list)

    for stream in streams:
      self._streams_by_id[stream.stream_id] = stream
      prep = _prepare_stream(stream)
      if prep is None:
        continue
      idx = len(self._prepared)
      self._prepared.append(prep)
      for tok in set(prep.core_tokens) | set(prep.trimmed_core_tokens):
        self._token_index[tok].add(idx)
      for core in (prep.core_tokens, prep.trimmed_core_tokens):
        joined = "".join(t for t in core if t not in {"+", "&"})
        if len(joined) >= 4:
          self._compound_index[joined].add(idx)
      if prep.epg_id_lower:
        self._epg_index[prep.epg_id_lower].append(idx)

    total_docs = max(len(self._prepared), 1)
    self._default_idf = math.log(1.0 + total_docs)
    self._token_idf: dict[str, float] = {
      tok: math.log(1.0 + total_docs / (1.0 + len(idxs)))
      for tok, idxs in self._token_index.items()
    }

  def match_lineup(
    self,
    channels: Iterable[LineupChannel],
    *,
    max_candidates: int = 10,
    invalid_stream_ids_by_chno: dict[int, set[int]] | None = None,
  ) -> list[MappedChannel]:
    """Multipass lineup matcher: high-confidence stations claim streams before
    fuzzy pass.
    """
    channel_list = list(channels)
    pool_limit = max(max_candidates * 2, 20)
    raw_by_chno: dict[int, list[MatchCandidate]] = {
      ch.chno: self.find_candidates(ch, limit=pool_limit) for ch in channel_list
    }

    # Pass 1: Aggregate best high-confidence claim scores per (stream_id,
    # station_group)
    group_by_chno: dict[int, str] = {
      ch.chno: _station_group_key(ch) for ch in channel_list
    }
    city_by_group: dict[str, str] = {}
    for ch in channel_list:
      gk = group_by_chno[ch.chno]
      if gk not in city_by_group:
        parens = PAREN_CITY_RE.findall(ch.name)
        city_by_group[gk] = normalize_lookup_key(parens[-1]) if parens else ""

    stream_claims: dict[int, list[tuple[float, int, str]]] = defaultdict(list)
    for ch in channel_list:
      gk = group_by_chno[ch.chno]
      bad_ids = (
        invalid_stream_ids_by_chno.get(ch.chno, set())
        if invalid_stream_ids_by_chno
        else set()
      )
      cands = raw_by_chno[ch.chno]
      if not cands:
        continue
      viable_cands = [c for c in cands if c.stream.stream_id not in bad_ids]
      if not viable_cands:
        continue
      top_cands = [c for c in viable_cands if _is_high_confidence_claim(c, ch)]
      for c in top_cands:
        stream_claims[c.stream.stream_id].append((c.score, -ch.chno, gk))

    exclusive_owner: dict[int, str] = {}
    best_claim_score: dict[int, float] = {}
    for sid, claims in stream_claims.items():
      best_by_group: dict[str, tuple[float, int]] = {}
      for sc, neg_chno, gk in claims:
        prev = best_by_group.get(gk)
        if prev is None or (sc, neg_chno) > prev:
          best_by_group[gk] = (sc, neg_chno)
      ranked_groups = sorted(
        best_by_group.items(),
        key=lambda kv: (kv[1][0], kv[1][1]),
        reverse=True,
      )
      win_gk, (win_score, _) = ranked_groups[0]
      best_claim_score[sid] = win_score
      if len(ranked_groups) == 1:
        exclusive_owner[sid] = win_gk
        continue
      runner_gk, (runner_score, _) = ranked_groups[1]
      win_city = city_by_group.get(win_gk, "")
      run_city = city_by_group.get(runner_gk, "")
      distinct_cities = bool(win_city and run_city and win_city != run_city)
      if win_score >= runner_score + 6.0 or distinct_cities:
        exclusive_owner[sid] = win_gk

    # Pass 2: Filter each channel's candidate list against streams claimed by
    # stronger stations
    out: list[MappedChannel] = []
    for ch in channel_list:
      gk = group_by_chno[ch.chno]
      filtered = [
        c
        for c in raw_by_chno[ch.chno]
        if exclusive_owner.get(c.stream.stream_id) in (None, gk)
        and (
          c.stream.stream_id not in best_claim_score
          or c.score >= best_claim_score[c.stream.stream_id] - 5.5
        )
      ]
      out.append(
        MappedChannel(channel=ch, candidates=filtered[:max_candidates])
      )
    return out

  def find_candidates(
    self, channel: LineupChannel, *, limit: int = 10
  ) -> list[MatchCandidate]:
    """Score and rank Xtream streams for a single Bell Fibe LineupChannel."""
    results: list[MatchCandidate] = []
    seen_stream_ids: set[int] = set()

    # 1. Explicit pinned stream override (by HD chno or SD chno)
    pinned_id = self.pinned_streams.get(channel.chno)
    if pinned_id is None and channel.sd_chno is not None:
      pinned_id = self.pinned_streams.get(channel.sd_chno)
    if pinned_id is not None and pinned_id in self._streams_by_id:
      p_stream = self._streams_by_id[pinned_id]
      _, p_tier, _ = _score_quality_tags(p_stream.name, p_stream.category_name)
      results.append(
        MatchCandidate(
          stream=p_stream,
          score=1000.0,
          reasons=["pinned_stream_id"],
          quality_tier=p_tier,
          has_native_epg=bool(p_stream.epg_channel_id.strip()),
        )
      )
      seen_stream_ids.add(pinned_id)

    # Precompute normalized aliases, callsign, and exclusions
    alias_specs: list[tuple[int, str, tuple[str, ...]]] = []
    for idx, alias in enumerate(channel.aliases or [channel.name]):
      toks = _strip_quality_tokens(_tokenize_raw(alias, split_alnum=True))
      if not toks:
        toks = _tokenize_raw(alias, split_alnum=True)
      if toks:
        alias_specs.append((idx, alias, tuple(toks)))

    callsign_tok = channel.callsign.lower() if channel.callsign else None
    tvg_id_lower = channel.tvg_id.lower() if channel.tvg_id else None

    exclude_phrases: list[str] = []
    for ex in channel.exclude:
      ex_toks = _tokenize_raw(ex, split_alnum=True)
      if ex_toks:
        exclude_phrases.append(f" {' '.join(ex_toks)} ")

    # 2. Gather candidate indices via inverted index (avoids scanning 50k+
    #    unrelated streams)
    candidate_indices: set[int] = set()
    if tvg_id_lower and tvg_id_lower in self._epg_index:
      candidate_indices.update(self._epg_index[tvg_id_lower])
    if callsign_tok:
      cs_root = CALLSIGN_ROOT_RE.sub("", callsign_tok).strip("-")
      for cs_variant in {callsign_tok, cs_root}:
        if not cs_variant:
          continue
        if cs_variant in self._token_index:
          candidate_indices.update(self._token_index[cs_variant])
        for sfx in (".ca", ".us"):
          cs_epg = f"{cs_variant}{sfx}"
          if cs_epg in self._epg_index:
            candidate_indices.update(self._epg_index[cs_epg])
      # US 4-letter OTA callsign on-air branding tail (e.g. WPIX -> "PIX" in "CW
      # PIX 11")
      if US_OTA_CALLSIGN_RE.match(cs_root):
        cs_tail = cs_root[1:]
        if (
          cs_tail in self._token_index
          and len(self._token_index[cs_tail]) <= 220
        ):
          candidate_indices.update(self._token_index[cs_tail])

    selective_cap = max(220, len(self._prepared) // 25)
    for _, _, a_toks in alias_specs:
      # Spaceless compound lookup (e.g. "Deja View" <-> "DEJAVIEW", "CTV Sci-Fi"
      # <-> "CTVSCIFI")
      compound_key = "".join(t for t in a_toks if t not in {"+", "&"})
      if len(compound_key) >= 4 and compound_key in self._compound_index:
        candidate_indices.update(self._compound_index[compound_key])
      if len(a_toks) >= 2 and a_toks[0] in {"the", "le", "la", "les"}:
        no_art_key = "".join(t for t in a_toks[1:] if t not in {"+", "&"})
        if len(no_art_key) >= 4 and no_art_key in self._compound_index:
          candidate_indices.update(self._compound_index[no_art_key])

      postings = [
        self._token_index[t] for t in a_toks if t in self._token_index
      ]
      if postings and len(postings) == len(a_toks):
        intersected = set(postings[0])
        for p in postings[1:]:
          intersected &= p
        candidate_indices.update(intersected)

      # Single-char brand lookup (e.g. "Z" -> "CANAL Z" / "Z TELE")
      if (
        len(a_toks) == 1
        and len(a_toks[0]) == 1
        and a_toks[0] in self._token_index
        and len(self._token_index[a_toks[0]]) <= selective_cap
      ):
        candidate_indices.update(self._token_index[a_toks[0]])

      # Fuzzy anchor retrieval via highest-IDF tokens (catches omitted/added
      # generic words)
      ranked_anchors = sorted(
        (
          t
          for t in a_toks
          if t in self._token_index
          and len(t) >= 2
          and not t.isdigit()
          and t not in {"+", "&"}
        ),
        key=lambda t: self._token_idf.get(t, 0.0),
        reverse=True,
      )
      if ranked_anchors:
        added_selective = False
        for anchor_tok in ranked_anchors[:3]:
          postings_set = self._token_index[anchor_tok]
          if len(postings_set) <= selective_cap:
            candidate_indices.update(postings_set)
            added_selective = True
        if not added_selective and len(ranked_anchors) >= 2:
          candidate_indices.update(
            self._token_index[ranked_anchors[0]]
            & self._token_index[ranked_anchors[1]]
          )
      elif len(a_toks) == 1 and len(a_toks[0]) >= 5:
        # Typo-tolerant lookup for single-token brands (e.g. "scifi" <-> "sifi")
        needle = a_toks[0]
        first_ch = needle[0]
        for vocab_tok, idxs in self._token_index.items():
          if (
            vocab_tok.startswith(first_ch)
            and abs(len(vocab_tok) - len(needle)) <= 2
            and len(idxs) <= selective_cap
            and difflib.SequenceMatcher(None, needle, vocab_tok).ratio() >= 0.84
          ):
            candidate_indices.update(idxs)

    target_regions = set(channel.regions)

    edit_cache: dict[tuple[tuple[str, ...], tuple[str, ...]], float] = {}

    # 3. Score each candidate stream
    for idx in candidate_indices:
      prep = self._prepared[idx]
      if prep.stream.stream_id in seen_stream_ids:
        continue
      if prep.is_nonlinear:
        continue

      # Hard exclusion check against normalized stream + category phrase
      if any(ex_phrase in prep.full_str for ex_phrase in exclude_phrases):
        continue

      score, reasons, has_native_epg = self._score_candidate(
        prep=prep,
        channel=channel,
        alias_specs=alias_specs,
        callsign_tok=callsign_tok,
        tvg_id_lower=tvg_id_lower,
        target_regions=target_regions,
        edit_cache=edit_cache,
      )
      if score >= self.min_score:
        results.append(
          MatchCandidate(
            stream=prep.stream,
            score=round(score, 2),
            reasons=reasons,
            quality_tier=prep.quality_tier,
            has_native_epg=has_native_epg,
          )
        )

    # Sort by descending score, preferring native EPG and lower stream_id on
    # exact ties
    results.sort(
      key=lambda c: (-c.score, not c.has_native_epg, c.stream.stream_id)
    )
    return results[:limit]

  def _build_weighted_edit_seq(
    self,
    toks: tuple[str, ...],
    allowed_extra: AbstractSet[str],
  ) -> tuple[str, list[float]]:
    """Build character sequence and corpus-IDF weights for weighted
    Levenshtein.
    """
    chars: list[str] = []
    weights: list[float] = []
    n_toks = len(toks)
    for idx, tok in enumerate(toks):
      if idx > 0 and tok not in {"+", "&"} and toks[idx - 1] not in {"+", "&"}:
        chars.append(" ")
        weights.append(0.08)
      if tok in {"+", "&"}:
        chars.append(tok)
        is_infix = 0 < idx < n_toks - 1
        weights.append(0.18 if is_infix else 1.50)
        continue
      k = len(tok)
      if tok in allowed_extra:
        unit = 0.12
      elif tok in {"movies", "movie", "cinema"}:
        unit = 0.38
      else:
        idf = self._token_idf.get(tok, self._default_idf)
        unit = min(1.50, max(0.80, idf / 6.0))
      total_w = (
        max(3.0, k * unit)
        if (k <= 2 and tok not in allowed_extra)
        else (k * unit)
      )
      per_ch = total_w / max(1, k)
      for ch in tok:
        chars.append(ch)
        weights.append(per_ch)
    return "".join(chars), weights

  def _is_rare_specialty_brand(
    self,
    a_toks: tuple[str, ...],
    c_toks: tuple[str, ...],
    allowed_extra: AbstractSet[str],
  ) -> bool:
    """Return True when `a_toks`/`c_toks` share an ultra-rare non-umbrella brand
    (`df <= 3`).
    """
    alias_set = set(a_toks)
    cand_set = set(c_toks)
    if (alias_set | cand_set) & MAJOR_SPINOFF_UMBRELLA_BRANDS:
      return False
    if any(
      len(t) >= 3
      and t not in allowed_extra
      and len(self._token_index.get(t, ())) <= 3
      for t in cand_set
    ):
      return True
    a_clean = "".join(t for t in a_toks if t != "&")
    c_clean = "".join(t for t in c_toks if t != "&")
    return (
      len(a_clean) >= 5
      and len(self._compound_index.get(a_clean, ())) <= 3
      and c_clean.startswith(a_clean)
    )

  def _weighted_edit_similarity(
    self,
    a_toks: tuple[str, ...],
    c_toks: tuple[str, ...],
    *,
    allowed_extra: AbstractSet[str],
    effective_spinoff_tokens: AbstractSet[str],
  ) -> float:
    """Compute IDF-weighted edit similarity in `[0, 1]` with numeric & spin-off
    safety.
    """
    if not a_toks or not c_toks:
      return 0.0
    a_nums = {t for t in a_toks if DECADE_OR_NUM_RE.match(t)}
    c_nums = {t for t in c_toks if DECADE_OR_NUM_RE.match(t)}
    if a_nums != c_nums:
      return 0.0

    a_clean = "".join(t for t in a_toks if t != "&")
    c_clean = "".join(t for t in c_toks if t != "&")
    if a_clean == c_clean and len(a_clean) >= 3:
      return 0.99

    alias_set = set(a_toks)
    cand_set = set(c_toks)
    added_spinoffs = (cand_set - alias_set) & effective_spinoff_tokens
    if added_spinoffs:
      # Allow "international" or "news" feed suffixes ONLY on ultra-rare
      # specialty brands
      # (df <= 3 in catalog, e.g. "Aaj Tak" -> "AAJTAK INTERNATIONAL", "NTD" ->
      # "NTD NEWS")
      if not (
        added_spinoffs <= {"international", "news"}
        and self._is_rare_specialty_brand(a_toks, c_toks, allowed_extra)
      ):
        return 0.0
      allowed_extra = set(allowed_extra) | added_spinoffs

    dropped_spinoffs = (alias_set - cand_set) & SPINOFF_MODIFIER_TOKENS
    if dropped_spinoffs:
      # Only allow omitting a redundant genre suffix ("Hollywood Suite 70s+
      # Movies" ->
      # "Hollywood Suite 70s+") when >= 2 distinctive brand tokens remain!
      soft_genre_spinoffs = {"movies", "movie", "cinema", "+"}
      if not (
        dropped_spinoffs <= soft_genre_spinoffs
        and len((cand_set - allowed_extra) - {"+", "&"}) >= 2
      ):
        return 0.0

    # 1-char brands ("Z", "W") must match on word boundaries, never inside
    # unrelated words
    if len(a_toks) == 1 and len(a_toks[0]) <= 2 and a_toks[0] not in cand_set:
      return 0.0

    # Leading brand anchor safety: prevent matching solely on trailing
    # boilerplate
    # ("MBC America" != "BBC America", "RAI International" != "CNN
    # International",
    # "ABP News" != "ABC News", "CCTV Documentary" != "Documentary")
    a_brand = [
      t for t in a_toks if t not in allowed_extra and t not in {"+", "&"}
    ]
    c_brand = [
      t for t in c_toks if t not in allowed_extra and t not in {"+", "&"}
    ]
    if a_brand and c_brand:
      lead_a = a_brand[0]
      lead_c = c_brand[0]
      if lead_a != lead_c and not (
        c_clean.startswith(lead_a) or a_clean.startswith(lead_c)
      ):
        if (
          lead_a in {"atn", "stc"}
          and len(a_brand) >= 2
          and a_brand[1:] == c_brand
          and len("".join(c_brand)) >= 4
        ):
          allowed_extra = set(allowed_extra) | {lead_a}
        elif (
          min(len(lead_a), len(lead_c)) >= 5
          and difflib.SequenceMatcher(None, lead_a, lead_c).ratio() >= 0.86
        ):
          pass
        else:
          return 0.0
      # Short distinctive brand words (<= 4 chars, e.g. "Pop", "Hits") cannot be
      # silently dropped
      for short_tok in a_brand:
        if (
          2 <= len(short_tok) <= 4
          and short_tok not in {"atn", "stc"}
          and short_tok not in cand_set
          and short_tok not in c_clean
        ):
          return 0.0

    shared_brand = (alias_set & cand_set) - allowed_extra - {"+", "&"}
    compound_overlap = not shared_brand and any(
      len(t) >= 4 and t not in allowed_extra and (t in c_clean or t in a_clean)
      for t in (alias_set | cand_set)
    )
    single_word_pair = (
      len(a_toks) == 1
      and len(c_toks) == 1
      and min(len(a_toks[0]), len(c_toks[0])) >= 5
    )
    if not (shared_brand or compound_overlap or single_word_pair):
      return 0.0

    s_chars, s_weights = self._build_weighted_edit_seq(a_toks, allowed_extra)
    t_chars, t_weights = self._build_weighted_edit_seq(c_toks, allowed_extra)
    sum_s = sum(s_weights)
    sum_t = sum(t_weights)
    denom = sum_s + sum_t
    if denom <= 0.0 or abs(sum_s - sum_t) > 0.22 * denom:
      return 0.0
    dist = _weighted_levenshtein(s_chars, s_weights, t_chars, t_weights)
    sim = max(0.0, 1.0 - (dist / denom))
    if single_word_pair and not shared_brand and sim < 0.86:
      return 0.0
    return sim

  @staticmethod
  def _build_identity_context(
    *,
    prep: _PreparedStream,
    channel: LineupChannel,
    callsign_tok: str | None,
    target_regions: set[str],
  ) -> tuple[
    str | None,
    str,
    set[str],
    frozenset[str],
    bool,
    bool,
    tuple[tuple[str, ...], ...],
  ]:
    """Derive callsign, qualifier, spinoff, and core-token context for Signal
    A–C.
    """
    cs_root = (
      CALLSIGN_ROOT_RE.sub("", callsign_tok).strip("-")
      if callsign_tok
      else None
    )
    epg_stem = (
      re.sub(
        r"[^a-z0-9]+",
        "",
        strip_accents(prep.epg_id_lower.rsplit(".", 1)[0]),
      )
      if prep.epg_id_lower
      else ""
    )
    allowed_extra = set(BENIGN_QUALIFIER_TOKENS)
    for reg_key in target_regions:
      allowed_extra.update(TARGET_REGION_QUALIFIER_TOKENS.get(reg_key, ()))
    ch_name_toks = _strip_quality_tokens(
      _tokenize_raw(channel.name, split_alnum=True)
    )
    has_station_brand_in_stream = any(
      len(t) >= 2 and t not in allowed_extra and t in prep.full_tokens
      for t in ch_name_toks
    )
    has_epg_callsign = bool(
      cs_root and len(cs_root) >= 3 and epg_stem in {callsign_tok, cs_root}
    )
    raw_cs_token_hit = bool(
      callsign_tok
      and (
        callsign_tok in prep.full_tokens
        or (cs_root and len(cs_root) >= 3 and cs_root in prep.full_tokens)
      )
    )
    is_sole_callsign_core = bool(
      cs_root
      and (
        prep.core_tokens == (cs_root,)
        or prep.trimmed_core_tokens == (cs_root,)
        or (callsign_tok and prep.core_tokens == (callsign_tok,))
      )
    )
    has_direct_callsign = has_epg_callsign or (
      raw_cs_token_hit
      and (
        not (cs_root and OTA_CALLSIGN_RE.match(cs_root))
        or is_sole_callsign_core
        or has_station_brand_in_stream
      )
    )
    cs_tail = (
      cs_root[1:] if (cs_root and US_OTA_CALLSIGN_RE.match(cs_root)) else None
    )
    has_ota_tail_brand = bool(
      cs_tail and cs_tail in prep.full_tokens and has_station_brand_in_stream
    )

    if callsign_tok and has_direct_callsign:
      allowed_extra.add(callsign_tok)
    if cs_root and has_direct_callsign:
      allowed_extra.add(cs_root)
    if has_ota_tail_brand and cs_tail:
      allowed_extra.add(cs_tail)
    effective_spinoff_tokens = SPINOFF_MODIFIER_TOKENS - allowed_extra

    is_ota_callsign_hit = bool(
      cs_root
      and OTA_CALLSIGN_RE.match(cs_root)
      and (has_direct_callsign or has_ota_tail_brand)
    )
    raw_cores = (
      (prep.core_tokens,)
      if prep.trimmed_core_tokens == prep.core_tokens
      else (prep.core_tokens, prep.trimmed_core_tokens)
    )
    cand_cores = (
      tuple(
        tuple(t for t in core if not (t.isdigit() and 1 <= int(t) <= 69))
        or core
        for core in raw_cores
      )
      if is_ota_callsign_hit
      else raw_cores
    )
    return (
      cs_root,
      epg_stem,
      allowed_extra,
      effective_spinoff_tokens,
      has_direct_callsign,
      has_ota_tail_brand,
      cand_cores,
    )

  def _score_alias_identity(
    self,
    *,
    prep: _PreparedStream,
    alias_specs: list[tuple[int, str, tuple[str, ...]]],
    cand_cores: tuple[tuple[str, ...], ...],
    allowed_extra: set[str],
    effective_spinoff_tokens: frozenset[str],
    cs_root: str | None,
    callsign_tok: str | None,
    has_direct_callsign: bool,
    edit_cache: dict[tuple[tuple[str, ...], tuple[str, ...]], float]
    | None = None,
  ) -> tuple[float, str]:
    """Signal A: Score exact, compound, phrase, fuzzy edit, subsequence, &
    acronym alias hits.
    """
    best_alias_score = 0.0
    best_alias_reason = ""

    for rank_idx, alias_raw, a_toks in alias_specs:
      specificity_bonus = max(0.0, 4.0 - rank_idx * 0.75)
      a_spaceless = "".join(t for t in a_toks if t != "&")
      for candidate_core in cand_cores:
        if not candidate_core:
          continue
        c_spaceless = "".join(t for t in candidate_core if t != "&")
        if candidate_core == a_toks:
          cand_score = 65.0 + specificity_bonus
          if cand_score > best_alias_score:
            best_alias_score = cand_score
            best_alias_reason = f"exact_alias:{alias_raw}"
          continue

        if len(a_spaceless) >= 3 and a_spaceless == c_spaceless:
          cand_score = 63.5 + specificity_bonus
          if cand_score > best_alias_score:
            best_alias_score = cand_score
            best_alias_reason = f"compound_alias:{alias_raw}"
          continue

        if len(a_toks) == 1 and len(a_toks[0]) == 1:
          extra_1ch = set(candidate_core) - {a_toks[0]}
          if (
            a_toks[0] in candidate_core
            and 1 <= len(extra_1ch) <= 2
            and extra_1ch <= BROADCAST_WRAPPER_TOKENS
          ):
            cand_score = 57.0 + specificity_bonus - 1.5 * len(extra_1ch)
            if cand_score > best_alias_score:
              best_alias_score = cand_score
              best_alias_reason = f"wrapper_alias:{alias_raw}"
          continue

        if _contains_token_sequence(candidate_core, a_toks):
          alias_set = set(a_toks)
          extra_tokens = [t for t in candidate_core if t not in alias_set]
          spinoff_hits = [
            t for t in extra_tokens if t in effective_spinoff_tokens
          ]
          unknown_extras = [
            t
            for t in extra_tokens
            if t not in allowed_extra and t not in effective_spinoff_tokens
          ]
          if (
            unknown_extras
            and cs_root
            and OTA_CALLSIGN_RE.match(cs_root)
            and a_toks in {(cs_root,), (callsign_tok,)}
            and not has_direct_callsign
          ):
            continue

          if spinoff_hits:
            if (
              not unknown_extras
              and set(spinoff_hits) <= {"international", "news"}
              and self._is_rare_specialty_brand(
                a_toks, candidate_core, allowed_extra
              )
            ):
              cand_score = 57.0 + specificity_bonus - 2.0 * len(extra_tokens)
            else:
              cand_score = 18.0 - 15.0 * len(spinoff_hits)
          elif not unknown_extras:
            cand_score = 60.0 + specificity_bonus - 1.5 * len(extra_tokens)
          elif len(a_toks) == 1 and len(a_toks[0]) <= 3:
            cand_score = 22.0 - 8.0 * len(unknown_extras)
          else:
            cand_score = 48.0 + specificity_bonus - 6.0 * len(unknown_extras)

          if cand_score > best_alias_score:
            best_alias_score = cand_score
            best_alias_reason = f"phrase_alias:{alias_raw}"
          continue

        cache_key = (a_toks, candidate_core)
        if edit_cache is not None and cache_key in edit_cache:
          edit_sim = edit_cache[cache_key]
        else:
          edit_sim = self._weighted_edit_similarity(
            a_toks,
            candidate_core,
            allowed_extra=allowed_extra,
            effective_spinoff_tokens=effective_spinoff_tokens,
          )
          if edit_cache is not None:
            edit_cache[cache_key] = edit_sim
        if edit_sim >= 0.78:
          cand_score = (
            45.0 + 17.0 * ((edit_sim - 0.78) / 0.22) + specificity_bonus * 0.75
          )
          if cand_score > best_alias_score:
            best_alias_score = cand_score
            best_alias_reason = f"edit_alias:{alias_raw}({edit_sim:.2f})"
          continue

        alias_set = set(a_toks)
        cand_set = set(candidate_core)
        a_nums = {t for t in alias_set if DECADE_OR_NUM_RE.match(t)}
        c_nums = {t for t in cand_set if DECADE_OR_NUM_RE.match(t)}
        if a_nums != c_nums:
          continue
        if ((cand_set - alias_set) & effective_spinoff_tokens) or (
          (alias_set - cand_set) & SPINOFF_MODIFIER_TOKENS
        ):
          continue

        q_idf = sum(
          self._token_idf.get(t, self._default_idf) for t in alias_set
        )
        ov_idf = sum(
          self._token_idf.get(t, self._default_idf)
          for t in (alias_set & cand_set)
        )
        if (
          len(candidate_core) >= 2
          and cand_set <= alias_set
          and _is_ordered_subsequence(candidate_core, a_toks)
          and q_idf > 0.0
          and (ov_idf / q_idf) >= 0.38
        ):
          distinctive_cand = cand_set - allowed_extra
          dropped_distinctive = (alias_set - cand_set) - allowed_extra
          if distinctive_cand:
            max_kept_idf = max(
              self._token_idf.get(t, self._default_idf)
              for t in distinctive_cand
            )
            max_dropped_idf = (
              max(
                self._token_idf.get(t, self._default_idf)
                for t in dropped_distinctive
              )
              if dropped_distinctive
              else 0.0
            )
            if max_kept_idf + 0.6 >= max_dropped_idf:
              cand_score = (
                46.0 + 10.0 * (ov_idf / q_idf) + specificity_bonus * 0.5
              )
              if cand_score > best_alias_score:
                best_alias_score = cand_score
                best_alias_reason = (
                  f"subseq_alias:{alias_raw}({ov_idf / q_idf:.2f})"
                )
        elif (
          len(candidate_core) == 1
          and len(candidate_core[0]) >= 3
          and candidate_core[0] not in allowed_extra
          and cand_set < alias_set
          and prep.epg_id_lower
          and len(self._token_index.get(candidate_core[0], ()))
          <= max(3, len(self._prepared) // 200)
        ):
          epg_norm = re.sub(
            r"[^a-z0-9]+",
            "",
            strip_accents(prep.epg_id_lower.rsplit(".", 1)[0]),
          )
          missing_distinctive = (alias_set - cand_set) - allowed_extra
          if missing_distinctive and all(
            len(tok) >= 3 and tok in epg_norm for tok in missing_distinctive
          ):
            cand_score = 56.0 + specificity_bonus * 0.75
            if cand_score > best_alias_score:
              best_alias_score = cand_score
              best_alias_reason = f"epg_acronym_alias:{alias_raw}"

    return best_alias_score, best_alias_reason

  @staticmethod
  def _score_regional_and_language_alignment(
    *,
    prep: _PreparedStream,
    channel: LineupChannel,
    target_regions: set[str],
  ) -> tuple[float, list[str]]:
    """Signal D: Score metro/province/country alignment, foreign penalties,
    timezone, & language.
    """
    delta = 0.0
    notes: list[str] = []

    for metro in tuple(CA_METRO_MARKERS) + tuple(US_METRO_MARKERS):
      if metro in target_regions and metro in prep.regions:
        delta += 24.0
        notes.append(f"region:{metro}")
    for sub in tuple(CA_PROVINCE_MARKERS) + tuple(US_STATE_MARKERS):
      if sub in target_regions and sub in prep.regions:
        delta += 16.0
        notes.append(f"region:{sub}")
    if "ca" in target_regions and "ca" in prep.regions:
      ca_boost = (
        12.0
        if channel.regions and channel.regions[0] in CANADA_PRIMARY_REGIONS
        else 6.0
      )
      delta += ca_boost
      notes.append("region:ca")
    elif "us" in target_regions and "us" in prep.regions:
      us_boost = (
        12.0
        if channel.regions and channel.regions[0] in US_PRIMARY_REGIONS
        else 5.0
      )
      delta += us_boost
      notes.append("region:us")
    elif "uk" in target_regions and "uk" in prep.regions:
      delta += 8.0
      notes.append("region:uk")

    if "foreign" in prep.prefix_regions:
      delta -= 45.0
      notes.append("penalty:foreign_region")
    elif "uk" in prep.prefix_regions and "uk" not in target_regions:
      delta -= 40.0
      notes.append("penalty:uk_feed")
    elif (
      "fr" in prep.prefix_regions
      and "fr" not in target_regions
      and "qc" not in prep.regions
      and "ca" not in prep.regions
    ):
      delta -= 28.0
      notes.append("penalty:france_feed")
    elif (
      "us" in prep.prefix_regions
      and "us" not in target_regions
      and "ca" not in prep.regions
    ):
      delta -= 18.0
      notes.append("penalty:us_feed_for_ca_only")

    alias_text_low = " ".join(channel.aliases).lower()
    prefers_west = "west" in target_regions or any(
      w in alias_text_low for w in ("west", "pacific")
    )
    if prefers_west:
      if {"west", "pacific"} & prep.full_tokens:
        delta += 8.0
        notes.append("tz:west")
      if {"east", "est", "eastern"} & prep.full_tokens and not any(
        w in alias_text_low for w in ("east", "eastern")
      ):
        delta -= 12.0
        notes.append("penalty:east_feed")
    else:
      if {"east", "est", "eastern"} & prep.full_tokens:
        delta += 5.0
        notes.append("tz:east")
      if {"west", "pacific"} & prep.full_tokens:
        delta -= 22.0
        notes.append("penalty:west_feed")

    if prep.is_timeshift:
      delta -= 18.0
      notes.append("penalty:timeshift")

    if channel.language == "fr":
      if {"fr", "francais", "french", "qc", "quebec"} & prep.full_tokens:
        delta += 5.0
      elif {"en", "english", "caen", "enca"} & prep.full_tokens:
        delta -= 15.0
    elif channel.language == "en" and (
      {"fr", "francais", "french", "cafr", "frca"} & prep.full_tokens
    ):
      delta -= 15.0

    return delta, notes

  def _score_candidate(
    self,
    *,
    prep: _PreparedStream,
    channel: LineupChannel,
    alias_specs: list[tuple[int, str, tuple[str, ...]]],
    callsign_tok: str | None,
    tvg_id_lower: str | None,
    target_regions: set[str],
    edit_cache: dict[tuple[tuple[str, ...], tuple[str, ...]], float]
    | None = None,
  ) -> tuple[float, list[str], bool]:
    score = 0.0
    reasons: list[str] = []

    (
      cs_root,
      epg_stem,
      allowed_extra,
      effective_spinoff_tokens,
      has_direct_callsign,
      has_ota_tail_brand,
      cand_cores,
    ) = self._build_identity_context(
      prep=prep,
      channel=channel,
      callsign_tok=callsign_tok,
      target_regions=target_regions,
    )

    best_alias_score, best_alias_reason = self._score_alias_identity(
      prep=prep,
      alias_specs=alias_specs,
      cand_cores=cand_cores,
      allowed_extra=allowed_extra,
      effective_spinoff_tokens=effective_spinoff_tokens,
      cs_root=cs_root,
      callsign_tok=callsign_tok,
      has_direct_callsign=has_direct_callsign,
      edit_cache=edit_cache,
    )
    if best_alias_score > 0:
      score += best_alias_score
      reasons.append(best_alias_reason)

    # Signal B: Native Guide Data (EPG) preference
    has_name_or_callsign = (
      best_alias_score > 0 or has_direct_callsign or has_ota_tail_brand
    )
    has_native_epg = False
    if (
      prep.epg_id_lower
      and has_name_or_callsign
      and (
        has_direct_callsign
        or _is_plausible_native_epg(
          prep.epg_id_lower,
          tvg_id_lower=tvg_id_lower,
          callsign_tok=callsign_tok,
          alias_specs=alias_specs,
        )
      )
    ):
      has_native_epg = True
      if (tvg_id_lower and prep.epg_id_lower == tvg_id_lower) or (
        cs_root and len(cs_root) >= 3 and epg_stem in {callsign_tok, cs_root}
      ):
        score += 32.0
        reasons.append(f"native_epg_exact:{prep.stream.epg_channel_id}")
      else:
        score += 22.0
        reasons.append(f"native_epg:{prep.stream.epg_channel_id}")

    # Signal C: Local Station Callsign Match
    if has_direct_callsign or has_ota_tail_brand:
      score += 56.0 if best_alias_score == 0 else 18.0
      reasons.append(f"callsign:{channel.callsign}")

    if score < 40.0:
      return 0.0, [], False

    # Signal D: Regional, Timezone, & Language Alignment
    reg_delta, reg_notes = self._score_regional_and_language_alignment(
      prep=prep,
      channel=channel,
      target_regions=target_regions,
    )
    score += reg_delta
    reasons.extend(reg_notes)

    # Signal E: Stream Quality Tier
    score += prep.quality_score
    reasons.extend(prep.quality_notes)

    return score, reasons, has_native_epg
