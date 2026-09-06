"""SQLite-backed storage with privacy-scoped access for the Jarvis core assistant.

All reads/writes go through the functions here, which take the requesting
user's id explicitly and enforce private/shared visibility rules in the query
itself. No other part of the codebase should touch the database directly.

Each call opens its own short-lived connection (WAL mode) rather than sharing
one connection across threads — the scheduler and the Telegram handlers run
in different threads, and SQLite connection objects aren't safe to share
across threads without care.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat_id TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'partner'))
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    scope TEXT NOT NULL CHECK (scope IN ('private', 'shared')),
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    sent_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Places Jarvis knows, kept here rather than as Home Assistant zones.
--
-- HA zones are the "official" way to do this, but creating one is manual UI work per
-- place and the user explicitly doesn't want that. His phone already reports raw GPS to
-- HA at ~5m accuracy, so Jarvis can hold its own geofences and he can name a place just
-- by being there: "remember this as the gym". Nothing needs configuring in HA.
CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    -- Generous by default: phone GPS drifts, and a radius that's too tight produces
    -- arrive/leave flapping every time he walks past a window.
    radius_m REAL NOT NULL DEFAULT 150,
    notes TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(owner_user_id, name)
);

-- Reminders that fire on arriving at or leaving a place rather than at a time. A
-- separate table from `reminders` on purpose: those are time-driven and every row needs
-- a due_at, and forcing a sentinel date in there to represent "no time" is the kind of
-- lie that eventually fires at the wrong moment.
CREATE TABLE IF NOT EXISTS location_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    place_id INTEGER NOT NULL REFERENCES places(id),
    trigger TEXT NOT NULL CHECK (trigger IN ('arrive', 'leave')),
    text TEXT NOT NULL,
    once INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_fired_at TEXT,
    done_at TEXT
);

-- Standing routines: "when I leave work, tell me the commute and what's waiting."
-- The action is a prompt run through Jarvis, so a routine can do anything he can,
-- without inventing a second little automation language.
CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('arrive', 'leave')),
    place_id INTEGER NOT NULL REFERENCES places(id),
    prompt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    -- Stops a routine re-firing while GPS jitters across the boundary.
    cooldown_minutes INTEGER NOT NULL DEFAULT 30,
    last_fired_at TEXT,
    created_at TEXT NOT NULL
);

-- Durable assistant-wide preferences. Things the user says once and expects to hold —
-- "send your notifications to my phone when I'm out" is behaviour, not a note, so it
-- has to survive a restart and be readable by the scheduler, not just by a chat turn.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('awaiting_confirmation', 'confirmed', 'cancelled')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS savings_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    target_amount REAL NOT NULL,
    target_date TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS era_account_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    account_type TEXT,
    balance REAL,
    available_balance REAL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS era_recurring_charge_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    charge_key TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('income', 'expense')),
    cadence TEXT,
    next_expected_date TEXT,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS era_category_spending_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_key TEXT NOT NULL,
    period TEXT NOT NULL CHECK (period IN ('this_month', 'last_30_days')),
    label TEXT NOT NULL,
    amount REAL NOT NULL,
    percent_of_total REAL,
    transaction_count INTEGER,
    last_synced_at TEXT NOT NULL,
    UNIQUE(category_key, period)
);

CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    category_key TEXT NOT NULL,
    category_label TEXT NOT NULL,
    monthly_limit REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(owner_user_id, category_key)
);

CREATE TABLE IF NOT EXISTS manual_recurring_charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('income', 'expense')),
    cadence TEXT NOT NULL,
    next_expected_date TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE migrations for columns added after the initial
    CREATE TABLE IF NOT EXISTS — safe to call on every startup, on a fresh or
    already-populated database."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(reminders)")}
    if "caldav_uid" not in existing:
        conn.execute("ALTER TABLE reminders ADD COLUMN caldav_uid TEXT")
    if "caldav_calendar" not in existing:
        conn.execute("ALTER TABLE reminders ADD COLUMN caldav_calendar TEXT")

    charge_cols = {row[1] for row in conn.execute("PRAGMA table_info(era_recurring_charge_cache)")}
    if "excluded" not in charge_cols:
        conn.execute("ALTER TABLE era_recurring_charge_cache ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- places ------------------------------------------------------------------

def create_place(
    db_path: str, owner_user_id: int, name: str, latitude: float, longitude: float,
    radius_m: float = 150, notes: str | None = None,
) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO places (owner_user_id, name, latitude, longitude, radius_m, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_user_id, name) DO UPDATE SET
                 latitude = excluded.latitude, longitude = excluded.longitude,
                 radius_m = excluded.radius_m, notes = COALESCE(excluded.notes, places.notes)""",
            (owner_user_id, name, latitude, longitude, radius_m, notes, _now()),
        )
        conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM places WHERE owner_user_id = ? AND name = ?", (owner_user_id, name)
        ).fetchone()
        return row[0]


