"""Agent-written material in the owner's Obsidian vault: research briefs and, later,
design documents and employee journals.

Two decisions from the owner shape everything here, and neither is inferred:

  * **Scope is briefs and design documents, not a note per commit.** His standing "
    everything goes to Obsidian" instruction does not mean narrating shipped code into
    the vault -- git already records that, in more detail and with more accuracy than a
    prose summary would. What belongs here is the written deliverable: a research brief, a
    design document, a decision and the reasoning behind it.
  * **Agent-written notes live in their own folder** (obsidian_client.AGENT_FOLDER),
    never mixed into 01-Research/02-Projects/03-Areas where his own hand-written notes
    are. He should never have to work out whether he wrote a note or a machine did.

The mechanism is the one crypto_journal.py established and proved: **code writes to the
vault, the model never calls the tool itself.** The agents whose output lands here are
one-shot `llm.research()` completions with no tool loop -- the same reason
staff.build_feed_briefing pre-fetches market data as text. So the orchestrator takes what
came back and files it.

And the rule that makes any of this worth doing, from docs/crypto-journal-design.md:
**anything written must be read back into a later prompt, or it is a diary, not
learning.** A research brief is the one honest exception, because its reader is the
owner rather than a later model -- it is a deliverable, and the vault is where he keeps
deliverables. Everything else in this module exists to be read back.

Every write here is best-effort. A vault on a disconnected drive must cost a brief its
note, never its findings -- those are already committed to the database by the time any
of this is reached.
"""
import logging
from datetime import datetime, timezone

from .obsidian_client import AGENT_FOLDER

logger = logging.getLogger(__name__)

# A brief is a deliverable the owner reads, not a block injected into a prompt, so this
# is generous where crypto_journal.PRIOR_CONTEXT_CHARS is tight. It exists only to stop a
# pathological, runaway response from becoming a megabyte of markdown.
BRIEF_BODY_CHARS = 20000
TITLE_CHARS = 90


def _day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def brief_title(topic: str, now: datetime | None = None) -> str:
    """One note per brief, dated first so the folder sorts chronologically.

    Deliberately flat rather than subfoldered: the owner asked to keep subfoldering
    simple, and a dated prefix already groups these the way a folder would when the
    listing is sorted.
    """
    topic = " ".join((topic or "untitled").split())[:TITLE_CHARS]
    return f"{_day(now)} Research -- {topic}"


def write_research_brief(obsidian, topic: str, findings: str, question: str | None = None,
                         source: str = "business", now: datetime | None = None) -> dict:
    """File a completed research brief into the agent folder.

    Called right after `complete_research(...)` in both research queues. Until this
    existed, both queues wrote their findings to SQLite and stopped -- nine business
    briefs and three personal ones had completed while the vault's newest research note
    was weeks old. This is decision 1's clearest case: a brief is exactly a "written
    deliverable".

    Returns a result dict; never raises. A failed vault write is logged and the caller
    carries on, because the findings are already safely in the database.
    """
    if obsidian is None:
        return {"written": False, "reason": "no vault client wired"}
    body = (findings or "").strip()
    if not body:
        return {"written": False, "reason": "no findings to write"}

    header = [f"**Topic:** {topic}"]
    if (question or "").strip():
        header.append(f"**Question asked:** {question.strip()}")
    header.append(f"**Researched:** {(now or datetime.now(timezone.utc)):%Y-%m-%d %H:%M} UTC "
                  f"by the {source} research queue, unattended.")
    header.append("")

    try:
        result = obsidian.write_note(
            AGENT_FOLDER, brief_title(topic, now),
            "\n".join(header) + body[:BRIEF_BODY_CHARS],
            tags=["research", "brief", source],
        )
        logger.info("research brief filed to the vault: %s", result.get("path"))
        return {"written": True, "path": result.get("path")}
    except Exception as e:
        # Never fatal: the findings are in the database either way.
        logger.exception("could not write the research brief for %r to the vault", topic)
        return {"written": False, "reason": f"{type(e).__name__}: {e}"}
