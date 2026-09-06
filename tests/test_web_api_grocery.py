"""Covers /api/grocery: pantry status board (backed by personal_db), and the
Kroger-backed shadow cart / store picker / recipe propose-confirm flow (backed by a
fake KrogerContext so no real Kroger account is needed for these tests).
"""
import asyncio
import json
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, personal_db
from assistant.core.engine import KrogerContext
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


class FakeKrogerMCPClient:
    """Mirrors StdioMCPClient's {"is_error", "content": [json_text]} return shape --
    including its real asyncio.run() call internally. The real MCPClient/StdioMCPClient
    classes call asyncio.run() inside call_tool(), which raises if invoked directly on
    an already-running event loop (the exact bug found and fixed for Era/phone in
    chat.py). A fake that skips that call would let a route regress to calling
    kroger.mcp_client.call_tool() synchronously on uvicorn's loop without any test
    ever catching it -- so this fake reproduces it deliberately."""

    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def call_tool(self, name, arguments):
        async def _fake_async_work():
            return None

        asyncio.run(_fake_async_work())  # raises if called from a running event loop
        self.calls.append((name, arguments))
        if name not in self._responses:
            return {"is_error": True, "content": [f"no fake response for {name}"]}
        return {"is_error": False, "content": [json.dumps(self._responses[name])]}


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


def _login(client, password="ownerpass"):
    name = "Dug" if password == "ownerpass" else "Partner"
    r = client.post("/api/login", json={"name": name, "password": password})
    assert r.status_code == 200
    return client


def _client(cfg, kroger=None):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None, kroger=kroger)
    return TestClient(app)


def test_requires_login(cfg):
    client = _client(cfg)
    assert client.get("/api/grocery/pantry").status_code == 401


def test_partner_is_refused_owner_only_grocery_data(cfg):
    client = _login(_client(cfg), "partnerpass")
    assert client.get("/api/grocery/pantry").status_code == 403


def test_pantry_upsert_and_list(cfg):
    client = _login(_client(cfg))
    resp = client.post("/api/grocery/pantry", json={"item": "milk", "status": "low"})
    assert resp.status_code == 200
    item_id = resp.json()["item_id"]

    listed = client.get("/api/grocery/pantry").json()
    assert len(listed) == 1
    assert listed[0]["item"] == "milk" and listed[0]["status"] == "low"

    # Re-upserting the same item updates status in place rather than duplicating it.
    client.post("/api/grocery/pantry", json={"item": "milk", "status": "out"})
    listed = client.get("/api/grocery/pantry").json()
    assert len(listed) == 1 and listed[0]["status"] == "out"

    assert client.delete(f"/api/grocery/pantry/{item_id}").status_code == 200
    assert client.get("/api/grocery/pantry").json() == []


def test_pantry_filters_by_status(cfg):
    client = _login(_client(cfg))
    client.post("/api/grocery/pantry", json={"item": "eggs", "status": "have"})
    client.post("/api/grocery/pantry", json={"item": "flour", "status": "out"})
    out_only = client.get("/api/grocery/pantry?status=out").json()
    assert len(out_only) == 1 and out_only[0]["item"] == "flour"


def test_pantry_requires_item(cfg):
    client = _login(_client(cfg))
    assert client.post("/api/grocery/pantry", json={"status": "have"}).status_code == 400


def test_pantry_rejects_bad_status(cfg):
    client = _login(_client(cfg))
    assert client.post("/api/grocery/pantry", json={"item": "milk", "status": "sortof"}).status_code == 400


def test_kroger_routes_503_when_not_configured(cfg):
    client = _login(_client(cfg, kroger=None))
    assert client.get("/api/grocery/cart").status_code == 503
    assert client.post("/api/grocery/cart/clear").status_code == 503
    assert client.get("/api/grocery/stores").status_code == 503


def test_view_cart_unwraps_the_real_tool_response(cfg):
    mcp = FakeKrogerMCPClient({"view_current_cart": {"success": True, "current_cart": [{"product_id": "p1", "quantity": 2}]}})
    kroger = KrogerContext(mcp_client=mcp, kroger_tools=[], sensitive_tools=set())
    client = _login(_client(cfg, kroger=kroger))

    resp = client.get("/api/grocery/cart")
    assert resp.status_code == 200
    assert resp.json()["current_cart"] == [{"product_id": "p1", "quantity": 2}]
    assert mcp.calls == [("view_current_cart", {})]


def test_recipe_propose_is_read_only_and_never_calls_add_to_cart(cfg):
    mcp = FakeKrogerMCPClient({"add_recipe_to_cart": {
        "dish": "chili", "matches": [{"ingredient": "ground beef", "matched": True, "product_id": "p1"}],
    }})
    kroger = KrogerContext(mcp_client=mcp, kroger_tools=[], sensitive_tools=set())
    client = _login(_client(cfg, kroger=kroger))

    resp = client.post("/api/grocery/recipe/propose", json={"dish": "chili", "ingredients": ["ground beef"]})
    assert resp.status_code == 200
    assert resp.json()["matches"][0]["product_id"] == "p1"
    assert all(name != "bulk_add_to_cart" for name, _ in mcp.calls)


def test_recipe_propose_requires_ingredients(cfg):
    kroger = KrogerContext(mcp_client=FakeKrogerMCPClient({}), kroger_tools=[], sensitive_tools=set())
    client = _login(_client(cfg, kroger=kroger))
    assert client.post("/api/grocery/recipe/propose", json={"dish": "chili"}).status_code == 400


def test_recipe_confirm_calls_bulk_add_to_cart_directly_no_pending_action(cfg):
    """The page's confirm button IS the confirmation -- this must never create a
    pending_actions row the way the same tool would if called from chat."""
    mcp = FakeKrogerMCPClient({"bulk_add_to_cart": {"success": True, "results": [{"product_id": "p1", "success": True}]}})
    kroger = KrogerContext(mcp_client=mcp, kroger_tools=[], sensitive_tools={"bulk_add_to_cart"})
    client = _login(_client(cfg, kroger=kroger))

    resp = client.post("/api/grocery/recipe/confirm", json={"items": [{"product_id": "p1", "quantity": 1}]})
    assert resp.status_code == 200
    assert mcp.calls == [("bulk_add_to_cart", {"items": [{"product_id": "p1", "quantity": 1}]})]

    owner = db.get_user_by_chat_id(cfg.db_path, "111")
    assert db.get_pending_action(cfg.db_path, owner["id"]) is None


def test_recipe_confirm_requires_items(cfg):
    kroger = KrogerContext(mcp_client=FakeKrogerMCPClient({}), kroger_tools=[], sensitive_tools=set())
    client = _login(_client(cfg, kroger=kroger))
    assert client.post("/api/grocery/recipe/confirm", json={}).status_code == 400


def test_set_preferred_store_requires_location_id(cfg):
    kroger = KrogerContext(mcp_client=FakeKrogerMCPClient({}), kroger_tools=[], sensitive_tools=set())
    client = _login(_client(cfg, kroger=kroger))
    assert client.post("/api/grocery/stores/preferred", json={}).status_code == 400


def test_a_kroger_tool_error_surfaces_as_502(cfg):
    mcp = FakeKrogerMCPClient({})  # no fake response registered -> is_error True
    kroger = KrogerContext(mcp_client=mcp, kroger_tools=[], sensitive_tools=set())
    client = _login(_client(cfg, kroger=kroger))
    assert client.get("/api/grocery/cart").status_code == 502
