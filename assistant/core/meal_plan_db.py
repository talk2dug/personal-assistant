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
