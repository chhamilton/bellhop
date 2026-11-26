"""2D `(Language x Genre)` Shannon self-information dial segmentation for Phase
(1).

Assigns strictly dial-contiguous channel groups by labeling each channel slot
with an
empirical `(language, genre)` leaf from `iptv-org` (with whole-word fallback
when
`iptv-org` omits categories), computing Shannon self-information `I(u) = -log2
P(u)`
over the lineup's own language and genre distributions (zero market hardcoding),
and
solving the exact 1D Minimum Description Length (MDL) dynamic program along the
dial.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from bellhop.pipeline.iptv_org import (
  IptvOrgIndex,
  normalize_country_iso2,
  normalize_lookup_key,
)
from bellhop.pipeline.models import LineupChannel

MACRO_GENRE_MAP: dict[str, str] = {
  "news": "News & Info",
  "business": "News & Info",
  "weather": "News & Info",
  "legislative": "News & Info",
  "sports": "Sports",
  "sports_overflow": "Sports",
  "outdoor": "Sports",
  "auto": "Sports",
  "movies": "Movies",
  "classic": "Movies",
  "series": "Entertainment & Lifestyle",
  "comedy": "Entertainment & Lifestyle",
  "entertainment": "Entertainment & Lifestyle",
  "culture": "Entertainment & Lifestyle",
  "lifestyle": "Entertainment & Lifestyle",
  "cooking": "Entertainment & Lifestyle",
  "travel": "Entertainment & Lifestyle",
  "fashion": "Entertainment & Lifestyle",
  "health": "Entertainment & Lifestyle",
  "science": "Science & Docs",
  "documentary": "Science & Docs",
  "education": "Science & Docs",
  "kids": "Kids & Family",
  "animation": "Kids & Family",
  "family": "Kids & Family",
  "music": "Music & Audio",
  "relax": "Music & Audio",
  "general": "Broadcast & Networks",
  "regional_broadcast": "Broadcast & Networks",
  "religious": "Broadcast & Networks",
  "shop": "Entertainment & Lifestyle",
}

SUBGENRE_DISPLAY: dict[str, str] = {
  "sports_overflow": "Sports — League & Overflows",
  "regional_broadcast": "Time-Shift & Regional",
  "lifestyle": "Lifestyle",
  "cooking": "Lifestyle",
}

ISO3_TO_ISO1_LANG: dict[str, str] = {
  "eng": "en",
  "fra": "fr",
  "fre": "fr",
  "spa": "es",
  "ita": "it",
  "ara": "ar",
  "por": "pt",
  "deu": "de",
  "ger": "de",
  "ell": "el",
  "gre": "el",
  "zho": "zh",
  "cmn": "zh",
  "yue": "zh",
  "hin": "hi",
  "pan": "pa",
  "urd": "ur",
  "tgl": "tl",
  "rus": "ru",
  "ukr": "uk",
  "pol": "pl",
  "ron": "ro",
  "tur": "tr",
  "vie": "vi",
  "jpn": "ja",
  "kor": "ko",
}

ISO1_TO_ISO3_LANG: dict[str, str] = {
  "en": "eng",
  "fr": "fra",
  "es": "spa",
  "it": "ita",
  "ar": "ara",
  "pt": "por",
  "de": "deu",
  "el": "ell",
  "zh": "zho",
  "hi": "hin",
  "pa": "pan",
  "ur": "urd",
  "tl": "tgl",
  "ru": "rus",
  "uk": "ukr",
  "pl": "pol",
  "ro": "ron",
  "tr": "tur",
  "vi": "vie",
  "ja": "jpn",
  "ko": "kor",
}

LANG_DISPLAY_NAMES: dict[str, str] = {
  "eng": "English",
  "fra": "French",
  "spa": "Spanish",
  "ita": "Italian",
  "ara": "Arabic",
  "por": "Portuguese",
  "deu": "German",
  "ell": "Greek",
  "zho": "Chinese",
  "cmn": "Mandarin",
  "yue": "Cantonese",
  "hin": "Hindi",
  "pan": "Punjabi",
  "urd": "Urdu",
  "tgl": "Filipino",
  "rus": "Russian",
  "ukr": "Ukrainian",
  "pol": "Polish",
  "ron": "Romanian",
  "tur": "Turkish",
  "vie": "Vietnamese",
  "jpn": "Japanese",
  "kor": "Korean",
}

SPORTS_OVERFLOW_RE = re.compile(
  r"\b(?:center\s+ice|centre\s+ice|league\s+pass|extra\s+innings|sunday\s+ticket|direct\s+kick|sports\s+programming)\b",
  re.IGNORECASE,
)

# Whole-word fallback rules applied ONLY when `iptv-org` lacks category tags for
# a slot
WORD_GENRE_FALLBACK: tuple[tuple[re.Pattern[str], str], ...] = (
  (SPORTS_OVERFLOW_RE, "sports_overflow"),
  (
    re.compile(
      r"\b(?:sports?|rds\d*|tsn\d*|sportsnet|espn\d*|fs1|fs2|nhl|nba|nfl|mlb|golf|hpitv|bein|dazn|eurosport|nesn|msg)\b",
      re.IGNORECASE,
    ),
    "sports",
  ),
  (
    re.compile(
      r"\b(?:movies?|movietime|cinema|cinepop|super\s*ecran|crave\d*|hbo\d*|starz\d*|showtime|cinemax|tcm|amc|sundance|ifc|super\s+channel|hollywood\s+suite|silver\s+screen|frissons)\b",
      re.IGNORECASE,
    ),
    "movies",
  ),
  (
    re.compile(
      r"\b(?:news|nouvelles|cnn|hln| fox\s+business|rdi|lcn|cp24|cnbc|msnbc|bloomberg|weather|meteo|meteomedia|legislature|assemblee|parliament|c-span|cspan|cpac|qub|euronews|france\s*24|al\s+jazeera)\b",
      re.IGNORECASE,
    ),
    "news",
  ),
  (
    re.compile(
      r"\b(?:kids?|youth\s+television|ytv|treehouse|disney|cartoon|boomerang|nickelodeon|nickjr|nicktoons|teletoon|tiji|yoopa|gulli|babytv)\b",
      re.IGNORECASE,
    ),
    "kids",
  ),
  (
    re.compile(
      r"\b(?:nature|wild|science|smithsonian|history|historia|explora|discovery|national\s+geographic|nat\s+geo|animal\s+planet|planete|canal\s+d|savoir)\b",
      re.IGNORECASE,
    ),
    "documentary",
  ),
  (
    re.compile(
      r"\b(?:stingray|music\s+choice|music|musique|much|cmt|axs|mezzo|mtv\s+live|mtv\s+classic|ami-audio|canal\s+m)\b",
      re.IGNORECASE,
    ),
    "music",
  ),
  (
    re.compile(
      r"\b(?:slice|w\s+network|ctv\s+life|home\s+network|flavour|hgtv|food\s+network|cooking|magnolia|tlc|bravo|casa|zeste|canal\s+vie|cottage|makeful)\b",
      re.IGNORECASE,
    ),
    "lifestyle",
  ),
  (
    re.compile(
      r"\b(?:ctv\s+drama|ctv\s+comedy|ctv\s+sci-fi|showcase|e!|a&e|usa\s+network|tnt|tbs|trutv|syfy|comedy|bet|vh1|mtv|hallmark|game\s+tv|game\s+show|gsn|evasion|prise\s*2|adult\s+swim|fx|fxx|lifetime|addik|series|temoin|elle\s+fictions)\b",
      re.IGNORECASE,
    ),
    "entertainment",
  ),
  (
    re.compile(
      r"\b(?:radio-canada|cbc|ctv|global|citytv|noovo|tva|tele-quebec|abc|cbs|nbc|fox|pbs|cw|ion|metv|omni|unis|tv5|artv|ami-tv|ami-tele|tfo)\b",
      re.IGNORECASE,
    ),
    "general",
  ),
)

PAREN_GROUP_RE = re.compile(r"\(([^()]+)\)")


@dataclass(slots=True)
class ChannelTaxonomyLeaf:
  """Per-channel `(language, leaf_genre, macro_genre)` input to 1D MDL
  segmentation.
  """

  chno: int
  name: str
  lang_iso3: str | None
  leaf_genre: str | None
  macro_genre: str | None


@dataclass(frozen=True, slots=True)
class DialSegment:
  """Contiguous dial slice `[start_idx, end_idx)` emitted by
  `segment_dial_channels`.
  """

  start_idx: int
  end_idx: int
  start_chno: int
  end_chno: int
  lang_node: str
  genre_node: str
  group_title: str


def _normalize_iso3_lang(lang: str | None) -> str | None:
  if not lang:
    return None
  low = lang.strip().lower()
  if not low:
    return None
  if low == "intl_hint":
    return "INTL_HINT"
  if low in ISO1_TO_ISO3_LANG:
    return ISO1_TO_ISO3_LANG[low]
  if low in ISO3_TO_ISO1_LANG:
    return ISO1_TO_ISO3_LANG.get(ISO3_TO_ISO1_LANG[low], low)
  return low[:3] if len(low) >= 3 else None


def _fallback_genre_from_title(name: str, callsign: str | None) -> str | None:
  hay = f"{name} {callsign or ''}"
  for pattern, genre_key in WORD_GENRE_FALLBACK:
    if pattern.search(hay):
      return genre_key
  return None


def _dedupe_preserve_order(items: Sequence[str]) -> list[str]:
  seen: set[str] = set()
  out: list[str] = []
  for raw in items:
    cleaned = " ".join(raw.split()).strip()
    if not cleaned:
      continue
    k = cleaned.casefold()
    if k not in seen:
      seen.add(k)
      out.append(cleaned)
  return out


def build_channel_leaves_and_enrich(
  channels: Sequence[LineupChannel],
  *,
  iptv_org_index: IptvOrgIndex | None = None,
  market: str = "",
  country: str = "CAN",
) -> list[ChannelTaxonomyLeaf]:
  """Enrich `LineupChannel` metadata from `iptv-org` and return taxonomy
  leaves.
  """
  iso2_country = normalize_country_iso2(country)
  market_city_norm = normalize_lookup_key(market.split(",", 1)[0])
  leaves: list[ChannelTaxonomyLeaf] = []

  for ch in channels:
    low_name = ch.name.strip().lower()
    if low_name == "on demand television":
      leaves.append(ChannelTaxonomyLeaf(ch.chno, ch.name, None, None, None))
      continue
    if "international programming" in low_name:
      leaves.append(
        ChannelTaxonomyLeaf(ch.chno, ch.name, "INTL_HINT", None, None)
      )
      continue
    if low_name == "radio":
      leaves.append(
        ChannelTaxonomyLeaf(
          ch.chno, ch.name, "INTL_HINT", "music", MACRO_GENRE_MAP["music"]
        )
      )
      continue

    if iptv_org_index is not None and not iptv_org_index.is_empty:
      ch.aliases = iptv_org_index.filter_active_aliases(
        ch.aliases, primary_name=ch.name, country=iso2_country
      )
      if iptv_org_index.is_permanently_closed_id(ch.tvg_id):
        ch.tvg_id = None

    match = (
      iptv_org_index.lookup(
        ch.name,
        tvg_id=ch.tvg_id,
        callsign=ch.callsign,
        aliases=ch.aliases,
        country=iso2_country,
      )
      if (iptv_org_index is not None and not iptv_org_index.is_empty)
      else None
    )

    lang_iso3: str | None = None
    leaf_genre: str | None = None

    if SPORTS_OVERFLOW_RE.search(ch.name):
      leaf_genre = "sports_overflow"

    if match is not None:
      info = match.channel
      if (
        match.was_replaced
        or not ch.tvg_id
        or (iptv_org_index is not None and not iptv_org_index.has_id(ch.tvg_id))
      ):
        ch.tvg_id = info.channel_id

      # Add active canonical name & alt_names whenever fuzzy station match
      # succeeded
      # (skipping generic US parent-network fallbacks on local affiliate slots
      # like "ABC (Burlington)")
      ch_base_norm = normalize_lookup_key(PAREN_GROUP_RE.sub("", ch.name))
      us_generic_networks = {"abc", "cbs", "nbc", "fox", "pbs", "cw", "thecw"}
      feed_city = (
        iptv_org_index.feed_city_for_callsign(ch.callsign)
        if iptv_org_index is not None
        else None
      )
      # Upgrade bare OTA callsign titles ("CBFT" -> "ICI Radio-Canada Télé
      # (Montreal)")
      if (
        ch.callsign
        and (
          ch_base_norm == normalize_lookup_key(ch.callsign)
          or normalize_lookup_key(ch.name)
          == normalize_lookup_key(f"{info.name} {ch.callsign}")
        )
        and normalize_lookup_key(info.name) != ch_base_norm
        and normalize_lookup_key(info.name) not in us_generic_networks
      ):
        qual = feed_city or ch.callsign
        ch.name = f"{info.name} ({qual})"
        ch_base_norm = normalize_lookup_key(info.name)
      if match.was_replaced or (
        match.similarity >= 0.78 and ch_base_norm not in us_generic_networks
      ):
        extra_aliases: list[str] = [info.name, *info.alt_names]
        parens = PAREN_GROUP_RE.findall(ch.name)
        city_qual = feed_city or (parens[-1].strip() if parens else "")
        if city_qual and normalize_lookup_key(city_qual) not in {
          "canada",
          "us",
          "usa",
          "hd",
          "east",
          "west",
        }:
          for base_alt in (info.name, *info.alt_names):
            if city_qual.lower() not in base_alt.lower():
              extra_aliases.append(f"{base_alt} {city_qual}")
          if "ici" in info.name.lower():
            extra_aliases.append(f"ICI {city_qual}")
        if iptv_org_index is not None:
          extra_aliases = iptv_org_index.filter_active_aliases(
            extra_aliases, primary_name=ch.name, country=iso2_country
          )
        ch.aliases = _dedupe_preserve_order([*ch.aliases, *extra_aliases])

      if leaf_genre is None:
        valid_cats = [c for c in info.categories if c in MACRO_GENRE_MAP]
        non_general = [c for c in valid_cats if c != "general"]
        leaf_genre = (
          non_general[0]
          if non_general
          else (valid_cats[0] if valid_cats else None)
        )

      if info.languages:
        lang_iso3 = info.languages[0]
        # Update 2-letter channel language when `iptv-org` provides an explicit
        # feed language
        # (unless local slot explicitly identified French on a bilingual
        # Canadian station)
        mapped_iso1 = ISO3_TO_ISO1_LANG.get(lang_iso3)
        if mapped_iso1 and not (
          ch.language == "fr" and info.country == "CA" and mapped_iso1 == "en"
        ):
          ch.language = mapped_iso1

    if leaf_genre is None:
      leaf_genre = _fallback_genre_from_title(ch.name, ch.callsign)

    if ch.language == "fr" and lang_iso3 in (None, "eng"):
      if match is None or match.channel.country == "CA":
        lang_iso3 = "fra"
    elif lang_iso3 is None:
      lang_iso3 = _normalize_iso3_lang(ch.language)

    # Distinguish out-of-market regional/time-shift broadcast feeds from
    # in-market locals
    if leaf_genre == "general":
      parens = PAREN_GROUP_RE.findall(ch.name)
      if parens and market_city_norm:
        city_tok = normalize_lookup_key(parens[-1])
        if (
          city_tok
          and city_tok != market_city_norm
          and city_tok
          not in {"canada", "us", "usa", "hd", "east", "english", "french"}
        ):
          leaf_genre = "regional_broadcast"

    macro_genre = MACRO_GENRE_MAP.get(leaf_genre) if leaf_genre else None
    leaves.append(
      ChannelTaxonomyLeaf(
        chno=ch.chno,
        name=ch.name,
        lang_iso3=lang_iso3,
        leaf_genre=leaf_genre,
        macro_genre=macro_genre,
      )
    )

  return leaves


def segment_dial_channels(
  leaves: Sequence[ChannelTaxonomyLeaf],
) -> list[DialSegment]:
  """Partition dial-sorted `leaves` into strictly contiguous `(Language x
  Genre)` groups.
  """
  n = len(leaves)
  if n == 0:
    return []

  real_langs = [
    x.lang_iso3 for x in leaves if x.lang_iso3 and x.lang_iso3 != "INTL_HINT"
  ]
  lang_counts = Counter(real_langs)
  known_lang_total = max(len(real_langs), 1)
  default_lang = lang_counts.most_common(1)[0][0] if lang_counts else "eng"
  major_langs = {
    lg for lg, cnt in lang_counts.items() if cnt / known_lang_total >= 0.05
  }

  known_genre_total = max(sum(1 for x in leaves if x.macro_genre), 1)
  macro_g_counts = Counter(x.macro_genre for x in leaves if x.macro_genre)
  sub_g_counts = Counter(
    x.leaf_genre for x in leaves if x.leaf_genre in SUBGENRE_DISPLAY
  )

  intl_n = sum(v for k, v in lang_counts.items() if k not in major_langs) + sum(
    1 for x in leaves if x.lang_iso3 == "INTL_HINT"
  )

  def _calc_ic_lang(node: str) -> float:
    if node == "ANY":
      return 0.0
    if node == "INTL":
      return -math.log2(min(max(intl_n, 1) / (known_lang_total + 10), 1.0))
    return -math.log2(min(max(lang_counts[node], 1) / known_lang_total, 1.0))

  def _calc_ic_genre(node: str) -> float:
    if node == "ANY":
      return 0.0
    if node.startswith("sub:"):
      sg = node[4:]
      return -math.log2(min(max(sub_g_counts[sg], 1) / known_genre_total, 1.0))
    return -math.log2(
      min(max(macro_g_counts[node], 1) / known_genre_total, 1.0)
    )

  all_lang_nodes = ["ANY", "INTL", *lang_counts.keys()]
  all_genre_nodes = [
    "ANY",
    *macro_g_counts.keys(),
    *(f"sub:{sg}" for sg, cnt in sub_g_counts.items() if cnt >= 6),
  ]
  lang_ic_by_idx = [_calc_ic_lang(ln) for ln in all_lang_nodes]
  genre_ic_by_idx = [_calc_ic_genre(gn) for gn in all_genre_nodes]
  lang_idx_by_node = {ln: idx for idx, ln in enumerate(all_lang_nodes)}
  genre_idx_by_node = {gn: idx for idx, gn in enumerate(all_genre_nodes)}

  leaf_bits: list[float] = []
  leaf_lang_hits: list[tuple[int, ...]] = []
  leaf_lang_known: list[int] = []
  leaf_genre_hits: list[tuple[int, ...]] = []
  leaf_genre_known: list[int] = []

  for ch in leaves:
    lb = 0.0
    if ch.lang_iso3 is None:
      leaf_lang_known.append(0)
      leaf_lang_hits.append(())
    else:
      leaf_lang_known.append(1)
      l_hits = [0]
      if ch.lang_iso3 == "INTL_HINT":
        l_hits.append(1)
        lb += lang_ic_by_idx[1]
      else:
        if ch.lang_iso3 not in major_langs:
          l_hits.append(1)
        if ch.lang_iso3 in lang_idx_by_node:
          c_idx = lang_idx_by_node[ch.lang_iso3]
          l_hits.append(c_idx)
          lb += lang_ic_by_idx[c_idx]
      leaf_lang_hits.append(tuple(l_hits))

    if ch.macro_genre is None:
      leaf_genre_known.append(0)
      leaf_genre_hits.append(())
    else:
      leaf_genre_known.append(1)
      g_hits = [0]
      m_idx = genre_idx_by_node.get(ch.macro_genre)
      if m_idx is not None:
        g_hits.append(m_idx)
      sub_idx = (
        genre_idx_by_node.get(f"sub:{ch.leaf_genre}")
        if ch.leaf_genre is not None
        else None
      )
      if sub_idx is not None:
        g_hits.append(sub_idx)
        lb += genre_ic_by_idx[sub_idx]
      elif m_idx is not None:
        lb += genre_ic_by_idx[m_idx]
      leaf_genre_hits.append(tuple(g_hits))

    leaf_bits.append(lb)

  prefix_leaf_bits: list[float] = [0.0]
  for b in leaf_bits:
    prefix_leaf_bits.append(prefix_leaf_bits[-1] + b)

  header_cost_bits = 1.65 * math.log2(n + 1)
  soft_min_target = min(5, max(1, n // 28))
  max_len = max(12, min(85, n))

  dp: list[float] = [float("inf")] * (n + 1)
  prev: list[int] = [-1] * (n + 1)
  seg_meta: dict[tuple[int, int], tuple[str, str]] = {}
  dp[0] = 0.0
  num_langs = len(all_lang_nodes)
  num_genres = len(all_genre_nodes)

  for j in range(1, n + 1):
    min_i = max(0, j - max_len)
    l_compat = [0] * num_langs
    g_compat = [0] * num_genres
    known_l = 0
    known_g = 0
    for i in range(j - 1, min_i - 1, -1):
      known_l += leaf_lang_known[i]
      for li in leaf_lang_hits[i]:
        l_compat[li] += 1
      known_g += leaf_genre_known[i]
      for gi in leaf_genre_hits[i]:
        g_compat[gi] += 1

      length = j - i
      if n >= 20 and length < 2:
        continue

      best_l = "ANY"
      best_l_bits = 0.0
      if known_l > 0:
        for li in range(1, num_langs):
          compat = l_compat[li]
          if compat == 0 or (compat / known_l) < 0.72:
            continue
          ic = lang_ic_by_idx[li]
          score = compat * ic - (known_l - compat) * 1.4 * ic
          if score > best_l_bits:
            best_l = all_lang_nodes[li]
            best_l_bits = score

      best_g = "ANY"
      best_g_bits = 0.0
      if known_g > 0:
        for gi in range(1, num_genres):
          compat = g_compat[gi]
          if compat == 0 or (compat / known_g) < 0.68:
            continue
          ic = genre_ic_by_idx[gi]
          score = compat * ic - (known_g - compat) * 1.4 * ic
          if score > best_g_bits:
            best_g = all_genre_nodes[gi]
            best_g_bits = score

      # Discourage uninformative genre catch-alls on any major lineup language
      # (>=5%)
      if best_g == "ANY" and (best_l == "ANY" or best_l in major_langs):
        best_g_bits -= 1.5 * length

      retained = best_l_bits + best_g_bits
      raw_bits = prefix_leaf_bits[j] - prefix_leaf_bits[i]
      info_loss = max(0.0, raw_bits - retained)

      short_pen = (
        (soft_min_target - length) * 3.2 if length < soft_min_target else 0.0
      )

      cut_bonus = 0.0
      if i > 0:
        a, b = leaves[i - 1].chno, leaves[i].chno
        if (a // 100) != (b // 100) or (b - a) >= 20:
          cut_bonus = 0.35 * header_cost_bits
        elif (a // 50) != (b // 50) or (b - a) >= 10:
          cut_bonus = 0.15 * header_cost_bits

      cost = dp[i] + header_cost_bits + short_pen + info_loss - cut_bonus
      if cost < dp[j]:
        dp[j] = cost
        prev[j] = i
        seg_meta[(i, j)] = (best_l, best_g)

  def _format_title(ln: str, gn: str) -> str:
    g_str = (
      ""
      if gn == "ANY"
      else (SUBGENRE_DISPLAY[gn[4:]] if gn.startswith("sub:") else gn)
    )
    if ln in ("ANY", default_lang):
      return g_str or "General & Variety"
    if ln == "INTL":
      return (
        f"International — {g_str}" if g_str else "International & Multicultural"
      )
    l_name = LANG_DISPLAY_NAMES.get(ln, ln.upper())
    return f"{l_name} — {g_str}" if g_str else l_name

  raw_spans: list[tuple[int, int, str, str, str]] = []
  cur = n
  while cur > 0:
    p = prev[cur]
    if p < 0:
      p = 0
    ln, gn = seg_meta.get((p, cur), ("ANY", "ANY"))
    raw_spans.append((p, cur, ln, gn, _format_title(ln, gn)))
    cur = p
  raw_spans.reverse()

  # Merge adjacent spans that resolved to the identical formatted title
  merged: list[tuple[int, int, str, str, str]] = []
  for i, j, ln, gn, title in raw_spans:
    if merged and merged[-1][4] == title:
      prev_i, _, prev_ln, prev_gn, _ = merged[-1]
      merged[-1] = (prev_i, j, prev_ln, prev_gn, title)
    else:
      merged.append((i, j, ln, gn, title))

  # Always include the contiguous dial range `(lo–hi)` (refined post-validation
  # at export)
  segments: list[DialSegment] = []
  for i, j, ln, gn, title in merged:
    lo = leaves[i].chno
    hi = leaves[j - 1].chno
    uniq_title = f"{title} ({lo}–{hi})" if lo != hi else f"{title} ({lo})"
    segments.append(
      DialSegment(
        start_idx=i,
        end_idx=j,
        start_chno=lo,
        end_chno=hi,
        lang_node=ln,
        genre_node=gn,
        group_title=uniq_title,
      )
    )

  return segments


def assign_dial_contiguous_groups(
  channels: list[LineupChannel],
  *,
  iptv_org_index: IptvOrgIndex | None = None,
  market: str = "",
  country: str = "CAN",
) -> list[DialSegment]:
  """Sort `channels` by dial number, enrich from `iptv-org`, and assign MDL dial
  groups.
  """
  if not channels:
    return []
  channels.sort(key=lambda c: c.chno)
  leaves = build_channel_leaves_and_enrich(
    channels,
    iptv_org_index=iptv_org_index,
    market=market,
    country=country,
  )
  segments = segment_dial_channels(leaves)
  for seg in segments:
    for idx in range(seg.start_idx, seg.end_idx):
      channels[idx].group = seg.group_title
  return segments
