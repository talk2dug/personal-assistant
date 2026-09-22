"""Questions whose answer is a row in SQLite, answered without a model.

Jack, 2026-09-21: *"the length of time i wait to get answers back from questions that I
database could pull up within micro seconds... the fact i have to wait a long time to get
info back about the current status of a project or whatever from the system is crazy."*

He is right, and the numbers back him up. "What is the current value of the paper
trading?" took **8.9 seconds** -- one SQL query, wrapped in a Claude CLI spawn and a full
agentic tool loop. The median owner turn is 6.9s. Nothing about "how is the store doing"
requires a language model to *find* the answer; the model was only ever being used to
read a number off a table and put it in a sentence, and that sentence is one this module
can write.

So this sits in front of everything, cheaper even than the local fast path next to it:
no network, no inference, just a read. Measured at ~25ms against his real 449MB
database, which is roughly 350x faster than the reply it replaces.

PRECISION OVER RECALL, deliberately. A wrong instant answer is far worse than a slow
right one, because it is confidently wrong AND it steals the turn from the model that
would have got it right. So every pattern here demands a recognised subject *and* a
status-shaped question, anything with extra intent in it ("...and publish the next one")
is refused, and any doubt returns None, which costs the turn nothing but the lookup.

It also fixes something the speed alone does not. Jack: *"He should be one person to me."*
An assistant whose answer to "how's the store" depends on whether the model felt like
calling a tool is not one person; it is a different colleague every time. A deterministic
answer to a factual question is the same answer every time, which is most of what being
one person means.
"""
import logging
import re

logger = logging.getLogger(__name__)

# Subject -> mission key. Plural, possessive and shorthand forms are all here rather than
# stemmed: the list is short, and a stemmer that turns "trading" into "trade" would also
# quietly match words nobody meant.
SUBJECTS = {
    "store": "store_products_live",
    "shop": "store_products_live",
    "jarvis store": "store_products_live",
    "storefront": "store_products_live",
    "listings": "store_products_live",
    "products": "store_products_live",
    "crypto": "crypto_pnl_30d",
    "crypto desk": "crypto_pnl_30d",
    "the desk": "crypto_pnl_30d",
    "trading": "crypto_pnl_30d",
    "paper trading": "crypto_pnl_30d",
    "paper trader": "crypto_pnl_30d",
    "trades": "crypto_pnl_30d",
}

# A question about state, not a request to change it.
_STATUS = re.compile(
    r"\b(status|how(?:'s| is| are| goes)?|hows|where (?:are|is)|what'?s (?:the )?(?:status|state)"
    r"|update on|doing|going|going on|progress|stand(?:ing)?)\b", re.I)

# Asking the number itself rather than how it is going. Its own pattern because his
# real question -- "What is the current value of the paper trading?", 8.9 seconds -- is
# not status-shaped at all, and that question is the whole reason this module exists.
_VALUE = re.compile(
    r"\b(value|worth|how much|how many|balance|p\s?&\s?l|pnl|profit|loss|made|earned"
    r"|up or down|sitting at|at now)\b", re.I)

# Anything that makes this more than a question. If he is asking for work to be done,
# even alongside a status question, the model must handle the whole turn.
_HAS_INTENT = re.compile(
    r"\b(make|create|build|publish|post|launch|add|remove|delete|change|set|fix|start|stop"
    r"|pause|resume|buy|sell|send|write|draft|approve|reject|run|turn"
    # Calendar verbs. "schedule" is guarded rather than banned outright, because "what's
    # my schedule today" is the commonest way he asks and is a pure question -- only
    # "schedule a/the/me something" is a request to change the diary.
    r"|book|reschedule|cancel|schedule\s+(?:a|an|the|me|us|it)"
    r"|move\s+(?:the|my))\b", re.I)

# "what is blocked", "what are you waiting on me for", "what do you need"
# Deliberately NOT "on my plate": that means "what do I have to do", which is
# plan_my_day's job and has its own test, not "which mission is stuck".
_BLOCKED = re.compile(
    r"\b(blocked|blocking|stuck|stalled|waiting on me|need from me|needs? me"
    r"|what do you need|waiting for me)\b", re.I)

# Asking about everything at once, with no subject named. Whole phrases only.
_WHOLE_BOARD = re.compile(
    r"(status|sitrep|update|where are we|how are we(?: doing)?|how are things"
    r"|how'?s everything|what'?s the status|what'?s going on|how'?s it (?:all )?going)",
    re.I)

