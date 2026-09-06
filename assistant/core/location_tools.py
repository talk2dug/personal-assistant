"""Chat tools for places, location reminders and routines.

Kept out of engine.py purely for size — engine is already ~1000 lines. These are
dispatched directly by engine rather than through a context client, because unlike Era
or Home Assistant there's no external service behind them: places and routines are
Jarvis's own state, and the only external part (reading GPS) belongs to the HA client
that's already wired.
"""
from . import db, location

LOCATION_TOOLS = [
    {"type": "function", "function": {
        "name": "get_my_location",
        "description": (
            "Where the user is right now — the named place if he's at one, otherwise the "
            "nearest known place and how far away, plus raw coordinates, GPS accuracy, "
            "speed and phone battery. Use it for 'where am I', 'am I at work', and before "
            "anything that depends on where he is."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "save_place",
        "description": (
            "Teach Jarvis a place. With no coordinates it saves wherever he is right now — "
            "which is how places should normally be created: he says 'remember this as the "
            "gym' while standing there and nothing needs configuring in Home Assistant. "
            "Saving an existing name updates it."
        ),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "e.g. 'work', 'the gym', 'mum's'."},
            "latitude": {"type": "number", "description": "Omit to use his current position."},
            "longitude": {"type": "number"},
            "radius_m": {"type": "number",
                         "description": "How close counts as being there. Default 150m; use more for a large site."},
            "notes": {"type": "string"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "list_places",
        "description": "Every place Jarvis knows, with how far each one is from him right now.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "delete_place",
        "description": "Forget a place. Also removes any location reminders and routines attached to it.",
        "parameters": {"type": "object", "properties": {"place_id": {"type": "integer"}},
                       "required": ["place_id"]},
    }},
    {"type": "function", "function": {
        "name": "add_location_reminder",
        "description": (
            "Remind him when he arrives at or leaves a place, rather than at a time — "
            "'remind me to get milk when I'm at the shop', 'when I leave work, remind me "
            "to call mum'. Name the place; if Jarvis doesn't know it yet, save it first."
        ),
        "parameters": {"type": "object", "properties": {
            "place_name": {"type": "string"},
            "trigger": {"type": "string", "enum": ["arrive", "leave"]},
            "text": {"type": "string", "description": "What to remind him about."},
            "repeat": {"type": "boolean",
                       "description": "True to fire every time. Default is once, then it's done."},
        }, "required": ["place_name", "trigger", "text"]},
    }},
    {"type": "function", "function": {
        "name": "list_location_reminders",
        "description": "Reminders waiting on him going somewhere, as opposed to on a time.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "cancel_location_reminder",
        "description": "Cancel a location reminder by its id.",
        "parameters": {"type": "object", "properties": {"reminder_id": {"type": "integer"}},
                       "required": ["reminder_id"]},
    }},
    {"type": "function", "function": {
        "name": "create_routine",
        "description": (
            "A standing routine that runs when he arrives at or leaves a place. The prompt "
            "is handled by you exactly as if he'd asked it, so a routine can do anything you "
            "can — check the commute, read what's waiting, turn the lights on. Example: "
            "trigger 'leave' at 'work' with prompt 'Tell me the drive time home and anything "
            "waiting for my approval.' Say what it will do and when, so he knows what he's "
            "agreed to."
        ),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "place_name": {"type": "string"},
            "trigger": {"type": "string", "enum": ["arrive", "leave"]},
            "prompt": {"type": "string", "description": "What you should do when it fires."},
            "cooldown_minutes": {"type": "integer",
                                 "description": "Minimum gap between firings. Default 30."},
        }, "required": ["name", "place_name", "trigger", "prompt"]},
    }},
    {"type": "function", "function": {
        "name": "list_routines",
        "description": "Every standing routine, what triggers it, and when it last ran.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "set_routine_enabled",
        "description": "Turn a routine on or off without deleting it.",
        "parameters": {"type": "object", "properties": {
            "routine_id": {"type": "integer"}, "enabled": {"type": "boolean"},
        }, "required": ["routine_id", "enabled"]},
    }},
    {"type": "function", "function": {
        "name": "delete_routine",
        "description": "Delete a routine permanently.",
        "parameters": {"type": "object", "properties": {"routine_id": {"type": "integer"}},
                       "required": ["routine_id"]},
    }},
]

LOCATION_TOOL_NAMES = {t["function"]["name"] for t in LOCATION_TOOLS}

LOCATION_SYSTEM_NOTE = (
    " You can see where the user is, from his phone's GPS via Home Assistant, and you keep "
    "your own named places — nothing has to be configured in Home Assistant for this. When he "
    "mentions somewhere he goes regularly and you don't already know it, offer to save it while "
    "he's there; save_place with no coordinates records wherever he is at that moment. You can "
    "set reminders that fire on arriving somewhere or leaving it rather than at a time, and "
    "standing routines that run when he arrives or leaves — a routine's prompt comes back to you "
    "to carry out, so it can do anything you can. Location is sensitive: report it when he asks "
    "or when it's genuinely relevant, don't narrate his movements unprompted, and never guess a "
    "position when the GPS fix is missing or poor — say you can't tell."
)


