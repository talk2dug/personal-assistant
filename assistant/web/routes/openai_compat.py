"""An OpenAI-compatible /v1/chat/completions shim so Home Assistant's built-in
'OpenAI Conversation' integration can be pointed at Jarvis instead of OpenAI —
configure that integration's Base URL as this server's /v1 and its API Key as
ha_conversation_api_key from config.json. This makes HA's Assist pipeline talk to
the SAME Jarvis as the web UI/Telegram: same handle_message, same tools, same
conversation history (keyed by the owner's user id) — HA's own per-request message
history is intentionally not used as context; only the latest user utterance is
extracted, since Jarvis's own DB-backed history is the single source of truth for
"remembering" across every surface. Auth is a static bearer token, not the session
cookie the rest of /api/* uses — Home Assistant can't do an interactive login.

"Same tools" is a promise this file has to keep by hand: handle_message takes each
context as a separate argument, so a context added elsewhere and not passed here
silently downgrades the voice assistant into a Jarvis that shares his memory but not
his abilities. When adding a context, add it to this call too.
"""
import asyncio
import functools
import json
import secrets
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...core import db
from ...core.engine import handle_message

router = APIRouter(prefix="/v1", tags=["openai_compat"])


def _require_ha_token(request: Request) -> None:
    cfg = request.app.state.cfg
    if not cfg.ha_conversation_api_key:
        raise HTTPException(503, "HA conversation endpoint not configured")
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not token or not secrets.compare_digest(token, cfg.ha_conversation_api_key):
        raise HTTPException(401, "invalid API key")


def _owner_user_id(request: Request) -> int:
    cfg = request.app.state.cfg
    owner_cfg = next((u for u in cfg.users if u.role == "owner"), None)
    if owner_cfg is None:
        raise HTTPException(500, "no owner configured")
    owner = db.get_user_by_chat_id(cfg.db_path, owner_cfg.telegram_chat_id)
    if owner is None:
        raise HTTPException(500, "owner not found in db")
    return owner["id"]


@router.get("/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": "jarvis", "object": "model", "created": 0, "owned_by": "jarvis"}],
    }


@router.post("/chat/completions")
async def chat_completions(request: Request):
    _require_ha_token(request)
    body = await request.json()
    messages = body.get("messages") or []
    stream = bool(body.get("stream"))

    text = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    text = (text or "").strip()

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if not text:
        reply = ""
    else:
        cfg = request.app.state.cfg
        user_id = _owner_user_id(request)
        loop = asyncio.get_running_loop()
        call = functools.partial(
            handle_message, cfg.db_path, request.app.state.llm, user_id, text,
            tz_name=cfg.timezone, era=request.app.state.era, calendar=request.app.state.calendar,
            phone=request.app.state.phone, mail=request.app.state.mail, obsidian=request.app.state.obsidian,
            home_assistant=request.app.state.home_assistant,
            # Owner-only, like every other surface — and this endpoint is owner-only by
            # construction: the bearer token is his and _owner_user_id resolves to him.
            # Omitting it made the HA Jarvis a different assistant wearing the same
            # history: he could read a conversation about the crypto staff and have no
            # staff, hiring or market tools to act on it.
            business=request.app.state.business, personal=request.app.state.personal,
            airbnb=request.app.state.airbnb, ticketmaster=request.app.state.ticketmaster,
            kroger=request.app.state.kroger, ccxt=request.app.state.ccxt,
            letterstream=request.app.state.letterstream,
        )
        reply = await loop.run_in_executor(None, call)

    if not stream:
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": "jarvis",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def sse():
        chunk = {
            "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": "jarvis",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        done_chunk = {
            "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": "jarvis",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(done_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")
