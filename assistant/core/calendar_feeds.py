"""Read-only iCalendar subscriptions — his work calendar, published as an .ics URL.

Distinct from caldav_client/sync_calendar, which two-way syncs his Apple calendars and
turns events into *reminders* he can be nudged about. A work calendar is neither: it is
somebody else's system, he cannot write to it, and 149 meetings must not become 149
reminders that text him. So these land in their own table and surface as `event` entries
on the Agenda — visible, never actuating.

The feed URL is a bearer token in disguise: anyone holding it can read his whole work
calendar. It lives in the database with his other private data, is never logged, and is
redacted out of any error text that might reach a UI or a log line.
"""
from __future__ import annotations

import logging
import re
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import sqlite3

from .db import _connect as _raw_connect, _now

logger = logging.getLogger(__name__)


def _connect(db_path: str) -> sqlite3.Connection:
    """db._connect returns bare tuples; everything here reads rows by name."""
    conn = _raw_connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

# Outlook publishes a bounded window (his runs roughly Feb→Oct), so there is no point
# expanding further than the feed can answer; past that it would invent empty weeks.
LOOKBACK_DAYS = 14
LOOKAHEAD_DAYS = 120

# A published Exchange feed is ~320KB. A ceiling well above that turns a redirect to some
# HTML error page into a clean failure rather than a parse of a login form.
MAX_BYTES = 12 * 1024 * 1024

_TOKENISH = re.compile(r"[0-9a-f]{16,}|[0-9a-f-]{32,}", re.I)

# Almost every one of his meetings is online, and the "location" is then a 120-character
# join URL. On a phone that pushes the actual meeting title off the row to tell him
# nothing -- he joins from the calendar invite, not from this screen. So a URL collapses
# to the name of the thing, and a real address survives intact.
_MEETING_HOSTS = (
    ("teams.microsoft", "Teams"), ("zoom.us", "Zoom"), ("meet.google", "Google Meet"),
    ("webex", "Webex"), ("gotomeet", "GoToMeeting"), ("chime.aws", "Chime (AWS)"),
)


def tidy_location(value: str) -> str:
    """A location worth showing next to the title, or ""."""
    text = " ".join((value or "").split())
    if not text:
        return ""
    lowered = text.lower()
    for needle, label in _MEETING_HOSTS:
        if needle in lowered:
            return label
    if lowered.startswith(("http://", "https://")):
        return "Online"
    # Outlook writes "Microsoft Teams Meeting" as a literal location for online meetings.
    if "teams meeting" in lowered:
        return "Teams"
    return text[:60]


def redact_url(url: str) -> str:
    """A feed URL is a credential. This is what may appear in a log or an error field."""
    if not url:
        return ""
    return _TOKENISH.sub("…", url)[:120]


def init_calendar_feeds(db_path: str) -> None:
    with closing(_connect(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS calendar_feeds (
                id INTEGER PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_synced_at TEXT,
                last_status TEXT,
                last_error TEXT,
                event_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE (owner_user_id, url)
            );

            CREATE TABLE IF NOT EXISTS calendar_feed_events (
                id INTEGER PRIMARY KEY,
                feed_id INTEGER NOT NULL,
                uid TEXT NOT NULL,
                starts_at TEXT NOT NULL,       -- ISO 8601, UTC, or a bare date if all-day
                ends_at TEXT,
                local_date TEXT NOT NULL,      -- the day it falls on in HIS timezone
                local_time TEXT,               -- "09:00", or NULL for an all-day event
                summary TEXT,
                location TEXT,
                all_day INTEGER NOT NULL DEFAULT 0,
                status TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (feed_id) REFERENCES calendar_feeds (id) ON DELETE CASCADE,
                UNIQUE (feed_id, uid, starts_at)
            );

            CREATE INDEX IF NOT EXISTS idx_feed_events_date
                ON calendar_feed_events (local_date);
            """
        )
        conn.commit()


def add_feed(db_path: str, owner_user_id: int, name: str, url: str) -> int:
    """Subscribe to a feed, or rename/re-enable one already subscribed to this URL."""
    init_calendar_feeds(db_path)
    with closing(_connect(db_path)) as conn:
        existing = conn.execute(
            "SELECT id FROM calendar_feeds WHERE owner_user_id = ? AND url = ?",
            (owner_user_id, url)).fetchone()
        if existing:
            conn.execute("UPDATE calendar_feeds SET name = ?, enabled = 1 WHERE id = ?",
                         (name, existing["id"]))
            conn.commit()
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO calendar_feeds (owner_user_id, name, url, created_at)
               VALUES (?, ?, ?, ?)""", (owner_user_id, name, url, _now()))
        conn.commit()
        return cur.lastrowid


def list_feeds(db_path: str, owner_user_id: int | None = None) -> list[dict]:
    init_calendar_feeds(db_path)
    sql = "SELECT * FROM calendar_feeds"
    args: tuple = ()
    if owner_user_id is not None:
        sql += " WHERE owner_user_id = ?"
        args = (owner_user_id,)
    with closing(_connect(db_path)) as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]
    # The URL is never handed back out. Nothing in the UI needs it, and a private feed
    # link in a JSON response is a link in a browser cache.
    for row in rows:
        row["url"] = redact_url(row.pop("url", ""))
    return rows


