"""Personal projects, to-dos, and delegated research over the web UI — the same
personal_db rows the chat tools in personal_tools.py read and write, so a task created
in chat shows up here and vice versa. Owner-only: this is the owner's own life, like
Finance and Crypto.
"""
from fastapi import APIRouter, HTTPException, Request

from ...core import personal_db
from ..auth import require_owner

router = APIRouter(prefix="/api/personal", tags=["personal"])


@router.get("/projects")
async def list_projects(request: Request, status: str | None = None):
    user = require_owner(request)
    cfg = request.app.state.cfg
    return personal_db.list_projects(cfg.db_path, user["id"], status)


@router.post("/projects")
async def create_project(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    project_id = personal_db.create_project(cfg.db_path, user["id"], name, body.get("goal"))
    return {"ok": True, "project_id": project_id}


@router.put("/projects/{project_id}")
async def update_project(project_id: int, request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    ok = personal_db.update_project(
        cfg.db_path, user["id"], project_id,
        name=body.get("name"), goal=body.get("goal"), status=body.get("status"))
    if not ok:
        raise HTTPException(404, "project not found or not permitted")
    return {"ok": True}


@router.get("/tasks")
async def list_tasks(request: Request, status: str | None = None, project_id: int | None = None):
    user = require_owner(request)
    cfg = request.app.state.cfg
    return personal_db.list_tasks(cfg.db_path, user["id"], status, project_id)


@router.post("/tasks")
async def create_task(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    task_id = personal_db.create_task(
        cfg.db_path, user["id"], text, body.get("project_id"),
        body.get("priority", "normal"), body.get("due_at"))
    return {"ok": True, "task_id": task_id}


@router.put("/tasks/{task_id}")
async def update_task(task_id: int, request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    ok = personal_db.update_task(
        cfg.db_path, user["id"], task_id, text=body.get("text"), status=body.get("status"),
        priority=body.get("priority"), project_id=body.get("project_id"))
    if not ok:
        raise HTTPException(404, "task not found or not permitted")
    return {"ok": True}


@router.get("/research")
async def list_research(request: Request, limit: int = 10):
    user = require_owner(request)
    cfg = request.app.state.cfg
    return personal_db.list_research(cfg.db_path, user["id"], limit)


@router.post("/research")
async def create_research(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    topic = (body.get("topic") or "").strip()
    if not topic:
        raise HTTPException(400, "topic is required")
    research_id = personal_db.create_research(
        cfg.db_path, user["id"], topic, body.get("question"), body.get("project_id"))
    return {"ok": True, "research_id": research_id}
