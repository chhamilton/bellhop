"""Shared normalization, SD/HD pairing, category/language inference, and alias builder for dynamic lineups."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bellhop.dial_grouping import assign_dial_contiguous_groups
from bellhop.iptv_org import IptvOrgIndex, fold_latin_accents
from bellhop.lineup_loader import (
  _load_official_logo_index,
  is_bell_fibe_lineup,
)
from bellhop.models import LineupChannel

PAREN_GROUP_RE = re.compile(r"\(([^()]+)\)")
TRAILING_NOISE_RE = re.compile(
  r"(?:\bChannel\s+\d+\b|\bNational\s+Schedule\b|\bNetwork\s+Feed\b|\bSchedule\b|\bAffiliate\b)",
  re.IGNORECASE,
)
HD_SUFFIX_RE = re.compile(
  r"(?:\s+(?:HD|FHD|4K|UHD|DT)|\s*-?\s*HD)$", re.IGNORECASE
)
CALLSIGN_HD_SUFFIX_RE = re.compile(r"(?:HD|-HD|-DT|DT)$", re.IGNORECASE)
CALLSIGN_DIR_SUFFIX_RE = re.compile(r"-(?:E|W|C|F|P|TV|DT|HD)$", re.IGNORECASE)
INITIALISM_SKIP_WORDS = frozenset(
  {"the", "of", "de", "du", "des", "la", "le", "les", "l", "d", "and", "et"}
)
WORD_SINGLE_DIGIT_RE = re.compile(r"^([A-Za-z]{5,})\s+([1-9])$")
LOCAL_CALLSIGN_CITY_RE = re.compile(
  r"^([A-Z0-9-]{3,8})\s+([A-Za-zÀ-ÿ .'-]+),\s*([A-Za-zÀ-ÿ .'-]+?)(?:\s*\(([^()]+)\))?$"
)

COUNTRY_PARENS = frozenset(
  {
    "canada",
    "usa",
    "us",
    "u.s.",
    "united states",
    "uk",
    "france",
    "international",
    "english",
    "french",
    "east",
    "west",
    "pacific",
    "atlantic",
    "central",
    "mountain",
    "hd",
    "sd",
  }
)

INITIALISM_END_NOUNS = frozenset(
  {
    "network",
    "television",
    "channel",
    "broadcasting",
    "legislature",
    "parliament",
    "corporation",
    "entertainment",
    "movies",
  }
)

FRENCH_KEYWORDS = frozenset(
  {
    "radio-canada",
    "ici",
    "tva",
    "noovo",
    "rds",
    "lcn",
    "rdi",
    "tele-quebec",
    "telequebec",
    "unis",
    "tv5",
    "meteo media",
    "meteomedia",
    "super ecran",
    "cinemama",
    "cinépop",
    "cinepop",
    "canal vie",
    "canal d",
    "zeste",
    "evasion",
    "historia",
    "series plus",
    "series+",
    "addik",
    "illico",
    "elle fictions",
    "temoin",
    "prisme",
    "teletoon fr",
    "télé-mag",
    "tele-mag",
    "matv",
    "savoir",
    "assemblee nationale",
    "cpac fr",
    "ami-tele",
    "canal m",
    "musiqueplus",
    "france 2",
    "france 3",
    "france 24",
    "tv5monde",
    "bfm",
    "tf1",
    "m6",
    "planete+",
    "mewa",
    "cbft",
    "cfjp",
    "cftm",
    "civm",
    "cftu",
  }
)

CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
  (
    "Music & Audio",
    (
      "stingray",
      "music choice",
      "mtv classic",
      "mtv live",
      "much",
      "cmt",
      "trace",
      "mezzo",
      "melody",
      "audio",
      "radio",
      "canal m",
      "ami-audio",
    ),
  ),
  (
    "Sports",
    (
      "tsn",
      "sportsnet",
      "rds",
      "tva sports",
      "espn",
      "fs1",
      "fs2",
      "fox sports",
      "cbs sports",
      "nbc sports",
      "golf",
      "nfl",
      "nba",
      "nhl",
      "mlb",
      "tennis",
      "bein",
      "dazn",
      "fubo",
      "fight",
      "wwe",
      "rev tv",
      "one soccer",
      "onesoccer",
      "fan duel",
      "fanduel",
      "bally",
      "msg",
      "nesn",
      "yes network",
      "marqueesports",
      "centre ice",
      "league pass",
      "sunday ticket",
      "eurosport",
    ),
  ),
  (
    "Movies & Premium",
    (
      "crave",
      "hbo",
      "starz",
      "super ecran",
      "cinepop",
      "cinemama",
      "showtime",
      "cinemax",
      "tmc",
      "tcm",
      "turner classic",
      "amc",
      "fxm",
      "fx movie",
      "sundance",
      "ifc",
      "silver screen",
      "hollywood suite",
      "movie",
      "cinema",
      "mgm",
      "epix",
      "encore",
      "paramount+ with showtime",
      "frissons",
    ),
  ),
  (
    "News & Information",
    (
      "news",
      "nouvelles",
      "cnn",
      "HLN",
      "msnbc",
      "fox business",
      "cnbc",
      "bloomberg",
      "bbc news",
      "bbc world",
      "euronews",
      "france 24",
      "al jazeera",
      "lcn",
      "rdi",
      "cp24",
      "cpac",
      "c-span",
      "cspan",
      "weather",
      "meteo",
      "bnn",
      "assemblee",
      "parliament",
      "ntd",
      "i24",
      "dw ",
      "deutsche welle",
      "nhk",
    ),
  ),
  (
    "Kids & Family",
    (
      "cartoon",
      "disney",
      "nickelodeon",
      "nick ",
      "nickjr",
      "nicktoons",
      "teennick",
      "ytv",
      "treehouse",
      "teletoon",
      "family",
      "wildbrain",
      "boomerang",
      "baby",
      "pbs kids",
      "universal kids",
      "tiji",
      "gulli",
      "yoopa",
    ),
  ),
  (
    "Science & History",
    (
      "discovery",
      "history",
      "historia",
      "nat geo",
      "national geographic",
      "science",
      "smithsonian",
      "animal planet",
      "love nature",
      "bbc earth",
      "curiosity",
      "nasa",
      "documentary",
      "planete",
      "explora",
      "savoir",
    ),
  ),
  (
    "Lifestyle & Entertainment",
    (
      "hgtv",
      "food",
      "bravo",
      "slice",
      "tlc",
      "lifetime",
      "a&e",
      "usa network",
      "tnt",
      "tbs",
      "tru tv",
      "trutv",
      "fx",
      "fxx",
      "syfy",
      "sci-fi",
      "comedy",
      "e!",
      "bet",
      "vh1",
      "mtv",
      "paramount network",
      "own",
      "oxygen",
      "we tv",
      "magnolia",
      "cooking",
      "travel",
      "evasion",
      "zeste",
      "canal vie",
      "canal d",
      "series",
      "addik",
      "elle ",
      "game show",
      "gsn",
      "hallmark",
      "tv land",
      "me tv",
      "metv",
      "grit",
      "ion",
      "bounce",
      "cottage",
      "makeful",
      "dtour",
      "dejaview",
      "gusto",
      "ctv drama",
      "ctv comedy",
      "ctv life",
    ),
  ),
]


def _strip_accents(text: str) -> str:
  return fold_latin_accents(text)


def _norm_key(text: str) -> str:
  s = _strip_accents(text).lower()
  return re.sub(r"[^a-z0-9]+", "", s)


@dataclass(slots=True)
class RawProviderSlot:
  """Intermediate channel slot extracted from an upstream lineup backend."""

  chno: int
  raw_name: str
  callsign: str | None = None
  network_name: str | None = None
  station_id: str | None = None
  logo_url: str | None = None
  language: str | None = None
  country: str = "CAN"
  is_hd_hint: bool | None = None


@dataclass(slots=True)
class _ParsedSlot:
  slot: RawProviderSlot
  clean_name: str
  base_identity_key: str
  base_callsign: str
  is_hd: bool
  language: str
  group: str
  regions: list[str]
  aliases: list[str] = field(default_factory=list)


def _clean_station_title(
  raw_name: str,
  callsign: str | None,
  network_name: str | None,
) -> tuple[str, list[str], bool]:
  """Return (clean_display_name, candidate_aliases, is_hd) from raw provider strings."""
  text = raw_name.strip()
  aliases: list[str] = []

  is_hd = bool(
    re.search(r"\b(?:HD|FHD|4K|UHD)\b", text, re.IGNORECASE)
    or (callsign and CALLSIGN_HD_SUFFIX_RE.search(callsign))
  )

  # Check local affiliate pattern: "CBFT Montreal, Quebec (Radio-Canada)"
  m_local = LOCAL_CALLSIGN_CITY_RE.match(text)
  if m_local:
    cs, city, _prov, net = m_local.groups()
    city_clean = city.strip()
    net_clean = (net or network_name or "").strip()
    if net_clean and net_clean.lower() not in COUNTRY_PARENS:
      display = f"{net_clean} ({city_clean})"
      aliases.extend(
        [
          f"{net_clean} {city_clean}",
          display,
          net_clean,
          f"{cs} {city_clean}",
          cs,
        ]
      )
      return display, _dedupe_strs(aliases), is_hd

  # Extract parenthetical hints (e.g. "(Canada)" -> drop; "(Tele-Mag)" -> keep as alias)
  parens = PAREN_GROUP_RE.findall(text)
  useful_parens = [
    p.strip() for p in parens if p.strip().lower() not in COUNTRY_PARENS
  ]
  stripped = PAREN_GROUP_RE.sub("", text).strip()
  stripped = TRAILING_NOISE_RE.sub("", stripped).strip(" -–—/")
  stripped_no_hd = HD_SUFFIX_RE.sub("", stripped).strip(" -–—/")

  display = stripped_no_hd or stripped or text
  if useful_parens and useful_parens[0].lower() not in display.lower():
    aliases.append(useful_parens[0])
    aliases.append(f"{useful_parens[0]} {display}")

  aliases.append(display)
  if stripped and stripped != display:
    aliases.append(stripped)
  if network_name and network_name.strip():
    aliases.append(network_name.strip())
  aliases.extend(_extract_initialisms(display))
  if callsign:
    cs_clean = callsign.strip()
    cs_base = CALLSIGN_HD_SUFFIX_RE.sub("", cs_clean).strip("-")
    cs_root = CALLSIGN_DIR_SUFFIX_RE.sub("", cs_base).strip("-")
    if len(cs_base) >= 3:
      aliases.append(cs_base)
    if cs_root != cs_base and len(cs_root) >= 3:
      aliases.append(cs_root)
    if cs_clean != cs_base and len(cs_clean) >= 3:
      aliases.append(cs_clean)

  return display, _dedupe_strs(aliases), is_hd


def _extract_initialisms(title: str) -> list[str]:
  """Derive 3–5 letter multi-word initialisms (`APTN`) and `<Word><Digit>` (`H2`) aliases."""
  folded = _strip_accents(title)
  words = [
    w
    for w in re.findall(r"[A-Za-z0-9]+", folded)
    if w.lower() not in INITIALISM_SKIP_WORDS
  ]
  out: list[str] = []
  if (
    3 <= len(words) <= 6
    and all(w[0].isalpha() for w in words)
    and words[-1].lower() in INITIALISM_END_NOUNS
  ):
    acronym = "".join(w[0] for w in words).upper()
    if 3 <= len(acronym) <= 5:
      out.append(acronym)
  m_wd = WORD_SINGLE_DIGIT_RE.match(folded.strip())
  if m_wd:
    out.append(f"{m_wd.group(1)[0].upper()}{m_wd.group(2)}")
  return out


def _dedupe_strs(items: list[str]) -> list[str]:
  out: list[str] = []
  seen: set[str] = set()
  for it in items:
    cleaned = " ".join(it.split()).strip()
    if not cleaned:
      continue
    k = cleaned.casefold()
    if k not in seen:
      seen.add(k)
      out.append(cleaned)
  return out


def _infer_language(
  display_name: str, callsign: str | None, explicit_lang: str | None
) -> str:
  if explicit_lang:
    low = explicit_lang.lower()
    if low.startswith("fr"):
      return "fr"
    if low.startswith("en"):
      return "en"
  hay = _strip_accents(f"{display_name} {callsign or ''}").lower()
  if any(kw in hay for kw in FRENCH_KEYWORDS):
    return "fr"
  return "en"


def _infer_group(display_name: str, callsign: str | None, language: str) -> str:
  hay = _strip_accents(f"{display_name} {callsign or ''}").lower()
  for group_name, keywords in CATEGORY_RULES:
    if any(kw.lower() in hay for kw in keywords):
      return group_name
  if language == "fr":
    return "Francophone"
  return "Local & Networks"


_EXPLICIT_NAME_METROS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
  ("montreal", "montreal", ("montreal", "qc")),
  ("quebec city", "quebeccity", ("qc",)),
  ("toronto", "toronto", ("toronto", "on")),
  ("ottawa", "ottawa", ("ottawa", "on")),
  ("vancouver", "vancouver", ("vancouver", "bc", "west")),
  ("calgary", "calgary", ("calgary", "ab", "west")),
  ("edmonton", "edmonton", ("edmonton", "ab", "west")),
  ("winnipeg", "winnipeg", ("winnipeg", "mb")),
  ("halifax", "halifax", ("halifax", "atlantic")),
  ("moncton", "moncton", ("moncton", "atlantic")),
  ("new york", "newyork", ("newyork", "ny")),
  ("los angeles", "losangeles", ("losangeles", "us_ca", "west")),
  ("chicago", "chicago", ("chicago", "il")),
)

_CA_FSA_REGION_MAP: dict[str, tuple[str, ...]] = {
  "A": ("atlantic",),
  "B": ("halifax", "atlantic"),
  "C": ("atlantic",),
  "E": ("atlantic",),
  "G": ("qc",),
  "H": ("montreal", "qc"),
  "J": ("qc",),
  "K": ("on",),
  "L": ("on",),
  "M": ("toronto", "on"),
  "N": ("on",),
  "P": ("on",),
  "R": ("winnipeg", "mb"),
  "S": ("sk",),
  "T": ("ab", "west"),
  "V": ("vancouver", "bc", "west"),
  "X": ("west",),
  "Y": ("west",),
}

_US_WEST_STATE_CODES = frozenset(
  {
    "AK",
    "AZ",
    "CA",
    "CO",
    "HI",
    "ID",
    "MT",
    "NM",
    "NV",
    "OR",
    "UT",
    "WA",
    "WY",
  }
)


def _append_unique(items: list[str], *values: str) -> None:
  for val in values:
    if val and val not in items:
      items.append(val)


def _infer_regions(
  display_name: str,
  country: str,
  postal_code: str,
  language: str,
  market: str = "",
) -> list[str]:
  regions: list[str] = []
  hay = _strip_accents(display_name).lower()
  market_norm = _strip_accents(market).lower()
  postal_clean = re.sub(r"\s+", "", postal_code.strip().upper())
  country_up = country.strip().upper()
  is_canada = country_up in {"CAN", "CA"}
  is_usa = country_up in {"USA", "US"}

  # 1. Explicit metro/timezone qualifiers in the channel's own display name
  explicit_metro = False
  for needle, _metro_id, implied in _EXPLICIT_NAME_METROS:
    if needle in hay:
      explicit_metro = True
      _append_unique(regions, *implied)
  if re.search(r"\b(?:west|pacific)\b", hay):
    explicit_metro = True
    _append_unique(regions, "west")
  elif re.search(r"\b(?:east|eastern)\b", hay):
    explicit_metro = True

  # 2. Headend postal-code & market inference (when channel isn't an out-of-market feed)
  if not explicit_metro:
    if is_canada:
      fsa_first = (
        postal_clean[:1] if postal_clean and postal_clean[0].isalpha() else ""
      )
      if fsa_first in _CA_FSA_REGION_MAP:
        _append_unique(regions, *_CA_FSA_REGION_MAP[fsa_first])
      if postal_clean[:2] in {"K1", "K2"} or "ottawa" in market_norm:
        _append_unique(regions, "ottawa", "on")
      if "montreal" in market_norm:
        _append_unique(regions, "montreal", "qc")
      elif "toronto" in market_norm:
        _append_unique(regions, "toronto", "on")
      elif "vancouver" in market_norm:
        _append_unique(regions, "vancouver", "bc", "west")
      elif "calgary" in market_norm:
        _append_unique(regions, "calgary", "ab", "west")
      elif "edmonton" in market_norm:
        _append_unique(regions, "edmonton", "ab", "west")
      elif "winnipeg" in market_norm:
        _append_unique(regions, "winnipeg", "mb")
      elif "halifax" in market_norm:
        _append_unique(regions, "halifax", "atlantic")

      state_token = (
        market_norm.rsplit(",", 1)[-1].strip().upper()
        if "," in market_norm
        else ""
      )
      if state_token == "QC":
        _append_unique(regions, "qc")
      elif state_token == "ON":
        _append_unique(regions, "on")
      elif state_token == "BC":
        _append_unique(regions, "bc", "west")
      elif state_token == "AB":
        _append_unique(regions, "ab", "west")
    elif is_usa:
      zip3 = (
        postal_clean[:3]
        if len(postal_clean) >= 5 and postal_clean[:5].isdigit()
        else ""
      )
      state_token = (
        market_norm.rsplit(",", 1)[-1].strip().upper()
        if "," in market_norm
        else ""
      )
      if (
        "new york" in market_norm
        or "manhattan" in market_norm
        or zip3 in {"100", "101", "102", "103", "104", "110", "111", "112"}
      ):
        _append_unique(regions, "newyork", "ny")
      elif "los angeles" in market_norm or (
        zip3.isdigit() and 900 <= int(zip3) <= 918
      ):
        _append_unique(regions, "losangeles", "us_ca", "west")
      elif "chicago" in market_norm or zip3 in {"606", "607", "608"}:
        _append_unique(regions, "chicago", "il")

      if state_token == "NY":
        _append_unique(regions, "ny")
      elif state_token == "CA":
        _append_unique(regions, "us_ca", "west")
      elif state_token == "IL":
        _append_unique(regions, "il")

      if state_token in _US_WEST_STATE_CODES or (
        zip3.isdigit() and int(zip3) >= 800
      ):
        _append_unique(regions, "west")

  # French-Canadian networks carry Quebec affinity even when market metadata is minimal
  if is_canada and (language == "fr" or "quebec" in hay):
    _append_unique(regions, "qc")

  if is_canada:
    _append_unique(regions, "ca", "us")
  elif is_usa:
    _append_unique(regions, "us")
  else:
    _append_unique(regions, country.lower()[:2])
  return regions


def normalize_provider_slots(
  raw_slots: list[RawProviderSlot],
  *,
  postal_code: str = "",
  market: str = "",
  provider_name: str = "",
  lineup_id: str = "",
  excluded_channels: set[int] | None = None,
  pair_sd_hd: bool = True,
  iptv_org_index: IptvOrgIndex | None = None,
) -> list[LineupChannel]:
  """Convert raw provider slots into deduplicated/paired `LineupChannel` objects."""
  excluded = excluded_channels or set()
  official_logos_by_chno = (
    _load_official_logo_index()
    if is_bell_fibe_lineup(lineup_id=lineup_id, provider=provider_name)
    else {}
  )

  parsed_list: list[_ParsedSlot] = []
  for rs in raw_slots:
    if rs.chno <= 0 or rs.chno in excluded:
      continue
    display, aliases, detected_hd = _clean_station_title(
      rs.raw_name,
      rs.callsign,
      rs.network_name,
    )
    is_hd = (
      rs.is_hd_hint
      if rs.is_hd_hint is not None
      else (detected_hd or rs.chno >= 1000)
    )
    cs_base = (
      CALLSIGN_HD_SUFFIX_RE.sub("", (rs.callsign or "").strip())
      .strip("-")
      .upper()
    )
    base_id = _norm_key(cs_base) if len(cs_base) >= 3 else _norm_key(display)
    lang = _infer_language(display, rs.callsign, rs.language)
    grp = _infer_group(display, rs.callsign, lang)
    regs = _infer_regions(display, rs.country, postal_code, lang, market=market)

    parsed_list.append(
      _ParsedSlot(
        slot=rs,
        clean_name=display,
        base_identity_key=base_id,
        base_callsign=cs_base,
        is_hd=is_hd,
        language=lang,
        group=grp,
        regions=regs,
        aliases=aliases,
      )
    )

  parsed_list.sort(key=lambda p: p.slot.chno)

  # Pair SD (<1000) and HD (>=1000 or explicit HD) slots representing the same station
  by_chno: dict[int, _ParsedSlot] = {p.slot.chno: p for p in parsed_list}
  consumed_sd_chnos: set[int] = set()
  sd_for_hd: dict[int, int] = {}

  if pair_sd_hd:
    # Pass 1: Classic +1000 offset pairing (Bell Fibe 101 <-> 1101, Telus, etc.)
    for p in parsed_list:
      if p.slot.chno >= 1000:
        cand_sd = p.slot.chno - 1000
        sd_slot = by_chno.get(cand_sd)
        if (
          sd_slot is not None
          and cand_sd not in consumed_sd_chnos
          and (
            sd_slot.base_identity_key == p.base_identity_key
            or (
              sd_slot.slot.logo_url and sd_slot.slot.logo_url == p.slot.logo_url
            )
            or (
              sd_slot.slot.station_id
              and sd_slot.slot.station_id == p.slot.station_id
            )
          )
        ):
          consumed_sd_chnos.add(cand_sd)
          sd_for_hd[p.slot.chno] = cand_sd

    # Pass 2: Same base_callsign / station_id pairing for non-1000 cable systems (e.g. Ch 28 SD <-> Ch 528 HD)
    unpaired_sd_by_key: dict[str, _ParsedSlot] = {}
    for p in parsed_list:
      if (
        p.slot.chno not in consumed_sd_chnos
        and not p.is_hd
        and p.base_identity_key
      ):
        unpaired_sd_by_key.setdefault(p.base_identity_key, p)

    for p in parsed_list:
      if p.slot.chno in sd_for_hd or p.slot.chno in consumed_sd_chnos:
        continue
      if p.is_hd and p.base_identity_key in unpaired_sd_by_key:
        sd_match = unpaired_sd_by_key.pop(p.base_identity_key)
        if sd_match.slot.chno != p.slot.chno:
          consumed_sd_chnos.add(sd_match.slot.chno)
          sd_for_hd[p.slot.chno] = sd_match.slot.chno

  channels: list[LineupChannel] = []
  for p in parsed_list:
    chno = p.slot.chno
    if chno in consumed_sd_chnos:
      continue

    sd_no = sd_for_hd.get(chno)
    merged_aliases = list(p.aliases)
    if sd_no is not None and sd_no in by_chno:
      merged_aliases = _dedupe_strs(merged_aliases + by_chno[sd_no].aliases)

    cs = p.base_callsign or (
      p.slot.callsign.strip() if p.slot.callsign else None
    )
    country_tld = "ca" if p.slot.country.upper() in {"CAN", "CA"} else "us"
    tvg_id = f"{cs}.{country_tld}" if cs else None
    logo = official_logos_by_chno.get(chno) or p.slot.logo_url or None

    channels.append(
      LineupChannel(
        chno=chno,
        sd_chno=sd_no,
        name=p.clean_name,
        group=p.group,
        language=p.language,
        callsign=cs,
        tvg_id=tvg_id,
        logo=logo,
        gracenote_station_id=p.slot.station_id,
        regions=p.regions,
        aliases=merged_aliases,
        exclude=[],
      )
    )

  primary_country = parsed_list[0].slot.country if parsed_list else "CAN"
  assign_dial_contiguous_groups(
    channels,
    iptv_org_index=iptv_org_index,
    market=market,
    country=primary_country,
  )

  # Infer automatic sibling exclusions (e.g., "RDS" excludes "2" and "info" if "RDS2" / "RDS Info" exist)
  populate_sibling_exclusions(channels)
  channels.sort(key=lambda c: c.chno)
  return channels


def _split_alnum_tokens(text: str) -> tuple[str, ...]:
  raw_toks = re.findall(r"[a-z0-9]+", _strip_accents(text).lower())
  out: list[str] = []
  for tok in raw_toks:
    m = re.match(r"^([a-z]{2,})(\d+)$", tok)
    if m:
      out.append(m.group(1))
      out.append(m.group(2))
    else:
      out.append(tok)
  return tuple(
    t for t in out if t not in {"hd", "fhd", "sd", "4k", "uhd", "dt"}
  )


def populate_sibling_exclusions(channels: list[LineupChannel]) -> None:
  """Populate sibling exclusion phrases (`RDS` vs `RDS 2`) across `channels`."""
  per_channel_phrases: list[tuple[LineupChannel, set[tuple[str, ...]]]] = []
  for ch in channels:
    phrases: set[tuple[str, ...]] = set()
    for candidate_str in [ch.name, ch.callsign or "", *ch.aliases]:
      toks = _split_alnum_tokens(candidate_str)
      if toks:
        phrases.add(toks)
    per_channel_phrases.append((ch, phrases))

  ignorable_suffix_starts = {
    "ca",
    "us",
    "east",
    "west",
    "pacific",
    "montreal",
    "quebec",
    "toronto",
    "ontario",
    "ottawa",
    "vancouver",
    "bc",
    "calgary",
    "edmonton",
    "alberta",
    "winnipeg",
    "halifax",
    "atlantic",
    "canada",
    "tv",
    "channel",
  }

  for ch, my_phrases in per_channel_phrases:
    exclusions: set[str] = set()
    for my_toks in my_phrases:
      n = len(my_toks)
      for other_ch, other_phrases in per_channel_phrases:
        if other_ch.chno == ch.chno:
          continue
        for other_toks in other_phrases:
          if len(other_toks) <= n or other_toks in my_phrases:
            continue
          if (
            other_toks[:n] == my_toks
            and other_toks[n] not in ignorable_suffix_starts
          ):
            exclusions.add(" ".join(other_toks))
    if exclusions:
      ch.exclude = sorted(exclusions)
