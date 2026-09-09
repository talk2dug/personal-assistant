"""Verifies Home Assistant's domain-based confirmation gate: call_service against a
sensitive domain (lock/cover/alarm_control_panel) must never reach
HomeAssistantClient.call_tool except through explicit confirmation; other domains
(light, switch, climate...) call directly — mirrors test_engine_confirmation.py but
for dynamic per-call sensitivity instead of a fixed tool-name set."""
import pytest

from assistant.core import business_db, db, engine


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
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


def test_a_list_of_entities_dispatches_in_one_call_not_one_per_light(db_path, owner_id):
    """The actual latency fix: 'turn off the lights' (plural) should reach call_service
    ONCE with a list, not once per light -- each extra call is a full extra model turn."""
    ha, client = make_ha()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "call_service", "arguments": {
                "domain": "light", "service": "turn_off",
                "entity_id": ["light.living_room", "light.kitchen"],
            }}}
        ]},
        {"role": "assistant", "content": "Turned off the lights."},
    ])

    engine.handle_message(db_path, llm, owner_id, "turn off the lights", home_assistant=ha)

    assert client.calls == [("call_service", {
        "domain": "light", "service": "turn_off", "entity_id": ["light.living_room", "light.kitchen"],
    })]
    assert db.get_pending_action(db_path, owner_id) is None


def test_home_assistant_tools_absent_when_no_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])

    reply = engine.handle_message(db_path, llm, owner_id, "hi", home_assistant=None)

    assert reply == "Hi there"


# --- the local-first fast path, wired into handle_message itself ------------------

class FakeHAClientWithEntities(FakeHAClient):
    def list_entities(self, domain=None):
        return {"entities": [{"entity_id": "light.kitchen_stove_1", "name": "Kitchen Stove 1", "state": "off"}]}


class FakeLocalLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, tools=None, think=False):
        self.calls += 1
        return self._responses.pop(0)


def test_a_confident_local_fast_path_answers_without_ever_calling_the_main_llm(db_path, owner_id):
    """The whole point: when local_llm is configured and confident, the main (slow,
    cloud) backend is never even consulted for a simple HA command."""
    client = FakeHAClientWithEntities()
    ha = engine.HomeAssistantContext(mcp_client=client, sensitive_domains={"lock", "cover", "alarm_control_panel"})
    local_llm = FakeLocalLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "call_service", "arguments": {
                "domain": "light", "service": "turn_on", "entity_id": "light.kitchen_stove_1",
            }}}
        ]},
    ])

    class ExplodingMainLLM:
        def chat(self, *a, **k):
            raise AssertionError("the main LLM should never be called when the fast path succeeds")

    reply = engine.handle_message(
        db_path, ExplodingMainLLM(), owner_id, "turn on the kitchen stove light",
        home_assistant=ha, local_llm=local_llm,
    )

    assert reply == "Turned on, sir."
    assert client.calls == [("call_service", {
        "domain": "light", "service": "turn_on", "entity_id": "light.kitchen_stove_1",
    })]
    history = db.recent_messages(db_path, owner_id)
    assert history[-2] == {"role": "user", "content": "turn on the kitchen stove light"}
    assert history[-1] == {"role": "assistant", "content": "Turned on, sir."}


def test_an_unconfident_local_fast_path_falls_back_to_the_main_llm_unchanged(db_path, owner_id):
    """local_llm declining (no tool call) must fall through to exactly today's
    behavior -- one user message persisted, the main backend handles it normally."""
    client = FakeHAClientWithEntities()
    ha = engine.HomeAssistantContext(mcp_client=client, sensitive_domains=set())
    local_llm = FakeLocalLLM([{"role": "assistant", "content": "not sure"}])
    main_llm = FakeLLM([{"role": "assistant", "content": "Which light do you mean, sir?"}])

    reply = engine.handle_message(
        db_path, main_llm, owner_id, "turn on the light",
        home_assistant=ha, local_llm=local_llm,
    )

    assert reply == "Which light do you mean, sir?"
    assert client.calls == []
    history = db.recent_messages(db_path, owner_id)
    assert history[-2] == {"role": "user", "content": "turn on the light"}
    assert history[-1] == {"role": "assistant", "content": "Which light do you mean, sir?"}
    # Exactly one user message recorded, not two -- the fast path's own decline path
    # must not have persisted anything before falling through.
    assert sum(1 for m in history if m["content"] == "turn on the light") == 1