def handle(db_path: str, owner_user_id: int, name: str, arguments: dict, home_assistant=None) -> dict:
    """Executes a location tool. home_assistant supplies GPS; without it the tools that
    need a live position say so rather than guessing."""

    def current_position(force: bool = True):
        """force wakes the phone for a fresh fix rather than reading a cached one.

        Default on, because every caller here is answering a question about *now* —
        "where am I", or pinning a place at his current position — and iOS may not have
        sent an update in hours. The background watcher is the one that has to be frugal
        with these; a direct question is worth a push.
        """
        if home_assistant is None:
            return None, {"error": "Home Assistant isn't connected, so I can't see where you are"}
        client = home_assistant.mcp_client
        fix = client.location_now() if force and hasattr(client, "location_now") else client.location()
        if fix.get("latitude") is None:
            return None, {"error": fix.get(
                "error", "no GPS position available — the phone isn't reporting to Home Assistant")}
        return fix, None

    if name == "get_my_location":
        fix, problem = current_position()
        if problem:
            return problem
        summary = location.describe(db.list_places(db_path, owner_user_id),
                                    fix["latitude"], fix["longitude"])
        return {**summary, "coordinates": {"latitude": fix["latitude"], "longitude": fix["longitude"]},
                "accuracy_m": fix.get("accuracy_m"), "speed": fix.get("speed"),
                "battery": fix.get("battery"), "ha_zone": fix.get("zone")}

    if name == "save_place":
        latitude, longitude = arguments.get("latitude"), arguments.get("longitude")
        if latitude is None or longitude is None:
            fix, problem = current_position()
            if problem:
                return problem
            latitude, longitude = fix["latitude"], fix["longitude"]
            if (fix.get("accuracy_m") or 0) > location.MAX_USABLE_ACCURACY_M:
                return {"error": f"the GPS fix is only accurate to {fix['accuracy_m']:.0f}m — "
                                 "too vague to pin a place on. Try again in a moment."}
        place_id = db.create_place(
            db_path, owner_user_id, arguments["name"], latitude, longitude,
            arguments.get("radius_m", 150), arguments.get("notes"))
        return {"ok": True, "place_id": place_id, "name": arguments["name"],
                "latitude": latitude, "longitude": longitude,
                "radius_m": arguments.get("radius_m", 150)}

    if name == "list_places":
        places = db.list_places(db_path, owner_user_id)
        fix = None
        if home_assistant is not None:
            candidate = home_assistant.mcp_client.location()
            fix = candidate if candidate.get("latitude") is not None else None
        result = []
        for place in places:
            entry = {k: place[k] for k in ("id", "name", "latitude", "longitude", "radius_m", "notes")}
            if fix:
                entry["distance_m"] = round(location.haversine_m(
                    fix["latitude"], fix["longitude"], place["latitude"], place["longitude"]))
            result.append(entry)
        return {"places": result}

    if name == "delete_place":
        return {"ok": db.delete_place(db_path, owner_user_id, arguments["place_id"])}

    if name in ("add_location_reminder", "create_routine"):
        place = db.get_place_by_name(db_path, owner_user_id, arguments["place_name"])
        if place is None:
            known = [p["name"] for p in db.list_places(db_path, owner_user_id)]
            return {"error": f"I don't know a place called {arguments['place_name']!r}",
                    "known_places": known,
                    "hint": "save_place first — with no coordinates it uses where he is now"}
        if name == "add_location_reminder":
            reminder_id = db.create_location_reminder(
                db_path, owner_user_id, place["id"], arguments["trigger"], arguments["text"],
                once=not arguments.get("repeat", False))
            return {"ok": True, "reminder_id": reminder_id, "place": place["name"],
                    "trigger": arguments["trigger"]}
        routine_id = db.create_routine(
            db_path, owner_user_id, arguments["name"], arguments["trigger"], place["id"],
            arguments["prompt"], arguments.get("cooldown_minutes", 30))
        return {"ok": True, "routine_id": routine_id, "place": place["name"],
                "trigger": arguments["trigger"]}

    if name == "list_location_reminders":
        return {"reminders": db.list_location_reminders(db_path, owner_user_id)}
    if name == "cancel_location_reminder":
        return {"ok": db.cancel_location_reminder(db_path, owner_user_id, arguments["reminder_id"])}
    if name == "list_routines":
        return {"routines": db.list_routines(db_path, owner_user_id)}
    if name == "set_routine_enabled":
        return {"ok": db.set_routine_enabled(
            db_path, owner_user_id, arguments["routine_id"], arguments["enabled"])}
    if name == "delete_routine":
        return {"ok": db.delete_routine(db_path, owner_user_id, arguments["routine_id"])}

    return {"error": f"unknown location tool {name}"}
