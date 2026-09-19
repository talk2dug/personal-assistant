"""Chat tools over the radio-awareness tables (core/radio.py).

Read-only, always on, not keyword-gated: "what's it like outside", "anything on the
scanner", "who's been hanging around the street" name no reliable keyword, and four
schemas are nowhere near the tool-count budget. The tables exist unconditionally (both
entrypoints init them), so these never fail on a missing table -- they report a stale
feed instead, which is the honest answer when the radio worker is not running.
"""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import radio

RADIO_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather_conditions",
            "description": (
                "Current local weather conditions as read out on NOAA Weather Radio "
                "(Richmond, 162.475 MHz) and transcribed here -- temperature, humidity, wind, "
                "pressure, sky, any hazards mentioned, the short forecast, plus any ACTIVE EAS "
                "warning or watch decoded from the same station. Use this for 'what's it like "
                "outside', 'any storms coming', 'is there a warning'. Reports if the feed is stale."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scanner_activity",
            "description": (
                "What has come over the Richmond Fire/EMS scanner recently: the transmissions "
                "flagged as worth knowing (fires, serious crashes, hazmat, anything near home) "
                "and a count of everything else heard. Use for 'anything on the scanner', "
                "'what's going on out there', 'was that sirens for something'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer",
                                "description": "How far back to look (default 120)."},
                    "include_all": {"type": "boolean",
                                    "description": "Also return the raw recent transcripts, "
                                                   "not just the flagged ones (default false)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_rf_surroundings",
            "description": (
                "The RF sensor node's picture of the street: which transmitters are the house's "
                "own sensors, which are neighbours' fixtures, how many vehicles pass, and any "
                "unfamiliar vehicle that keeps coming back (a 'check that car out' watch). Use "
                "for 'anything unusual around the house', 'who's been on the street', 'is that "
                "car back again', 'how busy is the road'."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_radio_alerts",
            "description": (
                "Everything the radio watch has flagged recently across all sources -- EAS "
                "weather alerts, flagged scanner calls, RF vehicle/new-device notices -- newest "
                "first, with whether each was already pushed to the owner."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "Look-back window (default 24)."},
                },
            },
        },
    },
]

RADIO_TOOL_NAMES = {t["function"]["name"] for t in RADIO_TOOLS}

RADIO_SYSTEM_NOTE = (
    " You also have a radio watch: get_weather_conditions gives the current conditions and "
    "any active warning exactly as NOAA Weather Radio is broadcasting them locally, "
    "get_scanner_activity gives what the Fire/EMS scanner has picked up, get_rf_surroundings "
    "gives the sensor node's view of the street (own sensors, neighbours, passing and "
    "recurring vehicles), and list_radio_alerts lists everything flagged. These come from "
    "radios in the house, not the internet. If a tool says a feed is stale, say the radio "
    "feed is down rather than describing conditions from it."
)


def _local(iso: str | None, tz_name: str) -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%a %H:%M")


def handle(db_path: str, name: str, arguments: dict, tz_name: str = "America/New_York") -> str:
    arguments = arguments or {}
    if name == "get_weather_conditions":
        return json.dumps(_weather(db_path, tz_name))
    if name == "get_scanner_activity":
        return json.dumps(_scanner(db_path, tz_name, int(arguments.get("minutes") or 120),
                                   bool(arguments.get("include_all"))))
    if name == "get_rf_surroundings":
        return json.dumps(_rf(db_path, tz_name))
    if name == "list_radio_alerts":
        return json.dumps(_alerts(db_path, tz_name, float(arguments.get("hours") or 24)))
    return json.dumps({"error": f"unknown radio tool {name!r}"})


