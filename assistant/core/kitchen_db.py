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
