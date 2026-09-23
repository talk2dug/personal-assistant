"""Narrative memory for scheduled employees, kept in the owner's own Obsidian vault.

NAMING: this module is still called crypto_journal because that is what it was built for
and its imports are wired that way across staff.py, scheduler.py and the tests. It now
also serves the engineering employees, whose entries are built from the work record and a
PR number rather than from ledger fills (role "engineer" throughout). The mechanism is
unchanged and was already ~90% generic; only the name is now narrower than the module.
Worth renaming on a quiet day, not worth the import churn during a feature.

Built to `docs/crypto-journal-design.md`. The problem it exists to solve, stated exactly:
the two crypto employees are amnesiac. `staff.assign()` makes one `llm.research()` call
and throws the context away; nothing survives to the next run but the live market
snapshot. The trade log proves the cost -- the same tickers bought, stopped out, and
re-bought days later on "fresh momentum", because the model never once saw the lesson it
had already paid for.

Three rules shape every function here, and all three come from how this codebase already
works rather than from anything new:

  * **The agent never calls the vault tool.** It ends its prose with a fenced ```journal
    block, exactly as it already ends with a ```orders block, and code parses that and
    writes the note -- the same split `paper_trading.execute_orders()` enforces for the
    ledger. A model that both decides and records is a model that can record a trade it
    never made.
  * **Retrieval is a pre-fetch, not a tool loop.** Scheduled staff are one-shot
    completions with no mid-run tool calling (`build_feed_briefing` already pre-fetches
    market data as text for exactly this reason). Prior notes arrive the same way, as one
    more text block, size-capped.
  * **Numbers come from the ledger, prose comes from the model.** Win rates, realised
    P&L and fills are read out of `paper_trades` at write time. A model asked to recall
    its own record will produce a flattering one.

`write_note` is deliberately append-only, which is right for a timeline and wrong for a
rolling summary that gets re-read into every prompt. `compact_summary()` below is the
housekeeping answer from the design's section 5.2: archive the old body, rewrite the note
in place with the current state only. It is ops-level code the agent never invokes, the
same category of thing as `check_stops()`.
"""
import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

JOURNAL_FOLDER = "04-Journal"
SUMMARY_FOLDER = "03-Areas"
ARCHIVE_FOLDER = "05-Archive"

# Where each role's notes live.
#
# The crypto desk keeps the folders it shipped with. Everything added since writes into
# the agent folder instead, which is the owner's decision 2: agent-written notes stay out
# of 01-Research/02-Projects/03-Areas, where his own hand-written notes are, so he never
# has to work out whether he wrote a note or a machine did.
#
# The crypto desk is the deliberate exception rather than an oversight. Its notes already
# exist in 04-Journal/03-Areas, are wikilinked to each other, and moving them would orphan
# those links for a cosmetic gain. Worth revisiting as a one-off migration; not worth
# breaking a working desk over.
_ROLE_FOLDERS = {
    "trader": (JOURNAL_FOLDER, SUMMARY_FOLDER),
    "analyst": (JOURNAL_FOLDER, SUMMARY_FOLDER),
}


def folders_for(role: str) -> tuple[str, str]:
    """(journal folder, running-summary folder) for a role."""
    from .obsidian_client import AGENT_FOLDER
    return _ROLE_FOLDERS.get(role, (AGENT_FOLDER, AGENT_FOLDER))


def role_for(employee: dict) -> str:
    """Which journalling role an employee plays.

    One source of truth, because three callers need to agree: staff.assign (which picks
    the prompt and the folders), run_housekeeping (which compacts the right summary note),
    and the tests. A trader runs against a ledger, an engineer against a repository, and an
    analyst only reports -- and that distinction sets what gets recorded, how often, and
    where it lands.
    """
    if "paper" in (employee.get("data_feeds") or ""):
        return "trader"
    if (employee.get("department") or "") == "engineering":
        return "engineer"
    return "analyst"

