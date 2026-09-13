"""Storage for LLM-drafted email replies awaiting the owner's review.

A sibling of personal_db.py/business_db.py: mail triage has nothing to do with either
bounded context, so it gets its own table rather than a scope column bolted onto an
existing one. Same discipline as those: short-lived WAL connections, every read/write
behind a function, owner id passed explicitly.

Nothing here ever sends anything -- a row's status only ever reflects what the OWNER
has done with it in the review surface (edited, dismissed) or the app noting it was
reconciled after a real send. The only place a message actually leaves the account is
mail_client.py's send(), which stays behind the existing pending_actions confirmation
gate untouched by any of this.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS email_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    folder TEXT NOT NULL DEFAULT 'INBOX',
    uid TEXT NOT NULL,
    from_address TEXT,
    subject TEXT,
    received_at TEXT,
    category TEXT,
    reasoning TEXT,
    draft_subject TEXT NOT NULL,
    draft_body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'drafted' CHECK (status IN ('drafted', 'edited', 'dismissed', 'sent')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_user_id, folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_email_drafts_status ON email_drafts(owner_user_id, status, created_at);

-- Audit trail for the autonomous junk-scan (scheduler.run_mail_junk_scan / mail_client
-- .scan_inbox_for_junk). That pass has always run unattended -- no confirmation, by
-- design, since moving into Junk is reversible -- but until now the only record of what
-- it did was a single logger.info line per scan. One row per message it flagged (whether
-- or not the move itself succeeded), so the owner has something real to look at instead
-- of trusting the scoring blindly. Purely informational: nothing reads this back to
-- decide anything, and nothing here can undo or redo a move.
CREATE TABLE IF NOT EXISTS mail_junk_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL,
    folder TEXT NOT NULL DEFAULT 'INBOX',
    from_address TEXT,
    subject TEXT,
    score REAL NOT NULL,
    reasons TEXT,
    moved INTEGER NOT NULL DEFAULT 0,
    moved_to TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mail_junk_log_created ON mail_junk_log(created_at);

-- Bills detected in email content (mail_bills.py) -- project 13's "detects bills and
-- records amount/due date/recurring status". A completely separate signal from Era's
-- recurring-charge detection (db.list_era_recurring_charges / finance.py), which only
-- ever sees bank transactions and therefore misses anything that arrives solely as an
-- emailed bill until after the money has already moved.
--
-- Every model-reasoned value is stored BOTH as the model gave it and, only when it
-- parses unambiguously, as a real typed value: amount_text/amount and
-- due_date_text/due_date. Same discipline as meal_plan_shopping_items' TEXT quantities --
-- "$80-$120" or "due on receipt" is real information, and coercing it into a float or an
-- ISO date would be false precision this table has no business pretending to. A NULL
-- amount/due_date means "the model did not give one number/date", never "zero"/"today".
--
-- reminder_id links to the reminders row created for a plausible future due date, and is
-- the idempotency guard that stops a re-scan double-nudging (see mail_bills.py). This
-- table is written by an autonomous classifier, so it holds no authority over anything:
-- nothing reads it back to spend money, pay anything, or touch manual_recurring_charges,
-- which the owner's budget projections key off and which stays owner-entered only.
CREATE TABLE IF NOT EXISTS email_bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    folder TEXT NOT NULL DEFAULT 'INBOX',
    uid TEXT NOT NULL,
    from_address TEXT,
    subject TEXT,
    received_at TEXT,
    payee TEXT,
    amount_text TEXT,          -- exactly as the model read it out of the message
    amount REAL,               -- only when amount_text is exactly one unambiguous number
    due_date TEXT,             -- ISO date, only when the model gave a real parseable one
    due_date_text TEXT,        -- how the message itself phrased it ("due on receipt")
    is_recurring INTEGER NOT NULL DEFAULT 0,
    cadence TEXT,              -- the model's guess ('monthly'), free text, '' if none
    confidence TEXT,           -- as given ('high'/'medium'/'low'), never re-scored here
    reasoning TEXT,
    reminder_id INTEGER,       -- reminders.id created for due_date, NULL if none was
    status TEXT NOT NULL DEFAULT 'detected'
        CHECK (status IN ('detected', 'confirmed', 'dismissed', 'paid')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_user_id, folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_email_bills_status ON email_bills(owner_user_id, status, created_at);

-- The bill scan's "already looked at this one" ledger, negatives included. email_bills
-- alone can't carry that: a message that ISN'T a bill leaves no row there, so a scan
-- keyed only on email_bills would re-ask the LLM about every newsletter in the inbox on
-- every single tick (a real, known cost in mail_triage.py, which only records its hits).
-- Deliberately not merged into email_bills as a status: a "bills" table listing
-- newsletters would make every read of it filter for what it's actually about.
CREATE TABLE IF NOT EXISTS email_bill_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    folder TEXT NOT NULL DEFAULT 'INBOX',
    uid TEXT NOT NULL,
    is_bill INTEGER NOT NULL DEFAULT 0,
    scanned_at TEXT NOT NULL,
    UNIQUE(owner_user_id, folder, uid)
);
"""


