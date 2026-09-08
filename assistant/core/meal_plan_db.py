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

from . import db, finance, kitchen_db
from .business_db import normalize_lead_name

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

-- A snapshot of one plan's net shopping needs -- deliberately its own table rather than
-- reusing kitchen_db.shopping_list_items: that table's CHECK constraint already has real
-- production rows under it (widening a CHECK on an already-populated table needs a real
-- rebuild-copy-rename migration, not a source edit -- see this project's own plan doc),
-- and a meal plan's shopping needs are a different concept anyway from the general
-- reactive low-stock queue. quantity_needed/quantity_on_hand/quantity_to_buy are all
-- model-reasoned free text (same convention as shopping_list_items.quantity_hint) rather
-- than real numbers -- netting "2 cups flour needed" against "half a bag on hand" is a
-- judgment call, not arithmetic a database should pretend to do.
CREATE TABLE IF NOT EXISTS meal_plan_shopping_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_plan_id INTEGER NOT NULL REFERENCES meal_plans(id),
    item TEXT NOT NULL,
    normalized_item TEXT NOT NULL,
    quantity_needed TEXT,
    quantity_on_hand TEXT,
    quantity_to_buy TEXT,
    category TEXT CHECK (category IN ('fresh_produce', 'frozen', 'pantry', 'dairy', 'meat', 'other')),
    kroger_product_id TEXT,
    on_sale INTEGER,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'added_to_cart', 'skipped', 'already_have')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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


# --- inventory-aware shopping list -------------------------------------------------

def gather_meal_plan_ingredients(db_path: str, owner_user_id: int, meal_plan_id: int) -> dict | None:
    """Collects every entry's recipe ingredients plus best-guess matching inventory rows
    for the model to reason quantities from -- applies/writes nothing, same
    propose-then-confirm split kitchen_db.cook_recipe already uses for the same reason
    (unit conversion and "how much do I actually still need" are judgment calls, not
    something this function should guess at). Only entries with a real recipe_id
    contribute ingredients; a freeform entry ("order pizza", "leftovers") has none to
    gather. Returns None if the plan doesn't exist/belong to this owner.
    """
    plan = get_meal_plan(db_path, owner_user_id, meal_plan_id)
    if plan is None:
        return None
    entries = list_meal_plan_entries(db_path, owner_user_id, meal_plan_id)
    inventory = kitchen_db.list_inventory(db_path, owner_user_id)

    needed = []
    for entry in entries:
        if entry["recipe_id"] is None:
            continue
        recipe = kitchen_db.get_recipe(db_path, owner_user_id, entry["recipe_id"])
        if recipe is None:
            continue
        for ing in recipe["ingredients"]:
            name = (ing.get("name") or "").strip()
            if not name:
                continue
            candidates = kitchen_db._ingredient_candidates(inventory, name)
            needed.append({
                "ingredient": ing,
                "from_meal": {"plan_date": entry["plan_date"], "meal_type": entry["meal_type"], "title": entry["title"]},
                "inventory_candidates": [
                    {"item": c["item"], "quantity": c["quantity"], "unit": c["unit"], "status": c["status"]}
                    for c in candidates
                ],
            })

    return {
        "meal_plan_id": meal_plan_id,
        "ingredients_needed": needed,
        "note": (
            "inventory_candidates are best-guess name matches, not confirmed, and the same "
            "ingredient may appear once per meal that uses it -- combine repeats yourself "
            "(e.g. two dinners both using onions) rather than treating them as separate "
            "needs. An empty inventory_candidates list means nothing on hand plausibly "
            "matches, so the full amount likely needs buying. Reason out a real "
            "quantity_to_buy per item given what's already on hand and how many meals use "
            "it, then call save_meal_plan_shopping_items with your own conclusions."
        ),
    }


def save_meal_plan_shopping_items(db_path: str, owner_user_id: int, meal_plan_id: int, items: list[dict]) -> dict | None:
    """Persists the model's reasoned shopping list. Replaces any previously saved list for
    this plan wholesale rather than merging -- regenerating is a full do-over, since the
    plan's own entries may have changed meaningfully since the last generation. Returns
    None if the plan doesn't exist/belong to this owner."""
    plan = get_meal_plan(db_path, owner_user_id, meal_plan_id)
    if plan is None:
        return None
    now = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute("DELETE FROM meal_plan_shopping_items WHERE meal_plan_id = ?", (meal_plan_id,))
        for item in items:
            name = (item.get("item") or "").strip()
            if not name:
                continue
            conn.execute(
                """INSERT INTO meal_plan_shopping_items
                       (meal_plan_id, item, normalized_item, quantity_needed, quantity_on_hand,
                        quantity_to_buy, category, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)""",
                (meal_plan_id, name, normalize_lead_name(name), item.get("quantity_needed"),
                 item.get("quantity_on_hand"), item.get("quantity_to_buy"), item.get("category"), now, now),
            )
        conn.commit()
    return {"meal_plan_id": meal_plan_id, "items": list_meal_plan_shopping_items(db_path, owner_user_id, meal_plan_id)}


def list_meal_plan_shopping_items(db_path: str, owner_user_id: int, meal_plan_id: int) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        plan = conn.execute(
            "SELECT id FROM meal_plans WHERE id = ? AND owner_user_id = ?", (meal_plan_id, owner_user_id)
        ).fetchone()
        if plan is None:
            return []
        return _rows(conn.execute(
            "SELECT * FROM meal_plan_shopping_items WHERE meal_plan_id = ? ORDER BY category, item",
            (meal_plan_id,),
        ))


def match_meal_plan_items_to_kroger(db_path: str, owner_user_id: int, meal_plan_id: int, kroger_mcp_client) -> dict | None:
    """Reuses kroger_recipe.match_ingredients against the saved quantity_to_buy items,
    fills kroger_product_id/on_sale on each matched row, and returns the proposed matches
    -- never writes to the cart itself. bulk_add_to_cart (already gated behind the
    existing pending_actions confirmation) is the actual write, called by the model
    directly once the owner confirms; no new confirmation gate is introduced here."""
    from . import kroger_recipe  # local import: avoids a module-load cycle with kitchen_tools

    items = list_meal_plan_shopping_items(db_path, owner_user_id, meal_plan_id)
    if not items:
        return {"meal_plan_id": meal_plan_id, "matches": []}

    result = kroger_recipe.match_ingredients(kroger_mcp_client, [i["item"] for i in items])
    matches = result.get("matches", [])

    now = _now()
    with closing(_connect(db_path)) as conn:
        for item, match in zip(items, matches):
            if not match.get("matched"):
                continue
            conn.execute(
                "UPDATE meal_plan_shopping_items SET kroger_product_id = ?, on_sale = ?, updated_at = ? WHERE id = ?",
                (match.get("product_id"), 1 if match.get("on_sale") else 0, now, item["id"]),
            )
        conn.commit()

    return {"meal_plan_id": meal_plan_id, "matches": matches}
