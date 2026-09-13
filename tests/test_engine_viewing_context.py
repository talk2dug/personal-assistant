"""viewing_context (Command Center Phase 4 -- "what's the owner currently looking at")
folds into the one outgoing turn only, exactly like image_bytes already does, and must
never be written back to the persisted conversation -- see handle_message's own
docstring. Covers both LLM backends: the non-agentic (Ollama-style .chat()) path, where
it's appended to the last message's content, and the agentic (Claude CLI) path, where
it's threaded through to llm.converse() as its own parameter (see test_claude_cli.py for
how _render actually places it in the rendered prompt)."""
import pytest

from assistant.core import db, engine


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


@pytest.fixture
def user_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


class FakeLLM:
    """Non-agentic backend -- plain .chat(messages, tools, think), no .agentic flag."""

    def __init__(self):
        self.seen_messages = None

    def chat(self, messages, tools=None, think=False):
        self.seen_messages = messages
        return {"role": "assistant", "content": "Sure, sir."}


class FakeAgenticLLM:
    agentic = True

    def __init__(self):
        self.converse_kwargs = None

    def converse(self, **kwargs):
        self.converse_kwargs = kwargs
        return "Sure, sir."


def test_viewing_context_reaches_the_outgoing_message_not_the_history(db_path, user_id):
    llm = FakeLLM()
    engine.handle_message(
        db_path, llm, user_id, "what am I looking at?",
        viewing_context="the Finance detail modal, showing $12,450 across 2 accounts",
    )

    last = llm.seen_messages[-1]
    assert last["role"] == "user"
    assert "currently looking at: the Finance detail modal" in last["content"]
    # The original text is still there too -- this augments, it doesn't replace.
    assert "what am I looking at?" in last["content"]


def test_viewing_context_is_never_persisted(db_path, user_id):
    llm = FakeLLM()
    engine.handle_message(
        db_path, llm, user_id, "what am I looking at?", viewing_context="the Crypto detail modal",
    )

    stored = db.recent_messages(db_path, user_id, limit=10)
    user_turn = next(m for m in stored if m["role"] == "user")
    assert "currently looking at" not in user_turn["content"]
    assert user_turn["content"] == "what am I looking at?"


def test_no_viewing_context_leaves_the_message_untouched(db_path, user_id):
    llm = FakeLLM()
    engine.handle_message(db_path, llm, user_id, "hello")

    assert llm.seen_messages[-1]["content"] == "hello"


def test_agentic_backend_receives_viewing_context_as_its_own_parameter(db_path, user_id):
    llm = FakeAgenticLLM()
    engine.handle_message(
        db_path, llm, user_id, "what am I looking at?", viewing_context="the Schedule detail modal",
    )

    assert llm.converse_kwargs["viewing_context"] == "the Schedule detail modal"


def test_agentic_backend_still_works_with_no_viewing_context(db_path, user_id):
    llm = FakeAgenticLLM()
    reply = engine.handle_message(db_path, llm, user_id, "hello")

    assert reply == "Sure, sir."
    assert llm.converse_kwargs["viewing_context"] is None
