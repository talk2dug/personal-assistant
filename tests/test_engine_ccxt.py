"""Verifies the confirmation gate for CCXT: placing an order or changing leverage/margin
is real money on a real exchange, so it must never reach mcp_client.call_tool except
through an explicit user confirmation. Mirrors test_engine_kroger.py's suite — same
gate, same mechanism, higher stakes.
"""
import pytest

from assistant.core import db, engine


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
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


CCXT_TOOLS = [
    {"type": "function", "function": {
        "name": "place-market-order",
        "description": "Place a real market order.",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string"}, "side": {"type": "string"}, "amount": {"type": "number"},
        }},
    }},
    {"type": "function", "function": {
        "name": "account-balance",
        "description": "Read the real account balance.",
        "parameters": {"type": "object", "properties": {}},
    }},
]


def make_ccxt():
    mcp = FakeMCPClient()
    sensitive = {"place-market-order", "set-leverage", "set-margin-mode", "place-futures-market-order"}
    return engine.CCXTContext(mcp_client=mcp, ccxt_tools=CCXT_TOOLS, sensitive_tools=sensitive), mcp


def test_place_order_creates_pending_action_not_a_real_call(db_path, owner_id):
    ccxt, mcp = make_ccxt()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "place-market-order",
                         "arguments": {"symbol": "BTC/USD", "side": "buy", "amount": 0.01}}}
        ]},
        {"role": "assistant", "content": "I'd like to buy 0.01 BTC at market — confirm?"},
    ])

    reply = engine.handle_message(db_path, llm, owner_id, "buy some bitcoin", ccxt=ccxt)

    assert mcp.calls == [], "a real order must never fire before explicit confirmation"
    pending = db.get_pending_action(db_path, owner_id)
    assert pending is not None
    assert pending["tool_name"] == "place-market-order"
    assert "confirm" in reply.lower()


def test_confirming_executes_the_real_order(db_path, owner_id):
    ccxt, mcp = make_ccxt()
    db.create_pending_action(db_path, owner_id, "place-market-order",
                             {"symbol": "BTC/USD", "side": "buy", "amount": 0.01})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "yes", ccxt=ccxt)

    assert mcp.calls == [("place-market-order", {"symbol": "BTC/USD", "side": "buy", "amount": 0.01})]
    assert db.get_pending_action(db_path, owner_id) is None
    assert "done" in reply.lower()


def test_cancelling_never_places_the_order(db_path, owner_id):
    ccxt, mcp = make_ccxt()
    db.create_pending_action(db_path, owner_id, "place-market-order",
                             {"symbol": "BTC/USD", "side": "buy", "amount": 0.01})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "no", ccxt=ccxt)

    assert mcp.calls == []
    assert db.get_pending_action(db_path, owner_id) is None
    assert "cancel" in reply.lower()


def test_ambiguous_reply_leaves_the_order_pending(db_path, owner_id):
    """The highest-stakes gate in the system must not default to executing on an
    unclear answer — same rule as every other sensitive tool, worth pinning here too."""
    ccxt, mcp = make_ccxt()
    db.create_pending_action(db_path, owner_id, "place-market-order",
                             {"symbol": "BTC/USD", "side": "buy", "amount": 0.01})
    llm = FakeLLM([{"role": "assistant", "content": "unclear"}])

    reply = engine.handle_message(db_path, llm, owner_id, "maybe idk", ccxt=ccxt)

    assert mcp.calls == []
    assert db.get_pending_action(db_path, owner_id) is not None
    assert "yes or no" in reply.lower()


def test_account_balance_is_not_gated(db_path, owner_id):
    ccxt, mcp = make_ccxt()
    llm = FakeLLM([
        {"role": "assistant", "tool_calls": [{"function": {"name": "account-balance", "arguments": {}}}]},
        {"role": "assistant", "content": "You have $500."},
    ])

    engine.handle_message(db_path, llm, owner_id, "what's my balance", ccxt=ccxt)

    assert mcp.calls == [("account-balance", {})]
    assert db.get_pending_action(db_path, owner_id) is None


def test_ccxt_tools_absent_when_no_ccxt_context(db_path, owner_id):
    llm = FakeLLM([{"role": "assistant", "content": "Hi there"}])
    reply = engine.handle_message(db_path, llm, owner_id, "hi", ccxt=None)
    assert reply == "Hi there"


def test_a_pending_ccxt_action_is_detected_even_with_no_other_context_configured(db_path, owner_id):
    """handle_message's early pending-action check must include ccxt, or a trade
    confirmation sent while no era/phone/mail/HA/kroger context exists would fall
    through to a normal turn instead of resolving the confirmation."""
    ccxt, mcp = make_ccxt()
    db.create_pending_action(db_path, owner_id, "place-market-order",
                             {"symbol": "BTC/USD", "side": "buy", "amount": 0.01})
    llm = FakeLLM([])

    reply = engine.handle_message(db_path, llm, owner_id, "yes", ccxt=ccxt)

    assert mcp.calls == [("place-market-order", {"symbol": "BTC/USD", "side": "buy", "amount": 0.01})]
    assert "done" in reply.lower()
