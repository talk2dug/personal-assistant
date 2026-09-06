"""Actionable-notification callbacks from Home Assistant.

When HA sends a push notification with buttons and one is tapped, it fires a
`mobile_app_notification_action` event; an automation forwards that here. This is the
endpoint that receives it.

The reason it's worth having is the confirmation gate. Locks, outbound SMS and outbound
email already stage into `pending_actions` and wait for an explicit yes — but until now
that yes had to be typed into chat. With this, Jarvis can push "Unlock the front door?"
to a phone and the answer comes back from the lock screen, resolved through exactly the
same `_resolve_pending_action` path a typed yes uses. No second, weaker approval route.

Auth is the shared bearer token HA is configured with, matching the shape the user's
automation already sends:

    if (req.get('authorization') !== `Bearer ${process.env.JARVIS_TOKEN}`) return 401

Unknown actions are recorded and returned rather than silently dropped: a notification
button that quietly does nothing is worse than one that reports it isn't wired up yet.
"""
import asyncio
import functools
import logging
import secrets
import time
from collections import deque

from fastapi import APIRouter, HTTPException, Request

from ...core import business_db, db
from ...core.engine import _resolve_pending_action, handle_message

logger = logging.getLogger(__name__)
router = APIRouter(tags=["notifications"])

# Small ring of what's arrived, so an automation that isn't landing can be diagnosed
# without adding logging to Home Assistant.
RECENT: deque = deque(maxlen=50)


def _require_token(request: Request) -> None:
    token = getattr(request.app.state.cfg, "notification_token", None)
    if not token:
        raise HTTPException(503, "notification actions are not configured")
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not supplied or not secrets.compare_digest(supplied, token):
        raise HTTPException(401, "invalid token")


def _owner_id(request: Request) -> int:
    cfg = request.app.state.cfg
    owner_cfg = next((u for u in cfg.users if u.role == "owner"), None)
    if owner_cfg is None:
        raise HTTPException(500, "no owner configured")
    row = db.get_user_by_chat_id(cfg.db_path, owner_cfg.telegram_chat_id)
    if row is None:
        raise HTTPException(500, "owner not found in db")
    return row["id"]


def _flatten(body: dict) -> dict:
    """Normalises the payload.

    HA templates vary: some automations post the action at the top level, others nest it
    under `data` or `action_data`, and the key is `action` in some versions and
    `actionName` in others. Accepting all of them means the automation doesn't have to be
    written a particular way to work.
    """
    merged = {}
    for key in ("action_data", "data"):
        nested = body.get(key)
        if isinstance(nested, dict):
            merged.update(nested)
    merged.update({k: v for k, v in body.items() if k not in ("action_data", "data")})
    action = merged.get("action") or merged.get("actionName") or merged.get("event")
    return {"action": str(action).upper() if action else "", "payload": merged}


async def _handle(request: Request, body: dict) -> dict:
    cfg = request.app.state.cfg
    owner = _owner_id(request)
    parsed = _flatten(body)
    action, payload = parsed["action"], parsed["payload"]
    RECENT.appendleft({"at": time.time(), "action": action, "payload": payload})
    logger.info("notification action %r payload=%s", action, {k: payload[k] for k in list(payload)[:6]})

    loop = asyncio.get_running_loop()

    # --- the staged-action gate: a yes/no from the lock screen -------------------
    if action in ("JARVIS_CONFIRM", "JARVIS_CANCEL", "CONFIRM", "CANCEL"):
        pending = db.get_pending_action(cfg.db_path, owner)
        if pending is None:
            return {"ok": False, "action": action, "result": "nothing is waiting for confirmation"}
        answer = "yes" if action in ("JARVIS_CONFIRM", "CONFIRM") else "no"
        # Deliberately the same function a typed reply goes through, so a phone approval
        # can't become a second, weaker path around the gate.
        call = functools.partial(
            _resolve_pending_action, cfg.db_path, request.app.state.llm,
            request.app.state.era, request.app.state.phone, request.app.state.mail,
            request.app.state.home_assistant, pending, answer,
            kroger=request.app.state.kroger, ccxt=request.app.state.ccxt,
            letterstream=request.app.state.letterstream,
        )
        reply = await loop.run_in_executor(None, call)
        return {"ok": True, "action": action, "tool": pending["tool_name"], "result": reply}

    # --- review queue: approve or reject from a notification ---------------------
    if action in ("JARVIS_APPROVE", "JARVIS_REJECT"):
        item_id = payload.get("item_id") or payload.get("id")
        if not item_id:
            return {"ok": False, "action": action, "result": "item_id is required"}
        decision = "approved" if action == "JARVIS_APPROVE" else "rejected"
        item = business_db.decide_review_item(
            cfg.db_path, owner, int(item_id), decision,
            option_id=payload.get("option_id"), note=payload.get("note"))
        if item is None:
            return {"ok": False, "action": action, "result": "no pending review item with that id"}
        return {"ok": True, "action": action, "result": f"{item['title']} -> {decision}"}

    # --- anything else: treat the text as something said to Jarvis ---------------
    text = payload.get("text") or payload.get("message") or payload.get("reply")
    if action in ("JARVIS_ASK", "REPLY", "JARVIS_SAY") or text:
        if not text:
            return {"ok": False, "action": action, "result": "no text supplied"}
        call = functools.partial(
            handle_message, cfg.db_path, request.app.state.llm, owner, text,
            tz_name=cfg.timezone, era=request.app.state.era, calendar=request.app.state.calendar,
            phone=request.app.state.phone, mail=request.app.state.mail,
            obsidian=request.app.state.obsidian, home_assistant=request.app.state.home_assistant,
            business=request.app.state.business,
            airbnb=request.app.state.airbnb, ticketmaster=request.app.state.ticketmaster,
            kroger=request.app.state.kroger, ccxt=request.app.state.ccxt,
            letterstream=request.app.state.letterstream,
        )
        reply = await loop.run_in_executor(None, call)
        return {"ok": True, "action": action or "REPLY", "result": reply}

    return {
        "ok": False, "action": action,
        "result": f"unrecognised action {action!r} — it was recorded but nothing is wired to it",
        "known": ["JARVIS_CONFIRM", "JARVIS_CANCEL", "JARVIS_APPROVE", "JARVIS_REJECT", "JARVIS_ASK"],
    }


@router.post("/notification-action")
async def notification_action(request: Request):
    """The path the user's Home Assistant automation posts to."""
    _require_token(request)
    try:
        body = await request.json()
    except Exception:
        body = dict(await request.form())
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    return await _handle(request, body)


@router.post("/api/notification-action")
async def notification_action_api(request: Request):
    """Same endpoint under the /api prefix, matching the rest of the app's convention.
    Both exist so an automation already pointed at either one keeps working."""
    return await notification_action(request)


@router.get("/api/notification-action/recent")
async def recent(request: Request):
    """What has actually arrived — for checking an automation is firing at all."""
    _require_token(request)
    return {"recent": list(RECENT)}
