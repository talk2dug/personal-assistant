"""Covers core/home_state.py -- the room-shaped view of the house the Command Center's
Presence & Sensors panel is built on.

The thing actually worth pinning here is honesty about provenance. Room presence is an
INFERENCE (a light being on is not an occupancy sensor), and the panel is only safe to
trust because the snapshot says which kind of evidence produced the answer. If `source`
ever silently starts reporting "motion" for a guess, the UI's dotted-underline
"inferred" marker disappears and the board starts stating guesses as fact -- so the
ranking and the source label are tested directly rather than through the UI.
"""
from assistant.core import home_state


def _state(entity_id, state, changed="2026-09-14T20:00:00+00:00", **attrs):
    return {
        "entity_id": entity_id,
        "state": state,
        "last_changed": changed,
        "attributes": attrs,
    }


class FakeClient:
    def __init__(self, states, fail=None):
        self._states = states
        self._fail = fail
        self.calls = 0

    def raw_states(self, max_age_seconds=4.0):
        self.calls += 1
        if self._fail:
            raise self._fail
        return self._states


class FakeContext:
    """Mirrors core/setup.py's HomeAssistantContext, which is what app.state actually
    holds -- the client is behind .mcp_client, not the object itself."""
    def __init__(self, client):
        self.mcp_client = client


HOME = [_state("person.jack_swayze", "home")]


# --- availability ------------------------------------------------------------

def test_no_home_assistant_is_data_not_an_exception():
    snap = home_state.snapshot(None, cameras=[])
    assert snap["available"] is False
    assert "not configured" in snap["error"]
    assert snap["rooms"] == []


def test_unreachable_home_assistant_still_returns_the_camera_block():
    """A dead HA must not take the panel down with it -- the cameras come from our own
    database and are still knowable."""
    client = FakeClient([], fail=RuntimeError("connection refused"))
    snap = home_state.snapshot(client, cameras=[{"key": "kitchen", "name": "Kitchen", "enabled": 1}])
    assert snap["available"] is False
    assert "connection refused" in snap["error"]
    assert snap["cameras"]["total"] == 1
    assert snap["cameras"]["enabled"] == 1


def test_accepts_the_context_wrapper_as_well_as_a_bare_client():
    """app.state.home_assistant is a HomeAssistantContext; scripts and tests hold the
    raw client. Both have to work or the route breaks in production only."""
    client = FakeClient(HOME)
    assert home_state.snapshot(FakeContext(client))["available"] is True
    assert home_state.snapshot(client)["available"] is True


# --- presence ----------------------------------------------------------------

def test_away_reports_where_from_the_phones_geocoded_location():
    snap = home_state.snapshot(FakeClient([
        _state("person.jack_swayze", "not_home"),
        _state("sensor.upstream_geocoded_location", "2802 North Ave\nRichmond, VA"),
    ]))
    presence = snap["presence"]
    assert presence["home"] is False
    assert presence["label"] == "2802 North Ave"
    assert presence["source"] == "away"


def test_no_tracker_at_all_is_unknown_not_away():
    """'unknown' means the phone dropped off HA, which happens routinely. Reporting that
    as away is what made the notification policy push to a phone that had gone to
    sleep -- the same distinction home_assistant_client.presence() already draws."""
    snap = home_state.snapshot(FakeClient([_state("person.jack_swayze", "unknown")]))
    assert snap["presence"]["home"] is None
    assert snap["presence"]["label"] == "Unknown"


def test_home_with_nothing_on_admits_the_room_is_unknown():
    snap = home_state.snapshot(FakeClient(HOME))
    presence = snap["presence"]
    assert presence["home"] is True
    assert presence["room"] is None
    assert presence["source"] == "none"
    assert "room unknown" in presence["detail"]


def test_a_light_being_on_infers_the_room_and_says_that_it_inferred_it():
    snap = home_state.snapshot(FakeClient(HOME + [_state("light.kitchen_stove_1", "on")]))
    presence = snap["presence"]
    assert presence["room"] == "Kitchen"
    assert presence["source"] == "activity"
    assert presence["detail"].startswith("inferred from")


def test_a_real_motion_sensor_outranks_any_amount_of_switched_on_lights():
    """Evidence quality beats recency: a bedroom motion sensor tripped an hour ago still
    beats a kitchen light someone left on a minute ago, because one of them is actually
    a person detector."""
    snap = home_state.snapshot(FakeClient(HOME + [
        _state("light.kitchen_stove_1", "on", changed="2026-09-14T23:59:00+00:00"),
        _state("binary_sensor.bedroom_dot_motion", "on", changed="2026-09-14T23:00:00+00:00"),
    ]))
    presence = snap["presence"]
    assert presence["room"] == "Bedroom"
    assert presence["source"] == "motion"
    assert presence["detail"] == "motion"


