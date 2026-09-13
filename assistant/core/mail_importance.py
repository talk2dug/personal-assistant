"""Provisional importance flagging for incoming mail -- and the feedback loop that is
actually the point of it.

Project 13's remaining half: "flags important emails touching personal finances,
relationships, or personal business". Asked where the line is on "important", the owner
deliberately refused to draw one:

    "if thres a way to flag something as important and let the AI start to learn. Or if
    it can tempo flag something as important and then i can say if it was or was not and
    continue learning"

So this is NOT a hand-written importance rule, and it is not a fixed classifier either.
It is a loop with three steps, and the middle one is his:

    1. a scheduled pass tentatively flags a message and says WHY (run_mail_importance_scan_once)
    2. he answers "yes it was" / "no it wasn't" on the review card, optionally in his own
       words (mail_db.record_importance_verdict, via the Review page or the chat tool)
    3. the next run's prompt carries his real past verdicts, so the model calibrates to
       HIS judgement instead of a generic notion of important (select_examples below)

Structurally this is mail_bills.py with a different question: scan recent non-junk mail,
one targeted LLM question per message, own table, own scan ledger so each message costs
exactly one round trip ever, a review_items card per hit, idempotent across re-runs.
Everything mail_bills.py says about safety holds here verbatim and is load-bearing: this
module READS and RECORDS only. No code path in it can archive, delete, move, mark read,
or send, which is what makes it safe to run unattended against the real mailbox. It does
not even create a reminder -- a flag is a question, not a task.

WHY FEW-SHOT, AND HOW EXAMPLES ARE SELECTED
-------------------------------------------
There is no fine-tuning anywhere in this system, so "continue learning" has exactly one
honest mechanism: put his own past verdicts in the prompt. The selection strategy, which
is the whole design, is:

  * RECENCY. Most recent verdicts first. What counts as important to him is a fact about
    his life right now (a house sale, a sick relative, a lapsed policy), not a permanent
    truth, so an answer from six months ago should lose to one from last week.

  * BALANCE, with a deliberately asymmetric backfill. Up to MAX_EXAMPLES_PER_LABEL (4)
    confirmed-important and 4 confirmed-not-important. If he has fewer than 4 positives,
    the spare slots are backfilled with MORE NEGATIVES, never the reverse, up to
    MAX_FEWSHOT_EXAMPLES (8) total. The asymmetry is the point: a prompt that is mostly
    "yes, important" teaches the model to say yes, and a classifier that flags everything
    is worse than no classifier at all (he will stop reading the cards, and then the
    feedback loop dies with it). Extra negatives can only ever make it more conservative,
    which is the direction we want to fail in.

  * MIXED, NOT GROUPED. The selected examples are re-sorted into one recency-ordered
    list rather than rendered as an all-yes block followed by an all-no block, so the
    model reads them as a judgement to calibrate against rather than a list with a
    trailing bias.

  * BOUNDED. Hard cap of 8 examples, each truncated (subject, sender, his note), so the
    prompt cannot grow with the training set -- this runs on the local Ollama backend
    too, where an unbounded prompt is a real failure and not just a cost.

  * HIS WORDS OUTRANK THE MODEL'S. Each example carries the note he left on the decision
    when there is one. "not important, this account is closed" is worth more than any
    number of confirmed/rejected booleans, and it is the only channel in the whole
    feature through which he can state a rule in his own language.

The examples are selected ONCE per run, before the first message is classified, so a
single pass judges every message by the same standard. A verdict recorded while a run is
in flight shows up on the next run, not halfway through this one.

NOT OVER-FLAGGING
-----------------
Two independent brakes, because "the assistant that cried wolf" is the specific failure
that would kill this feature:

  * a confidence threshold (config: mail_importance_confidence_threshold, default 0.75,
    deliberately conservative) that the model's own 0-1 self-rating must clear. An
    unparseable confidence never clears it -- unreadable certainty fails closed.
  * a per-run cap (config: mail_importance_max_flags_per_run, default 3). Messages past
    the cap are left unjudged, NOT dropped: they carry no ledger row, so the next tick
    picks them up. The worst case is a flag arriving a few hours late, never one lost.

Sub-threshold judgements are still written to the scan ledger with their category and
confidence, so the near-misses exist as real evidence when the threshold needs tuning.
"""
import json
import logging
import re

