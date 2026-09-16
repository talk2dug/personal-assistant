"""Storage for the owner's personal (non-business) life — projects, to-dos, and errands
delegated to Jarvis ("find me a doctor").

A sibling of business_db.py, which is itself a sibling of db.py — same reasoning applies
one level down: personal errands have nothing to do with the business's agent roster,
office sprites, or market/trend pipeline, so they get their own bounded-context module
rather than a scope column bolted onto business_projects/business_tasks. Same discipline
throughout: short-lived WAL connections, every read/write behind a function, owner id
passed explicitly.
"""
import re
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

-- The debt tracker. A sibling of credit_score_entries/dispute_items above, and it exists
-- because of exactly one thing the owner said when he was asked to list his debts out so
-- payoff priorities could be assigned:
--
--   "we will add the actual debt once the email scanner is woring most of the debt is in
--    there and i dont have it written down, also when text messages come in ill let jarvis
--    kow and he can add them as well. In assition once i get the scanner to email
--    configured, there will be physical main [mail] with debt that needs tobe tracked"
--
-- He is never going to type in a list of debts, because he does not have one. So this
-- table is ASSEMBLED from the three channels his debt information actually arrives
-- through -- his existing mail history (mail_debts.py), him telling Jarvis in chat, and
-- scanned physical mail, which lands in the same mailbox and therefore flows through the
-- same path with no extra machinery. Nothing here assumes he will ever sit down and
-- enter one by hand.
--
-- TWO INDEPENDENT STATE COLUMNS, and keeping them separate is load-bearing:
--
--   tracking_state  Is this a real debt of his AT ALL? 'proposed' is a guess a classifier
--                   made from an email and NOT yet part of his debt picture; 'tracked'
--                   means he confirmed it (or told Jarvis about it himself, which is the
--                   same thing); 'dismissed' means he said no. Every read that totals,
--                   ranks, or reports debt filters to 'tracked' -- a fuzzy email
--                   classifier must never be able to move his debt total on its own.
--   status          The real-world lifecycle of a debt he IS tracking: active, paid_off,
--                   in_dispute, closed. Meaningless until tracking_state='tracked'.
--
-- Merging those two into one enum was the tempting shortcut and it is wrong for the same
-- reason mail_db's email_bill_scans is a separate table from email_bills: a list of his
-- debts that is actually a list of guesses makes every single read of it filter for what
-- it is really about, and one missed filter silently reports invented money as his.
--
-- 'proposed' is ALSO what makes deduplication work across a historical mail sweep. Twelve
-- monthly statements from one card must converge on ONE debt with twelve balance
-- observations. Because statement #1 leaves a proposed row behind, statement #2 matches
-- it (find_matching_debt below searches proposed rows too) and appends an observation
-- instead of proposing a second card. See mail_debts.py.
--
-- account_last4 is never a full account number -- sanitize_account_last4() below reduces
-- whatever it is handed to at most four characters, so this column cannot hold one even
-- if a model hands one over. '' (never NULL) when unknown, so the UNIQUE index actually
-- constrains: SQLite treats NULLs as distinct, which would let duplicates straight in.
--
-- There is deliberately NO balance/apr/minimum_payment column here. Those are dated
-- observations (debt_observations below), the same way credit_score_entries models a
-- score as a series of dated checks rather than one mutable number: watching a balance
-- trend down is the entire point of a payoff effort, and an UPDATE would destroy it.
CREATE TABLE IF NOT EXISTS debts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    creditor TEXT NOT NULL,        -- as written/said, e.g. 'Capital One Bank (USA), N.A.'
    creditor_key TEXT NOT NULL,    -- normalize_creditor() of the above; the dedup key
    account_last4 TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'other' CHECK (kind IN (
        'credit_card', 'loan', 'student_loan', 'auto', 'mortgage', 'medical',
        'collections', 'other')),
    -- The owner's payoff order. NULL means he has not decided, which is the honest
    -- default and the normal state: he confirmed assigning priorities is a joint,
    -- ongoing effort, so nothing in this codebase ever writes this column on its own.
    -- suggest_payoff_orders() proposes orderings; only he sets one.
    priority INTEGER,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paid_off', 'in_dispute', 'closed')),
    tracking_state TEXT NOT NULL DEFAULT 'tracked'
        CHECK (tracking_state IN ('proposed', 'tracked', 'dismissed')),
    due_day INTEGER,               -- day of the monthly cycle payment is due, when known
    -- Provenance of the DEBT ITSELF (each fact's provenance rides on its observation):
    -- 'email' = a classifier inferred this account exists from a message, 'chat' = he
    -- told Jarvis, 'manual' = entered through the UI.
    origin TEXT NOT NULL DEFAULT 'chat' CHECK (origin IN ('email', 'chat', 'manual')),
    origin_detail TEXT,            -- human-readable, e.g. 'statement email from X, 2026-03-02'
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_user_id, creditor_key, account_last4)
);
CREATE INDEX IF NOT EXISTS idx_debts_tracking
    ON debts(owner_user_id, tracking_state, status);

