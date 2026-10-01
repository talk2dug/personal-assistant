"""touch_panel: room grouping from HA areas, the control whitelist, and next_up."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from assistant.core import touch_panel


class FakeHA:
    base_url = "http://ha"
    _headers = {"Authorization": "Bearer t"}

    def __init__(self, states):
        self.states = states
        self.calls = []
        self._states_cache = "cached"

    def raw_states(self):
        return self.states

    def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service, entity_id, data))
        return {"ok": True, "result": [{}]}


def _s(eid, state="on", **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs}


@pytest.fixture
def house(monkeypatch):
    states = [
        _s("light.bed_1", brightness=128, supported_color_modes=["color_temp", "hs"], friendly_name="Bed 1"),
        _s("light.bed_2", "off", supported_color_modes=["onoff"]),
        _s("light.bedroom_lights", entity_id=["light.bed_1", "light.bed_2"]),
        _s("switch.wp3_5_2_socket_1"),                 # no area: home_state.ROOMS puts it in the kitchen
        _s("switch.bench"),                            # no area, no fallback
        _s("switch.plug_child_lock", "off"),           # config entity: skipped
        _s("lock.front_door", "locked"),               # never on the panel
        _s("media_player.dot", "unavailable"),         # offline speaker: clutter
        _s("scene.bedtime", "unknown", friendly_name="Bedtime"),
    ]
    monkeypatch.setattr(touch_panel, "_registry", lambda c: {
        "area": {"light.bed_1": "Bedroom", "light.bed_2": "Bedroom"},
        "skip": {"switch.plug_child_lock"}})
    return FakeHA(states)


def test_rooms_group_by_area_with_fallbacks(house):
    view = touch_panel.rooms(house, {"lock"})
    rooms = {r["name"]: [t["entity_id"] for t in r["tiles"]] for r in view["rooms"]}
    # The group has no area of its own; it goes where its members are, and first.
    assert rooms["Bedroom"] == ["light.bedroom_lights", "light.bed_1", "light.bed_2"]
    assert rooms["Kitchen"] == ["switch.wp3_5_2_socket_1"]
    assert rooms["Other"] == ["switch.bench"]
    assert [r["name"] for r in view["rooms"]][-1] == "Other"
    everything = {t for ts in rooms.values() for t in ts}
    assert not everything & {"switch.plug_child_lock", "lock.front_door", "media_player.dot"}
    assert [s["entity_id"] for s in view["scenes"]] == ["scene.bedtime"]
    bed = next(r for r in view["rooms"] if r["name"] == "Bedroom")
    assert bed["on"] == 1  # the group isn't double counted
    b1 = next(t for t in bed["tiles"] if t["entity_id"] == "light.bed_1")
    assert b1["brightness_pct"] == 50 and b1["color"] and b1["color_temp"]


def test_control_passes_whitelisted_calls_and_drops_cache(house):
    touch_panel.control(house, "light.bed_1", "turn_on", {"brightness_pct": 40, "hs_color": [10, 90]})
    assert house.calls == [("light", "turn_on", ["light.bed_1"], {"brightness_pct": 40, "hs_color": [10, 90]})]
    assert house._states_cache is None


@pytest.mark.parametrize("eid,service,data", [
    ("lock.front_door", "unlock", {}),              # sensitive domain
    ("cover.garage", "open_cover", {}),              # not a panel domain at all
    ("light.bed_1", "reload", {}),                   # not a whitelisted service
    ("switch.bench", "turn_on", {"entity_id": "lock.front_door"}),  # smuggled target
    ("nonsense", "turn_on", {}),
])
def test_control_refuses_off_list(house, eid, service, data):
    with pytest.raises(touch_panel.PanelError):
        touch_panel.control(house, eid, service, data, {"lock", "cover"})
    assert house.calls == []


def test_room_off_turns_off_only_what_is_on(house):
    touch_panel.room_off(house, "Bedroom")
    assert house.calls == [("light", "turn_off", ["light.bedroom_lights", "light.bed_1"], None)]


def test_next_up_prefers_upcoming_timed_event(monkeypatch):
    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 9, 30, 10, 0, tzinfo=tz)

    def fake_upcoming(*a, **k):
        return {"days": [
            {"date": "2026-09-30", "entries": [
                {"kind": "event", "date": "2026-09-30", "title": "Standup", "detail": "09:00 · Teams", "done": False},
                {"kind": "task", "date": "2026-09-30", "title": "Pay rent", "detail": None, "done": False},
                {"kind": "event", "date": "2026-09-30", "title": "Dentist", "detail": "14:30", "done": False},
            ]},
        ]}
    from assistant.core import agenda
    monkeypatch.setattr(agenda, "upcoming", fake_upcoming)
    got = touch_panel.next_up("db", 1, "America/New_York", now=now)
    assert got == {"title": "Dentist", "kind": "event", "day": "Today", "time": "2:30 PM", "detail": None}


def test_home_room_comes_from_the_camera_on_the_same_ip(tmp_path):
    import sqlite3
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cameras (name TEXT, location TEXT, url TEXT, enabled INTEGER)")
    conn.executemany("INSERT INTO cameras VALUES (?,?,?,1)", [
        ("Bedroom", "bedroom", "http://192.168.0.135:8081/"),
        ("Laundry Room", "laundry room", "http://192.168.0.134:8081/")])
    conn.commit(); conn.close()
    rooms = ["Bathroom", "Bedroom", "Kitchen"]
    assert touch_panel.home_room(db, "192.168.0.135", rooms) == "Bedroom"
    assert touch_panel.home_room(db, "192.168.0.134", rooms) is None   # no HA area by that name
    assert touch_panel.home_room(db, "192.168.0.13", rooms) is None    # no prefix matches
    assert touch_panel.home_room(db, None, rooms) is None
