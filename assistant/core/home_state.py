"""A room-shaped view of the house, assembled from Home Assistant's entity states.

The Command Center's Presence & Sensors panel asks a question Home Assistant does not
answer directly -- "which room is Jack in, and what is that room doing right now?" HA
knows about 200-odd entities; it does not know that `sensor.bedroom_dot_temperature`,
`binary_sensor.bedroom_dot_motion` and `light.jacks_lamp` are all facts about the same
physical room. That mapping lives here, in one table, so both the web UI and (later)
anything else that needs a room-level answer read the same one.

Everything is derived from a SINGLE /api/states call (HomeAssistantClient.raw_states,
which caches it for a few seconds) -- never one request per entity. That distinction is
the whole reason the dashboard can poll this every few seconds without dragging the HA
box down.

Room presence is a best-effort inference, not a sensor, and says so: `source` on the
presence block tells the caller whether the room came from a real motion detector, from
something being switched on, or from nothing at all. The UI is expected to show that
distinction rather than present a guess as fact.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# One entry per physical room. `activity` entities are the ones whose being on/playing
# means a person is plausibly in that room right now; `motion` entities are real
# occupancy sensors and outrank them. Missing entities are skipped silently -- this list
# is allowed to describe a house that is still being built out.
ROOMS = [
    {
        "key": "kitchen",
        "name": "Kitchen",
        "camera": "kitchen",
        "temp": None,
        "motion": [],
        "activity": [
            "light.kitchen_stove_1",
            "light.kitchen_stove_2",
            "light.kitchen_range_light",
            "switch.kitchen_sink_socket_1",
            "switch.wp3_5_2_socket_1",
        ],
    },
    {
        "key": "livingroom",
        "name": "Living Room",
        "camera": "livingroom",
        "temp": "sensor.living_room_dot_temperature",
        "motion": [],
        "activity": [
            "media_player.living_room_dot",
            "media_player.echo_show",
            "light.living_room_lamp",
            "light.living_room_strip",
            "light.living_room_side_table",
            "switch.wp3_5_3_socket_1",
        ],
    },
    {
        "key": "bedroom",
        "name": "Bedroom",
        "camera": "bedroom",
        "temp": "sensor.bedroom_dot_temperature",
        "motion": ["binary_sensor.bedroom_dot_motion"],
        "activity": [
            "media_player.bedroom_dot",
            "light.bedroom_1",
            "light.bedroom_2",
            "light.bedroom_3",
            "light.bedroom_4",
            "light.bedroom_5",
            "light.bedroom_accent",
            "light.jacks_lamp",
            "light.brookes_lamp",
        ],
    },
    {
        "key": "office",
        "name": "Office",
        "camera": None,
        "temp": None,
        "motion": [],
        "activity": ["media_player.office_dot"],
    },
    {
        "key": "laundry",
        "name": "Laundry Room",
        "camera": "laundry",
        "temp": None,
        "motion": [],
        "activity": ["switch.laundry_room_switch_1"],
    },
]

# The oven/range. Its own block rather than a room field: the kitchen tile shows what
# the range is doing, which is the one appliance in this house that can matter urgently.
RANGE = {
    "operating_state": "sensor.kitchen_range_operating_state",
    "machine_state": "sensor.kitchen_range_machine_state",
    "job_state": "sensor.kitchen_range_job_state",
    "oven_mode": "sensor.kitchen_range_oven_mode",
    "temperature": "sensor.kitchen_range_temperature",
    "setpoint": "sensor.kitchen_range_setpoint",
    "completion_time": "sensor.kitchen_range_completion_time",
    "door": "binary_sensor.kitchen_range_door",
    "light": "light.kitchen_range_light",
}

PERSON_ENTITY = "person.jack_swayze"
PHONE_TRACKER = "device_tracker.upstream"
PHONE_LOCATION = "sensor.upstream_geocoded_location"
PHONE_BATTERY = "sensor.upstream_battery_level"

# States that mean "nobody has told us anything", as opposed to a real reading.
UNKNOWN = ("unknown", "unavailable", "none", "")
# States of an `activity` entity that suggest a person is there.
LIVE = ("on", "playing", "open")


def _num(raw):
    try:
        return round(float(raw), 1)
    except (TypeError, ValueError):
        return None


def _known(state) -> bool:
    return state is not None and str(state).lower() not in UNKNOWN


def _changed_at(entity: dict):
    """last_changed as an aware datetime, or None. Used only for ranking rooms."""
    raw = entity.get("last_changed") or entity.get("last_updated")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _index(states: list[dict]) -> dict:
    return {s["entity_id"]: s for s in states}


def _presence(by_id: dict, rooms: list[dict]) -> dict:
    """Where Jack is: the house-level answer from HA, the room-level one inferred.

    Home/away is a real reading (HA's own person entity). The ROOM is a guess, ranked
    best-evidence-first: a motion sensor that is currently tripped beats a light someone
    switched on, and among equals the most recently changed one wins. `source` carries
    which of those it was so the UI never dresses an inference up as a sensor.
    """
    person = by_id.get(PERSON_ENTITY) or by_id.get(PHONE_TRACKER)
    state = person["state"] if person else None
    home = None
    if _known(state):
        home = state == "home"

    away_where = None
    if home is False:
        geo = by_id.get(PHONE_LOCATION)
        if geo and _known(geo["state"]):
            away_where = geo["state"].splitlines()[0]
        elif _known(state):
            away_where = str(state).replace("_", " ").title()

    if home is not True:
        return {
            "home": home,
            "room": None,
            "room_key": None,
            "source": "unknown" if home is None else "away",
            "label": away_where or ("Away" if home is False else "Unknown"),
            "detail": "no tracker reporting" if home is None else (away_where or "not home"),
        }

    # Home: rank the rooms.
    best = None
    for room in rooms:
        if room["motion_live"]:
            rank, source = 2, "motion"
        elif room["activity_live"]:
            rank, source = 1, "activity"
        else:
            continue
        stamp = room["_changed"]
        if best is None or (rank, stamp or datetime.min.replace(tzinfo=timezone.utc)) > (
            best[0], best[1] or datetime.min.replace(tzinfo=timezone.utc)
        ):
            best = (rank, stamp, room, source)

    if best is None:
        return {
            "home": True, "room": None, "room_key": None, "source": "none",
            "label": "Home", "detail": "room unknown — no room sensor active",
        }

    _, _, room, source = best
    return {
        "home": True,
        "room": room["name"],
        "room_key": room["key"],
        "source": source,
        "label": f"Home · {room['name']}",
        "detail": "motion" if source == "motion" else f"inferred from {room['activity_by']}",
    }


def _rooms(by_id: dict) -> list[dict]:
    out = []
    for spec in ROOMS:
        motion_live = False
        stamps = []
        for eid in spec["motion"]:
            ent = by_id.get(eid)
            if ent and ent["state"] == "on":
                motion_live = True
                stamps.append(_changed_at(ent))

        activity_live, activity_by = False, None
        for eid in spec["activity"]:
            ent = by_id.get(eid)
            if ent and str(ent["state"]).lower() in LIVE:
                activity_live = True
                activity_by = activity_by or ent.get("attributes", {}).get("friendly_name", eid)
                stamps.append(_changed_at(ent))

        temp = None
        if spec["temp"]:
            ent = by_id.get(spec["temp"])
            if ent and _known(ent["state"]):
                temp = _num(ent["state"])

        known_stamps = [s for s in stamps if s]
        out.append({
            "key": spec["key"],
            "name": spec["name"],
            "camera": spec["camera"],
            "temp_f": temp,
            "motion_live": motion_live,
            "activity_live": activity_live,
            "activity_by": activity_by,
            "has_sensors": bool(spec["motion"] or spec["temp"]),
            "_changed": max(known_stamps) if known_stamps else None,
        })
    return out


def _range(by_id: dict) -> dict:
    def state_of(key):
        ent = by_id.get(RANGE[key])
        if ent is None or not _known(ent["state"]):
            return None
        return ent["state"]

    operating = state_of("operating_state")
    machine = state_of("machine_state")
    job = state_of("job_state")
    temp = _num(state_of("temperature"))
    setpoint = _num(state_of("setpoint"))
    door_ent = by_id.get(RANGE["door"])
    door_open = door_ent["state"] == "on" if door_ent and _known(door_ent["state"]) else None
    light_ent = by_id.get(RANGE["light"])

    # "Is the oven actually cooking?" -- HA splits this across three fields that each
    # say something slightly different, so collapse them into one word the UI can
    # colour on: the range reports operating_state 'run' whenever it is powered and
    # responsive, which is NOT the same as heating.
    if operating is None and machine is None:
        status = "offline"
    elif job and job not in ("ready", "none"):
        status = "cooking"
    elif machine and machine != "ready":
        status = machine
    elif operating == "run":
        status = "ready"
    else:
        status = operating or machine or "unknown"

    if status == "cooking":
        headline = f"{temp:.0f}°F" if temp is not None else "Cooking"
    elif temp is not None and temp > 100:
        headline = f"Warm · {temp:.0f}°F"
    else:
        headline = status.replace("_", " ").title()

    return {
        "available": operating is not None or machine is not None,
        "status": status,
        "headline": headline,
        "temperature_f": temp,
        "setpoint_f": setpoint,
        "oven_mode": state_of("oven_mode"),
        "job_state": job,
        "machine_state": machine,
        "door_open": door_open,
        "light_on": light_ent["state"] == "on" if light_ent else None,
        "completion_time": state_of("completion_time"),
    }


def snapshot(ha, cameras: list[dict] | None = None) -> dict:
    """The whole house in one object, or a clearly-marked failure.

    A dead or unconfigured Home Assistant is NOT an error here: the dashboard still has
    a Presence panel, it just has nothing to put in it, and one unreachable integration
    must never take the board down with it.
    """
    cameras = cameras or []
    camera_block = {
        "total": len(cameras),
        "enabled": sum(1 for c in cameras if c.get("enabled")),
        "list": [
            {"key": c["key"], "name": c["name"], "location": c.get("location"),
             "enabled": bool(c.get("enabled"))}
            for c in cameras
        ],
    }

    # Callers hand us whatever they have: app.state.home_assistant is a
    # HomeAssistantContext wrapper (see core/setup.py), while a test or a script holds
    # the bare client. Accept both rather than making every call site remember which.
    client = getattr(ha, "mcp_client", ha)
    if client is None:
        return {"available": False, "error": "Home Assistant is not configured",
                "presence": None, "rooms": [], "range": None, "cameras": camera_block}

    try:
        states = client.raw_states()
    except Exception as e:
        logger.warning("home snapshot: Home Assistant unreachable: %s", e)
        return {"available": False, "error": f"Home Assistant unreachable: {e}",
                "presence": None, "rooms": [], "range": None, "cameras": camera_block}

    by_id = _index(states)
    rooms = _rooms(by_id)
    presence = _presence(by_id, rooms)

    battery = by_id.get(PHONE_BATTERY)
    phone_battery = _num(battery["state"]) if battery and _known(battery["state"]) else None

    # _changed is a datetime used only for ranking; it must not cross the JSON boundary.
    for room in rooms:
        room.pop("_changed", None)

    return {
        "available": True,
        "error": None,
        "presence": presence,
        "rooms": rooms,
        "range": _range(by_id),
        "cameras": camera_block,
        "phone_battery": phone_battery,
        "entity_count": len(states),
    }
