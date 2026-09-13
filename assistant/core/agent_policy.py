"""The owner's own written engineering policy, put in front of the employees it governs.

He has four policy notes in the vault, written over several weeks, and until this existed
not one byte of them reached an engineer's prompt:

    03-Areas/Jarvis Engineering Policy
    03-Areas/Dev Pipeline Policy
    00-About Me/Engineering Team - Standing Operating Rules
    00-About Me/Working Standards - Jarvis Conduct

They are not vague aspirations. They say which employee backend work routes to, that a PR
must go to the tester before it reaches him, and that an agent which times out is a bug to
root-cause rather than a fact to report. Employees were being hired, briefed and run
without ever seeing any of it, and then judged against it.

This module is **pure retrieval**. No new store, no new table, no new tool, and nothing
here ever writes to the vault -- it reads four notes the owner maintains by hand and
splices them into the prompt as text. That shape is not a simplification, it is the only
one available: scheduled staff are one-shot `llm.research()` completions with no mid-run
tool loop, which is the same reason `staff.build_feed_briefing` pre-fetches market data
rather than offering a market tool. Policy arrives the same way, as one more text block.

Two rules carried over verbatim from the parts of this codebase that already work:

  * **Bounded by construction.** The whole block is capped (POLICY_CONTEXT_CHARS, the same
    2,000 as crypto_journal.PRIOR_CONTEXT_CHARS) and the budget is shared out across the
    notes, so the prompt cannot grow just because he added a paragraph to a note. This is
    injected on every single run of every engineering employee.
  * **His words outrank the model's.** Stated explicitly in the block's own header, the
    same way mail_importance.format_examples tells the model his past rulings outrank the
    general rules above them. A policy the model treats as a suggestion is not a policy.

A read failure degrades to a stated absence, never an exception. An employee that cannot
see the policy should still do the work; it just needs to know it is working blind, which
is why the "could not be read" case says so out loud rather than silently rendering less.
"""
import logging

logger = logging.getLogger(__name__)

# (folder, title) of every note that carries standing engineering policy. Explicit, not
# discovered by scanning a folder: 03-Areas and 00-About Me are full of the owner's
# personal notes (health, finance, a rental dispute), and a "read everything in the folder"
# rule would put his landlord dispute in front of a React engineer.
POLICY_NOTES = (
    ("03-Areas", "Jarvis Engineering Policy"),
    ("03-Areas", "Dev Pipeline Policy"),
    ("00-About Me", "Engineering Team - Standing Operating Rules"),
    ("00-About Me", "Working Standards - Jarvis Conduct"),
)

# Same cap and same reasoning as crypto_journal.PRIOR_CONTEXT_CHARS. The four notes total
# roughly 6,200 characters today, so this keeps about a third of each -- raise this one
# constant if he would rather spend the prompt budget on more of it.
POLICY_CONTEXT_CHARS = 2000
MIN_USEFUL_SHARE = 120          # below this a note's excerpt says nothing; skip it instead


def _strip_frontmatter(text: str, title: str = "") -> str:
    """Same helper shape as crypto_journal._strip_frontmatter. YAML frontmatter is pure
    overhead here -- tags and timestamps spend the character budget without telling the
    employee anything about the policy.

    The note's own `# Title` H1 goes too when it just repeats the heading this module
    already prints. Every note written by write_note opens with one, so leaving it in
    rendered as "## Dev Pipeline Policy" immediately followed by "# Dev Pipeline Policy"
    -- confusing to read and paid for out of a budget that is already tight.
    """
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            text = text[end + 4:]
    text = text.strip()
    if title:
        heading = f"# {title}"
        if text.startswith(heading):
            text = text[len(heading):].lstrip()
    return text


