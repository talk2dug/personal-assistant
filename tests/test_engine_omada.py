"""Verifies the confirmation gate for the Omada network integration: reboot_omada_device
actually interrupts real network service, so it must never reach the real OmadaClient
except through an explicit user confirmation. The read tools (list/health) are NOT gated.
Mirrors test_engine_git_ops.py/test_engine_kroger.py's suites.
"""
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


class FakeOmadaClient:
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


def make_omada():
    mcp = FakeOmadaClient()
    sensitive = {"reboot_omada_device"}
    return engine.OmadaContext(mcp_client=mcp, omada_tools=[
        {"type": "function", "function": {"name": n, "parameters": {}}}
        for n in ("list_omada_devices", "list_omada_clients", "get_omada_network_health", "reboot_omada_device")
    ], sensitive_tools=sensitive), mcp


def test_reboot_creates_a_pending_action_not_a_real_reboot(db_path, owner_id):
    omada, mcp = make_omada()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "reboot_omada_device", "arguments": {"mac": "AA:BB"}}}
        ]},
        {"role": "assistant", "content": "That will reboot the front AP — confirm?"},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "reboot the front AP", omada=omada)

    assert mcp.calls == [], "a real reboot must never happen before explicit confirmation"
    pending = db.get_pending_action(db_path, owner_id)
    assert pending is not None
    assert pending["tool_name"] == "reboot_omada_device"
    assert "confirm" in reply.lower()


def test_confirming_executes_the_real_reboot(db_path, owner_id):
    omada, mcp = make_omada()
    db.create_pending_action(db_path, owner_id, "reboot_omada_device", {"mac": "AA:BB"})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "yes", omada=omada)

    assert mcp.calls == [("reboot_omada_device", {"mac": "AA:BB"})]
    assert db.get_pending_action(db_path, owner_id) is None
    assert "done" in reply.lower()


def test_cancelling_never_reboots(db_path, owner_id):
    omada, mcp = make_omada()
    db.create_pending_action(db_path, owner_id, "reboot_omada_device", {"mac": "AA:BB"})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "no", omada=omada)

    assert mcp.calls == []
    assert db.get_pending_action(db_path, owner_id) is None
    assert "cancel" in reply.lower()


@pytest.mark.parametrize("tool_name,arguments", [
    ("list_omada_devices", {}),
    ("list_omada_clients", {}),
    ("get_omada_network_health", {}),
])
def test_read_only_omada_tools_are_not_gated(db_path, owner_id, tool_name, arguments):
    omada, mcp = make_omada()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": tool_name, "arguments": arguments}}
        ]},
        {"role": "assistant", "content": "Here you go."},
    ])

    engine.handle_message(db_path, llm, owner_id, "what's on the network", omada=omada)

    assert len(mcp.calls) == 1 and mcp.calls[0] == (tool_name, arguments)
    assert db.get_pending_action(db_path, owner_id) is None


def test_omada_tools_absent_when_no_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])
    reply = engine.handle_message(db_path, llm, owner_id, "hi", omada=None)
    assert reply == "Hi there"


def test_a_pending_omada_action_is_detected_with_no_other_context_configured(db_path, owner_id):
    """handle_message's early pending-action check must include omada, or a confirmation
    reply sent while no other context exists would fall through to a normal turn instead
    of resolving the confirmation."""
    omada, mcp = make_omada()
    db.create_pending_action(db_path, owner_id, "reboot_omada_device", {"mac": "CC:DD"})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "yes", omada=omada)

    assert mcp.calls == [("reboot_omada_device", {"mac": "CC:DD"})]
    assert "done" in reply.lower()


def test_select_tools_includes_omada_tools_when_configured():
    omada, _ = make_omada()
    tools = engine.select_tools("anything", omada=omada, route=False)
    names = {t["function"]["name"] for t in tools}
    assert names.issuperset(
        {"list_omada_devices", "list_omada_clients", "get_omada_network_health", "reboot_omada_device"})


def test_select_tools_omits_omada_tools_when_not_configured():
    tools = engine.select_tools("anything", omada=None, route=False)
    names = {t["function"]["name"] for t in tools}
    assert "reboot_omada_device" not in names


def test_build_system_prompt_includes_omada_note_when_configured():
    omada, _ = make_omada()
    prompt = engine.build_system_prompt("America/New_York", omada=omada)
    assert "reboot_omada_device" in prompt


def test_build_system_prompt_omits_omada_note_when_not_configured():
    prompt = engine.build_system_prompt("America/New_York", omada=None)
    assert "reboot_omada_device" not in prompt