# "what's on my agenda today", "what do I have tomorrow", "am I free Thursday"
#
# This one earns its place twice over. It is a pure read of a table that is already
# correct, so it has no business costing an agentic loop -- and it is the question that
# exposed the whole gap: the work calendar synced fine, the agenda merged it fine, the
# Agenda screen showed it fine, and chat had no tool that could see any of it. He asked
# what was on today and was told about a concert.
# A DAY must be named. This is the guard, not a nicety: an earlier version keyed on
# "what's on" and answered "what's on the shopping list" with "Nothing on today, sir."
# and "what's on my plate today" with an empty calendar. "What's on X" is a question
# about X, and only a time word makes it a question about his diary.
_WHEN = (r"(?:today|tonight|tomorrow|this (?:morning|afternoon|evening|week|weekend)"
         r"|the week|my week|my day|the day|this month"
         r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday)")

_AGENDA = re.compile(
    # "my agenda today", "any meetings tomorrow", "what's my schedule on Friday"
    rf"\b(?:agenda|schedule|calendar|meetings?|appointments?)\b(?=.*\b{_WHEN}\b)"
    # "what's on tomorrow" -- the word after "on" must be the day itself
    rf"|\bwhat(?:'?s| is)?\s+(?:on|up)\s+{_WHEN}\b"
    # "what do I have today", "what have I got on Thursday"
    rf"|\bwhat (?:do i have|have i got)\s+(?:on\s+)?{_WHEN}\b"
    rf"|\banything (?:on|planned)\s+(?:for\s+)?{_WHEN}\b"
    rf"|\bam i (?:free|busy)\b"
    rf"|\b(?:how|what) does (?:my|the) (?:day|week) look\b", re.I)

_TOMORROW = re.compile(r"\btomorrow\b", re.I)
# Any mention of the week at all. An earlier version wanted "this week"/"the week" and
# so answered "what does MY week look like" with today alone.
_WEEK = re.compile(r"\bweek\b", re.I)

# "what are you working on", "what have you done"
_WORKING = re.compile(
    r"\b(what (?:are|r) you (?:working on|doing)|what have you (?:done|been doing)"
    r"|what did you do|what'?s in flight)\b", re.I)


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _amount(value, unit: str) -> str:
    if value is None:
        return "nothing measured yet"
    if unit == "usd":
        return _money(float(value))
    n = float(value)
    return f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"


def _ago(hours: float | None) -> str:
    """Plain English, because 'last movement 31.4h' is a log line, not an answer."""
    if hours is None:
        return "never"
    if hours <= 0:
        # Clock skew between the writer and this reader, or a reading stamped ahead.
        # "-595 minutes ago" is the kind of detail that makes him distrust the whole
        # answer, and "just now" is true enough at that scale.
        return "just now"
    if hours < 1:
        return f"{int(hours * 60)} minutes ago"
    if hours < 48:
        return f"{int(round(hours))} hours ago"
    return f"{int(round(hours / 24))} days ago"


def _describe(m: dict) -> str:
    """One mission, as a person would say it out loud."""
    value = _amount(m.get("current"), m.get("unit") or "count")
    target = _amount(m.get("target"), m.get("unit") or "count")
    head = f"{m['title'].split(':')[0]}: {value}"
    if m.get("target") is not None:
        head += f" of {target}"
    # The measure's own note, when it says something the bare number does not. Live
    # example: the store read "0 of 10", which sounds like a dead shop, while the note
    # read "0 live, 3 waiting to go live" -- the factory works and the door is shut.
    note = (m.get("note") or "").strip()
    if note and note.split(",")[0].strip() != value:
        head += f" ({note})"

    if m.get("current") is None:
        return head + ". Nothing measured yet."

    moved = _ago(m.get("hours_since_movement"))
    if not m.get("stalled"):
        return f"{head}. Last moved {moved}."

    parts = [f"{head}. Hasn't moved in {moved.replace(' ago', '')}"]
    blocker = m.get("blocker")
    if blocker:
        why = (blocker.get("last_reason") or "").rstrip(".")
        parts.append(f"{blocker['agent']} is the hold-up" + (f" -- {why}" if why else ""))
    tried = [i for i in (m.get("interventions") or []) if i.get("action") != "escalated"]
    if tried:
        last = tried[0]
        outcome = "it worked" if last.get("outcome") else "no change yet"
        parts.append(f"I {last['action']} already; {outcome}")
    return ". ".join(parts) + "."


