"""The shape of a day: what is anchored to a clock, what is slipping, and what to pick up.

Built for one person with ADHD, which changes the design rather than just the wording:

**Nothing is presented as a flat list.** Thirty-three open tasks sorted by date is a wall,
and a wall gets closed. `plan_day` returns at most a handful to choose from, with the
reason each one surfaced attached, so choosing is a decision rather than a search.

**Blocked work is never offered.** "I can't book the trip until Ghost is situated with a
vet and a boarding facility" -- a list that offers the flight anyway is training him to
distrust the list. Blocked tasks are held back and reported as blocked, with the thing
they are waiting on named.

**Age is a first-class signal.** A task that has sat for three weeks is not low priority,
it is avoided, and the usual reason is that it has no obvious first move. Surfacing it by
age is how it stops being invisible.

**Missing something is data, not failure.** rhythm_log records skips as real answers, so
"you have skipped the bike four days running" can be said once at planning time instead of
the same nudge firing uselessly each evening.

Times are local throughout -- a rhythm is lived in local time, and a 06:45 that drifts with
UTC is worse than useless.
"""
import logging
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# How the weight is built. Each component is bounded so no single one can dominate: a
# task three months old must not outrank one that is due today and blocking two others.
AGE_WEIGHT_PER_DAY = 1.5
AGE_WEIGHT_CAP = 45.0          # ~30 days of ageing, then it stops climbing
OVERDUE_WEIGHT = 60.0
DUE_TODAY_WEIGHT = 45.0
DUE_SOON_WEIGHT = 20.0         # within DUE_SOON_DAYS
DUE_SOON_DAYS = 3
BLOCKS_OTHERS_WEIGHT = 25.0    # per task waiting on this one
BLOCKS_OTHERS_CAP = 50.0
PRIORITY_WEIGHT = {"high": 30.0, "normal": 0.0, "low": -15.0}
DOING_WEIGHT = 35.0            # already started; finishing beats starting something new

# A habit is slipping once it has gone this many times its own interval without happening.
# Scaled rather than fixed: four times a week means every ~1.75 days, so three quiet days
# is a real gap, while once a week means every seven and three days is nothing. A flat
# threshold nags about one and stays silent about the other.
SLIPPING_MULTIPLIER = 1.5

# How long a nudge stays sendable once its moment arrives. Only matters for lead_minutes
# of 0 -- "wake up" and "bed" have no lead, so their send window is a single instant and
# a scheduler that ticks once a minute would land on it only by luck. Anything with a real
# lead keeps the whole run-up as its window instead.
NUDGE_GRACE_MINUTES = 5


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _now_local(tz_name: str) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now()


def _days_between(earlier: str | None, today: date) -> int | None:
    if not earlier:
        return None
    try:
        return (today - date.fromisoformat(str(earlier)[:10])).days
    except ValueError:
        return None


# --- tasks ------------------------------------------------------------------------

def blockers_for(db_path: str, task_ids: list[int]) -> dict:
    """Which unfinished tasks each task is waiting on, by id.

    Only unfinished ones count: once the vet task is done it stops blocking the flight,
    and the flight should appear in tomorrow's plan without anybody editing a link.
    """
    if not task_ids:
        return {}
    marks = ",".join("?" * len(task_ids))
    out: dict[int, list[dict]] = {}
    with closing(_connect(db_path)) as conn:
        for row in conn.execute(
                f"SELECT b.task_id, t.id, t.text, t.status FROM task_blockers b "
                f"JOIN personal_tasks t ON t.id = b.blocked_by_id "
                f"WHERE b.task_id IN ({marks}) AND t.status NOT IN ('done', 'dropped')",
                task_ids):
            out.setdefault(row["task_id"], []).append(
                {"id": row["id"], "text": row["text"], "status": row["status"]})
    return out


def blocking_counts(db_path: str, task_ids: list[int]) -> dict:
    """How many unfinished tasks are waiting on each of these. A task that unblocks two
    others is worth more than its own description suggests."""
    if not task_ids:
        return {}
    marks = ",".join("?" * len(task_ids))
    with closing(_connect(db_path)) as conn:
        return {row["blocked_by_id"]: row["n"] for row in conn.execute(
            f"SELECT b.blocked_by_id, COUNT(*) n FROM task_blockers b "
            f"JOIN personal_tasks t ON t.id = b.task_id "
            f"WHERE b.blocked_by_id IN ({marks}) AND t.status NOT IN ('done', 'dropped') "
            f"GROUP BY b.blocked_by_id", task_ids)}


