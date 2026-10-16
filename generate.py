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
ONDEMAND_REGIONS = ["us"]  # VOD is the slow part (one detail call per movie). Add more if wanted.
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
    for t in timelines:
        cid = t.get("channelId")
        if not cid:
            continue
        for e in sorted(t.get("timelines", []), key=lambda x: x.get("start", "")):
            st, sp = parse_dt(e.get("start")), parse_dt(e.get("stop"))
            if not st or not sp:
                continue
            ep = e.get("episode", {}) or {}
            desc = ep.get("description", "") or ""
            icon = (((ep.get("series", {}) or {}).get("tile", {}) or {}).get("path", "")) or ""
            pr.append(f'<programme start="{xmltv_time(st)}" stop="{xmltv_time(sp)}" channel="{esc(rid(cid, region))}">'
                      f'<title lang="en">{esc(e.get("title", ""))}</title>'
                      + (f'<desc lang="en">{esc(desc)}</desc>' if desc else "")
                      + (f'<icon src="{esc(icon)}"/>' if icon else "") + '</programme>')
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
            m3u.append(f'#EXTINF:-1 tvg-id="{esc(oid)}" tvg-logo="{esc(logo)}" '
                       f'tvg-chno="{chno}" group-title="{esc(catname)}",{v.get("name", "")}\n'
                       f'{playout_url(path, token, stitcher)}')
            ch_meta.append({"id": oid, "name": v.get("name", ""), "number": chno, "logo": logo})
            pr.append(f'<programme start="{start}" stop="{stop}" channel="{esc(oid)}">'
                      f'<title lang="en">{esc(v.get("name", ""))}</title>'
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


def write_epg(path, ch_meta, pr_xml):
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
    with open(os.path.join(OUTDIR, "pluto_all.xml"), "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="AltPluto">\n'
                 + "\n".join(ch) + "\n" + "\n".join(pr) + "\n</tv>\n")
    print(f"==> merged {len(ch)} channels / {len(pr)} programmes into pluto_all")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    if "--merge" in sys.argv:          # VPN workflow calls this after all regions
        merge_from_disk(); return
    force_all = "--all" in sys.argv
    env_region = os.getenv("PLUTO_REGION", "").lower().strip()
    regions = list(REGIONS) if (force_all or not env_region) else [env_region]

    all_m3u, all_ch, all_pr = [], [], []
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
            write_m3u(os.path.join(OUTDIR, f"pluto_{region}.m3u"), m3u)
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


if __name__ == "__main__":
    main()
