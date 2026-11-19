# Provider Wire Formats & Matcher Scoring Reference

## 1. `ChannelMatcher` Scoring Summary (`src/bellhop/matcher.py`)
- **Hard disqualifiers:** Category banners, `24/7`, `PPV`, `VOD`, `catchup`, `18+`/`XXX`, `audio only` (note: `Adult Swim` and `Radio-Canada` are explicitly whitelisted), per-channel `exclude` phrases, identity baseline `< 40.0`, or total score `< 45.0`.
- **Additive signals:**
  - Manual pin (`pinned_streams`): `1000.0`
  - Exact core alias match: `+65.0` to `+69.0` (`+60.0` phrase match with benign qualifiers; heavy `-15.0` penalty per unrequested spin-off modifier like `2`, `Plus`, `Sci-Fi`, `West`, `Toronto`)
  - Plausible native EPG (`_is_plausible_native_epg`): `+32.0` (exact `tvg_id`) or `+22.0` (verified name/callsign match)
  - Callsign token (`CBFT`, `CFCF`, `WVNY`): `+18.0` (`+56.0` if callsign-only match)
  - Regional alignment: Montréal `+24`, Québec/CA-FR `+16`, Canada `+12`, US `+12`, East TZ `+5`, FR hint `+5` (penalties: Foreign `-45`, UK `-40`, France-for-QC `-28`, unrequested West `-22`, timeshift `+1/+2` `-18`)
  - Quality tier: `4K` `+15`, `FHD/1080p` `+14`, `RAW/60fps` `+11` (or `+4` bonus), `HD` `+10` (category HD `+5.5`), `HEVC` `+2`, `SD` `-8`, `(BK)/Backup` `-10`

## 2. TV Guide Backend Wire Format (`src/bellhop/providers/tvguide.py`)
Base URL: `https://backend.tvguide.com/tvschedules/tvguide` (requires `Referer: https://www.tvguide.com/` header)
- **US ZIP discovery:** `GET /serviceproviders/zipcode/{zip5}/web` → `data.items[]` (`id`, `name`, `city`, `state`, `type`)
- **Country discovery (Canada = `CAN`):** `GET /serviceproviders/country/{ISO3}/web` → `data.items[]` (Canadian postal codes like `H2Y1C6` are NOT accepted on `/zipcode/`; filter `/country/CAN/web` by FSA province/city instead)
- **Provider metadata:** `GET /serviceprovider/{providerId}/web` → `data.item`
- **Channel sources:** `GET /serviceprovider/{providerId}/sources/web` → `data.items[]`:
  `{fullName, name, number, sourceId, networkName, logo}`
- **Official station logo URL:** `https://www.tvguide.com/a/img/catalog` + `item["logo"]`
- **Bell Fibe TV Montréal (Digital) ID:** `TVG-9100000607` (Videotron Montréal Digital: `TVG-9166060733`)

## 3. Schedules Direct SD-JSON Wire Format (`src/bellhop/providers/schedules_direct.py`)
Base URL: `https://json.schedulesdirect.org/20141201`
- **Auth:** `POST /token` with `{"username": "...", "password": "<lowercase_sha1_hex>"}` → returns `{"code": 0, "token": "..."}`. Pass header `token: <token>` on all subsequent requests; re-authenticate on HTTP `401`/`403` or JSON `code` in `{4001, 4003, 4004}`.
- **Discovery:** `GET /headends?country=CAN&postalcode=H2Y1C6` (no spaces in Canadian postal codes).
- **Subscription & Retrieval:** `PUT /lineups/{ID}` (if `code == 2100` and `auto_add_lineup=True`), then `GET /lineups/{ID}` → `{"map": [...], "stations": [...], "metadata": {...}}`.