def init_mail_db(db_path: str) -> None:
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


def has_draft(db_path: str, owner_user_id: int, folder: str, uid: str) -> bool:
    """Whether this message already has a draft, so a repeat scan doesn't ask the LLM
    (or the owner) to look at the same email twice."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM email_drafts WHERE owner_user_id = ? AND folder = ? AND uid = ?",
            (owner_user_id, folder, uid),
        ).fetchone()
        return row is not None


def create_draft(
    db_path: str, owner_user_id: int, folder: str, uid: str, from_address: str, subject: str,
    received_at: str, category: str, reasoning: str, draft_subject: str, draft_body: str,
) -> int:
    """Returns the draft's id -- the existing one if this uid already has a draft
    (never overwritten by a later scan, including one the owner has since edited),
    otherwise the newly created row."""
    now = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO email_drafts
                   (owner_user_id, folder, uid, from_address, subject, received_at, category,
                    reasoning, draft_subject, draft_body, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'drafted', ?, ?)
               ON CONFLICT(owner_user_id, folder, uid) DO NOTHING""",
            (owner_user_id, folder, uid, from_address, subject, received_at, category,
             reasoning, draft_subject, draft_body, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM email_drafts WHERE owner_user_id = ? AND folder = ? AND uid = ?",
            (owner_user_id, folder, uid),
        ).fetchone()
        return row["id"]


def list_drafts(db_path: str, owner_user_id: int, status: str | None = None, limit: int = 50):
    query = "SELECT * FROM email_drafts WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def get_draft(db_path: str, owner_user_id: int, draft_id: int):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM email_drafts WHERE id = ? AND owner_user_id = ?", (draft_id, owner_user_id)
        ).fetchone()
        return dict(row) if row else None


def update_draft(
    db_path: str, owner_user_id: int, draft_id: int,
    draft_subject: str | None = None, draft_body: str | None = None, status: str | None = None,
) -> bool:
    """Lets the owner rewrite a draft before anything is sent, or dismiss/mark it handled.

    Editing the subject or body of a still-fresh draft moves it to 'edited' automatically
    unless the caller explicitly sets a status -- so the review surface can tell an
    untouched LLM suggestion from one the owner has already worked on, the same
    distinction market_leads/trend_leads statuses exist to preserve elsewhere in this
    codebase.
    """
    if status is not None and status not in ("drafted", "edited", "dismissed", "sent"):
        raise ValueError(f"invalid status: {status!r}")
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status FROM email_drafts WHERE id = ? AND owner_user_id = ?", (draft_id, owner_user_id)
        ).fetchone()
        if row is None:
            return False
        fields, params = [], []
        if draft_subject is not None:
            fields.append("draft_subject = ?")
            params.append(draft_subject)
        if draft_body is not None:
            fields.append("draft_body = ?")
            params.append(draft_body)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        elif (draft_subject is not None or draft_body is not None) and row["status"] == "drafted":
            fields.append("status = ?")
            params.append("edited")
        if not fields:
            return False
        fields.append("updated_at = ?")
        params.append(_now())
        params.extend([draft_id, owner_user_id])
        conn.execute(
            f"UPDATE email_drafts SET {', '.join(fields)} WHERE id = ? AND owner_user_id = ?", params
        )
        conn.commit()
        return True


