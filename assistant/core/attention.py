"""The attention bus: one path to Jack, and the only thing that knows what it costs.

Every pipeline that wants to reach him currently decides on its own whether to speak, and
none of them can see each other. That is not a theoretical problem. On 2026-09-21 the
weather watcher sent him nineteen texts in three hours about one coastal flood watch --
each send individually correct, because each pass genuinely had a hazard and no pass
could see the other eighteen -- while the store, his stated first priority, produced
nothing for four days and sent him nothing at all.

    Nineteen texts about a flood watch that needed nothing from him.
    Zero texts about the business making no money.

That inversion is the bug. It is not fixable inside either component: the weather watcher
cannot know the store is dead, and the store cannot know he has already been interrupted
nineteen times. Only something that sees every message can, so everything goes through
here and this decides.

Three rules, in order:

1. **Say a thing once.** Not "the same string once" -- the same *subject* once. Topics are
   the unit, because the nineteen texts were nineteen different strings.
2. **Spend a budget.** Attention is finite and routine notices compete for it. Urgency
   buys past the budget; nothing else does.
3. **Rank by mission, not by loudness.** A stalled money mission outranks the weather,
   always, and only this layer knows which is which.

Deliberately NOT a queue with a worker. It is a decision function: callers ask "may I say
this", get a yes or no now, and send through their own channel. A queue would add a
second place where messages can pile up unseen, which is the problem, not the fix.
"""
import hashlib
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# What a message is worth, and what that buys.
#
# `critical` is for things that are about to cost him money, safety or a deadline, and it
# ignores every limit here -- a tornado warning and a failing card payment must never be
# rate-limited by a busy afternoon. Everything else is subject to the budget, which is
# the point: the nineteen flood-watch texts were all `notice`.
PRIORITIES = ("critical", "high", "notice", "low")

# Per-priority cooldown: how long the same topic stays said, in hours.
COOLDOWN_HOURS = {"critical": 0.0, "high": 6.0, "notice": 24.0, "low": 72.0}

# How many non-critical messages he should receive in a rolling day. Set from what he
# actually tolerated before complaining: nineteen in three hours was far past it, and a
# handful a day is a useful assistant.
DAILY_BUDGET = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS attention_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    -- The SUBJECT, not the wording: 'mission:store_revenue', 'weather:flood:watch'.
    -- Dedupe hangs off this, so a caller that varies its prose cannot defeat it.
    topic TEXT NOT NULL,
    priority TEXT NOT NULL,
    -- Rendered text, kept for "what did you already tell me" and for the digest.
    body TEXT NOT NULL,
    -- sent | suppressed | budget. Suppressed messages are KEPT: "what did Jarvis decide
    -- not to tell me" is a question he must be able to ask, and a silent drop that
    -- leaves no trace is how the pipelines went quiet in the first place.
    disposition TEXT NOT NULL,
    -- How many times this topic wanted to speak and was held back since it last spoke.
    -- Reported when it finally does, because "still true, 14 times over" is information
    -- and fourteen identical texts are not.
    held INTEGER NOT NULL DEFAULT 0,
    source TEXT
);

CREATE INDEX IF NOT EXISTS idx_attention_topic ON attention_log(topic, id DESC);
CREATE INDEX IF NOT EXISTS idx_attention_at ON attention_log(at DESC);
"""


def init_attention_db(db_path: str) -> None:
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


def digest(text: str) -> str:
    """A stable hash of what a message SAYS, ignoring how it is punctuated or cased."""
    norm = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())
    return hashlib.sha1(" ".join(norm.split()).encode()).hexdigest()[:16]


def last_said(db_path: str, topic: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT * FROM attention_log
                WHERE topic = ? AND disposition = 'sent' ORDER BY id DESC LIMIT 1""",
            (topic,)).fetchone()
    return dict(row) if row else None


def spent_today(db_path: str, now: datetime | None = None) -> int:
    """Non-critical messages actually sent in the last 24 hours."""
    since = _iso((now or _now()) - timedelta(hours=24))
    with closing(_connect(db_path)) as conn:
        return int(conn.execute(
            """SELECT count(*) FROM attention_log
                WHERE at >= ? AND disposition = 'sent' AND priority != 'critical'""",
            (since,)).fetchone()[0])


