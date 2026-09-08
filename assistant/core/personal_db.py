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
    -- Set once the due-date watchdog (scheduler.py's run_task_watchdog) has notified the
    -- owner this task is due, so a slow poll interval can't notify the same task twice.
    -- Same nullable-marker pattern as reminders.sent_at.
    notified_at TEXT,
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
        # Idempotent migration, same pattern as staff.py/db.py: CREATE TABLE IF NOT EXISTS
        # won't add a column to a table that already exists from an earlier version.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(personal_tasks)")}
        if "notified_at" not in cols:
            conn.execute("ALTER TABLE personal_tasks ADD COLUMN notified_at TEXT")
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
    # A due_at edit means any earlier due-date notification is stale -- without this, a
    # task rescheduled after it already fired once would silently never notify again.
    if "due_at" in allowed:
        allowed["notified_at"] = None
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE personal_tasks SET {sets}, updated_at = ? WHERE id = ? AND owner_user_id = ?",
            [*allowed.values(), _now(), task_id, owner_user_id],
        )
        conn.commit()
        return cur.rowcount > 0


def due_tasks(db_path: str, as_of: str | None = None):
    """Open/doing tasks whose due_at has arrived and haven't been notified yet.

    Same shape as db.due_reminders(): a nullable *_at marker column the watchdog polls
    against and stamps, so a slow tick can't double-fire and a restart naturally catches
    anything overdue on its next pass rather than losing it.
    """
    as_of = as_of or _now()
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(
            "SELECT * FROM personal_tasks WHERE notified_at IS NULL AND due_at IS NOT NULL"
            " AND due_at <= ? AND status IN ('open', 'doing')",
            (as_of,),
        ))


def mark_task_notified(db_path: str, task_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE personal_tasks SET notified_at = ? WHERE id = ?", (_now(), task_id))
        conn.commit()


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


def list_research(db_path: str, owner_user_id: int, limit: int = 10, status: str | None = None):
    """Recent personal errands. `status` narrows to e.g. 'requested' (still in flight) --
    added for the dashboard's Active Work panel, which wants only what's actually running
    right now rather than history it would otherwise have to filter out client-side.
    """
    query = "SELECT * FROM personal_research WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


# --- pantry ----------------------------------------------------------------------
# Retired in favor of kitchen_db.py's quantity-tracked kitchen_inventory (see
# kitchen_db.migrate_pantry_to_inventory) -- list_pantry survives only as that
# migration's one-time read of whatever this table still holds.

def list_pantry(db_path: str, owner_user_id: int, status: str | None = None):
    query = "SELECT * FROM pantry_items WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY CASE status WHEN 'out' THEN 0 WHEN 'low' THEN 1 ELSE 2 END, item"
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))
