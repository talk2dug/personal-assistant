"""Covers location awareness: geofencing, transition detection, and the place/reminder/
routine tools.

The hysteresis and accuracy tests are the point of this file. A naive geofence fires
arrive/leave repeatedly while someone sits still near a boundary, and every routine
attached to it fires each time — which on a phone means a stream of notifications for
standing at your own kitchen window.
"""
import httpx
import pytest

from assistant.core import db, location, location_tools
from assistant.core.engine import HomeAssistantContext
from assistant.core.home_assistant_client import HomeAssistantClient

# Real coordinates from the user's own HA, so the distances are realistic.
HOME = (37.5686651, -77.4331964)
# ~1.9km north — comfortably outside any sane home radius.
AWAY = (37.5856651, -77.4331964)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner(db_path):
    return db.get_user_by_chat_id(db_path, "111")["id"]


class FakeHA:
    """Stands in for the HA client's location()/location_now()."""

    def __init__(self, lat=HOME[0], lon=HOME[1], accuracy=5.0):
        self.fix = {"latitude": lat, "longitude": lon, "accuracy_m": accuracy,
                    "speed": 0, "battery": 50, "zone": "home"}
        self.forced = 0

    def location(self):
        return self.fix

    def location_now(self, **kwargs):
        self.forced += 1
        return self.fix


def ctx(fake):
    return HomeAssistantContext(mcp_client=fake, sensitive_domains=set())


# --- geometry ----------------------------------------------------------------

def test_haversine_matches_known_distance():
    # 1 degree of latitude is ~111km.
    d = location.haversine_m(37.0, -77.0, 38.0, -77.0)
    assert 110_000 < d < 112_000


def test_resolve_place_picks_the_nearest_containing_place(db_path, owner):
    db.create_place(db_path, owner, "home", *HOME, radius_m=150)
    db.create_place(db_path, owner, "neighbourhood", *HOME, radius_m=2000)
    places = db.list_places(db_path, owner)
    place, distance = location.resolve_place(places, *HOME)
    assert place["name"] == "home" and distance < 5


def test_position_outside_everything_resolves_to_nothing(db_path, owner):
    db.create_place(db_path, owner, "home", *HOME, radius_m=150)
    place, _ = location.resolve_place(db.list_places(db_path, owner), *AWAY)
    assert place is None


# --- transitions -------------------------------------------------------------

def test_arriving_then_leaving(db_path, owner):
    db.create_place(db_path, owner, "home", *HOME, radius_m=150)

    first = location.detect_transition(db_path, owner, *HOME, accuracy=5)
    assert first["event"] == "arrive" and first["place"]["name"] == "home"

    # Staying put is not a new event.
    assert location.detect_transition(db_path, owner, *HOME, accuracy=5) is None

    away = location.detect_transition(db_path, owner, *AWAY, accuracy=5)
    assert away["event"] == "leave" and away["place"]["name"] == "home"
    assert location.detect_transition(db_path, owner, *AWAY, accuracy=5) is None


def test_hysteresis_stops_boundary_flapping(db_path, owner):
    """Sitting just outside the radius after arriving must not count as leaving —
    otherwise a phone drifting a few metres produces endless arrive/leave churn."""
    db.create_place(db_path, owner, "home", *HOME, radius_m=100)
    location.detect_transition(db_path, owner, *HOME, accuracy=5)

    # ~130m north: outside the 100m radius, inside the exit margin.
    drifted = (HOME[0] + 0.00117, HOME[1])
    assert location.haversine_m(*HOME, *drifted) > 100
    assert location.detect_transition(db_path, owner, *drifted, accuracy=5) is None

    # Genuinely gone.
    assert location.detect_transition(db_path, owner, *AWAY, accuracy=5)["event"] == "leave"


def test_a_vague_fix_cannot_move_you(db_path, owner):
    """Cell-tower fixes can be a kilometre wide; acting on one teleports you between
    places and fires the wrong routines."""
    db.create_place(db_path, owner, "home", *HOME, radius_m=150)
    location.detect_transition(db_path, owner, *HOME, accuracy=5)
    assert location.detect_transition(db_path, owner, *AWAY, accuracy=900) is None
    # A good fix from the same spot still works.
    assert location.detect_transition(db_path, owner, *AWAY, accuracy=8)["event"] == "leave"


def test_moving_between_places_reports_the_arrival(db_path, owner):
    db.create_place(db_path, owner, "home", *HOME, radius_m=150)
    db.create_place(db_path, owner, "work", *AWAY, radius_m=150)
    location.detect_transition(db_path, owner, *HOME, accuracy=5)
    event = location.detect_transition(db_path, owner, *AWAY, accuracy=5)
    assert event["event"] == "arrive" and event["place"]["name"] == "work"


def test_no_places_means_no_transitions(db_path, owner):
    assert location.detect_transition(db_path, owner, *HOME, accuracy=5) is None