from . import business_db, mail_db

logger = logging.getLogger(__name__)

# The model's own 0-1 confidence must reach this before anything is surfaced. Starts
# high on purpose: the first weeks decide whether he keeps reading these cards, and an
# empty queue is recoverable in a way "I stopped looking at those" is not. Overridable
# per-deployment (config.py's mail_importance_confidence_threshold), same precedent as
# junk_filter's mail_junk_score_threshold.
DEFAULT_CONFIDENCE_THRESHOLD = 0.75
# At most this many review cards per pass (config: mail_importance_max_flags_per_run).
DEFAULT_MAX_FLAGS_PER_RUN = 3

# Few-shot bounds -- see the module docstring's selection strategy.
MAX_FEWSHOT_EXAMPLES = 8
MAX_EXAMPLES_PER_LABEL = 4
EXAMPLE_SUBJECT_CHARS = 120
EXAMPLE_SENDER_CHARS = 80
EXAMPLE_NOTE_CHARS = 200

# The three the owner's own project goal names, plus a catch-all so the model isn't
# forced to mislabel something real (e.g. a health matter) into a category it doesn't fit.
CATEGORIES = ("personal_finances", "relationships", "personal_business", "other")
CATEGORY_LABELS = {
    "personal_finances": "personal finances",
    "relationships": "relationships",
    "personal_business": "personal business",
    "other": "other",
}

IMPORTANCE_SYSTEM = (
    "You are triaging one email for Jack, and deciding whether it is IMPORTANT TO HIM "
    "PERSONALLY -- specifically whether it touches his personal finances, his "
    "relationships with real people, or his personal business/admin (anything he has to "
    "handle himself: an account, a policy, a document, an appointment, a legal or "
    "official matter).\n\n"
    "Reply with ONLY a JSON object, no other text and no markdown fences, in this exact "
    "shape:\n"
    '{"important": true or false, '
    '"category": one of "personal_finances", "relationships", "personal_business", "other", '
    '"confidence": a number between 0 and 1 for how sure you are, '
    '"reason": one short sentence, addressed to Jack, saying why this mattered}\n\n'
    "Rules:\n"
    "- Marketing, newsletters, promotions, social-media notifications, shipping updates, "
    "receipts for routine purchases and automated 'your statement is ready' notices are "
    "NOT important, however urgent their wording pretends to be.\n"
    "- A real person writing to him personally about something in his life IS important. "
    "A mass mail that merely uses his first name is not.\n"
    "- Something with a real consequence he would care about missing -- money moving, a "
    "deadline, a document needing a signature, an account problem, a person needing a "
    "reply -- IS important.\n"
    "- Be conservative. Flagging mail that turns out not to matter costs him attention "
    "and trains him to ignore these; when genuinely unsure, say so with a LOW confidence "
    "rather than inflating it.\n"
    "- confidence must be a plain number between 0 and 1 (e.g. 0.82). Never a percentage, "
    "a word, or a range.\n"
    "- The reason is read back to him verbatim, so write it as one plain sentence he "
    "could agree or disagree with -- not a description of your process."
)

NO_EXAMPLES_NOTE = (
    "You have no past rulings from Jack yet, so judge this on the rules above and keep "
    "your confidence honest -- his answers will calibrate you from here on."
)


