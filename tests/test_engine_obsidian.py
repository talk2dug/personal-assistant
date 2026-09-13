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


def test_the_system_note_tells_jarvis_to_capture_his_own_deliverables(db_path, owner_id):
    """OBSIDIAN_SYSTEM_NOTE was worded entirely around "whenever the user shares something
    durable". Nothing told Jarvis to capture a conclusion HE reached or a deliverable that
    landed -- so the vault only ever recorded one side of the work."""
    note = engine.OBSIDIAN_SYSTEM_NOTE

    assert "your OWN side of the work" in note
    assert "design document" in note
    assert "06-Agents" in note


def test_the_capture_clause_is_scoped_away_from_a_note_per_commit(db_path, owner_id):
    """The owner's decision 1. His "everything goes to Obsidian" instruction does not mean
    narrating shipped code into the vault -- git already records that better."""
    note = engine.OBSIDIAN_SYSTEM_NOTE

    assert "does NOT mean a note per commit" in note
    assert "git already records" in note


def test_agent_written_notes_are_kept_out_of_the_owners_own_folders(db_path, owner_id):
    """His decision 2: he should never have to wonder whether he wrote a note or a machine
    did."""
    note = engine.OBSIDIAN_SYSTEM_NOTE

    assert "Do not file the user's own notes there" in note
    assert "do not file your agents' output" in note
