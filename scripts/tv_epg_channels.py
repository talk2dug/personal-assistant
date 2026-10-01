"""Runs ON tism: build /opt/iptv-epg/our.channels.xml -- the iptv-org/epg grabber entries
for exactly the channels in our Dispatcharr lineup (guide "proper path", step 2: a trimmed
channel list matching active streams).

stdin: JSON list of tvg_ids from Dispatcharr ("00sReplay.us@SD").
"""
import glob
import json
import sys
import xml.etree.ElementTree as ET

# When a channel is listed on several sites, prefer ones that carry full schedules and
# have held up in the iptv-org project (FAST platforms first: that's what this lineup is).
PREFERRED = ["pluto.tv", "plex.tv", "samsungtvplus.com", "tvpassport.com", "tvguide.com",
             "directv.com", "xumo.tv", "distro.tv", "freevee.com", "tvtv.us"]

ids = json.load(sys.stdin)
want = {}
for i in ids:
    want[i.lower()] = i
    want[i.split("@")[0].lower()] = i

found = {}
for path in glob.glob("/opt/iptv-epg/epg/sites/*/*.channels.xml"):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        continue
    for ch in root.iter("channel"):
        xid = (ch.get("xmltv_id") or "").strip()
        ours = want.get(xid.lower()) or want.get(xid.split("@")[0].lower())
        if not ours:
            continue
        site = ch.get("site") or ""
        rank = PREFERRED.index(site) if site in PREFERRED else len(PREFERRED)
        if ours not in found or rank < found[ours][0]:
            found[ours] = (rank, site, ch.get("lang") or "en", ch.get("site_id") or "", ch.text or "")

out = ET.Element("channels")
for ours, (_r, site, lang, site_id, name) in sorted(found.items()):
    e = ET.SubElement(out, "channel", site=site, lang=lang, xmltv_id=ours, site_id=site_id)
    e.text = name
ET.ElementTree(out).write("/opt/iptv-epg/our.channels.xml", encoding="utf-8", xml_declaration=True)
sites = {}
for _r, site, *_ in found.values():
    sites[site] = sites.get(site, 0) + 1
print(json.dumps({"ids": len(ids), "matched": len(found),
                  "sites": sorted(sites.items(), key=lambda kv: -kv[1])[:12]}))
