# AltPluto

Self-contained Pluto TV playlist + EPG generator (live + VOD). One Python file,
no framework. Best-of-both of matthuisman's scraper (multi-region, self-hosted
EPG, VOD) and BuddyChewChew's provider (no-VPN playout host + HD web profile).

## Output (into `output/`)
- `pluto_<region>.m3u` + `pluto_<region>.xml` — live channels + guide, per region
- `pluto_all.m3u` + `pluto_all.xml` — merged live, grouped by country
- `pluto_ondemand_<region>.m3u` + `.xml` — VOD movies as channels (enabled regions)
- The workflow gzips every `.xml`, so **guide URLs end in `.xml.gz`**

## Point TiviMate / IPTVBoss at
- Playlist: `…/output/pluto_all.m3u` (or a single region / the ondemand file)
- EPG: embedded via `url-tvg` in each playlist — nothing to set manually.
  Direct if needed: `…/output/pluto_all.xml.gz`

## Config knobs (top of `generate.py`)
| Knob | What it does |
|------|--------------|
| `CLIENT_ID` | Fixed **alt** device id — keeps AltPluto separate from your main repo |
| `XTVG_BASE` | Raw GitHub base (incl. `/output/`); change if the repo isn't `s-digweed/AltPluto` |
| `REGIONS` | `code: IP` map (your config's 14). Add more — one in-country IP each |
| `GROUP_BY` | `genre` = Pluto categories, anything else = country label (your `"country"`) |
| `REGIONALIZE` | `False` = your setting. `True` suffixes ids with `-<region>` (cleaner merged EPG) |
| `ENABLE_ONDEMAND` | VOD on/off |
| `ONDEMAND_REGIONS` | Which regions get VOD. Default `["us"]` — VOD is the slow part |

## One-time setup
1. Add repo secret **`KEEPALIVE_PAT`** = fine-grained PAT (Contents: Read/write)
   so auto-commits count as activity and GitHub never disables the schedule.
2. Run **Actions → Auto AltPluto Update → Run workflow**. Then it's every 6h.

## If a stream won't load (DRM)
In `boot()`, change `"drmCapabilities": "widevine:L3"` → `"drmCapabilities": ""`.

## Caveats / single points of failure
- **VOD no-VPN is best-effort** — BuddyChewChew never did VOD, so the `ipv4.pluto.tv`
  host for VOD paths is unverified. **Test one movie before trusting it**; if VOD
  won't play, set `ENABLE_ONDEMAND = False`.
- Streams play off the hardcoded `ipv4.pluto.tv` host — if Pluto retires it, update `PLAYOUT`.
- `REGIONS` IPs can go stale (freezing / wrong region) — swap for another IP in that country.
- `quality=720p` is a hint; a channel whose source is lower won't upscale.
- VOD across many regions is slow (one detail call per movie). Keep `ONDEMAND_REGIONS` small.