# The whole prior-context block is capped, the same way market data is capped to top
# movers rather than all 244 coins: this is injected on every single run, so an unbounded
# read is an unbounded prompt.
PRIOR_CONTEXT_CHARS = 2000
SUMMARY_SHARE_CHARS = 1200
RECENT_DAILY_NOTES = 2          # today + yesterday; the summary carries anything older

# Compaction triggers on size rather than on a fixed weekly calendar (the design's open
# question 2). A day trader's summary grows with how much actually happened, not with how
# many days passed, and a quiet week should not force a rewrite while a busy day should.
COMPACT_THRESHOLD_CHARS = 6000
KEEP_LESSONS = 10

_LESSON_LINE = re.compile(r"(?m)^- LESSON: (.+)$")
_THESIS_LINE = re.compile(r"(?m)^- THESIS ([A-Z0-9._-]{1,12}): (.+)$")


# --- note naming --------------------------------------------------------------

def summary_title(employee_title: str) -> str:
    """One evergreen note per employee. Per the design's section 6 this is deliberately
    per-employee (and later per-venue) rather than one shared desk note: merging a paper
    win rate with a dry-run one would quietly launder one venue's record into a claim
    about the other, and an honest win rate is the entire point."""
    return f"{employee_title} -- Running Summary"


def daily_title(employee_title: str, day: str) -> str:
    """One dated note per employee per day, so the file count is bounded by the calendar
    rather than by the run cadence -- 288 runs a day would otherwise be 288 notes."""
    return f"{day} -- {employee_title}"


# --- what the model is asked to emit ------------------------------------------

_BLOCK_EXAMPLE = """```journal
{"kind": "analysis",
 "summary": "one-line headline, e.g. 'BTC thesis unchanged: range-bound 74-80k'",
 "direction_change": false,
 "tickers": ["BTC", "ETH"],
 "detail": "the reasoning, in full sentences -- what you saw and why you concluded it",
 "lesson": "optional -- what this run confirmed or got wrong about an earlier call"}
```"""

_COMMON = (
    "\n\n--- YOUR JOURNAL ---\n"
    "You keep a journal in the owner's Obsidian vault. You do not write to it yourself: "
    "you end your prose with a fenced ```journal block and the system files it for you, "
    "exactly like your orders. Its entries are read back to you on later runs -- this is "
    "the only memory you have, so write it for the version of you that wakes up with no "
    "recollection of today.\n\n"
    + _BLOCK_EXAMPLE +
    "\n\nRules:\n"
    "  * `direction_change` is true only when this run's call actually contradicts the "
    "last one on file for that ticker -- not when you restate it with more confidence.\n"
    "  * `lesson` is for something you would want to know before making this call again. "
    "Leave it out rather than filling it with something generic.\n"
    "  * Write the detail as reasoning, not as a data dump. Prices are already recorded "
    "for you; why you acted on them is not.\n"
    "  * Do not describe trades you did not have filled. The ledger writes the fills into "
    "your entry itself, from its own record.\n"
)

_ANALYST_WHEN = (
    "  * Journal on every run. Your cadence is slow enough that each run is worth a "
    "record, and a run that changed nothing is itself a useful thing to have written "
    "down.\n"
    "--- end journal ---\n"
)

_ENGINEER_BLOCK_EXAMPLE = """```journal
{"kind": "change",
 "summary": "one-line headline, e.g. 'Split the chat timeout from background work'",
 "components": ["scheduler.py", "work_queue.py"],
 "detail": "what you changed and WHY -- the reasoning, the alternative you rejected, and
            anything that surprised you about how this codebase actually works",
 "lesson": "optional -- what you would want to know before touching this area again"}
```"""

