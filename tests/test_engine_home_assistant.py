"""Verifies Home Assistant's domain-based confirmation gate: call_service against a
sensitive domain (lock/cover/alarm_control_panel) must never reach
HomeAssistantClient.call_tool except through explicit confirmation; other domains
(light, switch, climate...) call directly — mirrors test_engine_confirmation.py but
for dynamic per-call sensitivity instead of a fixed tool-name set."""
import pytest

from assistant.core import db, engine


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


class FakeHAClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True}


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


def make_ha():
    client = FakeHAClient()
    ha = engine.HomeAssistantContext(mcp_client=client, sensitive_domains={"lock", "cover", "alarm_control_panel"})
    return ha, client


def test_lock_domain_creates_pending_action_not_a_real_call(db_path, owner_id):
    ha, client = make_ha()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "call_service", "arguments": {
                "domain": "lock", "service": "unlock", "entity_id": "lock.front_door",
            }}}
        ]},
        {"role": "assistant", "content": "I'd like to unlock the front door — confirm?"},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "unlock the front door", home_assistant=ha)

    assert client.calls == []
    pending = db.get_pending_action(db_path, owner_id)
    assert pending is not None
    assert pending["tool_name"] == "call_service"
    assert "confirm" in reply.lower()


def test_confirming_unlocks_for_real(db_path, owner_id):
    ha, client = make_ha()
    db.create_pending_action(db_path, owner_id, "call_service", {
        "domain": "lock", "service": "unlock", "entity_id": "lock.front_door",
    })
    llm = FakeLLM([])  # keyword match handles "yes"

    reply = engine.handle_message(db_path, llm, owner_id, "yes", home_assistant=ha)

    assert client.calls == [("call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"})]
    assert db.get_pending_action(db_path, owner_id) is None
    assert "done" in reply.lower()


def test_light_domain_calls_directly_without_confirmation(db_path, owner_id):
    ha, client = make_ha()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "call_service", "arguments": {
                "domain": "light", "service": "turn_on", "entity_id": "light.living_room",
            }}}
        ]},
        {"role": "assistant", "content": "Turned on the living room light."},
    ])

    engine.handle_message(db_path, llm, owner_id, "turn on the living room light", home_assistant=ha)

    assert client.calls == [("call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.living_room"})]
    assert db.get_pending_action(db_path, owner_id) is None


def test_home_assistant_tools_absent_when_no_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])

    reply = engine.handle_message(db_path, llm, owner_id, "hi", home_assistant=None)

    assert reply == "Hi there"
