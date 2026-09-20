"""The "needs you" board — what the team is stopped on, and where Jack answers it.

Deliberately separate from /api/review. That queue is for decisions the agents could have
made badly on their own; this one is for things no agent can produce at all — a token, an
account, a file off his disk. Mixing them would bury the handful of items that actually
halt a pipeline underneath a stream of mockups to approve.

Secrets go IN through here and never come back out: every read is served from
owner_requests.list_requests(), which masks them. The browser is told that a key exists
and its last four characters, which is enough to tell one key from another and useless to
anybody who gets hold of the response.
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...core import business_db, owner_requests
from ..auth import require_user

router = APIRouter(prefix="/api/needs", tags=["needs"])


def _owner_id(request: Request) -> int:
    user = require_user(request)
    if user["role"] != "owner":
        raise HTTPException(403, "the needs board is owner-only")
    return user["id"]


class Supply(BaseModel):
    value: str


class Decision(BaseModel):
    status: str
    note: str | None = None


@router.get("")
def board(request: Request, status: str | None = None, project_id: int | None = None):
    """The whole board, grouped by project so it reads as a manager's view.

    Projects are resolved here rather than in the client because the grouping IS the
    view: a request with no project still has to land somewhere, so it goes into an
    explicit "unassigned" bucket instead of vanishing from a grouped render.
    """
    uid = _owner_id(request)
    db_path = request.app.state.cfg.db_path
    items = owner_requests.list_requests(db_path, uid, status=status, project_id=project_id)

    names = {}
    try:
        for project in business_db.list_projects(db_path, uid):
            names[project["id"]] = project["name"]
    except Exception:
        # A database without the business tables must still render the board rather than
        # 500 — the requests themselves are what he came to see.
        pass

    groups: dict = {}
    for item in items:
        pid = item.get("project_id")
        key = str(pid) if pid is not None else "none"
        bucket = groups.setdefault(key, {
            "project_id": pid,
            "name": names.get(pid, "Unassigned" if pid is None else f"Project {pid}"),
            "items": [],
        })
        bucket["items"].append(item)

    return {
        "groups": sorted(groups.values(), key=lambda g: (g["project_id"] is None, g["name"])),
        "summary": owner_requests.blocked_summary(db_path, uid),
    }


@router.post("/{request_id}/provide")
def provide(request_id: int, body: Supply, request: Request):
    """Jack hands over the thing. Returns the masked board row, never the value he just
    sent — so a screenshot of this screen, or a logged response, leaks nothing."""
    uid = _owner_id(request)
    db_path = request.app.state.cfg.db_path
    value = body.value.strip()
    if not value:
        raise HTTPException(400, "nothing supplied")
    if not owner_requests.provide(db_path, uid, request_id, value):
        raise HTTPException(404, "no such request")
    row = next((r for r in owner_requests.list_requests(db_path, uid) if r["id"] == request_id), None)
    return {"ok": True, "item": row}


@router.post("/{request_id}/status")
def status(request_id: int, body: Decision, request: Request):
    uid = _owner_id(request)
    try:
        changed = owner_requests.set_status(
            request.app.state.cfg.db_path, uid, request_id, body.status, body.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not changed:
        raise HTTPException(404, "no such request")
    return {"ok": True}
