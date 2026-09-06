"""Protocol-level tests for the stdio MCP bridge the Claude subprocess talks to.

These drive the real script over real stdin/stdout as the CLI does, rather than calling
its functions directly — the whole value of this component is that it speaks JSON-RPC
correctly to an external process, which an in-process call would not exercise.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BRIDGE = str(Path(__file__).resolve().parents[1] / "assistant" / "mcp_bridge.py")

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_reminders", "description": "List reminders.",
        "parameters": {"type": "object", "properties": {"scope_filter": {"type": "string"}}, "required": []},
    }},
    {"type": "function", "function": {"name": "call_service", "description": "Control a device.",
                                      "parameters": {"type": "object", "properties": {}}}},
]


def run_bridge(requests, env_extra=None):
    """Feeds newline-delimited JSON-RPC in, parses the responses out."""
    env = dict(os.environ)
    # A real run always sets this; clear inherited state so cases that assert the
    # unconfigured behaviour actually test it.
    env.pop("JARVIS_TOOLS_SCHEMA", None)
    env.update(env_extra or {})
    stdin = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run(
        [sys.executable, BRIDGE], input=stdin, capture_output=True, text=True, env=env, timeout=60,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


@pytest.fixture
def schema_file(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text(json.dumps(TOOL_SCHEMAS), encoding="utf-8")
    return str(path)


def test_initialize_handshake():
    responses = run_bridge([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
    assert responses[0]["id"] == 1
    assert responses[0]["result"]["serverInfo"]["name"] == "jarvis"
    assert "tools" in responses[0]["result"]["capabilities"]


def test_tools_list_converts_ollama_schemas_to_mcp_shape(schema_file):
    responses = run_bridge(
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
        env_extra={"JARVIS_TOOLS_SCHEMA": schema_file},
    )
    tools = responses[0]["result"]["tools"]
    assert [t["name"] for t in tools] == ["list_reminders", "call_service"]
    # MCP uses inputSchema where Ollama uses function.parameters.
    assert tools[0]["inputSchema"]["properties"]["scope_filter"]["type"] == "string"
    assert tools[0]["description"] == "List reminders."


def test_tools_list_is_empty_without_a_schema_file():
    responses = run_bridge([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    assert responses[0]["result"]["tools"] == []


def test_tool_call_without_configuration_returns_an_error_result_not_a_crash():
    """A misconfigured bridge must hand the model an error it can report honestly,
    never die mid-turn and leave the CLI hanging."""
    responses = run_bridge([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "list_reminders", "arguments": {}}},
    ])
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert "error" in payload


def test_unreachable_backend_is_reported_as_a_tool_error(schema_file):
    responses = run_bridge(
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "list_reminders", "arguments": {}}}],
        env_extra={
            "JARVIS_TOOLS_SCHEMA": schema_file,
            # Nothing is listening here; the bridge must degrade, not explode.
            "JARVIS_TOOLS_URL": "http://127.0.0.1:9/api/tools/call",
            "JARVIS_TOOLS_TOKEN": "tok",
            "JARVIS_USER_ID": "1",
        },
    )
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert "tool call failed" in payload["error"]


def test_notifications_get_no_response_and_do_not_stop_the_loop():
    responses = run_bridge([
        {"jsonrpc": "2.0", "method": "notifications/initialized"},  # no id -> no reply
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
    ])
    assert len(responses) == 1
    assert responses[0]["id"] == 2


def test_malformed_line_is_skipped_rather_than_fatal():
    env = dict(os.environ)
    env.pop("JARVIS_TOOLS_SCHEMA", None)
    stdin = 'not json at all\n{"jsonrpc":"2.0","id":5,"method":"initialize","params":{}}\n'
    proc = subprocess.run(
        [sys.executable, BRIDGE], input=stdin, capture_output=True, text=True, env=env, timeout=60,
    )
    responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert responses[0]["id"] == 5