-- One dated observation of what a debt looked like at a point in time. Modelled on
-- credit_score_entries: separate dated rows feeding a current view, never one mutable
-- number overwritten in place.
--
-- A statement states balance, APR and minimum payment together as facts of the same
-- statement date, so all three live on one observation rather than in three parallel
-- series. They are resolved back INDEPENDENTLY though (see attach_current_values): the
-- newest observation might carry only a balance, and the last APR he has is then whatever
-- the newest observation that actually stated one said. "Most recent overall" would blank
-- out an APR that is still perfectly good.
--
-- Every model-extracted number is stored BOTH as written and, only when it parses
-- unambiguously, as a real typed value -- the paired text/typed convention mail_bills.py
-- established. "$80-$120" or "24.99%-29.99% variable" keeps its wording and leaves the
-- numeric column NULL rather than inventing precision. A NULL balance means "no number
-- was stated", never "zero".
--
-- source/source_ref/source_detail/confidence are the provenance the UI shows, and the
-- distinction it exists to make is: did HE say this, or did a classifier infer it from an
-- email? confirmed=1 only ever means the former (or a proposal he has since confirmed).
CREATE TABLE IF NOT EXISTS debt_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    debt_id INTEGER NOT NULL REFERENCES debts(id),
    observed_on TEXT NOT NULL,     -- ISO date the facts were TRUE (statement date), not insert time
    balance_text TEXT NOT NULL DEFAULT '',
    balance REAL,
    apr_text TEXT NOT NULL DEFAULT '',
    apr REAL,
    minimum_payment_text TEXT NOT NULL DEFAULT '',
    minimum_payment REAL,
    due_date TEXT,                 -- ISO, only when a real parseable one was given
    due_date_text TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL CHECK (source IN ('email', 'chat', 'manual')),
    source_ref TEXT NOT NULL DEFAULT '',   -- 'folder:uid' for email; '' for anything else
    source_detail TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT '',   -- as the model gave it; '' when he stated it
    confirmed INTEGER NOT NULL DEFAULT 0,  -- 1 = came from him directly, not inferred
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_debt_observations_debt
    ON debt_observations(debt_id, observed_on);
-- One observation per source message, ever. Partial (source_ref <> '') on purpose: chat
-- and manual observations carry no ref and he must be able to record as many of those as
-- he likes -- a plain UNIQUE would cap him at one balance per debt for the rest of time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_debt_observations_source
    ON debt_observations(debt_id, source_ref) WHERE source_ref <> '';
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


# --- debts + dated balance observations ---------------------------------------
# (see the debts/debt_observations schema comments above, and mail_debts.py)

