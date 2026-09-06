"""Personal tools (personal_tools.py's create_personal_task/list_personal_projects/etc.)
mirror business_tools.py's no-gate reasoning: every one writes only to Jarvis's own
database, so unlike Kroger/CCXT/LetterStream/Era they must execute directly, never through
the pending_actions confirmation gate.
"""
import pytest

from assistant.core import db, engine
from assistant.core.personal_tools import PERSONAL_TOOLS


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
        return {"ok": True}


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


def make_personal():
    mcp = FakeMCPClient()
    return engine.PersonalContext(mcp_client=mcp), mcp


def test_personal_tool_executes_directly_not_gated(db_path, owner_id):
    personal, mcp = make_personal()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "create_personal_task", "arguments": {"text": "book the movers"}}}
        ]},
        {"role": "assistant", "content": "Noted — added to your to-dos."},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "remind me to book the movers", personal=personal)

    assert mcp.calls == [("create_personal_task", {"text": "book the movers"})]
    assert db.get_pending_action(db_path, owner_id) is None
    assert "noted" in reply.lower()


def test_personal_tools_absent_when_no_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])
    reply = engine.handle_message(db_path, llm, owner_id, "hi", personal=None)
    assert reply == "Hi there"


def test_select_tools_includes_personal_tools_when_configured():
    personal, _ = make_personal()
    tools = engine.select_tools("anything at all", personal=personal, route=False)
    names = {t["function"]["name"] for t in tools}
    assert names.issuperset({t["function"]["name"] for t in PERSONAL_TOOLS})


def test_select_tools_omits_personal_tools_when_not_configured():
    tools = engine.select_tools("anything at all", personal=None, route=False)
    names = {t["function"]["name"] for t in tools}
    assert "create_personal_task" not in names


def test_build_system_prompt_includes_personal_note_when_configured():
    personal, _ = make_personal()
    prompt = engine.build_system_prompt("America/New_York", personal=personal)
    assert "personal projects" in prompt.lower() or "personal to-do" in prompt.lower() or "request_personal_research" in prompt


def test_build_system_prompt_omits_personal_note_when_not_configured():
    prompt = engine.build_system_prompt("America/New_York", personal=None)
    assert "request_personal_research" not in prompt
