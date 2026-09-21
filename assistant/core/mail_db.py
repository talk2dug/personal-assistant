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
from datetime import datetime, timedelta, timezone

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

-- Provisional importance flags (mail_importance.py) -- project 13's "flags important
-- emails touching personal finances, relationships, or personal business".
--
-- These three tables exist to serve a FEEDBACK LOOP, not a classifier. The owner was
-- asked where he'd draw the line on "important" and deliberately refused to name one:
-- "if thres a way to flag something as important and let the AI start to learn. Or if it
-- can tempo flag something as important and then i can say if it was or was not and
-- continue learning". So a flag here is a QUESTION ("I think this mattered -- did it?"),
-- and his answer is the actual product: it becomes a labelled example that steers the
-- next run's prompt. Nothing in this feature reads these tables to act on mail.
--
-- confidence is the model's own 0.0-1.0 self-rating, stored exactly as it gave it and
-- never re-scored here (same discipline as email_bills.confidence). It is NULL when the
-- model returned something that wasn't a plain 0-1 number, and a NULL confidence never
-- clears the threshold -- unreadable certainty fails closed, i.e. no flag.
CREATE TABLE IF NOT EXISTS email_importance_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    folder TEXT NOT NULL DEFAULT 'INBOX',
    uid TEXT NOT NULL,
    from_address TEXT,
    subject TEXT,
    received_at TEXT,
    category TEXT,             -- personal_finances | relationships | personal_business | other
    confidence REAL,           -- the model's own 0.0-1.0, as given
    reason TEXT,               -- one sentence, read back to him verbatim on the review card
    status TEXT NOT NULL DEFAULT 'flagged'
        CHECK (status IN ('flagged', 'confirmed', 'rejected')),
    verdict_note TEXT,         -- whatever he typed on the Review decision, real signal
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_user_id, folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_email_importance_flags_status
    ON email_importance_flags(owner_user_id, status, created_at);

-- The importance scan's "already judged this one" ledger, negatives included -- same
-- reasoning as email_bill_scans above (one LLM round trip per message, ever). It carries
-- category/confidence as well as the yes/no, so a near-miss that scored just under the
-- threshold is still visible when the threshold needs tuning: without that, the only
-- evidence for "the bar is too high" would be mail nobody ever hears about again.
CREATE TABLE IF NOT EXISTS email_importance_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    folder TEXT NOT NULL DEFAULT 'INBOX',
    uid TEXT NOT NULL,
    flagged INTEGER NOT NULL DEFAULT 0,
    category TEXT,
    confidence REAL,
    scanned_at TEXT NOT NULL,
    UNIQUE(owner_user_id, folder, uid)
);

