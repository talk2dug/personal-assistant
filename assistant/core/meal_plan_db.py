"""Meal planning: a plan spanning one pay period, with a per-day/per-meal-slot entry for
what's being eaten — a sibling of kitchen_db.py the same way kitchen_db.py is a sibling of
personal_db.py. References recipes (kitchen_db.py) and reuses kitchen_inventory for both
raw ingredients and batch-frozen prepared meals, but a meal plan's own shape (a date range,
per-slot entries, a shopping list snapshot, a shopping-day schedule) is its own bounded
concept, not a natural extension of any existing table.
"""
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

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

-- Phase 5: batch-cooked, vacuum-packed freezer meals for lazy nights/workweek lunches.
-- servings_made/portions_remaining are real integers, not model-reasoned free text like
-- meal_plan_shopping_items' quantities -- "how many portions did this batch make" is
-- something the owner states directly at cook time, a fact this table can just store,
-- not a judgment call to hand off. One portion is one freezer-ready unit regardless of
-- how many people it feeds (a portion packed to feed the household is still "1 portion"
-- for counting purposes) -- see add_meal_plan_entry, which always consumes exactly one
-- portion per batch_frozen entry rather than tracking partial-portion usage, which would
-- be false precision this table shouldn't pretend to have.
CREATE TABLE IF NOT EXISTS batch_cook_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    recipe_id INTEGER REFERENCES recipes(id),   -- NULL for a freeform title with no saved recipe
    title TEXT NOT NULL,                         -- denormalized display name, same convention as meal_plan_entries.title
    cooked_date TEXT NOT NULL,                   -- ISO date
    servings_made INTEGER NOT NULL,
    portions_remaining INTEGER NOT NULL,         -- starts equal to servings_made, decrements as meal_plan_entries consume it
    notes TEXT,
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
    source: str = "fresh", batch_session_id: int | None = None, notes: str | None = None,
) -> dict | None:
    """Upserts on (meal_plan_id, plan_date, meal_type) -- re-planning a slot overwrites it
    in place rather than duplicating. Returns None (rather than raising) if meal_plan_id
    doesn't belong to this owner, the same quiet-no-op-on-a-bad-id shape list_meal_plan_entries
    already uses.

    batch_session_id only takes effect when source='batch_frozen' -- given with any other
    source it's dropped rather than stored, since a dangling reference with nothing
    consuming it would be misleading. Whether this call is replacing a previous
    batch_frozen slot, creating a new one, or both, the old session's portion (if any) is
    restored before the new one is decremented, so re-planning a slot back and forth
    between batch sessions (or away from one) never leaks or double-spends a portion.
    """
    if source != "batch_frozen":
        batch_session_id = None
    now = _now()
    with closing(_connect(db_path)) as conn:
        plan = conn.execute(
            "SELECT id FROM meal_plans WHERE id = ? AND owner_user_id = ?", (meal_plan_id, owner_user_id)
        ).fetchone()
        if plan is None:
            return None

        existing = conn.execute(
            "SELECT source, batch_session_id FROM meal_plan_entries "
            "WHERE meal_plan_id = ? AND plan_date = ? AND meal_type = ?",
            (meal_plan_id, plan_date, meal_type),
        ).fetchone()
        if existing and existing["source"] == "batch_frozen" and existing["batch_session_id"] is not None:
            _adjust_batch_session_portions(conn, owner_user_id, existing["batch_session_id"], 1)

        conn.execute(
            """INSERT INTO meal_plan_entries
                   (meal_plan_id, plan_date, meal_type, recipe_id, title, servings_planned,
                    source, batch_session_id, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(meal_plan_id, plan_date, meal_type) DO UPDATE SET
                   recipe_id = excluded.recipe_id, title = excluded.title,
                   servings_planned = excluded.servings_planned, source = excluded.source,
                   batch_session_id = excluded.batch_session_id,
                   notes = excluded.notes, updated_at = excluded.updated_at""",
            (meal_plan_id, plan_date, meal_type, recipe_id, title, servings_planned,
             source, batch_session_id, notes, now, now),
        )

        if source == "batch_frozen" and batch_session_id is not None:
            _adjust_batch_session_portions(conn, owner_user_id, batch_session_id, -1)

        conn.commit()
        row = conn.execute(
            "SELECT * FROM meal_plan_entries WHERE meal_plan_id = ? AND plan_date = ? AND meal_type = ?",
            (meal_plan_id, plan_date, meal_type),
        ).fetchone()
        return dict(row)


