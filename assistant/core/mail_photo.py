"""Physical mail, photographed on a phone and read by a model.

Jack's scanner has been stuck in delivery for weeks, so the post is piling up unread --
which is exactly the wrong month for it, with six years of back taxes and a live credit
dispute both running. He asked for an album Jarvis could watch. Apple exposes no API for
a personal Photo Library, and the one mechanism that would work (a public Shared Album)
hands out an unauthenticated URL that anyone holding it can read forever. Photographs of
his post are account numbers, balances and his home address, so that trade was not worth
making. A Share Sheet shortcut posting straight to this server is one tap, authenticated,
and nothing sits in public.

WHAT THIS IS FOR, and what it is not. It reads one envelope or letter and files what it
says. It does NOT decide anything: a bill is not paid, a dispute is not answered, a
deadline is not diarised without him. The most it does on its own is raise a task, which
is reversible and is the thing he actually asked for.

THE READING IS A GUESS AND IS STORED AS ONE. A phone photo of a letter is a bad input --
glare, a fold across the total, half the page in shadow -- and a model reading "$1,240.00"
as "$1.240.00" would be a confident lie in his finances. So every extracted field keeps
the model's own confidence, the raw response is kept next to the parse, and the photo path
is kept so the original can always be looked at again. Nothing here overwrites a number
that came from a bank.
"""
import base64
import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# What a piece of post turns out to be. Kept short on purpose: these are the categories
# that change what he does next, not a taxonomy of stationery.
KINDS = ("bill", "statement", "tax", "legal", "collection", "government",
         "medical", "insurance", "bank", "personal", "junk", "other")

# Kinds where a missed deadline costs real money or a right. These get a task raised
# without being asked; everything else waits to be looked at.
ACTIONABLE = ("bill", "tax", "legal", "collection", "government")

PROMPT = """You are reading a photograph of a piece of physical mail for its recipient.

Report only what you can actually SEE. This is a phone photo, so glare, folds and shadow
are expected. If a value is unreadable or cut off, say null -- never guess at a number,
an account number or a date. A wrong figure here ends up in his finances.

Return ONE JSON object, no prose around it, with these keys:

  "sender"        the organisation that sent it, as printed, or null
  "kind"          one of: bill, statement, tax, legal, collection, government, medical,
                  insurance, bank, personal, junk, other
  "summary"       one sentence, plain English, on what this letter is telling him
  "amount"        the single amount he is being asked to PAY, as a number, or null.
                  Not a balance, not a credit limit, not a previous balance.
  "due_date"      the date payment or a response is due, YYYY-MM-DD, or null
  "account_ref"   the last 4 characters only of any account/reference number, or null.
                  Never return a full account number.
  "action"        what he has to DO, in one short line, or null if nothing is required
  "deadline_risk" true if missing a date on this letter costs money or forfeits a right
  "confidence"    your confidence that the above is right: "high", "medium" or "low"
  "unreadable"    a list of any field names you could not read with confidence

Read carefully for tax notices from the IRS or a state revenue department, and for
collection or legal letters. Those carry deadlines that matter."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_pieces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    -- The original photograph, always kept. A parse is a guess; the photo is the record,
    -- and "go and look at it again" has to stay possible.
    photo_path TEXT,
    sender TEXT,
    kind TEXT,
    summary TEXT,
    amount REAL,
    due_date TEXT,
    -- Last four characters only. A full account number does not belong in a database
    -- that syncs, backs up and gets read by a model.
    account_ref TEXT,
    action TEXT,
    deadline_risk INTEGER NOT NULL DEFAULT 0,
    confidence TEXT,
    -- What the model said it could not read, so a low-confidence field is visible as a
    -- gap rather than passing as fact.
    unreadable TEXT,
    -- The model's whole reply, kept so a bad parse can be diagnosed without the photo.
    raw TEXT,
    -- new | filed | ignored. A piece he has dealt with stops showing up.
    status TEXT NOT NULL DEFAULT 'new',
    task_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mail_pieces_at ON mail_pieces(at DESC);
CREATE INDEX IF NOT EXISTS idx_mail_pieces_status ON mail_pieces(status, id DESC);
"""