DEBT_KINDS = (
    "credit_card", "loan", "student_loan", "auto", "mortgage", "medical", "collections", "other",
)
DEBT_STATUSES = ("active", "paid_off", "in_dispute", "closed")
DEBT_TRACKING_STATES = ("proposed", "tracked", "dismissed")
DEBT_SOURCES = ("email", "chat", "manual")

# Corporate boilerplate that is never the distinguishing part of a creditor's name, so
# "Capital One", "CAPITAL ONE BANK (USA), N.A." and "Capital One Card Services" all reduce
# to the same key. Deliberately NOT in here: 'credit', 'loan', 'finance', 'one', 'main' --
# they look generic but are load-bearing brand parts (Credit One, OneMain Financial), and
# stripping them would merge two genuinely different creditors into one debt, which is the
# one direction this normalization must never fail in.
_CREDITOR_NOISE_TOKENS = frozenset({
    "bank", "banks", "na", "n", "a", "usa", "us", "inc", "llc", "llp", "ltd", "plc",
    "corp", "corporation", "company", "co", "the", "of", "and", "fsb", "association",
    "card", "cards", "cardservices", "cardmember", "cardmembers",
    "services", "service", "servicing", "financial", "group",
})
# Below this, a stripped key is too short to be safely distinctive ("M&T Bank" -> "mt"),
# so the un-stripped form is used instead. Erring toward NOT matching is the safe side:
# an unmatched debt becomes a second row the owner can merge, a wrongly matched one
# silently folds two real debts into one and loses money from his picture.
_MIN_CREDITOR_KEY_CHARS = 4


def normalize_creditor(name: str | None) -> str:
    """The dedup key for a creditor name. Lowercased, punctuation dropped, corporate
    boilerplate removed -- with a fallback to the un-stripped form whenever stripping
    would leave something too short to be distinctive. Returns '' for an empty name."""
    tokens = [t for t in re.split(r"[^0-9a-z]+", (name or "").lower()) if t]
    if not tokens:
        return ""
    full = "".join(tokens)
    stripped = "".join(t for t in tokens if t not in _CREDITOR_NOISE_TOKENS)
    return stripped if len(stripped) >= _MIN_CREDITOR_KEY_CHARS else full


def sanitize_account_last4(value: str | None) -> str:
    """At most the last four alphanumeric characters of whatever it is handed.

    This is a SAFETY function, not a formatting one: it is the only way an account
    identifier enters this table, so a model (or a person) handing over a full
    "4111 1111 1111 1234" cannot store one -- only "1234" survives. Returns '' for
    anything with fewer than two usable characters, which is not an identifier at all.
    """
    cleaned = re.sub(r"[^0-9A-Za-z]", "", str(value or ""))
    if len(cleaned) < 2:
        return ""
    return cleaned[-4:]


