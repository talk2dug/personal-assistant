"""Meal planning: a plan spanning one pay period, with a per-day/per-meal-slot entry for
what's being eaten — a sibling of kitchen_db.py the same way kitchen_db.py is a sibling of
personal_db.py. References recipes (kitchen_db.py) and reuses kitchen_inventory for both
raw ingredients and batch-frozen prepared meals, but a meal plan's own shape (a date range,
per-slot entries, a shopping list snapshot, a shopping-day schedule) is its own bounded
concept, not a natural extension of any existing table.
"""
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone

from . import db, finance

SCHEMA = """
CREATE TABLE IF NOT EXISTS meal_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    period_start TEXT NOT NULL,       -- ISO date, inclusive
    period_end TEXT NOT NULL,         -- ISO date, the next payday
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'completed', 'abandoned')),
    max_deliveries INTEGER NOT NULL DEFAULT 2,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meal_plan_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_plan_id INTEGER NOT NULL REFERENCES meal_plans(id),
    plan_date TEXT NOT NULL,
    meal_type TEXT NOT NULL CHECK (meal_type IN ('breakfast', 'lunch', 'dinner')),
    recipe_id INTEGER REFERENCES recipes(id),           -- NULL for "order pizza" / an unplanned placeholder
    title TEXT NOT NULL,                                 -- denormalized display name
    servings_planned INTEGER,
    source TEXT NOT NULL DEFAULT 'fresh'
        CHECK (source IN ('fresh', 'frozen_substitute', 'frozen_premade', 'batch_frozen', 'leftover', 'eating_out')),
    batch_session_id INTEGER,          -- set in a later phase, once batch_cook_sessions exists
    freezer_pull_reminder_id INTEGER,  -- reminders.id, set once a pull-reminder exists (later phase)
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(meal_plan_id, plan_date, meal_type)
);
"""


def init_meal_plan_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(cursor):
    return [dict(r) for r in cursor.fetchall()]


# --- pay-cycle helper ------------------------------------------------------------

def _income_recurring_charges(db_path: str, owner_user_id: int) -> list[dict]:
    """If ANY manual income charge exists, trust it completely and ignore Era's own
    income detection entirely, rather than unioning and double-counting the same real
    paycheck twice. This is a real, confirmed case in this deployment: Era's own
    auto-detection sees one payroll deposit and (wrongly) calls it 'monthly', while the
    owner has manually entered the true semi-monthly pattern (paid on the 15th and on
    the last calendar day) as two separate corrected entries. Only fall back to Era's
    own detection when there is no manual income entry at all.
    """
    manual = [c for c in db.list_manual_recurring_charges(db_path, owner_user_id) if c["direction"] == "income"]
    if manual:
        return manual
    return [c for c in db.list_era_recurring_charges(db_path, include_excluded=False) if c["direction"] == "income"]


def get_pay_periods(db_path: str, owner_user_id: int, today: date | None = None) -> list[dict]:
    """Wraps finance.find_pay_periods with this deployment's real income data. When called
    mid-cycle, both the current and the next period come back (the current one flagged
    is_current=True) -- which one "plan meals" actually means is for the planning
    conversation itself to settle, not something this function guesses at."""
    charges = _income_recurring_charges(db_path, owner_user_id)
    return finance.find_pay_periods(charges, today or date.today())


# --- meal plans --------------------------------------------------------------------

_MEAL_TYPE_ORDER = "CASE meal_type WHEN 'breakfast' THEN 0 WHEN 'lunch' THEN 1 ELSE 2 END"


def create_meal_plan(
    db_path: str, owner_user_id: int, period_start: str, period_end: str,
    max_deliveries: int = 2, notes: str | None = None,
) -> int:
    now = _now()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO meal_plans
                   (owner_user_id, period_start, period_end, max_deliveries, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, period_start, period_end, max_deliveries, notes, now, now),
        )
        conn.commit()
        return cur.lastrowid


def get_meal_plan(db_path: str, owner_user_id: int, meal_plan_id: int) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM meal_plans WHERE id = ? AND owner_user_id = ?", (meal_plan_id, owner_user_id)
        ).fetchone()
        return dict(row) if row else None


