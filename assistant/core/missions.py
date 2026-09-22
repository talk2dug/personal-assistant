"""Missions: the part of Jarvis that owns an outcome instead of a step.

Everything else in this codebase is defined by a step. `store_manager`'s job is "turn
approved concepts into listings", so on a morning with no approved concepts it does
nothing and reports success -- correctly. It reported success eight times in a row while
the store produced nothing for a week, and no component anywhere could notice, because
noticing requires something that holds the *goal* rather than the step.

Jack, 2026-09-21: *"We continuily have pipelines not moving becuase of this, like the
jarvisStore, nothing is happening, i have not been told why, and i have not been told its
being fixed."* Those are the three things a mission carries and a step never can: whether
the number moved, why it did not, and what was done about it.

The shape is a control loop, because drive is a control loop -- a target, a measured gap,
and escalation that does not stop until the gap closes. It is not a tone of voice in a
system prompt, and no amount of prompting an agent to "be proactive" produces it.

WHAT THIS MODULE IS NOT: it does not decide what to DO about a stall. It measures, it
diagnoses, and it remembers. executive.py acts on what it finds. Kept apart so that the
measuring stays honest -- a component that both sets the target and reports progress
against it will eventually report progress.
"""
import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    -- What winning means, in words he would use. A mission without a number is a wish,
    -- so `metric` names the number and `target` is the number.
    metric TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'count',
    target REAL,
    -- The agents that move this metric, most upstream first, comma separated. The
    -- executive walks it to find the stage where work actually stopped, which is almost
    -- never the stage that looks broken -- a social director with nothing to post is
    -- reporting a product creator that made nothing.
    chain TEXT,
    -- How long the number may sit still before this is a stall worth acting on.
    stall_hours REAL NOT NULL DEFAULT 24,
    -- active | paused | won. Paused matters: a mission he has deliberately parked must
    -- not keep escalating, and deleting it would lose its history.
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Only CHANGES are recorded, never every poll. "When did this last actually move" is the
-- central question of the whole module, and against a table of identical hourly readings
-- it is a scan with a subtlety in it; against this table it is max(at).
CREATE TABLE IF NOT EXISTS mission_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL REFERENCES missions(id),
    at TEXT NOT NULL,
    value REAL NOT NULL,
    note TEXT
);

-- Why it was stuck and what was done, kept because "i have not been told why, and i have
-- not been told its being fixed" is a memory problem as much as a notification one. A
-- stall he was told about yesterday and that was acted on yesterday should read
-- differently tomorrow, and only a record of the attempt makes that possible.
CREATE TABLE IF NOT EXISTS mission_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL REFERENCES missions(id),
    at TEXT NOT NULL,
    -- Why the number is not moving, in one line.
    diagnosis TEXT NOT NULL,
    -- What Jarvis did about it. 'escalated' when the honest answer is "nothing I may do".
    action TEXT NOT NULL,
    -- Filled in on a later tick: did the number move after this?
    outcome TEXT,
    -- Whether this one reached his phone, so a second identical attempt stays quiet.
    told_owner INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mission_readings ON mission_readings(mission_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_mission_interventions
    ON mission_interventions(mission_id, id DESC);
"""


def init_missions_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# --- the missions themselves ------------------------------------------------------

def upsert_mission(db_path: str, key: str, title: str, metric: str, target: float | None,
                   unit: str = "count", chain: str | None = None,
                   stall_hours: float = 24, status: str = "active") -> int:
    """Define a mission, or update its definition. Its history survives either way."""
    now = _iso(_now())
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO missions (key, title, metric, unit, target, chain, stall_hours,
                                     status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   title=excluded.title, metric=excluded.metric, unit=excluded.unit,
                   target=excluded.target, chain=excluded.chain,
                   stall_hours=excluded.stall_hours, updated_at=excluded.updated_at""",
            (key, title, metric, unit, target, chain, stall_hours, status, now, now))
        conn.commit()
        return int(conn.execute("SELECT id FROM missions WHERE key = ?", (key,)).fetchone()["id"])


