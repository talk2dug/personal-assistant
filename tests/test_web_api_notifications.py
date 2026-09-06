"""Covers the Home Assistant actionable-notification callback.

The important property: tapping "Unlock" on a phone notification must go through the
*same* confirmation machinery a typed "yes" goes through. If it didn't, the gate that
protects locks, outbound SMS and outbound email would have a second, weaker way around
it — which is worse than not having the notification at all.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import business_db, db
from assistant.core.engine import HomeAssistantContext
from assistant.web.app import create_app

TOKEN = "test-notification-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@dataclass
class FakeConfig:
    db_path: str
    notification_token: str | None = TOKEN
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    device_api_key: str | None = None
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None
    generated_media_path: str = "generated"


class FakeLLM:
    """Classifies confirmations the way the real engine expects."""

    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "ok"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner_id(db_path):
    return db.get_user_by_chat_id(db_path, "111")["id"]


@pytest.fixture
def executed():
    return []


@pytest.fixture
def client(db_path, executed):
    class FakeHA:
        def call_tool(self, name, arguments):
            executed.append((name, arguments))
            return {"ok": True}

    cfg = FakeConfig(
        db_path=db_path,
        users=[UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw")],
    )
    return TestClient(create_app(
        cfg, FakeLLM(), era=None, calendar=None, static_dir=None,
        home_assistant=HomeAssistantContext(mcp_client=FakeHA(), sensitive_domains={"lock"}),
    ))


def test_rejects_a_missing_or_wrong_token(client):
    assert client.post("/notification-action", json={"action": "JARVIS_CONFIRM"}).status_code == 401
    assert client.post("/notification-action", json={"action": "JARVIS_CONFIRM"},
                       headers={"Authorization": "Bearer nope"}).status_code == 401


def test_disabled_when_no_token_configured(db_path):
    cfg = FakeConfig(db_path=db_path, notification_token=None)
    c = TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))
    assert c.post("/notification-action", json={"action": "X"}, headers=AUTH).status_code == 503


def test_confirming_from_a_notification_executes_the_staged_action(client, db_path, owner_id, executed):
    """The whole point: approve a door unlock from the lock screen."""
    db.create_pending_action(db_path, owner_id, "call_service",
                             {"domain": "lock", "service": "unlock", "entity_id": "lock.front"})

    resp = client.post("/notification-action", json={"action": "JARVIS_CONFIRM"}, headers=AUTH)
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert executed == [("call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.front"})]
    assert db.get_pending_action(db_path, owner_id) is None


def test_cancelling_from_a_notification_does_not_execute(client, db_path, owner_id, executed):
    db.create_pending_action(db_path, owner_id, "call_service",
                             {"domain": "lock", "service": "unlock", "entity_id": "lock.front"})
    resp = client.post("/notification-action", json={"action": "JARVIS_CANCEL"}, headers=AUTH)
    assert resp.status_code == 200
    assert executed == [], "a cancel must never run the staged action"
    assert db.get_pending_action(db_path, owner_id) is None


def test_confirming_with_nothing_staged_is_a_no_op(client, executed):
    """A stale notification tapped later must not fire whatever happens to be staged
    next — and here, with nothing staged, must simply do nothing."""
    resp = client.post("/notification-action", json={"action": "JARVIS_CONFIRM"}, headers=AUTH)
    assert resp.json()["ok"] is False
    assert executed == []


def test_approving_a_review_item(client, db_path, owner_id):
    item_id = business_db.create_review_item(db_path, owner_id, "Approve the decal", kind="art")
    resp = client.post("/notification-action",
                       json={"action": "JARVIS_APPROVE", "data": {"item_id": item_id}}, headers=AUTH)
    assert resp.json()["ok"] is True
    assert business_db.get_review_item(db_path, owner_id, item_id)["status"] == "approved"


def test_payload_shape_variations_are_accepted(client, db_path, owner_id):
    """HA automations template this differently depending on how they were written;
    the integration shouldn't depend on one particular shape."""
    for body in (
        {"action": "JARVIS_APPROVE", "item_id": None},
        {"actionName": "JARVIS_APPROVE", "action_data": {"item_id": None}},
        {"action": "jarvis_approve", "data": {"id": None}},
    ):
        item_id = business_db.create_review_item(db_path, owner_id, "Item", kind="art")
        for container in ("item_id", "action_data", "data"):
            if container in body and isinstance(body[container], dict):
                body[container] = {k: item_id for k in body[container]}
        if "item_id" in body:
            body["item_id"] = item_id
        resp = client.post("/notification-action", json=body, headers=AUTH)
        assert resp.json()["ok"] is True, body


def test_free_text_is_answered_by_jarvis(client):
    resp = client.post("/notification-action",
                       json={"action": "JARVIS_ASK", "data": {"text": "hello"}}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True and resp.json()["result"]


def test_an_unknown_action_is_reported_not_silently_dropped(client):
    """A notification button that quietly does nothing is worse than one that says it
    isn't wired up."""
    resp = client.post("/notification-action", json={"action": "SOMETHING_NEW"}, headers=AUTH)
    body = resp.json()
    assert body["ok"] is False
    assert "unrecognised" in body["result"]
    assert "JARVIS_CONFIRM" in body["known"]


def test_recent_shows_what_arrived(client):
    client.post("/notification-action", json={"action": "SOMETHING_NEW"}, headers=AUTH)
    recent = client.get("/api/notification-action/recent", headers=AUTH).json()["recent"]
    assert recent and recent[0]["action"] == "SOMETHING_NEW"


def test_the_api_prefixed_path_works_too(client, db_path, owner_id):
    item_id = business_db.create_review_item(db_path, owner_id, "Item", kind="art")
    resp = client.post("/api/notification-action",
                       json={"action": "JARVIS_APPROVE", "item_id": item_id}, headers=AUTH)
    assert resp.json()["ok"] is True
