"""Finance dashboard section: balances/bills/income (read from the Era cache tables,
refreshed periodically by scheduler.py — not live per-request), savings goals CRUD,
and the calendar/projection views built from finance.py's pure computation."""
from datetime import date

from fastapi import APIRouter, HTTPException, Request

from ...core import db, finance
from ..auth import require_owner

router = APIRouter(prefix="/api/finance", tags=["finance"])


def _current_balance(db_path: str) -> float:
    return sum(a["balance"] or 0 for a in db.list_era_accounts(db_path))


def _forecast_charges(db_path: str, owner_user_id: int) -> list[dict]:
    """Non-excluded Era-detected charges plus manually-entered ones — this is what
    projections and the calendar view should actually use, as opposed to the raw Era
    cache (which a management view shows in full, excluded ones included, for toggling)."""
    era_charges = db.list_era_recurring_charges(db_path, include_excluded=False)
    manual_charges = db.list_manual_recurring_charges(db_path, owner_user_id)
    return era_charges + manual_charges


@router.get("/summary")
async def summary(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    accounts = db.list_era_accounts(cfg.db_path)
    return {
        "accounts": accounts,
        "recurring_charges": db.list_era_recurring_charges(cfg.db_path, include_excluded=False),
        "total_balance": sum(a["balance"] or 0 for a in accounts),
    }


@router.get("/projection")
async def projection(request: Request, horizon_days: int = 180):
    user = require_owner(request)
    cfg = request.app.state.cfg
    charges = _forecast_charges(cfg.db_path, user["id"])
    series = finance.project_balance(_current_balance(cfg.db_path), charges, horizon_days=horizon_days)

    goals = db.list_savings_goals(cfg.db_path, user["id"])
    for goal in goals:
        goal["reachable_on"] = finance.goal_progress(series, goal["target_amount"])

    return {"series": series, "goals": goals}


@router.get("/calendar")
async def calendar_view(request: Request, start: str, end: str):
    user = require_owner(request)
    cfg = request.app.state.cfg
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)

    charges = _forecast_charges(cfg.db_path, user["id"])
    occurrences = finance.expand_occurrences(charges, start_date, end_date)
    reminders = [
        r for r in db.list_reminders(cfg.db_path, user["id"])
        if start <= r["due_at"][:10] <= end
    ]
    return {"occurrences": occurrences, "reminders": reminders}


@router.get("/goals")
async def list_goals(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    return db.list_savings_goals(cfg.db_path, user["id"])


@router.post("/goals")
async def create_goal(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    name = (body.get("name") or "").strip()
    target_amount = body.get("target_amount")
    if not name or not isinstance(target_amount, (int, float)):
        raise HTTPException(422, "name and target_amount are required")
    goal_id = db.create_savings_goal(cfg.db_path, user["id"], name, float(target_amount), body.get("target_date"))
    return {"id": goal_id}


@router.put("/goals/{goal_id}")
async def update_goal(goal_id: int, request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    ok = db.update_savings_goal(
        cfg.db_path, goal_id,
        name=body.get("name"), target_amount=body.get("target_amount"),
        target_date=body.get("target_date", "__unset__"),
    )
    if not ok:
        raise HTTPException(404, "goal not found")
    return {"ok": True}


@router.delete("/goals/{goal_id}")
async def delete_goal(goal_id: int, request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    ok = db.delete_savings_goal(cfg.db_path, goal_id)
    if not ok:
        raise HTTPException(404, "goal not found")
    return {"ok": True}


@router.get("/spending")
async def spending(request: Request, period: str = "this_month"):
    user = require_owner(request)
    cfg = request.app.state.cfg
    if period not in ("this_month", "last_30_days"):
        raise HTTPException(422, "period must be 'this_month' or 'last_30_days'")

    categories = db.list_era_category_spending(cfg.db_path, period)
    budgets_by_category = {b["category_key"]: b for b in db.list_budgets(cfg.db_path, user["id"])}
    for c in categories:
        budget = budgets_by_category.pop(c["category_key"], None)
        c["budget_limit"] = budget["monthly_limit"] if budget else None

    # Budgets set for a category with no spending this period still deserve a row (0 spent).
    for category_key, budget in budgets_by_category.items():
        categories.append({
            "category_key": category_key, "label": budget["category_label"], "amount": 0.0,
            "percent_of_total": None, "transaction_count": 0, "budget_limit": budget["monthly_limit"],
        })

    return {"period": period, "categories": categories}


@router.get("/budgets")
async def list_budgets(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    return db.list_budgets(cfg.db_path, user["id"])


@router.post("/budgets")
async def create_budget(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    category_key = (body.get("category_key") or "").strip()
    category_label = (body.get("category_label") or "").strip()
    monthly_limit = body.get("monthly_limit")
    if not category_key or not category_label or not isinstance(monthly_limit, (int, float)):
        raise HTTPException(422, "category_key, category_label, and monthly_limit are required")
    budget_id = db.create_budget(cfg.db_path, user["id"], category_key, category_label, float(monthly_limit))
    return {"id": budget_id}


@router.delete("/budgets/{budget_id}")
async def delete_budget(budget_id: int, request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    ok = db.delete_budget(cfg.db_path, budget_id)
    if not ok:
        raise HTTPException(404, "budget not found")
    return {"ok": True}


VALID_CADENCES = {
    "daily", "weekly", "biweekly", "semimonthly", "monthly", "quarterly",
    "semiannual", "yearly", "annual", finance.MONTHLY_ON_DAY, finance.MONTHLY_ON_LAST_DAY,
}


@router.get("/recurring")
async def list_recurring(request: Request):
    """Management view: every Era-detected charge (excluded ones included, so they can
    be un-excluded) plus every manually-entered one. Forecasts use _forecast_charges
    instead, which filters out excluded Era charges."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    return {
        "era": db.list_era_recurring_charges(cfg.db_path, include_excluded=True),
        "manual": db.list_manual_recurring_charges(cfg.db_path, user["id"]),
    }


@router.post("/recurring/manual")
async def create_manual_recurring(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    description = (body.get("description") or "").strip()
    amount = body.get("amount")
    direction = body.get("direction")
    cadence = body.get("cadence")
    next_expected_date = body.get("next_expected_date")
    if not description or not isinstance(amount, (int, float)) or direction not in ("income", "expense"):
        raise HTTPException(422, "description, amount, and direction ('income'|'expense') are required")
    if cadence not in VALID_CADENCES:
        raise HTTPException(422, f"cadence must be one of {sorted(VALID_CADENCES)}")
    if not next_expected_date:
        raise HTTPException(422, "next_expected_date is required")
    charge_id = db.create_manual_recurring_charge(
        cfg.db_path, user["id"], description, float(amount), direction, cadence, next_expected_date,
    )
    return {"id": charge_id}


@router.delete("/recurring/manual/{charge_id}")
async def delete_manual_recurring(charge_id: int, request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    ok = db.delete_manual_recurring_charge(cfg.db_path, charge_id)
    if not ok:
        raise HTTPException(404, "charge not found")
    return {"ok": True}


@router.put("/recurring/era/{charge_key}")
async def set_era_recurring_excluded(charge_key: str, request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    excluded = body.get("excluded")
    if not isinstance(excluded, bool):
        raise HTTPException(422, "excluded (bool) is required")
    ok = db.set_era_recurring_charge_excluded(cfg.db_path, charge_key, excluded)
    if not ok:
        raise HTTPException(404, "charge not found")
    return {"ok": True}