def match_debt(candidates: list[dict], creditor_key: str, account_last4: str = "") -> dict:
    """Which existing debt (if any) a newly-seen creditor+account belongs to.

    Pure, so the hardest correctness problem in this feature is testable without a
    database or a mailbox. `candidates` is every non-dismissed debt of the owner's --
    proposed rows INCLUDED, which is what makes twelve monthly statements converge on one
    debt instead of twelve (see the debts schema comment).

    Returns {"debt_id": int|None, "ambiguous": bool, "reason": str} and sometimes
    "learned_account_last4". Three outcomes, and the distinction matters:

      debt_id set        attach the observation here.
      None, ambiguous    two or more accounts could plausibly be this one. Do NOT guess
                         and do NOT create -- ask him. Guessing here either merges two
                         real debts or splits one; both corrupt the picture silently.
      None, not ambiguous  genuinely new. Safe to create.

    The one inference it does make is the useful one: a statement that names an account
    ending, matched against a single debt for that creditor whose account number was never
    known, adopts it and reports the number it learned. That is the ordinary sequence when
    the first statement found says only "Capital One" and a later one says "ending 4821".
    It is deliberately refused the moment any OTHER account for that creditor is already
    known, because then the unknown one is just as likely to be a third card.
    """
    account_last4 = sanitize_account_last4(account_last4)
    same_creditor = [c for c in candidates if c.get("creditor_key") == creditor_key]
    if not creditor_key or not same_creditor:
        return {"debt_id": None, "ambiguous": False, "reason": "no existing debt for this creditor"}

    if account_last4:
        exact = [c for c in same_creditor if (c.get("account_last4") or "") == account_last4]
        if exact:
            return {"debt_id": exact[0]["id"], "ambiguous": False,
                    "reason": f"same creditor and same account ending {account_last4}"}
        known = [c for c in same_creditor if (c.get("account_last4") or "")]
        unknown = [c for c in same_creditor if not (c.get("account_last4") or "")]
        if len(unknown) == 1 and not known:
            return {"debt_id": unknown[0]["id"], "ambiguous": False,
                    "learned_account_last4": account_last4,
                    "reason": ("same creditor, and the account number wasn't known until "
                               f"this message gave it ({account_last4})")}
        if unknown:
            return {"debt_id": None, "ambiguous": True,
                    "reason": (f"{len(same_creditor)} accounts with this creditor, and it's "
                               f"not clear whether ending {account_last4} is one of them or new")}
        return {"debt_id": None, "ambiguous": False,
                "reason": f"same creditor but a different account ({account_last4})"}

    if len(same_creditor) == 1:
        return {"debt_id": same_creditor[0]["id"], "ambiguous": False,
                "reason": "the only account tracked for this creditor"}
    return {"debt_id": None, "ambiguous": True,
            "reason": (f"{len(same_creditor)} accounts with this creditor and no account "
                       "number in the message to tell them apart")}


def create_debt(
    db_path: str, owner_user_id: int, creditor: str, account_last4: str = "",
    kind: str = "other", tracking_state: str = "tracked", origin: str = "chat",
    origin_detail: str | None = None, due_day: int | None = None, notes: str | None = None,
    status: str = "active",
) -> int:
    """Returns the debt's id -- the existing one if this creditor+account is already
    known, otherwise the new row. Same ON CONFLICT DO NOTHING shape as mail_db.create_bill:
    a second sighting must never overwrite a row the owner has already ruled on.

    priority is deliberately not a parameter. Payoff order is his call, set only through
    set_debt_priority, never as a side effect of something noticing a debt exists.
    """
    if kind not in DEBT_KINDS:
        raise ValueError(f"invalid debt kind: {kind!r}")
    if tracking_state not in DEBT_TRACKING_STATES:
        raise ValueError(f"invalid tracking_state: {tracking_state!r}")
    if origin not in DEBT_SOURCES:
        raise ValueError(f"invalid origin: {origin!r}")
    if status not in DEBT_STATUSES:
        raise ValueError(f"invalid status: {status!r}")

    creditor_key = normalize_creditor(creditor)
    account_last4 = sanitize_account_last4(account_last4)
    now = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO debts
                   (owner_user_id, creditor, creditor_key, account_last4, kind, status,
                    tracking_state, due_day, origin, origin_detail, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_user_id, creditor_key, account_last4) DO NOTHING""",
            (owner_user_id, creditor, creditor_key, account_last4, kind, status,
             tracking_state, due_day, origin, origin_detail, notes, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM debts WHERE owner_user_id = ? AND creditor_key = ? AND account_last4 = ?",
            (owner_user_id, creditor_key, account_last4),
        ).fetchone()
        return row["id"]


def find_matching_debt(
    db_path: str, owner_user_id: int, creditor: str, account_last4: str = "",
) -> dict:
    """match_debt against everything the owner has that isn't dismissed."""
    with closing(_connect(db_path)) as conn:
        candidates = _rows(conn.execute(
            "SELECT id, creditor, creditor_key, account_last4, tracking_state FROM debts"
            " WHERE owner_user_id = ? AND tracking_state != 'dismissed' ORDER BY id",
            (owner_user_id,),
        ))
    return match_debt(candidates, normalize_creditor(creditor), account_last4)


