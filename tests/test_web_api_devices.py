"""routes/devices.py's /turn endpoint: a kiosk terminal only ever polls /{device_id} for
its on-screen state, so show_camera's result has to ride along on that polled object
(camera + camera_seq) rather than just the /turn response itself -- see the
pending_camera_views docstring in vision.py for why a plain variable can't carry it."""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, kitchen_db, vision
from assistant.core.engine import PersonalContext
from assistant.core.personal_tools import PersonalClient
from assistant.web.app import create_app
from assistant.web.routes import devices as devices_route


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    device_api_key: str = "device-secret"


class FakeSTT:
    def __init__(self, text):
        self.text = text

    def transcribe(self, audio_bytes):
        return self.text


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    vision.init_vision_db(path)
    kitchen_db.init_kitchen_db(path)  # /turn and get_state both check pending_recipe_views
    return path


@pytest.fixture
def cfg(db_path):
    return FakeConfig(
        db_path=db_path,
        users=[UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="ownerpass")],
    )


@pytest.fixture(autouse=True)
def reset_device_state():
    # DEVICE_STATE is a module-level in-memory dict by design (see its own docstring) --
    # tests must not leak state into each other through it.
    devices_route.DEVICE_STATE.clear()
    yield
    devices_route.DEVICE_STATE.clear()


def make_client(cfg, llm, transcript="show me the kitchen", personal=None):
    db.upsert_user(cfg.db_path, "111", "Dug", "owner")
    app = create_app(cfg, llm, era=None, calendar=None, stt=FakeSTT(transcript), static_dir=None, personal=personal)
    return TestClient(app)


def test_turn_relays_show_camera_through_polled_state(cfg):
    vision.add_camera(cfg.db_path, key="kitchen", name="Kitchen", url="http://192.168.0.135:8081/", location="kitchen")
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "show_camera", "arguments": {"location": "kitchen"}}}
        ]},
        {"role": "assistant", "content": "Kitchen's up, sir."},
    ])
    client = make_client(cfg, llm)

    resp = client.post(
        "/api/devices/touch1/turn?key=device-secret",
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "Kitchen's up, sir."
    assert body["camera"] == {"key": "kitchen", "name": "Kitchen", "location": "kitchen"}

    polled = client.get("/api/devices/touch1?key=device-secret").json()
    assert polled["camera"] == {"key": "kitchen", "name": "Kitchen", "location": "kitchen"}
    assert polled["camera_seq"] == 1


def test_turn_without_show_camera_leaves_no_camera_seq(cfg):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there, sir."}])
    client = make_client(cfg, llm)

    resp = client.post(
        "/api/devices/touch1/turn?key=device-secret",
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )
    assert resp.json()["camera"] is None

    polled = client.get("/api/devices/touch1?key=device-secret").json()
    assert "camera_seq" not in polled


def test_camera_seq_increments_so_the_kiosk_can_detect_a_fresh_request(cfg):
    vision.add_camera(cfg.db_path, key="kitchen", name="Kitchen", url="http://192.168.0.135:8081/", location="kitchen")
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "show_camera", "arguments": {"location": "kitchen"}}}
        ]},
        {"role": "assistant", "content": "Kitchen's up, sir."},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "show_camera", "arguments": {"location": "kitchen"}}}
        ]},
        {"role": "assistant", "content": "Kitchen's up again, sir."},
    ])
    client = make_client(cfg, llm)

    for _ in range(2):
        client.post(
            "/api/devices/touch1/turn?key=device-secret",
            files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
        )

    polled = client.get("/api/devices/touch1?key=device-secret").json()
    assert polled["camera_seq"] == 2


# --- display_recipe / pending_recipe_views relay ----------------------------------

def _personal_for(cfg):
    owner = db.upsert_user(cfg.db_path, "111", "Dug", "owner")
    return PersonalContext(mcp_client=PersonalClient(cfg.db_path, owner))


def test_turn_at_the_kitchens_own_kiosk_shows_the_recipe_the_same_turn(cfg):
    """display_recipe's target device_id (laptop1, kitchen_tools.KITCHEN_DEVICE_MAP) is
    the same device asking here -- an immediate same-turn display, not just eventually
    via the next poll."""
    personal = _personal_for(cfg)
    recipe_id = kitchen_db.create_recipe(
        cfg.db_path, personal.mcp_client.owner_user_id, "Pancakes",
        [{"name": "flour", "quantity": "2", "unit": "cups"}], ["Mix.", "Cook."])
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "display_recipe", "arguments": {"recipe_id": recipe_id}}}
        ]},
        {"role": "assistant", "content": "Pulled it up, sir."},
    ])
    client = make_client(cfg, llm, transcript="show the pancake recipe on the kitchen screen", personal=personal)

    resp = client.post(
        "/api/devices/laptop1/turn?key=device-secret",
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )
    assert resp.status_code == 200

    polled = client.get("/api/devices/laptop1?key=device-secret").json()
    assert polled["recipe_seq"] == 1
    assert polled["recipe"]["title"] == "Pancakes"


def test_display_recipe_from_a_different_device_reaches_the_target_on_its_next_poll(cfg):
    """The command is issued at touch1 but targets the kitchen screen (laptop1) -- touch1
    never sees it, laptop1 picks it up purely from its own poll, no /turn involved."""
    personal = _personal_for(cfg)
    recipe_id = kitchen_db.create_recipe(
        cfg.db_path, personal.mcp_client.owner_user_id, "Chili",
        [{"name": "ground beef", "quantity": "1", "unit": "lb"}], ["Brown.", "Simmer."])
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "display_recipe", "arguments": {"recipe_id": recipe_id}}}
        ]},
        {"role": "assistant", "content": "Showing it in the kitchen, sir."},
    ])
    client = make_client(cfg, llm, transcript="show the chili recipe on the kitchen screen", personal=personal)

    client.post(
        "/api/devices/touch1/turn?key=device-secret",
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )
    # touch1 (the device that asked) never gets a recipe of its own.
    assert "recipe_seq" not in client.get("/api/devices/touch1?key=device-secret").json()

    polled = client.get("/api/devices/laptop1?key=device-secret").json()
    assert polled["recipe_seq"] == 1
    assert polled["recipe"]["title"] == "Chili"


def test_recipe_seq_increments_so_the_kiosk_can_detect_a_fresh_display(cfg):
    personal = _personal_for(cfg)
    recipe_id = kitchen_db.create_recipe(
        cfg.db_path, personal.mcp_client.owner_user_id, "Pancakes", [{"name": "flour"}], ["Mix."])
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [{"function": {"name": "display_recipe", "arguments": {"recipe_id": recipe_id}}}]},
        {"role": "assistant", "content": "Up on screen."},
        {"role": "assistant", "tool_calls": [{"function": {"name": "display_recipe", "arguments": {"recipe_id": recipe_id}}}]},
        {"role": "assistant", "content": "Up again."},
    ])
    client = make_client(cfg, llm, transcript="show pancakes on the kitchen screen", personal=personal)

    for _ in range(2):
        client.post(
            "/api/devices/laptop1/turn?key=device-secret",
            files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
        )

    polled = client.get("/api/devices/laptop1?key=device-secret").json()
    assert polled["recipe_seq"] == 2