def init_mail_photo_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_json(text: str) -> dict | None:
    """The first JSON object in the reply. Models fence it, preface it, or apologise
    around it; none of that should lose the answer."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except ValueError:
            pass
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    start = None
    return None


def _number(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9.\-]", "", value)
        try:
            return float(cleaned) if cleaned not in ("", "-", ".") else None
        except ValueError:
            return None
    return None


def _date(value):
    if not isinstance(value, str):
        return None
    found = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    return found.group(0) if found else None


def _last4(value):
    """Whatever the model returned, only four characters survive. Belt and braces: the
    prompt asks for four, and a model that ignores that must not be the reason a full
    account number lands in the database."""
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value)
    return cleaned[-4:] if cleaned else None


def read_photo(bridge, image_bytes: bytes) -> dict:
    """Run one photo through the vision model. Never raises; returns parsed=False."""
    if bridge is None:
        return {"parsed": False, "error": "the GPU bridge is not configured", "raw": ""}
    try:
        job = bridge.run_sync(
            "mail", "vision", PROMPT,
            images=[base64.b64encode(image_bytes).decode()],
            options={"num_predict": 2048, "num_ctx": 8192})
    except Exception as exc:                                        # noqa: BLE001
        logger.exception("mail photo: vision call failed")
        return {"parsed": False, "error": str(exc)[:200], "raw": ""}

    if job.get("status") != "done":
        return {"parsed": False, "raw": "",
                "error": job.get("error") or f"job status: {job.get('status')}"}

    raw = job.get("result") or ""
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        return {"parsed": False, "raw": raw,
                "error": "no JSON object in the model's reply"}

    kind = str(parsed.get("kind") or "other").strip().lower()
    confidence = str(parsed.get("confidence") or "low").strip().lower()
    unreadable = parsed.get("unreadable")
    return {
        "parsed": True,
        "raw": raw,
        "sender": (str(parsed["sender"]).strip()[:120] if parsed.get("sender") else None),
        "kind": kind if kind in KINDS else "other",
        "summary": (str(parsed["summary"]).strip()[:400] if parsed.get("summary") else None),
        "amount": _number(parsed.get("amount")),
        "due_date": _date(parsed.get("due_date")),
        "account_ref": _last4(parsed.get("account_ref")),
        "action": (str(parsed["action"]).strip()[:200] if parsed.get("action") else None),
        "deadline_risk": bool(parsed.get("deadline_risk")),
        "confidence": confidence if confidence in ("high", "medium", "low") else "low",
        "unreadable": [str(u)[:40] for u in unreadable] if isinstance(unreadable, list) else [],
    }


def record(db_path: str, owner_user_id: int, reading: dict,
           photo_path: str | None = None) -> int:
    """File one piece of post. Returns its id."""
    init_mail_photo_db(db_path)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO mail_pieces
                   (owner_user_id, at, photo_path, sender, kind, summary, amount,
                    due_date, account_ref, action, deadline_risk, confidence,
                    unreadable, raw, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new',?)""",
            (owner_user_id, _now(), photo_path, reading.get("sender"),
             reading.get("kind"), reading.get("summary"), reading.get("amount"),
             reading.get("due_date"), reading.get("account_ref"), reading.get("action"),
             1 if reading.get("deadline_risk") else 0, reading.get("confidence"),
             json.dumps(reading.get("unreadable") or []), (reading.get("raw") or "")[:8000],
             _now()))
        conn.commit()
        return int(cur.lastrowid)


def should_raise_task(reading: dict) -> bool:
    """Whether this piece is worth a task without being asked.

    Deliberately narrow. A task per envelope would turn the list he uses to run his day
    into a pile of junk mail, and then he would stop reading it -- which costs more than
    the occasional missed flyer. So: something to DO, and either a kind that carries a
    real deadline or the model explicitly saying a date matters here.
    """
    if not reading.get("parsed") or not reading.get("action"):
        return False
    return bool(reading.get("deadline_risk")) or reading.get("kind") in ACTIONABLE


def task_text(reading: dict) -> str:
    sender = reading.get("sender") or "Unopened mail"
    action = reading.get("action") or "Deal with this letter"
    bits = [f"{sender}: {action}"]
    if reading.get("amount") is not None:
        bits.append(f"${reading['amount']:,.2f}")
    if reading.get("due_date"):
        bits.append(f"due {reading['due_date']}")
    line = " - ".join(bits)
    if reading.get("confidence") != "high":
        # Said out loud rather than hidden in a column: he should open the envelope
        # before acting on a number a model squinted at.
        line += " (read from a photo, check the letter)"
    return line


def attach_task(db_path: str, piece_id: int, task_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE mail_pieces SET task_id = ? WHERE id = ?", (task_id, piece_id))
        conn.commit()


def recent(db_path: str, owner_user_id: int, limit: int = 50,
           status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM mail_pieces WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    init_mail_photo_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    for row in rows:
        try:
            row["unreadable"] = json.loads(row.get("unreadable") or "[]")
        except ValueError:
            row["unreadable"] = []
    return rows


def set_status(db_path: str, owner_user_id: int, piece_id: int, status: str) -> bool:
    if status not in ("new", "filed", "ignored"):
        raise ValueError("status must be new, filed or ignored")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE mail_pieces SET status = ? WHERE id = ? AND owner_user_id = ?",
            (status, piece_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0
