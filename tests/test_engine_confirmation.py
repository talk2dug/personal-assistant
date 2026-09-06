"""Verifies the confirmation gate: a sensitive Era tool call must never reach
mcp_client.call_tool except through an explicit user confirmation."""
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


class FakeMCPClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"is_error": False, "content": ["ok"]}


class FakeLLM:
    """Returns canned responses in order; ignores the actual messages/tools passed in."""

    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


ERA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "billing__upgrade",
            "description": "Upgrade the subscription.",
            "parameters": {"type": "object", "properties": {"target_plan": {"type": "string"}}},
        },
    }
]


def make_era(sensitive=True):
    mcp = FakeMCPClient()
    sensitive_tools = {"billing__upgrade"} if sensitive else set()
    return engine.EraContext(mcp_client=mcp, era_tools=ERA_TOOLS, sensitive_tools=sensitive_tools), mcp


def test_sensitive_tool_call_creates_pending_action_not_a_real_call(db_path, owner_id):
    era, mcp = make_era()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "billing__upgrade", "arguments": {"target_plan": "automate"}}}
        ]},
        {"role": "assistant", "content": "I'd like to upgrade you to automate — confirm?"},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "upgrade my plan", era=era)

    assert mcp.calls == []  # never actually called
    pending = db.get_pending_action(db_path, owner_id)
    assert pending is not None
    assert pending["tool_name"] == "billing__upgrade"
    assert "confirm" in reply.lower()


def test_confirming_executes_the_real_call(db_path, owner_id):
    era, mcp = make_era()
    db.create_pending_action(db_path, owner_id, "billing__upgrade", {"target_plan": "automate"})
    llm = FakeLLM([])  # keyword match handles "yes", no LLM call needed

    reply = engine.handle_message(db_path, llm, owner_id, "yes", era=era)

    assert mcp.calls == [("billing__upgrade", {"target_plan": "automate"})]
    assert db.get_pending_action(db_path, owner_id) is None
    assert "done" in reply.lower()


def test_cancelling_never_calls_era(db_path, owner_id):
    era, mcp = make_era()
    db.create_pending_action(db_path, owner_id, "billing__upgrade", {"target_plan": "automate"})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "no", era=era)

    assert mcp.calls == []
    assert db.get_pending_action(db_path, owner_id) is None
    assert "cancel" in reply.lower()


def test_ambiguous_reply_leaves_action_pending_and_does_not_call_era(db_path, owner_id):
    era, mcp = make_era()
    db.create_pending_action(db_path, owner_id, "billing__upgrade", {"target_plan": "automate"})
    llm = FakeLLM([{"role": "assistant", "content": "unclear"}])

    reply = engine.handle_message(db_path, llm, owner_id, "maybe later idk", era=era)

    assert mcp.calls == []
    assert db.get_pending_action(db_path, owner_id) is not None  # still awaiting
    assert "yes or no" in reply.lower()


def test_non_sensitive_era_tool_calls_directly(db_path, owner_id):
    era, mcp = make_era(sensitive=False)
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "billing__upgrade", "arguments": {"target_plan": "automate"}}}
        ]},
        {"role": "assistant", "content": "Upgraded you."},
    ])

    engine.handle_message(db_path, llm, owner_id, "upgrade my plan", era=era)

    assert mcp.calls == [("billing__upgrade", {"target_plan": "automate"})]
    assert db.get_pending_action(db_path, owner_id) is None


def test_era_tools_absent_when_no_era_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])

    reply = engine.handle_message(db_path, llm, owner_id, "hi", era=None)

    assert reply == "Hi there"
