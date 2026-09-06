"""Covers /api/personal — the owner's personal projects, to-dos, and delegated research
over the web UI, backed by personal_db.py. Owner-only, like Finance and Crypto.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, personal_db
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


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    personal_db.init_personal_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    db.upsert_user(path, "222", "Partner", "partner")
    return path


@pytest.fixture
def cfg(db_path):
    return FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="ownerpass"),
        UserConfig(telegram_chat_id="222", display_name="Partner", role="partner", web_password="partnerpass"),
    ])


@pytest.fixture
def client(cfg):
    return TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))


def _login(client, password="ownerpass"):
    name = "Dug" if password == "ownerpass" else "Partner"
    r = client.post("/api/login", json={"name": name, "password": password})
    assert r.status_code == 200
    return client


def test_requires_login(client):
    assert client.get("/api/personal/projects").status_code == 401


def test_partner_is_refused_owner_only_personal_data(client):
    _login(client, "partnerpass")
    assert client.get("/api/personal/projects").status_code == 403


def test_project_crud(client):
    _login(client)
    created = client.post("/api/personal/projects", json={"name": "Refinish the deck", "goal": "before winter"})
    assert created.status_code == 200
    project_id = created.json()["project_id"]

    listed = client.get("/api/personal/projects").json()
    assert len(listed) == 1
    assert listed[0]["name"] == "Refinish the deck"
    assert listed[0]["status"] == "active"

    assert client.put(f"/api/personal/projects/{project_id}", json={"status": "done"}).status_code == 200
    assert client.get("/api/personal/projects?status=done").json()[0]["id"] == project_id


def test_create_project_requires_name(client):
    _login(client)
    assert client.post("/api/personal/projects", json={"goal": "no name"}).status_code == 400


def test_update_missing_project_404s(client):
    _login(client)
    assert client.put("/api/personal/projects/999", json={"status": "done"}).status_code == 404


def test_task_crud_and_project_filing(client):
    _login(client)
    project_id = client.post("/api/personal/projects", json={"name": "Move apartments"}).json()["project_id"]

    task = client.post("/api/personal/tasks", json={
        "text": "book the movers", "project_id": project_id, "priority": "high",
    })
    assert task.status_code == 200
    task_id = task.json()["task_id"]

    listed = client.get(f"/api/personal/tasks?project_id={project_id}").json()
    assert len(listed) == 1
    assert listed[0]["text"] == "book the movers"
    assert listed[0]["priority"] == "high"
    assert listed[0]["project_name"] == "Move apartments"

    assert client.put(f"/api/personal/tasks/{task_id}", json={"status": "done"}).status_code == 200
    assert client.get("/api/personal/tasks?status=done").json()[0]["id"] == task_id


def test_task_without_project_is_unfiled(client):
    _login(client)
    client.post("/api/personal/tasks", json={"text": "call the dentist"})
    listed = client.get("/api/personal/tasks").json()
    assert len(listed) == 1
    assert listed[0]["project_id"] is None


def test_create_task_requires_text(client):
    _login(client)
    assert client.post("/api/personal/tasks", json={}).status_code == 400


def test_update_missing_task_404s(client):
    _login(client)
    assert client.put("/api/personal/tasks/999", json={"status": "done"}).status_code == 404


def test_research_create_and_list(client):
    _login(client)
    created = client.post("/api/personal/research", json={
        "topic": "Find a dentist", "question": "someone taking new patients nearby",
    })
    assert created.status_code == 200
    research_id = created.json()["research_id"]

    listed = client.get("/api/personal/research").json()
    assert len(listed) == 1
    assert listed[0]["id"] == research_id
    assert listed[0]["status"] == "requested"
    assert listed[0]["findings"] is None


def test_create_research_requires_topic(client):
    _login(client)
    assert client.post("/api/personal/research", json={"question": "no topic"}).status_code == 400
