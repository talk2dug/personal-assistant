"""SMS as a Jarvis channel, over the LTE modem on jarvisaudio2.

Why this exists at all, when Telegram and the web UI already reach Jarvis: **SMS is not
the internet**. It rides the cellular control plane, needs no data bearer, works on a
~5W Pi, and keeps working when the house WAN is down and the power is out. That makes it
the one channel that survives the outage the home-security work is meant to cover -- a
sensor trips, Jarvis texts, the owner texts back a command, none of it touching the
router. Every other channel here dies with the internet.

Shape follows the voice terminals rather than inventing a second pattern: the Pi holds
the hardware and POLLS this server (see device/jarvis_cellular.py). The server never
reaches out to the Pi. That direction is deliberate -- the Pi can be on cellular, on
wifi, or moved to another network without anything here needing to know where it went,
and there is no inbound port to open on a device whose whole job is to be reachable when
everything else is not.

THE SECURITY BOUNDARY IS THE ALLOW-LIST. Anyone in the world can text +1 686-207-7308.
An unknown number reaching handle_message() would be an unauthenticated stranger driving
the owner's assistant -- one with mail, money, calendars and the front door. So inbound
is refused unless the sender is explicitly listed, it fails CLOSED (an empty list accepts
nobody), and a refusal is recorded rather than silently dropped, because "who has been
texting this number" is something the owner should be able to look at.
"""
import hashlib
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sms_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    number TEXT NOT NULL,
    text TEXT NOT NULL,
    -- inbound:  received | refused | handled | failed
    -- outbound: queued   | sent    | failed
    status TEXT NOT NULL,
    -- Why an inbound message was refused, or why a send failed. Null on the happy path.
    detail TEXT,
    -- Content fingerprint for inbound dedupe. The Pi may hand us the same message twice
    -- (a retry, or a delete that did not land), and answering a question twice is worse
    -- than missing the retry.
    fingerprint TEXT,
    -- Which inbound message an outbound one answers, so a conversation can be followed.
    reply_to_id INTEGER REFERENCES sms_messages(id),
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sms_status ON sms_messages(status, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sms_fingerprint ON sms_messages(fingerprint)
    WHERE fingerprint IS NOT NULL;
"""

# A single SMS is 160 GSM-7 characters; longer ones are split by the network into
# concatenated parts and billed per part. Jarvis is wordy by nature, so replies are
# trimmed rather than silently costing five segments each.
MAX_SMS_CHARS = 450


def init_cellular_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_number(number: str) -> str:
    """Reduce a phone number to comparable digits.

    The same sender arrives as '+12027408240', '2027408240' or '(202) 740-8240' depending
    on the network, the handset and whether it came through a short code. An allow-list
    that compares raw strings would let the same person in or out depending on formatting,
    which is not a property a security boundary is allowed to have. US numbers are
    reduced to their 10 significant digits so a leading 1 or +1 cannot change the answer.
    """
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def classify(number: str, allowed: list[str] | None,
             guests: list[str] | None) -> str:
    """'owner', 'guest', or 'refused' for an incoming sender.

    The owner list wins if a number is somehow on both -- the more specific grant should
    not be weakened by also appearing on the weaker one.
    """
    if is_allowed(number, allowed):
        return "owner"
    if is_allowed(number, guests):
        return "guest"
    return "refused"


def is_allowed(number: str, allowed: list[str] | None) -> bool:
    """Whether this sender may drive Jarvis.

    Fails closed on purpose: no configured list means nobody, never everybody. The
    failure mode of the other choice is a stranger with the owner's mail and front door.
    """
    if not allowed:
        return False
    target = normalize_number(number)
    if not target:
        return False
    return any(normalize_number(a) == target for a in allowed)


# --- guests -------------------------------------------------------------------
#
# A second, weaker tier. Someone on this list can talk to Jarvis but reaches none of the
# owner's life.
#
# DEFAULT DENY: this enumerates what a guest KEEPS, not what it loses. A block-list
# silently exposes every integration added later -- the next capability wired into the
# assistant would be reachable by a stranger the day it shipped, and nobody would notice.
# With an allow-list the new thing is unavailable to guests until someone decides
# otherwise, which is the failure direction worth having.
#
# Note `home_assistant` is deliberately NOT here, even though presence.py lets a voice-
# terminal guest use it. That is not an inconsistency: a guest at a kiosk is standing in
# the house and could flip the switch by hand. A guest with this number is texting from
# anywhere on earth, and must not be able to unlock a door.
GUEST_KEEPS_CONTEXTS = (
    "recipe",         # recipe lookups -- public data, nothing personal
    "ticketmaster",   # event search
    "airbnb",         # listing search
    "local_llm",      # the local fast path, no data of its own
)

GUEST_KEY_PREFIX = "__sms_guest__:"


def guest_contexts(contexts: dict) -> dict:
    """The same contexts with everything a guest may not reach forced to None.

    Genuinely absent rather than merely discouraged -- the model cannot decline to use a
    tool it was never given, which is the only version of this that holds.
    """
    return {k: (v if k in GUEST_KEEPS_CONTEXTS else None) for k, v in contexts.items()}


def guest_user_id(db_path: str, number: str) -> int:
    """A per-NUMBER guest row, mirroring presence.py's per-device one.

    Per number rather than one shared guest so two people texting never inherit each
    other's conversation, and neither ever touches the owner's history.
    """
    from . import db as core_db
    key = normalize_number(number)
    return core_db.get_or_create_guest_user(
        db_path, f"{GUEST_KEY_PREFIX}{key}", f"SMS guest ({key})")


def fingerprint(number: str, text: str, stamp: str | None) -> str:
    """Stable identity for an inbound message, for dedupe across retries."""
    raw = f"{normalize_number(number)}|{(text or '').strip()}|{(stamp or '')[:16]}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


def record_inbound(db_path: str, number: str, text: str, status: str,
                   stamp: str | None = None, detail: str | None = None) -> int | None:
    """Log an inbound message. Returns its id, or None if it was a duplicate.

    None means "already seen, do nothing" rather than an error: the Pi retrying a
    delivery it was not sure landed is normal and correct behaviour, and must not produce
    a second answer.
    """
    fp = fingerprint(number, text, stamp)
    with closing(_connect(db_path)) as conn:
        try:
            cur = conn.execute(
                """INSERT INTO sms_messages (direction, number, text, status, detail,
                                             fingerprint, created_at)
                   VALUES ('inbound', ?, ?, ?, ?, ?, ?)""",
                (number, text, status, detail, fp, _now()))
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def set_inbound_status(db_path: str, message_id: int, status: str,
                       detail: str | None = None) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE sms_messages SET status = ?, detail = COALESCE(?, detail) "
                     "WHERE id = ?", (status, detail, message_id))
        conn.commit()


def trim_for_sms(text: str) -> str:
    """Fit a reply into a sane number of SMS segments, breaking at a sentence if possible.

    Truncation is visible ('...') rather than silent: a reply that just stops mid-word
    reads like a bug, and the owner cannot tell a cut-off answer from a complete one.
    """
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= MAX_SMS_CHARS:
        return cleaned
    window = cleaned[:MAX_SMS_CHARS]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut > MAX_SMS_CHARS // 2:
        return window[:cut + 1]
    return window.rstrip() + "..."


def queue_outbound(db_path: str, number: str, text: str,
                   reply_to_id: int | None = None) -> int:
    """Put a message in the outbox for the Pi to collect and send."""
    body = trim_for_sms(text)
    if not body:
        raise ValueError("refusing to queue an empty SMS")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO sms_messages (direction, number, text, status, reply_to_id,
                                         created_at)
               VALUES ('outbound', ?, ?, 'queued', ?, ?)""",
            (number, body, reply_to_id, _now()))
        conn.commit()
        return cur.lastrowid


def pending_outbound(db_path: str, limit: int = 10) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            """SELECT id, number, text FROM sms_messages
                WHERE direction = 'outbound' AND status = 'queued'
                ORDER BY id LIMIT ?""", (limit,))]


def mark_sent(db_path: str, message_id: int, ok: bool = True,
              detail: str | None = None) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """UPDATE sms_messages SET status = ?, sent_at = ?, detail = COALESCE(?, detail)
                WHERE id = ? AND direction = 'outbound'""",
            ("sent" if ok else "failed", _now(), detail, message_id))
        conn.commit()
        return cur.rowcount > 0


def recent(db_path: str, limit: int = 50) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM sms_messages ORDER BY id DESC LIMIT ?", (limit,))]
