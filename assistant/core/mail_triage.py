"""Draft-reply generation for incoming mail.

Stage 2 of the mail-handling work: decide which recent messages genuinely warrant a
human reply, ask the LLM to draft one, and store it for the owner to review and edit --
never send it. Sending stays exactly where it already was, behind mail_client.py's
send() and the existing pending_actions confirmation gate (see test_engine_mail.py);
nothing in this module calls it or anything like it.

No separate "is this junk" classifier lives here on purpose. A message not warranting a
reply (a receipt, a newsletter, a shipping notice) is simply not drafted -- this module
never files, labels, or otherwise acts on those, which is out of scope for this stage.
"""
import json
import logging
import re

from . import business_db, mail_db

logger = logging.getLogger(__name__)

# Senders a human reply is never warranted for, regardless of what the message says --
# these are automated systems with nobody reading a reply, so drafting one is pure noise
# in the review queue. Checked before the LLM ever sees the message, the same
# filter-at-the-source reasoning detector.py uses for its confidence thresholds.
AUTOMATED_SENDER_PATTERNS = (
    r"no-?reply@", r"do-?not-?reply@", r"notifications?@", r"mailer-daemon@",
    r"postmaster@", r"bounces?@", r"newsletter@", r"digest@", r"automated@",
)

TRIAGE_SYSTEM = (
    "You triage incoming email for a small maker/print business owner. For the message "
    "given, decide whether it genuinely warrants a personal reply from him (a customer "
    "question, an order issue, a vendor or collaboration inquiry, a personal message) as "
    "opposed to something that doesn't (a receipt, a shipping notification, a newsletter, "
    "an automated alert, spam, or something purely informational).\n\n"
    "Reply with ONLY a JSON object, no other text and no markdown fences, in this exact "
    "shape:\n"
    '{"needs_reply": true or false, "category": a short label, "reasoning": one sentence, '
    '"draft_subject": the reply subject line ("" if needs_reply is false), '
    '"draft_body": a short, polite, ready-to-edit draft reply written in his voice '
    '("" if needs_reply is false)}\n\n'
    "Keep the draft body concise (under 150 words), never invent facts (prices, dates, "
    "order status, availability) you were not given, and leave a bracketed placeholder "
    "like [confirm turnaround time] for anything you cannot know from the message itself."
)


def _looks_automated(from_address: str) -> bool:
    addr = (from_address or "").lower()
    return any(re.search(p, addr) for p in AUTOMATED_SENDER_PATTERNS)


def _parse_json_object(text: str) -> dict | None:
    """Models reliably wrap JSON in prose or a code fence even when told not to --
    pull out the first {...} block rather than trusting content to be bare JSON."""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        logger.warning("mail triage: could not parse LLM response as JSON")
        return None
    return parsed if isinstance(parsed, dict) else None


def classify_and_draft(llm, message: dict) -> dict | None:
    """Asks the LLM whether `message` (a dict shaped like MailClient.read_message's
    return value) warrants a reply, and if so, drafts one.

    Returns None for anything that doesn't need one, including a response the model
    didn't answer in the expected shape -- silence is the safe failure mode here, never
    a fabricated draft.
    """
    if _looks_automated(message.get("from", "")):
        return None

    prompt = (
        f"From: {message.get('from', '')}\n"
        f"Subject: {message.get('subject', '')}\n"
        f"Date: {message.get('date', '')}\n\n"
        f"{(message.get('body') or '')[:3000]}"
    )
    response = llm.chat(
        messages=[
            {"role": "system", "content": TRIAGE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        tools=None, think=False,
    )
    parsed = _parse_json_object((response or {}).get("content") or "")
    if not parsed or not parsed.get("needs_reply"):
        return None
    if not (parsed.get("draft_subject") and parsed.get("draft_body")):
        return None
    return parsed


def run_mail_triage_once(
    db_path: str, llm, mail_client, owner_user_id: int, folder: str = "INBOX", limit: int = 15,
) -> dict:
    """Scans the most recent messages in `folder`, drafts replies for the ones that
    need one, and stores them for review.

    Safe to call repeatedly (a scheduled tick or an on-demand web trigger both go
    through this): a uid that already has a draft is skipped outright, so re-running
    never re-asks the LLM about the same message or duplicates a review card. Nothing
    here ever sends anything.
    """
    mail_db.init_mail_db(db_path)
    listing = mail_client.list_recent(folder=folder, limit=limit)
    scanned = drafted = 0
    for header in listing.get("emails", []):
        uid = header["uid"]
        if mail_db.has_draft(db_path, owner_user_id, folder, uid):
            continue
        scanned += 1
        message = mail_client.read_message(uid, folder=folder)
        if message.get("error"):
            logger.warning("mail triage: could not read uid %s: %s", uid, message["error"])
            continue
        try:
            result = classify_and_draft(llm, message)
        except Exception:
            logger.exception("mail triage: classification failed for uid %s", uid)
            continue
        if result is None:
            continue

        draft_id = mail_db.create_draft(
            db_path, owner_user_id, folder=folder, uid=uid,
            from_address=message.get("from", ""), subject=message.get("subject", ""),
            received_at=message.get("date", ""), category=result.get("category", ""),
            reasoning=result.get("reasoning", ""),
            draft_subject=result["draft_subject"], draft_body=result["draft_body"],
        )
        drafted += 1

        # Surfaces the draft in the existing /api/review/items queue -- kind='other'
        # already fits the review_items CHECK constraint, so this needs no schema change
        # there. A failure here must not lose the draft itself; the email_drafts row
        # above is the source of truth regardless of whether this card gets filed.
        try:
            business_db.create_review_item(
                db_path, owner_user_id,
                title=f"Reply: {message.get('subject') or '(no subject)'}",
                kind="other", source_agent="mail_triage",
                summary=f"From {message.get('from', '')} — {result.get('category', '')}".strip(" —"),
                detail=result["draft_body"], ref_table="email_drafts", ref_id=draft_id,
            )
        except Exception:
            logger.exception("mail triage: failed to create review item for draft %s", draft_id)

    return {"scanned": scanned, "drafted": drafted}
