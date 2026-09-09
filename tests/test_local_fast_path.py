"""local_fast_path.try_home_assistant_fast_path -- the local-first fast path for simple
Home Assistant commands. Tested against a fake local LLM client and a fake
HomeAssistantContext/client, since the real behavior (never guess, never fall through
after a real action, never touch sensitive domains) has to hold regardless of which
local model is actually configured.
"""
import json

import pytest

from assistant.core import db, engine, local_fast_path


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


class FakeHAClient:
    def __init__(self, entities=None, call_service_result=None, raise_on_call=False):
        self.calls = []
        self._entities = entities if entities is not None else [
            {"entity_id": "light.kitchen_stove_1", "name": "Kitchen Stove 1", "state": "off"},
            {"entity_id": "light.kitchen_stove_2", "name": "Kitchen stove 2", "state": "off"},
        ]
        self._call_service_result = call_service_result or {"ok": True}
        self._raise_on_call = raise_on_call

    def list_entities(self, domain=None):
        return {"entities": self._entities}

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._raise_on_call:
            raise RuntimeError("HA unreachable")
        if name == "call_service":
            return self._call_service_result
        return {"error": f"unexpected tool {name}"}


def make_ha(sensitive_domains=("lock", "cover", "alarm_control_panel"), **kwargs):
    return engine.HomeAssistantContext(mcp_client=FakeHAClient(**kwargs), sensitive_domains=set(sensitive_domains))


class FakeLocalLLM:
    """responses is a list of ollama-shaped message dicts, consumed in order -- the
    first for the tool-decision turn, the second (if reached) for the reply-phrasing
    turn."""

    def __init__(self, responses, raise_on_call=False):
        self._responses = list(responses)
        self.raise_on_call = raise_on_call
        self.calls = []

    def chat(self, messages, tools=None, think=False):
        self.calls.append({"messages": messages, "tools": tools})
        if self.raise_on_call:
            raise RuntimeError("local model unreachable")
        return self._responses.pop(0)


def _tool_call_message(name, arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def _text_message(content):
    return {"role": "assistant", "content": content}


# --- bail-out cases (safe to fall through, no side effects) ----------------------

def test_returns_none_when_local_llm_is_not_configured(db_path, owner_id):
    ha = make_ha()
    result = local_fast_path.try_home_assistant_fast_path(None, ha, db_path, owner_id, "turn on the kitchen lights")
    assert result is None


def test_returns_none_when_home_assistant_is_not_configured(db_path, owner_id):
    llm = FakeLocalLLM([])
    result = local_fast_path.try_home_assistant_fast_path(llm, None, db_path, owner_id, "turn on the kitchen lights")
    assert result is None


def test_returns_none_for_a_message_with_no_ha_keywords(db_path, owner_id):
    """The cheap bail-out: an unrelated message never even reaches the local model."""
    ha = make_ha()
    llm = FakeLocalLLM([])
    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "what's on my calendar today")
    assert result is None
    assert llm.calls == []


def test_returns_none_when_the_model_makes_no_tool_call(db_path, owner_id):
    ha = make_ha()
    llm = FakeLocalLLM([_text_message("I'm not sure which light you mean.")])
    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "turn on the light")
    assert result is None


def test_returns_none_when_the_model_makes_multiple_tool_calls(db_path, owner_id):
    ha = make_ha()
    message = {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "call_service", "arguments": {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen_stove_1"}}},
        {"function": {"name": "call_service", "arguments": {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen_stove_2"}}},
    ]}
    llm = FakeLocalLLM([message])
    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "turn on one light and off the other")
    assert result is None


def test_returns_none_for_an_unknown_tool_name(db_path, owner_id):
    ha = make_ha()
    llm = FakeLocalLLM([_tool_call_message("delete_everything", {})])
    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "turn on the light")
    assert result is None


def test_returns_none_and_never_dispatches_a_sensitive_domain(db_path, owner_id):
    """Locks/covers/alarms always go through the main path's real confirmation
    conversation -- the fast path must not even attempt these."""
    ha = make_ha()
    llm = FakeLocalLLM([_tool_call_message("call_service", {
        "domain": "lock", "service": "unlock", "entity_id": "lock.front_door",
    })])

    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "unlock the front door")

    assert result is None
    assert ha.mcp_client.calls == []
    assert db.get_pending_action(db_path, owner_id) is None


