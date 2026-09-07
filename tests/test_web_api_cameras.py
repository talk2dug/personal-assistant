"""routes/cameras.py: the authenticated MJPEG proxy show_camera's window actually plays.
Auth, camera-not-found, and the mjpeg-only restriction are all real behavior a browser
would hit; the successful-stream case fakes the upstream uStreamer response rather than
opening a real socket, since what matters here is that this route proxies bytes through
with the right content type, not urllib itself."""
from dataclasses import dataclass, field
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, vision
from assistant.web.app import create_app
from assistant.web.routes import cameras


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "Hello from Jarvis"}


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


@pytest.fixture
def client(cfg):
    db.upsert_user(cfg.db_path, "111", "Dug", "owner")
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    c = TestClient(app)
    c.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    return c


def test_stream_requires_login(cfg):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    resp = TestClient(app).get("/api/cameras/kitchen/stream")
    assert resp.status_code == 401


def test_unknown_camera_is_404(client):
    resp = client.get("/api/cameras/nope/stream")
    assert resp.status_code == 404


def test_rtsp_camera_is_not_yet_streamable(client, cfg):
    vision.add_camera(cfg.db_path, key="frontdoor", name="Front Door", url="rtsp://192.168.0.50/", kind="rtsp")
    resp = client.get("/api/cameras/frontdoor/stream")
    assert resp.status_code == 501


def test_mjpeg_camera_proxies_the_upstream_bytes(client, cfg, monkeypatch):
    vision.add_camera(cfg.db_path, key="kitchen", name="Kitchen", url="http://192.168.0.135:8081/", location="kitchen")

    class FakeUpstream:
        def __enter__(self):
            return BytesIO(b"--frame\r\nfake jpeg bytes\r\n")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cameras.urllib.request, "urlopen", lambda url, timeout=10: FakeUpstream())

    resp = client.get("/api/cameras/kitchen/stream")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "multipart/x-mixed-replace; boundary=frame"
    assert resp.content == b"--frame\r\nfake jpeg bytes\r\n"
