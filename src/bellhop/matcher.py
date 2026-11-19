"""Multi-signal channel matcher mapping Xtream streams to Bell Fibe Montreal slots."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from bellhop.models import (
  LineupChannel,
  MappedChannel,
  MatchCandidate,
  XtreamStream,
)

# Dummy separator rows common in Xtream catalogs (e.g. "##### CANADA #####")
SEPARATOR_RE = re.compile(r"^[\s#=*_~<>|-]{3,}.*[\s#=*_~<>|-]{3,}$")

# Explicit prefix blocks at the beginning of stream names:
# e.g. "[CA-FR] ", "(QC) ", "VIP | CA | ", "CA: ", "QC - "
LEADING_BRACKET_RE = re.compile(r"^\s*(?:[\[\(]([^\]\)]{1,20})[\]\)]\s*)+")
DELIMITER_SPLIT_RE = re.compile(r"\s*(?:\||¦|::|—|–|\s-\s|:)\s*")

# Codec / framerate / resolution tokens stripped before channel identity comparison
QUALITY_AND_TECH_TOKENS = frozenset(
  {
    "4k",
    "8k",
    "uhd",
    "fhd",
    "hd",
    "sd",
    "raw",
    "lq",
    "hq",
    "2160p",
    "1080p",
    "1080i",
    "720p",
    "576p",
    "540p",
    "480p",
    "480i",
    "hevc",
    "h265",
    "x265",
    "h264",
    "x264",
    "avc",
    "mpeg2",
    "mpeg4",
    "60fps",
    "59fps",
    "50fps",
    "30fps",
    "fps",
    "aac",
    "ac3",
    "eac3",
    "dolby",
    "5.1",
    "stereo",
    "vip",
    "backup",
    "bkp",
    "bk",
    "alt",
    "feed",
  }
)

# Leading country/market/platform prefix tokens when ":" or "|" or space is used
LEADING_COUNTRY_CODES = frozenset(
  {
    "ca",
    "can",
    "qc",
    "frca",
    "cafr",
    "enca",
    "caen",
    "us",
    "usa",
    "uk",
    "gb",
    "fr",
    "prime",
    "tv",
    "rk",
    "roku",
    "tubi",
    "att",
    "pluto",
  }
)

# Extra tokens inside a stream name that do NOT change the identity of a channel
BENIGN_QUALIFIER_TOKENS = frozenset(
  {
    "ca",
    "can",
    "canada",
    "canadian",
    "qc",
    "quebec",
    "montreal",
    "mtl",
    "burlington",
    "plattsburgh",
    "vermont",
    "vt",
    "ny",
    "us",
    "usa",
    "east",
    "est",
    "eastern",
    "fr",
    "francais",
    "french",
    "en",
    "english",
    "channel",
    "network",
    "tv",
    "tele",
    "television",
    "live",
    "local",
    "national",
    "main",
    "primary",
    "r",
  }
)

# Tokens that strongly indicate a DIFFERENT spin-off channel if not present in the alias
SPINOFF_MODIFIER_TOKENS = frozenset(
  {
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "plus",
    "info",
    "news",
    "nouvelles",
    "sports",
    "sport",
    "kids",
    "jr",
    "junior",
    "xd",
    "family",
    "wild",
    "life",
    "drama",
    "comedy",
    "scifi",
    "sci",
    "fi",
    "speed",
    "movies",
    "movie",
    "cinema",
    "action",
    "world",
    "extra",
    "xtra",
    "redzone",
    "golazo",
    "espanol",
    "latino",
    "arabic",
    "west",
    "pacific",
    "ontario",
    "alberta",
    "atlantic",
    "bc",
    "vancouver",
    "toronto",
    "calgary",
    "edmonton",
    "winnipeg",
    "ottawa",
    "halifax",
    "quebeccity",
    "sherbrooke",
  }
)

MONTREAL_MARKERS = frozenset(
  {
    "montreal",
    "mtl",
    "cbft",
    "cfjp",
    "cftm",
    "civm",
    "cftu",
    "cfcf",
    "ckmi",
    "cjnt",
    "cbmt",
    "habs",
    "canadiens",
  }
)

QUEBEC_MARKERS = frozenset(
  {"qc", "quebec", "franco", "francophone", "cafr", "frca"}
)

CANADA_MARKERS = frozenset({"ca", "can", "canada", "canadian"})
US_MARKERS = frozenset(
  {
    "us",
    "usa",
    "burlington",
    "plattsburgh",
    "vermont",
    "wvny",
    "wcax",
    "wptz",
    "wfff",
    "wetk",
  }
)
UK_MARKERS = frozenset(
  {"uk", "gb", "united kingdom", "british", "sky uk", "fhd uk"}
)
FOREIGN_PREFIX_MARKERS = frozenset(
  {
    "au",
    "aus",
    "australia",
    "nz",
    "in",
    "ind",
    "india",
    "za",
    "sa",
    "ie",
    "irl",
    "de",
    "ger",
    "es",
    "esp",
    "it",
    "ita",
    "pt",
    "por",
    "br",
    "bra",
    "mx",
    "mex",
    "ar",
    "arg",
    "tr",
    "turk",
    "arab",
    "arabic",
    "af",
    "africa",
    "nl",
    "pl",
    "ro",
    "gr",
  }
)

NON_LINEAR_RE = re.compile(
  r"(?:\b(?:24/7|24x7|24-7|ppv|vod|catchup|xxx|adult(?!\s*swim)|audio\s+only)\b|(?<!\w)18\+(?!\w))",
  re.IGNORECASE,
)
TIMESHIFT_RE = re.compile(r"(?:^|\s)\+[123](?:\s|$|\])")
LETTER_DIGIT_RE = re.compile(r"^([a-z]{2,})(\d+)$")
E_EXCLAMATION_RE = re.compile(r"(?<![a-z0-9])e!(?![a-z0-9])", re.IGNORECASE)
CODEC_CLEAN_RE = re.compile(
  r"\b(?:h\.?26[45]|x26[45]|hevc|mpeg-?[24]|1080[pi]|720p|2160p|540p|480[pi]|[356]0\s*fps|5\.1)\b",
  re.IGNORECASE,
)


def strip_accents(text: str) -> str:
  """Convert accented characters (e.g. Montréal, Télé-Québec, Écran) to ASCII."""
  normalized = unicodedata.normalize("NFKD", text)
  return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalize_symbols(text: str) -> str:
  s = strip_accents(text)
  s = re.sub(r"\bat&t\b", "att", s, flags=re.IGNORECASE)
  s = E_EXCLAMATION_RE.sub(" eexclamation ", s)
  # Convert "+" to " plus " unless it is a timeshift like +1 / +2
  s = re.sub(r"\+(?![123]\b)", " plus ", s)
  s = s.replace("&", " and ")
  return s.lower()


def _tokenize_raw(text: str, *, split_alnum: bool = True) -> list[str]:
  s = _normalize_symbols(text)
  raw_tokens = re.findall(r"[a-z0-9]+", s)
  if not split_alnum:
    return raw_tokens
  out: list[str] = []
  for tok in raw_tokens:
    m = LETTER_DIGIT_RE.match(tok)
    if m:
      out.append(m.group(1))
      out.append(m.group(2))
    else:
      out.append(tok)
  return out


def _strip_quality_tokens(tokens: Iterable[str]) -> list[str]:
  return [t for t in tokens if t not in QUALITY_AND_TECH_TOKENS]


def _score_quality_tags(
  raw_name: str, category_name: str = ""
) -> tuple[float, str, list[str]]:
  # Normalize Unicode superscripts (e.g. ᴴᴰ -> HD, ᴿᴬᵂ -> RAW, ⁶⁰ᶠᵖˢ -> 60FPS, ⁽ᴮᴷ⁾ -> (BK))
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
  """Verify a provider's epg_channel_id plausibly belongs to this channel (filters panel typos)."""
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

  # If all tokens of every alias are common network words (e.g. "ICI Montreal" / "Radio-Canada"),
  # require all tokens of at least one multi-word alias to appear in epg_compact.
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
    # Treat short leading segments (e.g. "CA", "QC", "VIP", "CA-FR", "CANADA") as prefix metadata
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

  # Remove parenthetical codec/resolution suffixes while preserving city/callsign hints in full_tokens
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

  # Build full token set (without codec noise like h264/mpeg2 so "2" won't false-trigger exclusions)
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


