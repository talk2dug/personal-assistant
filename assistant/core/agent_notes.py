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


# --- the business agents' working journal ----------------------------------------------
#
# Jack: "all agents should be using obsidian", and separately, for the dashboard: "i want
# to see their thoughts on the market research and how they are coming to the conclusions".
# Those are the same requirement seen from two sides -- the reasoning has to be written
# down somewhere durable, in prose, per agent, per run.
#
# The hired staff already journal (staff.infer_data_feeds gives everyone the `journal`
# feed). The seven background agents in agents.py did not: only the research queue ever
# touched the vault, so market_finder, trend_scout, product_creator, art_director,
# store_manager and social_director each re-derived their conclusions from scratch on
# every run and left no trace of why they chose anything.
#
# This module's own rule applies and is the reason read_journal exists at all: **anything
# written must be read back into a later prompt, or it is a diary, not learning.** So a
# journal entry is written after a run AND the last few are fed into the next one. That is
# what turns "the agents use Obsidian" into agents that actually get better, which is the
# thing Jack is paying attention to.

# Tight, unlike a brief: this text goes into a prompt on every run, and an agent that
# spends its context re-reading a fortnight of its own musings has no room left to think.
JOURNAL_BODY_CHARS = 4000
JOURNAL_PRIOR_CHARS = 2500
JOURNAL_PRIOR_ENTRIES = 3


def journal_title(agent: str, now: datetime | None = None) -> str:
    """One note per agent per day, dated first so the folder sorts chronologically.

    Per day rather than per run: several runs a day is normal, and a note each would bury
    the folder the owner also reads. Same day appends into the same note.
    """
    return f"{_day(now)} Journal -- {agent}"


def write_journal(obsidian, agent: str, summary: str, reasoning: str = "",
                  decisions: list | None = None, now: datetime | None = None) -> dict:
    """Record what this agent concluded and why. Never raises.

    `summary` is the one-line outcome; `reasoning` is the prose that matters -- what it
    looked at, what it ruled out, and on what grounds. An entry with a summary and no
    reasoning is permitted but close to useless: it tells the next run what happened and
    not one thing about why.
    """
    if obsidian is None:
        return {"written": False, "reason": "no vault client wired"}
    stamp = (now or datetime.now(timezone.utc))
    lines = [f"### {stamp:%H:%M} UTC", "", (summary or "").strip() or "(no summary)"]
    if (reasoning or "").strip():
        lines += ["", "**Reasoning**", "", reasoning.strip()[:JOURNAL_BODY_CHARS]]
    if decisions:
        lines += ["", "**Decisions**", ""]
        lines += [f"- {str(d).strip()}" for d in decisions if str(d).strip()]
    lines.append("")

    title = journal_title(agent, stamp)
    try:
        existing = ""
        try:
            previous = obsidian.read_note(AGENT_FOLDER, title)
            existing = (previous.get("content") or "").rstrip() + "\n\n"
        except Exception:
            existing = ""          # first entry of the day; not an error
        result = obsidian.write_note(
            AGENT_FOLDER, title, existing + "\n".join(lines),
            tags=["journal", "agent", agent])
        return {"written": True, "path": result.get("path")}
    except Exception as e:
        # A vault on a disconnected drive costs the agent its note, never its work --
        # everything real was committed to the database before this was reached.
        logger.warning("could not journal %s to the vault: %s", agent, e)
        return {"written": False, "reason": f"{type(e).__name__}: {e}"}


def read_journal(obsidian, agent: str, entries: int = JOURNAL_PRIOR_ENTRIES) -> str:
    """The agent's own recent notes, as a block to prepend to its next prompt.

    Returns "" when there is nothing to say, so a caller can concatenate unconditionally.
    This is the half that makes the writing worth doing.
    """
    if obsidian is None:
        return ""
    try:
        listing = obsidian.list_notes(AGENT_FOLDER)
        names = listing.get("notes") if isinstance(listing, dict) else listing
        titles = sorted(
            (n if isinstance(n, str) else n.get("title", "")) for n in (names or []))
        mine = [t for t in titles if t.endswith(f"Journal -- {agent}")][-entries:]
        if not mine:
            return ""
        chunks = []
        for title in reversed(mine):                # newest first
            try:
                note = obsidian.read_note(AGENT_FOLDER, title)
                body = (note.get("content") or "").strip()
                if body:
                    chunks.append(f"--- {title} ---\n{body}")
            except Exception:
                continue
        if not chunks:
            return ""
        joined = "\n\n".join(chunks)[:JOURNAL_PRIOR_CHARS]
        return ("\n\nYOUR OWN RECENT NOTES (what you concluded on previous runs, and why). "
                "Build on these rather than re-deriving them, and say so plainly if you are "
                "now changing your mind about one:\n\n" + joined + "\n")
    except Exception as e:
        logger.warning("could not read %s journal from the vault: %s", agent, e)
        return ""
