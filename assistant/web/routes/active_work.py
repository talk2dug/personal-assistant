"""A single read-only status feed for the chat window's "Active Work" panel (personal
dashboard task 17) -- everything currently queued or in progress across the different
background systems (delegated personal research, the built-in agents, hired staff, the
async work queue behind assign_work, and ops plans), each with a best-effort estimated
finish where enough history exists to derive one.

Companion to /api/review (routes/review.py): that's a decision queue (things needing the
owner's yes/no), this is pure visibility -- nothing here can be acted on, same "read-only,
not a control panel" stance as /api/agents/status, which is why this uses require_user
rather than require_owner (a partner can see what's running same as they can see the
office).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from ...core import business_db, db as core_db, ops_plans, personal_db, staff
from ...core.agents import AGENT_ROSTER
from ..auth import require_user

router = APIRouter(prefix="/api/active-work", tags=["active-work"])

# How much completed-run history to look at when estimating a finish time for a running
# item -- recent-and-few, so one old outlier can't skew today's estimate.
HISTORY_SAMPLE = 5

_AGENT_TITLES = {a["key"]: a["title"] for a in AGENT_ROSTER}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _duration_seconds(start: str | None, end: str | None) -> float | None:
    a, b = _parse(start), _parse(end)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def _eta_label(started_at: str, history: list[float]) -> str | None:
    """Median of recent comparable completions vs. elapsed time on this one. None (no
    estimate) when there isn't enough history to say anything better than a guess."""
    if not history:
        return None
    started = _parse(started_at)
    if started is None:
        return None
    history = sorted(history)
    median = history[len(history) // 2]
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    remaining = median - elapsed
    if remaining <= 30:
        return "wrapping up"
    minutes = round(remaining / 60)
    return "under a minute left" if minutes < 1 else f"~{minutes}m left"


def _research_items(db_path: str, owner_id: int) -> list[dict]:
    queued = personal_db.list_research(db_path, owner_id, limit=20, status="requested")
    done = personal_db.list_research(db_path, owner_id, limit=30, status="done")
    history = [d for d in (
        _duration_seconds(r["created_at"], r["completed_at"]) for r in done
    ) if d is not None][:HISTORY_SAMPLE]
    return [{
        "id": f"research-{r['id']}", "kind": "research",
        "label": f"Researching: {r['topic']}", "status": "queued",
        "started_at": r["created_at"], "eta_label": _eta_label(r["created_at"], history),
    } for r in queued]


def _agent_run_items(db_path: str) -> list[dict]:
    runs = business_db.recent_agent_runs(db_path, limit=150)
    items = []
    for r in runs:
        if r["status"] != "running":
            continue
        history = [d for d in (
            _duration_seconds(x["started_at"], x["finished_at"]) for x in runs
            if x["agent"] == r["agent"] and x["status"] == "ok" and x["finished_at"]
        ) if d is not None][:HISTORY_SAMPLE]
        title = _AGENT_TITLES.get(r["agent"], r["agent"])
        items.append({
            "id": f"agent_run-{r['id']}", "kind": "agent_run",
            "label": f"{title} running", "status": "running",
            "started_at": r["started_at"], "eta_label": _eta_label(r["started_at"], history),
        })
    return items


def _staff_work_items(db_path: str) -> list[dict]:
    work = staff.recent_work(db_path, limit=150)
    items = []
    for w in work:
        if w["status"] != "running":
            continue
        history = [d for d in (
            _duration_seconds(x["started_at"], x["finished_at"]) for x in work
            if x["key"] == w["key"] and x["status"] == "delivered" and x["finished_at"]
        ) if d is not None][:HISTORY_SAMPLE]
        assignment = (w["assignment"] or "")[:70]
        items.append({
            "id": f"staff_work-{w['id']}", "kind": "staff_work",
            "label": f"{w['title']}: {assignment}", "status": "running",
            "started_at": w["started_at"], "eta_label": _eta_label(w["started_at"], history),
        })
    return items


def _queued_work_items(business_ctx) -> list[dict]:
    """Only the not-yet-claimed half of the work queue -- a running job already has its
    own staff_work row (see _staff_work_items), so surfacing both here would show the
    same piece of work twice."""
    queue = getattr(business_ctx, "work_queue", None)
    if queue is None:
        return []
    jobs = queue.jobs(status="queued", limit=20)
    return [{
        "id": f"work_queue-{j['id']}", "kind": "queued_work",
        "label": f"Queued for {j['employee_key']}: {(j['assignment'] or '')[:70]}",
        "status": "queued", "started_at": j["created_at"], "eta_label": None,
    } for j in jobs]


def _ops_plan_items(db_path: str, owner_id: int) -> list[dict]:
    items = []
    for plan in ops_plans.list_plans(db_path, owner_id, status="approved", limit=10):
        items.append({
            "id": f"ops_plan-{plan['id']}", "kind": "ops_plan",
            "label": f"Approved, queued to run: {plan['summary'][:70]}", "status": "queued",
            "started_at": plan["updated_at"], "eta_label": None,
        })
    for plan in ops_plans.list_plans(db_path, owner_id, status="running", limit=10):
        full = ops_plans.get_plan(db_path, plan["id"])
        steps = full["steps"] if full else []
        done = sum(1 for s in steps if s["status"] in ("succeeded", "failed", "skipped"))
        items.append({
            "id": f"ops_plan-{plan['id']}", "kind": "ops_plan",
            "label": f"Running: {plan['summary'][:70]} (step {min(done + 1, len(steps))}/{len(steps)})",
            "status": "running", "started_at": plan["updated_at"], "eta_label": None,
        })
    return items


def _owner_id(cfg) -> int | None:
    owner = next((u for u in cfg.users if u.role == "owner"), None)
    if owner is None:
        return None
    row = core_db.get_user_by_chat_id(cfg.db_path, owner.telegram_chat_id)
    return row["id"] if row else None


@router.get("")
async def active_work(request: Request):
    require_user(request)
    cfg = request.app.state.cfg
    business_ctx = getattr(request.app.state, "business", None)

    items = _agent_run_items(cfg.db_path) + _staff_work_items(cfg.db_path) + _queued_work_items(business_ctx)

    owner_id = _owner_id(cfg)
    if owner_id is not None:
        items += _research_items(cfg.db_path, owner_id)
        items += _ops_plan_items(cfg.db_path, owner_id)

    items.sort(key=lambda i: (i["status"] != "running", i["started_at"]))
    return {"items": items}