def get_debt(db_path: str, owner_user_id: int, debt_id: int):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM debts WHERE id = ? AND owner_user_id = ?", (debt_id, owner_user_id)
        ).fetchone()
        if row is None:
            return None
        debt = dict(row)
        observations = _rows(conn.execute(
            "SELECT * FROM debt_observations WHERE debt_id = ? ORDER BY observed_on, id", (debt_id,)
        ))
    return attach_current_values(debt, observations)


def update_debt(db_path: str, owner_user_id: int, debt_id: int, **fields) -> bool:
    """Edits a debt's identity or lifecycle. Never touches observations: a wrong balance is
    corrected by recording a new observation, not by rewriting history."""
    allowed = {
        k: v for k, v in fields.items()
        if k in ("creditor", "account_last4", "kind", "status", "tracking_state",
                 "due_day", "notes", "origin_detail")
        and v is not None
    }
    if not allowed:
        return False
    if "kind" in allowed and allowed["kind"] not in DEBT_KINDS:
        raise ValueError(f"invalid debt kind: {allowed['kind']!r}")
    if "status" in allowed and allowed["status"] not in DEBT_STATUSES:
        raise ValueError(f"invalid status: {allowed['status']!r}")
    if "tracking_state" in allowed and allowed["tracking_state"] not in DEBT_TRACKING_STATES:
        raise ValueError(f"invalid tracking_state: {allowed['tracking_state']!r}")
    # Renaming the creditor has to move the dedup key with it, or the row stops matching
    # its own future statements.
    if "creditor" in allowed:
        allowed["creditor_key"] = normalize_creditor(allowed["creditor"])
    if "account_last4" in allowed:
        allowed["account_last4"] = sanitize_account_last4(allowed["account_last4"])

    sets = ", ".join(f"{k} = ?" for k in allowed)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE debts SET {sets}, updated_at = ? WHERE id = ? AND owner_user_id = ?",
            [*allowed.values(), _now(), debt_id, owner_user_id],
        )
        conn.commit()
        return cur.rowcount > 0


def merge_debts(db_path: str, owner_user_id: int, keep_id: int, merge_ids: list[int],
                note: str | None = None) -> dict:
    """Records that several rows are one obligation: keep one, dismiss the rest.

    The mail sweep files a row per creditor NAME, so a card sold to a collector and
    serviced by an agency becomes three rows for one debt -- and totalling them invents
    money the owner does not owe. This is how he says so.

    Dismissed rather than deleted, and the observations stay attached to their own rows.
    Those rows are the evidence: a statement email really did arrive from Unifin, and
    throwing that away would make the merge unreviewable and lose the provenance the whole
    debt tracker is built on. `list_debts` already excludes anything not 'tracked', so a
    dismissed row leaves every total immediately without leaving the record.

    Refuses to dismiss the row being kept -- that would silently erase the debt entirely,
    which is the one outcome worse than double-counting it.
    """
    targets = [i for i in merge_ids if i != keep_id]
    if not targets:
        return {"kept": keep_id, "merged": [], "note": "nothing to merge"}

    with closing(_connect(db_path)) as conn:
        kept = conn.execute(
            "SELECT creditor, account_last4 FROM debts WHERE id = ? AND owner_user_id = ?",
            (keep_id, owner_user_id)).fetchone()
        if kept is None:
            raise ValueError(f"no debt {keep_id} to merge into")
        label = f"{kept['creditor']}" + (f" (...{kept['account_last4']})" if kept["account_last4"] else "")
        reason = (note or "").strip() or f"Same obligation as {label}"
        placeholders = ",".join("?" for _ in targets)
        cur = conn.execute(
            f"""UPDATE debts SET tracking_state = 'dismissed', updated_at = ?,
                   notes = COALESCE(notes || ' | ', '') || ?
                WHERE id IN ({placeholders}) AND owner_user_id = ? AND tracking_state != 'dismissed'""",
            [_now(), f"Merged into debt #{keep_id}: {reason}", *targets, owner_user_id])
        conn.commit()
        merged = cur.rowcount
    return {"kept": keep_id, "kept_label": label, "merged": merged, "merged_ids": targets}


