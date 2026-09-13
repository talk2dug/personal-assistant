"""The creative pipeline's feedback loop: the owner's own verdicts, fed back to the agent
that earned them.

`review_items` holds 84 decided cards carrying real verdicts -- product_creator 6 approved
/ 10 rejected, store_manager 1/4, art_director 3/2 -- and `decision_note` was read by
nothing at all. One of those notes says, in his own words:

    "I already have this created. No need to make it again"

Meanwhile the product creator opens every run knowing only a list of titles not to repeat.
It was told *what* it had already proposed and never once *what he thought of it*. So it
keeps proposing the same kind of thing, and he keeps rejecting it, and neither side moves.

THE SELECTION STRATEGY IS NOT NEW, AND DELIBERATELY SO
------------------------------------------------------
This mirrors `mail_importance.select_examples` / `format_examples` point for point,
against `review_items` filtered by `source_agent` instead of the mail tables. That module
is the one place in this system where a real learning loop already works, its strategy was
reasoned out at length, and reinventing it here would mean making the same mistakes again
on a different table. Each property is carried over for the same reason it was chosen
there:

  * RECENCY. Most recent verdicts first. What he wants from the shop is a fact about his
    business right now -- a season, a local event, a product line he has just stopped
    making -- not a permanent truth.

  * BALANCE, WITH AN ASYMMETRIC BACKFILL. Up to MAX_EXAMPLES_PER_LABEL approvals and as
    many rejections. Spare approval slots are backfilled with MORE REJECTIONS, never the
    reverse. The asymmetry is the point, and it lands even harder here than it does in
    mail: a prompt weighted toward "approved" teaches the model that its last batch was
    good and to produce more of it, which is precisely the failure the live numbers show
    (product_creator is already rejected almost 2:1). Extra rejections can only make it
    more selective, which is the direction it should fail in.

  * MIXED, NOT GROUPED. Re-sorted into one recency-ordered list rather than an all-yes
    block followed by an all-no block, so the model reads them as a standard to calibrate
    against rather than a list with a trailing bias.

  * BOUNDED. Hard cap, every field truncated. The prompt cannot grow with the number of
    decisions -- and these agents run unattended on a schedule, so an unbounded prompt is
    a real cost rather than a theoretical one.

  * HIS WORDS OUTRANK THE MODEL'S. Each example carries the note he left, and the block
    says outright that his notes beat the general instructions above them. "I already have
    this created" is worth more than any number of approved/rejected booleans, and the
    note is the only channel through which he can state a rule in his own language.

Everything here is READ-ONLY. It selects and formats; it writes nothing.
"""
import logging

from . import business_db

logger = logging.getLogger(__name__)

# Same bounds as mail_importance's, and for the same reasons -- see the module docstring.
MAX_FEWSHOT_EXAMPLES = 8
MAX_EXAMPLES_PER_LABEL = 4
EXAMPLE_TITLE_CHARS = 120
EXAMPLE_SUMMARY_CHARS = 120
EXAMPLE_NOTE_CHARS = 200

NO_EXAMPLES_NOTE = (
    "The owner has not ruled on anything from you yet, so there is nothing to calibrate "
    "against. Propose what you genuinely think is best and expect to be corrected."
)


def _truncate(text: str | None, limit: int) -> str:
    value = (text or "").strip().replace("\n", " ")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def select_examples(
    db_path: str, owner_user_id: int, source_agent: str,
    max_total: int = MAX_FEWSHOT_EXAMPLES, per_label: int = MAX_EXAMPLES_PER_LABEL,
) -> list[dict]:
    """The owner's past verdicts on this agent's own work.

    Recent-first, balanced up to `per_label` each way, spare slots backfilled with
    REJECTIONS only, capped at `max_total`, then merged into one recency-ordered list.
    Identical in shape to mail_importance.select_examples; see the module docstring for
    why each property is the way it is.

    Scoped by `source_agent` so each agent is calibrated only against rulings on its own
    output. A rejected sticker concept says nothing about a social caption, and mixing
    them would teach every agent the other's lesson.
    """
    approved = business_db.list_verdict_examples(
        db_path, owner_user_id, source_agent, status="approved", limit=per_label)
    # The only asymmetry: rejections may take the slots approvals didn't fill, never the
    # other way round. A prompt tilted toward "approved" teaches this thing to repeat itself.
    rejected_room = max(per_label, max_total - len(approved))
    rejected = business_db.list_verdict_examples(
        db_path, owner_user_id, source_agent, status="rejected", limit=rejected_room)

    merged = (approved + rejected)[:max_total]
    merged.sort(key=lambda row: (row.get("decided_at") or "", row.get("id") or 0), reverse=True)
    return merged


def format_examples(examples: list[dict]) -> str:
    """Render selected verdicts as the prompt block.

    Every field is truncated, so this text is bounded by construction no matter how many
    decisions accumulate.
    """
    if not examples:
        return NO_EXAMPLES_NOTE

    lines = [
        "The owner has already ruled on the work below — these are his actual decisions on "
        "your own past output, most recent first. They outrank the general instructions "
        "above wherever they disagree. Calibrate to them: do not re-propose something he "
        "rejected, or a close variation of it, unless you can say what is different this "
        "time.",
        "",
    ]
    for example in examples:
        verdict = "APPROVED" if example.get("status") == "approved" else "REJECTED"
        title = _truncate(example.get("title"), EXAMPLE_TITLE_CHARS) or "(untitled)"
        summary = _truncate(example.get("summary"), EXAMPLE_SUMMARY_CHARS)
        lines.append(f"- [{verdict}] {title}" + (f" | {summary}" if summary else ""))
        note = _truncate(example.get("decision_note"), EXAMPLE_NOTE_CHARS)
        if note:
            # His actual words. The single most valuable line in the whole block -- see
            # the module docstring's "HIS WORDS OUTRANK THE MODEL'S".
            lines.append(f"    He said: {note}")
    return "\n".join(lines)


def build_verdict_briefing(db_path: str, owner_user_id: int, source_agent: str) -> str:
    """The whole block, ready to splice into an agent's prompt. Never raises.

    A failure to read past verdicts must not stop the agent producing work -- it just
    produces it uncalibrated, which is exactly the state it was in before any of this
    existed, so failing closed here would trade a working pipeline for a better one that
    sometimes doesn't run.
    """
    try:
        examples = select_examples(db_path, owner_user_id, source_agent)
    except Exception:
        logger.exception("could not read past verdicts for %s", source_agent)
        return ""
    return ("\n\n--- THE OWNER'S PAST DECISIONS ON YOUR WORK ---\n"
            + format_examples(examples)
            + "\n--- end past decisions ---\n")