def list_places(db_path: str, owner_user_id: int) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM places WHERE owner_user_id = ? ORDER BY name", (owner_user_id,))]


def get_place_by_name(db_path: str, owner_user_id: int, name: str):
    with closing(_connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM places WHERE owner_user_id = ? AND name = ? COLLATE NOCASE",
            (owner_user_id, name),
        ).fetchone()
        return dict(row) if row else None


def delete_place(db_path: str, owner_user_id: int, place_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        # Clear dependents first: a routine pointing at a deleted place would never fire
        # and would be invisible in any "why didn't this run" investigation.
        conn.execute("DELETE FROM location_reminders WHERE place_id = ? AND owner_user_id = ?",
                     (place_id, owner_user_id))
        conn.execute("DELETE FROM routines WHERE place_id = ? AND owner_user_id = ?",
                     (place_id, owner_user_id))
        cur = conn.execute("DELETE FROM places WHERE id = ? AND owner_user_id = ?",
                           (place_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0


# --- location reminders ------------------------------------------------------

def create_location_reminder(
    db_path: str, owner_user_id: int, place_id: int, trigger: str, text: str, once: bool = True,
) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO location_reminders (owner_user_id, place_id, trigger, text, once, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (owner_user_id, place_id, trigger, text, 1 if once else 0, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_location_reminders(db_path: str, owner_user_id: int, include_done: bool = False) -> list[dict]:
    query = ("SELECT r.*, p.name AS place_name FROM location_reminders r "
             "JOIN places p ON p.id = r.place_id WHERE r.owner_user_id = ?")
    if not include_done:
        query += " AND r.done_at IS NULL"
    query += " ORDER BY r.created_at"
    with closing(_connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(query, (owner_user_id,))]


def due_location_reminders(db_path: str, place_id: int, trigger: str) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            """SELECT r.*, p.name AS place_name FROM location_reminders r
               JOIN places p ON p.id = r.place_id
               WHERE r.place_id = ? AND r.trigger = ? AND r.done_at IS NULL""",
            (place_id, trigger))]


def mark_location_reminder_fired(db_path: str, reminder_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """UPDATE location_reminders
               SET last_fired_at = ?, done_at = CASE WHEN once = 1 THEN ? ELSE NULL END
               WHERE id = ?""",
            (_now(), _now(), reminder_id),
        )
        conn.commit()


def cancel_location_reminder(db_path: str, owner_user_id: int, reminder_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE location_reminders SET done_at = ? WHERE id = ? AND owner_user_id = ? AND done_at IS NULL",
            (_now(), reminder_id, owner_user_id),
        )
        conn.commit()
        return cur.rowcount > 0


# --- routines ----------------------------------------------------------------

def create_routine(
    db_path: str, owner_user_id: int, name: str, trigger: str, place_id: int, prompt: str,
    cooldown_minutes: int = 30,
) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO routines (owner_user_id, name, trigger, place_id, prompt, cooldown_minutes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, name, trigger, place_id, prompt, cooldown_minutes, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_routines(db_path: str, owner_user_id: int) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            """SELECT r.*, p.name AS place_name FROM routines r
               JOIN places p ON p.id = r.place_id
               WHERE r.owner_user_id = ? ORDER BY r.created_at""", (owner_user_id,))]


def due_routines(db_path: str, place_id: int, trigger: str) -> list[dict]:
    """Enabled routines for this transition whose cooldown has expired."""
    with closing(_connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            """SELECT r.*, p.name AS place_name FROM routines r
               JOIN places p ON p.id = r.place_id
               WHERE r.place_id = ? AND r.trigger = ? AND r.enabled = 1
                 AND (r.last_fired_at IS NULL
                      OR datetime(r.last_fired_at) <= datetime('now', '-' || r.cooldown_minutes || ' minutes'))""",
            (place_id, trigger))]


def has_armed_location_triggers(db_path: str, owner_user_id: int) -> bool:
    """Whether anything is actually waiting on the user moving.

    Forcing a location update costs a push to the phone, so the watcher only pays that
    when there is something to trigger. With no location reminders and no enabled
    routines there is nothing to learn by waking the phone every few minutes.
    """
    with closing(_connect(db_path)) as conn:
        reminders = conn.execute(
            "SELECT 1 FROM location_reminders WHERE owner_user_id = ? AND done_at IS NULL LIMIT 1",
            (owner_user_id,),
        ).fetchone()
        if reminders:
            return True
        routines = conn.execute(
            "SELECT 1 FROM routines WHERE owner_user_id = ? AND enabled = 1 LIMIT 1",
            (owner_user_id,),
        ).fetchone()
        return routines is not None


def mark_routine_fired(db_path: str, routine_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE routines SET last_fired_at = ? WHERE id = ?", (_now(), routine_id))
        conn.commit()


def set_routine_enabled(db_path: str, owner_user_id: int, routine_id: int, enabled: bool) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE routines SET enabled = ? WHERE id = ? AND owner_user_id = ?",
            (1 if enabled else 0, routine_id, owner_user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_routine(db_path: str, owner_user_id: int, routine_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute("DELETE FROM routines WHERE id = ? AND owner_user_id = ?",
                           (routine_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0


# --- settings ----------------------------------------------------------------

def get_setting(db_path: str, key: str, default: str | None = None) -> str | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(db_path: str, key: str, value: str) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, _now()),
        )
        conn.commit()


def all_settings(db_path: str) -> dict:
    with closing(_connect(db_path)) as conn:
        return {row[0]: row[1] for row in conn.execute("SELECT key, value FROM settings")}


# --- users -------------------------------------------------------------------

def upsert_user(db_path: str, telegram_chat_id: str, display_name: str, role: str) -> int:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO users (telegram_chat_id, display_name, role)
               VALUES (?, ?, ?)
               ON CONFLICT(telegram_chat_id) DO UPDATE SET display_name = excluded.display_name""",
            (telegram_chat_id, display_name, role),
        )
        conn.commit()
    return get_user_by_chat_id(db_path, telegram_chat_id)["id"]


def get_user_by_chat_id(db_path: str, telegram_chat_id: str):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT id, telegram_chat_id, display_name, role FROM users WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "telegram_chat_id": row[1], "display_name": row[2], "role": row[3]}


def get_user_by_id(db_path: str, user_id: int):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT id, telegram_chat_id, display_name, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "telegram_chat_id": row[1], "display_name": row[2], "role": row[3]}


def all_users(db_path: str):
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("SELECT id, telegram_chat_id, display_name, role FROM users").fetchall()
    return [{"id": r[0], "telegram_chat_id": r[1], "display_name": r[2], "role": r[3]} for r in rows]


# --- reminders -----------------------------------------------------------------

def add_reminder(db_path: str, requesting_user_id: int, text: str, due_at: str, scope: str) -> int:
    if scope not in ("private", "shared"):
        raise ValueError(f"invalid scope: {scope!r}")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO reminders (owner_user_id, scope, text, due_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (requesting_user_id, scope, text, due_at, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_reminders(db_path: str, requesting_user_id: int, scope_filter: str | None = None):
    """Reminders visible to requesting_user_id: their own private ones, plus every shared one."""
    query = """
        SELECT id, owner_user_id, scope, text, due_at, sent_at, created_at, caldav_uid, caldav_calendar
        FROM reminders
        WHERE ((scope = 'private' AND owner_user_id = ?) OR scope = 'shared')
    """
    params = [requesting_user_id]
    if scope_filter is not None:
        if scope_filter not in ("private", "shared"):
            raise ValueError(f"invalid scope_filter: {scope_filter!r}")
        query += " AND scope = ?"
        params.append(scope_filter)
    query += " ORDER BY due_at ASC"
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_reminder_row(r) for r in rows]


def cancel_reminder(db_path: str, requesting_user_id: int, reminder_id: int) -> bool:
    """Deletes a reminder if the requester owns it, or it's shared. False if not found/not permitted."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT owner_user_id, scope FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        if row is None:
            return False
        owner_user_id, scope = row
        if scope == "private" and owner_user_id != requesting_user_id:
            return False
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.commit()
        return True


def get_reminder_by_id(db_path: str, reminder_id: int):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT id, owner_user_id, scope, text, due_at, sent_at, created_at, caldav_uid, caldav_calendar
               FROM reminders WHERE id = ?""",
            (reminder_id,),
        ).fetchone()
    return _reminder_row(row) if row else None


def due_reminders(db_path: str, as_of: str | None = None):
    """All unsent reminders due at or before as_of (default: now). Used by the scheduler — not scoped
    to a single user, since it needs to notify every relevant recipient regardless of who's asking."""
    as_of = as_of or _now()
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT id, owner_user_id, scope, text, due_at, sent_at, created_at, caldav_uid, caldav_calendar
               FROM reminders WHERE sent_at IS NULL AND due_at <= ?""",
            (as_of,),
        ).fetchall()
    return [_reminder_row(r) for r in rows]


def mark_reminder_sent(db_path: str, reminder_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE reminders SET sent_at = ? WHERE id = ?", (_now(), reminder_id))
        conn.commit()


def _reminder_row(r):
    return {
        "id": r[0], "owner_user_id": r[1], "scope": r[2], "text": r[3],
        "due_at": r[4], "sent_at": r[5], "created_at": r[6],
        "caldav_uid": r[7], "caldav_calendar": r[8],
    }


# --- CalDAV linkage (Apple Calendar two-way sync) -------------------------------------

def set_caldav_link(db_path: str, reminder_id: int, uid: str, calendar_url: str) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE reminders SET caldav_uid = ?, caldav_calendar = ? WHERE id = ?",
            (uid, calendar_url, reminder_id),
        )
        conn.commit()


def find_reminder_by_caldav_uid(db_path: str, uid: str):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT id, owner_user_id, scope, text, due_at, sent_at, created_at, caldav_uid, caldav_calendar
               FROM reminders WHERE caldav_uid = ?""",
            (uid,),
        ).fetchone()
    return _reminder_row(row) if row else None


def reminders_linked_to_calendar(db_path: str, calendar_url: str):
    """Not-yet-fired reminders currently linked to this calendar — used by the pull-sync
    to detect events that were deleted on the Apple Calendar side."""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT id, owner_user_id, scope, text, due_at, sent_at, created_at, caldav_uid, caldav_calendar
               FROM reminders WHERE caldav_calendar = ? AND caldav_uid IS NOT NULL AND sent_at IS NULL""",
            (calendar_url,),
        ).fetchall()
    return [_reminder_row(r) for r in rows]


def update_reminder_from_remote(db_path: str, reminder_id: int, text: str, due_at: str) -> None:
    """Applies an Apple-Calendar-side edit to the linked reminder — Apple's version wins on pull."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE reminders SET text = ?, due_at = ? WHERE id = ?", (text, due_at, reminder_id)
        )
        conn.commit()


# --- conversations ---------------------------------------------------------------

def add_message(db_path: str, user_id: int, role: str, content: str) -> None:
    if role not in ("user", "assistant"):
        raise ValueError(f"invalid role: {role!r}")
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO conversations (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, _now()),
        )
        conn.commit()


def recent_messages(db_path: str, user_id: int, limit: int = 20):
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT role, content FROM conversations WHERE user_id = ?
               ORDER BY id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


# --- pending actions (confirmation gate for sensitive tool calls) --------------------

def create_pending_action(db_path: str, user_id: int, tool_name: str, arguments: dict) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO pending_actions (user_id, tool_name, arguments, status, created_at)
               VALUES (?, ?, ?, 'awaiting_confirmation', ?)""",
            (user_id, tool_name, json.dumps(arguments), _now()),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_action(db_path: str, user_id: int):
    """The most recent awaiting-confirmation action for this user, if any."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT id, user_id, tool_name, arguments, status, created_at
               FROM pending_actions WHERE user_id = ? AND status = 'awaiting_confirmation'
               ORDER BY id DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "user_id": row[1], "tool_name": row[2],
        "arguments": json.loads(row[3]), "status": row[4], "created_at": row[5],
    }


