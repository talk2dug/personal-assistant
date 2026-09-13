"""Bill detection from email content.

Project 13's unbuilt half: "detects bills and records amount/due date/recurring status".
Era's recurring-charge detection (db.list_era_recurring_charges, finance.py) works off
bank/Era transaction data, so it can only ever notice a bill AFTER the money has moved,
and never notices one that exists solely as an emailed invoice on an account nothing is
auto-paying. This reads the mail itself.

Shaped deliberately like mail_triage.py -- scan recent mail, ask the LLM one targeted
question per message, store the answer in its own table, surface it as a review_items
row -- because that is this codebase's proven "classify autonomously, act never" pattern.
This module READS and RECORDS only. It cannot archive, delete, move, mark read, or send:
no code path here calls anything that mutates the mailbox, which is exactly what makes it
safe to run unattended on the owner's real inbox.

Two deliberate inversions of mail_triage's rules, both load-bearing:

  * mail_triage skips AUTOMATED_SENDER_PATTERNS (no-reply@, notifications@, billing
    robots) because nobody reads a reply to those. Here that filter would delete the
    feature -- essentially every real bill arrives from exactly those addresses. So the
    automated-sender skip is intentionally absent, and its job (keeping rubbish out) is
    done instead by scanning only non-junk mail: the junk pass has normally already moved
    junk out of INBOX, and anything it flagged but failed to move is skipped explicitly
    via mail_db.is_logged_junk -- a scam invoice both scores as junk and reads like a bill.

  * mail_triage only records its hits, so every newsletter in the inbox costs it an LLM
    round trip on every single tick. This one records negatives too (email_bill_scans), so
    a message is judged exactly once.

The one real-world side effect is a reminder (db.add_reminder) for a plausible future due
date -- a nudge, dated ahead of the date, never a payment. Nothing here writes to
manual_recurring_charges: the owner's budget and projection math reads that table, and a
fuzzy email classifier inserting rows into it would corrupt real financial projections.
A recurring-looking bill is proposed in the review card and left for him to act on.
"""
import json
import logging
import re
from datetime import date, timedelta

from . import business_db, db, mail_db

logger = logging.getLogger(__name__)

# How far ahead of the due date the nudge lands. Three days is enough to move money or
# catch a failed autopay without the reminder arriving so early it gets dismissed and
# forgotten.
REMINDER_LEAD_DAYS = 3
# Reminder at 09:00 local wall-clock on the day it's due to fire, same naive-local shape
# meal_plan_db.schedule_freezer_pulls already uses for its "tonight" reminders.
REMINDER_HOUR = "09:00:00"
# A due date further out than this is a model hallucination (a "2130" or a misread
# statement-period year), not a bill -- the row is still recorded, it just doesn't get a
# reminder sitting in the table for a century.
MAX_DUE_DATE_HORIZON_DAYS = 400

BILL_SYSTEM = (
    "You read one email and decide whether it is a BILL: a request for money the "
    "recipient personally owes, or a notice that a payment he owes is coming due.\n\n"
    "Reply with ONLY a JSON object, no other text and no markdown fences, in this exact "
    "shape:\n"
    '{"is_bill": true or false, "payee": who is owed (company or person name), '
    '"amount": the amount due EXACTLY as the message writes it, including the currency '
    'symbol (e.g. "$142.53"), or "" if the message never states one, '
    '"due_date": the due date as YYYY-MM-DD, or "" if the message does not state or '
    'clearly imply one, '
    '"due_date_text": how the message itself phrases the timing (e.g. "due on receipt", '
    '"within 30 days"), or "", '
    '"is_recurring": true if this is a regularly repeating bill (utility, subscription, '
    'insurance, rent, loan payment) rather than a one-off, '
    '"cadence": "monthly", "weekly", "quarterly", "annual" or "" if unclear, '
    '"confidence": "high", "medium" or "low", '
    '"reasoning": one sentence saying why}\n\n'
    "Rules:\n"
    "- A receipt, payment confirmation, or thank-you for money ALREADY PAID is NOT a "
    "bill, even though it names an amount.\n"
    "- Marketing, offers, price lists, shipping notices, and balance/statement-available "
    "notices with nothing actually owed are NOT bills.\n"
    "- An overdue notice or a final demand IS a bill.\n"
    "- NEVER guess or calculate an amount or a date that is not in the message. Leave the "
    "field \"\" instead. A wrong number here becomes a wrong reminder about real money.\n"
    "- Only convert a due date to YYYY-MM-DD when the message gives a real one (possibly "
    "relative to today's date, which you are told); otherwise leave due_date \"\" and put "
    "the wording in due_date_text."
)