def weigh_task(task: dict, today: date, blocks_count: int = 0) -> dict:
    """What this task is worth picking up today, and why.

    Returns the reasons alongside the number on purpose. A score with no explanation is
    something to argue with; "sat for 10 days, unblocks 2 others" is something to act on,
    and it is also how a wrong weight becomes visible instead of just feeling off.
    """
    score, reasons = 0.0, []

    age = _days_between(task.get("created_at"), today)
    if age and age > 0:
        aged = min(age * AGE_WEIGHT_PER_DAY, AGE_WEIGHT_CAP)
        score += aged
        if age >= 7:
            reasons.append(f"sat for {age} days")

    # Positive means the date has passed. _days_between measures today MINUS the date,
    # so this is already "days overdue" and must not be negated again.
    overdue_by = _days_between(task.get("due_at"), today)
    if overdue_by is not None:
        if overdue_by > 0:
            score += OVERDUE_WEIGHT
            reasons.append(f"overdue by {overdue_by}d")
        elif overdue_by == 0:
            score += DUE_TODAY_WEIGHT
            reasons.append("due today")
        elif -overdue_by <= DUE_SOON_DAYS:
            score += DUE_SOON_WEIGHT
            reasons.append(f"due in {-overdue_by}d")

    if blocks_count:
        score += min(blocks_count * BLOCKS_OTHERS_WEIGHT, BLOCKS_OTHERS_CAP)
        reasons.append(f"unblocks {blocks_count} other" + ("s" if blocks_count > 1 else ""))

    score += PRIORITY_WEIGHT.get(task.get("priority") or "normal", 0.0)
    if (task.get("priority") or "") == "high":
        reasons.append("high priority")

    if task.get("status") == "doing":
        score += DOING_WEIGHT
        reasons.append("already started")

    return {"score": round(score, 1), "reasons": reasons}


def shortlist(db_path: str, owner_user_id: int, today: date, limit: int = 5,
              track: str | None = "personal") -> dict:
    """The few tasks worth choosing between today, and the ones that are blocked.

    Deliberately short. The point is to make choosing possible, and a list long enough to
    need scrolling has already failed at that.

    `track` defaults to personal, which is the whole reason the column exists: "wake-word
    arbitration" and "find a vet for Ghost" are not comparable, and ranking them against
    each other produces a list that is useless for planning either kind of day. Pass None
    to weigh both together, or 'project' for the build side.
    """
    query = ("SELECT * FROM personal_tasks WHERE owner_user_id = ? "
             "AND status IN ('open', 'doing')")
    params: list = [owner_user_id]
    if track is not None:
        query += " AND track = ?"
        params.append(track)
    with closing(_connect(db_path)) as conn:
        tasks = [dict(r) for r in conn.execute(query, params)]
    if not tasks:
        return {"pick_from": [], "blocked": [], "open_count": 0}

    ids = [t["id"] for t in tasks]
    waiting_on = blockers_for(db_path, ids)
    unblocks = blocking_counts(db_path, ids)

    ready, blocked = [], []
    for task in tasks:
        holders = waiting_on.get(task["id"], [])
        weighed = weigh_task(task, today, unblocks.get(task["id"], 0))
        entry = {**task, **weighed}
        if holders:
            blocked.append({**entry, "waiting_on": holders})
        else:
            ready.append(entry)

    ready.sort(key=lambda t: t["score"], reverse=True)
    blocked.sort(key=lambda t: t["score"], reverse=True)
    return {"pick_from": ready[:limit], "blocked": blocked,
            "open_count": len(tasks), "ready_count": len(ready)}


# --- the rhythm -------------------------------------------------------------------

def _runs_today(rhythm: dict, today: date) -> bool:
    days = (rhythm.get("days") or "").strip()
    if not days:
        return True
    try:
        return today.weekday() in {int(d) for d in days.split(",") if d.strip() != ""}
    except ValueError:
        return True


def _log_index(conn, rhythm_ids: list[int], since: str) -> dict:
    if not rhythm_ids:
        return {}
    marks = ",".join("?" * len(rhythm_ids))
    out: dict[int, list[dict]] = {}
    for row in conn.execute(
            f"SELECT rhythm_id, on_date, state FROM rhythm_log "
            f"WHERE rhythm_id IN ({marks}) AND on_date >= ? ORDER BY on_date DESC",
            [*rhythm_ids, since]):
        out.setdefault(row["rhythm_id"], []).append(dict(row))
    return out


