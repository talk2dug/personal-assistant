"""Covers ha_config.py (saving scenes from live light state, validating and writing
automations through HA's config API) and its confirmation gate in engine.py.

httpx is replaced by a small in-memory Home Assistant that stores what the config API is
sent and then exposes it as an entity, the way the real one reloads after a write.
"""
import re

import httpx
import pytest

from assistant.core import business_db, db, engine, ha_config
from assistant.core.ha_config_tools import CONFIG_INTENT_RE
from assistant.core.home_assistant_client import HomeAssistantClient


class Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status_code, payload, text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


def light(eid, state="on", brightness=None, mode=None, **color):
    attrs = {"friendly_name": eid.split(".")[1]}
    if brightness is not None:
        attrs["brightness"] = brightness
    if mode:
        attrs["color_mode"] = mode
    attrs.update(color)
    return {"entity_id": eid, "state": state, "attributes": attrs}


class FakeHA:
    def __init__(self, states):
        self.states = states
        self.posts = []
        self.deletes = []
        self.services = [
            {"domain": "light", "services": {"turn_on": {}, "turn_off": {}}},
            {"domain": "scene", "services": {"turn_on": {}}},
            {"domain": "lock", "services": {"unlock": {}}},
        ]

    def get(self, url, headers=None, timeout=None):
        if url.endswith("/api/states"):
            return Resp(200, self.states)
        if url.endswith("/api/services"):
            return Resp(200, self.services)
        return Resp(404, {"message": "Resource not found"})

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        m = re.search(r"/api/config/(scene|automation)/config/(.+)$", url)
        if m:
            domain, cid = m.groups()
            name = json.get("name") or json.get("alias")
            self.states = [s for s in self.states if s.get("attributes", {}).get("id") != cid]
            attrs = {"id": cid, "friendly_name": name}
            if domain == "scene":
                attrs["entity_id"] = list(json["entities"])
            self.states.append({"entity_id": f"{domain}.{ha_config.slugify(name)}",
                                "state": "on" if domain == "automation" else "unknown", "attributes": attrs})
            return Resp(200, {"result": "ok"})
        return Resp(200, [])

    def delete(self, url, headers=None, timeout=None):
        self.deletes.append(url)
        return Resp(200, {"result": "ok"})


BEDROOM = [
    light("light.bedroom_1", "unavailable"),
    light("light.bedroom_3", "on", 128, "color_temp", color_temp_kelvin=2700),
    light("light.bedroom_4", "on", 26, "hs", hs_color=[240.0, 80.0]),
    light("light.bedroom_5", "off"),
    light("light.jacks_lamp", "on", 255, "color_temp", color_temp_kelvin=4000),
    {"entity_id": "person.jack_swayze", "state": "home", "attributes": {"friendly_name": "Jack"}},
    {"entity_id": "sun.sun", "state": "above_horizon", "attributes": {"elevation": 20}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {}},
]


@pytest.fixture
def ha(monkeypatch):
    fake = FakeHA([dict(s) for s in BEDROOM])
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(httpx, "post", fake.post)
    monkeypatch.setattr(httpx, "delete", fake.delete)
    return fake


@pytest.fixture
def client():
    return HomeAssistantClient("http://ha.local", "tok")


# ---------------------------------------------------------------------- scenes

def test_save_scene_captures_the_live_levels_of_the_rooms_lights(ha, client):
    out = client.call_tool("save_scene", {"name": "Bed TV Time", "room": "bedroom"})

    assert out["ok"] and out["entity_id"] == "scene.bed_tv_time"
    url, body = ha.posts[-1]
    assert url.endswith("/api/config/scene/config/jarvis_bed_tv_time")
    assert body["name"] == "Bed TV Time"
    assert body["entities"]["light.bedroom_3"] == {
        "state": "on", "brightness": 128, "color_mode": "color_temp", "color_temp_kelvin": 2700}
    assert body["entities"]["light.bedroom_4"]["hs_color"] == [240.0, 80.0]
    assert body["entities"]["light.bedroom_5"] == {"state": "off"}
    # An unavailable light is reported, never saved as "off".
    assert "light.bedroom_1" not in body["entities"]
    assert out["skipped_unavailable"] == ["light.bedroom_1"]
    assert "bedroom_3 on 50% 2700K" in out["captured"]


def test_an_existing_scene_name_is_not_overwritten_without_replace(ha, client):
    client.call_tool("save_scene", {"name": "Bed TV Time", "room": "bedroom"})
    ha.posts.clear()

    out = client.call_tool("save_scene", {"name": "bed tv time", "room": "bedroom"})
    assert out["exists"] is True and ha.posts == []

    out = client.call_tool("save_scene", {"name": "Bed TV Time", "room": "bedroom", "replace": True})
    assert out["ok"] and out["replaced"] is True
    assert ha.posts[-1][0].endswith("/jarvis_bed_tv_time")