def _parse_json_object(text: str) -> dict | None:
    """Same extraction as mail_triage._parse_json_object: models wrap JSON in prose or a
    fence even when told not to."""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        logger.warning("mail bills: could not parse LLM response as JSON")
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_amount(text: str | None) -> float | None:
    """A real number only when the model's amount string contains exactly one.

    "$80-$120", "between $80 and $120" and "varies" all return None: the text is kept
    verbatim in amount_text and the numeric column stays NULL rather than picking one end
    of a range and calling it the bill. Same reasoning meal_plan_shopping_items keeps
    model-reasoned quantities as TEXT.
    """
    if not text:
        return None
    numbers = re.findall(r"\d[\d,]*(?:\.\d+)?", str(text))
    if len(numbers) != 1:
        return None
    try:
        return float(numbers[0].replace(",", ""))
    except ValueError:
        return None


def _parse_due_date(value: str | None) -> str | None:
    """An ISO date only when the model actually gave one. Anything else ("next month",
    "Oct 1", an impossible 2026-13-45) is dropped rather than guessed at."""
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def reminder_due_at(due_date: str | None, today: date) -> str | None:
    """When to nudge about a bill due on `due_date`, or None for no nudge at all.

    REMINDER_LEAD_DAYS ahead of the due date, never ON it -- a reminder that arrives the
    morning something is due is not a warning. For a bill due sooner than that lead time,
    it clamps to today (which fires on the next reminder poll) rather than sliding forward
    onto the due date. A date that is today, already past, or implausibly far out gets no
    reminder; the bill row and its review card still record it.
    """
    parsed = _parse_due_date(due_date)
    if parsed is None:
        return None
    due = date.fromisoformat(parsed)
    if due <= today or (due - today).days > MAX_DUE_DATE_HORIZON_DAYS:
        return None
    remind_on = max(due - timedelta(days=REMINDER_LEAD_DAYS), today)
    return f"{remind_on.isoformat()}T{REMINDER_HOUR}"