def _held_since_last_send(db_path: str, topic: str) -> int:
    with closing(_connect(db_path)) as conn:
        last = conn.execute(
            """SELECT id FROM attention_log
                WHERE topic = ? AND disposition = 'sent' ORDER BY id DESC LIMIT 1""",
            (topic,)).fetchone()
        floor = int(last["id"]) if last else 0
        return int(conn.execute(
            "SELECT count(*) FROM attention_log WHERE topic = ? AND id > ? AND disposition != 'sent'",
            (topic, floor)).fetchone()[0])


def should_say(db_path: str, topic: str, body: str, priority: str = "notice",
               now: datetime | None = None) -> dict:
    """Decide whether this should reach him, and record the decision either way.

    Returns {"say": bool, "body": str, "why": str, "held": int}. The body comes back
    possibly rewritten: when something has been held several times the honest message is
    not the original sentence again, it is that the thing is *still* true and has been
    for a while. Repetition becomes duration, which is what he actually needs to know.
    """
    now = now or _now()
    priority = priority if priority in PRIORITIES else "notice"

    def log(disposition: str, why: str, text: str, held: int = 0) -> dict:
        with closing(_connect(db_path)) as conn:
            conn.execute(
                """INSERT INTO attention_log (at, topic, priority, body, disposition, held, source)
                   VALUES (?,?,?,?,?,?,?)""",
                (_iso(now), topic, priority, text, disposition, held, why))
            conn.commit()
        return {"say": disposition == "sent", "body": text, "why": why, "held": held}

    if priority == "critical":
        return log("sent", "critical, never held", body)

    prior = last_said(db_path, topic)
    if prior is not None:
        said_at = datetime.fromisoformat(prior["at"])
        if said_at.tzinfo is None:
            said_at = said_at.replace(tzinfo=timezone.utc)
        age_hours = (now - said_at).total_seconds() / 3600.0
        cooldown = COOLDOWN_HOURS.get(priority, 24.0)
        if age_hours < cooldown:
            # An unchanged subject inside its cooldown is the nineteen-texts case.
            if digest(prior["body"]) == digest(body):
                return log("suppressed", f"said {age_hours:.1f}h ago, unchanged", body,
                           _held_since_last_send(db_path, topic) + 1)
            # Same subject, genuinely different news. Worth saying, but say what
            # CHANGED rather than restating the whole thing from scratch.
            body = f"Update: {body}"

    if spent_today(db_path, now=now) >= DAILY_BUDGET:
        return log("budget", f"daily budget of {DAILY_BUDGET} spent", body,
                   _held_since_last_send(db_path, topic) + 1)

    held = _held_since_last_send(db_path, topic)
    if held >= 3:
        # It has wanted to say this several times and been held. The duration is now the
        # more useful fact than the sentence.
        body = f"{body} (still true - {held} checks since I last mentioned it)"
    return log("sent", "clear to send", body, held)


def recent(db_path: str, hours: float = 24, disposition: str | None = None,
           limit: int = 100, now: datetime | None = None) -> list[dict]:
    """What he was told, and what he was not. Both, on purpose."""
    since = _iso((now or _now()) - timedelta(hours=hours))
    sql = "SELECT * FROM attention_log WHERE at >= ?"
    params: list = [since]
    if disposition:
        sql += " AND disposition = ?"
        params.append(disposition)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def held_back(db_path: str, hours: float = 24, now: datetime | None = None) -> list[dict]:
    """Topics that wanted to speak and did not, with how often. The other half of the
    ledger: a bus that silently drops things is the failure it was built to prevent."""
    since = _iso((now or _now()) - timedelta(hours=hours))
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            """SELECT topic, count(*) AS times, max(at) AS last_at,
                      max(priority) AS priority
                 FROM attention_log
                WHERE at >= ? AND disposition != 'sent'
                GROUP BY topic ORDER BY times DESC""", (since,))]
