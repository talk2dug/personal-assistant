"""The SMS channel's server side -- what the Pi holding the modem polls.

Three endpoints and nothing more: hand me a message that arrived, tell me what to send,
tell me it went. The Pi owns the radio; this owns the decision about whether a message
gets to drive Jarvis at all.

Authentication is the shared device key, the same one the voice terminals use. That is
the right level here: this Pi is exactly as trusted as the kiosks, it sits on the same
LAN, and inventing a second credential for the same class of device would mean two
things to rotate instead of one.

The allow-list is the part that actually matters, and it is enforced HERE rather than on
the Pi. The Pi is a radio and a forwarder; putting the security decision on the far side
of the network would mean trusting a device on a shelf to decide who may spend the
owner's money. See core/cellular.is_allowed -- it fails closed.
"""
import asyncio
import functools
import logging
import secrets

from fastapi import APIRouter, HTTPException, Request

from ...core import cellular
from ...core.engine import handle_message
from ..auth import require_owner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cellular", tags=["cellular"])


def _require_device_key(request: Request) -> None:
    cfg = request.app.state.cfg
    supplied = request.headers.get("x-device-key") or request.query_params.get("key", "")
    expected = getattr(cfg, "device_api_key", None)
    if not expected or not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "device key required")


def _owner_user_id(request: Request) -> int | None:
    from ...core import db as core_db
    cfg = request.app.state.cfg
    owner = next((u for u in cfg.users if u.role == "owner"), None)
    if owner is None:
        return None
    row = core_db.get_user_by_chat_id(cfg.db_path, owner.telegram_chat_id)
    return row["id"] if row else None


@router.post("/inbound")
async def inbound(request: Request):
    """A text arrived at the modem. Decide whether it may speak to Jarvis, and if so,
    answer it.

    The reply is queued rather than returned: the Pi that delivered this should not have
    to hold an HTTP connection open for however long a real Jarvis turn takes (a tool-
    using answer can run for minutes), and a dropped connection must not lose the answer.
    It collects the reply on its next outbox poll.
    """
    _require_device_key(request)
    cfg = request.app.state.cfg
    body = await request.json()
    number = (body.get("number") or "").strip()
    text = (body.get("text") or "").strip()
    stamp = body.get("timestamp")
    if not number or not text:
        raise HTTPException(400, "number and text are required")

    allowed = getattr(cfg, "sms_allowed_numbers", None)
    if not cellular.is_allowed(number, allowed):
        # Recorded, not dropped: "who has been texting this number" is something the
        # owner should be able to look at, and a refused message is exactly the kind of
        # thing worth noticing on a line nobody is supposed to know about yet.
        cellular.record_inbound(cfg.db_path, number, text, "refused", stamp,
                                detail="sender is not on sms_allowed_numbers")
        logger.warning("cellular: refused SMS from unlisted number %s", number)
        # 200, not 403: the Pi did its job correctly, and there is nothing for it to
        # retry. A 4xx would make a well-behaved forwarder keep resending.
        return {"accepted": False, "reason": "sender not allowed"}

    message_id = cellular.record_inbound(cfg.db_path, number, text, "received", stamp)
    if message_id is None:
        return {"accepted": True, "duplicate": True}

    user_id = _owner_user_id(request)
    if user_id is None:
        cellular.set_inbound_status(cfg.db_path, message_id, "failed", "no owner user")
        raise HTTPException(503, "no owner configured")

    contexts = {
        "era": request.app.state.era, "calendar": request.app.state.calendar,
        "phone": request.app.state.phone, "mail": request.app.state.mail,
        "obsidian": request.app.state.obsidian,
        "home_assistant": request.app.state.home_assistant,
        "business": request.app.state.business, "personal": request.app.state.personal,
        "airbnb": request.app.state.airbnb, "ticketmaster": request.app.state.ticketmaster,
        "kroger": request.app.state.kroger, "ccxt": request.app.state.ccxt,
        "letterstream": request.app.state.letterstream,
        "git_ops": request.app.state.git_ops, "recipe": request.app.state.recipe,
        "local_llm": request.app.state.local_llm,
    }
    call = functools.partial(
        handle_message, cfg.db_path, request.app.state.llm, user_id, text,
        tz_name=cfg.timezone, **contexts)
    try:
        reply = await asyncio.get_running_loop().run_in_executor(None, call)
    except Exception as e:
        cellular.set_inbound_status(cfg.db_path, message_id, "failed", str(e)[:200])
        logger.exception("cellular: handling an inbound SMS failed")
        # Say so over SMS rather than leaving the sender staring at nothing -- on this
        # channel silence is indistinguishable from the whole house being down, which is
        # precisely the situation this channel exists for.
        cellular.queue_outbound(cfg.db_path, number,
                                "Sorry -- I hit an error handling that. It is logged.",
                                reply_to_id=message_id)
        return {"accepted": True, "replied": False}

    cellular.set_inbound_status(cfg.db_path, message_id, "handled")
    if reply and reply.strip():
        cellular.queue_outbound(cfg.db_path, number, reply, reply_to_id=message_id)
    return {"accepted": True, "replied": bool(reply and reply.strip())}


@router.get("/outbox")
async def outbox(request: Request, limit: int = 5):
    """Messages waiting to go out. The Pi polls this."""
    _require_device_key(request)
    return {"messages": cellular.pending_outbound(request.app.state.cfg.db_path, limit)}


@router.post("/sent/{message_id}")
async def sent(message_id: int, request: Request):
    """The Pi reporting what happened to one outbound message."""
    _require_device_key(request)
    body = await request.json() if await request.body() else {}
    ok = bool(body.get("ok", True))
    if not cellular.mark_sent(request.app.state.cfg.db_path, message_id, ok,
                              body.get("detail")):
        raise HTTPException(404, "no such outbound message")
    return {"ok": True}


@router.get("/messages")
async def messages(request: Request, limit: int = 50):
    """The log, for the owner -- including refused senders. Owner-only, unlike the three
    endpoints above: this is a conversation history, not a device task."""
    require_owner(request)
    return {"messages": cellular.recent(request.app.state.cfg.db_path, limit)}