-- The labelled training set: one row per message the OWNER has actually ruled on. This
-- is the only thing in the feature that is ground truth rather than a model opinion, and
-- it is what mail_importance.select_examples feeds back into the next run's prompt.
--
-- Deliberately its own table rather than a column on email_importance_flags, for one
-- concrete reason: the most valuable label is the one no flag exists for. "That one WAS
-- important" about a message the scan quietly passed over is a false negative -- exactly
-- the case a flags-only store can never represent, and exactly what the chat tool
-- (mark_email_importance) records. flag_id is therefore nullable.
--
-- UNIQUE(owner,folder,uid) with an upsert: a later verdict about the same message
-- replaces the earlier one. He is allowed to change his mind, and the training set must
-- hold what he believes now, not an average of every answer he's ever given.
CREATE TABLE IF NOT EXISTS email_importance_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    folder TEXT NOT NULL DEFAULT 'INBOX',
    uid TEXT NOT NULL,
    from_address TEXT,
    subject TEXT,
    category TEXT,             -- the category the model claimed, '' when he volunteered the label
    model_reason TEXT,         -- why the model thought so, '' for a chat-volunteered label
    label INTEGER NOT NULL,    -- 1 = he said it WAS important, 0 = he said it was NOT
    note TEXT,                 -- his own words on the decision: the highest-signal field here
    source TEXT NOT NULL DEFAULT 'review' CHECK (source IN ('review', 'chat')),
    flag_id INTEGER,           -- the flag he was answering, NULL when he volunteered it
    created_at TEXT NOT NULL,
    UNIQUE(owner_user_id, folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_email_importance_examples_label
    ON email_importance_examples(owner_user_id, label, created_at);

-- The historical debt sweep's "already judged this one" ledger (mail_debts.py). Same
-- reasoning as email_bill_scans -- one LLM round trip per message, ever, negatives
-- included -- but here it is doing considerably more work than saving money.
--
-- mail_bills.py only ever looks at the most recent messages, and the owner's debt is not
-- in his recent messages, it is in his history: "most of the debt is in there and i dont
-- have it written down". So the sweep runs BACKWARDS through a mailbox with tens of
-- thousands of messages, across every folder, and cannot finish in one pass. This table
-- is what makes that bounded and resumable: each run shortlists candidates by IMAP
-- search, skips everything already in here, classifies up to a per-run cap, and stops.
-- Run it again and it picks up where it left off. Interrupt it mid-run and the messages
-- it already judged stay judged.
--
-- folder is part of the key because the same UID means different messages in different
-- folders. That also means the SAME statement filed in both INBOX and an archive folder
-- is legitimately judged twice -- deduplicating THAT is a separate job, done on the debt
-- side by personal_db.has_equivalent_observation, not here.
CREATE TABLE IF NOT EXISTS email_debt_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    folder TEXT NOT NULL DEFAULT 'INBOX',
    uid TEXT NOT NULL,
    is_debt INTEGER NOT NULL DEFAULT 0,
    debt_id INTEGER,           -- the debts row this message became evidence for, if any
    scanned_at TEXT NOT NULL,
    UNIQUE(owner_user_id, folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_email_debt_scans_debt ON email_debt_scans(debt_id);
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


def find_open_duplicate(db_path: str, owner_user_id: int, payee: str | None,
                        amount: float | None, within_days: int = 35) -> dict | None:
    """An open bill this email is just another copy of, or None.

    create_bill is keyed on the EMAIL uid, which is right for "one row per message" and
    wrong for "one row per bill". Shopify sent the same "a bill payment failed" notice
    every two days, so one $39 charge became four bills, four reminders and $156 on the
    calendar. A dunning notice is not a new debt.

    Matching is deliberately narrow -- same payee, same amount, still open, and recent.
    The window matters as much as the match: next month's genuine Shopify invoice IS a new
    bill, and suppressing it would be a worse failure than the duplicate. A bill with no
    amount is never matched this way, because "payee with no amount" is too broad to be
    evidence of anything.
    """
    if not payee or amount is None:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT * FROM email_bills
                WHERE owner_user_id = ?
                  AND LOWER(TRIM(payee)) = LOWER(TRIM(?))
                  AND amount IS NOT NULL
                  AND ABS(amount - ?) < 0.005
                  AND COALESCE(status, '') NOT IN ('paid', 'dismissed', 'ignored')
                  AND created_at >= ?
                ORDER BY id LIMIT 1""",
            (owner_user_id, payee, float(amount), cutoff)).fetchone()
        return _bill_row(dict(row)) if row else None


def note_duplicate_notice(db_path: str, bill_id: int, due_date: str | None) -> None:
    """Record that a repeat notice arrived for a bill already tracked.

    The due date is allowed to move FORWARD only. A dunning notice usually restates a
    later date, and taking the newest blindly would let a bill walk its own deadline into
    the future indefinitely; taking the earliest would ignore a genuine extension.
    """
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT due_date FROM email_bills WHERE id = ?",
                           (bill_id,)).fetchone()
        current = row["due_date"] if row else None
        if due_date and (not current or due_date > current):
            conn.execute("UPDATE email_bills SET due_date = ?, updated_at = ? WHERE id = ?",
                         (due_date, _now(), bill_id))
            conn.commit()


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


# --- provisional importance flags + the owner's labelled verdicts ---------------------
# (see the three email_importance_* schema comments above, and mail_importance.py)

IMPORTANCE_STATUSES = ("flagged", "confirmed", "rejected")


def has_scanned_for_importance(db_path: str, owner_user_id: int, folder: str, uid: str) -> bool:
    """Whether the importance scan has already judged this message -- flagged or not."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM email_importance_scans WHERE owner_user_id = ? AND folder = ? AND uid = ?",
            (owner_user_id, folder, uid),
        ).fetchone()
        return row is not None


def mark_importance_scanned(
    db_path: str, owner_user_id: int, folder: str, uid: str, flagged: bool,
    category: str = "", confidence: float | None = None,
) -> None:
    """Records that this uid has been judged, and what the model thought even when that
    wasn't enough to surface anything. Only ever called after a real answer from the
    model -- a message that couldn't be read, or that blew up mid-classification, is
    deliberately left unmarked so the next pass retries it (same rule as bills)."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO email_importance_scans
                   (owner_user_id, folder, uid, flagged, category, confidence, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_user_id, folder, uid) DO NOTHING""",
            (owner_user_id, folder, uid, int(flagged), category, confidence, _now()),
        )
        conn.commit()