def set_debt_priority(db_path: str, owner_user_id: int, debt_id: int, priority: int | None) -> bool:
    """The owner's payoff rank for one debt (1 = pay this first). None clears it.

    The only writer of debts.priority in the whole codebase, and it is only ever reached
    from something he explicitly did -- a chat tool call or the Debts UI. suggest_payoff_
    orders() offers orderings; it does not and must not apply them. He was explicit that
    assigning payoff priorities is a joint, ongoing effort, so a suggestion that quietly
    wrote itself in as a decision would be the system overstepping exactly where he asked
    it not to.
    """
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE debts SET priority = ?, updated_at = ? WHERE id = ? AND owner_user_id = ?",
            (priority, _now(), debt_id, owner_user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def add_debt_observation(
    db_path: str, debt_id: int, observed_on: str | None = None,
    balance_text: str = "", balance: float | None = None,
    apr_text: str = "", apr: float | None = None,
    minimum_payment_text: str = "", minimum_payment: float | None = None,
    due_date: str | None = None, due_date_text: str = "",
    source: str = "chat", source_ref: str = "", source_detail: str = "",
    confidence: str = "", confirmed: bool = False, notes: str | None = None,
) -> int | None:
    """Records what a debt looked like on `observed_on`. Returns the new row's id, or None
    when an observation for this exact source message already exists (the partial unique
    index), which is what makes a re-run of the mail sweep a no-op rather than a duplicate.
    """
    if source not in DEBT_SOURCES:
        raise ValueError(f"invalid source: {source!r}")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO debt_observations
                   (debt_id, observed_on, balance_text, balance, apr_text, apr,
                    minimum_payment_text, minimum_payment, due_date, due_date_text,
                    source, source_ref, source_detail, confidence, confirmed, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (debt_id, observed_on or _today(), balance_text, balance, apr_text, apr,
             minimum_payment_text, minimum_payment, due_date, due_date_text,
             source, source_ref, source_detail, confidence, int(confirmed), notes, _now()),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount else None


def list_debt_observations(db_path: str, debt_id: int):
    """Oldest-first, so it plots directly as a trend (same ordering rule as
    list_credit_score_entries)."""
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(
            "SELECT * FROM debt_observations WHERE debt_id = ? ORDER BY observed_on, id", (debt_id,)
        ))


def has_equivalent_observation(db_path: str, debt_id: int, observed_on: str, balance_text: str) -> bool:
    """Whether this debt already has an observation stating the same balance on the same
    date. The source-ref unique index can't catch this case: the SAME statement filed in
    both INBOX and an archive folder is two different folder:uid refs, and a historical
    sweep that searches every folder will meet both. Without this, one statement becomes
    two identical points on the balance trend.
    """
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM debt_observations WHERE debt_id = ? AND observed_on = ? AND balance_text = ?",
            (debt_id, observed_on, balance_text),
        ).fetchone()
        return row is not None


def _resolve_current(observations: list[dict], text_field: str, value_field: str) -> dict:
    """The newest observation that actually stated this fact, resolved as a number when
    one was ever readable.

    Two passes on purpose. The newest observation carrying a real typed value wins, so a
    later statement that only said "see attached" doesn't blank out a perfectly good APR.
    Falling back to the newest observation with any TEXT at all keeps a debt whose balance
    has only ever been written as a range ("$80-$120") visible as that range rather than
    as nothing, with the numeric field honestly NULL.
    """
    ordered = sorted(observations, key=lambda o: (o.get("observed_on") or "", o.get("id") or 0), reverse=True)
    for obs in ordered:
        if obs.get(value_field) is not None:
            return {"value": obs[value_field], "text": obs.get(text_field) or "",
                    "observed_on": obs.get("observed_on"), "source": obs.get("source"),
                    "confirmed": bool(obs.get("confirmed"))}
    for obs in ordered:
        if (obs.get(text_field) or "").strip():
            return {"value": None, "text": obs[text_field], "observed_on": obs.get("observed_on"),
                    "source": obs.get("source"), "confirmed": bool(obs.get("confirmed"))}
    return {"value": None, "text": "", "observed_on": None, "source": None, "confirmed": False}


