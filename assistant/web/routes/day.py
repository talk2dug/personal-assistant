"""The day planner: the shape of today, what he has committed to, and what is slipping.

Deliberately one GET rather than four. The three answers are related -- what is already
anchored to a clock bounds how much of the task shortlist is realistic, and what is
slipping is the reason to spend one of today's slots on something with no deadline at all
-- and fetching them separately would let the page render a plan that contradicts itself
mid-load.

Everything here is scoped to the PERSONAL track. Build work has its own home in the
Tasks and project views; "wake-word arbitration" and "find a vet for Ghost" are not
comparable, and a planner that ranks them against each other is useless for planning
either kind of day.
"""
from datetime import date

from fastapi import APIRouter, HTTPException, Request

from ...core import personal_db, routine
from ..auth import require_owner

router = APIRouter(prefix="/api/day", tags=["day"])


def _today(request: Request, on_date: str | None) -> date:
    if not on_date:
        tz = getattr(request.app.state.cfg, "timezone", "America/New_York")
        return routine._now_local(tz).date()
    try:
        return date.fromisoformat(on_date)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")


@router.get("")
async def plan(request: Request, on_date: str | None = None, track: str = "personal"):
    owner = require_owner(request)
    cfg = request.app.state.cfg
    if track not in ("personal", "project"):
        raise HTTPException(400, "track must be personal or project")
    return routine.plan_day(cfg.db_path, owner["id"], tz_name=cfg.timezone,
                            today=_today(request, on_date), track=track)


@router.post("/pick/{task_id}")
async def pick(task_id: int, request: Request):
    """Commit a task to today. Distinct from starting it -- this is the choosing step the
    planner exists for, and it is per-day so that not getting to something shows up as a
    fact rather than silently carrying forward."""
    owner = require_owner(request)
    body = await request.json() if await request.body() else {}
    on_date = _today(request, body.get("on_date")).isoformat()
    added = personal_db.pick_for_day(request.app.state.cfg.db_path, owner["id"],
                                     task_id, on_date)
    return {"ok": True, "added": added, "on_date": on_date}


@router.delete("/pick/{task_id}")
async def unpick(task_id: int, request: Request, on_date: str | None = None):
    owner = require_owner(request)
    when = _today(request, on_date).isoformat()
    removed = personal_db.unpick_for_day(request.app.state.cfg.db_path, owner["id"],
                                         task_id, when)
    return {"ok": True, "removed": removed}


@router.post("/rhythm/{rhythm_id}/log")
async def log_rhythm(rhythm_id: int, request: Request):
    """Records that an anchor or habit happened, or deliberately did not.

    A skip is stored as a real answer rather than left as silence: "skipped the bike, was
    away" is a fact about his week, and an empty row would make that week look identical
    to one where he ignored the question.
    """
    owner = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    state = body.get("state", "done")
    if state not in ("done", "skipped"):
        raise HTTPException(400, "state must be done or skipped")
    on_date = _today(request, body.get("on_date")).isoformat()
    # Ownership is checked here rather than in the write: rhythm_log keys on rhythm_id
    # alone, so without this anyone signed in could log against another user's rhythm.
    mine = {r["id"] for r in personal_db.list_rhythm(cfg.db_path, owner["id"],
                                                     include_disabled=True)}
    if rhythm_id not in mine:
        raise HTTPException(404, "no such rhythm")
    personal_db.log_rhythm(cfg.db_path, rhythm_id, on_date, state,
                           note=body.get("note"), source="dashboard")
    return {"ok": True, "state": state, "on_date": on_date}


@router.delete("/rhythm/{rhythm_id}/log")
async def clear_rhythm_log(rhythm_id: int, request: Request, on_date: str | None = None):
    """Undo -- he ticked the wrong thing. Leaves no row rather than writing a 'skipped',
    because 'I have not answered yet' and 'I chose not to' are different facts."""
    import sqlite3
    from contextlib import closing
    owner = require_owner(request)
    cfg = request.app.state.cfg
    when = _today(request, on_date).isoformat()
    mine = {r["id"] for r in personal_db.list_rhythm(cfg.db_path, owner["id"],
                                                     include_disabled=True)}
    if rhythm_id not in mine:
        raise HTTPException(404, "no such rhythm")
    with closing(sqlite3.connect(cfg.db_path)) as conn:
        conn.execute("DELETE FROM rhythm_log WHERE rhythm_id = ? AND on_date = ?",
                     (rhythm_id, when))
        conn.commit()
    return {"ok": True}


@router.get("/rhythm")
async def list_rhythm(request: Request):
    owner = require_owner(request)
    return {"rhythm": personal_db.list_rhythm(request.app.state.cfg.db_path, owner["id"],
                                              include_disabled=True)}


@router.put("/rhythm/{rhythm_id}")
async def update_rhythm(rhythm_id: int, request: Request):
    """Correcting the seeded times, which is expected: they were inferred from a wake
    reminder and a commute distance, not from him."""
    owner = require_owner(request)
    body = await request.json()
    try:
        changed = personal_db.update_rhythm(request.app.state.cfg.db_path, owner["id"],
                                            rhythm_id, **body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not changed:
        raise HTTPException(404, "no such rhythm, or nothing to change")
    return {"ok": True}