def resolve_pending_action(db_path: str, action_id: int, status: str) -> None:
    if status not in ("confirmed", "cancelled"):
        raise ValueError(f"invalid resolution status: {status!r}")
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE pending_actions SET status = ? WHERE id = ?", (status, action_id))
        conn.commit()


# --- savings goals (ours, not Era's) ---------------------------------------------

def create_savings_goal(db_path: str, owner_user_id: int, name: str, target_amount: float, target_date: str | None = None) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO savings_goals (owner_user_id, name, target_amount, target_date, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (owner_user_id, name, target_amount, target_date, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_savings_goals(db_path: str, owner_user_id: int):
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT id, owner_user_id, name, target_amount, target_date, created_at
               FROM savings_goals WHERE owner_user_id = ? ORDER BY target_date IS NULL, target_date ASC""",
            (owner_user_id,),
        ).fetchall()
    return [_goal_row(r) for r in rows]


def update_savings_goal(
    db_path: str, goal_id: int, name: str | None = None, target_amount: float | None = None,
    target_date: str | None = "__unset__",
) -> bool:
    """Only non-None (or explicitly-passed target_date) fields are updated. Returns False if not found."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT id FROM savings_goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            return False
        fields, params = [], []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if target_amount is not None:
            fields.append("target_amount = ?")
            params.append(target_amount)
        if target_date != "__unset__":
            fields.append("target_date = ?")
            params.append(target_date)
        if fields:
            params.append(goal_id)
            conn.execute(f"UPDATE savings_goals SET {', '.join(fields)} WHERE id = ?", params)
            conn.commit()
        return True


def delete_savings_goal(db_path: str, goal_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute("DELETE FROM savings_goals WHERE id = ?", (goal_id,))
        conn.commit()
        return cur.rowcount > 0


def _goal_row(r):
    return {
        "id": r[0], "owner_user_id": r[1], "name": r[2],
        "target_amount": r[3], "target_date": r[4], "created_at": r[5],
    }


# --- Era cache (balances + recurring charges, refreshed periodically) ------------

def upsert_era_account(
    db_path: str, account_key: str, name: str, account_type: str | None,
    balance: float | None, available_balance: float | None,
) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO era_account_cache (account_key, name, account_type, balance, available_balance, last_synced_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_key) DO UPDATE SET
                   name = excluded.name, account_type = excluded.account_type,
                   balance = excluded.balance, available_balance = excluded.available_balance,
                   last_synced_at = excluded.last_synced_at""",
            (account_key, name, account_type, balance, available_balance, _now()),
        )
        conn.commit()


def list_era_accounts(db_path: str):
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT account_key, name, account_type, balance, available_balance, last_synced_at FROM era_account_cache"
        ).fetchall()
    return [
        {
            "account_key": r[0], "name": r[1], "account_type": r[2],
            "balance": r[3], "available_balance": r[4], "last_synced_at": r[5],
        }
        for r in rows
    ]


def upsert_era_recurring_charge(
    db_path: str, charge_key: str, description: str, amount: float, direction: str,
    cadence: str | None, next_expected_date: str | None,
) -> None:
    if direction not in ("income", "expense"):
        raise ValueError(f"invalid direction: {direction!r}")
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO era_recurring_charge_cache
                   (charge_key, description, amount, direction, cadence, next_expected_date, last_synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(charge_key) DO UPDATE SET
                   description = excluded.description, amount = excluded.amount, direction = excluded.direction,
                   cadence = excluded.cadence, next_expected_date = excluded.next_expected_date,
                   last_synced_at = excluded.last_synced_at""",
            (charge_key, description, amount, direction, cadence, next_expected_date, _now()),
        )
        conn.commit()


def list_era_recurring_charges(db_path: str, include_excluded: bool = True):
    """include_excluded=False is what forecasting code should use — a user-excluded
    Era detection (e.g. a mis-flagged internal transfer) is intentionally kept out of
    projections/calendar while still being visible to a management UI that wants to
    show and toggle it. Note: SQLite's upsert syntax also uses the word "excluded" as
    a pseudo-table name (unrelated) — the ON CONFLICT clause in upsert_era_recurring_charge
    deliberately never references this column, which is what makes it survive a resync."""
    query = """SELECT charge_key, description, amount, direction, cadence, next_expected_date,
                      last_synced_at, excluded FROM era_recurring_charge_cache"""
    if not include_excluded:
        query += " WHERE excluded = 0"
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(query).fetchall()
    return [
        {
            "charge_key": r[0], "description": r[1], "amount": r[2], "direction": r[3],
            "cadence": r[4], "next_expected_date": r[5], "last_synced_at": r[6], "excluded": bool(r[7]),
        }
        for r in rows
    ]


def set_era_recurring_charge_excluded(db_path: str, charge_key: str, excluded: bool) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE era_recurring_charge_cache SET excluded = ? WHERE charge_key = ?",
            (1 if excluded else 0, charge_key),
        )
        conn.commit()
        return cur.rowcount > 0


def upsert_era_category_spending(
    db_path: str, category_key: str, period: str, label: str,
    amount: float, percent_of_total: float | None, transaction_count: int | None,
) -> None:
    if period not in ("this_month", "last_30_days"):
        raise ValueError(f"invalid period: {period!r}")
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO era_category_spending_cache
                   (category_key, period, label, amount, percent_of_total, transaction_count, last_synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(category_key, period) DO UPDATE SET
                   label = excluded.label, amount = excluded.amount,
                   percent_of_total = excluded.percent_of_total, transaction_count = excluded.transaction_count,
                   last_synced_at = excluded.last_synced_at""",
            (category_key, period, label, amount, percent_of_total, transaction_count, _now()),
        )
        conn.commit()


def list_era_category_spending(db_path: str, period: str):
    if period not in ("this_month", "last_30_days"):
        raise ValueError(f"invalid period: {period!r}")
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT category_key, label, amount, percent_of_total, transaction_count, last_synced_at
               FROM era_category_spending_cache WHERE period = ? ORDER BY amount DESC""",
            (period,),
        ).fetchall()
    return [
        {
            "category_key": r[0], "label": r[1], "amount": r[2],
            "percent_of_total": r[3], "transaction_count": r[4], "last_synced_at": r[5],
        }
        for r in rows
    ]


# --- budgets (ours, not Era's — Era has categories/spending analysis but no limits) ---

def create_budget(db_path: str, owner_user_id: int, category_key: str, category_label: str, monthly_limit: float) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO budgets (owner_user_id, category_key, category_label, monthly_limit, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(owner_user_id, category_key) DO UPDATE SET monthly_limit = excluded.monthly_limit""",
            (owner_user_id, category_key, category_label, monthly_limit, _now()),
        )
        conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM budgets WHERE owner_user_id = ? AND category_key = ?", (owner_user_id, category_key)
        ).fetchone()
        return row[0]


def list_budgets(db_path: str, owner_user_id: int):
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT id, owner_user_id, category_key, category_label, monthly_limit, created_at
               FROM budgets WHERE owner_user_id = ? ORDER BY category_label""",
            (owner_user_id,),
        ).fetchall()
    return [
        {
            "id": r[0], "owner_user_id": r[1], "category_key": r[2],
            "category_label": r[3], "monthly_limit": r[4], "created_at": r[5],
        }
        for r in rows
    ]


def delete_budget(db_path: str, budget_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))
        conn.commit()
        return cur.rowcount > 0


# --- manual recurring charges (bills Era's own detection missed or got wrong) ------

def create_manual_recurring_charge(
    db_path: str, owner_user_id: int, description: str, amount: float,
    direction: str, cadence: str, next_expected_date: str,
) -> int:
    if direction not in ("income", "expense"):
        raise ValueError(f"invalid direction: {direction!r}")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO manual_recurring_charges
                   (owner_user_id, description, amount, direction, cadence, next_expected_date, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, description, amount, direction, cadence, next_expected_date, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_manual_recurring_charges(db_path: str, owner_user_id: int):
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT id, description, amount, direction, cadence, next_expected_date, created_at
               FROM manual_recurring_charges WHERE owner_user_id = ? ORDER BY description""",
            (owner_user_id,),
        ).fetchall()
    return [
        {
            "id": r[0], "description": r[1], "amount": r[2], "direction": r[3],
            "cadence": r[4], "next_expected_date": r[5], "created_at": r[6],
        }
        for r in rows
    ]


def delete_manual_recurring_charge(db_path: str, charge_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute("DELETE FROM manual_recurring_charges WHERE id = ?", (charge_id,))
        conn.commit()
        return cur.rowcount > 0
