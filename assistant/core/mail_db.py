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
