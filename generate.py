#!/usr/bin/env python3
"""
AltPluto — self-contained Pluto TV playlist + EPG generator (live + VOD).

Best-of-both of matthuisman's scraper (multi-region + self-hosted EPG + VOD)
and BuddyChewChew's provider (no-VPN playout host + HD web profile).

Writes into OUTDIR:
  pluto_<region>.m3u / .xml            live channels + guide, per region
  pluto_all.m3u / .xml                 merged live, grouped by country
  pluto_ondemand_<region>.m3u / .xml   VOD movies as channels (if enabled)

The workflow gzips the .xml files, so guide URLs end in .xml.gz.
"""

import os
import re
import sys
import html
import json
import requests
from datetime import datetime, timezone, timedelta

# ──────────────────────────────── CONFIG ────────────────────────────────────
# Fixed alt device id — keeps AltPluto separate from your main Pluto repo.
CLIENT_ID = "5d75112c-a468-4689-aaaf-aa74836101d2"

# Raw GitHub base for the guides (used in each .m3u's url-tvg). Trailing slash.
# Includes /output/ because OUTDIR is ./output. Change repo name if needed.
XTVG_BASE = "https://raw.githubusercontent.com/s-digweed/AltPluto/main/output/"

# region code -> an IP in that country (your config.json mapping). Add more here.
REGIONS = {
    "us": "45.50.96.71",  "uk": "51.89.255.67",  "ca": "23.227.38.65",
    "de": "91.107.201.140", "fr": "51.210.0.1",  "es": "82.98.134.10",
    "it": "82.50.39.1",   "mx": "187.174.0.1",   "ar": "181.5.200.1",
    "br": "179.93.224.1", "cl": "186.78.49.1",   "no": "62.50.173.1",
    "se": "80.217.1.1",   "dk": "62.44.180.1",
}

REGION_LABELS = {
    "us": "United States", "uk": "United Kingdom", "ca": "Canada", "de": "Germany",
    "fr": "France", "es": "Spain", "it": "Italy", "mx": "Mexico", "ar": "Argentina",
    "br": "Brazil", "cl": "Chile", "no": "Norway", "se": "Sweden", "dk": "Denmark",
}

OUTDIR      = "output"     # matches your config's ./output
GROUP_BY    = "country"    # "genre" (Pluto categories) or anything else = country label
REGIONALIZE = False        # your setting. True = suffix ids with -<region> (cleaner merged EPG)
WRITE_ALL   = True

# VPN mode: set env PLUTO_VPN=1 (the WireGuard workflow does this). The runner's
# real egress IP is already in-region, so we send NO X-Forwarded-For — the IPs in
# REGIONS are then ignored and only the region CODES/labels are used.
USE_VPN = os.getenv("PLUTO_VPN") == "1"

# ---- VOD / on-demand ----
ENABLE_ONDEMAND  = True
ONDEMAND_REGIONS = ["us", "uk", "ca"]  # each region gets its own pluto_ondemand_<region>.m3u (per-file size limit, not combined)
# ─────────────────────────────────────────────────────────────────────────────

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")

PLAYOUT = "https://ipv4.pluto.tv/api/tn/video/playout/v2"   # no-VPN host
QUALITY = ("&quality=720p&deviceMake=chrome&deviceType=web&deviceModel=web"
           "&deviceVersion=133.0.0&architecture=x86_64&buildVersion=1.0.0"
           "&includeExtendedEvents=true&masterJWTPassthrough=true")

S = requests.Session()
S.headers.update({
    "accept": "*/*", "accept-language": "en-US,en;q=0.9",
    "origin": "https://pluto.tv", "referer": "https://pluto.tv/", "user-agent": UA,
})
TIMEOUT = 30


def auth(token, xff):
    h = {"authorization": f"Bearer {token}"}
    if xff:
        h["X-Forwarded-For"] = xff
    return h


def boot(xff):
    """serverSideAds=false + modern chrome/web profile = HD renditions + a JWT
    that ipv4.pluto.tv honours regardless of viewer IP (no VPN). If a channel
    ever demands DRM, blank drmCapabilities below to ''."""
    params = {
        "appName": "web", "appVersion": "8.1.0", "deviceVersion": "133.0.0",
        "deviceModel": "web", "deviceMake": "chrome", "deviceType": "web",
        "clientID": CLIENT_ID, "clientModelNumber": "1.0.0",
        "channelID": "5a4d3a00ad95e4718ae8d8db", "serverSideAds": "false",
        "constraints": "", "drmCapabilities": "widevine:L3", "blockingMode": "",
        "clientTime": datetime.now(timezone.utc).isoformat(),
    }
    d = S.get("https://boot.pluto.tv/v4/start", params=params,
              headers=({"X-Forwarded-For": xff} if xff else {}), timeout=TIMEOUT).json()
    return d.get("sessionToken", ""), d.get("stitcherParams", "")