def create_importance_flag(
    db_path: str, owner_user_id: int, folder: str, uid: str, from_address: str, subject: str,
    received_at: str, category: str, confidence: float | None, reason: str,
) -> int:
    """Returns the flag's id -- the existing one if this uid was already flagged (a later
    scan never overwrites a row he may already have ruled on), otherwise the new row."""
    now = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO email_importance_flags
                   (owner_user_id, folder, uid, from_address, subject, received_at,
                    category, confidence, reason, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'flagged', ?, ?)
               ON CONFLICT(owner_user_id, folder, uid) DO NOTHING""",
            (owner_user_id, folder, uid, from_address, subject, received_at,
             category, confidence, reason, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM email_importance_flags WHERE owner_user_id = ? AND folder = ? AND uid = ?",
            (owner_user_id, folder, uid),
        ).fetchone()
        return row["id"]


def list_importance_flags(db_path: str, owner_user_id: int, status: str | None = None, limit: int = 50):
    query = "SELECT * FROM email_importance_flags WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def get_importance_flag(db_path: str, owner_user_id: int, flag_id: int):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM email_importance_flags WHERE id = ? AND owner_user_id = ?",
            (flag_id, owner_user_id),
        ).fetchone()
        return dict(row) if row else None


def get_importance_flag_by_uid(db_path: str, owner_user_id: int, folder: str, uid: str):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM email_importance_flags WHERE owner_user_id = ? AND folder = ? AND uid = ?",
            (owner_user_id, folder, uid),
        ).fetchone()
        return dict(row) if row else None


def record_importance_example(
    db_path: str, owner_user_id: int, folder: str, uid: str, from_address: str, subject: str,
    category: str, model_reason: str, label: bool, note: str = "", source: str = "review",
    flag_id: int | None = None,
) -> int:
    """Writes (or replaces) the owner's verdict about one message as a labelled example.

    An upsert on purpose: he is allowed to change his mind, and the training set has to
    hold what he believes NOW. Returns the example's id.
    """
    if source not in ("review", "chat"):
        raise ValueError(f"invalid source: {source!r}")
    now = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO email_importance_examples
                   (owner_user_id, folder, uid, from_address, subject, category, model_reason,
                    label, note, source, flag_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_user_id, folder, uid) DO UPDATE SET
                   label = excluded.label,
                   note = excluded.note,
                   source = excluded.source,
                   flag_id = COALESCE(excluded.flag_id, email_importance_examples.flag_id),
                   category = CASE WHEN excluded.category != '' THEN excluded.category
                                   ELSE email_importance_examples.category END,
                   model_reason = CASE WHEN excluded.model_reason != '' THEN excluded.model_reason
                                       ELSE email_importance_examples.model_reason END,
                   created_at = excluded.created_at""",
            (owner_user_id, folder, uid, from_address, subject, category, model_reason,
             int(bool(label)), note, source, flag_id, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM email_importance_examples WHERE owner_user_id = ? AND folder = ? AND uid = ?",
            (owner_user_id, folder, uid),
        ).fetchone()
        return row["id"]


def record_importance_verdict(
    db_path: str, owner_user_id: int, flag_id: int, important: bool, note: str = "",
) -> dict | None:
    """The owner's answer to one flag: stamps the flag row AND files the labelled example
    in one call, so the Review page and the chat tool can never drift into recording a
    verdict the next run's prompt won't see. Returns the updated flag, or None if there
    is no such flag.

    Nothing about a verdict here touches the message itself -- it is not archived, read,
    moved or replied to. The only thing that changes is what Jarvis has learned.
    """
    flag = get_importance_flag(db_path, owner_user_id, flag_id)
    if flag is None:
        return None
    status = "confirmed" if important else "rejected"
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """UPDATE email_importance_flags
               SET status = ?, verdict_note = ?, decided_at = ?, updated_at = ?
               WHERE id = ? AND owner_user_id = ?""",
            (status, note, _now(), _now(), flag_id, owner_user_id),
        )
        conn.commit()
    record_importance_example(
        db_path, owner_user_id, folder=flag["folder"], uid=flag["uid"],
        from_address=flag["from_address"] or "", subject=flag["subject"] or "",
        category=flag["category"] or "", model_reason=flag["reason"] or "",
        label=important, note=note or "", source="review", flag_id=flag_id,
    )
    return get_importance_flag(db_path, owner_user_id, flag_id)


