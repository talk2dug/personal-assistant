"""Location awareness — where the user is, and what should happen when that changes.

Geofencing is done here rather than with Home Assistant zones. HA zones are the official
route, but each one is manual UI work, and the user was explicit that he doesn't want to
do that. His phone already reports GPS to HA at ~5m accuracy, so Jarvis keeps its own
places and he can name one just by being there.

The transition detector is the part worth being careful about. GPS wanders, especially
indoors, and a naive "am I inside the circle" check fires arrive/leave repeatedly while
someone sits still near a boundary. Two defences: a hysteresis margin so leaving needs
more movement than arriving did, and an accuracy gate so a low-confidence fix can't move
you at all.
"""
import logging
import math

from . import db

logger = logging.getLogger(__name__)

EARTH_RADIUS_M = 6371000.0

# Leaving requires being this much further out than the radius that let you arrive.
# Without it, a phone drifting a few metres across the edge produces an endless
# arrive/leave/arrive stream, and every routine attached to it fires each time.
EXIT_HYSTERESIS_M = 60.0

# A fix worse than this is treated as "no idea where he is" rather than as a position.
# Cell-tower fixes can be a kilometre wide and would otherwise teleport him between
# places.
MAX_USABLE_ACCURACY_M = 250.0

CURRENT_PLACE_KEY = "current_place_id"


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def resolve_place(places: list[dict], latitude: float, longitude: float, current_place_id=None):
    """Which known place this position is in, or None.

    current_place_id applies the exit hysteresis: staying counted as "here" needs only
    the plain radius, but being moved out needs to be clearly outside it.
    """
    best = None
    best_distance = None
    for place in places:
        distance = haversine_m(latitude, longitude, place["latitude"], place["longitude"])
        radius = place["radius_m"]
        if place["id"] == current_place_id:
            radius += EXIT_HYSTERESIS_M
        if distance <= radius and (best_distance is None or distance < best_distance):
            best, best_distance = place, distance
    return best, best_distance


def describe(places: list[dict], latitude: float, longitude: float) -> dict:
    """Human-useful summary: the place if inside one, else the nearest and how far."""
    place, distance = resolve_place(places, latitude, longitude)
    if place is not None:
        return {"place": place["name"], "place_id": place["id"], "distance_m": round(distance)}
    nearest, nearest_distance = None, None
    for candidate in places:
        d = haversine_m(latitude, longitude, candidate["latitude"], candidate["longitude"])
        if nearest_distance is None or d < nearest_distance:
            nearest, nearest_distance = candidate, d
    if nearest is None:
        return {"place": None, "place_id": None}
    return {
        "place": None, "place_id": None,
        "nearest": nearest["name"],
        "nearest_distance_m": round(nearest_distance),
        "nearest_distance_miles": round(nearest_distance / 1609.34, 1),
    }


def detect_transition(db_path: str, owner_user_id: int, latitude: float, longitude: float,
                      accuracy: float | None = None) -> dict | None:
    """Compares this fix to the last known place and returns a transition, or None.

    Returns {"event": "arrive"|"leave", "place": {...}} — a single event per call. A move
    straight from one place to another reports the arrival, because that's the one worth
    acting on; the departure is implied and firing both would double up notifications.
    """
    if accuracy is not None and accuracy > MAX_USABLE_ACCURACY_M:
        logger.debug("ignoring fix with %.0fm accuracy", accuracy)
        return None

    places = db.list_places(db_path, owner_user_id)
    if not places:
        return None

    previous_raw = db.get_setting(db_path, CURRENT_PLACE_KEY)
    previous_id = int(previous_raw) if previous_raw and previous_raw.isdigit() else None
    place, _ = resolve_place(places, latitude, longitude, current_place_id=previous_id)
    new_id = place["id"] if place else None

    if new_id == previous_id:
        return None

    db.set_setting(db_path, CURRENT_PLACE_KEY, str(new_id) if new_id else "")

    if new_id is not None:
        return {"event": "arrive", "place": place}
    previous = next((p for p in places if p["id"] == previous_id), None)
    if previous is None:
        return None
    return {"event": "leave", "place": previous}