def playout_url(path, token, stitcher):
    return f"{PLAYOUT}{path}?{stitcher}&jwt={token}{QUALITY}"


def esc(s):
    return html.escape(str(s or ""), quote=True)


def parse_dt(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def xmltv_time(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


def movie_year(*objs):
    """Release year. Pluto's true release lives at episode.clip.originalReleaseDate
    (ISO datetime); fall back to a couple of plainer fields if ever present."""
    for o in objs:
        if not isinstance(o, dict):
            continue
        cand = (o.get("clip") or {}).get("originalReleaseDate") or o.get("originalReleaseDate")
        # reject the epoch-0 "1970-01-01" placeholder these channels use (not a real year)
        if isinstance(cand, str) and cand[:4].isdigit() and not cand.startswith("1970-01-01"):
            y = int(cand[:4])
            if 1900 < y < 2100:
                return y
        for k in ("year", "releaseYear"):
            v = o.get(k)
            if isinstance(v, int) and 1900 < v < 2100:
                return v
            if isinstance(v, str) and len(v) == 4 and v.isdigit():
                return int(v)
    return None


def add_year(title, yr):
    t = title or ""
    if yr and not re.search(r'\(\d{4}\)\s*$', t):   # don't double up if already there
        return f"{t} ({yr})"
    return t


def is_movie(ep, series):
    stype = (series.get("type") or "").lower()
    if stype in ("film", "movie"):
        return True
    # Virtual channels tag EVERYTHING "live" (movies, series, news), so type is
    # useless here. A movie = long AND no real episode position (S/E 0 or 1).
    # Long TV episodes (e.g. MST3K S7E2, 105min) are excluded by number>1.
    dur_min = (ep.get("duration") or 0) / 60000.0
    s, n = ep.get("season"), ep.get("number")
    return dur_min >= 75 and s in (None, 0, 1) and n in (None, 0, 1)


# ─────────────────────────────── LIVE TV ────────────────────────────────────
def get_categories(token, xff):
    data = S.get("https://service-channels.clusters.pluto.tv/v2/guide/categories",
                 headers=auth(token, xff), timeout=TIMEOUT).json().get("data", [])
    cat = {}
    for c in data:
        for cid in c.get("channelIDs", []):
            cat[cid] = c.get("name", "Pluto TV")
    return cat


def get_channels(token, xff):
    data = S.get("https://service-channels.clusters.pluto.tv/v2/guide/channels",
                 params={"offset": "0", "limit": "1000", "sort": "number:asc"},
                 headers=auth(token, xff), timeout=TIMEOUT).json().get("data", [])
    cats = get_categories(token, xff)
    out = []
    for c in data:
        cid, name = c.get("id"), c.get("name")
        if not cid or not name:
            continue
        imgs = c.get("images", []) or []
        logo = next((i.get("url") for i in imgs if i.get("type") == "colorLogoPNG"), None) \
            or (imgs[0].get("url") if imgs else "")
        out.append({"id": cid, "name": name, "number": c.get("number", 0),
                    "logo": logo or "", "group": cats.get(cid, "Pluto TV")})
    return out


def get_timelines(token, xff, channel_ids):
    """Rolling ~28h window (now-1h .. now+27h), 4h slices, 30 channels/call."""
    data = []
    for offset in range(-1, 24, 4):
        d = datetime.now(timezone.utc) + timedelta(hours=offset)
        start = d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for i in range(0, len(channel_ids), 30):
            try:
                r = S.get("https://service-channels.clusters.pluto.tv/v2/guide/timelines",
                          params={"start": start, "duration": "240",
                                  "channelIds": ",".join(channel_ids[i:i + 30])},
                          headers=auth(token, xff), timeout=TIMEOUT)
                data.extend(r.json().get("data", []))
            except Exception as e:
                print(f"    WARN timeline chunk: {e}")
    return data


def rid(cid, region):
    return f"{cid}-{region}" if REGIONALIZE else cid


def build_region(region, xff):
    print(f"==> live {region}")
    token, stitcher = boot(xff)
    if not token:
        print(f"    skip {region}: no token"); return None
    channels = get_channels(token, xff)
    if not channels:
        print(f"    skip {region}: no channels"); return None
    print(f"    {len(channels)} channels")
    timelines = get_timelines(token, xff, [c["id"] for c in channels])

    # Flag channels whose S/E is a BOGUS constant (e.g. CBC news stamped S01 E01 on
    # every show). A channel is placeholder only if ALL its non-movie episodes share
    # one (season,number) across >=3 entries; channels with varied real S/E are kept.
    _se, _cnt = {}, {}
    for t in timelines:
        cid0 = t.get("channelId")
        if not cid0:
            continue
        for e in t.get("timelines", []):
            ep0 = e.get("episode", {}) or {}
            if is_movie(ep0, ep0.get("series", {}) or {}):
                continue
            s0, n0 = ep0.get("season"), ep0.get("number")
            if isinstance(s0, int) and isinstance(n0, int) and s0 > 0 and n0 > 0:
                _se.setdefault(cid0, set()).add((s0, n0))
                _cnt[cid0] = _cnt.get(cid0, 0) + 1
    placeholder_cids = {c0 for c0 in _se if len(_se[c0]) == 1 and _cnt[c0] >= 3}

    m3u, ch_meta = [], []
    for c in channels:
        grp = c["group"] if GROUP_BY == "genre" else REGION_LABELS.get(region, region.upper())
        path = f"/stitch/hls/channel/{c['id']}/master.m3u8"
        m3u.append(f'#EXTINF:-1 tvg-id="{esc(rid(c["id"], region))}" tvg-logo="{esc(c["logo"])}" '
                   f'tvg-chno="{c["number"]}" group-title="{esc(grp)}",{c["name"]}\n'
                   f'{playout_url(path, token, stitcher)}')
        ch_meta.append({"id": rid(c["id"], region), "name": c["name"],
                        "number": c["number"], "logo": c["logo"]})

    pr = []
    # The timeline is fetched in 4h slices, so the programme straddling each slice
    # boundary is returned TWICE. Keep one per (channel, start) — duplicates break
    # IPTVBoss (its export goes blank past the first duplicate) even though TiviMate
    # tolerates them when reading the raw XML.
    seen_starts = set()
    for t in timelines:
        cid = t.get("channelId")
        if not cid:
            continue
        for e in sorted(t.get("timelines", []), key=lambda x: x.get("start", "")):
            st, sp = parse_dt(e.get("start")), parse_dt(e.get("stop"))
            if not st or not sp:
                continue
            if (cid, st) in seen_starts:
                continue
            seen_starts.add((cid, st))
            ep = e.get("episode", {}) or {}
            series = ep.get("series", {}) or {}
            title = series.get("name") or e.get("title", "")
            subtitle = ep.get("name", "") or ""
            season, number = ep.get("season"), ep.get("number")
            desc = ep.get("description", "") or ""
            icon = ((series.get("tile", {}) or {}).get("path", "")) or ""
            p = f'<programme start="{xmltv_time(st)}" stop="{xmltv_time(sp)}" channel="{esc(rid(cid, region))}">'
            if is_movie(ep, series):
                # movies: title + year, no sub-title / episode-num
                p += f'<title lang="en">{esc(add_year(title, movie_year(ep, series)))}</title>'
            else:
                p += f'<title lang="en">{esc(title)}</title>'
                if subtitle and subtitle != title:
                    p += f'<sub-title lang="en">{esc(subtitle)}</sub-title>'
                if (isinstance(season, int) and isinstance(number, int) and season > 0
                        and number > 0 and cid not in placeholder_cids):
                    p += f'<episode-num system="onscreen">S{season:02d} E{number:02d}</episode-num>'
                    p += f'<episode-num system="xmltv_ns">{season - 1}.{number - 1}.</episode-num>'
            if desc:
                p += f'<desc lang="en">{esc(desc)}</desc>'
            if icon:
                p += f'<icon src="{esc(icon)}"/>'
            pr.append(p + '</programme>')
    print(f"    {len(pr)} programmes")
    return m3u, ch_meta, pr


# ─────────────────────────────── VOD / ON-DEMAND ────────────────────────────
# NOTE: VOD no-VPN is best-effort — BuddyChewChew never did VOD, so the ipv4
# playout host for VOD paths is unverified. Test one movie before trusting it.
def vod_categories(token, xff):
    return S.get("https://service-vod.clusters.pluto.tv/v4/vod/categories",
                 params={"includeItems": "false", "offset": "1000", "page": "1", "sort": "number:asc"},
                 headers=auth(token, xff), timeout=TIMEOUT).json().get("categories", [])


def vod_items(token, xff, cat_id):
    return S.get(f"https://service-vod.clusters.pluto.tv/v4/vod/categories/{cat_id}/items",
                 params={"offset": "0", "page": "1"}, headers=auth(token, xff), timeout=TIMEOUT).json()


def vod_item(token, xff, item_id):
    d = S.get("https://service-vod.clusters.pluto.tv/v4/vod/items",
              params={"ids": item_id}, headers=auth(token, xff), timeout=TIMEOUT).json()
    return d[0] if isinstance(d, list) and d else {}


def build_ondemand(region, xff):
    print(f"==> VOD {region}")
    token, stitcher = boot(xff)
    if not token:
        print(f"    skip VOD {region}: no token"); return None
    cats = vod_categories(token, xff)
    if not cats:
        print(f"    skip VOD {region}: no categories"); return None

    start = xmltv_time(datetime.now(timezone.utc) - timedelta(days=1))
    stop = xmltv_time(datetime.now(timezone.utc) + timedelta(days=1))
    chno = 9000
    m3u, ch_meta, pr = [], [], []

    for c in cats:
        if not c:
            continue
        catname = c.get("name", "Movies")
        try:
            items = vod_items(token, xff, c["_id"]).get("items", [])
        except Exception as e:
            print(f"    WARN VOD cat {catname}: {e}"); continue
        for it in items:
            if it.get("type") != "movie":
                continue
            iid = it.get("_id")
            if not iid:
                continue
            try:
                v = vod_item(token, xff, iid)
            except Exception:
                continue
            if not v:
                continue
            stitched = v.get("stitched", {}) or {}
            path = stitched.get("path")
            if not path:
                hls = next((p for p in (stitched.get("paths", []) or []) if p.get("type") == "hls"), None)
                path = hls.get("path") if hls else None
            if not path:
                continue
            logo = (v.get("featuredImage", {}) or {}).get("path", "") or ""
            oid = rid(iid, region)
            vname = add_year(v.get("name", ""), movie_year(v, v.get("series", {}) or {}))  # VOD = movies
            m3u.append(f'#EXTINF:-1 tvg-id="{esc(oid)}" tvg-logo="{esc(logo)}" '
                       f'tvg-chno="{chno}" group-title="{esc(catname)}",{vname}\n'
                       f'{playout_url(path, token, stitcher)}')
            ch_meta.append({"id": oid, "name": vname, "number": chno, "logo": logo})
            pr.append(f'<programme start="{start}" stop="{stop}" channel="{esc(oid)}">'
                      f'<title lang="en">{esc(vname)}</title>'
                      + (f'<desc lang="en">{esc(v.get("description", ""))}</desc>' if v.get("description") else "")
                      + (f'<icon src="{esc(logo)}"/>' if logo else "") + '</programme>')
            chno += 1
    print(f"    {len(ch_meta)} VOD items")
    return m3u, ch_meta, pr


# ─────────────────────────────── WRITERS ────────────────────────────────────
def write_m3u(path, body_lines):
    header = f'#EXTM3U url-tvg="{XTVG_BASE}{os.path.basename(path)[:-4]}.xml.gz"\n'
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(body_lines) + "\n")


def _channel_xml(m):
    return (f'<channel id="{esc(m["id"])}"><display-name>{esc(m["name"])}</display-name>'
            f'<lcn>{m["number"]}</lcn>'
            + (f'<icon src="{esc(m["logo"])}"/>' if m["logo"] else "") + '</channel>')


_PR_KEY = re.compile(r'<programme start="([^"]*)"[^>]*channel="([^"]*)"')


def dedupe_programmes(pr_xml):
    """Keep the first programme per (channel, start). Duplicates come from 4h-slice
    seams and from the same channel id appearing in several regions; IPTVBoss
    chokes on them (export goes blank past the first duplicate)."""
    seen, out = set(), []
    for p in pr_xml:
        m = _PR_KEY.search(p)
        key = (m.group(2), m.group(1)) if m else p
        if key in seen:
            continue
        seen.add(key); out.append(p)
    return out


def write_epg(path, ch_meta, pr_xml):
    pr_xml = dedupe_programmes(pr_xml)
    seen, ch_xml = set(), []
    for m in ch_meta:                       # dedupe channel defs by id
        if m["id"] in seen:
            continue
        seen.add(m["id"]); ch_xml.append(_channel_xml(m))
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="AltPluto">\n'
                + "\n".join(ch_xml) + "\n" + "\n".join(pr_xml) + "\n</tv>\n")


