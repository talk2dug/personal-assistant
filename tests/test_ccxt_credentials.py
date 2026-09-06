"""Verifies the CCXT credential boundary in setup.py: the real exchange secret must
never reach a tool schema the model can see, and must always be injected server-side on
every actual call regardless of what the model supplied.

This is the load-bearing safety property for CCXT specifically. The underlying MCP
server (@mcpfun/mcp-server-ccxt) takes apiKey/secret as tool-call *arguments* rather than
environment variables, which means without this stripping/injection layer, the real
Kraken secret would need to sit in the model's own context to construct a valid call —
and from there, in conversation history and the pending_actions table in plain text.
"""
from assistant.core.setup import (
    CCXT_CREDENTIAL_FIELDS, _CCXTCredentialClient, _strip_ccxt_credential_fields,
)


def test_credential_fields_are_stripped_from_the_exposed_schema():
    raw_schema = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"}, "side": {"type": "string"}, "amount": {"type": "number"},
            "exchange": {"type": "string"}, "apiKey": {"type": "string"}, "secret": {"type": "string"},
        },
        "required": ["exchange", "symbol", "side", "amount", "apiKey", "secret"],
    }

    cleaned = _strip_ccxt_credential_fields(raw_schema)

    assert set(cleaned["properties"]) == {"symbol", "side", "amount"}
    assert cleaned["required"] == ["symbol", "side", "amount"]
    assert not (set(cleaned["properties"]) & CCXT_CREDENTIAL_FIELDS)


def test_stripping_a_schema_with_no_credential_fields_is_a_no_op():
    schema = {"type": "object", "properties": {"foo": {"type": "string"}}, "required": ["foo"]}
    assert _strip_ccxt_credential_fields(schema) == schema


class FakeRawClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"is_error": False, "content": ["ok"]}

    def list_tools(self):
        return [{"name": "place-market-order", "description": "d", "input_schema": {}}]


def test_call_tool_injects_the_real_credentials():
    raw = FakeRawClient()
    client = _CCXTCredentialClient(raw, exchange="kraken", api_key="real-key", api_secret="real-secret")

    client.call_tool("place-market-order", {"symbol": "BTC/USD", "side": "buy", "amount": 0.01})

    name, sent = raw.calls[0]
    assert sent["exchange"] == "kraken"
    assert sent["apiKey"] == "real-key"
    assert sent["secret"] == "real-secret"
    assert sent["symbol"] == "BTC/USD"


def test_the_model_cannot_override_the_configured_account():
    """Nothing upstream should be able to supply exchange/apiKey/secret at all (the
    exposed schema has no such properties), but if something stray showed up in
    arguments anyway, the configured account must still win -- never a model-supplied
    value, which could point a real trade at the wrong account."""
    raw = FakeRawClient()
    client = _CCXTCredentialClient(raw, exchange="kraken", api_key="real-key", api_secret="real-secret")

    client.call_tool("place-market-order", {
        "symbol": "BTC/USD", "exchange": "binance", "apiKey": "attacker-key", "secret": "attacker-secret",
    })

    _, sent = raw.calls[0]
    assert sent["exchange"] == "kraken"
    assert sent["apiKey"] == "real-key"
    assert sent["secret"] == "real-secret"


def test_list_tools_passes_through_to_the_raw_client():
    raw = FakeRawClient()
    client = _CCXTCredentialClient(raw, exchange="kraken", api_key="k", api_secret="s")
    assert client.list_tools() == raw.list_tools()
