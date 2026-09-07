"""Storage for the mail triage feature (junk-flagging, draft-reply, auto-filing) -- an
audit trail of what Jarvis decided about each email and what it did about it.

A sibling of personal_db.py/business_db.py, kept separate from db.py for the same
reason: mail triage is an optional feature (only relevant when iCloud Mail is
configured) with its own small, bounded schema, not columns bolted onto a core table.
Same discipline as those modules: short-lived connections, every read/write behind a
function, owner id passed explicitly.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_triage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    folder TEXT NOT NULL,
    uid TEXT NOT NULL,
    sender TEXT,
    subject TEXT,
    is_junk INTEGER NOT NULL DEFAULT 0,
    junk_score REAL,
    junk_reasons TEXT,
    category TEXT,
    category_confidence REAL,
    category_reasons TEXT,
    suggested_folder TEXT,
    filed_folder TEXT,
    drafted_reply TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_user_id, folder, uid)
);
"""


def init_mail_triage_db(db_path: str) -> None:
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


def _row(r):
    if r is None:
        return None
    d = dict(r)
    if d.get("junk_reasons"):
        d["junk_reasons"] = json.loads(d["junk_reasons"])
    if d.get("category_reasons"):
        d["category_reasons"] = json.loads(d["category_reasons"])
    return d


def upsert_triage(
    db_path: str, owner_user_id: int, folder: str, uid: str, *,
    sender: str | None = None, subject: str | None = None,
    is_junk: bool = False, junk_score: float | None = None, junk_reasons: list[str] | None = None,
    category: str | None = None, category_confidence: float | None = None,
    category_reasons: list[str] | None = None, suggested_folder: str | None = None,
    filed_folder: str | None = None, drafted_reply: str | None = None,
) -> int:
    """Insert or refresh the triage record for one (owner, folder, uid). Fields not
    supplied on an update (filed_folder, drafted_reply) keep their previous value rather
    than being wiped -- e.g. re-running triage_inbox must not erase that a message was
    already filed or drafted for."""
    now = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO mail_triage_log
                   (owner_user_id, folder, uid, sender, subject, is_junk, junk_score, junk_reasons,
                    category, category_confidence, category_reasons, suggested_folder, filed_folder,
                    drafted_reply, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_user_id, folder, uid) DO UPDATE SET
                   sender = excluded.sender, subject = excluded.subject,
                   is_junk = excluded.is_junk, junk_score = excluded.junk_score,
                   junk_reasons = excluded.junk_reasons, category = excluded.category,
                   category_confidence = excluded.category_confidence,
                   category_reasons = excluded.category_reasons,
                   suggested_folder = excluded.suggested_folder,
                   filed_folder = COALESCE(excluded.filed_folder, mail_triage_log.filed_folder),
                   drafted_reply = COALESCE(excluded.drafted_reply, mail_triage_log.drafted_reply),
                   updated_at = excluded.updated_at""",
            (owner_user_id, folder, uid, sender, subject, 1 if is_junk else 0, junk_score,
             json.dumps(junk_reasons or []), category, category_confidence,
             json.dumps(category_reasons or []), suggested_folder, filed_folder, drafted_reply,
             now, now),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM mail_triage_log WHERE owner_user_id = ? AND folder = ? AND uid = ?",
            (owner_user_id, folder, uid),
        ).fetchone()[0]


def get_triage(db_path: str, owner_user_id: int, folder: str, uid: str):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM mail_triage_log WHERE owner_user_id = ? AND folder = ? AND uid = ?",
            (owner_user_id, folder, uid),
        ).fetchone()
    return _row(row)


def mark_filed(db_path: str, owner_user_id: int, folder: str, uid: str, filed_folder: str) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """UPDATE mail_triage_log SET filed_folder = ?, updated_at = ?
               WHERE owner_user_id = ? AND folder = ? AND uid = ?""",
            (filed_folder, _now(), owner_user_id, folder, uid),
        )
        conn.commit()


def list_triage(
    db_path: str, owner_user_id: int, limit: int = 20,
    only_junk: bool | None = None, category: str | None = None,
):
    query = "SELECT * FROM mail_triage_log WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if only_junk is not None:
        query += " AND is_junk = ?"
        params.append(1 if only_junk else 0)
    if category:
        query += " AND category = ?"
        params.append(category)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return [_row(r) for r in conn.execute(query, params)]