def test_returns_none_when_the_local_model_call_raises(db_path, owner_id):
    ha = make_ha()
    llm = FakeLocalLLM([], raise_on_call=True)
    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "turn on the kitchen lights")
    assert result is None


def test_returns_none_when_entity_lookup_fails(db_path, owner_id):
    class BrokenHAClient(FakeHAClient):
        def list_entities(self, domain=None):
            raise RuntimeError("HA unreachable")

    ha = engine.HomeAssistantContext(mcp_client=BrokenHAClient(), sensitive_domains=set())
    llm = FakeLocalLLM([])
    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "turn on the kitchen lights")
    assert result is None


def test_returns_none_when_dispatch_itself_errors(db_path, owner_id):
    ha = make_ha(raise_on_call=True)
    llm = FakeLocalLLM([_tool_call_message("call_service", {
        "domain": "light", "service": "turn_on", "entity_id": "light.kitchen_stove_1",
    })])
    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "turn on the kitchen light")
    assert result is None


# --- the real success path --------------------------------------------------------

def test_dispatches_a_single_light_and_returns_a_deterministic_reply(db_path, owner_id):
    """call_service skips the reply-phrasing model call entirely -- every garbled
    reply seen live during testing happened on that second call, and there's no real
    information to phrase for a plain on/off action anyway."""
    ha = make_ha()
    llm = FakeLocalLLM([
        _tool_call_message("call_service", {
            "domain": "light", "service": "turn_on", "entity_id": "light.kitchen_stove_1",
        }),
    ])

    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "turn on the kitchen stove light")

    assert result == "Turned on, sir."
    assert ha.mcp_client.calls == [("call_service", {
        "domain": "light", "service": "turn_on", "entity_id": "light.kitchen_stove_1",
    })]
    # Exactly one model call for the whole attempt -- no chained second call.
    assert len(llm.calls) == 1


def test_batches_multiple_entities_into_one_real_dispatch(db_path, owner_id):
    """The actual fix this whole fast path exists to prove out: 'both kitchen lights'
    should reach call_service once with a list, not once per light."""
    ha = make_ha()
    llm = FakeLocalLLM([
        _tool_call_message("call_service", {
            "domain": "light", "service": "turn_on",
            "entity_id": ["light.kitchen_stove_1", "light.kitchen_stove_2"],
        }),
    ])

    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "turn on both kitchen stove lights")

    assert result == "Both turned on, sir."
    assert ha.mcp_client.calls == [("call_service", {
        "domain": "light", "service": "turn_on",
        "entity_id": ["light.kitchen_stove_1", "light.kitchen_stove_2"],
    })]


def test_falls_back_to_a_deterministic_reply_if_phrasing_fails_after_a_real_dispatch(db_path, owner_id):
    """Once a real action has happened, this must never return None again -- falling
    through to the main path after a real side effect risks it deciding the same thing
    over again and double-executing. Uses a read-only tool (get_entity_state) since
    call_service no longer makes a second call at all -- this covers the fallback for
    the tools that still do."""
    class ReadHAClient(FakeHAClient):
        def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return {"entity_id": "light.kitchen_stove_1", "state": "off", "attributes": {}}

    ha = engine.HomeAssistantContext(mcp_client=ReadHAClient(), sensitive_domains=set())
    llm = FakeLocalLLM([
        _tool_call_message("get_entity_state", {"entity_id": "light.kitchen_stove_1"}),
    ])
    original_chat = llm.chat
    call_count = {"n": 0}

    def flaky_chat(messages, tools=None, think=False):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return original_chat(messages, tools=tools, think=think)
        raise RuntimeError("connection dropped")

    llm.chat = flaky_chat

    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "is the kitchen stove light on")

    assert result is not None
    assert "Done" in result
    assert ha.mcp_client.calls == [("get_entity_state", {"entity_id": "light.kitchen_stove_1"})]


