"""Finance dashboard section: balances/bills/income (read from the Era cache tables,
refreshed periodically by scheduler.py — not live per-request), savings goals CRUD,
and the calendar/projection views built from finance.py's pure computation."""
from datetime import date

from fastapi import APIRouter, HTTPException, Request

from ...core import db, finance, meal_plan_db
from ..auth import require_owner

router = APIRouter(prefix="/api/finance", tags=["finance"])


def _current_balance(db_path: str) -> float:
    """Spendable cash only — excludes investment/retirement (illiquid) and liability
    (owed, not owned) accounts, so projections and the safe-to-spend number reflect money
    that can actually cover upcoming bills, not a blended net-worth-ish figure. See
    finance.classify_account_type/group_account_balances for how accounts are bucketed."""
    return finance.spendable_balance(db.list_era_accounts(db_path))


@router.get("/summary")
async def summary(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    accounts = db.list_era_accounts(cfg.db_path)
    groups = finance.group_account_balances(accounts)
    # Each account gets its classify_account_type() bucket attached so BalanceCards.jsx
    # can group them for display without reimplementing the keyword classification in JS.
    annotated_accounts = [{**a, "balance_group": finance.classify_account_type(a.get("account_type"))} for a in accounts]
    return {
        "accounts": annotated_accounts,
        "recurring_charges": db.list_era_recurring_charges(cfg.db_path, include_excluded=False),
        # total_balance is spendable cash (not every account blended together) — see
        # _current_balance's docstring. cash/investment/liability_balance are the same
        # three numbers broken out explicitly for BalanceCards.jsx's grouped display.
        "total_balance": groups["cash"],
        "cash_balance": groups["cash"],
        "investment_balance": groups["investment"],
        "liability_balance": groups["liability"],
    }


@router.get("/projection")
async def projection(request: Request, horizon_days: int = 180):
    user = require_owner(request)
    cfg = request.app.state.cfg
    charges = db.list_forecast_charges(cfg.db_path, user["id"])
    series = finance.project_balance(_current_balance(cfg.db_path), charges, horizon_days=horizon_days)

    goals = db.list_savings_goals(cfg.db_path, user["id"])
    for goal in goals:
        goal["reachable_on"] = finance.goal_progress(series, goal["target_amount"])

    return {"series": series, "goals": goals, "safe_to_spend": meal_plan_db.get_safe_to_spend(cfg.db_path, user["id"])}


@router.get("/safe-to-spend")
async def safe_to_spend(request: Request):
    """A lightweight standalone version of /projection's safe_to_spend field, for callers
    (the Command Center's FinanceCard/FinanceModal) that want just this number without
    pulling the full projection series."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    return meal_plan_db.get_safe_to_spend(cfg.db_path, user["id"])


@router.get("/safety-buffer")
async def get_safety_buffer(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    return {"safety_buffer": float(db.get_setting(cfg.db_path, db.FINANCE_SAFETY_BUFFER_SETTING, "0") or 0)}


@router.put("/safety-buffer")
async def set_safety_buffer(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    value = body.get("safety_buffer")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise HTTPException(422, "safety_buffer must be a non-negative number")
    db.set_setting(cfg.db_path, db.FINANCE_SAFETY_BUFFER_SETTING, str(float(value)))
    return {"ok": True, "safety_buffer": float(value)}


@router.get("/insights")
async def insights(request: Request):
    """Era's forecast_spending/get_cash_flow/compare_spending_periods, cached by
    scheduler.refresh_era_cache — already chat-reachable, this just surfaces the same
    cached payloads on the dashboard. See db.upsert_era_insight's docstring for why these
    are cached opaquely rather than parsed into typed fields."""
    require_owner(request)
    cfg = request.app.state.cfg
    return db.list_era_insights(cfg.db_path)


@router.get("/net-worth")
async def net_worth_history(request: Request, limit: int = 365):
    require_owner(request)
    cfg = request.app.state.cfg
    return db.list_net_worth_snapshots(cfg.db_path, limit=limit)


@router.get("/calendar")
async def calendar_view(request: Request, start: str, end: str):
    user = require_owner(request)
    cfg = request.app.state.cfg
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)

    charges = db.list_forecast_charges(cfg.db_path, user["id"])
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


VALID_CADENCES = finance.VALID_CADENCES


@router.get("/recurring")
async def list_recurring(request: Request):
    """Management view: every Era-detected charge (excluded ones included, so they can
    be un-excluded) plus every manually-entered one. Forecasts use db.list_forecast_charges
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