def merge_from_disk():
    """VPN mode runs one region per process, so build pluto_all by reading the
    per-region files back off disk (regions in REGIONS order)."""
    all_m3u = []
    for region in REGIONS:
        f = os.path.join(OUTDIR, f"pluto_{region}.m3u")
        if not os.path.exists(f):
            continue
        label = REGION_LABELS.get(region, region.upper())
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line.startswith("#EXTINF"):
                    all_m3u.append(re.sub(r'group-title="[^"]*"', f'group-title="{esc(label)}"', line))
                elif line and not line.startswith("#EXTM3U"):
                    all_m3u.append(line)
    write_m3u(os.path.join(OUTDIR, "pluto_all.m3u"), all_m3u)

    seen, ch, pr = set(), [], []
    for region in REGIONS:
        f = os.path.join(OUTDIR, f"pluto_{region}.xml")
        if not os.path.exists(f):
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line.startswith("<channel "):
                    m = re.search(r'id="([^"]*)"', line)
                    key = m.group(1) if m else line
                    if key in seen:
                        continue
                    seen.add(key); ch.append(line)
                elif line.startswith("<programme "):
                    pr.append(line)
    pr = dedupe_programmes(pr)
    with open(os.path.join(OUTDIR, "pluto_all.xml"), "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="AltPluto">\n'
                 + "\n".join(ch) + "\n" + "\n".join(pr) + "\n</tv>\n")
    print(f"==> merged {len(ch)} channels / {len(pr)} programmes into pluto_all")


