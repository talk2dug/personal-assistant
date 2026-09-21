"""The project board — what each pipeline is working on, and what it needs from Jack.

Two lanes per project, in one fetch, because they are two halves of the same question.
"What is the team doing" without "what is it stuck on" reads as healthy progress right up
until you notice nothing has shipped; "what is it stuck on" without "what is it doing"
gives no sense of whether clearing a blocker actually releases anything.

The blocker lane is deliberately first. It is the only lane he can act on, and burying it
under a task list is how a board stops being read.

Kept separate from /api/review, which asks him to DECIDE between options an agent already
produced. These are things no agent can produce at all. Mixing them would put the handful
of items that halt a whole pipeline underneath a stream of mockups to approve.

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

# Finished work is shown, but only the most recent few: the point is "something is
# moving", which a handful of items answers and a full history buries.
DONE_SHOWN = 5


def _owner_id(request: Request) -> int:
    user = require_user(request)
    if user["role"] != "owner":
        raise HTTPException(403, "the project board is owner-only")
    return user["id"]


class Supply(BaseModel):
    value: str


class Decision(BaseModel):
    status: str
    note: str | None = None


class TaskStatus(BaseModel):
    status: str


@router.get("")
def board(request: Request, project_id: int | None = None):
    """Every project with anything against it: its blockers, then its work.

    Projects are resolved here rather than in the client because the grouping IS the
    view: a request or task with no project still has to land somewhere, so it goes into
    an explicit "unassigned" bucket instead of vanishing from a grouped render.
    """
    uid = _owner_id(request)
    db_path = request.app.state.cfg.db_path

    items = owner_requests.list_requests(db_path, uid, project_id=project_id)
    try:
        tasks = business_db.list_tasks(db_path, uid, project_id=project_id)
        projects = {p["id"]: p for p in business_db.list_projects(db_path, uid)}
    except Exception:
        # A database without the business tables must still render the blockers — they
        # are the half he can act on, and they do not depend on the task tables at all.
        tasks, projects = [], {}

    groups: dict = {}

    def bucket(pid):
        key = str(pid) if pid is not None else "none"
        if key not in groups:
            project = projects.get(pid)
            groups[key] = {
                "project_id": pid,
                "name": (project["name"] if project
                         else ("Unassigned" if pid is None else f"Project {pid}")),
                "goal": project["goal"] if project else None,
                "project_status": project["status"] if project else None,
                "items": [], "tasks": [],
                "task_counts": {"open": 0, "doing": 0, "done": 0},
            }
        return groups[key]

    for item in items:
        bucket(item.get("project_id"))["items"].append(item)

    for task in tasks:
        group = bucket(task.get("project_id"))
        status = task.get("status") or "open"
        counts = group["task_counts"]
        counts[status] = counts.get(status, 0) + 1
        # list_tasks already sorts doing -> open -> done, so trimming the finished ones
        # here keeps that order without a second sort. 'dropped' is counted but never
        # shown: he abandoned it deliberately, and a board that keeps redisplaying
        # abandoned work is a board that argues with him.
        if status in ("open", "doing") or (status == "done" and counts["done"] <= DONE_SHOWN):
            group["tasks"].append(task)

    ordered = sorted(
        groups.values(),
        # Whatever is blocking work comes first, then whatever has the most live work,
        # and the unassigned bucket always sinks to the bottom.
        key=lambda g: (
            g["project_id"] is None,
            -sum(1 for i in g["items"] if i["status"] == "open"),
            -(g["task_counts"].get("doing", 0) + g["task_counts"].get("open", 0)),
            g["name"],
        ),
    )
    return {"groups": ordered, "summary": owner_requests.blocked_summary(db_path, uid)}


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


@router.post("/tasks/{task_id}/status")
def task_status(task_id: int, body: TaskStatus, request: Request):
    """The team owns these tasks, but he can close one out or drop it.

    Constrained to the statuses business_tasks actually uses -- update_task takes **fields
    and would happily write 'finished' or 'DONE' into the status column, which nothing
    would ever match again and which would quietly vanish the row from every count.
    """
    uid = _owner_id(request)
    allowed = {"open", "doing", "done", "dropped"}
    if body.status not in allowed:
        raise HTTPException(400, f"status must be one of {sorted(allowed)}")
    if not business_db.update_task(request.app.state.cfg.db_path, uid, task_id,
                                   status=body.status):
        raise HTTPException(404, "no such task")
    return {"ok": True}