def rhythm_status(db_path: str, owner_user_id: int, today: date) -> dict:
    """Today's anchors and how every habit is tracking against its own target."""
    since = (today - timedelta(days=30)).isoformat()
    with closing(_connect(db_path)) as conn:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM day_rhythm WHERE owner_user_id = ? AND enabled = 1 "
            "ORDER BY kind, at_time IS NULL, at_time, name", (owner_user_id,))]
        logs = _log_index(conn, [i["id"] for i in items], since)

    week_start = (today - timedelta(days=today.weekday())).isoformat()
    anchors, habits = [], []
    for item in items:
        entries = logs.get(item["id"], [])
        done_dates = [e["on_date"] for e in entries if e["state"] == "done"]
        last_done = done_dates[0] if done_dates else None
        today_entry = next((e for e in entries if e["on_date"] == today.isoformat()), None)
        common = {
            **item,
            "last_done_on": last_done,
            "days_since": _days_between(last_done, today),
            "today_state": today_entry["state"] if today_entry else None,
        }
        if item["kind"] == "anchor":
            anchors.append({**common, "due_today": _runs_today(item, today)})
        else:
            this_week = sum(1 for d in done_dates if d >= week_start)
            target = item.get("target_per_week") or 0
            # Slipping is measured against the item's own rate, not a fixed number of
            # days: twice a week going quiet for three days is normal, four times a week
            # going quiet for three days is not.
            gap_allowed = (7 / target if target else 7) * SLIPPING_MULTIPLIER
            since_last = common["days_since"]
            habits.append({
                **common,
                "this_week": this_week,
                "target_per_week": target,
                "remaining_this_week": max(0, target - this_week) if target else None,
                "slipping": bool(since_last is None or since_last > gap_allowed),
            })
    return {"anchors": anchors, "habits": habits}


def plan_day(db_path: str, owner_user_id: int, tz_name: str = "America/New_York",
             today: date | None = None, limit: int = 5,
             track: str | None = "personal") -> dict:
    """Everything the 'plan my day' routine needs, as data.

    One call rather than three, because the three answers are related: what is already
    committed to the clock bounds how much of the task shortlist is realistic, and what is
    slipping is the reason to spend one of today's slots on something that has no deadline
    at all.
    """
    today = today or _now_local(tz_name).date()
    rhythm = rhythm_status(db_path, owner_user_id, today)
    tasks = shortlist(db_path, owner_user_id, today, limit=limit, track=track)
    slipping = [h for h in rhythm["habits"] if h["slipping"]]

    # What he already committed to today, lifted out of the shortlist so the plan reads as
    # "here is your day" rather than re-offering him things he has already chosen.
    from . import personal_db
    picked_ids = personal_db.picks_for_day(db_path, owner_user_id, today.isoformat())
    by_id = {t["id"]: t for t in tasks["pick_from"] + tasks["blocked"]}
    picked = [by_id[i] for i in picked_ids if i in by_id]
    tasks["pick_from"] = [t for t in tasks["pick_from"] if t["id"] not in set(picked_ids)]

    details = personal_db.list_task_details(
        db_path, [t["id"] for t in picked + tasks["pick_from"]])
    for task in picked + tasks["pick_from"] + tasks["blocked"]:
        task["details"] = details.get(task["id"], [])

    return {
        "date": today.isoformat(),
        "weekday": today.strftime("%A"),
        "anchors": [a for a in rhythm["anchors"] if a["due_today"]],
        "habits": rhythm["habits"],
        "slipping": slipping,
        "picked": picked,
        **tasks,
    }


# --- the week, and tomorrow --------------------------------------------------------

def week_shape(db_path: str, owner_user_id: int, start: date, days: int = 7) -> list[dict]:
    """One row per day in the strip: how loaded it is and what's on it, so a day can be
    picked without leaving today's view. Loops rhythm_status rather than a single wider
    query -- same multi-day-grouped shape as agenda.upcoming(), and rhythm_status already
    does the "what's due, what's slipping" work per day; a week is seven of those."""
    out = []
    for i in range(days):
        day = start + timedelta(days=i)
        rhythm = rhythm_status(db_path, owner_user_id, day)
        anchors = [a for a in rhythm["anchors"] if a["due_today"]]
        out.append({
            "date": day.isoformat(),
            "weekday": day.strftime("%a"),
            "day_num": day.day,
            "anchor_count": len(anchors),
            "hard_count": sum(1 for a in anchors if a.get("hard")),
            "done_count": sum(1 for a in anchors if a.get("today_state") == "done"),
            "slipping_count": sum(1 for h in rhythm["habits"] if h["slipping"]),
        })
    return out