def get_current_meal_plan(db_path: str, owner_user_id: int) -> dict | None:
    """The most recently created plan that's still draft or active -- what list_meal_plan
    defaults to when no id is given. A completed/abandoned plan never surfaces here."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT * FROM meal_plans WHERE owner_user_id = ? AND status IN ('draft', 'active')
               ORDER BY id DESC LIMIT 1""",
            (owner_user_id,),
        ).fetchone()
        return dict(row) if row else None


def list_meal_plan_entries(db_path: str, owner_user_id: int, meal_plan_id: int) -> list[dict]:
    """Empty (not an error) if meal_plan_id doesn't exist or belong to this owner --
    callers that need to distinguish "no entries yet" from "no such plan" should check
    get_meal_plan first, same as get_meal_plan_with_entries below does."""
    with closing(_connect(db_path)) as conn:
        plan = conn.execute(
            "SELECT id FROM meal_plans WHERE id = ? AND owner_user_id = ?", (meal_plan_id, owner_user_id)
        ).fetchone()
        if plan is None:
            return []
        return _rows(conn.execute(
            f"SELECT * FROM meal_plan_entries WHERE meal_plan_id = ? ORDER BY plan_date, {_MEAL_TYPE_ORDER}",
            (meal_plan_id,),
        ))


def get_meal_plan_with_entries(db_path: str, owner_user_id: int, meal_plan_id: int | None = None) -> dict | None:
    """meal_plan_id=None defaults to the current draft/active plan -- what list_meal_plan
    (the chat tool) uses so the model doesn't need to already know an id to ask "how's the
    plan looking so far"."""
    plan = get_meal_plan(db_path, owner_user_id, meal_plan_id) if meal_plan_id is not None \
        else get_current_meal_plan(db_path, owner_user_id)
    if plan is None:
        return None
    return {"plan": plan, "entries": list_meal_plan_entries(db_path, owner_user_id, plan["id"])}


def add_meal_plan_entry(
    db_path: str, owner_user_id: int, meal_plan_id: int, plan_date: str, meal_type: str, title: str, *,
    recipe_id: int | None = None, servings_planned: int | None = None,
    source: str = "fresh", notes: str | None = None,
) -> dict | None:
    """Upserts on (meal_plan_id, plan_date, meal_type) -- re-planning a slot overwrites it
    in place rather than duplicating. Returns None (rather than raising) if meal_plan_id
    doesn't belong to this owner, the same quiet-no-op-on-a-bad-id shape list_meal_plan_entries
    already uses."""
    now = _now()
    with closing(_connect(db_path)) as conn:
        plan = conn.execute(
            "SELECT id FROM meal_plans WHERE id = ? AND owner_user_id = ?", (meal_plan_id, owner_user_id)
        ).fetchone()
        if plan is None:
            return None
        conn.execute(
            """INSERT INTO meal_plan_entries
                   (meal_plan_id, plan_date, meal_type, recipe_id, title, servings_planned,
                    source, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(meal_plan_id, plan_date, meal_type) DO UPDATE SET
                   recipe_id = excluded.recipe_id, title = excluded.title,
                   servings_planned = excluded.servings_planned, source = excluded.source,
                   notes = excluded.notes, updated_at = excluded.updated_at""",
            (meal_plan_id, plan_date, meal_type, recipe_id, title, servings_planned, source, notes, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM meal_plan_entries WHERE meal_plan_id = ? AND plan_date = ? AND meal_type = ?",
            (meal_plan_id, plan_date, meal_type),
        ).fetchone()
        return dict(row)


def remove_meal_plan_entry(db_path: str, owner_user_id: int, entry_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """DELETE FROM meal_plan_entries WHERE id = ? AND meal_plan_id IN
                   (SELECT id FROM meal_plans WHERE owner_user_id = ?)""",
            (entry_id, owner_user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def finalize_meal_plan(db_path: str, owner_user_id: int, meal_plan_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE meal_plans SET status = 'active', updated_at = ? WHERE id = ? AND owner_user_id = ?",
            (_now(), meal_plan_id, owner_user_id),
        )
        conn.commit()
        return cur.rowcount > 0
