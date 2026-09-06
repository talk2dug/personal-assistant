"""Covers /api/tools/call — the endpoint the Claude MCP bridge executes tools through.

This route holds full owner-level power (every Jarvis tool), authenticated only by a
static token, so the auth cases matter as much as the dispatch case. The dispatch test
pins the property the whole design rests on: it goes through the SAME
engine._dispatch_tool_call the Ollama path uses, so confirmation gating has one
implementation rather than one per backend.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import business_db, db
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    claude_tools_api_key: str | None = "secret-token"
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
    business_db.init_business_db(path)
    return path


@pytest.fixture
def user_id(db_path):
    db.upsert_user(db_path, "111", "Dug", "owner")
    return db.get_user_by_chat_id(db_path, "111")["id"]


def make_client(cfg, **kwargs):
    return TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None, **kwargs))


def test_rejects_a_wrong_token(db_path, user_id):
    client = make_client(FakeConfig(db_path=db_path))
    resp = client.post(
        "/api/tools/call",
        json={"name": "list_reminders", "arguments": {}, "user_id": user_id},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_rejects_a_missing_token(db_path, user_id):
    client = make_client(FakeConfig(db_path=db_path))
    resp = client.post("/api/tools/call", json={"name": "list_reminders", "arguments": {}, "user_id": user_id})
    assert resp.status_code == 401


def test_disabled_when_no_key_is_configured(db_path, user_id):
    client = make_client(FakeConfig(db_path=db_path, claude_tools_api_key=None))
    resp = client.post(
        "/api/tools/call",
        json={"name": "list_reminders", "arguments": {}, "user_id": user_id},
        headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 503


def test_executes_a_real_tool_through_the_shared_dispatcher(db_path, user_id):
    """A round trip against the real dispatcher and the real db: create a reminder,
    then read it back through the same endpoint."""
    client = make_client(FakeConfig(db_path=db_path))
    auth = {"Authorization": "Bearer secret-token"}

    created = client.post("/api/tools/call", json={
        "name": "add_reminder", "user_id": user_id,
        "arguments": {"text": "call the vet", "due_at": "2026-09-04T09:00:00", "scope": "private"},
    }, headers=auth)
    assert created.status_code == 200
    assert '"ok": true' in created.json()["result"]

    listed = client.post("/api/tools/call", json={
        "name": "list_reminders", "user_id": user_id, "arguments": {},
    }, headers=auth)
    assert "call the vet" in listed.json()["result"]


def test_sensitive_call_stages_for_confirmation_instead_of_executing(db_path, user_id):
    """The safety property that makes a single gating implementation worth having:
    a sensitive tool reached via the Claude backend lands in pending_actions rather
    than firing, exactly as it does on the Ollama path."""
    class FakeHA:
        def call_tool(self, name, arguments):
            raise AssertionError("a gated tool must not execute before confirmation")

    from assistant.core.engine import HomeAssistantContext

    cfg = FakeConfig(db_path=db_path)
    app_client = TestClient(create_app(
        cfg, FakeLLM(), era=None, calendar=None, static_dir=None,
        home_assistant=HomeAssistantContext(mcp_client=FakeHA(), sensitive_domains={"lock"}),
    ))
    resp = app_client.post("/api/tools/call", json={
        "name": "call_service", "user_id": user_id,
        "arguments": {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"},
    }, headers={"Authorization": "Bearer secret-token"})

    assert "awaiting_confirmation" in resp.json()["result"]
    pending = db.get_pending_action(db_path, user_id)
    assert pending is not None and pending["tool_name"] == "call_service"


def test_requires_name_and_user_id(db_path):
    client = make_client(FakeConfig(db_path=db_path))
    resp = client.post("/api/tools/call", json={"arguments": {}}, headers={"Authorization": "Bearer secret-token"})
    assert resp.status_code == 400


def test_passes_the_same_contexts_as_every_other_surface(db_path, user_id, monkeypatch):
    """This route is the one the real deployment actually runs through (llm_backend is
    claude_cli), so a context missing here is a context missing in production, not a
    theoretical gap. Introspects _dispatch_tool_call's own signature rather than a
    hand-maintained list, so a context added later and not wired into this route's call
    fails this test instead of shipping — same technique used for the HA voice endpoint
    after it was found silently missing `business`.
    """
    import inspect

    from assistant.web.routes import tools as tools_route

    seen = {}

    def fake_dispatch(db_path, tz_name, requesting_user_id, name, arguments, era, calendar, phone=None, **kwargs):
        seen.update(kwargs)
        return "{}"

    monkeypatch.setattr(tools_route, "_dispatch_tool_call", fake_dispatch)

    # The real signature, not fake_dispatch's **kwargs-hiding one, is what a context
    # added later would actually show up on.
    from assistant.core.engine import _dispatch_tool_call

    # employee_key is a per-call value from the request body (like name/arguments), not
    # a long-lived integration wired onto app.state, so it's excluded the same way.
    non_contexts = {
        "db_path", "tz_name", "requesting_user_id", "name", "arguments", "era", "calendar",
        "phone", "employee_key",
    }
    contexts = [p for p in inspect.signature(_dispatch_tool_call).parameters if p not in non_contexts]

    cfg = FakeConfig(db_path=db_path)
    sentinels = {name: object() for name in contexts}
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    for name, value in sentinels.items():
        setattr(app.state, name, value)

    resp = TestClient(app).post("/api/tools/call", json={
        "name": "list_reminders", "user_id": user_id, "arguments": {},
    }, headers={"Authorization": "Bearer secret-token"})
    assert resp.status_code == 200

    missing = [name for name in contexts if name not in seen]
    assert not missing, (
        f"/api/tools/call does not pass {missing} to _dispatch_tool_call — the Claude CLI "
        f"backend (the one actually running in production) would be missing those "
        f"abilities. Add them to the call in assistant/web/routes/tools.py."
    )
    wrong = [n for n in contexts if seen.get(n) is not sentinels[n]]
    assert not wrong, f"passed something other than app.state for {wrong}"


def test_employee_key_from_the_request_body_reaches_dispatch(db_path, user_id, monkeypatch):
    """employee_key comes from the request body (set by staff.assign()'s employees via
    the MCP bridge), not app.state -- a request without one must still dispatch fine
    (the owner's own converse() never sends it), and one that's present must arrive."""
    from assistant.web.routes import tools as tools_route

    seen = {}

    def fake_dispatch(*args, employee_key=None, **kwargs):
        seen["employee_key"] = employee_key
        return "{}"

    monkeypatch.setattr(tools_route, "_dispatch_tool_call", fake_dispatch)
    client = make_client(FakeConfig(db_path=db_path))

    client.post("/api/tools/call", json={
        "name": "list_reminders", "user_id": user_id, "arguments": {}, "employee_key": "systems_engineer",
    }, headers={"Authorization": "Bearer secret-token"})
    assert seen["employee_key"] == "systems_engineer"

    client.post("/api/tools/call", json={
        "name": "list_reminders", "user_id": user_id, "arguments": {},
    }, headers={"Authorization": "Bearer secret-token"})
    assert seen["employee_key"] is None