# ── updates.log — rolling 7-day, per-country channel changes ─────────────────
# Before a region's M3U is overwritten, last run's copy (already in the repo) is
# compared with the new one by tvg-id: new id = Added, missing id = Removed, same
# id with a different name = Renamed. Changes accumulate per DAY in
# updates_state.json (several runs on one day merge into one date), days older than
# UPDATES_DAYS are pruned, and Updates.log is re-rendered newest day first.
UPDATES_LOG   = "updates.log"
UPDATES_STATE = "updates_state.json"
UPDATES_DAYS  = 7
_EXTINF = re.compile(r'^#EXTINF:-?\d+((?:\s+[\w-]+="[^"]*")*)\s*,(.*)$')


def m3u_names(lines):
    """{tvg-id: channel name} from M3U lines (a line may carry '\\n' + URL)."""
    out = {}
    for chunk in lines:
        for line in chunk.split("\n"):
            m = _EXTINF.match(line.strip())
            if not m:
                continue
            tid = re.search(r'tvg-id="([^"]*)"', m.group(1))
            name = html.unescape(m.group(2)).strip()
            if tid and tid.group(1):
                out[tid.group(1)] = name
    return out


def read_m3u_names(path):
    try:
        with open(path, encoding="utf-8") as f:
            return m3u_names(f.read().splitlines())
    except FileNotFoundError:
        return None