def list_importance_examples(
    db_path: str, owner_user_id: int, label: bool | None = None, limit: int = 10,
) -> list[dict]:
    """Most recent verdicts first. mail_importance.select_examples owns the actual
    selection strategy (balance, backfill, cap) -- this is only the query."""
    query = "SELECT * FROM email_importance_examples WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if label is not None:
        query += " AND label = ?"
        params.append(int(bool(label)))
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def importance_stats(db_path: str, owner_user_id: int) -> dict:
    """How the flagging is actually doing, straight from his own verdicts -- what the
    Email page's Important tab shows. precision is confirmed / (confirmed + rejected),
    and is None until he has ruled on at least one flag: a 0-of-0 rendered as "0%" would
    read as "this thing is always wrong" on its first day."""
    with closing(_connect(db_path)) as conn:
        counts = dict(conn.execute(
            """SELECT
                   COUNT(*) AS flagged_total,
                   SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed,
                   SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected,
                   SUM(CASE WHEN status = 'flagged' THEN 1 ELSE 0 END) AS awaiting_verdict
               FROM email_importance_flags WHERE owner_user_id = ?""",
            (owner_user_id,),
        ).fetchone())
        scanned = conn.execute(
            "SELECT COUNT(*) AS n FROM email_importance_scans WHERE owner_user_id = ?",
            (owner_user_id,),
        ).fetchone()["n"]
        examples = dict(conn.execute(
            """SELECT
                   COUNT(*) AS total,
                   SUM(CASE WHEN label = 1 THEN 1 ELSE 0 END) AS positive,
                   SUM(CASE WHEN label = 0 THEN 1 ELSE 0 END) AS negative
               FROM email_importance_examples WHERE owner_user_id = ?""",
            (owner_user_id,),
        ).fetchone())

    confirmed = counts["confirmed"] or 0
    rejected = counts["rejected"] or 0
    decided = confirmed + rejected
    return {
        "messages_judged": scanned,
        "flagged_total": counts["flagged_total"] or 0,
        "confirmed": confirmed,
        "rejected": rejected,
        "awaiting_verdict": counts["awaiting_verdict"] or 0,
        "precision": round(confirmed / decided, 3) if decided else None,
        "examples_total": examples["total"] or 0,
        "examples_positive": examples["positive"] or 0,
        "examples_negative": examples["negative"] or 0,
    }


# --- the historical debt sweep's scan ledger (see email_debt_scans above, mail_debts.py) --

def has_scanned_for_debt(db_path: str, owner_user_id: int, folder: str, uid: str) -> bool:
    """Whether the debt sweep has already judged this message -- debt or not. This is the
    whole resumability mechanism: a run shortlists thousands of candidates and skips
    everything already in here, so repeated runs make progress through a backlog instead
    of re-doing the front of it."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM email_debt_scans WHERE owner_user_id = ? AND folder = ? AND uid = ?",
            (owner_user_id, folder, uid),
        ).fetchone()
        return row is not None


def scanned_debt_uids(db_path: str, owner_user_id: int, folder: str) -> set:
    """Every uid in `folder` this sweep has already judged, in one query.

    The per-uid check above is one round trip per candidate, and a shortlist can run to
    thousands of candidates of which nearly all are already judged -- on a resumed run
    that is thousands of queries to decide to do nothing. This is the same answer as a set.
    """
    with closing(_connect(db_path)) as conn:
        return {
            row["uid"] for row in conn.execute(
                "SELECT uid FROM email_debt_scans WHERE owner_user_id = ? AND folder = ?",
                (owner_user_id, folder),
            )
        }


def mark_debt_scanned(
    db_path: str, owner_user_id: int, folder: str, uid: str, is_debt: bool,
    debt_id: int | None = None,
) -> None:
    """Records that this uid has been judged. Only ever called after a real answer from the
    model -- a message that couldn't be read, or that blew up mid-classification, is left
    unmarked on purpose so a later run retries it instead of losing it forever. In a sweep
    whose whole job is a one-time pass over his history, "lost forever" is literal."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO email_debt_scans (owner_user_id, folder, uid, is_debt, debt_id, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_user_id, folder, uid) DO NOTHING""",
            (owner_user_id, folder, uid, int(is_debt), debt_id, _now()),
        )
        conn.commit()


def debt_scan_stats(db_path: str, owner_user_id: int) -> dict:
    """How far the sweep has got through his history, per folder -- the only honest answer
    to "have you finished looking?", which is a question a resumable backlog pass has to be
    able to answer."""
    with closing(_connect(db_path)) as conn:
        rows = _rows(conn.execute(
            """SELECT folder, COUNT(*) AS judged, SUM(is_debt) AS debts
               FROM email_debt_scans WHERE owner_user_id = ? GROUP BY folder ORDER BY folder""",
            (owner_user_id,),
        ))
    return {
        "messages_judged": sum(r["judged"] for r in rows),
        "debt_messages": sum(r["debts"] or 0 for r in rows),
        "by_folder": [{"folder": r["folder"], "judged": r["judged"], "debts": r["debts"] or 0}
                      for r in rows],
    }
