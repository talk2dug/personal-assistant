"""Storage for the owner's own recipe catalog and kitchen inventory — a sibling of
personal_db.py the same way personal_db.py is a sibling of business_db.py: recipes,
what's on hand, and the shopping list it feeds have nothing to do with to-dos/projects/
credit tracking, so they get their own bounded-context module rather than growing
personal_db.py indefinitely.

Ingredients and steps are stored as JSON-in-TEXT (same convention db.py already uses for
pending_actions.arguments) rather than a normalized ingredients table: matching a
recipe's ingredients against inventory happens via LLM reasoning at cook time, not a SQL
join, so a relational ingredients table would add join complexity without buying
anything.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from .business_db import normalize_lead_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    servings INTEGER,
    ingredients TEXT NOT NULL,   -- JSON [{"name","quantity","unit","notes"}]
    steps TEXT NOT NULL,         -- JSON [string, ...] in order
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'photo', 'recipe_api')),
    photo_path TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Real quantities, replacing the old personal_db.pantry_items have/low/out enum (see
-- migrate_pantry_to_inventory) -- recipe deduction, Kroger-purchase quantities, and
-- photo-based "how much is left" all need actual math, which an enum can't do. The
-- have/low/out badge isn't gone, it's just derived from quantity vs low_threshold at
-- read time (see _inventory_row) rather than stored directly.
CREATE TABLE IF NOT EXISTS kitchen_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    normalized_item TEXT NOT NULL,   -- business_db.normalize_lead_name(item); matches
                                      -- "Milk" / "milk!" / "  MILK " as one row.
    quantity REAL NOT NULL DEFAULT 0,
    unit TEXT,
    low_threshold REAL,
    photo_path TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_user_id, normalized_item)
);

-- Audit trail for every inventory change, regardless of source -- lets a bad Kroger
-- name-match or a surprising deduction be traced back after the fact rather than just
-- overwritten silently.
CREATE TABLE IF NOT EXISTS kitchen_inventory_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    delta REAL,              -- NULL when this write was an absolute set (a photo recount)
    quantity_after REAL NOT NULL,
    unit TEXT,
    reason TEXT NOT NULL CHECK (reason IN (
        'purchase_manual', 'purchase_receipt', 'purchase_kroger',
        'cook_deduction', 'photo_recount', 'manual_adjust', 'pantry_migration'
    )),
    recipe_id INTEGER REFERENCES recipes(id),
    created_at TEXT NOT NULL
);

-- Dedup for sync_kroger_orders: view_order_history has no since/cursor param, always
-- returning the full history, so a repeat sync (the hourly job, or a manual re-trigger)
-- needs this to avoid double-counting an order already folded into kitchen_inventory.
-- One row per Kroger account, not per owner_user_id -- see sync_kroger_orders' docstring
-- for why order_id is actually the order's placed_at timestamp, not a real Kroger id.
CREATE TABLE IF NOT EXISTS kroger_synced_orders (
    order_id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL,
    synced_at TEXT NOT NULL
);
"""


def init_kitchen_db(db_path: str) -> None:
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


def _recipe_row(row: dict) -> dict:
    """Ingredients/steps are always native lists to callers -- JSON only at the storage
    boundary, same discipline db.py's pending_actions.arguments reader follows."""
    row = dict(row)
    row["ingredients"] = json.loads(row["ingredients"])
    row["steps"] = json.loads(row["steps"])
    return row


# --- recipes -------------------------------------------------------------------

def create_recipe(
    db_path: str, owner_user_id: int, title: str, ingredients: list, steps: list,
    servings: int | None = None, source: str = "manual", photo_path: str | None = None,
    notes: str | None = None,
) -> int:
    now = _now()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO recipes (owner_user_id, title, servings, ingredients, steps, source,"
            " photo_path, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_user_id, title, servings, json.dumps(ingredients), json.dumps(steps),
             source, photo_path, notes, now, now),
        )
        conn.commit()
        return cur.lastrowid


def list_recipes(db_path: str, owner_user_id: int, query: str | None = None) -> list[dict]:
    sql = "SELECT * FROM recipes WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if query:
        sql += " AND title LIKE ?"
        params.append(f"%{query}%")
    sql += " ORDER BY title"
    with closing(_connect(db_path)) as conn:
        return [_recipe_row(r) for r in _rows(conn.execute(sql, params))]


def get_recipe(db_path: str, owner_user_id: int, recipe_id: int) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM recipes WHERE id = ? AND owner_user_id = ?", (recipe_id, owner_user_id)
        ).fetchone()
        return _recipe_row(row) if row else None


def update_recipe(db_path: str, owner_user_id: int, recipe_id: int, **fields) -> bool:
    allowed = {
        k: v for k, v in fields.items()
        if k in ("title", "servings", "ingredients", "steps", "notes", "photo_path") and v is not None
    }
    if not allowed:
        return False
    if "ingredients" in allowed:
        allowed["ingredients"] = json.dumps(allowed["ingredients"])
    if "steps" in allowed:
        allowed["steps"] = json.dumps(allowed["steps"])
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE recipes SET {sets}, updated_at = ? WHERE id = ? AND owner_user_id = ?",
            [*allowed.values(), _now(), recipe_id, owner_user_id],
        )
        conn.commit()
        return cur.rowcount > 0