def tomorrow_brief(db_path: str, owner_user_id: int, today: date,
                   tz_name: str = "America/New_York") -> dict:
    """First-up, cannot-slip, carries-over, prep-tonight -- everything tomorrow already
    knows before it arrives.

    Carries over is today's picks that never got marked done, diffed against tomorrow's
    own plan so nothing shows up twice. Prep tonight reuses task_details: a 'note' detail
    labelled 'Prep tonight' on a task carrying into tomorrow, the same convention
    task_note() already uses for labelled detail lines -- no schema change needed for one
    more label.
    """
    from . import personal_db
    tomorrow = today + timedelta(days=1)
    brief = plan_day(db_path, owner_user_id, tz_name=tz_name, today=tomorrow)

    today_picks = personal_db.picks_for_day(db_path, owner_user_id, today.isoformat())
    with closing(_connect(db_path)) as conn:
        undone = [dict(r) for r in conn.execute(
            f"SELECT * FROM personal_tasks WHERE id IN "
            f"({','.join('?' * len(today_picks))}) AND status NOT IN ('done', 'dropped')",
            today_picks)] if today_picks else []
    # Only dedupe against what's explicitly picked for tomorrow already -- pick_from is
    # the ranked shortlist of every open task, which would include this one trivially and
    # hide it from "carries over" for no reason.
    already_tomorrow = {t["id"] for t in brief["picked"]}
    carries_over = [t for t in undone if t["id"] not in already_tomorrow]

    prep = []
    all_ids = [t["id"] for t in brief["picked"] + carries_over]
    if all_ids:
        details = personal_db.list_task_details(db_path, all_ids)
        for task_id, items in details.items():
            for d in items:
                if d["kind"] == "note" and (d.get("label") or "").strip().lower() == "prep tonight":
                    prep.append({"task_id": task_id, "text": d["value"]})

    hard_anchors = [a for a in brief["anchors"] if a.get("hard")]
    return {
        "date": brief["date"],
        "weekday": brief["weekday"],
        "anchors": brief["anchors"],
        "cannot_slip": hard_anchors,
        "carries_over": carries_over,
        "prep_tonight": prep,
    }


# --- what to text, and when -------------------------------------------------------