def remove_meal_plan_entry(db_path: str, owner_user_id: int, entry_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        entry = conn.execute(
            """SELECT source, batch_session_id FROM meal_plan_entries WHERE id = ? AND meal_plan_id IN
                   (SELECT id FROM meal_plans WHERE owner_user_id = ?)""",
            (entry_id, owner_user_id),
        ).fetchone()
        if entry is None:
            return False
        if entry["source"] == "batch_frozen" and entry["batch_session_id"] is not None:
            _adjust_batch_session_portions(conn, owner_user_id, entry["batch_session_id"], 1)
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


# --- Phase 4: shopping-day scheduling + freezer-pull reminders ---------------------

def propose_shopping_days(db_path: str, owner_user_id: int, meal_plan_id: int) -> dict | None:
    """Gathers a finalized plan's entries (date/meal/title/source) alongside its
    max_deliveries cap for the model to reason a shopping-day schedule from -- decides
    nothing itself, same propose-then-confirm shape as gather_meal_plan_ingredients.
    Shelf-life/fresh-vs-frozen judgment is deliberately not reproduced here as a rule
    table (see this module's own docstring): the model looks at which entries are fresh
    and close to their plan_date vs. already frozen/pantry-stable, and proposes up to
    max_deliveries shopping dates spanning the period for him to confirm in chat.
    Nothing is written here -- there's no separate 'confirm' call, since a shopping-day
    schedule is conversation output, not stored state (unlike the shopping list itself,
    which save_meal_plan_shopping_items does persist). Returns None if the plan doesn't
    exist/belong to this owner.
    """
    plan = get_meal_plan(db_path, owner_user_id, meal_plan_id)
    if plan is None:
        return None
    entries = list_meal_plan_entries(db_path, owner_user_id, meal_plan_id)
    return {
        "meal_plan_id": meal_plan_id,
        "period_start": plan["period_start"],
        "period_end": plan["period_end"],
        "max_deliveries": plan["max_deliveries"],
        "entries": [
            {"plan_date": e["plan_date"], "meal_type": e["meal_type"], "title": e["title"], "source": e["source"]}
            for e in entries
        ],
        "note": (
            "Never exceed max_deliveries shopping dates. 'fresh' entries need their "
            "ingredients bought close to their own plan_date; frozen_substitute/"
            "frozen_premade/batch_frozen/leftover/eating_out entries need nothing bought "
            "close to their date at all, so they place no constraint on which days you "
            "pick. Reason out a real shopping-day schedule from this and present it to "
            "him for confirmation in chat -- this tool stores nothing itself."
        ),
    }


_FREEZER_PULL_SOURCES = ("frozen_substitute", "frozen_premade", "batch_frozen")


def schedule_freezer_pulls(db_path: str, owner_user_id: int, meal_plan_id: int) -> dict | None:
    """Creates a private reminder the evening before each freezer-sourced entry's
    plan_date ('pull X from the freezer tonight for tomorrow's Y'), via db.add_reminder,
    and stores the created reminder's id back onto that entry's own
    freezer_pull_reminder_id column. Idempotent: an entry that already has a reminder is
    skipped rather than creating a duplicate, so this is safe to call again after the
    plan changes (a newly added/changed freezer entry picks up a reminder; entries
    already covered are left alone). Meant to be called once a plan is finalized.
    Returns None if the plan doesn't exist/belong to this owner.
    """
    plan = get_meal_plan(db_path, owner_user_id, meal_plan_id)
    if plan is None:
        return None
    entries = list_meal_plan_entries(db_path, owner_user_id, meal_plan_id)
    created = []
    for entry in entries:
        if entry["source"] not in _FREEZER_PULL_SOURCES or entry["freezer_pull_reminder_id"] is not None:
            continue
        pull_date = date.fromisoformat(entry["plan_date"]) - timedelta(days=1)
        due_at = f"{pull_date.isoformat()}T18:00:00"
        text = f"Pull {entry['title']} from the freezer tonight for tomorrow's {entry['meal_type']}."
        reminder_id = db.add_reminder(db_path, owner_user_id, text, due_at, scope="private")
        _set_entry_freezer_pull_reminder(db_path, entry["id"], reminder_id)
        created.append({"entry_id": entry["id"], "reminder_id": reminder_id, "due_at": due_at, "text": text})
    return {"meal_plan_id": meal_plan_id, "created": created}


def _set_entry_freezer_pull_reminder(db_path: str, entry_id: int, reminder_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE meal_plan_entries SET freezer_pull_reminder_id = ?, updated_at = ? WHERE id = ?",
            (reminder_id, _now(), entry_id),
        )
        conn.commit()


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


# --- Phase 5: batch-cook-and-freeze tracking ---------------------------------------

def log_batch_cook_session(
    db_path: str, owner_user_id: int, title: str, servings_made: int, *,
    cooked_date: str | None = None, recipe_id: int | None = None, notes: str | None = None,
) -> dict:
    """Logs a batch-cook-and-freeze session (e.g. 'made a triple batch of chili, got 8
    portions in the freezer'). portions_remaining starts equal to servings_made and
    decrements by one each time a meal_plan_entry consumes this session (see
    add_meal_plan_entry's batch_session_id handling). cooked_date defaults to today."""
    now = _now()
    cooked = cooked_date or date.today().isoformat()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO batch_cook_sessions
                   (owner_user_id, recipe_id, title, cooked_date, servings_made, portions_remaining,
                    notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, recipe_id, title, cooked, servings_made, servings_made, notes, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM batch_cook_sessions WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)


def get_batch_cook_session(db_path: str, owner_user_id: int, session_id: int) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM batch_cook_sessions WHERE id = ? AND owner_user_id = ?", (session_id, owner_user_id)
        ).fetchone()
        return dict(row) if row else None