def test_discards_a_garbled_reply_and_uses_the_plain_fallback(db_path, owner_id):
    """Confirmed live: this local model occasionally leaks raw formatting into its
    reply content (one real run: "thought\\n<channel|>Successfully turned on both
    kitchen stove lights."). Exercised via get_entity_state -- call_service's own
    reply is deterministic now and never reaches this sanitizer at all."""
    class ReadHAClient(FakeHAClient):
        def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return {"entity_id": "light.kitchen_stove_1", "state": "off", "attributes": {}}

    ha = engine.HomeAssistantContext(mcp_client=ReadHAClient(), sensitive_domains=set())
    llm = FakeLocalLLM([
        _tool_call_message("get_entity_state", {"entity_id": "light.kitchen_stove_1"}),
        _text_message("thought\n<channel|>The kitchen stove light is off."),
    ])

    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "is the kitchen stove light on")

    assert result is not None
    assert "<" not in result and "|" not in result
    assert "Done" in result


def test_discards_a_bare_nonsense_token_reply_too(db_path, owner_id):
    """A second, different real failure mode: the model replied with a bare "1024" --
    no leaked markup at all, just not a real sentence. A per-character check alone (the
    <>| one above) would miss this."""
    class ReadHAClient(FakeHAClient):
        def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return {"entity_id": "light.kitchen_stove_1", "state": "off", "attributes": {}}

    ha = engine.HomeAssistantContext(mcp_client=ReadHAClient(), sensitive_domains=set())
    llm = FakeLocalLLM([
        _tool_call_message("get_entity_state", {"entity_id": "light.kitchen_stove_1"}),
        _text_message("1024"),
    ])

    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "is the kitchen stove light on")

    assert result is not None
    assert "Done" in result


# --- _deterministic_action_reply itself -------------------------------------------

@pytest.mark.parametrize("arguments,expected", [
    ({"domain": "light", "service": "turn_on", "entity_id": "light.kitchen_stove_1"}, "Turned on, sir."),
    ({"domain": "light", "service": "turn_off", "entity_id": "light.kitchen_stove_1"}, "Turned off, sir."),
    ({"domain": "light", "service": "turn_on", "entity_id": ["light.a", "light.b"]}, "Both turned on, sir."),
    ({"domain": "light", "service": "turn_off", "entity_id": ["light.a", "light.b", "light.c"]}, "All 3 turned off, sir."),
    ({"domain": "switch", "service": "toggle", "entity_id": "switch.fan"}, "Toggled, sir."),
    ({"domain": "climate", "service": "set_something_unknown", "entity_id": "climate.hall"}, "Done, sir."),
])
def test_deterministic_action_reply_shapes(arguments, expected):
    assert local_fast_path._deterministic_action_reply(arguments) == expected


def test_get_entity_state_style_read_only_call_also_works(db_path, owner_id):
    class ReadHAClient(FakeHAClient):
        def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return {"entity_id": "light.kitchen_stove_1", "state": "off", "attributes": {}}

    ha = engine.HomeAssistantContext(mcp_client=ReadHAClient(), sensitive_domains=set())
    llm = FakeLocalLLM([
        _tool_call_message("get_entity_state", {"entity_id": "light.kitchen_stove_1"}),
        _text_message("Kitchen Stove 1 is currently off, sir."),
    ])

    result = local_fast_path.try_home_assistant_fast_path(llm, ha, db_path, owner_id, "is the kitchen stove light on")

    assert result == "Kitchen Stove 1 is currently off, sir."