def due_nudges(db_path: str, owner_user_id: int, tz_name: str = "America/New_York",
               now: datetime | None = None) -> list[dict]:
    """Nudges that should go out right now and have not already been sent today.

    Lead nudges only -- "leave in 15 minutes" is actionable, "you were supposed to leave
    20 minutes ago" is just a reproach, so a missed window is dropped rather than sent
    late. A slipping habit gets one nudge a day at most, and only in the evening, because
    telling him at 07:00 that he has not ridden the bike is noise he cannot act on yet.
    """
    now = now or _now_local(tz_name)
    today = now.date()
    out = []
    with closing(_connect(db_path)) as conn:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM day_rhythm WHERE owner_user_id = ? AND enabled = 1",
            (owner_user_id,))]
        sent = {(r["rhythm_id"], r["kind"]) for r in conn.execute(
            "SELECT rhythm_id, kind FROM rhythm_nudges WHERE on_date = ?", (today.isoformat(),))}
        done = {r["rhythm_id"] for r in conn.execute(
            "SELECT rhythm_id FROM rhythm_log WHERE on_date = ?", (today.isoformat(),))}

    for item in items:
        if item["id"] in done:
            continue
        if item["kind"] == "anchor" and item["at_time"] and _runs_today(item, today):
            lead = item["lead_minutes"]
            if lead is None:
                continue
            try:
                hour, minute = (int(x) for x in item["at_time"].split(":")[:2])
            except ValueError:
                continue
            when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            fire_at = when - timedelta(minutes=lead)
            # A window, not an instant: the scheduler ticks on an interval, so an exact
            # comparison would miss most nudges entirely. For a lead of 0 the run-up is
            # empty, so a short grace after the time is the window instead -- otherwise
            # "wake up at 06:45" has a zero-width window and never fires at all.
            window_end = max(when, fire_at + timedelta(minutes=NUDGE_GRACE_MINUTES))
            if fire_at <= now < window_end and (item["id"], "lead") not in sent:
                minutes_left = max(0, int((when - now).total_seconds() // 60))
                out.append({"rhythm_id": item["id"], "kind": "lead", "name": item["name"],
                            "at_time": item["at_time"], "minutes_left": minutes_left,
                            "hard": bool(item["hard"]), "category": item["category"],
                            "text": _lead_text(item, minutes_left)})
    return out


def _lead_text(item: dict, minutes_left: int) -> str:
    when = "now" if minutes_left <= 1 else f"in {minutes_left} min"
    if item["category"] == "work" and "leave" in item["name"].lower():
        return f"{item['name']} {when} — {item['at_time']}."
    return f"{item['name']} {when} ({item['at_time']})."


def record_nudge(db_path: str, rhythm_id: int, kind: str, on_date: str) -> bool:
    """Marks a nudge as sent. Returns False if it had already gone out, so a caller that
    races another tick sends nothing rather than texting him twice."""
    with closing(_connect(db_path)) as conn:
        try:
            conn.execute(
                "INSERT INTO rhythm_nudges (rhythm_id, on_date, kind, sent_at) VALUES (?, ?, ?, ?)",
                (rhythm_id, on_date, kind, datetime.now().isoformat()))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def send_due_nudges(db_path: str, owner_user_id: int, sms_number: str | None = None,
                    notify=None, tz_name: str = "America/New_York",
                    now: datetime | None = None) -> list[dict]:
    """Texts whatever is due right now. Returns what actually went out.

    SMS first and by design: he asked to be reminded "via text messages of when I need to
    leave", and a push notification that lands behind a lock screen he is not looking at
    is not the same thing. `notify` is the fallback for when the modem is unreachable --
    a missed "leave for work" is worse than one delivered the ordinary way.

    Each nudge is claimed before it is sent, not after. A send that fails after the claim
    costs one missed reminder; claiming after sending would risk texting him the same
    thing on every scheduler tick, which is how a person mutes an assistant.
    """
    sent = []
    for nudge in due_nudges(db_path, owner_user_id, tz_name=tz_name, now=now):
        on_date = (now or _now_local(tz_name)).date().isoformat()
        if not record_nudge(db_path, nudge["rhythm_id"], nudge["kind"], on_date):
            continue
        delivered = False
        if sms_number:
            try:
                from . import cellular
                cellular.queue_outbound(db_path, sms_number, nudge["text"])
                delivered = True
            except Exception:
                logger.exception("could not queue nudge SMS for rhythm %s", nudge["rhythm_id"])
        if not delivered and notify is not None:
            try:
                notify(nudge["text"])
                delivered = True
            except Exception:
                logger.exception("could not deliver nudge for rhythm %s", nudge["rhythm_id"])
        sent.append({**nudge, "delivered": delivered})
    return sent


# --- getting it onto his phone ----------------------------------------------------

# How each kind of detail is introduced in a calendar note. Labelled rather than bare so a
# list of three phone numbers is readable at arm's length, standing in a car park.
_DETAIL_PREFIX = {
    "phone": "Call",
    "address": "Go to",
    "person": "Ask for",
    "link": "Link",
    "note": "",
}


def task_note(task: dict, details: list[dict], blockers: list[dict] | None = None) -> str:
    """Everything needed to actually do this task, as plain text for a calendar note.

    His phone is where he looks when he is out, and the calendar is already synced there.
    An event that is only a title sends him back to a laptop to find the vet's number,
    which defeats the point of putting it in the calendar at all.

    Plain text, not markdown: iOS renders calendar notes as-is, so asterisks would show up
    as asterisks. Phone numbers are left exactly as written because iOS linkifies them
    itself, and reformatting them is how that stops working.
    """
    lines = []
    for detail in details or []:
        prefix = _DETAIL_PREFIX.get(detail.get("kind"), "")
        label = (detail.get("label") or "").strip()
        value = (detail.get("value") or "").strip()
        if not value:
            continue
        head = " ".join(part for part in (prefix, label) if part)
        lines.append(f"{head}: {value}" if head else value)

    if blockers:
        lines.append("")
        lines.append("Waiting on: " + "; ".join(b["text"] for b in blockers))

    if task.get("due_at"):
        lines.append("")
        lines.append(f"Due {str(task['due_at'])[:10]}")

    return "\n".join(lines).strip()


def task_note_for(db_path: str, task_id: int, owner_user_id: int) -> str:
    """task_note for a stored task, with its details and blockers loaded."""
    from . import personal_db
    task = next((t for t in personal_db.list_tasks(db_path, owner_user_id)
                 if t["id"] == task_id), None)
    if task is None:
        return ""
    details = personal_db.list_task_details(db_path, [task_id]).get(task_id, [])
    blockers = blockers_for(db_path, [task_id]).get(task_id, [])
    return task_note(task, details, blockers)