def get_mission(db_path: str, key: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM missions WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def list_missions(db_path: str, status: str | None = "active") -> list[dict]:
    sql = "SELECT * FROM missions"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY id"
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def set_status(db_path: str, key: str, status: str) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE missions SET status = ?, updated_at = ? WHERE key = ?",
                     (status, _iso(_now()), key))
        conn.commit()


# --- readings ---------------------------------------------------------------------

def record_reading(db_path: str, mission_id: int, value: float, note: str | None = None,
                   now: datetime | None = None) -> bool:
    """Record a measurement. Returns True if the number actually moved.

    An unchanged reading is deliberately not stored. That is the whole trick behind
    last_movement(): the table holds movements, so the most recent row IS the last time
    this mission made progress, however many times it has been polled since.
    """
    now = now or _now()
    with closing(_connect(db_path)) as conn:
        prev = conn.execute(
            "SELECT id, value, note FROM mission_readings WHERE mission_id = ? "
            "ORDER BY id DESC LIMIT 1", (mission_id,)).fetchone()
        if prev is not None and float(prev["value"]) == float(value):
            # The number has not moved, so no new row -- that invariant is what makes
            # the latest row the last progress. But the NOTE can still have changed and
            # still matters: the store sitting at 0 live while the queue behind it goes
            # from 3 waiting to 10 is the same number and a different situation. Refresh
            # it in place so the note stays true without inventing a movement.
            if (prev["note"] or None) != (note or None):
                conn.execute("UPDATE mission_readings SET note = ? WHERE id = ?",
                             (note, prev["id"]))
                conn.commit()
            return False
        conn.execute(
            "INSERT INTO mission_readings (mission_id, at, value, note) VALUES (?,?,?,?)",
            (mission_id, _iso(now), float(value), note))
        conn.commit()
    return True


def latest_reading(db_path: str, mission_id: int) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM mission_readings WHERE mission_id = ? ORDER BY id DESC LIMIT 1",
            (mission_id,)).fetchone()
    return dict(row) if row else None


def last_movement(db_path: str, mission_id: int) -> datetime | None:
    row = latest_reading(db_path, mission_id)
    return _parse(row["at"]) if row else None


def hours_since_movement(db_path: str, mission_id: int,
                         now: datetime | None = None) -> float | None:
    """Hours since this number last changed, or None if it has never been read.

    None is not zero and must not be treated as fresh: a mission that has never produced
    a reading is the most stalled thing in the system, not the newest.
    """
    moved = last_movement(db_path, mission_id)
    if moved is None:
        return None
    return ((now or _now()) - moved).total_seconds() / 3600.0


def is_stalled(db_path: str, mission: dict, now: datetime | None = None) -> bool:
    """Whether this mission has sat still longer than it is allowed to.

    A mission already at or past its target is never stalled -- a won mission that stops
    moving has stopped for the right reason.
    """
    if mission["status"] != "active":
        return False
    reading = latest_reading(db_path, mission["id"])
    if reading is None:
        return True
    if mission["target"] is not None and float(reading["value"]) >= float(mission["target"]):
        return False
    since = hours_since_movement(db_path, mission["id"], now=now)
    return since is not None and since >= float(mission["stall_hours"])


# --- what the chain is doing ------------------------------------------------------

def chain_report(db_path: str, chain: str | None, hours: float = 72,
                 now: datetime | None = None) -> list[dict]:
    """For each agent in a mission's chain: is it running, and is it producing?

    The distinction the whole diagnosis rests on. An agent that is scheduled, running on
    time and skipping every run looks perfectly healthy from every angle the system
    currently has -- green in the office view, no errors in the log, a tidy row in
    agent_runs -- and is the exact signature of a dead pipeline.
    """
    if not chain:
        return []
    now = now or _now()
    since = _iso(now - timedelta(hours=hours))
    out = []
    with closing(_connect(db_path)) as conn:
        for name in [c.strip() for c in chain.split(",") if c.strip()]:
            rows = [dict(r) for r in conn.execute(
                """SELECT status, summary, started_at FROM agent_runs
                    WHERE agent = ? AND started_at >= ?
                    ORDER BY started_at DESC LIMIT 40""", (name, since))]
            # Consecutive non-productive runs, newest first. A single skip is normal;
            # a run of them is the pipeline telling you where it stopped.
            barren = 0
            for r in rows:
                if r["status"] == "ok":
                    break
                barren += 1
            last_ok = next((r["started_at"] for r in rows if r["status"] == "ok"), None)
            out.append({
                "agent": name,
                "runs": len(rows),
                "barren_streak": barren,
                "last_status": rows[0]["status"] if rows else None,
                "last_reason": (rows[0]["summary"] if rows else None),
                "last_ok": last_ok,
                "producing": bool(rows) and barren == 0,
                "never_ran": not rows,
            })
    return out


