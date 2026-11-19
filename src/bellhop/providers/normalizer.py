"""Shared normalization, SD/HD pairing, category/language inference, and alias builder for dynamic lineups."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from bellhop.lineup_loader import _load_official_logo_index
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
  norm = unicodedata.normalize("NFKD", text)
  return "".join(ch for ch in norm if not unicodedata.combining(ch))


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
  if callsign:
    cs_clean = callsign.strip()
    cs_base = CALLSIGN_HD_SUFFIX_RE.sub("", cs_clean).strip("-")
    if len(cs_base) >= 3:
      aliases.append(cs_base)
    if cs_clean != cs_base and len(cs_clean) >= 3:
      aliases.append(cs_clean)

  return display, _dedupe_strs(aliases), is_hd


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


def _infer_regions(
  display_name: str, country: str, postal_code: str, language: str
) -> list[str]:
  regions: list[str] = []
  hay = _strip_accents(display_name).lower()
  postal_up = postal_code.strip().upper()

  if ("montreal" in hay or postal_up.startswith("H")) and (
    language == "fr" or "montreal" in hay
  ):
    regions.append("montreal")
  if (
    (language == "fr" or postal_up[:1] in {"G", "H", "J"} or "quebec" in hay)
    and "qc" not in regions
    and country.upper() in {"CAN", "CA"}
  ):
    regions.append("qc")
  if country.upper() in {"CAN", "CA"}:
    regions.append("ca")
    regions.append("us")
  elif country.upper() in {"USA", "US"}:
    regions.append("us")
  else:
    regions.append(country.lower()[:2])
  return regions


def normalize_provider_slots(
  raw_slots: list[RawProviderSlot],
  *,
  postal_code: str = "",
  excluded_channels: set[int] | None = None,
  pair_sd_hd: bool = True,
) -> list[LineupChannel]:
  """Convert raw provider slots into deduplicated/paired `LineupChannel` objects."""
  excluded = excluded_channels or set()
  official_logos_by_chno = _load_official_logo_index()

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
    regs = _infer_regions(display, rs.country, postal_code, lang)

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

  # Infer automatic sibling exclusions (e.g., "RDS" excludes "2" and "info" if "RDS2" / "RDS Info" exist)
  _populate_sibling_exclusions(channels)
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


def _populate_sibling_exclusions(channels: list[LineupChannel]) -> None:
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
    "montreal",
    "quebec",
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
