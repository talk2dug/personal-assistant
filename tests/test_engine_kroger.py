"""Verifies the confirmation gate for Kroger: add_items_to_cart/bulk_add_to_cart/
mark_order_placed (real changes to the owner's actual Kroger account) must never reach
mcp_client.call_tool except through an explicit user confirmation. Mirrors
test_engine_confirmation.py's Era suite — same gate, same mechanism, different context.
"""
import pytest

from assistant.core import business_db, db, engine


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


class FakeMCPClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"is_error": False, "content": ["ok"]}


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools=None, think=False):
        return self._responses.pop(0)


KROGER_TOOLS = [
    {"type": "function", "function": {
        "name": "add_items_to_cart",
        "description": "Add an item to the real Kroger cart.",
        "parameters": {"type": "object", "properties": {"upc": {"type": "string"}, "quantity": {"type": "integer"}}},
    }},
    {"type": "function", "function": {
        "name": "search_products",
        "description": "Search Kroger products.",
        "parameters": {"type": "object", "properties": {"term": {"type": "string"}}},
    }},
]


def make_kroger():
    mcp = FakeMCPClient()
    sensitive = {"add_items_to_cart", "bulk_add_to_cart", "mark_order_placed"}
    return engine.KrogerContext(mcp_client=mcp, kroger_tools=KROGER_TOOLS, sensitive_tools=sensitive), mcp


def test_add_to_cart_creates_pending_action_not_a_real_call(db_path, owner_id):
    kroger, mcp = make_kroger()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "add_items_to_cart", "arguments": {"upc": "0001111041700", "quantity": 2}}}
        ]},
        {"role": "assistant", "content": "I'd like to add 2 of that to your cart — confirm?"},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "add milk to my kroger cart", kroger=kroger)

    assert mcp.calls == [], "a real cart-write must never fire before confirmation"
    pending = db.get_pending_action(db_path, owner_id)
    assert pending is not None
    assert pending["tool_name"] == "add_items_to_cart"
    assert "confirm" in reply.lower()


def test_confirming_executes_the_real_cart_add(db_path, owner_id):
    kroger, mcp = make_kroger()
    db.create_pending_action(db_path, owner_id, "add_items_to_cart", {"upc": "0001111041700", "quantity": 2})
    llm = FakeLLM([])  # keyword match handles "yes", no LLM call needed

    reply = engine.handle_message(db_path, llm, owner_id, "yes", kroger=kroger)

    assert mcp.calls == [("add_items_to_cart", {"upc": "0001111041700", "quantity": 2})]
    assert db.get_pending_action(db_path, owner_id) is None
    assert "done" in reply.lower()


def test_cancelling_never_touches_the_real_cart(db_path, owner_id):
    kroger, mcp = make_kroger()
    db.create_pending_action(db_path, owner_id, "add_items_to_cart", {"upc": "0001111041700", "quantity": 2})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "no", kroger=kroger)

    assert mcp.calls == []
    assert db.get_pending_action(db_path, owner_id) is None
    assert "cancel" in reply.lower()


def test_search_products_is_not_gated(db_path, owner_id):
    """Search/store lookup can't change the owner's account, so it must execute
    directly — gating it too would make every grocery question a confirmation prompt."""
    kroger, mcp = make_kroger()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "search_products", "arguments": {"term": "milk"}}}
        ]},
        {"role": "assistant", "content": "Found a few kinds of milk."},
    ])

    engine.handle_message(db_path, llm, owner_id, "find milk at kroger", kroger=kroger)

    assert mcp.calls == [("search_products", {"term": "milk"})]
    assert db.get_pending_action(db_path, owner_id) is None


def test_kroger_tools_absent_when_no_kroger_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])
    reply = engine.handle_message(db_path, llm, owner_id, "hi", kroger=None)
    assert reply == "Hi there"


def test_a_pending_kroger_action_is_detected_even_with_no_other_context_configured(db_path, owner_id):
    """handle_message's early pending-action check must include kroger, or a cart
    confirmation sent while no era/phone/mail/HA context exists would be invisible —
    it would fall through to a normal turn instead of resolving the confirmation."""
    kroger, mcp = make_kroger()
    db.create_pending_action(db_path, owner_id, "add_items_to_cart", {"upc": "0001111041700", "quantity": 2})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "yes", kroger=kroger)

    assert mcp.calls == [("add_items_to_cart", {"upc": "0001111041700", "quantity": 2})]
    assert "done" in reply.lower()