def diff_region(old, new):
    added   = [new[i] for i in new.keys() - old.keys()]
    removed = [old[i] for i in old.keys() - new.keys()]
    renamed = [[old[i], new[i]] for i in old.keys() & new.keys() if old[i] != new[i]]
    return added, removed, renamed


def _merge(lst, items):
    for it in items:
        if it not in lst:
            lst.append(it)


def render_updates_log(state):
    order = {r: n for n, r in enumerate(REGIONS)}
    out = [f"AltPluto channel changes - last {UPDATES_DAYS} days (newest first)",
           "Times in UTC. A date only appears if something changed that day.", ""]
    if not state:
        out.append("No channel changes in the last week.")
    for day in sorted(state, reverse=True):
        d = datetime.strptime(day, "%Y-%m-%d").strftime("%m/%d/%Y")
        out += ["=" * 40, d, "=" * 40, ""]
        for region in sorted(state[day], key=lambda r: order.get(r, 99)):
            rec = state[day][region]
            out.append(region.upper())
            for w in rec.get("warn", []):
                out.append(f"!! {w}")
            for note in rec.get("note", []):
                out.append(note)
            sections = []
            if rec.get("added"):
                sections.append(["Added"] + [f"+ {n}" for n in sorted(rec["added"], key=str.lower)])
            if rec.get("removed"):
                sections.append(["Removed"] + [f"- {n}" for n in sorted(rec["removed"], key=str.lower)])
            if rec.get("renamed"):
                sections.append(["Renamed"] + [f"~ {a} to {b}" for a, b in
                                               sorted(rec["renamed"], key=lambda x: x[0].lower())])
            for sec in sections:
                out.append("")
                out += sec
            out.append("")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_updates_log(changes, now=None):
    """changes: list of (region, old_names_or_None, new_names)."""
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    try:
        with open(UPDATES_STATE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    logged = 0
    for region, old, new in changes:
        if old is None:
            rec = state.setdefault(today, {}).setdefault(region, {})
            _merge(rec.setdefault("note", []),
                   [f"First snapshot ({len(new)} channels) - nothing to compare yet"])
            logged += 1
            continue
        added, removed, renamed = diff_region(old, new)
        if not (added or removed or renamed):
            continue
        rec = state.setdefault(today, {}).setdefault(region, {})
        if old and len(removed) > 0.5 * len(old):
            _merge(rec.setdefault("warn", []),
                   [f"{now:%H:%M} run: {len(removed)} of {len(old)} channels missing - "
                    "possibly a partial fetch, check the Actions log"])
        _merge(rec.setdefault("added", []), added)
        _merge(rec.setdefault("removed", []), removed)
        _merge(rec.setdefault("renamed", []), renamed)
        logged += 1
    cutoff = (now - timedelta(days=UPDATES_DAYS - 1)).strftime("%Y-%m-%d")
    state = {d: v for d, v in state.items() if d >= cutoff and v}
    with open(UPDATES_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    with open(UPDATES_LOG, "w", encoding="utf-8") as f:
        f.write(render_updates_log(state))
    print(f"==> Updates.log: {logged} region(s) changed this run; "
          f"{len(state)} day(s) in the 7-day window")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    if "--merge" in sys.argv:          # VPN workflow calls this after all regions
        merge_from_disk(); return
    force_all = "--all" in sys.argv
    env_region = os.getenv("PLUTO_REGION", "").lower().strip()
    regions = list(REGIONS) if (force_all or not env_region) else [env_region]

    all_m3u, all_ch, all_pr = [], [], []
    changes = []
    for region in regions:
        xff = None if USE_VPN else REGIONS.get(region)
        if not USE_VPN and not xff:
            print(f"!! no IP for region '{region}'"); continue
        try:
            res = build_region(region, xff)
        except Exception as e:
            print(f"    ERROR live {region}: {e}"); res = None
        if res:
            m3u, ch_meta, pr = res
            m3u_path = os.path.join(OUTDIR, f"pluto_{region}.m3u")
            changes.append((region, read_m3u_names(m3u_path), m3u_names(m3u)))
            write_m3u(m3u_path, m3u)
            write_epg(os.path.join(OUTDIR, f"pluto_{region}.xml"), ch_meta, pr)
            label = REGION_LABELS.get(region, region.upper())
            for line in m3u:
                if line.startswith("#EXTINF"):
                    line = re.sub(r'group-title="[^"]*"', f'group-title="{esc(label)}"', line)
                all_m3u.append(line)
            all_ch.extend(ch_meta); all_pr.extend(pr)

        if ENABLE_ONDEMAND and region in ONDEMAND_REGIONS:
            try:
                vres = build_ondemand(region, xff)
            except Exception as e:
                print(f"    ERROR VOD {region}: {e}"); vres = None
            if vres:
                vm, vch, vpr = vres
                write_m3u(os.path.join(OUTDIR, f"pluto_ondemand_{region}.m3u"), vm)
                write_epg(os.path.join(OUTDIR, f"pluto_ondemand_{region}.xml"), vch, vpr)

    if WRITE_ALL and len(regions) > 1:
        write_m3u(os.path.join(OUTDIR, "pluto_all.m3u"), all_m3u)
        write_epg(os.path.join(OUTDIR, "pluto_all.xml"), all_ch, all_pr)
        print("==> wrote pluto_all.m3u / pluto_all.xml")

    write_updates_log(changes)


if __name__ == "__main__":
    main()
