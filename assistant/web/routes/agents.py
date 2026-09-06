"""Live agent + GPU bridge state for the pixel office UI.

One endpoint, polled by the browser, describing who is working on what and what the
shared GPU is doing. Deliberately a single call rather than several: the office renders
one coherent scene per frame, and stitching it from three endpoints that resolved at
different moments produces a picture that was never actually true at any instant.

Everything here is read-only. The office is a window onto the agents, not a control
panel — starting work still goes through chat, where the confirmation gate lives.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from ...core import business_db, staff
from ...core.agents import AGENT_ROSTER
from ..auth import require_user

router = APIRouter(prefix="/api/agents", tags=["agents"])

# How long after a run finishes the character still shows as "just finished", so a fast
# agent doesn't blink from idle to idle and look like it never ran.
RECENT_WINDOW = timedelta(minutes=10)


def _agent_states(db_path: str, gpu_jobs: list[dict]) -> list[dict]:
    runs = business_db.recent_agent_runs(db_path, limit=120)
    latest: dict[str, dict] = {}
    for run in runs:
        latest.setdefault(run["agent"], run)

    gpu_by_agent: dict[str, list[dict]] = {}
    for job in gpu_jobs:
        gpu_by_agent.setdefault(job["agent"], []).append(job)

    now = datetime.now(timezone.utc)
    agents = []
    for member in AGENT_ROSTER:
        run = latest.get(member["key"])
        jobs = gpu_by_agent.get(member["key"], [])
        running_job = next((j for j in jobs if j["status"] == "running"), None)
        queued_job = next((j for j in jobs if j["status"] == "queued"), None)

        if running_job:
            status, detail = "on_gpu", f"{running_job['task_type']} on simrig"
        elif queued_job:
            status, detail = "waiting_gpu", f"queued for {queued_job['task_type']}"
        elif run and run["status"] == "running":
            status, detail = "working", "running"
        elif run and run["finished_at"] and _parse(run["finished_at"]) > now - RECENT_WINDOW:
            status = "just_finished" if run["status"] == "ok" else "failed"
            detail = run["summary"] or run["status"]
        else:
            status, detail = "idle", (run["summary"] if run else "no runs yet")

        agents.append({
            **member,
            "status": status,
            "detail": detail,
            "last_run_at": run["started_at"] if run else None,
            "last_status": run["status"] if run else None,
        })

    # Hired staff sit in the same office as the built-in agents. They journal to
    # staff_work rather than agent_runs, so their state is derived separately, but the
    # shape is identical: the UI should not care which kind of colleague it is drawing.
    agents.extend(_hired_states(db_path, now))
    return agents


def _hired_states(db_path: str, now: datetime) -> list[dict]:
    try:
        people = staff.list_staff(db_path)
    except Exception:
        # A database without the staff tables yet must not blank the whole office.
        return []

    out = []
    for person in people:
        history = staff.recent_work(db_path, key=person["key"], limit=1)
        work = history[0] if history else None

        if person["status"] == "paused":
            status, detail = "idle", "paused"
        elif work and work["status"] == "running":
            status, detail = "working", (work["assignment"] or "")[:60]
        elif work and work["finished_at"] and _parse(work["finished_at"]) > now - RECENT_WINDOW:
            status = "just_finished" if work["status"] == "delivered" else "failed"
            detail = (work["assignment"] or "")[:60]
        else:
            detail = (work["assignment"] or "")[:60] if work else "no assignments yet"
            status = "idle"

        out.append({
            "key": person["key"],
            "title": person["title"],
            "role": person["department"],
            "character": person["character"],
            "hired": True,
            "seniority": person["seniority"],
            "status": status,
            "detail": detail,
            "last_run_at": work["started_at"] if work else None,
            "last_status": work["status"] if work else None,
        })
    return out


def _parse(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@router.get("/status")
async def status(request: Request):
    require_user(request)
    cfg = request.app.state.cfg
    bridge = getattr(request.app.state, "bridge", None)

    gpu_jobs: list[dict] = []
    gpu = {
        "configured": bridge is not None, "reachable": False, "mode": "available",
        "reason": None, "queued": 0, "running": 0, "vram_free_gb": None,
        "loaded_models": [], "comfyui": False, "jobs": [],
    }
    if bridge is not None:
        snapshot = bridge.status()
        # Running first so the office can seat the active job at the machine and line the
        # rest up behind it in submission order.
        gpu_jobs = bridge.jobs(status="running", limit=5) + bridge.jobs(status="queued", limit=15)
        gpu = {
            "configured": True,
            "reachable": snapshot["reachable"],
            "mode": snapshot["mode"],
            "reason": snapshot.get("reason"),
            "queued": snapshot["queued"],
            "running": snapshot["running"],
            "vram_free_gb": snapshot["vram_free_gb"],
            "vram_used_gb": snapshot["vram_used_gb"],
            "loaded_models": snapshot["loaded_models"],
            "comfyui": snapshot.get("comfyui", {}).get("reachable", False),
            "routing": snapshot.get("task_routing", {}),
            "jobs": [
                {"id": j["id"], "agent": j["agent"], "task_type": j["task_type"],
                 "status": j["status"], "model": j["model"], "queued_at": j["queued_at"]}
                for j in gpu_jobs
            ],
        }

    owner = next((u for u in cfg.users if u.role == "owner"), None)
    pipeline = {}
    if owner is not None:
        from ...core import db as core_db

        row = core_db.get_user_by_chat_id(cfg.db_path, owner.telegram_chat_id)
        if row is not None:
            uid = row["id"]
            pipeline = {
                "concepts": len(business_db.list_product_concepts(cfg.db_path, uid, status="proposed")),
                "briefs": len(business_db.list_art_briefs(cfg.db_path, uid, status="draft")),
                "listings": len(business_db.list_store_listings(cfg.db_path, uid, status="draft")),
                "posts": len(business_db.list_social_posts(cfg.db_path, uid, status="draft")),
                "market_leads": len(business_db.list_market_leads(cfg.db_path, uid, status="new")),
                "trend_leads": len(business_db.list_trend_leads(cfg.db_path, uid, status="new")),
            }

    return {
        "agents": _agent_states(cfg.db_path, gpu_jobs),
        "gpu": gpu,
        "pipeline": pipeline,
        "agents_scheduled": getattr(getattr(request.app.state, "business", None), "agents_scheduled", False),
    }