def set_feed_enabled(db_path: str, feed_id: int, enabled: bool) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE calendar_feeds SET enabled = ? WHERE id = ?",
                     (1 if enabled else 0, feed_id))
        conn.commit()


def remove_feed(db_path: str, feed_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("DELETE FROM calendar_feed_events WHERE feed_id = ?", (feed_id,))
        conn.execute("DELETE FROM calendar_feeds WHERE id = ?", (feed_id,))
        conn.commit()


def _fetch(url: str, timeout: int = 60) -> str:
    import httpx

    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError(f"feed is larger than {MAX_BYTES // 1024 // 1024}MB")
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def parse_events(text: str, tz_name: str, start: date, end: date) -> list[dict]:
    """Every occurrence in the window, already in his timezone.

    Recurrence is expanded rather than read off DTSTART: his feed carries 13 RRULEs and 4
    EXDATEs, so the weekly standup exists once in the file and 17 times in the window.
    Taking DTSTART alone would show it on one day and lose the rest.
    """
    import icalendar
    import recurring_ical_events

    # Checked here rather than at fetch time so it holds however the text arrived. A
    # revoked or mistyped share link answers 200 with a sign-in page; icalendar's own
    # error for that is "Content line could not be parsed into parts", which tells nobody
    # what went wrong. Worse would be parsing it as zero events and calling that a
    # successful sync -- that quietly empties his week and reports success.
    if "BEGIN:VCALENDAR" not in text[:4096]:
        raise ValueError("not an iCalendar feed — the link may have expired")

    tz = ZoneInfo(tz_name)
    calendar = icalendar.Calendar.from_ical(text)
    window_start = datetime.combine(start, time.min, tzinfo=timezone.utc)
    window_end = datetime.combine(end, time.max, tzinfo=timezone.utc)

    out = []
    for event in recurring_ical_events.of(calendar).between(window_start, window_end):
        status = str(event.get("STATUS") or "").upper()
        if status == "CANCELLED":
            continue                       # a meeting called off is not on his day
        raw_start = getattr(event.get("DTSTART"), "dt", None)
        if raw_start is None:
            continue
        raw_end = getattr(event.get("DTEND"), "dt", None)

        # An all-day event's DTSTART is a plain date with no time and no tzinfo. Sending
        # it through astimezone would crash; treating it as midnight UTC would drag it to
        # the previous day for anyone west of London.
        all_day = not isinstance(raw_start, datetime)
        if all_day:
            local_date, local_time, starts_at = raw_start, None, raw_start.isoformat()
        else:
            if raw_start.tzinfo is None:
                raw_start = raw_start.replace(tzinfo=tz)
            local = raw_start.astimezone(tz)
            local_date, local_time = local.date(), local.strftime("%H:%M")
            starts_at = raw_start.astimezone(timezone.utc).isoformat()

        ends_at = None
        if isinstance(raw_end, datetime):
            if raw_end.tzinfo is None:
                raw_end = raw_end.replace(tzinfo=tz)
            ends_at = raw_end.astimezone(timezone.utc).isoformat()
        elif isinstance(raw_end, date):
            ends_at = raw_end.isoformat()

        location = tidy_location(str(event.get("LOCATION") or ""))
        out.append({
            "uid": str(event.get("UID") or "")[:200],
            "starts_at": starts_at,
            "ends_at": ends_at,
            "local_date": local_date.isoformat(),
            "local_time": local_time,
            "summary": " ".join(str(event.get("SUMMARY") or "(no title)").split())[:200],
            "location": location,
            "all_day": 1 if all_day else 0,
            "status": status or None,
        })
    return out


def sync_feed(db_path: str, feed_id: int, tz_name: str = "America/New_York",
              fetch=_fetch) -> dict:
    """Refresh one feed. Returns {"ok", "events", "error"}.

    On success the window is *replaced*, so a meeting cancelled upstream disappears here
    too. On failure nothing is deleted: the last good copy of his week is worth more than
    a technically-current empty one, and a flaky corporate endpoint at 6am must not be the
    reason he walks into the office blind.
    """
    init_calendar_feeds(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM calendar_feeds WHERE id = ?", (feed_id,)).fetchone()
    if row is None:
        return {"ok": False, "events": 0, "error": "no such feed"}

    today = date.today()
    start = today - timedelta(days=LOOKBACK_DAYS)
    end = today + timedelta(days=LOOKAHEAD_DAYS)

    try:
        events = parse_events(fetch(row["url"]), tz_name, start, end)
    except Exception as exc:                                        # noqa: BLE001
        message = redact_url(f"{type(exc).__name__}: {exc}")[:200]
        logger.warning("calendar feed %s (%s) failed: %s", feed_id, row["name"], message)
        with closing(_connect(db_path)) as conn:
            conn.execute(
                """UPDATE calendar_feeds
                      SET last_status = 'error', last_error = ?, last_synced_at = ?
                    WHERE id = ?""", (message, _now(), feed_id))
            conn.commit()
        return {"ok": False, "events": 0, "error": message}

    stamp = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """DELETE FROM calendar_feed_events
                WHERE feed_id = ? AND local_date >= ? AND local_date <= ?""",
            (feed_id, start.isoformat(), end.isoformat()))
        conn.executemany(
            """INSERT OR REPLACE INTO calendar_feed_events
                   (feed_id, uid, starts_at, ends_at, local_date, local_time,
                    summary, location, all_day, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(feed_id, e["uid"], e["starts_at"], e["ends_at"], e["local_date"],
              e["local_time"], e["summary"], e["location"], e["all_day"], e["status"], stamp)
             for e in events])
        conn.execute(
            """UPDATE calendar_feeds
                  SET last_status = 'ok', last_error = NULL,
                      last_synced_at = ?, event_count = ?
                WHERE id = ?""", (stamp, len(events), feed_id))
        conn.commit()
    logger.info("calendar feed %r: %d event(s) in %s..%s", row["name"], len(events), start, end)
    return {"ok": True, "events": len(events), "error": None}


def sync_all(db_path: str, tz_name: str = "America/New_York") -> dict:
    """Every enabled feed. One bad feed never stops the next."""
    results = {}
    init_calendar_feeds(db_path)
    with closing(_connect(db_path)) as conn:
        feeds = [dict(r) for r in conn.execute(
            "SELECT id, name FROM calendar_feeds WHERE enabled = 1 ORDER BY id")]
    for feed in feeds:
        try:
            results[feed["name"]] = sync_feed(db_path, feed["id"], tz_name)
        except Exception as exc:                                    # noqa: BLE001
            logger.exception("calendar feed %s blew up", feed["id"])
            results[feed["name"]] = {"ok": False, "events": 0,
                                     "error": redact_url(str(exc))[:200]}
    return results


def events_between(db_path: str, owner_user_id: int, start: date, end: date) -> list[dict]:
    init_calendar_feeds(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT e.*, f.name AS feed_name
                 FROM calendar_feed_events e
                 JOIN calendar_feeds f ON f.id = e.feed_id
                WHERE f.owner_user_id = ? AND f.enabled = 1
                  AND e.local_date >= ? AND e.local_date <= ?
                ORDER BY e.local_date, COALESCE(e.local_time, '00:00')""",
            (owner_user_id, start.isoformat(), end.isoformat())).fetchall()
    return [dict(r) for r in rows]
