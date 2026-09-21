"""Everything with a date on it, in one place.

The dated facts of this system live in six different tables and one remote calendar, and
until now each had its own screen. That is fine for asking "what bills are due" and useless
for the question Jack actually has -- *what is coming*. Rent, a dispute deadline, a payday,
a task from the financial plan and a doctor's appointment all land on the same week and only
matter relative to one another.

So this collects, it does not own. Every source keeps its own table and its own screen;
this reads them and returns one ordered list. Nothing here writes, which is what makes it
safe to add a source: the worst a broken one can do is contribute nothing.

That last point is enforced rather than hoped for. Each source is gathered inside its own
try, because the failure that matters here is one flaky source -- a CalDAV server on a bad
connection -- blanking a calendar that would otherwise have shown rent and a payoff
deadline. A missing source is reported in `unavailable` so the UI can say which, rather
than quietly showing a lighter week than the one he has.
"""
import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# What each kind means, and the order they sort in when they fall on the same day. Money
# leaving the account outranks a reminder to buy milk.
KINDS = ("bill", "income", "deadline", "task", "reminder", "event")
_KIND_RANK = {k: i for i, k in enumerate(KINDS)}


def _as_date(value) -> date | None:
    """A date from whatever a table happens to store: ISO date, ISO timestamp, or None."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for parse in (lambda t: date.fromisoformat(t[:10]),
                  lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")).date()):
        try:
            return parse(text)
        except (ValueError, TypeError):
            continue
    return None



def _table_missing(exc) -> bool:
    """Whether a database error is simply "that feature is not in use here".

    A table that has never been created is not a broken source -- the email-bill scanner
    may never have run, and saying "email_bills unavailable" on a fresh install would train
    him to ignore the one warning that matters. A real fault (a locked or corrupt database)
    still reports.
    """
    import sqlite3

    return isinstance(exc, sqlite3.OperationalError) and "no such table" in str(exc).lower()


def _entry(kind, when, title, *, detail=None, amount=None, source=None, ref=None,
           done=False, urgent=False) -> dict:
    return {"kind": kind, "date": when.isoformat(), "title": title, "detail": detail,
            "amount": amount, "source": source, "ref": ref, "done": done, "urgent": urgent}


def _tasks(db_path: str, owner_user_id: int, start: date, end: date) -> list:
    from . import personal_db

    out = []
    for task in personal_db.list_tasks(db_path, owner_user_id):
        when = _as_date(task.get("due_at"))
        if when is None or not (start <= when <= end):
            continue
        status = (task.get("status") or "open").lower()
        if status in ("done", "dropped"):
            continue
        out.append(_entry("task", when, task.get("text") or "(untitled task)",
                          source="tasks", ref=task.get("id"),
                          urgent=(task.get("priority") == "high")))
    return out


def _reminders(db_path: str, owner_user_id: int, start: date, end: date) -> list:
    """His reminders, minus the ones a bill already speaks for.

    When Jarvis finds a bill in his inbox it also files a reminder for it, so the same
    Shopify invoice arrived on the calendar twice -- once as a bill carrying the amount,
    once as a reminder that does not. The bill entry wins; a duplicate on a calendar is
    worse than useless because it reads as two payments.
    """
    import sqlite3
    from contextlib import closing

    from . import db as core_db

    linked = set()
    conn = sqlite3.connect(db_path, timeout=30)
    with closing(conn):
        try:
            linked = {row[0] for row in conn.execute(
                "SELECT reminder_id FROM email_bills WHERE reminder_id IS NOT NULL")}
        except sqlite3.Error:
            pass

    out = []
    for r in core_db.list_reminders(db_path, owner_user_id):
        if r.get("id") in linked:
            continue
        when = _as_date(r.get("due_at"))
        if when is None or not (start <= when <= end):
            continue
        out.append(_entry("reminder", when, r.get("text") or "(reminder)",
                          source="reminders", ref=r.get("id")))
    return out


def _recurring(db_path: str, owner_user_id: int, start: date, end: date) -> list:
    """Bills and paychecks, projected across the window by finance.expand_occurrences.

    Deliberately NOT stepped by a day count here. finance.py already knows that "the 15th"
    clamps in February and that "last day of month" is not every 30 days, and a second
    implementation of that would drift against the projection the rest of the app runs on
    -- two screens disagreeing about when rent is due is worse than one screen missing it.

    Reads manual_recurring_charges, which is where the real commitments live: Era's own
    cache holds eight rows and excludes payroll, so a calendar built from it alone showed
    no rent, no car payment and no wages.
    """
    import sqlite3
    from contextlib import closing

    from . import finance

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    with closing(conn):
        try:
            charges = [dict(r) for r in conn.execute(
                "SELECT * FROM manual_recurring_charges WHERE owner_user_id = ?",
                (owner_user_id,))]
        except sqlite3.Error as exc:
            if not _table_missing(exc):
                raise
            charges = []
        try:
            charges += [dict(r) for r in conn.execute(
                "SELECT * FROM era_recurring_charge_cache WHERE COALESCE(excluded, 0) = 0")]
        except sqlite3.Error:
            pass

    out = []
    for occurrence in finance.expand_occurrences(charges, start, end):
        when = _as_date(occurrence.get("date") or occurrence.get("when"))
        if when is None:
            continue
        amount = float(occurrence.get("amount") or 0)
        direction = (occurrence.get("direction") or "").lower()
        inbound = direction in ("in", "income", "credit", "inflow") or (
            not direction and amount > 0)
        out.append(_entry("income" if inbound else "bill", when,
                          occurrence.get("description") or "(recurring)",
                          amount=abs(amount), source="recurring",
                          detail=occurrence.get("cadence")))
    return out


def _email_bills(db_path: str, owner_user_id: int, start: date, end: date) -> list:
    """Bills Jarvis found in his inbox and he confirmed.

    A separate source from the recurring commitments on purpose: these are one specific
    invoice with one specific due date rather than a standing arrangement, and they are
    the ones that surprise people.
    """
    import sqlite3
    from contextlib import closing

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    with closing(conn):
        try:
            rows = [dict(r) for r in conn.execute(
                """SELECT payee, amount, due_date, status FROM email_bills
                    WHERE owner_user_id = ? AND due_date IS NOT NULL
                      AND COALESCE(status, '') != 'dismissed'""", (owner_user_id,))]
        except sqlite3.Error as exc:
            if _table_missing(exc):
                return []
            raise
    out = []
    for row in rows:
        when = _as_date(row["due_date"])
        if when is None or not (start <= when <= end):
            continue
        out.append(_entry("bill", when, f"{row['payee'] or 'bill'} (from email)",
                          amount=float(row["amount"]) if row["amount"] else None,
                          source="email_bills",
                          detail=None if row["status"] == "confirmed" else row["status"]))
    return out


def _credit_deadlines(db_path: str, owner_user_id: int, start: date, end: date) -> list:
    """Dispute response windows -- the only dates here with a legal clock attached.

    A window that has CLOSED is the most valuable item on the calendar, because the bureau
    is then obliged to act, so an overdue one is kept in the list and marked urgent rather
    than filtered out with the past.
    """
    from . import credit

    out = []
    for dispute in credit.open_disputes(db_path, owner_user_id):
        deadline = dispute.get("deadline") or dispute.get("response_deadline")
        when = _as_date(deadline.get("due_on") if isinstance(deadline, dict) else deadline)
        if when is None or when > end:
            continue
        overdue = when < date.today()
        if when < start and not overdue:
            continue
        out.append(_entry(
            "deadline", when,
            f"{dispute.get('bureau', '?')}: {dispute.get('creditor_name') or 'dispute'}",
            detail=("response overdue -- the bureau is now obliged to act" if overdue
                    else "bureau response due"),
            source="disputes", ref=dispute.get("id"), urgent=True))
    return out


def _calendar(calendar_ctx, start: date, end: date) -> list:
    """His real calendar, over CalDAV."""
    if calendar_ctx is None:
        return []
    client = getattr(calendar_ctx, "client", None) or getattr(calendar_ctx, "mcp_client", None)
    urls = getattr(calendar_ctx, "calendar_urls", None) or []
    if client is None or not urls:
        return []
    out = []
    begin = datetime.combine(start, datetime.min.time())
    finish = datetime.combine(end, datetime.max.time())
    for url in urls:
        for event in client.list_events(url, begin, finish) or []:
            when = _as_date(event.get("start") or event.get("dtstart"))
            if when is None:
                continue
            out.append(_entry("event", when, event.get("summary") or "(event)",
                              detail=event.get("location"), source="calendar"))
    return out


def upcoming(db_path: str, owner_user_id: int, *, days: int = 45, back_days: int = 7,
             calendar_ctx=None) -> dict:
    """Every dated thing in the window, oldest first, grouped by day.

    `back_days` exists so a missed bill or a task that came due yesterday is still on the
    screen. A calendar that hides what was just missed is a calendar that lets it stay
    missed.
    """
    today = date.today()
    start = today - timedelta(days=max(0, back_days))
    end = today + timedelta(days=max(1, days))

    entries, unavailable = [], []
    sources = (
        ("tasks", lambda: _tasks(db_path, owner_user_id, start, end)),
        ("reminders", lambda: _reminders(db_path, owner_user_id, start, end)),
        ("recurring", lambda: _recurring(db_path, owner_user_id, start, end)),
        ("email_bills", lambda: _email_bills(db_path, owner_user_id, start, end)),
        ("disputes", lambda: _credit_deadlines(db_path, owner_user_id, start, end)),
        ("calendar", lambda: _calendar(calendar_ctx, start, end)),
    )
    for name, gather in sources:
        try:
            entries.extend(gather())
        except Exception as exc:                          # noqa: BLE001
            # One flaky source must never blank the calendar. Naming it beats a lighter
            # week than the one he actually has.
            logger.warning("agenda source %s failed: %s", name, exc)
            unavailable.append({"source": name, "error": f"{type(exc).__name__}: {exc}"[:160]})

    entries.sort(key=lambda e: (e["date"], _KIND_RANK.get(e["kind"], 99), e["title"]))

    days_out: dict = {}
    for entry in entries:
        days_out.setdefault(entry["date"], []).append(entry)

    money_in = sum(e["amount"] or 0 for e in entries
                   if e["kind"] == "income" and e["date"] >= today.isoformat())
    money_out = sum(e["amount"] or 0 for e in entries
                    if e["kind"] == "bill" and e["date"] >= today.isoformat())
    return {
        "today": today.isoformat(),
        "from": start.isoformat(), "to": end.isoformat(),
        "days": [{"date": d, "entries": days_out[d]} for d in sorted(days_out)],
        "counts": {k: sum(1 for e in entries if e["kind"] == k) for k in KINDS},
        "money_in": round(money_in, 2),
        "money_out": round(money_out, 2),
        "overdue": [e for e in entries if e["date"] < today.isoformat() and not e["done"]],
        "unavailable": unavailable,
    }