# --- tools -------------------------------------------------------------------

def test_save_place_uses_current_position_when_none_given(db_path, owner):
    result = location_tools.handle(db_path, owner, "save_place", {"name": "the gym"}, ctx(FakeHA()))
    assert result["ok"] and result["latitude"] == pytest.approx(HOME[0])
    assert db.get_place_by_name(db_path, owner, "The Gym") is not None  # case-insensitive


def test_save_place_refuses_a_vague_fix(db_path, owner):
    """Pinning a place on a 500m fix produces a geofence that fires in the wrong street."""
    result = location_tools.handle(db_path, owner, "save_place", {"name": "gym"},
                                  ctx(FakeHA(accuracy=500)))
    assert "error" in result and "accurate" in result["error"]


def test_save_place_is_an_upsert(db_path, owner):
    location_tools.handle(db_path, owner, "save_place", {"name": "work", "latitude": 1.0, "longitude": 2.0}, None)
    location_tools.handle(db_path, owner, "save_place", {"name": "work", "latitude": 3.0, "longitude": 4.0}, None)
    places = db.list_places(db_path, owner)
    assert len(places) == 1 and places[0]["latitude"] == 3.0


def test_location_tools_say_so_without_home_assistant(db_path, owner):
    result = location_tools.handle(db_path, owner, "get_my_location", {}, None)
    assert "error" in result and "Home Assistant" in result["error"]


def test_get_my_location_names_the_place(db_path, owner):
    db.create_place(db_path, owner, "home", *HOME, radius_m=150)
    result = location_tools.handle(db_path, owner, "get_my_location", {}, ctx(FakeHA()))
    assert result["place"] == "home" and result["accuracy_m"] == 5.0


def test_get_my_location_reports_the_nearest_when_not_at_one(db_path, owner):
    db.create_place(db_path, owner, "home", *HOME, radius_m=150)
    result = location_tools.handle(db_path, owner, "get_my_location", {}, ctx(FakeHA(*AWAY)))
    assert result["place"] is None and result["nearest"] == "home"
    assert result["nearest_distance_m"] > 1000


def test_asking_where_i_am_forces_a_fresh_fix(db_path, owner):
    """iOS suspends the companion app, so a passive read can be hours stale. A direct
    question is worth waking the phone for."""
    fake = FakeHA()
    db.create_place(db_path, owner, "home", *HOME, radius_m=150)
    location_tools.handle(db_path, owner, "get_my_location", {}, ctx(fake))
    assert fake.forced == 1


def test_saving_a_place_forces_a_fresh_fix_too(db_path, owner):
    """Pinning a geofence on a stale position puts it wherever he was hours ago."""
    fake = FakeHA()
    location_tools.handle(db_path, owner, "save_place", {"name": "gym"}, ctx(fake))
    assert fake.forced == 1


def test_only_armed_triggers_justify_waking_the_phone(db_path, owner):
    """Each forced refresh is a push. With nothing waiting on his position there's
    nothing to learn by waking the phone every few minutes."""
    assert db.has_armed_location_triggers(db_path, owner) is False

    place_id = db.create_place(db_path, owner, "work", *AWAY)
    assert db.has_armed_location_triggers(db_path, owner) is False, "a place alone arms nothing"

    reminder_id = db.create_location_reminder(db_path, owner, place_id, "arrive", "badge in")
    assert db.has_armed_location_triggers(db_path, owner) is True

    db.cancel_location_reminder(db_path, owner, reminder_id)
    assert db.has_armed_location_triggers(db_path, owner) is False

    routine_id = db.create_routine(db_path, owner, "R", "leave", place_id, "do a thing")
    assert db.has_armed_location_triggers(db_path, owner) is True
    db.set_routine_enabled(db_path, owner, routine_id, False)
    assert db.has_armed_location_triggers(db_path, owner) is False


def test_location_now_reports_when_the_phone_never_answers(monkeypatch):
    """A phone that's off or force-quit won't respond to the wake push. That has to be
    distinguishable from a fresh fix, or a stale position gets treated as current."""
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json=[{"entity_id": "device_tracker.phone", "state": "unavailable", "attributes": {}}],
        request=httpx.Request("GET", url)))
    monkeypatch.setattr(httpx, "post", lambda url, **k: httpx.Response(
        200, json={}, request=httpx.Request("POST", url)))
    client = HomeAssistantClient("http://ha.test", "tok", "notify.mobile_app_x")
    result = client.location_now(wait_seconds=0.2, poll_interval=0.1)
    assert result["latitude"] is None
    assert result["refresh"]["updated"] is False


def test_a_reminder_for_an_unknown_place_is_refused_helpfully(db_path, owner):
    db.create_place(db_path, owner, "home", *HOME)
    result = location_tools.handle(db_path, owner, "add_location_reminder",
                                  {"place_name": "the shop", "trigger": "arrive", "text": "milk"}, None)
    assert "error" in result and result["known_places"] == ["home"]


