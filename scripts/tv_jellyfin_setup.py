"""First-run setup of Jellyfin on tism for the TV project (guide Part 4 + libraries).

Idempotent where Jellyfin allows it: re-running skips the wizard once it's complete and
doesn't add a second tuner/guide or duplicate libraries.
"""
import json
import secrets
import sys
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CFG_PATH = Path(__file__).resolve().parent.parent / "config.json"
cfg = json.load(open(CFG_PATH, encoding="utf-8"))
tv = cfg["tv"]
J = tv["jellyfin_url"]
TUNER = "http://192.168.0.57:9191/hdhr/US%20Live"
GUIDE = "http://192.168.0.57:9191/output/epg/US%20Live"
AUTH = 'MediaBrowser Client="Jarvis", Device="jarvisbox", DeviceId="jarvis-tv-setup", Version="1.0"'
h = httpx.Client(base_url=J, timeout=120, headers={"Authorization": AUTH})

if not tv.get("jellyfin_password"):
    tv["jellyfin_user"], tv["jellyfin_password"] = "jack", secrets.token_urlsafe(14)
    CFG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

info = h.get("/System/Info/Public").json()
if not info.get("StartupWizardCompleted"):
    h.post("/Startup/Configuration", json={"UICulture": "en-US", "MetadataCountryCode": "US",
                                           "PreferredMetadataLanguage": "en"}).raise_for_status()
    h.get("/Startup/User").raise_for_status()
    h.post("/Startup/User", json={"Name": tv["jellyfin_user"], "Password": tv["jellyfin_password"]}).raise_for_status()
    h.post("/Startup/RemoteAccess", json={"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False}).raise_for_status()
    h.post("/Startup/Complete").raise_for_status()
    print("wizard complete")

tok = h.post("/Users/AuthenticateByName", json={"Username": tv["jellyfin_user"], "Pw": tv["jellyfin_password"]}).json()
h.headers["Authorization"] = AUTH + f', Token="{tok["AccessToken"]}"'
print("signed in as", tok["User"]["Name"])

# Libraries on the 2 TB drive (NFS: ubuntuserver002 /mnt/TwoTB/media -> tism /mnt/media -> /media)
have = {f["Name"] for f in h.get("/Library/VirtualFolders").json()}
for name, ctype, path in (("Movies", "movies", "/media/movies"), ("TV Shows", "tvshows", "/media/tv"),
                          ("Music", "music", "/media/music")):
    if name not in have:
        h.post("/Library/VirtualFolders", params={"name": name, "collectionType": ctype, "paths": path,
                                                  "refreshLibrary": "false"}, json={"LibraryOptions": {}}).raise_for_status()
        print("library added:", name, path)

# Live TV: tuner + guide + recordings on the big drive
lt = h.get("/System/Configuration/livetv").json()
if not any(t.get("Url") == TUNER for t in lt.get("TunerHosts", [])):
    h.post("/LiveTv/TunerHosts", json={"Type": "hdhomerun", "Url": TUNER, "FriendlyName": "Dispatcharr US Live",
                                       "ImportFavoritesOnly": False, "AllowHWTranscoding": True,
                                       "EnableStreamLooping": False, "TunerCount": 0}).raise_for_status()
    print("tuner added")
if not any(p.get("Path") == GUIDE for p in lt.get("ListingProviders", [])):
    h.post("/LiveTv/ListingProviders", params={"validateListings": "false", "validateLogin": "false"},
           json={"Type": "xmltv", "Path": GUIDE, "EnableAllTuners": True}).raise_for_status()
    print("guide added")
lt = h.get("/System/Configuration/livetv").json()
lt["RecordingPath"] = "/media/recordings"
lt["GuideDays"] = 3
h.post("/System/Configuration/livetv", json=lt).raise_for_status()

# Hardware transcoding: Intel HD 505 via VA-API (verified with vainfo on tism)
enc = h.get("/System/Configuration/encoding").json()
enc.update({"HardwareAccelerationType": "vaapi", "VaapiDevice": "/dev/dri/renderD128",
            "EnableHardwareEncoding": True, "HardwareDecodingCodecs": ["h264", "hevc", "mpeg2video", "vc1", "vp8", "vp9"],
            "EnableDecodingColorDepth10Hevc": True, "EnableTonemapping": False})
h.post("/System/Configuration/encoding", json=enc).raise_for_status()
print("hardware transcoding: vaapi on /dev/dri/renderD128")

# Guide refresh every 12h (guide Part 4), then run it now
tasks = h.get("/ScheduledTasks").json()
guide_task = next(t for t in tasks if t.get("Key") == "RefreshGuide" or "Guide" in t.get("Name", ""))
h.post(f"/ScheduledTasks/{guide_task['Id']}/Triggers",
       json=[{"Type": "IntervalTrigger", "IntervalTicks": 12 * 3600 * 10_000_000}]).raise_for_status()
h.post(f"/ScheduledTasks/Running/{guide_task['Id']}").raise_for_status()
print("guide refresh scheduled every 12h and started now")
