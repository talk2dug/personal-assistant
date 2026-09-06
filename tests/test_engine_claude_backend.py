"""Covers engine.handle_message's agentic branch — the fork that hands the whole turn
to an agentic backend (the Claude CLI) instead of running the local hop loop.

The point of these tests is that everything *around* the fork stays identical: the same
pending-confirmation gate runs first, the same history goes in, the same persistence
happens after. Only the middle changes.
"""
import pytest

from assistant.core import db
from assistant.core.engine import HomeAssistantContext, build_system_prompt, handle_message, select_tools


class FakeAgentic:
    """Stands in for ClaudeCLIClient: records what it was handed, returns fixed text."""

    agentic = True

    def __init__(self, reply="Very good, sir.", raises=None):
        self.reply = reply
        self.raises = raises
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.reply

    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "confirm"}


class FakeOllama:
    agentic = False

    def __init__(self):
        self.calls = []

    def chat(self, messages, tools=None, think=False):
        self.calls.append(messages)
        return {"role": "assistant", "content": "local reply"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


@pytest.fixture
def user_id(db_path):
    db.upsert_user(db_path, "111", "Dug", "owner")
    return db.get_user_by_chat_id(db_path, "111")["id"]


def test_agentic_backend_gets_the_turn_and_its_reply_is_persisted(db_path, user_id):
    llm = FakeAgentic()
    reply = handle_message(db_path, llm, user_id, "what's on my plate today?")

    assert reply == "Very good, sir."
    assert len(llm.calls) == 1
    history = db.recent_messages(db_path, user_id, limit=10)
    assert history[-1] == {"role": "assistant", "content": "Very good, sir."}
    assert history[-2]["content"] == "what's on my plate today?"


def test_agentic_backend_receives_history_and_the_tool_catalog(db_path, user_id):
    db.add_message(db_path, user_id, "user", "remind me to call mum")
    db.add_message(db_path, user_id, "assistant", "Done, sir.")
    llm = FakeAgentic()
    handle_message(db_path, llm, user_id, "and what did I just ask?")

    call = llm.calls[0]
    contents = [m["content"] for m in call["history"]]
    assert "remind me to call mum" in contents
    assert call["history"][-1]["content"] == "and what did I just ask?"
    # The same catalog the Ollama path would get, so the backends can't disagree.
    assert [t["function"]["name"] for t in call["tools"]] == \
        [t["function"]["name"] for t in select_tools("and what did I just ask?")]


def test_system_prompt_sent_to_claude_carries_no_timestamp(db_path, user_id):
    """Prompt-cache hygiene: a per-turn timestamp in the system prompt would re-bill
    Claude Code's ~25k-token preamble every message."""
    llm = FakeAgentic()
    handle_message(db_path, llm, user_id, "hello")
    call = llm.calls[0]
    assert "current local time" not in call["system_prompt"].lower()
    assert "America/New_York" in call["system_prompt"]
    # ...but the real time is still supplied, on the turn itself.
    assert call["now"]


def test_pending_confirmation_is_resolved_before_the_agentic_path_runs(db_path, user_id):
    """The gate must come first regardless of backend — otherwise a 'yes' would be
    handled as a fresh message and the staged action would never execute."""
    executed = []

    class FakeHAClient:
        def call_tool(self, name, arguments):
            executed.append((name, arguments))
            return {"ok": True}

    ha = HomeAssistantContext(mcp_client=FakeHAClient(), sensitive_domains={"lock"})
    db.create_pending_action(db_path, user_id, "call_service",
                             {"domain": "lock", "service": "unlock", "entity_id": "lock.front"})

    llm = FakeAgentic()
    reply = handle_message(db_path, llm, user_id, "yes", home_assistant=ha)

    assert executed == [("call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.front"})]
    assert "Done" in reply
    # The agentic path was never entered — this turn was a confirmation, not a new ask.
    assert llm.calls == []


def test_backend_failure_is_reported_honestly_not_silently_swallowed(db_path, user_id):
    llm = FakeAgentic(raises=RuntimeError("usage limit reached"))
    reply = handle_message(db_path, llm, user_id, "hello")

    assert "usage limit reached" in reply
    assert db.recent_messages(db_path, user_id, limit=1)[0]["content"] == reply


def test_blank_agentic_reply_falls_back_instead_of_returning_empty(db_path, user_id):
    llm = FakeAgentic(reply="   ")
    reply = handle_message(db_path, llm, user_id, "hello")
    assert reply.strip()
    assert "at a loss for words" in reply


def test_image_is_forwarded_to_the_agentic_backend(db_path, user_id):
    llm = FakeAgentic()
    handle_message(db_path, llm, user_id, "what am I looking at?", image_bytes=b"jpegbytes")
    assert llm.calls[0]["image_bytes"] == b"jpegbytes"


def test_non_agentic_backend_still_uses_the_local_hop_loop(db_path, user_id):
    """The Ollama path must be untouched by the fork — it stays the fallback for when
    the new inference hardware lands."""
    llm = FakeOllama()
    reply = handle_message(db_path, llm, user_id, "hello")

    assert reply == "local reply"
    assert len(llm.calls) == 1
    # The local path still gets a system message with the concrete current time in it.
    assert llm.calls[0][0]["role"] == "system"
    assert "current local time is" in llm.calls[0][0]["content"].lower()


def test_build_system_prompt_includes_only_configured_integrations():
    bare = build_system_prompt("America/New_York")
    with_ha = build_system_prompt("America/New_York", home_assistant=object())
    assert "Home Assistant" not in bare
    assert "Home Assistant" in with_ha