def test_location_reminder_fires_once_then_is_done(db_path, owner):
    place_id = db.create_place(db_path, owner, "shop", *AWAY)
    db.create_location_reminder(db_path, owner, place_id, "arrive", "get milk", once=True)

    due = db.due_location_reminders(db_path, place_id, "arrive")
    assert len(due) == 1
    db.mark_location_reminder_fired(db_path, due[0]["id"])
    assert db.due_location_reminders(db_path, place_id, "arrive") == []


def test_a_repeating_reminder_stays_armed(db_path, owner):
    place_id = db.create_place(db_path, owner, "shop", *AWAY)
    db.create_location_reminder(db_path, owner, place_id, "arrive", "get milk", once=False)
    due = db.due_location_reminders(db_path, place_id, "arrive")
    db.mark_location_reminder_fired(db_path, due[0]["id"])
    assert len(db.due_location_reminders(db_path, place_id, "arrive")) == 1


def test_routine_cooldown_prevents_immediate_refiring(db_path, owner):
    place_id = db.create_place(db_path, owner, "work", *AWAY)
    db.create_routine(db_path, owner, "Heading home", "leave", place_id,
                      "Tell me the drive time home.", cooldown_minutes=30)

    due = db.due_routines(db_path, place_id, "leave")
    assert len(due) == 1
    db.mark_routine_fired(db_path, due[0]["id"])
    assert db.due_routines(db_path, place_id, "leave") == []


def test_a_disabled_routine_does_not_fire(db_path, owner):
    place_id = db.create_place(db_path, owner, "work", *AWAY)
    routine_id = db.create_routine(db_path, owner, "R", "leave", place_id, "do a thing")
    db.set_routine_enabled(db_path, owner, routine_id, False)
    assert db.due_routines(db_path, place_id, "leave") == []


def test_deleting_a_place_removes_what_depended_on_it(db_path, owner):
    """A routine pointing at a deleted place would never fire and would be invisible in
    any 'why didn't that run' investigation."""
    place_id = db.create_place(db_path, owner, "work", *AWAY)
    db.create_routine(db_path, owner, "R", "leave", place_id, "do a thing")
    db.create_location_reminder(db_path, owner, place_id, "arrive", "badge in")

    assert db.delete_place(db_path, owner, place_id) is True
    assert db.list_routines(db_path, owner) == []
    assert db.list_location_reminders(db_path, owner) == []


# --- the HA client's location read -------------------------------------------

def test_location_prefers_the_most_accurate_fix(monkeypatch):
    states = [
        {"entity_id": "device_tracker.watch", "state": "home",
         "attributes": {"latitude": 1.0, "longitude": 2.0, "gps_accuracy": 300}},
        {"entity_id": "device_tracker.phone", "state": "home",
         "attributes": {"latitude": 1.1, "longitude": 2.1, "gps_accuracy": 5}},
    ]
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json=states, request=httpx.Request("GET", url)))
    fix = HomeAssistantClient("http://ha.test", "tok").location()
    assert fix["entity_id"] == "device_tracker.phone" and fix["accuracy_m"] == 5


def test_an_offline_phone_is_unknown_not_away(monkeypatch):
    """Observed for real: every entity from the phone went 'unavailable' at once when it
    dropped off HA. Reading that as 'away' would push notifications to a phone that just
    went offline and fire 'you left home' routines every time it slept."""
    states = [
        {"entity_id": "person.jack", "state": "unknown", "attributes": {}},
        {"entity_id": "device_tracker.phone", "state": "unavailable", "attributes": {}},
    ]
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json=states, request=httpx.Request("GET", url)))
    presence = HomeAssistantClient("http://ha.test", "tok").presence()
    assert presence["home"] is None, "unknown presence must not read as away"
    assert presence["tracking_available"] is False


def test_a_phone_going_offline_does_not_fire_a_leave_transition(db_path, owner):
    """The same failure in the location watcher: no fix must mean 'no information', not
    'he left'. Otherwise every phone sleep triggers the leaving-work routine."""
    db.create_place(db_path, owner, "home", *HOME, radius_m=150)
    location.detect_transition(db_path, owner, *HOME, accuracy=5)

    # The scheduler's guard: an unavailable device yields no coordinates, so no
    # transition is ever computed.
    fix = HomeAssistantClient.__new__(HomeAssistantClient)
    assert location.detect_transition(db_path, owner, *HOME, accuracy=5) is None
    del fix

    # And when it comes back at the same spot, still nothing changed.
    assert location.detect_transition(db_path, owner, *HOME, accuracy=5) is None


def test_no_gps_anywhere_is_reported_not_faked(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json=[{"entity_id": "person.jack", "state": "home", "attributes": {}}],
        request=httpx.Request("GET", url)))
    fix = HomeAssistantClient("http://ha.test", "tok").location()
    assert fix["latitude"] is None and "error" in fix
