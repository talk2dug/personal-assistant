"""Covers /api/schedule — reminders and places over the web UI, exercising the exact
same db.py functions and CalDAV-push behavior the chat tool path already uses
(engine.py's add_reminder/list_reminders/cancel_reminder dispatch), just reachable
without going through the LLM.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    ha_conversation_api_key: str | None = None


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


class FakeCalendarClient:
    def __init__(self):
        self.created = []
        self.deleted = []

    def create_event(self, calendar_url, summary, start):
        self.created.append((calendar_url, summary, start))
        return "fake-uid-123"

    def delete_event(self, calendar_url, uid, near):
        self.deleted.append((calendar_url, uid))


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def cfg(db_path):
    return FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="ownerpass"),
    ])


def _login(client):
    r = client.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    assert r.status_code == 200
    return client


def test_requires_login(cfg):
    client = TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))
    assert client.get("/api/schedule/reminders").status_code == 401


def test_create_and_list_reminder_round_trips_local_time(cfg):
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)))
    resp = client.post("/api/schedule/reminders", json={
        "text": "call the vet", "due_at": "2026-09-10T09:00:00", "scope": "private",
    })
    assert resp.status_code == 200 and resp.json()["ok"]

    listed = client.get("/api/schedule/reminders").json()
    assert len(listed) == 1
    assert listed[0]["text"] == "call the vet"
    # Stored as UTC, must come back converted to the configured local timezone, not raw UTC.
    assert listed[0]["due_at"].startswith("2026-09-10T09:00:00")
    assert "caldav_uid" not in listed[0]  # internal plumbing, not for the client


def test_create_reminder_pushes_to_caldav_when_configured(cfg):
    from assistant.core.engine import CalendarContext
    cal_client = FakeCalendarClient()
    calendar = CalendarContext(client=cal_client, personal_calendar="personal-cal", shared_calendar="shared-cal")
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=calendar, static_dir=None)))

    client.post("/api/schedule/reminders", json={
        "text": "trash day", "due_at": "2026-09-10T09:00:00", "scope": "shared",
    })
    assert len(cal_client.created) == 1
    assert cal_client.created[0][0] == "shared-cal"
    assert cal_client.created[0][1] == "trash day"


def test_a_caldav_failure_does_not_fail_reminder_creation(cfg):
    from assistant.core.engine import CalendarContext

    class BrokenCalendarClient:
        def create_event(self, *a, **kw):
            raise RuntimeError("CalDAV is down")

    calendar = CalendarContext(client=BrokenCalendarClient(), personal_calendar="p", shared_calendar="s")
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=calendar, static_dir=None)))

    resp = client.post("/api/schedule/reminders", json={
        "text": "still works", "due_at": "2026-09-10T09:00:00", "scope": "private",
    })
    assert resp.status_code == 200 and resp.json()["ok"]


def test_delete_reminder_also_deletes_the_caldav_event(cfg):
    from assistant.core.engine import CalendarContext
    cal_client = FakeCalendarClient()
    calendar = CalendarContext(client=cal_client, personal_calendar="p", shared_calendar="s")
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=calendar, static_dir=None)))

    created = client.post("/api/schedule/reminders", json={
        "text": "one-off", "due_at": "2026-09-10T09:00:00", "scope": "private",
    }).json()

    resp = client.delete(f"/api/schedule/reminders/{created['reminder_id']}")
    assert resp.status_code == 200
    assert cal_client.deleted == [("p", "fake-uid-123")]
    assert client.get("/api/schedule/reminders").json() == []


def test_delete_missing_reminder_404s(cfg):
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)))
    assert client.delete("/api/schedule/reminders/999").status_code == 404


def test_create_reminder_requires_text_and_due_at(cfg):
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)))
    assert client.post("/api/schedule/reminders", json={"text": "no due date"}).status_code == 400
    assert client.post("/api/schedule/reminders", json={"due_at": "2026-09-10T09:00:00"}).status_code == 400


def test_create_reminder_rejects_a_bad_scope(cfg):
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)))
    resp = client.post("/api/schedule/reminders", json={
        "text": "x", "due_at": "2026-09-10T09:00:00", "scope": "public",
    })
    assert resp.status_code == 400


def test_places_crud(cfg):
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)))
    created = client.post("/api/schedule/places", json={
        "name": "home", "latitude": 35.6, "longitude": -82.5,
    })
    assert created.status_code == 200
    place_id = created.json()["place_id"]

    listed = client.get("/api/schedule/places").json()
    assert len(listed) == 1 and listed[0]["name"] == "home"

    assert client.delete(f"/api/schedule/places/{place_id}").status_code == 200
    assert client.get("/api/schedule/places").json() == []


def test_create_place_requires_coordinates(cfg):
    client = _login(TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)))
    resp = client.post("/api/schedule/places", json={"name": "nowhere"})
    assert resp.status_code == 400
