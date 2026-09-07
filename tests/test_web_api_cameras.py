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


class FakeUpstream:
    """Stands in for the object urllib.request.urlopen returns: a context manager whose
    body IS the readable (not one level down, the way a plain BytesIO would need
    wrapping) -- real uStreamer's own boundary token (boundarydonotcross, not the more
    obvious 'frame') is exactly what a real bug here looked like, so this fake carries a
    distinct, deliberately-unlikely-to-be-hardcoded boundary too."""

    def __init__(self, body=b"--realboundary\r\nfake jpeg bytes\r\n", content_type=None):
        self._buf = BytesIO(body)
        self.headers = {
            "Content-Type": content_type or "multipart/x-mixed-replace;boundary=realboundary",
        }

    def read(self, n):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_mjpeg_camera_proxies_the_upstream_bytes_and_boundary(client, cfg, monkeypatch):
    vision.add_camera(cfg.db_path, key="kitchen", name="Kitchen", url="http://192.168.0.135:8081/", location="kitchen")
    captured_url = {}

    def fake_urlopen(url, timeout=10):
        captured_url["url"] = url
        return FakeUpstream()

    monkeypatch.setattr(cameras.urllib.request, "urlopen", fake_urlopen)

    resp = client.get("/api/cameras/kitchen/stream")
    assert resp.status_code == 200
    # uStreamer's own root ("/") serves an HTML preview page, not video -- the real
    # stream lives at /stream, and the boundary declared here must be the upstream's
    # actual one or a browser can't split the multipart body into frames at all.
    assert captured_url["url"] == "http://192.168.0.135:8081/stream"
    assert resp.headers["content-type"] == "multipart/x-mixed-replace;boundary=realboundary"
    assert resp.content == b"--realboundary\r\nfake jpeg bytes\r\n"


def test_stream_url_not_double_suffixed_if_already_present(client, cfg, monkeypatch):
    vision.add_camera(cfg.db_path, key="kitchen", name="Kitchen", url="http://192.168.0.135:8081/stream", location="kitchen")
    captured_url = {}

    def fake_urlopen(url, timeout=10):
        captured_url["url"] = url
        return FakeUpstream()

    monkeypatch.setattr(cameras.urllib.request, "urlopen", fake_urlopen)

    client.get("/api/cameras/kitchen/stream")
    assert captured_url["url"] == "http://192.168.0.135:8081/stream"


def test_unreachable_camera_is_a_bad_gateway_not_a_silent_200(client, cfg, monkeypatch):
    vision.add_camera(cfg.db_path, key="kitchen", name="Kitchen", url="http://192.168.0.135:8081/", location="kitchen")

    def fake_urlopen(url, timeout=10):
        raise OSError("Connection refused")

    monkeypatch.setattr(cameras.urllib.request, "urlopen", fake_urlopen)

    resp = client.get("/api/cameras/kitchen/stream")
    assert resp.status_code == 502