def count_pending_drafts(db_path: str, owner_user_id: int) -> int:
    with closing(_connect(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM email_drafts WHERE owner_user_id = ? AND status IN ('drafted', 'edited')",
            (owner_user_id,),
        ).fetchone()["n"]


# --- junk-scan audit log (see mail_junk_log's schema comment above) ---------------

def log_junk_action(
    db_path: str, uid: str, folder: str, from_address: str, subject: str,
    score: float, reasons: list[str], moved: bool, moved_to: str | None = None,
) -> int:
    """Records one message scan_inbox_for_junk decided was junk this pass, regardless of
    whether the move into Junk actually succeeded (moved=False + no moved_to means it was
    flagged but the move itself failed, still worth surfacing). Called from
    scheduler.record_junk_scan_results, once per flagged result -- never for messages that
    scored under threshold, so this table doesn't fill up with every message ever scanned."""
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO mail_junk_log
                   (uid, folder, from_address, subject, score, reasons, moved, moved_to, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, folder, from_address, subject, score, json.dumps(reasons or []), int(moved), moved_to, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_junk_log(db_path: str, limit: int = 50) -> list[dict]:
    """Most recent auto-junked messages first, for the Email page's audit view."""
    with closing(_connect(db_path)) as conn:
        rows = _rows(conn.execute(
            "SELECT * FROM mail_junk_log ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ))
    for row in rows:
        row["reasons"] = json.loads(row["reasons"]) if row.get("reasons") else []
        row["moved"] = bool(row["moved"])
    return rows


def is_logged_junk(db_path: str, folder: str, uid: str) -> bool:
    """Whether the junk scan already flagged this message. Read by the bill scan so a
    message that was scored as junk but whose move into Junk failed (moved=0) can't come
    back around as a bill review card -- a scam invoice is exactly the kind of mail that
    both scores as junk AND reads like a bill."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM mail_junk_log WHERE folder = ? AND uid = ?", (folder, uid)
        ).fetchone()
        return row is not None


# --- detected bills (see email_bills' schema comment above, and mail_bills.py) --------

BILL_STATUSES = ("detected", "confirmed", "dismissed", "paid")


def has_scanned_for_bill(db_path: str, owner_user_id: int, folder: str, uid: str) -> bool:
    """Whether the bill scan has already made a decision about this message -- bill or
    not. The negatives are the point: see email_bill_scans' schema comment."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM email_bill_scans WHERE owner_user_id = ? AND folder = ? AND uid = ?",
            (owner_user_id, folder, uid),
        ).fetchone()
        return row is not None


def mark_bill_scanned(db_path: str, owner_user_id: int, folder: str, uid: str, is_bill: bool) -> None:
    """Records that this uid has been judged. Only ever called after a real answer from
    the model -- a message that couldn't be read, or that blew up mid-classification, is
    deliberately left unmarked so the next pass retries it instead of losing it."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO email_bill_scans (owner_user_id, folder, uid, is_bill, scanned_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(owner_user_id, folder, uid) DO NOTHING""",
            (owner_user_id, folder, uid, int(is_bill), _now()),
        )
        conn.commit()


def create_bill(
    db_path: str, owner_user_id: int, folder: str, uid: str, from_address: str, subject: str,
    received_at: str, payee: str, amount_text: str, amount: float | None,
    due_date: str | None, due_date_text: str, is_recurring: bool, cadence: str,
    confidence: str, reasoning: str,
) -> int:
    """Returns the bill's id -- the existing one if this uid already produced a bill (a
    later scan never overwrites a row the owner may already have confirmed or dismissed),
    otherwise the newly created row. Same ON CONFLICT DO NOTHING shape as create_draft."""
    now = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO email_bills
                   (owner_user_id, folder, uid, from_address, subject, received_at, payee,
                    amount_text, amount, due_date, due_date_text, is_recurring, cadence,
                    confidence, reasoning, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'detected', ?, ?)
               ON CONFLICT(owner_user_id, folder, uid) DO NOTHING""",
            (owner_user_id, folder, uid, from_address, subject, received_at, payee,
             amount_text, amount, due_date, due_date_text, int(is_recurring), cadence,
             confidence, reasoning, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM email_bills WHERE owner_user_id = ? AND folder = ? AND uid = ?",
            (owner_user_id, folder, uid),
        ).fetchone()
        return row["id"]


def _bill_row(row: dict) -> dict:
    row["is_recurring"] = bool(row["is_recurring"])
    return row


def list_bills(db_path: str, owner_user_id: int, status: str | None = None, limit: int = 50):
    query = "SELECT * FROM email_bills WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    # Soonest-due first, with undated bills after the dated ones rather than sorting as
    # empty strings at the top: a due date is the whole reason to look at this list.
    query += " ORDER BY due_date IS NULL, due_date, created_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return [_bill_row(r) for r in _rows(conn.execute(query, params))]


def get_bill(db_path: str, owner_user_id: int, bill_id: int):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM email_bills WHERE id = ? AND owner_user_id = ?", (bill_id, owner_user_id)
        ).fetchone()
        return _bill_row(dict(row)) if row else None


def set_bill_reminder(db_path: str, bill_id: int, reminder_id: int) -> None:
    """Links the reminder created for this bill's due date back onto the bill, which is
    what makes re-running the scan safe: a row that already has a reminder_id never gets
    a second one (same precedent as meal_plan_entries.freezer_pull_reminder_id)."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE email_bills SET reminder_id = ?, updated_at = ? WHERE id = ?",
            (reminder_id, _now(), bill_id),
        )
        conn.commit()


def update_bill_status(db_path: str, owner_user_id: int, bill_id: int, status: str) -> bool:
    """The owner's verdict on a detected bill, set from the Review page's decision
    write-through. Nothing about a status here pays, schedules, or cancels anything --
    it only records what he said about a row this classifier produced."""
    if status not in BILL_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE email_bills SET status = ?, updated_at = ? WHERE id = ? AND owner_user_id = ?",
            (status, _now(), bill_id, owner_user_id),
        )
        conn.commit()
        return cur.rowcount > 0