def delete_recipe(db_path: str, owner_user_id: int, recipe_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "DELETE FROM recipes WHERE id = ? AND owner_user_id = ?", (recipe_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0


# --- kitchen inventory ---------------------------------------------------------

def _inventory_row(row: dict) -> dict:
    """Adds the derived have/low/out badge -- see kitchen_inventory's schema comment for
    why this is computed at read time rather than stored."""
    row = dict(row)
    if row["quantity"] <= 0:
        row["status"] = "out"
    elif row["low_threshold"] is not None and row["quantity"] <= row["low_threshold"]:
        row["status"] = "low"
    else:
        row["status"] = "have"
    return row


def upsert_inventory_item(
    db_path: str, owner_user_id: int, item: str, *,
    quantity_delta: float | None = None, quantity_set: float | None = None,
    unit: str | None = None, low_threshold: float | None = None, notes: str | None = None,
    photo_path: str | None = None, reason: str = "manual_adjust", recipe_id: int | None = None,
) -> dict:
    """The one write primitive every inventory-changing path funnels through: manual
    purchase, receipt scan, Kroger sync, cook-time deduction, photo recount, or a direct
    adjustment. Matches by normalized name so "Milk" and "milk!" are the same row.

    Give quantity_set for an absolute recount (a photo estimate of what's left) or
    quantity_delta for anything additive/subtractive (a purchase, a deduction) -- not
    both. Quantity is clamped at 0; a deduction that would have gone negative is
    reported back via the returned dict's "shortfall" key (how much was missing) so a
    caller like cook-time deduction can tell the owner "you were short on X" instead of
    silently going negative or pretending nothing happened.
    """
    normalized = normalize_lead_name(item)
    now = _now()
    with closing(_connect(db_path)) as conn:
        existing = conn.execute(
            "SELECT * FROM kitchen_inventory WHERE owner_user_id = ? AND normalized_item = ?",
            (owner_user_id, normalized),
        ).fetchone()
        current_qty = existing["quantity"] if existing else 0.0

        if quantity_set is not None:
            new_qty = quantity_set
        elif quantity_delta is not None:
            new_qty = current_qty + quantity_delta
        else:
            new_qty = current_qty

        shortfall = -new_qty if new_qty < 0 else None
        new_qty = max(new_qty, 0.0)

        if existing is None:
            conn.execute(
                """INSERT INTO kitchen_inventory
                       (owner_user_id, item, normalized_item, quantity, unit, low_threshold,
                        photo_path, notes, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (owner_user_id, item, normalized, new_qty, unit, low_threshold, photo_path, notes, now),
            )
        else:
            conn.execute(
                """UPDATE kitchen_inventory SET
                       quantity = ?, unit = COALESCE(?, unit), low_threshold = COALESCE(?, low_threshold),
                       photo_path = COALESCE(?, photo_path), notes = COALESCE(?, notes), updated_at = ?
                   WHERE id = ?""",
                (new_qty, unit, low_threshold, photo_path, notes, now, existing["id"]),
            )

        conn.execute(
            """INSERT INTO kitchen_inventory_log
                   (owner_user_id, item, delta, quantity_after, unit, reason, recipe_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, item, quantity_delta, new_qty, unit, reason, recipe_id, now),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM kitchen_inventory WHERE owner_user_id = ? AND normalized_item = ?",
            (owner_user_id, normalized),
        ).fetchone()
        result = _inventory_row(row)
        result["shortfall"] = shortfall
        return result


def list_inventory(db_path: str, owner_user_id: int, status: str | None = None) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        rows = [_inventory_row(r) for r in _rows(
            conn.execute("SELECT * FROM kitchen_inventory WHERE owner_user_id = ? ORDER BY item", (owner_user_id,))
        )]
    if status:
        rows = [r for r in rows if r["status"] == status]
    return rows


def get_inventory_item(db_path: str, owner_user_id: int, item: str) -> dict | None:
    normalized = normalize_lead_name(item)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM kitchen_inventory WHERE owner_user_id = ? AND normalized_item = ?",
            (owner_user_id, normalized),
        ).fetchone()
        return _inventory_row(row) if row else None


def delete_inventory_item(db_path: str, owner_user_id: int, item_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "DELETE FROM kitchen_inventory WHERE id = ? AND owner_user_id = ?", (item_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0


def inventory_log(db_path: str, owner_user_id: int, item: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM kitchen_inventory_log WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if item:
        sql += " AND item = ?"
        params.append(item)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(sql, params))


def migrate_pantry_to_inventory(db_path: str, owner_user_id: int) -> list[str]:
    """One-time move off the old have/low/out pantry_items table (personal_db.py) into
    real quantities here -- see kitchen_inventory's schema comment for why. Placeholder
    quantities only (have->10, low->2, out->0, low_threshold=2); the owner will want a
    real photo-assisted pass afterward for accurate numbers.

    Idempotent by construction: migrated rows are deleted from pantry_items immediately
    after, so a second call (e.g. the next boot) finds nothing left to re-copy, and can
    never clobber real edits already made in kitchen_inventory since the first run.
    """
    from . import personal_db  # local import: only this one-time migration needs it

    placeholder_qty = {"have": 10.0, "low": 2.0, "out": 0.0}
    migrated = []
    for row in personal_db.list_pantry(db_path, owner_user_id):
        upsert_inventory_item(
            db_path, owner_user_id, row["item"],
            quantity_set=placeholder_qty.get(row["status"], 10.0),
            low_threshold=2.0, notes=row.get("notes"), reason="pantry_migration",
        )
        migrated.append(row["item"])
    if migrated:
        with closing(_connect(db_path)) as conn:
            conn.execute("DELETE FROM pantry_items WHERE owner_user_id = ?", (owner_user_id,))
            conn.commit()
    return migrated


# --- Kroger order sync -----------------------------------------------------------

def _unwrap_mcp_content(result: dict) -> dict:
    """Every raw kroger-mcp tool result comes back as {"is_error", "content": [json_text]}
    -- same shape kroger_recipe.py's _parse_content unwraps, duplicated here (rather than
    imported) so this storage module doesn't reach into a tool-schema module for a
    two-line helper."""
    if result.get("is_error"):
        raise RuntimeError("; ".join(result.get("content") or ["kroger tool call failed"]))
    content = result.get("content") or []
    return json.loads(content[0]) if content else {}


def sync_kroger_orders(kroger_mcp_client, db_path: str, owner_user_id: int) -> dict:
    """Pulls newly-placed Kroger orders into kitchen_inventory.

    IMPORTANT ASYMMETRY, confirmed via a live call before this was written (an empty
    real order history, plus kroger-mcp's own tool docstring): view_order_history does
    NOT reach Kroger's real purchase-history API -- Kroger's public API grants no such
    permission to third parties. It only replays orders that were placed by calling this
    same MCP server's own mark_order_placed, which only ever fires after Jarvis's own
    add_items_to_cart/bulk_add_to_cart built that cart in the first place. A Kroger trip
    made independently on Kroger's own app/site is invisible here by a hard limit of
    Kroger's API, not a bug in this function -- the owner's manual/receipt purchase entry
    (kitchen_tools.record_purchase, POST /purchases(/from-receipt)) is the real path for
    those, Kroger receipts included.

    A second real limitation, also only discoverable by reading kroger-mcp's own source:
    add_items_to_cart/bulk_add_to_cart never store a human-readable product name locally,
    only the raw product_id (a UPC-like code) -- so each line item's name has to be
    resolved with its own get_product_details call (a plain catalog read, no OAuth/
    confirmation gate) before it means anything in inventory. A lookup that fails (no
    preferred store set, product no longer available, transient error) falls back to a
    "Kroger item <product_id>" placeholder rather than dropping the item silently --
    the raw product_id stays in the log/notes either way so a bad name is correctable.

    Dedup via kroger_synced_orders: view_order_history has no since/cursor param and
    always returns the full history, so a repeat call (the hourly scheduler job, or a
    manual re-trigger) must not double-count an order already folded in. Orders carry no
    stable id of their own in view_order_history's response (mark_order_placed's own
    "order_id" -- history length at write time -- isn't recoverable later from the list),
    so each order's placed_at timestamp is used instead; it's set once by
    datetime.now().isoformat() at mark_order_placed time and never changes.
    """
    try:
        raw = kroger_mcp_client.call_tool("view_order_history", {"limit": 50})
        payload = _unwrap_mcp_content(raw)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    orders = payload.get("orders") or []
    with closing(_connect(db_path)) as conn:
        already_synced = {
            r["order_id"] for r in conn.execute(
                "SELECT order_id FROM kroger_synced_orders WHERE owner_user_id = ?", (owner_user_id,)
            ).fetchall()
        }

    synced_orders = 0
    items_updated = []
    for order in orders:
        order_id = order.get("placed_at")
        if not order_id or order_id in already_synced:
            continue

        for item in order.get("items") or []:
            product_id = item.get("product_id")
            if not product_id:
                continue
            quantity = item.get("quantity") or 1

            name = f"Kroger item {product_id}"
            try:
                details = _unwrap_mcp_content(
                    kroger_mcp_client.call_tool("get_product_details", {"product_id": product_id}))
                if details.get("success") and details.get("description"):
                    name = details["description"]
            except Exception:
                pass  # honest fallback name above, not a dropped item

            result = upsert_inventory_item(
                db_path, owner_user_id, name, quantity_delta=quantity,
                reason="purchase_kroger", notes=f"Kroger product_id {product_id}",
            )
            items_updated.append({"item": result["item"], "quantity": result["quantity"], "unit": result["unit"]})

        with closing(_connect(db_path)) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO kroger_synced_orders (order_id, owner_user_id, synced_at) VALUES (?, ?, ?)",
                (order_id, owner_user_id, _now()),
            )
            conn.commit()
        synced_orders += 1

    return {"ok": True, "synced_orders": synced_orders, "items_updated": items_updated}