def attach_current_values(debt: dict, observations: list[dict]) -> dict:
    """The current view of a debt, derived from its observations rather than stored.

    Pure, and the reason it is pure is that "what is the balance now" is a question with a
    real answer only in terms of when it was last observed and who said so -- so every
    current_* value ships with its date, its source and whether he confirmed it. A UI that
    can show "$4,200 as of 2026-03-02, from a statement email" versus "$4,200, he told me"
    is the entire provenance requirement, and it cannot do that from a single number.
    """
    balance = _resolve_current(observations, "balance_text", "balance")
    apr = _resolve_current(observations, "apr_text", "apr")
    minimum = _resolve_current(observations, "minimum_payment_text", "minimum_payment")
    numeric = [o for o in observations if o.get("balance") is not None]
    numeric.sort(key=lambda o: (o.get("observed_on") or "", o.get("id") or 0))

    out = dict(debt)
    out.update({
        "current_balance": balance["value"], "current_balance_text": balance["text"],
        "current_balance_observed_on": balance["observed_on"],
        "current_balance_source": balance["source"], "current_balance_confirmed": balance["confirmed"],
        "current_apr": apr["value"], "current_apr_text": apr["text"],
        "current_apr_observed_on": apr["observed_on"], "current_apr_source": apr["source"],
        "current_minimum_payment": minimum["value"],
        "current_minimum_payment_text": minimum["text"],
        "current_minimum_payment_observed_on": minimum["observed_on"],
        "current_minimum_payment_source": minimum["source"],
        "observation_count": len(observations),
        "first_observed_on": observations[0]["observed_on"] if observations else None,
        "last_observed_on": max((o["observed_on"] for o in observations), default=None),
        # Only points with a real number -- a trend line drawn through "see attached" is a
        # lie about a balance.
        "balance_trend": [
            {"observed_on": o["observed_on"], "balance": o["balance"],
             "source": o["source"], "confirmed": bool(o["confirmed"])}
            for o in numeric
        ],
        # Monthly interest at the last known rate on the last known balance. NULL unless
        # BOTH are real numbers -- this is the "which debt is costing the most" figure and
        # a half-guessed one would rank his payoff order on fiction.
        "estimated_monthly_interest": (
            round(balance["value"] * apr["value"] / 100.0 / 12.0, 2)
            if balance["value"] is not None and apr["value"] is not None else None
        ),
    })
    return out


def list_debts(
    db_path: str, owner_user_id: int, tracking_state: str | None = "tracked",
    status: str | None = None,
):
    """Debts with their current values attached, one query for the observations rather
    than one per debt.

    tracking_state defaults to 'tracked' on purpose and every caller that totals or ranks
    debt should leave it there: 'proposed' rows are a classifier's guesses about his money
    and are not part of his debt picture until he says so. Pass None only to show him
    everything (the review/confirmation surfaces).
    """
    query = "SELECT * FROM debts WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if tracking_state:
        query += " AND tracking_state = ?"
        params.append(tracking_state)
    if status:
        query += " AND status = ?"
        params.append(status)
    # Debts he has ranked come first in his order; the rest fall back to biggest-first,
    # which is the order he'd naturally scan for "what is the damage".
    query += " ORDER BY priority IS NULL, priority, creditor"
    with closing(_connect(db_path)) as conn:
        debts = _rows(conn.execute(query, params))
        if not debts:
            return []
        placeholders = ",".join("?" for _ in debts)
        observations = _rows(conn.execute(
            f"SELECT * FROM debt_observations WHERE debt_id IN ({placeholders}) ORDER BY observed_on, id",
            [d["id"] for d in debts],
        ))
    by_debt: dict[int, list[dict]] = {}
    for obs in observations:
        by_debt.setdefault(obs["debt_id"], []).append(obs)
    return [attach_current_values(d, by_debt.get(d["id"], [])) for d in debts]