def test_among_equal_evidence_the_most_recently_changed_room_wins():
    snap = home_state.snapshot(FakeClient(HOME + [
        _state("light.kitchen_stove_1", "on", changed="2026-09-14T20:00:00+00:00"),
        _state("media_player.living_room_dot", "playing", changed="2026-09-14T23:30:00+00:00"),
    ]))
    assert snap["presence"]["room"] == "Living Room"


def test_an_unavailable_entity_is_not_evidence_of_anything():
    snap = home_state.snapshot(FakeClient(HOME + [
        _state("media_player.office_dot", "unavailable"),
    ]))
    assert snap["presence"]["room"] is None


# --- rooms -------------------------------------------------------------------

def test_rooms_carry_temperature_and_never_leak_the_ranking_timestamp():
    """_changed is an internal sort key. It is a datetime, so leaving it in the payload
    would blow up JSON serialisation at the route boundary."""
    snap = home_state.snapshot(FakeClient(HOME + [
        _state("sensor.bedroom_dot_temperature", "76.1"),
    ]))
    bedroom = next(r for r in snap["rooms"] if r["key"] == "bedroom")
    assert bedroom["temp_f"] == 76.1
    assert "_changed" not in bedroom
    assert all("_changed" not in room for room in snap["rooms"])


def test_a_room_with_no_sensors_reports_nothing_rather_than_a_default():
    snap = home_state.snapshot(FakeClient(HOME))
    laundry = next(r for r in snap["rooms"] if r["key"] == "laundry")
    assert laundry["temp_f"] is None
    assert laundry["motion_live"] is False
    assert laundry["activity_live"] is False


# --- the range ---------------------------------------------------------------

def test_range_offline_when_it_reports_nothing():
    snap = home_state.snapshot(FakeClient(HOME))
    assert snap["range"]["available"] is False
    assert snap["range"]["status"] == "offline"


def test_range_powered_and_responsive_is_ready_not_cooking():
    """operating_state 'run' only means the range is awake. Treating that as cooking
    would light the kitchen tile amber every hour of every day."""
    snap = home_state.snapshot(FakeClient(HOME + [
        _state("sensor.kitchen_range_operating_state", "run"),
        _state("sensor.kitchen_range_machine_state", "ready"),
        _state("sensor.kitchen_range_job_state", "ready"),
    ]))
    assert snap["range"]["status"] == "ready"


def test_range_with_a_live_job_is_cooking_and_leads_with_its_temperature():
    snap = home_state.snapshot(FakeClient(HOME + [
        _state("sensor.kitchen_range_operating_state", "run"),
        _state("sensor.kitchen_range_machine_state", "run"),
        _state("sensor.kitchen_range_job_state", "preheat"),
        _state("sensor.kitchen_range_temperature", "350"),
        _state("binary_sensor.kitchen_range_door", "on"),
    ]))
    r = snap["range"]
    assert r["status"] == "cooking"
    assert r["headline"] == "350°F"
    assert r["temperature_f"] == 350.0
    assert r["door_open"] is True


def test_a_ready_range_that_is_still_hot_says_so():
    """Residual heat is worth surfacing -- the oven is off but the door is still not
    somewhere to put your hand."""
    snap = home_state.snapshot(FakeClient(HOME + [
        _state("sensor.kitchen_range_machine_state", "ready"),
        _state("sensor.kitchen_range_operating_state", "run"),
        _state("sensor.kitchen_range_job_state", "ready"),
        _state("sensor.kitchen_range_temperature", "175"),
    ]))
    assert snap["range"]["headline"] == "Warm · 175°F"


def test_unknown_numeric_readings_do_not_become_zero():
    """HA reports 'unknown' for a setpoint that was never set. A dashboard showing 0°F
    would be stating something false."""
    snap = home_state.snapshot(FakeClient(HOME + [
        _state("sensor.kitchen_range_machine_state", "ready"),
        _state("sensor.kitchen_range_setpoint", "unknown"),
        _state("sensor.kitchen_range_temperature", "unknown"),
    ]))
    assert snap["range"]["setpoint_f"] is None
    assert snap["range"]["temperature_f"] is None


# --- cameras -----------------------------------------------------------------

def test_disabled_cameras_are_counted_separately_from_registered_ones():
    snap = home_state.snapshot(FakeClient(HOME), cameras=[
        {"key": "kitchen", "name": "Kitchen", "location": "kitchen", "enabled": 1},
        {"key": "garage", "name": "Garage", "location": "garage", "enabled": 0},
    ])
    assert snap["cameras"]["total"] == 2
    assert snap["cameras"]["enabled"] == 1
    assert [c["enabled"] for c in snap["cameras"]["list"]] == [True, False]