def _weather(db_path: str, tz_name: str) -> dict:
    st = radio.feed_status(db_path)
    cond = radio.latest_conditions(db_path)
    out = {
        "source": "NOAA Weather Radio KHB37 Richmond, 162.475 MHz, transcribed locally",
        "feed_stale": st["weather_stale"],
        "eas_decoder_stale": st["eas_stale"],
    }
    if st["weather_stale"]:
        out["note"] = ("The weather stream has not been heard for a while -- the radio feed is "
                       "down. Do not describe conditions from this.")
    if cond:
        out["conditions"] = {
            "as_of": _local(cond["at"], tz_name),
            "temperature_f": cond.get("temperature_f"), "humidity_pct": cond.get("humidity_pct"),
            "wind": cond.get("wind"), "pressure_in": cond.get("pressure_in"),
            "pressure_trend": cond.get("pressure_trend"), "sky": cond.get("sky"),
            "heat_index_f": cond.get("heat_index_f"), "summary": cond.get("summary"),
            "hazards": cond.get("hazards") or None, "forecast": cond.get("forecast") or None,
        }
    else:
        out["conditions"] = None
        out.setdefault("note", "No conditions have been extracted yet.")
    out["active_alerts"] = [{
        "event": e["title"], "severity": e["severity"], "at": _local(e["at"], tz_name),
        "areas": e["meta"].get("area_names"), "affects_home_counties": e["meta"].get("affects_me"),
        "expires": _local(e["meta"].get("expires_at"), tz_name),
    } for e in radio.active_eas(db_path)]
    return out


def _scanner(db_path: str, tz_name: str, minutes: int, include_all: bool) -> dict:
    st = radio.feed_status(db_path)
    flagged = radio.recent_items(db_path, hours=minutes / 60, kinds=("scanner",), limit=30)
    heard = radio.recent_transcripts(db_path, radio.STREAM_SCANNER, minutes=minutes, limit=200)
    out = {
        "feed_stale": st["scanner_stale"],
        "window_minutes": minutes,
        "transmissions_heard": len(heard),
        "flagged": [{
            "at": _local(f["at"], tz_name), "severity": f["severity"], "summary": f["title"],
            "category": f["meta"].get("category"), "location": f["meta"].get("location"),
            "heard": f.get("body"),
        } for f in flagged],
    }
    if st["scanner_stale"]:
        out["note"] = "The scanner stream has not been heard for a while -- the radio feed is down."
    elif not heard:
        out["note"] = ("Nothing has keyed up on the monitored channels in that window. These are "
                       "Fire/EMS talkaround channels, so quiet is normal.")
    if include_all:
        out["transcripts"] = [{"at": _local(h["started_at"], tz_name), "text": h["text"]}
                              for h in heard[:40]]
    return out


def _rf(db_path: str, tz_name: str) -> dict:
    st = radio.feed_status(db_path)
    rep = radio.rf_report(db_path)
    if not rep:
        return {"feed_stale": True, "note": "No RF baseline report has been collected yet."}
    devices = rep.get("devices", [])

    def pick(cls):
        return [{
            "device": d.get("label") or d["fingerprint"], "visits": d["visits"],
            "days_seen": d["days_seen"], "typical_hours_local": d.get("typical_hours"),
            "last_seen": _local(d["last_seen"], tz_name), "first_seen": _local(d["first_seen"], tz_name),
        } for d in devices if d["class"] == cls]

    return {
        "feed_stale": st["rf_stale"],
        "report_generated": _local(rep.get("generated_at"), tz_name),
        "note": ("The sensor node has not reported for a while -- treat this as possibly out "
                 "of date." if st["rf_stale"] else None),
        "counts": rep.get("counts"),
        "traffic": rep.get("traffic"),
        "alerts": [a.get("text") for a in rep.get("alerts", [])],
        "vehicles_to_check": pick("vehicle-watch"),
        "regular_vehicles": pick("vehicle-regular"),
        "repeat_vehicles": pick("vehicle-repeat"),
        "neighbour_fixtures": pick("fixture-neighbour"),
        "new_unknown_transmitters": pick("unknown-new"),
        "own_sensors": pick("own"),
        "unlabelled_note": ("Devices without a label are shown by fingerprint; the owner names "
                            "them on the sensor node with rf_sensorctl label."),
    }


def _alerts(db_path: str, tz_name: str, hours: float) -> dict:
    items = radio.recent_items(db_path, hours=hours, limit=60)
    return {
        "hours": hours,
        "alerts": [{
            "at": _local(i["at"], tz_name), "kind": i["kind"], "severity": i["severity"],
            "title": i["title"], "body": i.get("body"),
            "pushed_to_owner": bool(i.get("notified_at")) and i["severity"] != "info",
        } for i in items],
    }
