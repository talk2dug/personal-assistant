"""stdio MCP server exposing Jarvis's tools to the Claude CLI subprocess.

Claude Code only accepts custom tools over MCP, so this is the adapter between its
agent loop and Jarvis's tool catalog. It is intentionally the thinnest possible layer:

  claude -p  <--stdio/JSON-RPC-->  this bridge  <--HTTP-->  jarvis web process
                                                             (live Era/IMAP/HA clients,
                                                              engine._dispatch_tool_call,
                                                              confirmation gating, db)

It holds no integration state of its own. tools/list is served from a schema file the
caller wrote before spawning (so listing costs no network round trip), and tools/call is
forwarded to /api/tools/call on the running web process. Rebuilding Era's MCP session,
the IMAP login, and the Home Assistant client inside this short-lived process instead
would add seconds to every single message.

Transport is hand-rolled line-delimited JSON-RPC rather than the mcp SDK's server half:
verified working against the real CLI, ~zero import cost (matters — this process is
spawned per message), and immune to the SDK's v1/v2 server API churn.

Invoked by ClaudeCLIClient, not by hand. Configuration comes from the environment:
  JARVIS_TOOLS_URL     -- e.g. http://127.0.0.1:8080/api/tools/call
  JARVIS_TOOLS_TOKEN   -- bearer token for that endpoint
  JARVIS_TOOLS_SCHEMA  -- path to the JSON file of tool schemas to advertise
  JARVIS_USER_ID       -- the Jarvis user id tool calls execute as
  JARVIS_EMPLOYEE_KEY  -- optional: which staff.py employee is calling, for tools like
                          request_capability that need to know who's asking
"""
import json
import os
import sys

import httpx

# This subprocess's stdio is a pipe, not a console, but Python still picks an encoding
# for it from the OS locale rather than defaulting to UTF-8 -- on Windows that's cp1252.
# The CLI parent sends and expects real UTF-8 JSON-RPC over these pipes (tool arguments
# and results routinely carry real non-ASCII text: em-dashes, curly quotes, a BOM read
# back from a file), so left at the platform default, every one of those characters gets
# decoded as cp1252 on the way in and mis-encoded again on the way out -- silent
# corruption, not a crash, so it went unnoticed until a git_read_file/write round-trip
# corrupted a file badly enough (a BOM landing outside its string literal) to break
# Python's own parser. Confirmed directly: sys.stdout.encoding reports cp1252 here.
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

PROTOCOL_VERSION = "2024-11-05"


def _load_tools() -> list[dict]:
    """Reads the Ollama-format schema list written by ClaudeCLIClient and converts it
    to MCP's shape. Same source list engine.select_tools produces for the Ollama path,
    so the two backends can't disagree about what tools exist."""
    path = os.environ.get("JARVIS_TOOLS_SCHEMA")
    if not path or not os.path.exists(path):
        return []
    tools = []
    for entry in json.loads(open(path, encoding="utf-8").read()):
        fn = entry.get("function", entry)
        tools.append({
            "name": fn["name"],
            "description": fn.get("description") or "",
            "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return tools


def _call_tool(name: str, arguments: dict) -> str:
    url = os.environ.get("JARVIS_TOOLS_URL")
    token = os.environ.get("JARVIS_TOOLS_TOKEN")
    user_id = os.environ.get("JARVIS_USER_ID")
    if not url or not token or user_id is None:
        return json.dumps({"error": "tool bridge is not configured"})
    payload = {"name": name, "arguments": arguments, "user_id": int(user_id)}
    employee_key = os.environ.get("JARVIS_EMPLOYEE_KEY")
    if employee_key:
        payload["employee_key"] = employee_key
    try:
        # Generous timeout: some real tool calls behind this are genuinely slow (Era
        # does live bank lookups, IMAP search walks the mailbox). Better to wait than
        # to hand the model a spurious failure it might then report as fact.
        resp = httpx.post(
            url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json().get("result", "")
    except Exception as e:
        # Returned as a tool result rather than raised: the model should see the failure
        # and say so honestly, which is far better than the bridge dying mid-turn.
        return json.dumps({"error": f"tool call failed: {e}"})


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    tools = _load_tools()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = request.get("method")
        request_id = request.get("id")

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jarvis", "version": "1.0.0"},
            }})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})
        elif method == "tools/call":
            params = request.get("params") or {}
            result = _call_tool(params.get("name"), params.get("arguments") or {})
            _send({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": result}],
            }})
        elif request_id is not None:
            # Notifications (no id) need no reply; anything else unrecognised gets an
            # empty result so the CLI isn't left waiting on a response that never comes.
            _send({"jsonrpc": "2.0", "id": request_id, "result": {}})


if __name__ == "__main__":
    main()