def _parse_json_object(text: str) -> dict | None:
    """Same extraction as mail_bills/mail_triage: models wrap JSON in prose or a fence
    even when told not to."""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        logger.warning("mail importance: could not parse LLM response as JSON")
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_confidence(value) -> float | None:
    """A real 0-1 confidence, or None.

    Strict on purpose, and strictness IS the safety here. "high", "95", "0.8-0.9" and
    "very sure" all return None, and a None confidence can never clear the threshold, so
    a model that stops answering the question the way it was asked goes quiet rather than
    flagging everything at some invented certainty. Same refusal-to-coerce as
    mail_bills._parse_amount: the raw judgement is still recorded either way.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0.0 or number > 1.0:  # NaN, or outside 0-1
        return None
    return number


def _truncate(text: str | None, limit: int) -> str:
    value = (text or "").strip().replace("\n", " ")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def select_examples(
    db_path: str, owner_user_id: int,
    max_total: int = MAX_FEWSHOT_EXAMPLES, per_label: int = MAX_EXAMPLES_PER_LABEL,
) -> list[dict]:
    """The owner's past verdicts to calibrate the next classification against.

    Recent-first, balanced up to `per_label` each way, spare slots backfilled with
    NEGATIVES only, capped at `max_total`, then merged into one recency-ordered list.
    See the module docstring for why each of those is the way it is.
    """
    positives = mail_db.list_importance_examples(db_path, owner_user_id, label=True, limit=per_label)
    # The only asymmetry: negatives may take the slots positives didn't fill, never the
    # other way round. A prompt tilted toward "yes" teaches this thing to nag.
    negative_room = max(per_label, max_total - len(positives))
    negatives = mail_db.list_importance_examples(db_path, owner_user_id, label=False, limit=negative_room)

    merged = (positives + negatives)[:max_total]
    merged.sort(key=lambda row: (row.get("created_at") or "", row.get("id") or 0), reverse=True)
    return merged


def format_examples(examples: list[dict]) -> str:
    """Renders selected verdicts as the prompt block. Every field is truncated: this text
    is bounded by construction, no matter how large the training set grows."""
    if not examples:
        return NO_EXAMPLES_NOTE

    lines = [
        "Jack has already ruled on the emails below. These are his actual judgements, "
        "most recent first -- they outrank the general rules above wherever they "
        "disagree. Calibrate to them.",
        "",
    ]
    for example in examples:
        verdict = "IMPORTANT" if example.get("label") else "NOT important"
        lines.append(
            f"- [{verdict}] from {_truncate(example.get('from_address'), EXAMPLE_SENDER_CHARS)} "
            f"| subject: {_truncate(example.get('subject'), EXAMPLE_SUBJECT_CHARS) or '(no subject)'}"
        )
        note = _truncate(example.get("note"), EXAMPLE_NOTE_CHARS)
        if note:
            lines.append(f"    Jack said: {note}")
    return "\n".join(lines)


def classify_importance(llm, message: dict, examples: list[dict] | None = None) -> dict | None:
    """Asks the LLM whether `message` (shaped like MailClient.read_message's return) is
    important to the owner, calibrated by `examples` (his past verdicts).

    Returns None for anything that isn't important and for output that didn't parse --
    silence is the safe failure mode, exactly as in mail_bills.classify_bill. A claimed
    hit with no reason is also dropped: the reason IS the card ("I flagged this because
    X -- was it?"), and a flag he can't evaluate is a question he can't answer.

    A confidence that didn't parse comes back as None rather than a guess; the caller's
    threshold check then declines to surface it. The judgement is still returned so the
    scan ledger can record the near-miss.
    """
    prompt = (
        f"{format_examples(examples or [])}\n\n"
        "--- The email to judge now ---\n"
        f"From: {message.get('from', '')}\n"
        f"Subject: {message.get('subject', '')}\n"
        f"Date: {message.get('date', '')}\n\n"
        f"{(message.get('body') or '')[:3000]}"
    )
    response = llm.chat(
        messages=[
            {"role": "system", "content": IMPORTANCE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        tools=None, think=False,
    )
    parsed = _parse_json_object((response or {}).get("content") or "")
    if not parsed or not parsed.get("important"):
        return None

    reason = str(parsed.get("reason") or "").strip()
    if not reason:
        return None

    category = str(parsed.get("category") or "").strip().lower().replace(" ", "_")
    if category not in CATEGORIES:
        category = "other"

    return {
        "category": category,
        "confidence": _parse_confidence(parsed.get("confidence")),
        "reason": reason,
    }


def _review_detail(result: dict, threshold: float, stats: dict, example_count: int) -> str:
    """What he reads on the card. It has to make the ask unmistakable -- this card exists
    to collect an answer, not to inform him -- and it has to say plainly that nothing was
    done to the message, or "flagged" will read as "filed somewhere"."""
    confidence = result["confidence"]
    lines = [
        f"I flagged this as important because: {result['reason']}",
        "Was it? Approve if it mattered, reject if it didn't — either answer teaches me, "
        "and anything you type in the note teaches me more than the yes/no does.",
        f"Category: {CATEGORY_LABELS.get(result['category'], result['category'])}. "
        f"Confidence {confidence:.2f} (I only raise these above {threshold:.2f}).",
    ]
    if example_count:
        lines.append(
            f"Calibrated against your last {example_count} ruling"
            f"{'s' if example_count != 1 else ''} "
            f"({stats['examples_positive']} important, {stats['examples_negative']} not, overall)."
        )
    else:
        lines.append("This is one of my first guesses — I have none of your rulings to go on yet.")
    lines.append(
        "Nothing has been done to the message itself: it hasn't been read, moved, "
        "archived or replied to, and it won't be. This is only about what I learn."
    )
    return "\n\n".join(lines)


def run_mail_importance_scan_once(
    db_path: str, llm, mail_client, owner_user_id: int, folder: str = "INBOX",
    limit: int = 20, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    max_flags: int = DEFAULT_MAX_FLAGS_PER_RUN,
) -> dict:
    """Scans the most recent messages in `folder`, provisionally flags the ones that look
    important to him, and asks him about each one through the review queue.

    Safe to call repeatedly -- a scheduled tick and a manual run are the same call. A uid
    already judged is skipped before the LLM, create_importance_flag is ON CONFLICT DO
    NOTHING, and a message that couldn't be read or that blew up mid-classification is
    deliberately left unmarked so the next pass retries it.

    Nothing here mutates the mailbox.
    """
    mail_db.init_mail_db(db_path)
    examples = select_examples(db_path, owner_user_id)
    stats = mail_db.importance_stats(db_path, owner_user_id)
    listing = mail_client.list_recent(folder=folder, limit=limit)

    scanned = flagged = below_threshold = 0
    capped = False

    for header in listing.get("emails", []):
        uid = header["uid"]
        if mail_db.has_scanned_for_importance(db_path, owner_user_id, folder, uid):
            continue
        # A message the junk scan already flagged is not a candidate for "important",
        # even if its move into Junk failed -- same guard mail_bills uses, and for the
        # same reason: urgent-sounding fraud is exactly what scores as junk AND reads as
        # something that matters.
        if mail_db.is_logged_junk(db_path, folder, uid):
            continue
        if flagged >= max_flags:
            # Deliberately left unjudged rather than force-marked: no ledger row means
            # the next tick picks it up. A late flag is fine; a lost one is not.
            capped = True
            break
        scanned += 1

        message = mail_client.read_message(uid, folder=folder)
        if message.get("error"):
            logger.warning("mail importance: could not read uid %s: %s", uid, message["error"])
            continue
        try:
            result = classify_importance(llm, message, examples=examples)
        except Exception:
            logger.exception("mail importance: classification failed for uid %s", uid)
            continue

        if result is None:
            mail_db.mark_importance_scanned(db_path, owner_user_id, folder, uid, flagged=False)
            continue

        confidence = result["confidence"]
        if confidence is None or confidence < threshold:
            # Judged, recorded, not surfaced. The category/confidence are kept so the
            # near-misses are real evidence if the threshold turns out to be too high.
            mail_db.mark_importance_scanned(
                db_path, owner_user_id, folder, uid, flagged=False,
                category=result["category"], confidence=confidence,
            )
            below_threshold += 1
            continue

        flag_id = mail_db.create_importance_flag(
            db_path, owner_user_id, folder=folder, uid=uid,
            from_address=message.get("from", ""), subject=message.get("subject", ""),
            received_at=message.get("date", ""), category=result["category"],
            confidence=confidence, reason=result["reason"],
        )
        mail_db.mark_importance_scanned(
            db_path, owner_user_id, folder, uid, flagged=True,
            category=result["category"], confidence=confidence,
        )
        flagged += 1

        # The ask itself. A failure filing the card must not lose the flag: the
        # email_importance_flags row above is the source of truth either way.
        try:
            business_db.create_review_item(
                db_path, owner_user_id,
                title=f"Important? {message.get('subject') or '(no subject)'}",
                kind="other", source_agent="mail_importance",
                summary=(
                    f"{CATEGORY_LABELS.get(result['category'], result['category'])} — "
                    f"from {message.get('from', '')}"
                ),
                detail=_review_detail(result, threshold, stats, len(examples)),
                ref_table="email_importance_flags", ref_id=flag_id,
            )
        except Exception:
            logger.exception("mail importance: failed to create review item for flag %s", flag_id)

    return {
        "scanned": scanned, "flagged": flagged, "below_threshold": below_threshold,
        "examples_used": len(examples), "capped": capped,
    }
