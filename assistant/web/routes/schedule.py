"""Personal schedule: reminders and named places, over the web UI.

Reminders already exist as a chat tool (engine.py's add_reminder/list_reminders/
cancel_reminder dispatch) — this is the same capability through a page instead of a
chat turn, so it duplicates that dispatch's exact behavior (local<->UTC conversion,
best-effort CalDAV push/delete) rather than routing through the LLM for something that
doesn't need one.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from ...core import db
from ..auth import require_user

router = APIRouter(prefix="/api/schedule", tags=["schedule"])


def _local_to_utc_iso(local_iso: str, tz_name: str) -> str:
    dt = datetime.fromisoformat(local_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(timezone.utc).isoformat()


def _utc_to_local_iso(utc_iso: str, tz_name: str) -> str:
    dt = datetime.fromisoformat(utc_iso)
    return dt.astimezone(ZoneInfo(tz_name)).isoformat()


@router.get("/reminders")
async def list_reminders(request: Request, scope: str | None = None):
    user = require_user(request)
    cfg = request.app.state.cfg
    reminders = db.list_reminders(cfg.db_path, user["id"], scope_filter=scope)
    for r in reminders:
        r["due_at"] = _utc_to_local_iso(r["due_at"], cfg.timezone)
        del r["caldav_uid"], r["caldav_calendar"]
    return reminders


@router.post("/reminders")
async def create_reminder(request: Request):
    user = require_user(request)
    cfg = request.app.state.cfg
    body = await request.json()
    text = (body.get("text") or "").strip()
    due_at = body.get("due_at")
    scope = body.get("scope", "private")
    if not text or not due_at:
        raise HTTPException(400, "text and due_at are required")
    if scope not in ("private", "shared"):
        raise HTTPException(400, "scope must be 'private' or 'shared'")

    due_at_utc = _local_to_utc_iso(due_at, cfg.timezone)
    reminder_id = db.add_reminder(cfg.db_path, user["id"], text=text, due_at=due_at_utc, scope=scope)

    calendar = request.app.state.calendar
    if calendar is not None:
        # Same as the chat path: a CalDAV hiccup must not fail reminder creation --
        # the reminder still fires via the scheduler even if the push to Apple fails.
        try:
            calendar_url = calendar.calendar_for_scope(scope)
            uid = calendar.client.create_event(
                calendar_url, summary=text, start=datetime.fromisoformat(due_at_utc))
            db.set_caldav_link(cfg.db_path, reminder_id, uid, calendar_url)
        except Exception:
            pass
    return {"ok": True, "reminder_id": reminder_id}


@router.delete("/reminders/{reminder_id}")
async def delete_reminder(reminder_id: int, request: Request):
    user = require_user(request)
    cfg = request.app.state.cfg
    calendar = request.app.state.calendar
    reminder = db.get_reminder_by_id(cfg.db_path, reminder_id) if calendar is not None else None

    ok = db.cancel_reminder(cfg.db_path, user["id"], reminder_id=reminder_id)
    if not ok:
        raise HTTPException(404, "reminder not found or not permitted")

    if calendar is not None and reminder is not None and reminder["caldav_uid"]:
        try:
            calendar.client.delete_event(
                reminder["caldav_calendar"], reminder["caldav_uid"],
                near=datetime.fromisoformat(reminder["due_at"]))
        except Exception:
            pass
    return {"ok": True}


@router.get("/places")
async def list_places(request: Request):
    user = require_user(request)
    cfg = request.app.state.cfg
    return db.list_places(cfg.db_path, user["id"])


@router.post("/places")
async def create_place(request: Request):
    user = require_user(request)
    cfg = request.app.state.cfg
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name or body.get("latitude") is None or body.get("longitude") is None:
        raise HTTPException(400, "name, latitude, and longitude are required")
    place_id = db.create_place(
        cfg.db_path, user["id"], name, float(body["latitude"]), float(body["longitude"]),
        radius_m=float(body.get("radius_m", 150)), notes=body.get("notes"))
    return {"ok": True, "place_id": place_id}


@router.delete("/places/{place_id}")
async def delete_place(place_id: int, request: Request):
    user = require_user(request)
    cfg = request.app.state.cfg
    ok = db.delete_place(cfg.db_path, user["id"], place_id)
    if not ok:
        raise HTTPException(404, "place not found")
    return {"ok": True}
