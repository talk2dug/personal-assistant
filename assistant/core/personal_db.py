"""Storage for the owner's personal (non-business) life — projects, to-dos, and errands
delegated to Jarvis ("find me a doctor").

A sibling of business_db.py, which is itself a sibling of db.py — same reasoning applies
one level down: personal errands have nothing to do with the business's agent roster,
office sprites, or market/trend pipeline, so they get their own bounded-context module
rather than a scope column bolted onto business_projects/business_tasks. Same discipline
throughout: short-lived WAL connections, every read/write behind a function, owner id
passed explicitly.
"""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    goal TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'done', 'dropped')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS personal_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    project_id INTEGER REFERENCES personal_projects(id),
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'doing', 'done', 'dropped')),
    priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high')),
    due_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Errands with real legwork behind them ("find me a doctor", "look into X") -- delegated
-- rather than answered inline, and picked up by a background job (personal_agents.py).
CREATE TABLE IF NOT EXISTS personal_research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    question TEXT,
    findings TEXT,
    sources TEXT,
    status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested', 'done', 'failed')),
    project_id INTEGER REFERENCES personal_projects(id),
    created_at TEXT NOT NULL,
    completed_at TEXT
);

-- Deliberately a status enum (have/low/out), not a quantity+unit+reorder_at like
-- business_inventory -- "I'm half empty on X" is a status the owner reports in
-- conversation, not a count he's tracking, and the user explicitly asked for
-- something "not overly complicated."
CREATE TABLE IF NOT EXISTS pantry_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'have' CHECK (status IN ('have', 'low', 'out')),
    notes TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_user_id, item)
);
"""


def init_personal_db(db_path: str) -> None:
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


# --- projects ----------------------------------------------------------------

def create_project(db_path: str, owner_user_id: int, name: str, goal: str | None = None) -> int:
    now = _now()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO personal_projects (owner_user_id, name, goal, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (owner_user_id, name, goal, now, now),
        )
        conn.commit()
        return cur.lastrowid


def list_projects(db_path: str, owner_user_id: int, status: str | None = None):
    query = "SELECT * FROM personal_projects WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC"
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def update_project(db_path: str, owner_user_id: int, project_id: int, **fields) -> bool:
    allowed = {k: v for k, v in fields.items() if k in ("name", "goal", "status") and v is not None}
    if not allowed:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE personal_projects SET {sets}, updated_at = ? WHERE id = ? AND owner_user_id = ?",
            [*allowed.values(), _now(), project_id, owner_user_id],
        )
        conn.commit()
        return cur.rowcount > 0


# --- tasks -------------------------------------------------------------------

def create_task(
    db_path: str, owner_user_id: int, text: str, project_id: int | None = None,
    priority: str = "normal", due_at: str | None = None,
) -> int:
    now = _now()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO personal_tasks (owner_user_id, project_id, text, priority, due_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_user_id, project_id, text, priority, due_at, now, now),
        )
        conn.commit()
        return cur.lastrowid


def list_tasks(db_path: str, owner_user_id: int, status: str | None = None, project_id: int | None = None):
    query = (
        "SELECT t.*, p.name AS project_name FROM personal_tasks t"
        " LEFT JOIN personal_projects p ON p.id = t.project_id"
        " WHERE t.owner_user_id = ?"
    )
    params: list = [owner_user_id]
    if status:
        query += " AND t.status = ?"
        params.append(status)
    if project_id is not None:
        query += " AND t.project_id = ?"
        params.append(project_id)
    query += (
        " ORDER BY CASE t.status WHEN 'doing' THEN 0 WHEN 'open' THEN 1 ELSE 2 END,"
        " CASE t.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, t.created_at"
    )
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def update_task(db_path: str, owner_user_id: int, task_id: int, **fields) -> bool:
    allowed = {
        k: v for k, v in fields.items()
        if k in ("text", "status", "priority", "due_at", "project_id") and v is not None
    }
    if not allowed:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE personal_tasks SET {sets}, updated_at = ? WHERE id = ? AND owner_user_id = ?",
            [*allowed.values(), _now(), task_id, owner_user_id],
        )
        conn.commit()
        return cur.rowcount > 0


# --- research (delegated errands) --------------------------------------------

def create_research(
    db_path: str, owner_user_id: int, topic: str, question: str | None = None,
    project_id: int | None = None,
) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO personal_research (owner_user_id, topic, question, project_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (owner_user_id, topic, question, project_id, _now()),
        )
        conn.commit()
        return cur.lastrowid


def pending_research(db_path: str, limit: int = 3):
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(
            "SELECT * FROM personal_research WHERE status = 'requested' ORDER BY created_at LIMIT ?", (limit,)
        ))


def complete_research(db_path: str, research_id: int, findings: str, sources: str = "", status: str = "done") -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE personal_research SET findings = ?, sources = ?, status = ?, completed_at = ? WHERE id = ?",
            (findings, sources, status, _now(), research_id),
        )
        conn.commit()


def list_research(db_path: str, owner_user_id: int, limit: int = 10):
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(
            "SELECT * FROM personal_research WHERE owner_user_id = ? ORDER BY created_at DESC LIMIT ?",
            (owner_user_id, limit),
        ))


# --- pantry --------------------------------------------------------------------

def upsert_pantry_item(db_path: str, owner_user_id: int, item: str, status: str = "have", notes: str | None = None) -> int:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO pantry_items (owner_user_id, item, status, notes, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(owner_user_id, item) DO UPDATE SET
                 status = excluded.status,
                 notes = COALESCE(excluded.notes, pantry_items.notes),
                 updated_at = excluded.updated_at""",
            (owner_user_id, item, status, notes, _now()),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM pantry_items WHERE owner_user_id = ? AND item = ?", (owner_user_id, item)
        ).fetchone()[0]


def list_pantry(db_path: str, owner_user_id: int, status: str | None = None):
    query = "SELECT * FROM pantry_items WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY CASE status WHEN 'out' THEN 0 WHEN 'low' THEN 1 ELSE 2 END, item"
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def delete_pantry_item(db_path: str, owner_user_id: int, item_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "DELETE FROM pantry_items WHERE id = ? AND owner_user_id = ?", (item_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0
