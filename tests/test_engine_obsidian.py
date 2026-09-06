"""Verifies Obsidian tools reach ObsidianContext.mcp_client.call_tool with no
confirmation gate (unlike Era/phone/mail's sensitive tools — local vault writes
aren't gated, per the user's explicit choice for proactive capture)."""
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


class FakeObsidianClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True, "new": True}


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


def test_write_note_calls_directly_without_confirmation(db_path, owner_id):
    client = FakeObsidianClient()
    obsidian = engine.ObsidianContext(mcp_client=client)
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "write_note", "arguments": {
                "folder": "00-About Me", "title": "Preferences", "content": "Prefers dark mode.",
            }}}
        ]},
        {"role": "assistant", "content": "Noted that for you, sir."},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "I prefer dark mode everywhere", obsidian=obsidian)

    assert client.calls == [("write_note", {
        "folder": "00-About Me", "title": "Preferences", "content": "Prefers dark mode.",
    })]
    assert db.get_pending_action(db_path, owner_id) is None  # never gated
    assert "noted" in reply.lower()


def test_obsidian_tools_absent_when_no_obsidian_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])

    reply = engine.handle_message(db_path, llm, owner_id, "hi", obsidian=None)

    assert reply == "Hi there"
