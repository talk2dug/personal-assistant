"""One aggregated read for the Command Center board.

The board is a single screen showing a dozen different things at once, refreshed every
few seconds. Fetching each panel from its own endpoint would mean ten-plus round trips
per tick, ten sets of loading states, and ten chances for one slow integration to make
the whole screen look broken. So the cheap, local, SQLite-backed panels are assembled
here in one pass and returned together.

What is deliberately NOT in here: anything that talks to a slow or flaky third party on
the request path. SSH host health does real TCP checks, the finance/crypto panels hit
cached vendor data, and the agent roster polls on its own faster tick -- those keep
their existing endpoints and their existing failure behaviour. Home Assistant is the
one exception, and it is only in here because raw_states() caches and because its
failure is handled as data (`home.available: false`) rather than as an exception.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from ...core import db as core_db
from ...core import home_state, personal_db, vision
from ..auth import require_owner

router = APIRouter(prefix="/api/command-center", tags=["command-center"])

# How many rows the live event log holds. The board shows ~9; the rest are there so the
# drill-down modal has something to scroll without a second query.
EVENT_LIMIT = 40


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(raw) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Rows written before timezone-awareness was consistent still exist in this DB;
    # treat a naive stamp as UTC rather than dropping the row from the feed entirely.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _owner_id(cfg) -> int | None:
    owner = next((u for u in cfg.users if u.role == "owner"), None)
    if owner is None:
        return None
    row = core_db.get_user_by_chat_id(cfg.db_path, owner.telegram_chat_id)
    return row["id"] if row else None


# --------------------------------------------------------------------------- tasks

def _task_bucket(due: datetime | None, now: datetime) -> str:
    """Which column of the Tasks panel a task falls into.

    Day boundaries are computed in UTC, which is a known simplification: a task due late
    tonight local time can read as 'tomorrow'. Everything else in this app already
    formats due dates in the browser's own timezone (see Schedule.jsx), so the UI gets
    the raw due_at too and re-buckets locally -- this is the server-side default for
    anything that reads the numbers without re-deriving them.
    """
    if due is None:
        return "someday"
    if due < now:
        return "overdue"
    if due.date() == now.date():
        return "today"
    if due < now + timedelta(days=7):
        return "week"
    return "later"


def _tasks(db_path: str, owner_id: int | None) -> dict:
    if owner_id is None:
        return {"buckets": {}, "items": [], "open": 0, "doing": 0}

    rows = personal_db.list_tasks(db_path, owner_id)
    now = _now()
    live = [t for t in rows if t["status"] in ("open", "doing")]

    items = []
    for task in live:
        due = _parse(task.get("due_at"))
        items.append({
            "id": task["id"],
            "text": task["text"],
            "status": task["status"],
            "priority": task["priority"],
            "due_at": task.get("due_at"),
            "project_id": task.get("project_id"),
            "bucket": _task_bucket(due, now),
        })

    # Most-urgent first: anything with a due date, soonest first, then high priority,
    # then oldest -- so the panel's top few rows are always the ones that actually want
    # doing rather than whatever happened to be created last.
    rank = {"high": 0, "normal": 1, "low": 2}
    items.sort(key=lambda t: (
        t["due_at"] is None,
        t["due_at"] or "",
        rank.get(t["priority"], 1),
        t["id"],
    ))

    buckets = {}
    for item in items:
        buckets[item["bucket"]] = buckets.get(item["bucket"], 0) + 1

    return {
        "buckets": buckets,
        "items": items,
        "open": sum(1 for t in live if t["status"] == "open"),
        "doing": sum(1 for t in live if t["status"] == "doing"),
    }


# ----------------------------------------------------------------------- pipelines

def _count(conn, sql: str, params=()) -> int:
    try:
        return conn.execute(sql, params).fetchone()[0]
    except sqlite3.Error:
        return 0


def _pipelines(conn) -> list[dict]:
    """The two standing pipelines, each stage counted from the table that actually
    backs it. These are real row counts, not a status model laid over the top: a
    'Creative' count of 6 means six art briefs are sitting in draft right now."""
    business = [
        {"label": "Research", "count": _count(conn, "SELECT COUNT(*) FROM business_research WHERE status != 'done'"),
         "note": "briefs open", "k": "base"},
        {"label": "Creative", "count": _count(conn, "SELECT COUNT(*) FROM art_briefs WHERE status = 'draft'"),
         "note": "drafts", "k": "ok"},
        {"label": "Product", "count": _count(conn, "SELECT COUNT(*) FROM product_concepts WHERE status = 'proposed'"),
         "note": "proposed", "k": "warn"},
        {"label": "Listings", "count": _count(conn, "SELECT COUNT(*) FROM store_listings WHERE status = 'draft'"),
         "note": "unlisted", "k": "base"},
        {"label": "Live", "count": _count(conn, "SELECT COUNT(*) FROM store_listings WHERE status = 'approved'"),
         "note": "on the shelf", "k": "dim"},
    ]
    development = [
        {"label": "Backlog", "count": _count(conn, "SELECT COUNT(*) FROM business_tasks WHERE status = 'open'"),
         "note": "specced", "k": "dim"},
        {"label": "Building", "count": _count(conn, "SELECT COUNT(*) FROM business_tasks WHERE status = 'doing'"),
         "note": "in hand", "k": "ok"},
        {"label": "Queued", "count": _count(conn, "SELECT COUNT(*) FROM work_queue WHERE status IN ('queued','running')"),
         "note": "work queue", "k": "base"},
        {"label": "PRs open", "count": _count(conn, "SELECT COUNT(*) FROM github_pr_state WHERE state = 'open'"),
         "note": "on github", "k": "base"},
        {"label": "Your call", "count": _count(conn, "SELECT COUNT(*) FROM review_items WHERE status = 'pending'"),
         "note": "held for review", "k": "warn"},
    ]
    return [
        {"name": "Business — research to shelf", "stages": business},
        {"name": "Development — spec to deploy", "stages": development},
    ]


# --------------------------------------------------------------------------- events

def _events(conn, owner_id: int | None) -> list[dict]:
    """The live event log: what Jarvis and his staff have actually done lately.

    Each source contributes rows in its own shape, normalised to (at, text, kind) and
    merged newest-first. Sources are queried with their own small LIMIT so one chatty
    table (staff_work has ~1,800 rows) can't crowd every other source out of the feed.
    """
    events: list[dict] = []
    per_source = 12

    def add(rows, text_fn, kind):
        """kind may be a constant or a function of the row -- a failure has to be able
        to colour itself amber, which is the whole reason the log is tinted at all."""
        for row in rows:
            at = _parse(row["at"])
            if at is None:
                continue
            text = text_fn(row)
            if text:
                events.append({"at": at, "text": text,
                               "kind": kind(row) if callable(kind) else kind})

    try:
        add(conn.execute(
            "SELECT agent, status, summary, COALESCE(finished_at, started_at) AS at "
            "FROM agent_runs WHERE summary IS NOT NULL AND summary != '' "
            "ORDER BY COALESCE(finished_at, started_at) DESC LIMIT ?", (per_source,)),
            lambda r: f"{r['agent'].replace('_', ' ')}: {r['summary']}",
            lambda r: "warn" if r["status"] == "error" else "base")
    except sqlite3.Error:
        pass

    try:
        add(conn.execute(
            "SELECT s.title, w.assignment, w.status, COALESCE(w.finished_at, w.started_at) AS at "
            "FROM staff_work w JOIN staff s ON s.id = w.staff_id "
            "ORDER BY COALESCE(w.finished_at, w.started_at) DESC LIMIT ?", (per_source,)),
            lambda r: f"{r['title']} {'failed on' if r['status'] == 'failed' else 'finished'}: "
                      f"{(r['assignment'] or '').strip()[:90]}",
            lambda r: "warn" if r["status"] == "failed" else "base")
    except sqlite3.Error:
        pass

    try:
        add(conn.execute(
            "SELECT title, kind, status, COALESCE(decided_at, created_at) AS at "
            "FROM review_items ORDER BY COALESCE(decided_at, created_at) DESC LIMIT ?", (per_source,)),
            lambda r: (f"Waiting on you: {r['title']}" if r["status"] == "pending"
                       else f"You {r['status']}: {r['title']}"),
            lambda r: "warn" if r["status"] == "pending" else "base")
    except sqlite3.Error:
        pass

    try:
        add(conn.execute(
            "SELECT code, side, qty, price, at FROM paper_trades ORDER BY at DESC LIMIT ?",
            (per_source,)),
            lambda r: f"Crypto desk {r['side']} {r['qty']:.4g} {r['code']} at ${r['price']:,.2f}",
            "ok")
    except sqlite3.Error:
        pass

    try:
        add(conn.execute(
            "SELECT camera_key, kind, label, at FROM vision_events ORDER BY at DESC LIMIT ?",
            (per_source,)),
            lambda r: f"{r['camera_key'].title()} camera: {r['label'] or r['kind']}",
            "dim")
    except sqlite3.Error:
        pass

    if owner_id is not None:
        try:
            add(conn.execute(
                "SELECT text, status, updated_at AS at FROM personal_tasks "
                "WHERE owner_user_id = ? AND status = 'done' ORDER BY updated_at DESC LIMIT ?",
                (owner_id, per_source)),
                lambda r: f"Task done: {r['text'][:90]}",
                "dim")
        except sqlite3.Error:
            pass

    events.sort(key=lambda e: e["at"], reverse=True)
    return [
        {"at": e["at"].isoformat(), "text": e["text"], "kind": e["kind"]}
        for e in events[:EVENT_LIMIT]
    ]


# ------------------------------------------------------------------------ readouts

def _readouts(conn, owner_id: int | None, tasks: dict) -> list[dict]:
    """The strip along the bottom: eight numbers that each answer one question at a
    glance. Every one is a real count -- a readout with nothing behind it renders as
    '—' rather than a zero, so an integration that isn't wired up yet never looks like
    a system reporting good news."""
    def score():
        if owner_id is None:
            return None
        row = conn.execute(
            "SELECT score, bureau FROM credit_score_entries WHERE owner_user_id = ? "
            "ORDER BY recorded_on DESC, id DESC LIMIT 1", (owner_id,)).fetchone()
        return row

    out = []

    row = None
    try:
        row = score()
    except sqlite3.Error:
        pass
    out.append({"label": "Credit score", "value": str(row["score"]) if row else "—",
                "sub": row["bureau"].title() if row else "no entries", "k": "ok" if row else "dim"})

    # This table records a verdict per flagged mail; anything still undecided is what
    # actually wants the owner's eyes.
    flagged = _count(conn, "SELECT COUNT(*) FROM email_importance_flags "
                           "WHERE status NOT IN ('confirmed', 'rejected')")
    out.append({"label": "Inbox", "value": str(flagged), "sub": "flagged for you",
                "k": "warn" if flagged else "dim"})

    out.append({"label": "Tasks", "value": str(tasks["open"] + tasks["doing"]),
                "sub": f"{tasks['buckets'].get('overdue', 0)} overdue",
                "k": "warn" if tasks["buckets"].get("overdue") else "base"})

    pending = _count(conn, "SELECT COUNT(*) FROM review_items WHERE status = 'pending'")
    out.append({"label": "Review queue", "value": str(pending), "sub": "needs your call",
                "k": "warn" if pending else "dim"})

    # "Low" is per-item: an item carries its own low_threshold, and one that has never
    # been given a threshold is only low when it has actually run out.
    low = _count(conn, "SELECT COUNT(*) FROM kitchen_inventory WHERE "
                       "quantity <= COALESCE(low_threshold, 0)")
    total = _count(conn, "SELECT COUNT(*) FROM kitchen_inventory")
    out.append({"label": "Pantry", "value": f"{total - low}/{total}" if total else "—",
                "sub": f"{low} low or out" if total else "nothing tracked",
                "k": "warn" if low else "base"})

    planned = _count(
        conn,
        "SELECT COUNT(*) FROM meal_plan_entries e JOIN meal_plans p ON p.id = e.meal_plan_id "
        "WHERE p.status = 'active'")
    out.append({"label": "Meal plan", "value": str(planned) if planned else "—",
                "sub": "meals planned" if planned else "no active plan",
                "k": "ok" if planned else "dim"})

    cart = _count(conn, "SELECT COUNT(*) FROM shopping_list_items WHERE status = 'pending'")
    out.append({"label": "Shopping", "value": str(cart), "sub": "items to buy",
                "k": "base" if cart else "dim"})

    staff_live = _count(conn, "SELECT COUNT(*) FROM staff WHERE status = 'active'")
    out.append({"label": "Staff", "value": str(staff_live), "sub": "on the roster", "k": "base"})

    return out


# ---------------------------------------------------------------------- the handler

@router.get("/snapshot")
async def snapshot(request: Request):
    """Everything the board needs that is cheap enough to read on every tick."""
    require_owner(request)
    cfg = request.app.state.cfg
    owner_id = _owner_id(cfg)

    try:
        cameras = vision.list_cameras(cfg.db_path)
    except Exception:
        cameras = []

    home = home_state.snapshot(request.app.state.home_assistant, cameras)

    conn = _conn(cfg.db_path)
    try:
        tasks = _tasks(cfg.db_path, owner_id)
        payload = {
            "server_time": _now().isoformat(),
            "home": home,
            "tasks": tasks,
            "pipelines": _pipelines(conn),
            "events": _events(conn, owner_id),
            "readouts": _readouts(conn, owner_id, tasks),
        }
    finally:
        conn.close()

    return payload