def test_a_scene_may_never_carry_a_lock(ha, client):
    out = client.call_tool("save_scene", {"name": "Leaving", "entity_ids": ["light.jacks_lamp", "lock.front_door"]})
    assert "error" in out and ha.posts == []


def test_an_unknown_room_names_the_real_ones(ha, client):
    out = client.call_tool("save_scene", {"name": "x", "room": "garage"})
    assert "Bedroom" in out["error"]


# ----------------------------------------------------------------- automations

SUNSET = {
    "alias": "Sunset lights when home",
    "triggers": [{"platform": "sun", "event": "sunset", "offset": "-00:30:00"}],
    "conditions": [{"condition": "state", "entity_id": "person.jack_swayze", "state": "home"}],
    "actions": [{"service": "light.turn_on", "target": {"entity_id": ["light.jacks_lamp"]},
                 "data": {"brightness_pct": 60, "transition": 900}}],
}


def test_validation_names_every_missing_entity_and_service(ha, client):
    bad = {**SUNSET, "actions": [{"action": "light.fade_in", "target": {"entity_id": "light.nope"}}]}
    check = ha_config.validate_automation(client, bad)
    assert not check["ok"]
    assert "no such entity: light.nope" in check["errors"]
    assert "no such service: light.fade_in" in check["errors"]


def test_old_key_spellings_are_normalized_to_current_format(ha, client):
    check = ha_config.validate_automation(client, SUNSET)
    assert check["ok"], check["errors"]
    assert check["automation"]["triggers"][0]["trigger"] == "sun"
    assert check["automation"]["actions"][0]["action"] == "light.turn_on"


def test_sensitive_services_are_called_out(ha, client):
    unlock = {**SUNSET, "actions": [{"action": "lock.unlock", "target": {"entity_id": "lock.front_door"}}]}
    assert ha_config.validate_automation(client, unlock)["sensitive"] == ["lock.unlock"]


def test_create_automation_writes_through_the_config_api(ha, client):
    out = client.call_tool("create_automation", SUNSET)
    assert out["ok"] and out["entity_id"] == "automation.sunset_lights_when_home"
    url, body = ha.posts[-1]
    assert url.endswith("/api/config/automation/config/jarvis_sunset_lights_when_home")
    assert body["conditions"][0]["entity_id"] == "person.jack_swayze"


# ---------------------------------------------------------------- engine gate

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


def _ctx(client):
    return engine.HomeAssistantContext(mcp_client=client, sensitive_domains={"lock", "cover", "alarm_control_panel"})


def _dispatch(db_path, owner_id, ctx, name, args):
    import json
    return json.loads(engine._dispatch_tool_call(
        db_path, "America/New_York", owner_id, name, args, era=None, calendar=None, home_assistant=ctx))


def test_saving_a_scene_needs_no_confirmation(ha, client, db_path, owner_id):
    out = _dispatch(db_path, owner_id, _ctx(client), "save_scene", {"name": "Bed TV Time", "room": "bedroom"})
    assert out["ok"] and db.get_pending_action(db_path, owner_id) is None


def test_a_broken_automation_is_sent_back_not_staged(ha, client, db_path, owner_id):
    bad = {**SUNSET, "conditions": [{"condition": "state", "entity_id": "person.jack", "state": "home"}]}
    out = _dispatch(db_path, owner_id, _ctx(client), "create_automation", bad)
    assert "no such entity: person.jack" in out["problems"]
    assert db.get_pending_action(db_path, owner_id) is None


def test_a_valid_automation_waits_for_yes_then_is_written(ha, client, db_path, owner_id):
    ctx = _ctx(client)
    out = _dispatch(db_path, owner_id, ctx, "create_automation", SUNSET)
    assert out["status"] == "awaiting_confirmation"
    assert not any("/api/config/automation" in u for u, _ in ha.posts)

    reply = engine.handle_message(db_path, None, owner_id, "yes", home_assistant=ctx)

    assert "done" in reply.lower()
    assert any(u.endswith("/jarvis_sunset_lights_when_home") for u, _ in ha.posts)


def test_deleting_a_scene_waits_for_yes(ha, client, db_path, owner_id):
    client.call_tool("save_scene", {"name": "Bed TV Time", "room": "bedroom"})
    out = _dispatch(db_path, owner_id, _ctx(client), "delete_scene", {"entity_id": "scene.bed_tv_time"})
    assert out["status"] == "awaiting_confirmation" and ha.deletes == []


# ------------------------------------------------------------- fast-path guard

@pytest.mark.parametrize("text", [
    "take the current bedroom lights and call it Bed TV Time",
    "save these lights as a scene",
    "turn on the lights slowly when the sun starts going down, only when I'm home",
    "make an automation for the porch light",
])
def test_scene_and_automation_requests_skip_the_local_fast_path(text):
    assert CONFIG_INTENT_RE.search(text)


@pytest.mark.parametrize("text", ["turn on the bedroom lights", "dim the living room lamp to 30%"])
def test_plain_light_commands_still_use_the_fast_path(text):
    assert not CONFIG_INTENT_RE.search(text)
