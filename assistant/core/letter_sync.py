"""Ask LetterStream what actually happened to his letters, and believe the answer.

Jack, 2026-09-22, on his three credit disputes: *"on their site im showing each job is in
production phase, and i wont have money in a ballance there. When there are letters to be
paid for i will go in and pay for them."*

He paid for them himself on LetterStream's website. That is a perfectly ordinary thing to
do and this system had no way to notice it. A letter only ever became 'mailed' here if
Jarvis itself ran the authorize step, so three letters that were genuinely in production,
with real USPS certified tracking numbers, still read 'quoted' -- and Jarvis told him
flatly and repeatedly that nothing had been sent. It was wrong three times in a row, and
then tried to authorize jobs LetterStream had already submitted, which failed with
`-960: doauth (id not valid or already submitted)`.

TWO THINGS WERE WRONG, AND ONLY ONE OF THEM WAS THE SYNC.

**The lookup was wrong.** `jobstatus` wants LetterStream's own numeric job id (14912191).
What we store in `job_name` is their BATCH id (jrv1790083419), so every jobstatus call
came back `-928: invalid job id` -- which Jarvis reported to him as "tracking failed on
every one of them, that's not a fluke", and read as evidence that nothing had been sent.
`docstatus` takes the doc id we DO hold, and answers completely: status, cost, date,
tracking number and the numeric job id we were missing.

**Nothing ever asked.** Our record of a letter was a record of what Jarvis had done to it,
not of what was true. Those differ the moment he does anything himself, and a system that
can only see its own actions will contradict him with total confidence.

-960 IS NOT A FAILURE. "Already submitted" means the letter is in the mail. It is the
success case arriving by an unexpected route, and treating it as an error is what turned
a finished job into an argument.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# The bureaus get 30 days from receipt under the FCRA. Counted from mailing rather than
# delivery because that is the date we know on the day it happens; record_letter_delivered
# refines it later from the real delivery date.
RESPONSE_DAYS = 30

# LetterStream answers in prose -- "Job 14912191 In Production" -- so the state has to be
# read out of the sentence. Ordered: the first match wins, so 'delivered' is tested before
# the looser 'production'.
_STATES = (
    ("delivered", re.compile(r"\bdeliver", re.I)),
    ("mailed", re.compile(r"\b(in production|mailed|printed|shipped|in transit|"
                          r"accepted|processing)\b", re.I)),
    ("cancelled", re.compile(r"\b(cancel|void|refund)", re.I)),
)


def read_state(status_text: str) -> str | None:
    """What LetterStream's sentence means for us: mailed, delivered, cancelled, or None.

    None rather than a guess when it says something unrecognised: a letter wrongly marked
    mailed starts a 30-day clock against a deadline that is not running.
    """
    text = str(status_text or "")
    for state, pattern in _STATES:
        if pattern.search(text):
            return state
    return None


def _parse_date(raw: str | None) -> str | None:
    """LetterStream's '09/22/2026 4:15 pm' as an ISO timestamp, or None."""
    if not raw:
        return None
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M%p", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _due_from(mailed_at: str | None) -> str | None:
    if not mailed_at:
        return None
    try:
        when = datetime.fromisoformat(mailed_at)
    except ValueError:
        return None
    return (when + timedelta(days=RESPONSE_DAYS)).date().isoformat()


def _docs(payload: dict) -> list[dict]:
    """LetterStream returns a single dict for one doc and a list for several."""
    found = (payload or {}).get("doc")
    if found is None:
        return []
    return found if isinstance(found, list) else [found]


def status_of(letterstream, doc_ids: list[str]) -> dict:
    """{doc_id: {state, status, tracking, job_id, cost, date}} straight from LetterStream.

    docstatus, never jobstatus: the id we hold is their batch id, and asking jobstatus
    with it returns `-928 invalid job id` for a letter that is perfectly fine.
    """
    if not doc_ids:
        return {}
    out = {}
    for doc in _docs(letterstream.doc_status(*doc_ids)):
        code = str(doc.get("code", "")).strip()
        if code and code != "0":
            logger.info("letterstream: %s -> %s %s", doc.get("id"), code,
                        doc.get("details"))
            continue
        mailed_at = _parse_date(doc.get("date"))
        out[str(doc.get("id"))] = {
            "state": read_state(doc.get("status")),
            "status": doc.get("status"), "tracking": doc.get("tracking") or None,
            "job_id": doc.get("job") or None, "cost": doc.get("cost"),
            "mailed_at": mailed_at, "response_due_at": _due_from(mailed_at),
        }
    return out