def suggest_payoff_orders(debts: list[dict]) -> dict:
    """Two legitimate payoff strategies over the same debts, as SUGGESTIONS.

    Avalanche (highest rate first) costs the least money; snowball (smallest balance
    first) clears accounts soonest and is the one that actually keeps people going. Both
    are real strategies and which one is right is a judgement about him, not about the
    numbers -- so this returns both, ranked, and assigns nothing. Writing either of these
    into debts.priority would be the system deciding something he said explicitly was a
    joint call.

    Debts missing the number a strategy ranks by are listed separately rather than sorted
    as zero, which would park an unknown balance at the top of the snowball order.
    """
    active = [d for d in debts if d.get("status") == "active"]

    def _rank(key, reverse):
        ranked = [d for d in active if d.get(key) is not None]
        ranked.sort(key=lambda d: d[key], reverse=reverse)
        return [{"debt_id": d["id"], "creditor": d["creditor"], key: d[key]} for d in ranked]

    return {
        "avalanche": {
            "label": "Highest interest rate first",
            "why": "Costs the least in total interest.",
            "order": _rank("current_apr", True),
            "unranked": [{"debt_id": d["id"], "creditor": d["creditor"]}
                         for d in active if d.get("current_apr") is None],
        },
        "snowball": {
            "label": "Smallest balance first",
            "why": "Clears whole accounts soonest, which is easier to keep going.",
            "order": _rank("current_balance", False),
            "unranked": [{"debt_id": d["id"], "creditor": d["creditor"]}
                         for d in active if d.get("current_balance") is None],
        },
    }


def debt_summary(db_path: str, owner_user_id: int) -> dict:
    """Totals across his tracked debts, plus both suggested payoff orderings.

    total_balance is explicitly a FLOOR, not a truth, and unknown_balance_count is what
    says so: a debt whose balance has never been stated as a readable number contributes
    nothing to the total. Reporting a total as if it were complete, when the whole feature
    exists because he does not know what he owes, would be the one genuinely harmful thing
    this could tell him.
    """
    debts = list_debts(db_path, owner_user_id, tracking_state="tracked")
    active = [d for d in debts if d["status"] == "active"]
    balances = [d["current_balance"] for d in active if d["current_balance"] is not None]
    minimums = [d["current_minimum_payment"] for d in active if d["current_minimum_payment"] is not None]
    interest = [d["estimated_monthly_interest"] for d in active if d["estimated_monthly_interest"] is not None]
    costliest = max(
        (d for d in active if d["estimated_monthly_interest"] is not None),
        key=lambda d: d["estimated_monthly_interest"], default=None,
    )
    return {
        "debt_count": len(active),
        "total_balance": round(sum(balances), 2) if balances else 0.0,
        "known_balance_count": len(balances),
        "unknown_balance_count": len(active) - len(balances),
        "total_minimum_payment": round(sum(minimums), 2) if minimums else 0.0,
        "estimated_monthly_interest": round(sum(interest), 2) if interest else 0.0,
        "costliest_debt": (
            {"debt_id": costliest["id"], "creditor": costliest["creditor"],
             "estimated_monthly_interest": costliest["estimated_monthly_interest"],
             "current_apr": costliest["current_apr"]}
            if costliest else None
        ),
        "unprioritized_count": sum(1 for d in active if d["priority"] is None),
        "proposed_count": len(list_debts(db_path, owner_user_id, tracking_state="proposed")),
        "suggested_payoff_orders": suggest_payoff_orders(debts),
    }