_ENGINEER_COMMON = (
    "\n\n--- YOUR ENGINEERING JOURNAL ---\n"
    "You keep a journal in the owner's Obsidian vault. You do not write to it yourself: "
    "you end your prose with a fenced ```journal block and the system files it for you. "
    "Its entries are read back to you on later runs -- this is the only memory you have "
    "between assignments, so write it for the version of you that wakes up having never "
    "seen this codebase.\n\n"
    + _ENGINEER_BLOCK_EXAMPLE +
    "\n\nRules:\n"
    "  * `kind` is \"change\" for work you carried out, \"design\" for a design document or "
    "an architectural decision, \"lesson\" for something learned that is not tied to one "
    "change.\n"
    "  * Write the detail as REASONING, not as a changelog. What the diff was is already in "
    "git, in more detail and more accurately than you could restate it. Why you did it that "
    "way, what you tried first, and what this codebase turned out to actually do are not "
    "recorded anywhere else.\n"
    "  * `lesson` is for something you would want to know before touching this area again "
    "-- a constraint that is not obvious from the code, a test that matters more than it "
    "looks, a place where the obvious approach is wrong. Leave it out rather than filling "
    "it with something generic.\n"
    "  * Do not claim work you did not finish. The system records the real outcome of this "
    "assignment, and any pull request you opened, into your entry from its own record.\n"
    "  * Journal on every run. You run rarely enough that each assignment is worth a "
    "record.\n"
    "--- end journal ---\n"
)

_TRADER_WHEN = (
    "  * Journal only on a run where something actually happened: you traded, your "
    "direction on a ticker changed, or you learned something specific. A routine "
    "'nothing to do' run needs no block, and the system will not file one -- say so in "
    "prose and move on. You run often; a journal that records every tick is a journal "
    "nobody, including you, will ever read.\n"
    "--- end journal ---\n"
)

_OUTPUT_ORDER = (
    "\nOutput order when you have several blocks: reasoning prose first, then the "
    "```journal block, then the ```orders block, and the one-line verdict JSON (if you "
    "were asked for one) absolutely last.\n"
)

# An engineer has no ledger and never emits an orders block. Naming one would invite it to
# invent a block nothing parses, and the whole reason the journal format is described so
# precisely is that a model given a vague output contract produces a different one each run.
_ENGINEER_OUTPUT_ORDER = (
    "\nOutput order when you have several blocks: your report prose first, then the "
    "```journal block, and the one-line verdict JSON (if you were asked for one) "
    "absolutely last.\n"
)


def journal_instructions(role: str) -> str:
    """The prompt addition, per role.

    Plain concatenation, never str.format: this text contains literal JSON braces, and
    format() reading `{"kind": ...}` as a field name is the exact bug that silently
    stopped every scheduled employee for over an hour (see staff.build_verdict_instructions).
    """
    if role == "engineer":
        return _ENGINEER_COMMON + _ENGINEER_OUTPUT_ORDER
    when = _TRADER_WHEN if role == "trader" else _ANALYST_WHEN
    return _COMMON + when + _OUTPUT_ORDER


# --- parsing ------------------------------------------------------------------

