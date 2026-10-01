"""The home-control panel on the touch terminals (touch1, touch2, laptop1).

The terminals started as display-only voice screens. This gives them the other half of
a wall panel: every light and switch in the house, grouped by room, plus a strip with
the weather, the drive and whatever is next on his schedule.

Rooms come from Home Assistant's own areas, not a table in here. home_state.ROOMS is a
hand-kept presence map for the Command Center; the control panel has to show *everything*
controllable, and the areas are where Jack already files new devices. Area lookup is one
websocket read of HA's registries (REST has none), cached for a few minutes.
Areas change on the order of weeks, and entity states still come fresh from raw_states().

Entities without an area are placed by fallbacks, in this order: a light group goes to
the area most of its members are in; anything home_state.ROOMS lists goes to that room;
the rest go under "Other". So a new plug still appears without a code change, just not
in its room until it gets an area.

Control goes through a per-domain whitelist of services and data keys, minus the
configured sensitive domains. The device key is shared by every terminal and sits in a
kiosk URL, so it must never be able to reach a lock or a garage door.
"""
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import home_state

logger = logging.getLogger(__name__)

PANEL_DOMAINS = ("light", "switch", "fan", "scene", "media_player")

# domain -> service -> data keys that service may carry.
_LIGHT_DATA = {"brightness_pct", "color_temp_kelvin", "hs_color", "rgb_color", "effect", "transition"}
SERVICES = {
    "light": {"turn_on": _LIGHT_DATA, "turn_off": {"transition"}, "toggle": set()},
    "switch": {"turn_on": set(), "turn_off": set(), "toggle": set()},
    "fan": {"turn_on": {"percentage"}, "turn_off": set(), "toggle": set(), "set_percentage": {"percentage"}},
    "scene": {"turn_on": set()},
    "media_player": {"media_play_pause": set(), "volume_set": {"volume_level"},
                     "turn_on": set(), "turn_off": set()},
}

# Belt and braces for the registry filter below: names that are never a panel button even
# if HA doesn't flag them as config entities.
_NOISE = re.compile(r"_do_not_disturb$|_status_led$|_child_lock$")

REGISTRY_TTL_SEC = 300
_registry_cache: dict = {"at": 0.0, "value": None}


class PanelError(Exception):
    """A control request the panel refuses. The message is safe to show on screen."""