def first_blocked_stage(report: list[dict]) -> dict | None:
    """The most upstream stage that is not producing -- the real blocker.

    Reading the chain from the downstream end gives the wrong answer every time. The
    store's loudest symptom was the social director having nothing to post; the cause was
    four stages upstream, and fixing anything nearer the symptom would have done nothing.
    """
    for stage in report:
        if stage["never_ran"] or not stage["producing"]:
            return stage
    return None


# --- interventions ----------------------------------------------------------------

def record_intervention(db_path: str, mission_id: int, diagnosis: str, action: str,
                        told_owner: bool = False, now: datetime | None = None) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO mission_interventions (mission_id, at, diagnosis, action, told_owner)
               VALUES (?,?,?,?,?)""",
            (mission_id, _iso(now or _now()), diagnosis, action, 1 if told_owner else 0))
        conn.commit()
        return int(cur.lastrowid)


def recent_interventions(db_path: str, mission_id: int, hours: float = 72,
                         now: datetime | None = None) -> list[dict]:
    since = _iso((now or _now()) - timedelta(hours=hours))
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM mission_interventions
                WHERE mission_id = ? AND at >= ? ORDER BY id DESC""",
            (mission_id, since))]


def already_tried(db_path: str, mission_id: int, action: str, hours: float = 12,
                  now: datetime | None = None) -> bool:
    """Whether this exact remedy was already attempted recently.

    Stops the executive rerunning a remedy every tick when the remedy is not working --
    the machine version of the same repetition he objected to on his phone, and more
    expensive, because each attempt costs a web-searching model call.
    """
    return any(i["action"] == action
               for i in recent_interventions(db_path, mission_id, hours=hours, now=now))


def close_out(db_path: str, mission_id: int, outcome: str, now: datetime | None = None) -> None:
    """Write the result onto the most recent open intervention, once the number moves."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT id FROM mission_interventions
                WHERE mission_id = ? AND outcome IS NULL ORDER BY id DESC LIMIT 1""",
            (mission_id,)).fetchone()
        if row is None:
            return
        conn.execute("UPDATE mission_interventions SET outcome = ? WHERE id = ?",
                     (outcome, row["id"]))
        conn.commit()


# --- the whole picture ------------------------------------------------------------

def snapshot(db_path: str, now: datetime | None = None) -> list[dict]:
    """Every active mission with its number, its gap and its blocker. One call, because
    this is what the Command Center, the briefing and the executive all want."""
    now = now or _now()
    out = []
    for m in list_missions(db_path):
        reading = latest_reading(db_path, m["id"])
        report = chain_report(db_path, m.get("chain"), now=now)
        out.append({
            **m,
            "current": float(reading["value"]) if reading else None,
            # The measure's own words about the number. Often the most useful part:
            # "0 live" reads as a dead shop, while "0 live, 3 waiting to go live" says
            # the factory works and the door is shut, which is a different problem.
            "note": reading["note"] if reading else None,
            "last_movement": reading["at"] if reading else None,
            "hours_since_movement": hours_since_movement(db_path, m["id"], now=now),
            "stalled": is_stalled(db_path, m, now=now),
            "chain_report": report,
            "blocker": first_blocked_stage(report),
            "interventions": recent_interventions(db_path, m["id"], now=now)[:5],
        })
    return out