def _open_letters(db_path: str, owner_user_id: int) -> list[dict]:
    from contextlib import closing
    import sqlite3

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            """SELECT dl.* FROM dispute_letters dl
                 JOIN dispute_items di ON di.id = dl.dispute_item_id
                WHERE di.owner_user_id = ?
                  AND dl.doc_id IS NOT NULL AND trim(dl.doc_id) <> ''
                  -- Finished means DELIVERED, which is a date on the row rather than a
                  -- status: dispute_letters.status is constrained to quoted|mailed, so
                  -- filtering on status = 'delivered' would match nothing and re-ask
                  -- LetterStream about settled mail forever.
                  AND dl.delivered_at IS NULL""",
            (owner_user_id,))]


def sync(db_path: str, owner_user_id: int, letterstream) -> dict:
    """Reconcile every open letter against LetterStream. Returns what changed.

    Read-only against LetterStream -- docstatus costs nothing and mails nothing. The only
    writes are to our own record, bringing it in line with theirs.
    """
    from . import personal_db

    letters = _open_letters(db_path, owner_user_id)
    if not letters:
        return {"ok": True, "checked": 0, "changed": [], "unchanged": 0}

    by_doc = {l["doc_id"]: l for l in letters}
    try:
        live = status_of(letterstream, list(by_doc))
    except Exception as exc:                                        # noqa: BLE001
        logger.exception("could not reach LetterStream for a status sync")
        return {"ok": False, "why": f"{type(exc).__name__}: {exc}",
                "checked": 0, "changed": [], "unchanged": 0}

    changed, unchanged = [], 0
    for doc_id, letter in by_doc.items():
        found = live.get(doc_id)
        if not found or not found["state"]:
            unchanged += 1
            continue
        if found["state"] == letter.get("status"):
            # Same state, but a tracking number may have appeared since.
            if found["tracking"] and not letter.get("tracking_number"):
                personal_db.update_dispute_letter_tracking(
                    db_path, owner_user_id, letter["id"], found["tracking"])
                changed.append({"letter_id": letter["id"], "to": letter["status"],
                                "tracking": found["tracking"],
                                "note": "tracking number arrived"})
            else:
                unchanged += 1
            continue

        if found["state"] in ("mailed", "delivered"):
            personal_db.mark_dispute_letter_mailed(
                db_path, owner_user_id, letter["id"], tracking_number=found["tracking"])
            if found["state"] == "delivered":
                personal_db.record_letter_delivered(
                    db_path, owner_user_id, letter["id"],
                    delivered_on=found["mailed_at"] or _now_iso(),
                    response_due_at=found["response_due_at"])
            else:
                _set_due(db_path, letter["id"], found["mailed_at"],
                         found["response_due_at"])
            changed.append({"letter_id": letter["id"], "to": found["state"],
                            "recipient": letter.get("recipient_name"),
                            "tracking": found["tracking"],
                            "response_due_at": found["response_due_at"],
                            "note": found["status"]})
        else:
            unchanged += 1

    return {"ok": True, "checked": len(by_doc), "changed": changed,
            "unchanged": unchanged}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_due(db_path: str, letter_id: int, mailed_at: str | None,
             response_due_at: str | None) -> None:
    """The reply deadline, and the real mailing date rather than the moment we noticed.

    The deadline is the entire point of a certified dispute: a bureau that does not answer
    inside 30 days has to delete the item. A letter recorded as mailed with no clock on it
    is a letter nobody will chase.
    """
    from contextlib import closing
    import sqlite3

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """UPDATE dispute_letters
                  SET response_due_at = COALESCE(?, response_due_at),
                      mailed_at = COALESCE(?, mailed_at)
                WHERE id = ?""",
            (response_due_at, mailed_at, letter_id))
        conn.commit()


def already_submitted(error) -> bool:
    """Whether an authorize failure means the letter is already on its way.

    -960 reads as a failure and is the opposite: LetterStream is saying this job has
    already been released into production. Jack paid for three letters on their site, told
    Jarvis to authorize them, and got an error for a job that was already done.
    """
    code = str(getattr(error, "code", "")).strip()
    detail = str(getattr(error, "detail", error) or "").lower()
    return code == "-960" or "already submitted" in detail