def parse_journal_entry(text: str | None) -> dict | None:
    """Pull the journal block out of an employee's response.

    Deliberately the same shape as `paper_trading.parse_orders`: tolerant about the fence
    label, last block wins (a model that restates puts the final answer last), and
    anything unparseable yields no entry rather than a guessed one. A fabricated journal
    entry is worse than a missing one -- it becomes a "lesson" the desk trusts forever.

    An orders payload is explicitly skipped: the trader emits both blocks in one reply,
    and a bare ```json fence around its orders must never be filed as a journal entry.
    Fence scanning is shared with parse_orders rather than reimplemented -- two parsers
    reading the same reply with two subtly different regexes is how one of them ends up
    swallowing the other's block (see paper_trading.fenced_blocks for the real incident).
    """
    if not text:
        return None
    from .paper_trading import fenced_blocks
    blocks = fenced_blocks(text, {"", "journal", "json"})
    for block in reversed(blocks):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "orders" in data:
            continue
        summary = str(data.get("summary") or "").strip()
        detail = str(data.get("detail") or "").strip()
        lesson = str(data.get("lesson") or "").strip()
        if not (summary or detail or lesson):
            continue        # a block with none of the three carries no narrative at all
        kind = str(data.get("kind") or "analysis").strip().lower()
        if kind not in ("analysis", "trade_outcome", "lesson", "change", "design"):
            kind = "analysis"
        # `components` is the engineering alias for `tickers`: the same field -- what this
        # entry is ABOUT -- under the name each role would naturally reach for. One parsed
        # key rather than two keeps a single code path; the rendering picks the label.
        # Ticker codes are upper-cased and short; file and module names are neither, so
        # only the crypto spelling gets normalised.
        raw_tickers, raw_components = data.get("tickers"), data.get("components")
        if isinstance(raw_tickers, list):
            tickers = [str(t).strip().upper()[:12] for t in raw_tickers if str(t).strip()]
        elif isinstance(raw_components, list):
            tickers = [str(c).strip()[:60] for c in raw_components if str(c).strip()]
        else:
            tickers = []
        return {
            "kind": kind,
            "summary": summary[:300],
            "detail": detail[:4000],
            "lesson": lesson[:500] or None,
            "tickers": tickers[:12],
            "direction_change": bool(data.get("direction_change")),
        }
    return None


# --- reading prior context back into the next run -----------------------------

def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            return text[end + 4:].lstrip()
    return text


