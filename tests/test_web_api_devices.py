"""routes/devices.py's /turn endpoint: a kiosk terminal only ever polls /{device_id} for
its on-screen state, so show_camera's result has to ride along on that polled object
(camera + camera_seq) rather than just the /turn response itself -- see the
pending_camera_views docstring in vision.py for why a plain variable can't carry it."""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, vision
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


def make_client(cfg, llm):
    db.upsert_user(cfg.db_path, "111", "Dug", "owner")
    app = create_app(cfg, llm, era=None, calendar=None, stt=FakeSTT("show me the kitchen"), static_dir=None)
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
