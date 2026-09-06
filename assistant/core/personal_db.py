"""Storage for the owner's personal (non-business) life â€” projects, to-dos, errands
delegated to Jarvis ("find me a doctor"), pantry status, and now credit score history
and credit-report dispute tracking.

A sibling of business_db.py, which is itself a sibling of db.py â€” same reasoning applies
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

-- Manual credit score entries -- there is no live credit-bureau feed, so every row here
-- is something the owner told Jarvis after checking a score himself (a bureau site, a
-- lender's soft pull, Credit Karma, etc). One row per check, not a single current-value
-- column, so the dashboard can show a real trend over time rather than just a snapshot.
CREATE TABLE IF NOT EXISTS credit_score_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    bureau TEXT NOT NULL CHECK (bureau IN ('experian', 'equifax', 'transunion', 'other')),
    score INTEGER NOT NULL CHECK (score BETWEEN 300 AND 850),
    recorded_on TEXT NOT NULL,  -- local date the score was actually checked/pulled
    source TEXT,                -- e.g. 'Credit Karma', 'Chase Credit Journey', 'hard pull'
    notes TEXT,
    created_at TEXT NOT NULL
);

-- One disputed credit-report item's whole lifecycle, tracked per bureau. The same
-- inaccurate tradeline reported by two bureaus is two rows here, not one -- each bureau
-- is disputed, mailed, and resolved independently, with its own letter.
CREATE TABLE IF NOT EXISTS dispute_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    bureau TEXT NOT NULL CHECK (bureau IN ('experian', 'equifax', 'transunion', 'other')),
    creditor_name TEXT NOT NULL,
    account_reference TEXT,          -- account/reference number on the report, if any
    item_description TEXT NOT NULL,  -- what's being disputed
    reason TEXT NOT NULL,            -- why it's inaccurate -- goes into the dispute letter
    status TEXT NOT NULL DEFAULT 'drafted' CHECK (status IN ('drafted', 'mailed', 'resolved')),
    resolution TEXT,                 -- filled in once status -> resolved
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Each real LetterStream mailing attempt against a dispute item. Kept separate from
-- dispute_items (rather than columns on it) because a follow-up letter -- no response
-- in 30 days, say -- is a second real mailing against the same item, not a second item.
-- status is 'quoted' the moment LetterStream's preauth prices it (no money spent, nothing
-- mailed) and only becomes 'mailed' after letterstream_authorize_mail has actually been
-- confirmed and run -- see personal_tools.py's record_dispute_letter_mailed.
CREATE TABLE IF NOT EXISTS dispute_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_item_id INTEGER NOT NULL REFERENCES dispute_items(id),
    letter_text TEXT NOT NULL,
    recipient_name TEXT NOT NULL,
    recipient_address TEXT NOT NULL,
    recipient_address_2 TEXT,
    recipient_city TEXT NOT NULL,
    recipient_state TEXT NOT NULL,
    recipient_zip TEXT NOT NULL,
    mail_type TEXT NOT NULL DEFAULT 'certified',
    quoted_cost TEXT,       -- LetterStream's preauth quote, straight from its response
    authcode TEXT,          -- preauth authcode; consumed once by letterstream_authorize_mail
    job_name TEXT,
    doc_id TEXT,            -- stable id used for later tracking lookups
    tracking_number TEXT,   -- USPS cert/tracking number, once known
    status TEXT NOT NULL DEFAULT 'quoted' CHECK (status IN ('quoted', 'mailed')),
    quoted_at TEXT NOT NULL,
    mailed_at TEXT
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


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


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


# --- credit score history --------------------------------------------------------

def create_credit_score_entry(
    db_path: str, owner_user_id: int, bureau: str, score: int, recorded_on: str | None = None,
    source: str | None = None, notes: str | None = None,
) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO credit_score_entries"
            " (owner_user_id, bureau, score, recorded_on, source, notes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_user_id, bureau, score, recorded_on or _today(), source, notes, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_credit_score_entries(db_path: str, owner_user_id: int, bureau: str | None = None):
    query = "SELECT * FROM credit_score_entries WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if bureau:
        query += " AND bureau = ?"
        params.append(bureau)
    query += " ORDER BY recorded_on ASC, id ASC"
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def delete_credit_score_entry(db_path: str, owner_user_id: int, entry_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "DELETE FROM credit_score_entries WHERE id = ? AND owner_user_id = ?", (entry_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0


# --- dispute items -----------------------------------------------------------

def create_dispute_item(
    db_path: str, owner_user_id: int, bureau: str, creditor_name: str, item_description: str,
    reason: str, account_reference: str | None = None,
) -> int:
    now = _now()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO dispute_items"
            " (owner_user_id, bureau, creditor_name, account_reference, item_description, reason,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_user_id, bureau, creditor_name, account_reference, item_description, reason, now, now),
        )
        conn.commit()
        return cur.lastrowid


def list_dispute_items(db_path: str, owner_user_id: int, status: str | None = None, bureau: str | None = None):
    query = "SELECT * FROM dispute_items WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    if bureau:
        query += " AND bureau = ?"
        params.append(bureau)
    query += " ORDER BY CASE status WHEN 'drafted' THEN 0 WHEN 'mailed' THEN 1 ELSE 2 END, updated_at DESC"
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def get_dispute_item(db_path: str, owner_user_id: int, dispute_item_id: int):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM dispute_items WHERE id = ? AND owner_user_id = ?",
            (dispute_item_id, owner_user_id),
        ).fetchone()
        return dict(row) if row else None


def update_dispute_item(db_path: str, owner_user_id: int, dispute_item_id: int, **fields) -> bool:
    allowed = {
        k: v for k, v in fields.items()
        if k in ("status", "resolution", "creditor_name", "item_description", "reason", "account_reference")
        and v is not None
    }
    if not allowed:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE dispute_items SET {sets}, updated_at = ? WHERE id = ? AND owner_user_id = ?",
            [*allowed.values(), _now(), dispute_item_id, owner_user_id],
        )
        conn.commit()
        return cur.rowcount > 0


# --- dispute letters (LetterStream mailings against an item) -----------------

def create_dispute_letter(
    db_path: str, dispute_item_id: int, letter_text: str, recipient_name: str, recipient_address: str,
    recipient_city: str, recipient_state: str, recipient_zip: str, recipient_address_2: str | None = None,
    mail_type: str = "certified", quoted_cost: str | None = None, authcode: str | None = None,
    job_name: str | None = None, doc_id: str | None = None,
) -> int:
    now = _now()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO dispute_letters"
            " (dispute_item_id, letter_text, recipient_name, recipient_address, recipient_address_2,"
            "  recipient_city, recipient_state, recipient_zip, mail_type, quoted_cost, authcode,"
            "  job_name, doc_id, status, quoted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'quoted', ?)",
            (dispute_item_id, letter_text, recipient_name, recipient_address, recipient_address_2,
             recipient_city, recipient_state, recipient_zip, mail_type, quoted_cost, authcode,
             job_name, doc_id, now),
        )
        conn.commit()
        return cur.lastrowid


def list_dispute_letters(db_path: str, owner_user_id: int, dispute_item_id: int):
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(
            "SELECT dl.* FROM dispute_letters dl"
            " JOIN dispute_items di ON di.id = dl.dispute_item_id"
            " WHERE dl.dispute_item_id = ? AND di.owner_user_id = ?"
            " ORDER BY dl.quoted_at DESC",
            (dispute_item_id, owner_user_id),
        ))


def get_dispute_letter(db_path: str, owner_user_id: int, dispute_letter_id: int):
    """Owner-scoped through a join on the parent item -- dispute_letters has no
    owner_user_id column of its own, so ownership is only ever provable this way."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT dl.*, di.bureau AS bureau FROM dispute_letters dl"
            " JOIN dispute_items di ON di.id = dl.dispute_item_id"
            " WHERE dl.id = ? AND di.owner_user_id = ?",
            (dispute_letter_id, owner_user_id),
        ).fetchone()
        return dict(row) if row else None