async def _fetch_registry(base_url: str, token: str) -> dict:
    import websockets

    url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    async with websockets.connect(url, max_size=None, open_timeout=10) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise RuntimeError("Home Assistant rejected the token")
        out = {}
        for i, kind in enumerate(("area", "device", "entity"), 1):
            await ws.send(json.dumps({"id": i, "type": f"config/{kind}_registry/list"}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == i:
                    if not msg.get("success"):
                        raise RuntimeError(f"{kind} registry: {msg.get('error')}")
                    out[kind] = msg["result"]
                    break
    return out


def _registry(client) -> dict:
    """{"area": {entity_id: area name}, "skip": {entity_ids HA hides or files as
    config/diagnostic}}. REST has no registry access, hence the one websocket read."""
    now = time.monotonic()
    cached = _registry_cache["value"]
    if cached is not None and now - _registry_cache["at"] < REGISTRY_TTL_SEC:
        return cached
    import asyncio

    token = client._headers["Authorization"].removeprefix("Bearer ")
    raw = asyncio.run(asyncio.wait_for(_fetch_registry(client.base_url, token), 20))
    area_names = {a["area_id"]: a["name"] for a in raw["area"]}
    device_area = {d["id"]: d.get("area_id") for d in raw["device"]}
    value = {"area": {}, "skip": set()}
    for e in raw["entity"]:
        eid = e["entity_id"]
        if e.get("hidden_by") or e.get("disabled_by") or e.get("entity_category"):
            value["skip"].add(eid)
        area = e.get("area_id") or device_area.get(e.get("device_id"))
        if area in area_names:
            value["area"][eid] = area_names[area]
    _registry_cache.update(at=now, value=value)
    return value


def _fallback_room() -> dict[str, str]:
    return {eid: room["name"] for room in home_state.ROOMS for eid in room["activity"]}


def _tile(s: dict) -> dict:
    eid = s["entity_id"]
    a = s.get("attributes", {})
    modes = a.get("supported_color_modes") or []
    tile = {
        "entity_id": eid,
        "domain": eid.split(".", 1)[0],
        "name": a.get("friendly_name") or eid,
        "state": s.get("state"),
        "available": s.get("state") not in ("unavailable", "unknown") or eid.startswith("scene."),
        "group": bool(a.get("entity_id")) and eid.startswith("light."),
        "members": a.get("entity_id") or [],
    }
    if tile["domain"] == "light":
        bri = a.get("brightness")
        tile.update({
            "brightness_pct": round(bri / 2.55) if isinstance(bri, (int, float)) else None,
            "dimmable": any(m not in ("onoff",) for m in modes),
            "color": any(m in ("hs", "rgb", "rgbw", "rgbww", "xy") for m in modes),
            "color_temp": "color_temp" in modes,
            "color_mode": a.get("color_mode"),
            "hs_color": a.get("hs_color"),
            "rgb_color": a.get("rgb_color"),
            "color_temp_kelvin": a.get("color_temp_kelvin"),
            "min_kelvin": a.get("min_color_temp_kelvin") or 2000,
            "max_kelvin": a.get("max_color_temp_kelvin") or 6500,
            "effects": a.get("effect_list") or [],
            "effect": a.get("effect"),
        })
    elif tile["domain"] == "media_player":
        tile.update({"media_title": a.get("media_title"), "volume_level": a.get("volume_level")})
    elif tile["domain"] == "fan":
        tile["percentage"] = a.get("percentage")
    return tile


def rooms(client, sensitive_domains=()) -> dict:
    """Every controllable entity, grouped into rooms. Raises if HA is unreachable."""
    states = client.raw_states()
    registry = _registry(client)
    areas = registry["area"]
    fallback = _fallback_room()
    allowed = [d for d in PANEL_DOMAINS if d not in set(sensitive_domains)]

    tiles = {}
    for s in states:
        eid = s["entity_id"]
        domain = eid.split(".", 1)[0]
        if domain not in allowed or eid in registry["skip"] or _NOISE.search(eid):
            continue
        # A speaker that is off the network is not a control, it's clutter.
        if domain == "media_player" and s.get("state") == "unavailable":
            continue
        tiles[eid] = _tile(s)

    placed: dict[str, str] = {}
    for eid, t in tiles.items():
        room = areas.get(eid) or fallback.get(eid)
        if room:
            placed[eid] = room
    for eid, t in tiles.items():
        if eid in placed or not t["group"]:
            continue
        member_rooms = Counter(areas.get(m) or fallback.get(m) for m in t["members"])
        member_rooms.pop(None, None)
        if member_rooms:
            placed[eid] = member_rooms.most_common(1)[0][0]

    by_room: dict[str, list] = {}
    scenes = []
    for eid, t in tiles.items():
        if t["domain"] == "scene":
            scenes.append(t)
            continue
        by_room.setdefault(placed.get(eid, "Other"), []).append(t)

    order = {"light": 0, "switch": 1, "fan": 2, "media_player": 3}
    out = []
    for name, items in by_room.items():
        # The room's group light first (it's the "whole room" button), then by type, name.
        items.sort(key=lambda t: (not t["group"], order.get(t["domain"], 9), t["name"].lower()))
        out.append({
            "name": name,
            "tiles": items,
            "on": sum(1 for t in items if t["state"] == "on" and not t["group"]),
        })
    out.sort(key=lambda r: (r["name"] == "Other", r["name"]))
    scenes.sort(key=lambda t: t["name"].lower())
    return {"rooms": out, "scenes": scenes}


def control(client, entity_id: str, service: str, data: dict | None = None,
            sensitive_domains=()) -> dict:
    """Run one whitelisted service call. Raises PanelError for anything off the list."""
    if not isinstance(entity_id, str) or "." not in entity_id:
        raise PanelError("bad entity")
    ids = [entity_id]
    domain = entity_id.split(".", 1)[0]
    if domain in set(sensitive_domains) or domain not in SERVICES:
        raise PanelError(f"{domain} can't be controlled from a terminal")
    allowed = SERVICES[domain].get(service)
    if allowed is None:
        raise PanelError(f"{domain}.{service} isn't allowed from a terminal")
    data = data or {}
    extra = set(data) - allowed
    if extra:
        raise PanelError(f"unexpected fields: {', '.join(sorted(extra))}")
    result = client.call_service(domain, service, ids, data)
    # The panel re-polls right after a tap; serve it the new state, not the cached one.
    client._states_cache = None
    return {"ok": True, "entity_id": entity_id, "service": service, "changed": len(result.get("result") or [])}


def room_off(client, room_name: str, sensitive_domains=()) -> dict:
    """Everything that's on in one room, off, in as few service calls as there are domains."""
    view = rooms(client, sensitive_domains)
    room = next((r for r in view["rooms"] if r["name"] == room_name), None)
    if room is None:
        raise PanelError(f"no room called {room_name}")
    by_domain: dict[str, list] = {}
    for t in room["tiles"]:
        if t["state"] == "on" and t["domain"] in ("light", "switch", "fan"):
            by_domain.setdefault(t["domain"], []).append(t["entity_id"])
    for domain, ids in by_domain.items():
        client.call_service(domain, "turn_off", ids)
    client._states_cache = None
    return {"ok": True, "turned_off": sum(len(v) for v in by_domain.values())}


def home_room(db_path: str, client_ip: str | None, room_names: list[str]) -> str | None:
    """The room a terminal lives in, from the camera on the same machine.

    Each terminal's camera is registered with its own IP (cameras.url), and the kiosk
    loads this page from that same IP -- so the request's address names the room
    without any per-terminal config. Deliberately NOT terminal_cameras: that table gates
    what presence.py lets a terminal say aloud, and a UI default has no business there.
    """
    if not client_ip:
        return None
    import sqlite3
    from contextlib import closing as _closing
    try:
        with _closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute("SELECT name, location, url FROM cameras WHERE enabled = 1").fetchall()
    except sqlite3.Error:
        return None
    by_lower = {n.lower(): n for n in room_names}
    for name, location, url in rows:
        if f"//{client_ip}:" in (url or "") or f"//{client_ip}/" in (url or ""):
            for candidate in (name, location):
                if candidate and candidate.lower() in by_lower:
                    return by_lower[candidate.lower()]
    return None


# --- the info strip -------------------------------------------------------------------

_info_cache: dict = {}
INFO_TTL_SEC = 120


def _weather(client) -> dict | None:
    w = client.get_weather(forecast_type="daily")
    if "error" in w:
        return None
    today = next((f for f in w.get("forecast") or [] if isinstance(f, dict) and "error" not in f), {})
    return {
        "condition": w.get("condition"),
        "temperature": w.get("temperature"),
        "unit": w.get("temperature_unit"),
        "high": today.get("temperature"),
        "low": today.get("templow"),
        "precip": today.get("precipitation_probability"),
    }


def _traffic(client) -> list[dict]:
    routes = client.get_travel_time().get("routes") or []
    out = []
    for r in routes:
        # An unavailable route stays in the list: "Work: --" says the sensor is down,
        # an empty strip would look like no route was ever set up.
        try:
            minutes = round(float(r["minutes"]))
        except (TypeError, ValueError):
            minutes = None
        name = re.sub(r"(?i)\s*waze( travel time)?\s*", " ", r.get("name") or "").strip() or "Drive"
        out.append({"name": name, "minutes": minutes, "route": r.get("route")})
    return out


_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})")