def _subject_in(text: str) -> str | None:
    """The mission he is asking about, longest name first so 'paper trading' beats
    'trading' and 'jarvis store' beats 'store'."""
    low = text.lower()
    for name in sorted(SUBJECTS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", low):
            return SUBJECTS[name]
    return None


def _day_label(day_date: str, today: str) -> str:
    from datetime import date, timedelta
    try:
        d = date.fromisoformat(day_date)
        t = date.fromisoformat(today)
    except ValueError:
        return day_date
    if d == t:
        return "Today"
    if d == t + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%A")


def _agenda_answer(db_path: str, text: str) -> str | None:
    """His day, read straight out of the merged agenda. None if it cannot be read."""
    try:
        from . import agenda
    except Exception:
        return None

    from datetime import date, timedelta

    if _WEEK.search(text):
        span, window = 7, "this week"
    elif _TOMORROW.search(text):
        span, window = 2, "tomorrow"
    else:
        span, window = 1, "today"

    try:
        data = agenda.upcoming(db_path, 1, days=span, back_days=0)
    except Exception:
        logger.debug("direct answer: agenda read failed, falling back", exc_info=True)
        return None

    today = data.get("today") or date.today().isoformat()
    # Pick the dates explicitly rather than trusting the window the call came back with.
    # Asking about today and being handed tomorrow's meetings too is its own small lie.
    try:
        start = date.fromisoformat(today)
    except ValueError:
        return None
    if window == "tomorrow":
        wanted_dates = {(start + timedelta(days=1)).isoformat()}
    elif window == "this week":
        wanted_dates = {(start + timedelta(days=i)).isoformat() for i in range(7)}
    else:
        wanted_dates = {today}

    lines = []
    for day in (data.get("days") or []):
        if day.get("date") not in wanted_dates:
            continue
        entries = day.get("entries") or []
        if not entries:
            continue

        def at(entry):
            """Sort by the clock. The agenda groups by kind, which puts a 16:00 meetup
            above an 08:00 standup -- fine on a screen with columns, wrong when it is
            read aloud or arrives as a text."""
            found = re.match(r"^(\d{1,2}):(\d{2})", (entry.get("detail") or "").strip())
            return (0, int(found.group(1)), int(found.group(2))) if found else (1, 0, 0)

        lines.append(f"{_day_label(day.get('date', ''), today)}:")
        for e in sorted(entries, key=at):
            when = (e.get("detail") or "").split("·")[0].strip()
            head = f"{when} " if re.match(r"^\d{1,2}:\d{2}", when) else ""
            kind = "" if e.get("kind") == "event" else f"{e.get('kind')}: "
            # Task titles here are whole paragraphs of reasoning. Useful on the task
            # itself, unreadable in a list of what his day holds.
            title = (e.get("title") or "").strip()
            if len(title) > 72:
                title = title[:69].rstrip(" ,.-") + "..."
            lines.append(f"- {head}{kind}{title}")
    if not lines:
        return f"Nothing on {window}, sir."
    return "\n".join(lines)


def try_direct_answer(db_path: str, user_text: str) -> str | None:
    """A finished reply, or None to let the normal path handle the turn untouched."""
    text = (user_text or "").strip()
    # A long message is a conversation, not a lookup, whatever words are in it.
    if not text or len(text) > 160:
        return None
    if _HAS_INTENT.search(text):
        return None

    if _AGENDA.search(text):
        answer = _agenda_answer(db_path, text)
        if answer is not None:
            return answer

    try:
        from . import missions
        snapshot = missions.snapshot(db_path)
    except Exception:
        # Never let a lookup failure swallow the turn -- fall through to the real path.
        logger.debug("direct answer: snapshot failed, falling back", exc_info=True)
        return None
    if not snapshot:
        return None

    key = _subject_in(text)
    if key is not None and (_STATUS.search(text) or _VALUE.search(text)):
        m = next((x for x in snapshot if x["key"] == key), None)
        return _describe(m) if m else None

    if _BLOCKED.search(text):
        stalled = [m for m in snapshot if m.get("stalled")]
        if not stalled:
            return "Nothing's blocked on you, sir. Everything's moving."
        lines = [_describe(m) for m in stalled]
        return "Waiting on you:\n" + "\n".join(f"- {line}" for line in lines)

    if _WORKING.search(text):
        acted = [(m, i) for m in snapshot for i in (m.get("interventions") or [])
                 if i.get("action") != "escalated"][:4]
        if not acted:
            return "Nothing needed doing, sir -- every mission's moving on its own."
        return "Lately:\n" + "\n".join(
            f"- {m['title'].split(':')[0]}: {i['action']}"
            f"{' -- ' + i['outcome'] if i.get('outcome') else ' (no change yet)'}"
            for m, i in acted)

    # A bare "status" / "where are we": the whole board. Matched as whole phrases, never
    # as "short and status-shaped" -- that rule answered "how is the greenhouse doing"
    # with the store and the crypto desk, which is the confidently-wrong failure this
    # module is most obliged to avoid. An unrecognised subject must reach the model.
    if _WHOLE_BOARD.fullmatch(text.strip(" .?!")):
        return "\n".join(f"- {_describe(m)}" for m in snapshot)

    return None
