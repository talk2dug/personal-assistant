#!/usr/bin/env python3
"""EAS / SAME alert watcher for the NOAA weather stream on jarvishackrf2.

NOAA Weather Radio precedes every warning/watch with a SAME digital header (AFSK burst,
sent three times) that names the event type, the counties, and the expiry. Decoding that
header is deterministic and instant -- no transcription, no LLM -- so it is the primary
"severe weather alert" signal. Speech transcription (done on the Jarvis box) fills in the
narrative afterwards.

Pipeline:  Icecast /weather.mp3  ->  ffmpeg (22.05 kHz s16le)  ->  multimon-ng -a EAS
           -> this script parses "EAS: ZCZC-..." lines -> JSON lines in EVENTS_PATH.

Validated 2026-09-18 with a synthesized SAME burst pushed through the exact mp3 chain
(12 kHz / 32 kbps) -- decodes cleanly, so listening to the mount (not the raw dongle) is
fine and keeps this independent of the rtl_fm service.

Jarvis reads EVENTS_PATH over SSH (tail) on its tick. The header is repeated three times
per alert; identical headers within DEDUPE_WINDOW collapse to one event.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

STREAM_URL = os.environ.get("EAS_STREAM_URL", "http://127.0.0.1:8000/weather.mp3")
EVENTS_PATH = os.environ.get("EAS_EVENTS_PATH", "/home/pi/fm_node/eas_events.jsonl")
# Counties Jack cares about (SAME/FIPS, 6 digits). Richmond city, Henrico, Chesterfield,
# Hanover. Anything else still gets logged, just with affects_me=false.
MY_FIPS = set(filter(None, os.environ.get(
    "EAS_MY_FIPS", "051760,051087,051041,051085").split(",")))
DEDUPE_WINDOW = 300     # seconds; the same header repeats 3x within a few seconds
RESPAWN_BACKOFF = 5

EVENT_NAMES = {
    "TOR": "Tornado Warning", "TOA": "Tornado Watch",
    "SVR": "Severe Thunderstorm Warning", "SVA": "Severe Thunderstorm Watch",
    "FFW": "Flash Flood Warning", "FFA": "Flash Flood Watch", "FFS": "Flash Flood Statement",
    "FLW": "Flood Warning", "FLA": "Flood Watch", "FLS": "Flood Statement",
    "HWW": "High Wind Warning", "HWA": "High Wind Watch",
    "WSW": "Winter Storm Warning", "WSA": "Winter Storm Watch", "BZW": "Blizzard Warning",
    "HUW": "Hurricane Warning", "HUA": "Hurricane Watch", "TRW": "Tropical Storm Warning",
    "TRA": "Tropical Storm Watch", "SSW": "Storm Surge Warning", "SSA": "Storm Surge Watch",
    "EWW": "Extreme Wind Warning", "SMW": "Special Marine Warning",
    "SPS": "Special Weather Statement", "SVS": "Severe Weather Statement",
    "CFW": "Coastal Flood Warning", "CFA": "Coastal Flood Watch",
    "DSW": "Dust Storm Warning", "FRW": "Fire Warning", "HMW": "Hazardous Materials Warning",
    "CAE": "Child Abduction Emergency", "CDW": "Civil Danger Warning",
    "CEM": "Civil Emergency Message", "EQW": "Earthquake Warning", "EVI": "Evacuation Immediate",
    "LEW": "Law Enforcement Warning", "LAE": "Local Area Emergency",
    "NUW": "Nuclear Power Plant Warning", "RHW": "Radiological Hazard Warning",
    "SPW": "Shelter In Place Warning", "VOW": "Volcano Warning",
    "EAN": "Emergency Action Notification", "NIC": "National Information Center",
    "NPT": "National Periodic Test", "RMT": "Required Monthly Test", "RWT": "Required Weekly Test",
    "DMO": "Practice/Demo Warning", "ADR": "Administrative Message",
}
# Warnings are urgent, watches/advisories are notice, tests are info.
SEVERITY = {
    **{k: "urgent" for k in ("TOR", "SVR", "FFW", "FLW", "HWW", "WSW", "BZW", "HUW", "TRW",
                              "SSW", "EWW", "SMW", "DSW", "FRW", "HMW", "CAE", "CDW", "CEM",
                              "EQW", "EVI", "LEW", "NUW", "RHW", "SPW", "VOW", "EAN")},
    **{k: "notice" for k in ("TOA", "SVA", "FFA", "FLA", "HWA", "WSA", "HUA", "TRA", "SSA",
                              "CFW", "CFA", "SPS", "SVS", "FFS", "FLS", "LAE")},
    **{k: "info" for k in ("NPT", "RMT", "RWT", "DMO", "ADR", "NIC")},
}
FIPS_NAMES = {
    "051760": "Richmond city", "051087": "Henrico", "051041": "Chesterfield", "051085": "Hanover",
    "051036": "Charles City", "051127": "New Kent", "051145": "Powhatan", "051075": "Goochland",
    "051053": "Dinwiddie", "051149": "Prince George", "051570": "Colonial Heights",
    "051670": "Hopewell", "051730": "Petersburg", "051007": "Amelia", "051049": "Cumberland",
    "051109": "Louisa", "051033": "Caroline", "051097": "King William", "051101": "King and Queen",
    "051183": "Sussex", "051181": "Surry", "051135": "Nottoway", "051111": "Lunenburg",
}

SAME_RE = re.compile(r"ZCZC-(?P<org>[A-Z]{3})-(?P<event>[A-Z]{3})-(?P<areas>[0-9-]+?)\+"
                     r"(?P<purge>\d{4})-(?P<issued>\d{7})-(?P<sender>[^-]+)-")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"{now_iso()} {msg}", flush=True)


def parse_same(header: str) -> dict | None:
    m = SAME_RE.search(header)
    if not m:
        return None
    areas = [a for a in m.group("areas").split("-") if a]
    ev = m.group("event")
    purge = m.group("purge")
    issued = m.group("issued")
    return {
        "originator": m.group("org"), "event_code": ev,
        "event": EVENT_NAMES.get(ev, ev), "severity": SEVERITY.get(ev, "notice"),
        "areas": areas,
        "area_names": [FIPS_NAMES.get(a[-6:] if len(a) >= 6 else a, a) for a in areas],
        "affects_me": any((a[-6:] in MY_FIPS) for a in areas),
        "purge_hhmm": f"{purge[:2]}:{purge[2:]}",
        "issued_utc": f"day {issued[:3]} {issued[3:5]}:{issued[5:]}Z",
        "sender": m.group("sender"), "raw": header,
    }


def append_event(ev: dict) -> None:
    os.makedirs(os.path.dirname(EVENTS_PATH), exist_ok=True)
    with open(EVENTS_PATH, "a") as f:
        f.write(json.dumps(ev, separators=(",", ":")) + "\n")


def run() -> None:
    log(f"eas_watch starting: stream={STREAM_URL} events={EVENTS_PATH} my_fips={sorted(MY_FIPS)}")
    recent: dict[str, float] = {}
    while True:
        # The reconnect flags are HTTP-protocol options; ffmpeg rejects them on a file
        # input, which is what the self-test (and any replay) feeds in.
        net = (["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
               if STREAM_URL.startswith("http") else [])
        ff = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *net,
             "-i", STREAM_URL, "-f", "s16le", "-ar", "22050", "-ac", "1", "-"],
            stdout=subprocess.PIPE)
        mm = subprocess.Popen(["multimon-ng", "-t", "raw", "-a", "EAS", "-"],
                              stdin=ff.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, bufsize=1)
        ff.stdout.close()
        try:
            for line in mm.stdout:
                line = line.strip()
                if not line.startswith("EAS:"):
                    continue
                header = line[4:].strip()
                t = time.time()
                if header == "NNNN":
                    log("EAS end-of-message")
                    continue
                if t - recent.get(header, 0) < DEDUPE_WINDOW:
                    continue
                recent[header] = t
                for k in [k for k, v in recent.items() if t - v > DEDUPE_WINDOW]:
                    del recent[k]
                parsed = parse_same(header) or {"raw": header, "event": "unparsed",
                                                "severity": "notice", "affects_me": True}
                parsed["at"] = now_iso()
                append_event(parsed)
                log(f"EAS {parsed.get('event_code', '?')} {parsed['event']} "
                    f"areas={parsed.get('area_names')} affects_me={parsed['affects_me']}")
        finally:
            for p in (mm, ff):
                try:
                    p.kill()
                except Exception:
                    pass
        log(f"decoder pipeline ended (ffmpeg {ff.poll()}, multimon {mm.poll()}); "
            f"respawning in {RESPAWN_BACKOFF}s")
        time.sleep(RESPAWN_BACKOFF)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--parse":      # self-test: --parse "<header>"
        print(json.dumps(parse_same(sys.argv[2]), indent=1))
        sys.exit(0)
    try:
        run()
    except KeyboardInterrupt:
        pass