def _head(text: str, limit: int) -> str:
    """The START of a policy note, unlike crypto_journal._tail's end-of-note.

    Deliberately opposite, because the two note shapes are opposite. A journal is a
    timeline where the newest entry is the one that matters. A policy note accumulates
    standing rules that all still apply, and its opening sections are the foundational
    ones -- truncating from the front would cut the rule and keep the postscript.

    The truncation is announced rather than silent: an employee that can see it is reading
    an excerpt can say "the policy note continues past what I was shown" instead of
    confidently asserting that a rule does not exist.
    """
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...(this policy note continues past what you were shown)"


def _allocate(sizes: list[int], budget: int) -> list[int]:
    """Share `budget` across notes so a short note releases what it doesn't need.

    Water-filling rather than a flat budget/len split: three short notes and one long one
    should not each be cut to a quarter while most of the budget goes unspent. Returns
    allocations in the original order.
    """
    order = sorted(range(len(sizes)), key=lambda i: sizes[i])
    out = [0] * len(sizes)
    remaining, left = budget, len(sizes)
    for i in order:
        share = remaining // max(left, 1)
        take = min(sizes[i], share)
        out[i] = take
        remaining -= take
        left -= 1
    return out


def build_policy_briefing(obsidian, max_chars: int = POLICY_CONTEXT_CHARS) -> str:
    """The owner's standing engineering policy, pre-fetched into this run's prompt.

    Mirrors staff.build_feed_briefing and crypto_journal.build_prior_context exactly: the
    orchestrator reads, the employee just receives text. Returns "" when there is no vault
    client, so an employee holding the feed on a deployment without a vault runs normally.
    """
    if obsidian is None:
        return ""

    bodies, unreadable = [], []
    for folder, title in POLICY_NOTES:
        try:
            note = obsidian.read_note(folder, title)
        except Exception:
            logger.exception("could not read the policy note %s/%s", folder, title)
            unreadable.append(title)
            continue
        if not isinstance(note, dict) or note.get("error"):
            # A missing note is worth saying out loud: these are hand-maintained, and a
            # rename in Obsidian would otherwise silently drop a policy nobody notices is
            # gone until an employee breaks the rule it contained.
            logger.warning("policy note not found in the vault: %s/%s", folder, title)
            unreadable.append(title)
            continue
        body = _strip_frontmatter(note.get("content") or "", title)
        if body:
            bodies.append((title, body))

    if not bodies:
        if unreadable:
            return ("\n\n--- STANDING POLICY ---\nThe owner's policy notes could not be "
                    "read from the vault this run (" + ", ".join(unreadable) + "). Work to "
                    "your own best judgement, and say in your report that you could not "
                    "see the standing policy.\n--- end standing policy ---\n")
        return ""

    shares = _allocate([len(b) for _, b in bodies], max_chars)
    parts = [
        "\n\n--- STANDING POLICY, written by the owner himself ---",
        "These are his own notes on how this team works. They are not suggestions and they "
        "outrank your own defaults and anything in your job description that disagrees with "
        "them. Where a rule below decides the question, follow it rather than re-deciding it.",
    ]
    for (title, body), share in zip(bodies, shares):
        # Skip only a note whose excerpt would be a meaningless fragment. A note that fits
        # whole within its share is always included however short it is -- the minimum is
        # about truncation being useless, not about brevity, and conflating the two
        # silently dropped every short policy note.
        if share < len(body) and share < MIN_USEFUL_SHARE:
            continue
        parts.append(f"\n## {title}\n{_head(body, share)}")
    if unreadable:
        parts.append("\nNote: these policy notes could not be read this run: "
                     + ", ".join(unreadable) + ".")
    parts.append("--- end standing policy ---\n")
    return "\n".join(parts)


def policy_employees(db_path: str) -> list[dict]:
    """Active employees granted the `policy` feed. Explicit, never inferred -- same rule as
    `paper` and `journal`, though for a gentler reason: this one grants no power and writes
    nothing, so the risk of a wrong grant is wasted prompt budget rather than an employee
    quietly picking up a capability from how its job description happened to be worded."""
    from . import staff
    return [e for e in staff.list_staff(db_path)
            if "policy" in (e.get("data_feeds") or "") and e["status"] == "active"]