def list_batch_cook_sessions(db_path: str, owner_user_id: int, only_available: bool = True) -> list[dict]:
    """What's in the freezer right now -- only_available=True (the default, and what the
    list_batch_frozen_inventory chat tool uses) restricts to sessions that still have
    portions left; pass False for the full cook history including fully-consumed ones."""
    query = "SELECT * FROM batch_cook_sessions WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if only_available:
        query += " AND portions_remaining > 0"
    query += " ORDER BY cooked_date DESC, id DESC"
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def _adjust_batch_session_portions(conn: sqlite3.Connection, owner_user_id: int, session_id: int, delta: int) -> None:
    """Applies delta to one session's portions_remaining, clamped to [0, servings_made] --
    a quiet no-op (not an error) if session_id doesn't exist or belong to this owner, the
    same forgiving shape the rest of this module uses for a bad/foreign id. Takes an
    already-open connection so the meal_plan_entries write and this portion adjustment
    commit together as one transaction (see add_meal_plan_entry/remove_meal_plan_entry)."""
    row = conn.execute(
        "SELECT servings_made, portions_remaining FROM batch_cook_sessions WHERE id = ? AND owner_user_id = ?",
        (session_id, owner_user_id),
    ).fetchone()
    if row is None:
        return
    new_remaining = max(0, min(row["servings_made"], row["portions_remaining"] + delta))
    conn.execute(
        "UPDATE batch_cook_sessions SET portions_remaining = ?, updated_at = ? WHERE id = ?",
        (new_remaining, _now(), session_id),
    )