def classify_bill(llm, message: dict, today: date | None = None) -> dict | None:
    """Asks the LLM whether `message` (shaped like MailClient.read_message's return) is a
    bill, and normalizes what comes back.

    Returns None for anything that isn't one -- including output that didn't parse, and a
    claimed bill carrying neither an amount nor any hint of a due date, which is nothing
    the owner could act on and not worth a review card. Silence is the safe failure mode:
    a missed bill is a bill he handles the way he always has, while a fabricated one is a
    false reminder about money.
    """
    today = today or date.today()
    prompt = (
        f"Today's date is {today.isoformat()}.\n\n"
        f"From: {message.get('from', '')}\n"
        f"Subject: {message.get('subject', '')}\n"
        f"Date: {message.get('date', '')}\n\n"
        f"{(message.get('body') or '')[:3000]}"
    )
    response = llm.chat(
        messages=[
            {"role": "system", "content": BILL_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        tools=None, think=False,
    )
    parsed = _parse_json_object((response or {}).get("content") or "")
    if not parsed or not parsed.get("is_bill"):
        return None

    amount_text = str(parsed.get("amount") or "").strip()
    due_date = _parse_due_date(parsed.get("due_date"))
    due_date_text = str(parsed.get("due_date_text") or "").strip()
    if not amount_text and not due_date and not due_date_text:
        return None

    return {
        "payee": str(parsed.get("payee") or "").strip(),
        "amount_text": amount_text,
        "amount": _parse_amount(amount_text),
        "due_date": due_date,
        "due_date_text": due_date_text,
        "is_recurring": bool(parsed.get("is_recurring")),
        "cadence": str(parsed.get("cadence") or "").strip(),
        "confidence": str(parsed.get("confidence") or "").strip(),
        "reasoning": str(parsed.get("reasoning") or "").strip(),
    }


def _review_detail(bill: dict, due_at: str | None) -> str:
    """What the owner reads on the review card. Says plainly what was and wasn't done --
    a card that looks like it filed something it didn't is worse than no card."""
    lines = [bill["reasoning"] or "Detected as a bill in an incoming email."]
    if bill["amount_text"] and bill["amount"] is None:
        lines.append(
            f"Amount as written: {bill['amount_text']} — not a single clear number, so it's "
            "stored as-is rather than guessed at."
        )
    if bill["due_date_text"] and not bill["due_date"]:
        lines.append(f"Timing as written: {bill['due_date_text']} — no exact date to schedule against.")
    if due_at:
        lines.append(f"Reminder set for {due_at[:10]}, ahead of the {bill['due_date']} due date.")
    elif bill["due_date"]:
        lines.append(f"No reminder set — the {bill['due_date']} due date isn't a plausible future date.")
    else:
        lines.append("No reminder set — no usable due date in the message.")
    if bill["is_recurring"]:
        cadence = f" ({bill['cadence']})" if bill["cadence"] else ""
        lines.append(
            f"Looks like a recurring bill{cadence}. It hasn't been added to your recurring "
            "charges — budget projections read that table, so that stays your call: add it "
            "with add_manual_recurring_charge if this is right."
        )
    return "\n\n".join(lines)


def run_mail_bill_scan_once(
    db_path: str, llm, mail_client, owner_user_id: int, folder: str = "INBOX",
    limit: int = 20, today: date | None = None,
) -> dict:
    """Scans the most recent messages in `folder` for bills, records what it finds, and
    nudges about real due dates.

    Safe to call repeatedly -- a scheduled tick and a manual run are the same call. Every
    side effect is guarded separately: a uid already judged is skipped before the LLM (so
    no duplicate rows and no repeat cost), create_bill is ON CONFLICT DO NOTHING, and a
    bill that already carries a reminder_id never gets a second reminder. A message that
    couldn't be read or that blew up mid-classification is left unmarked on purpose, so a
    transient IMAP or model failure means "try again next pass", not "this bill is
    invisible forever".

    Nothing here mutates the mailbox.
    """
    mail_db.init_mail_db(db_path)
    today = today or date.today()
    listing = mail_client.list_recent(folder=folder, limit=limit)
    scanned = detected = reminders = 0

    for header in listing.get("emails", []):
        uid = header["uid"]
        if mail_db.has_scanned_for_bill(db_path, owner_user_id, folder, uid):
            continue
        if mail_db.is_logged_junk(db_path, folder, uid):
            continue
        scanned += 1

        message = mail_client.read_message(uid, folder=folder)
        if message.get("error"):
            logger.warning("mail bills: could not read uid %s: %s", uid, message["error"])
            continue
        try:
            result = classify_bill(llm, message, today=today)
        except Exception:
            logger.exception("mail bills: classification failed for uid %s", uid)
            continue

        if result is None:
            mail_db.mark_bill_scanned(db_path, owner_user_id, folder, uid, is_bill=False)
            continue

        bill_id = mail_db.create_bill(
            db_path, owner_user_id, folder=folder, uid=uid,
            from_address=message.get("from", ""), subject=message.get("subject", ""),
            received_at=message.get("date", ""), payee=result["payee"],
            amount_text=result["amount_text"], amount=result["amount"],
            due_date=result["due_date"], due_date_text=result["due_date_text"],
            is_recurring=result["is_recurring"], cadence=result["cadence"],
            confidence=result["confidence"], reasoning=result["reasoning"],
        )
        mail_db.mark_bill_scanned(db_path, owner_user_id, folder, uid, is_bill=True)
        detected += 1

        # The nudge. Only for a bill that doesn't already have one -- reminder_id on the
        # row is the guard, not this pass's own bookkeeping, so it holds even if a row
        # somehow comes back around from an older scan.
        due_at = reminder_due_at(result["due_date"], today)
        existing = mail_db.get_bill(db_path, owner_user_id, bill_id)
        if due_at and existing is not None and existing["reminder_id"] is None:
            label = result["payee"] or message.get("subject") or "a bill"
            amount = f" — {result['amount_text']}" if result["amount_text"] else ""
            try:
                reminder_id = db.add_reminder(
                    db_path, owner_user_id,
                    f"Bill due {result['due_date']}: {label}{amount}",
                    due_at, scope="private",
                )
                mail_db.set_bill_reminder(db_path, bill_id, reminder_id)
                reminders += 1
            except Exception:
                logger.exception("mail bills: failed to create reminder for bill %s", bill_id)

        # Surfaces it in the existing /api/review/items queue, same as mail_triage's
        # drafts -- kind='other' already fits the review_items CHECK constraint, and
        # ref_table='email_bills' lands it in the personal lane (business_db
        # .classify_pipeline). A failure here must not lose the bill: the email_bills row
        # above is the source of truth whether or not this card gets filed.
        try:
            summary_bits = [
                result["amount_text"] or "amount not stated",
                f"due {result['due_date']}" if result["due_date"]
                else (result["due_date_text"] or "no due date stated"),
                f"from {message.get('from', '')}",
            ]
            business_db.create_review_item(
                db_path, owner_user_id,
                title=f"Bill: {result['payee'] or message.get('subject') or '(no subject)'}",
                kind="other", source_agent="mail_bills",
                summary=" — ".join(bit for bit in summary_bits if bit),
                detail=_review_detail(result, due_at),
                ref_table="email_bills", ref_id=bill_id,
            )
        except Exception:
            logger.exception("mail bills: failed to create review item for bill %s", bill_id)

    return {"scanned": scanned, "detected": detected, "reminders": reminders}
