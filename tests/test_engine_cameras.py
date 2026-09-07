"""Camera tools (show_camera/list_cameras/add_camera) are always-on -- no context object
gates them, unlike Era/mail/phone -- so these tests exercise them straight through
handle_message with no extra wiring, plus the pending_camera_views handoff itself
(engine.py can't just return the camera in the reply text: the reply is spoken text,
and routes/chat.py needs the structured {key, name, location} to open the window)."""
import pytest

from assistant.core import db, engine, vision


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    vision.init_vision_db(path)
    return path


@pytest.fixture
def user_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


def test_show_camera_resolves_by_location_and_stages_the_pending_view(db_path, user_id):
    vision.add_camera(db_path, key="kitchen", name="Kitchen", url="http://192.168.0.135:8081/", location="kitchen")
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "show_camera", "arguments": {"location": "kitchen"}}}
        ]},
        {"role": "assistant", "content": "Here's the kitchen, sir."},
    ])

    reply = engine.handle_message(db_path, llm, user_id, "show me the kitchen")

    assert reply == "Here's the kitchen, sir."
    pending = vision.pop_pending_camera_view(db_path, user_id)
    assert pending == {"key": "kitchen", "name": "Kitchen", "location": "kitchen"}


def test_pending_camera_view_is_one_shot(db_path, user_id):
    vision.set_pending_camera_view(db_path, user_id, {"key": "kitchen", "name": "Kitchen", "location": "kitchen"})

    assert vision.pop_pending_camera_view(db_path, user_id) is not None
    assert vision.pop_pending_camera_view(db_path, user_id) is None


def test_show_camera_with_no_match_reports_the_error_and_stages_nothing(db_path, user_id):
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "show_camera", "arguments": {"location": "attic"}}}
        ]},
        {"role": "assistant", "content": "There's no camera registered for the attic, sir."},
    ])

    engine.handle_message(db_path, llm, user_id, "show me the attic")

    assert vision.pop_pending_camera_view(db_path, user_id) is None


def test_add_camera_registers_it_for_later_lookup(db_path, user_id):
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "add_camera", "arguments": {
                "location": "kitchen", "url": "http://192.168.0.135:8081/",
            }}}
        ]},
        {"role": "assistant", "content": "Kitchen camera registered, sir."},
    ])

    engine.handle_message(db_path, llm, user_id, "add a camera called kitchen at http://192.168.0.135:8081/")

    camera = vision.find_camera(db_path, "kitchen")
    assert camera is not None
    assert camera["url"] == "http://192.168.0.135:8081/"
    assert camera["kind"] == "mjpeg"


def test_list_cameras_reports_registered_cameras(db_path, user_id):
    vision.add_camera(db_path, key="kitchen", name="Kitchen", url="http://192.168.0.135:8081/", location="kitchen")
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "list_cameras", "arguments": {}}}
        ]},
        {"role": "assistant", "content": "You have one camera, in the kitchen."},
    ])

    reply = engine.handle_message(db_path, llm, user_id, "what cameras do we have")

    assert reply == "You have one camera, in the kitchen."


def test_camera_tools_are_not_keyword_gated(db_path, user_id):
    # A fixed 3-tool addition, unlike Era's 51 -- always offered, same as location/business
    # tools, so a message with no obvious camera wording still gets show_camera etc.
    names = {t["function"]["name"] for t in engine.select_tools("what did I just ask you?")}
    assert {"show_camera", "list_cameras", "add_camera"} <= names
