"""One-shot staging for "Jarvis shows the owner something mid-conversation" -- the
show_content tool's landing spot (engine.py). Same shape as vision.py's
pending_camera_views (set/pop, one row per user, cleared the instant it's read) but
deliberately its own table rather than overloading vision.py, which stays scoped to
cameras/presence. See routes/chat.py's send_message, which pops this in the same HTTP
turn right after the LLM reply, exactly like it already does for the camera view.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_ui_content (
    user_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init_ui_content_db(db_path: str) -> None:
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


def set_pending_content(db_path: str, user_id: int, kind: str, payload: dict) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO pending_ui_content (user_id, kind, payload_json, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   kind = excluded.kind, payload_json = excluded.payload_json,
                   created_at = excluded.created_at""",
            (user_id, kind, json.dumps(payload), _now()),
        )
        conn.commit()


def pop_pending_content(db_path: str, user_id: int) -> dict | None:
    """One-shot: returns the staged content (if any) and clears it, so a slow poller
    can't pop the same modal open twice -- identical reasoning to
    vision.pop_pending_camera_view."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT kind, payload_json FROM pending_ui_content WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM pending_ui_content WHERE user_id = ?", (user_id,))
        conn.commit()
        return {"kind": row["kind"], **json.loads(row["payload_json"])}