def _tail(text: str, limit: int) -> str:
    """The END of a note, not the start.

    Both note shapes here append: `write_note` adds new material at the bottom, so the
    last N characters are the most recent entries. Truncating from the front would feed
    the model the oldest notes it has and call them context.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    return "...(earlier entries trimmed)...\n" + text[-limit:]


def build_prior_context(obsidian, employee_title: str, max_chars: int = PRIOR_CONTEXT_CHARS,
                        recent_days: int = RECENT_DAILY_NOTES, role: str = "analyst") -> str:
    """The employee's own earlier notes, pre-fetched into this run's prompt.

    Mirrors `staff.build_feed_briefing` exactly -- the orchestrator reads, the agent just
    receives text -- because scheduled staff have no tool loop to fetch with. Every read
    failure degrades to a stated absence rather than an exception: a vault on a
    disconnected drive must cost this employee its memory, not its run.
    """
    if obsidian is None:
        return ""

    journal_folder, summary_folder = folders_for(role)

    summary_body = ""
    try:
        note = obsidian.read_note(summary_folder, summary_title(employee_title))
        if isinstance(note, dict) and not note.get("error"):
            summary_body = _tail(_strip_frontmatter(note.get("content") or ""),
                                 min(SUMMARY_SHARE_CHARS, max_chars))
    except Exception:
        logger.exception("could not read the running summary for %s", employee_title)

    dailies, budget = [], max_chars - len(summary_body)
    try:
        suffix = f" -- {employee_title}"
        titles = sorted((n["title"] for n in obsidian.list_notes(journal_folder).get("notes", [])
                         if n["title"].endswith(suffix)), reverse=True)[:recent_days]
        for title in titles:
            if budget <= 120:       # not enough room left to say anything useful
                break
            note = obsidian.read_note(journal_folder, title)
            if not isinstance(note, dict) or note.get("error"):
                continue
            body = _tail(_strip_frontmatter(note.get("content") or ""), budget)
            if body:
                dailies.append(f"{title}:\n{body}")
                budget -= len(body)
    except Exception:
        logger.exception("could not read recent journal notes for %s", employee_title)

    parts = ["\n\n--- YOUR OWN PRIOR NOTES (you wrote these on earlier runs; they are "
             "your memory, and nothing else carries over) ---"]
    if summary_body:
        parts.append("RUNNING SUMMARY:\n" + summary_body)
    if dailies:
        parts.append("RECENT DAILY ENTRIES:\n" + "\n\n".join(dailies))
    if not summary_body and not dailies:
        # Said out loud rather than left blank: an employee that silently gets no prior
        # context cannot tell "I have never written anything" from "the vault is broken",
        # and the second one is something it should mention in its report.
        parts.append("No journal entries on file yet. This is the first run keeping one, "
                     "so write today's entry as the starting point rather than assuming "
                     "earlier reasoning exists somewhere you cannot see.")
    return "\n".join(parts) + "\n--- end prior notes ---\n"


# --- writing this run's entry -------------------------------------------------

def _fmt_price(v) -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1:
        return f"${v:,.2f}"
    if a >= 0.01:
        return f"${v:.4f}"
    if a == 0:
        return "$0"
    return f"${v:.4g}"


def _opening_link(db_path: str | None, employee_title: str, code: str) -> str:
    """A wikilink back to the day the position being closed was opened.

    The design asks for a link to the opening *entry*; this links to the opening day's
    note instead, deliberately. A heading anchor would have to reproduce the opening
    run's own HH:MM heading exactly, and a run's note heading is stamped when the entry is
    written rather than when the fill happened -- close enough to look right and just
    wrong often enough to leave broken links scattered through the vault. A link that
    always resolves, plus the timestamp in the text, is worth more than a precise one
    that sometimes doesn't.
    """
    if not db_path:
        return ""
    try:
        from . import paper_trading
        opened = paper_trading.last_buy_at(db_path, code)
    except Exception:
        return ""
    if not opened:
        return ""
    return f" -- opened {opened[:16].replace('T', ' ')}Z, see [[{daily_title(employee_title, opened[:10])}]]"


def _fill_lines(fills: list[dict], employee_title: str, db_path: str | None) -> list[str]:
    lines = ["", "Filled this run (the ledger's record, not the employee's claim):"]
    for f in fills:
        if f.get("side") in ("raise_stop", "lower_stop"):
            # Same fill list, but this one moved a stop rather than a coin. Printed as a
            # trade it reads "RAISE_STOP 0 ARB @ $0.163 = $0.00", which in a journal he
            # reads back later looks like a zero-quantity trade rather than the ratchet
            # doing its job. lower_stop is the short side's mirror -- same treatment.
            why = f" -- \"{f['reason']}\"" if f.get("reason") else ""
            verb = "STOP RAISED" if f.get("side") == "raise_stop" else "STOP LOWERED"
            lines.append(f"- {verb} {f.get('code')} {_fmt_price(f.get('from_stop'))} "
                         f"-> {_fmt_price(f.get('to_stop'))} "
                         f"(spot {_fmt_price(f.get('price'))}){why}")
            continue
        realized = f.get("realized")
        tail = ""
        if realized is not None:
            tail = f" -> realised ${realized:+,.2f}" + _opening_link(db_path, employee_title, f["code"])
        why = f" -- \"{f['reason']}\"" if f.get("reason") else ""
        lines.append(f"- {str(f.get('side', '')).upper()} {f.get('qty', 0):.6g} {f.get('code')} "
                     f"@ {_fmt_price(f.get('price'))} = ${f.get('usd', 0):,.2f} "
                     f"(fee ${f.get('fee', 0):,.2f}){tail}{why}")
    return lines


_PR_REFERENCE = re.compile(r"(?:/pull/|\bPR\s*#|\bpull request\s*#|(?<![\w/])#)(\d{1,6})\b",
                           re.IGNORECASE)


def pr_numbers(text: str | None) -> list[int]:
    """Pull request numbers mentioned in an engineer's output.

    The engineering equivalent of reading fills out of the ledger rather than believing
    the model's account of its own trades -- but only partly, and the difference is worth
    being honest about. A fill is the ledger's own fact; this is still scraped from the
    model's prose, because an employee opens a PR through its own git tool and nothing in
    the work record captures the number.

    So this is recorded as "the PR this run said it opened", never as proof one exists.
    Bounded at six digits and de-duplicated in first-seen order; a run that mentions no PR
    yields nothing rather than a guess.
    """
    if not text:
        return []
    seen, out = set(), []
    for match in _PR_REFERENCE.finditer(text):
        number = int(match.group(1))
        if number not in seen:
            seen.add(number)
            out.append(number)
    return out[:8]


def _work_lines(work: dict | None, prs: list[int]) -> list[str]:
    """What actually happened this assignment, from the work record rather than the model's
    claim about it -- the same split _fill_lines enforces for the trading desk.

    A failed run still journals, and says so plainly. "I tried this and it did not work,
    here is how far I got" is exactly the entry the next run most needs, and it is the one
    a model narrating its own history is least likely to write.
    """
    if not work:
        return []
    lines = ["", "This assignment (the system's own record, not the employee's claim):"]
    assignment = " ".join(str(work.get("assignment") or "").split())
    if assignment:
        lines.append(f"- Asked to: {assignment[:400]}")
    status = str(work.get("status") or "unknown")
    lines.append(f"- Outcome: {status}")
    if work.get("error"):
        lines.append(f"- Failed with: {str(work['error'])[:300]}")
    if prs:
        lines.append("- Pull request(s) this run reported opening: "
                     + ", ".join(f"#{n}" for n in prs))
    return lines


def _stats_line(stats: dict | None) -> str | None:
    if not stats:
        return None
    rate = stats.get("win_rate_pct")
    rate_text = f"{rate}%" if rate is not None else "n/a"
    return (f"Record at this point: {stats.get('closed_trades', 0)} closed round-trips, "
            f"{stats.get('wins', 0)} won ({rate_text}), realised "
            f"${stats.get('realized_pnl', 0):+,.2f}, fees ${stats.get('fees_paid', 0):,.2f}, "
            f"equity ${stats.get('equity', 0):,.2f}.")


def record_run(obsidian, employee_title: str, entry: dict | None, role: str = "analyst",
               fills: list[dict] | None = None, stats: dict | None = None,
               db_path: str | None = None, now: datetime | None = None,
               work: dict | None = None, output: str | None = None) -> dict:
    """File this run's journal entry. Returns what was written, and why not when not.

    The owner's answer to the design's open question 1 is encoded here: the day trader
    journals only on a run with a trade, a direction change, or an explicit lesson, while
    the analyst -- running far less often -- journals every run. A trader at a frequent
    cadence writing an entry per tick produces a log nobody will read, including itself.

    A run that filled orders is always journaled even when the model emitted no block at
    all: the fills are the ledger's own fact, and losing them because the model forgot its
    formatting would put a hole in exactly the record this exists to build.

    An engineer journals every run, for the same reason the analyst does -- it runs rarely
    enough that every assignment is worth a record -- and its entry is built from the work
    record (`work`, a staff_work row) plus any PR number in `output`, standing in for the
    trader's fills. A FAILED assignment is journaled too, deliberately: "I tried this and it
    did not work, here is how far I got" is the entry the next run most needs, and is
    exactly what a model narrating its own history will quietly omit.
    """
    fills = fills or []
    now = now or datetime.now(timezone.utc)
    closes = [f for f in fills if f.get("realized") is not None]
    direction_change = bool(entry and entry.get("direction_change"))
    lesson = (entry or {}).get("lesson")
    prs = pr_numbers(output) if role == "engineer" else []

    if entry is None and not fills and not (role == "engineer" and work):
        return {"journaled": False, "reason": "no journal block and nothing was filled"}
    if role == "trader" and not (fills or direction_change or lesson):
        return {"journaled": False,
                "reason": "routine run: no fill, no direction change, no lesson"}

    journal_folder, summary_folder = folders_for(role)
    subject_label = "Components" if role == "engineer" else "Tickers"

    lines = [f"### {now:%H:%M} UTC"]
    if entry:
        if entry["summary"]:
            lines.append(f"**{entry['summary']}**")
        if entry["tickers"]:
            lines.append(f"{subject_label}: {', '.join(entry['tickers'])}")
        if direction_change:
            lines.append("**Direction change** from the last call on file.")
        if entry["detail"]:
            lines += ["", entry["detail"]]
        if lesson:
            # Written in this exact form on purpose: compact_summary() below greps for it,
            # so a lesson survives the housekeeping pass that trims everything else.
            lines += ["", f"- LESSON: {lesson}"]
    else:
        lines.append("_No journal block in this run's output -- the system's own record "
                     "of what happened follows._")
    if fills:
        lines += _fill_lines(fills, employee_title, db_path)
    if role == "engineer":
        lines += _work_lines(work, prs)
    stats_line = _stats_line(stats)
    if stats_line:
        lines += ["", stats_line]

    day = f"{now:%Y-%m-%d}"
    tags = (["agent", "journal", role] if role == "engineer" else ["crypto", "journal", role])
    written = obsidian.write_note(journal_folder, daily_title(employee_title, day),
                                  "\n".join(lines), tags=tags)

    # Section 2.2: routine runs write only the dated entry. The summary -- the note that
    # gets re-read into every prompt -- takes only what should never need re-deriving.
    # For an engineer a shipped PR is the equivalent of a closed trade: the thing that
    # actually landed, and the thing worth still knowing about several runs later.
    folded = None
    if direction_change or lesson or closes or prs:
        fold = [f"### {now:%Y-%m-%d %H:%M} UTC"]
        headline = (entry or {}).get("summary") or ""
        # THESIS lines are a crypto shape -- "the current call on this ticker", which
        # compact_summary keeps one of per ticker because a later call supersedes an
        # earlier one. An engineer's components have no such superseding relationship:
        # touching scheduler.py twice is two facts, not a revision of one.
        if role != "engineer":
            for ticker in ((entry or {}).get("tickers") or [])[:6]:
                if headline:
                    fold.append(f"- THESIS {ticker}: {headline}")
        if headline and (role == "engineer" or not (entry or {}).get("tickers")):
            fold.append(f"- {headline}")
        if lesson:
            fold.append(f"- LESSON: {lesson}")
        for f in closes:
            fold.append(f"- CLOSED {f['code']}: realised ${f['realized']:+,.2f}"
                        + (f" -- \"{f['reason']}\"" if f.get("reason") else ""))
        if prs:
            fold.append("- SHIPPED: " + ", ".join(f"#{n}" for n in prs)
                        + (f" -- {headline}" if headline else ""))
        if stats_line:
            fold.append(f"- RECORD: {stats_line}")
        summary_tags = (["agent", "summary", role] if role == "engineer"
                        else ["crypto", "summary", role])
        folded = obsidian.write_note(summary_folder, summary_title(employee_title),
                                     "\n".join(fold), tags=summary_tags)

    return {"journaled": True, "reason": "written", "daily": written.get("path"),
            "summary": (folded or {}).get("path"), "folded": folded is not None}


# --- housekeeping: keeping the re-read note bounded ---------------------------

def compact_summary(obsidian, employee_title: str, stats: dict | None = None,
                    threshold: int = COMPACT_THRESHOLD_CHARS, keep_lessons: int = KEEP_LESSONS,
                    now: datetime | None = None, force: bool = False,
                    role: str = "analyst") -> dict:
    """Archive the running summary's history and rewrite it as current state only.

    This is the design's section 5.2, and it exists because `write_note` is append-only by
    deliberate design. That is correct for a capture tool and fatal for a note re-read
    into every prompt: left alone, the one note meant to prevent prompt bloat becomes the
    prompt bloat. Nothing is deleted -- the old body moves to `05-Archive` first -- and
    the rewritten note keeps the same `- THESIS`/`- LESSON` line format, so the next pass
    can read what this one wrote.

    Not an agent-facing tool, by the same rule that keeps `execute_orders()` out of the
    employee's hands: housekeeping is code the desk never calls.
    """
    now = now or datetime.now(timezone.utc)
    title = summary_title(employee_title)
    _, summary_folder = folders_for(role)
    path = obsidian.note_path(summary_folder, title)
    if not path.exists():
        return {"compacted": False, "reason": "no running summary yet"}
    raw = path.read_text(encoding="utf-8")
    if len(raw) < threshold and not force:
        return {"compacted": False, "reason": f"{len(raw)} chars, under the {threshold} threshold",
                "size": len(raw)}

    body = _strip_frontmatter(raw)
    lessons = _LESSON_LINE.findall(body)[-keep_lessons:]
    theses: dict[str, str] = {}
    for ticker, text in _THESIS_LINE.findall(body):
        theses[ticker] = text        # last mention of a ticker wins -- it is the current call

    family = "agent" if role == "engineer" else "crypto"
    archived = obsidian.write_note(
        ARCHIVE_FOLDER, f"{employee_title} Summary -- {now:%Y-%m-%d}",
        f"Compacted at {now:%Y-%m-%d %H:%M} UTC from a {len(raw)}-character running "
        f"summary. Full prior body:\n\n" + body, tags=[family, "archive"])

    created = re.search(r"(?m)^created: (.+)$", raw)
    header = (f"---\ntags: [{family}, summary]\ncreated: "
              f"{created.group(1) if created else now.isoformat()}\n"
              f"updated: {now.isoformat()}\n---\n\n# {title}\n\n")
    out = [f"_Compacted {now:%Y-%m-%d %H:%M} UTC. Everything trimmed from here is in "
           f"[[{employee_title} Summary -- {now:%Y-%m-%d}]], nothing was deleted._\n"]
    # An engineer keeps no per-ticker thesis -- see the fold in record_run. Its lessons are
    # the part worth carrying forward, so its compacted note is lessons only rather than a
    # permanently empty "current thesis" heading.
    if role != "engineer":
        out += ["## Current thesis by ticker\n"]
        out += [f"- THESIS {t}: {text}" for t, text in sorted(theses.items())] or ["_none on file yet_"]
        out += [""]
    out += [f"## Last {len(lessons)} lesson(s) learned\n"]
    out += [f"- LESSON: {lesson}" for lesson in lessons] or ["_none on file yet_"]
    stats_line = _stats_line(stats)
    if stats_line:
        out += ["", "## Machine-computed record (from the ledger, not estimated)\n",
                f"- RECORD: {stats_line}"]
    path.write_text(header + "\n".join(out) + "\n", encoding="utf-8")

    return {"compacted": True, "reason": "rewritten", "was": len(raw),
            "now": len(header) + len("\n".join(out)), "archived": archived.get("path"),
            "lessons_kept": len(lessons), "theses_kept": len(theses)}


def journal_employees(db_path: str) -> list[dict]:
    """Active employees granted the `journal` feed. Explicit, never inferred -- exactly
    like `paper`, and for the same reason: half the roster's job descriptions mention
    crypto, and journaling every one of them would fill the owner's vault with dev-team
    chatter."""
    from . import staff
    return [e for e in staff.list_staff(db_path)
            if "journal" in (e.get("data_feeds") or "") and e["status"] == "active"]


def run_housekeeping(db_path: str, obsidian, threshold: int = COMPACT_THRESHOLD_CHARS,
                     now: datetime | None = None) -> list[dict]:
    """Compact every journalling employee's running summary that has grown past the
    threshold. Cheap to call often: an under-threshold note is one stat and a read."""
    if obsidian is None:
        return []
    results = []
    for emp in journal_employees(db_path):
        stats = None
        if "paper" in (emp.get("data_feeds") or ""):
            try:
                from . import paper_trading
                stats = paper_trading.performance(db_path)
            except Exception:
                logger.exception("could not read paper performance for %s", emp["key"])
        try:
            outcome = compact_summary(obsidian, emp["title"], stats=stats, threshold=threshold,
                                      now=now, role=role_for(emp))
        except Exception as e:
            logger.exception("summary compaction failed for %s", emp["key"])
            outcome = {"compacted": False, "reason": f"{type(e).__name__}: {e}"}
        outcome["employee"] = emp["key"]
        results.append(outcome)
    return results