def mark_dispute_letter_mailed(
    db_path: str, owner_user_id: int, dispute_letter_id: int, tracking_number: str | None = None,
) -> bool:
    """Bookkeeping only -- never calls LetterStream itself. This must only ever be
    called after letterstream_authorize_mail has actually run and succeeded (see
    personal_tools.py's record_dispute_letter_mailed / the credit route's mail_letter),
    since this is what flips a dispute item from 'drafted' to 'mailed' in the tracker.
    """
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT dl.dispute_item_id FROM dispute_letters dl"
            " JOIN dispute_items di ON di.id = dl.dispute_item_id"
            " WHERE dl.id = ? AND di.owner_user_id = ?",
            (dispute_letter_id, owner_user_id),
        ).fetchone()
        if row is None:
            return False
        now = _now()
        conn.execute(
            "UPDATE dispute_letters SET status = 'mailed', mailed_at = ?,"
            " tracking_number = COALESCE(?, tracking_number) WHERE id = ?",
            (now, tracking_number, dispute_letter_id),
        )
        conn.execute(
            "UPDATE dispute_items SET status = 'mailed', updated_at = ? WHERE id = ?",
            (now, row["dispute_item_id"]),
        )
        conn.commit()
        return True


def update_dispute_letter_tracking(db_path: str, owner_user_id: int, dispute_letter_id: int, tracking_number: str) -> bool:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT dl.id FROM dispute_letters dl JOIN dispute_items di ON di.id = dl.dispute_item_id"
            " WHERE dl.id = ? AND di.owner_user_id = ?",
            (dispute_letter_id, owner_user_id),
        ).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE dispute_letters SET tracking_number = ? WHERE id = ?", (tracking_number, dispute_letter_id))
        conn.commit()
        return True
