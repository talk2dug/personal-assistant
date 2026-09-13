"""show_content is always-on -- no context object gates it, same as the camera tools
(see test_engine_cameras.py, which this mirrors) -- plus the pending_ui_content handoff
itself: engine.py can't just return the content in the reply text, since the reply is
spoken text and routes/chat.py needs the structured payload to open the modal."""
import pytest

from assistant.core import business_db, db, engine, ui_content


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    ui_content.init_ui_content_db(path)
    business_db.init_business_db(path)
    return path


@pytest.fixture
def user_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


def test_show_content_text_stages_the_pending_content(db_path, user_id):
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "show_content", "arguments": {
                "kind": "text", "title": "Draft reply", "body": "Dear Sir, ...",
            }}}
        ]},
        {"role": "assistant", "content": "Here's the draft, sir."},
    ])

    reply = engine.handle_message(db_path, llm, user_id, "show me the draft")

    assert reply == "Here's the draft, sir."
    pending = ui_content.pop_pending_content(db_path, user_id)
    assert pending == {"kind": "text", "title": "Draft reply", "body": "Dear Sir, ..."}


def test_show_content_text_requires_title_and_body(db_path, user_id):
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "show_content", "arguments": {"kind": "text", "title": "Only a title"}}}
        ]},
        {"role": "assistant", "content": "I don't have a body to show yet, sir."},
    ])

    engine.handle_message(db_path, llm, user_id, "show me that")

    assert ui_content.pop_pending_content(db_path, user_id) is None


def test_show_content_review_item_stages_its_id(db_path, user_id):
    item_id = business_db.create_review_item(
        db_path, user_id, "Logo mockup v2", kind="art", summary="Pick a direction")
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "show_content", "arguments": {"kind": "review_item", "review_item_id": item_id}}}
        ]},
        {"role": "assistant", "content": "Here's the mockup, sir."},
    ])

    engine.handle_message(db_path, llm, user_id, "show me the mockup")

    pending = ui_content.pop_pending_content(db_path, user_id)
    assert pending == {"kind": "review_item", "review_item_id": item_id}


def test_show_content_review_item_with_unknown_id_stages_nothing(db_path, user_id):
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "show_content", "arguments": {"kind": "review_item", "review_item_id": 999}}}
        ]},
        {"role": "assistant", "content": "I can't find that one, sir."},
    ])

    engine.handle_message(db_path, llm, user_id, "show me item 999")

    assert ui_content.pop_pending_content(db_path, user_id) is None


def test_show_content_review_item_is_owner_scoped(db_path, user_id):
    other_user_id = db.upsert_user(db_path, "222", "Someone Else", "owner")
    item_id = business_db.create_review_item(db_path, other_user_id, "Not yours", kind="other")
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "show_content", "arguments": {"kind": "review_item", "review_item_id": item_id}}}
        ]},
        {"role": "assistant", "content": "I can't find that one, sir."},
    ])

    engine.handle_message(db_path, llm, user_id, "show me that item")

    assert ui_content.pop_pending_content(db_path, user_id) is None


def test_pending_content_is_one_shot(db_path, user_id):
    ui_content.set_pending_content(db_path, user_id, "text", {"title": "t", "body": "b"})
    assert ui_content.pop_pending_content(db_path, user_id) is not None
    assert ui_content.pop_pending_content(db_path, user_id) is None


def test_show_content_is_not_keyword_gated(db_path, user_id):
    names = {t["function"]["name"] for t in engine.select_tools("what did I just ask you?")}
    assert "show_content" in names
