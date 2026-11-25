"""Regional, broadcast, quality, and regex vocabularies for `ChannelMatcher`."""

from __future__ import annotations

import re

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
    "the",
    "le",
    "la",
    "les",
    "l",
    "channel",
    "network",
    "tv",
    "tele",
    "television",
    "canal",
    "chaine",
    "live",
    "local",
    "national",
    "main",
    "primary",
    "r",
    "&",
  }
)

BROADCAST_WRAPPER_TOKENS = frozenset(
  {
    "canal",
    "chaine",
    "tele",
    "television",
    "tv",
    "channel",
    "network",
    "fr",
    "francais",
    "french",
    "qc",
    "quebec",
    "ca",
    "canada",
    "en",
    "english",
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
    "+",
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
    "international",
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
    "moncton",
    "acadie",
    "saguenay",
    "mauricie",
    "estrie",
    "gatineau",
    "manitoba",
    "saskatchewan",
    "yukon",
    "quebeccity",
    "sherbrooke",
  }
)

# Mainstream umbrella networks that operate separate "<Brand> News" or
# "<Brand> International" sister channels (never collapse into one channel)
MAJOR_SPINOFF_UMBRELLA_BRANDS = frozenset(
  {
    "abc",
    "nbc",
    "cbs",
    "fox",
    "pbs",
    "cw",
    "cbc",
    "ctv",
    "global",
    "tva",
    "bbc",
    "sky",
    "cnn",
    "cnbc",
    "msnbc",
    "tsn",
    "rds",
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

CA_METRO_MARKERS: dict[str, frozenset[str]] = {
  "montreal": MONTREAL_MARKERS,
  "toronto": frozenset({"toronto", "gta", "cfto", "cblt", "ciii", "cp24"}),
  "ottawa": frozenset({"ottawa", "cjoh", "cbco", "cbot", "chot"}),
  "vancouver": frozenset({"vancouver", "cbut", "chan", "civt"}),
  "calgary": frozenset({"calgary", "cfcn", "cbrt", "cict"}),
  "edmonton": frozenset({"edmonton", "cfrn", "cbxt", "citv"}),
  "winnipeg": frozenset({"winnipeg", "cbwt", "cknd", "cjay"}),
  "halifax": frozenset({"halifax", "cjch", "cbht"}),
}

US_METRO_MARKERS: dict[str, frozenset[str]] = {
  "newyork": frozenset(
    {"newyork", "nyc", "manhattan", "wnbc", "wcbs", "wabc", "wnyw", "wpix"}
  ),
  "losangeles": frozenset(
    {"losangeles", "knbc", "kcbs", "kabc", "kttv", "ktla"}
  ),
  "chicago": frozenset({"chicago", "wmaq", "wbbm", "wls", "wgn", "wfld"}),
}

CA_PROVINCE_MARKERS: dict[str, frozenset[str]] = {
  "qc": QUEBEC_MARKERS,
  "on": frozenset({"ontario"}),
  "bc": frozenset({"bc", "britishcolumbia"}),
  "ab": frozenset({"alberta"}),
  "mb": frozenset({"manitoba"}),
  "sk": frozenset({"saskatchewan"}),
  "atlantic": frozenset({"atlantic", "maritimes", "novascotia"}),
}

US_STATE_MARKERS: dict[str, frozenset[str]] = {
  "ny": frozenset({"ny", "newyork"}),
  "us_ca": frozenset({"california", "socal", "norcal"}),
  "il": frozenset({"illinois"}),
}

# Target-region keys mapped to benign qualifier tokens allowed in stream names
TARGET_REGION_QUALIFIER_TOKENS: dict[str, frozenset[str]] = {
  "montreal": frozenset({"montreal", "mtl"}),
  "qc": frozenset({"qc", "quebec"}),
  "toronto": frozenset({"toronto", "gta", "ontario", "on"}),
  "ottawa": frozenset({"ottawa", "ontario", "on"}),
  "on": frozenset({"ontario", "on"}),
  "vancouver": frozenset({"vancouver", "bc", "west", "pacific"}),
  "bc": frozenset({"bc", "west", "pacific"}),
  "calgary": frozenset({"calgary", "alberta", "ab", "west"}),
  "edmonton": frozenset({"edmonton", "alberta", "ab", "west"}),
  "ab": frozenset({"alberta", "ab", "west"}),
  "winnipeg": frozenset({"winnipeg", "manitoba", "mb"}),
  "halifax": frozenset({"halifax", "atlantic"}),
  "moncton": frozenset({"moncton", "acadie", "atlantic"}),
  "atlantic": frozenset({"atlantic", "moncton", "acadie"}),
  "newyork": frozenset({"new", "york", "nyc", "ny"}),
  "ny": frozenset({"ny", "new", "york"}),
  "losangeles": frozenset({"los", "angeles", "la", "west", "pacific"}),
  "us_ca": frozenset({"california", "west", "pacific"}),
  "chicago": frozenset({"chicago", "il", "illinois", "central"}),
  "west": frozenset({"west", "pacific"}),
}

CANADA_PRIMARY_REGIONS = (
  frozenset(CA_METRO_MARKERS) | frozenset(CA_PROVINCE_MARKERS) | {"ca"}
)
US_PRIMARY_REGIONS = (
  frozenset(US_METRO_MARKERS) | frozenset(US_STATE_MARKERS) | {"us"}
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
GLUED_HD_SUFFIX_RE = re.compile(r"^([a-z]{4,})(fhd|uhd|hd)$")
DECADE_OR_NUM_RE = re.compile(r"^\d+s?$")
OTA_CALLSIGN_RE = re.compile(r"^(?:[cwk][a-z]{3}|[wk][a-z]{2})$")
US_OTA_CALLSIGN_RE = re.compile(r"^[wk][a-z]{3}$")
PAREN_CITY_RE = re.compile(r"\(([^()]+)\)")
CALLSIGN_ROOT_RE = re.compile(r"-(?:e|w|c|f|p|tv|dt|hd)$", re.IGNORECASE)
E_EXCLAMATION_RE = re.compile(r"(?<![a-z0-9])e!(?![a-z0-9])", re.IGNORECASE)
INFIX_PLUS_RE = re.compile(r"(?<=[a-z0-9])\s+\+\s+(?=[a-z0-9])", re.IGNORECASE)
CARDINAL_WORD_MAP: dict[str, str] = {
  "one": "1",
  "two": "2",
  "three": "3",
  "four": "4",
  "five": "5",
}
CARDINAL_AFTER_BRAND_RE = re.compile(
  r"(?<=[a-z0-9])\s+\b(one|two|three|four|five)\b",
  re.IGNORECASE,
)
CODEC_CLEAN_RE = re.compile(
  r"\b(?:h\.?26[45]|x26[45]|hevc|mpeg-?[24]|1080[pi]|720p|2160p|540p|480[pi]|[356]0\s*fps|5\.1)\b",
  re.IGNORECASE,
)
