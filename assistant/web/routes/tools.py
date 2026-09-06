"""Internal tool-execution endpoint for the Claude CLI backend.

The Claude CLI runs its own agent loop in a subprocess, so it reaches Jarvis's tools
over MCP rather than through engine.handle_message's hop loop. The MCP bridge that
subprocess speaks to (assistant/mcp_bridge.py) is deliberately dumb — it holds no Era
session, no IMAP login, no Home Assistant client. It forwards every tool call here, to
the already-running web process, which still has all of those live and warm on
app.state. That keeps per-message latency at one loopback hop instead of rebuilding
every integration for each turn.

Crucially, this route dispatches through engine._dispatch_tool_call — the exact same
function the Ollama path uses. Confirmation gating for locks, SMS, and outbound email
therefore has ONE implementation, not one per backend: a sensitive call staged here
lands in the same pending_actions table and is resolved by the same code.

Auth is a static bearer token (claude_tools_api_key) rather than the session cookie the
rest of /api/* uses, because the caller is a subprocess, not a browser. Treat that token
as equivalent to full owner access — anything holding it can invoke every tool Jarvis
has.
"""
import asyncio
import functools
import secrets

from fastapi import APIRouter, HTTPException, Request

from ...core.engine import _dispatch_tool_call

router = APIRouter(prefix="/api/tools", tags=["tools"])


def _require_token(request: Request) -> None:
    cfg = request.app.state.cfg
    key = getattr(cfg, "claude_tools_api_key", None)
    if not key:
        raise HTTPException(503, "internal tool endpoint not configured")
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not token or not secrets.compare_digest(token, key):
        raise HTTPException(401, "invalid API key")


@router.post("/call")
async def call_tool(request: Request):
    _require_token(request)
    body = await request.json()
    name = body.get("name")
    arguments = body.get("arguments") or {}
    user_id = body.get("user_id")
    if not name or user_id is None:
        raise HTTPException(400, "name and user_id are required")

    cfg = request.app.state.cfg
    # _dispatch_tool_call is blocking (Era/IMAP/HA calls, and some use asyncio.run
    # internally, which raises on a thread that already has a running loop) — same
    # executor pattern as routes/chat.py's handle_message call.
    loop = asyncio.get_running_loop()
    call = functools.partial(
        _dispatch_tool_call,
        cfg.db_path, cfg.timezone, int(user_id), name, arguments,
        request.app.state.era, request.app.state.calendar, request.app.state.phone,
        mail=request.app.state.mail, obsidian=request.app.state.obsidian,
        home_assistant=request.app.state.home_assistant, business=request.app.state.business,
        personal=request.app.state.personal,
        airbnb=request.app.state.airbnb, ticketmaster=request.app.state.ticketmaster,
        kroger=request.app.state.kroger, ccxt=request.app.state.ccxt,
        letterstream=request.app.state.letterstream, git_ops=request.app.state.git_ops,
    )
    result = await loop.run_in_executor(None, call)
    # _dispatch_tool_call always returns a JSON string, including for its own error
    # cases, so it's passed through verbatim rather than re-wrapped.
    return {"result": result}