class ChannelMatcher:
  """Indexes an Xtream catalog and ranks candidate streams for each cable lineup channel."""

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
      if prep.epg_id_lower:
        self._epg_index[prep.epg_id_lower].append(idx)

  def match_lineup(
    self,
    channels: Iterable[LineupChannel],
    *,
    max_candidates: int = 10,
  ) -> list[MappedChannel]:
    """Return ranked stream candidates for every channel in the lineup."""
    return [
      MappedChannel(
        channel=ch,
        candidates=self.find_candidates(ch, limit=max_candidates),
      )
      for ch in channels
    ]

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

    # 2. Gather candidate indices via inverted index (avoids scanning 50k+ unrelated streams)
    candidate_indices: set[int] = set()
    if tvg_id_lower and tvg_id_lower in self._epg_index:
      candidate_indices.update(self._epg_index[tvg_id_lower])
    if callsign_tok and callsign_tok in self._token_index:
      candidate_indices.update(self._token_index[callsign_tok])

    for _, _, a_toks in alias_specs:
      postings = [
        self._token_index[t] for t in a_toks if t in self._token_index
      ]
      if postings and len(postings) == len(a_toks):
        intersected = set(postings[0])
        for p in postings[1:]:
          intersected &= p
        candidate_indices.update(intersected)

    target_regions = set(channel.regions)

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

    # Sort by descending score, preferring native EPG and lower stream_id on exact ties
    results.sort(
      key=lambda c: (-c.score, not c.has_native_epg, c.stream.stream_id)
    )
    return results[:limit]

  def _score_candidate(
    self,
    *,
    prep: _PreparedStream,
    channel: LineupChannel,
    alias_specs: list[tuple[int, str, tuple[str, ...]]],
    callsign_tok: str | None,
    tvg_id_lower: str | None,
    target_regions: set[str],
  ) -> tuple[float, list[str], bool]:
    score = 0.0
    reasons: list[str] = []

    # Signal A: Alias / Channel Identity Match
    best_alias_score = 0.0
    best_alias_reason = ""
    allowed_extra = set(BENIGN_QUALIFIER_TOKENS)
    if callsign_tok:
      allowed_extra.add(callsign_tok)

    for rank_idx, alias_raw, a_toks in alias_specs:
      specificity_bonus = max(0.0, 4.0 - rank_idx * 0.75)
      for candidate_core in (prep.core_tokens, prep.trimmed_core_tokens):
        if candidate_core == a_toks:
          cand_score = 65.0 + specificity_bonus
          if cand_score > best_alias_score:
            best_alias_score = cand_score
            best_alias_reason = f"exact_alias:{alias_raw}"
        elif _contains_token_sequence(candidate_core, a_toks):
          # Ultra-short 1-char aliases (like "Z") require exact core match
          if len(a_toks) == 1 and len(a_toks[0]) <= 1:
            continue
          alias_set = set(a_toks)
          extra_tokens = [t for t in candidate_core if t not in alias_set]
          spinoff_hits = [
            t for t in extra_tokens if t in SPINOFF_MODIFIER_TOKENS
          ]
          unknown_extras = [
            t
            for t in extra_tokens
            if t not in allowed_extra and t not in SPINOFF_MODIFIER_TOKENS
          ]

          if spinoff_hits:
            # Strongly penalize unrequested spin-offs (e.g. "CTV Sci-Fi" for "CTV")
            cand_score = 18.0 - 15.0 * len(spinoff_hits)
          elif not unknown_extras:
            cand_score = 60.0 + specificity_bonus - 1.5 * len(extra_tokens)
          else:
            # Short single-word aliases shouldn't loosely match multi-word unrelated titles
            if len(a_toks) == 1 and len(a_toks[0]) <= 3:
              cand_score = 22.0 - 8.0 * len(unknown_extras)
            else:
              cand_score = 48.0 + specificity_bonus - 6.0 * len(unknown_extras)

          if cand_score > best_alias_score:
            best_alias_score = cand_score
            best_alias_reason = f"phrase_alias:{alias_raw}"

    if best_alias_score > 0:
      score += best_alias_score
      reasons.append(best_alias_reason)

    # Signal B: Native Guide Data (EPG) preference — only boosts streams with name/callsign evidence
    has_name_or_callsign = best_alias_score > 0 or (
      callsign_tok is not None and callsign_tok in prep.full_tokens
    )
    has_native_epg = False
    if (
      prep.epg_id_lower
      and has_name_or_callsign
      and _is_plausible_native_epg(
        prep.epg_id_lower,
        tvg_id_lower=tvg_id_lower,
        callsign_tok=callsign_tok,
        alias_specs=alias_specs,
      )
    ):
      has_native_epg = True
      if tvg_id_lower and prep.epg_id_lower == tvg_id_lower:
        score += 32.0
        reasons.append(f"native_epg_exact:{prep.stream.epg_channel_id}")
      else:
        score += 22.0
        reasons.append(f"native_epg:{prep.stream.epg_channel_id}")

    # Signal C: Local Station Callsign Match (e.g. CBFT, CFCF, WVNY)
    if callsign_tok and callsign_tok in prep.full_tokens:
      if best_alias_score == 0:
        score += 56.0
      else:
        score += 18.0
      reasons.append(f"callsign:{channel.callsign}")

    # Must have at least one strong identity signal before adding region/quality boosts
    if score < 40.0:
      return 0.0, [], False

    # Signal D: Regional & Market Alignment (Montreal > Quebec > Canada > US)
    if "montreal" in target_regions and "montreal" in prep.regions:
      score += 24.0
      reasons.append("region:montreal")
    if "qc" in target_regions and "qc" in prep.regions:
      score += 16.0
      reasons.append("region:qc")
    if "ca" in target_regions and "ca" in prep.regions:
      # Higher boost if Canada is primary region vs secondary
      ca_boost = (
        12.0
        if channel.regions and channel.regions[0] in {"montreal", "qc", "ca"}
        else 6.0
      )
      score += ca_boost
      reasons.append("region:ca")
    elif "us" in target_regions and "us" in prep.regions:
      us_boost = 12.0 if channel.regions and channel.regions[0] == "us" else 5.0
      score += us_boost
      reasons.append("region:us")
    elif "uk" in target_regions and "uk" in prep.regions:
      score += 8.0
      reasons.append("region:uk")

    # Penalize foreign feeds when target channel is North American / Quebecois
    if "foreign" in prep.prefix_regions:
      score -= 45.0
      reasons.append("penalty:foreign_region")
    elif "uk" in prep.prefix_regions and "uk" not in target_regions:
      score -= 40.0
      reasons.append("penalty:uk_feed")
    elif (
      "fr" in prep.prefix_regions
      and "fr" not in target_regions
      and "qc" not in prep.regions
      and "ca" not in prep.regions
    ):
      # Distinguish France (TF1/Canal+ FR category) from Quebec French unless France is allowed
      score -= 28.0
      reasons.append("penalty:france_feed")
    elif (
      "us" in prep.prefix_regions
      and "us" not in target_regions
      and "ca" not in prep.regions
    ):
      score -= 18.0
      reasons.append("penalty:us_feed_for_ca_only")

    # East vs West time-zone preference for Montreal (Eastern Time)
    if {"east", "est", "eastern"} & prep.full_tokens:
      score += 5.0
      reasons.append("tz:east")
    if {"west", "pacific"} & prep.full_tokens and not any(
      w in (" ".join(channel.aliases).lower()) for w in ("west", "pacific")
    ):
      score -= 22.0
      reasons.append("penalty:west_feed")

    # Timeshift penalty (+1 / +2)
    if prep.is_timeshift:
      score -= 18.0
      reasons.append("penalty:timeshift")

    # Language hint alignment
    if channel.language == "fr" and (
      {"fr", "francais", "french", "qc", "quebec"} & prep.full_tokens
    ):
      score += 5.0
    elif channel.language == "en" and (
      {"fr", "francais", "french"} & set(prep.core_tokens)
    ):
      score -= 15.0

    # Signal E: Stream Quality Tier
    score += prep.quality_score
    reasons.extend(prep.quality_notes)

    return score, reasons, has_native_epg