def next_up(db_path: str, owner_user_id: int, tz_name: str, calendar_ctx=None,
            now: datetime | None = None) -> dict | None:
    """The next thing on his schedule: the next calendar event if there is one within two
    weeks, otherwise the next dated task/deadline/bill. Reminders are left out, the same
    way the Schedule panel leaves them out: a nudge isn't an appointment."""
    from . import agenda

    tz = ZoneInfo(tz_name)
    now = now or datetime.now(tz)
    view = agenda.upcoming(db_path, owner_user_id, days=14, back_days=0,
                           exclude_kinds=("reminder", "income"), calendar_ctx=calendar_ctx)
    candidates = []
    for day in view.get("days", []):
        for e in day["entries"]:
            if e.get("done"):
                continue
            when = datetime.fromisoformat(e["date"]).replace(tzinfo=tz)
            m = _TIME_RE.match(e.get("detail") or "") if e["kind"] == "event" else None
            timed = bool(m)
            if m:
                when = when.replace(hour=int(m.group(1)), minute=int(m.group(2)))
                if when < now - timedelta(minutes=10):
                    continue
            elif when.date() < now.date():
                continue
            candidates.append((e["kind"] != "event", when.date(), not timed, when, e))
    if not candidates:
        return None
    _, _, untimed, when, e = min(candidates, key=lambda c: c[:4])
    days = (when.date() - now.date()).days
    # No %-d / %-I: the server is Windows, whose strftime rejects them.
    day_label = ("Today" if days == 0 else "Tomorrow" if days == 1
                 else f"{when.strftime('%a %b')} {when.day}")
    detail = e.get("detail") or ""
    if not untimed:
        detail = _TIME_RE.sub("", detail).lstrip(" ·")
    return {
        "title": e["title"],
        "kind": e["kind"],
        "day": day_label,
        "time": None if untimed else f"{when.hour % 12 or 12}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}",
        "detail": detail or None,
    }


def info(client, db_path: str, owner_user_id: int, tz_name: str, calendar_ctx=None) -> dict:
    """Weather, drive times and the next scheduled item. Each part fails on its own:
    a dead calendar must not blank the weather."""
    cached = _info_cache.get("value")
    if cached and time.monotonic() - _info_cache.get("at", 0) < INFO_TTL_SEC:
        return cached
    # The terminals' own clocks can't be trusted for this: touch2 runs on UTC with an
    # en-GB locale and showed 22:27 at 6:27 PM. The page formats in this zone instead.
    out: dict = {"errors": {}, "timezone": tz_name}
    for key, fn in (("weather", lambda: _weather(client)),
                    ("traffic", lambda: _traffic(client)),
                    ("next", lambda: next_up(db_path, owner_user_id, tz_name, calendar_ctx))):
        try:
            out[key] = fn()
        except Exception as exc:  # noqa: BLE001 -- reported per part, see docstring
            logger.warning("touch panel %s failed: %s", key, exc)
            out[key] = None
            out["errors"][key] = f"{type(exc).__name__}: {exc}"[:160]
    _info_cache.update(at=time.monotonic(), value=out)
    return out
