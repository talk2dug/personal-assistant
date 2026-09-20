"""What the team needs FROM Jack, as a queue he can actually clear.

A sibling of review_items, and the distinction is the whole point. review_items asks him
to DECIDE something the agents could not decide themselves -- pick this mockup, approve
that concept -- and the answer is a choice among options the agent already generated.
This table asks him to SUPPLY something no agent can generate at all: an API token, a
storefront login, an account that needs a human with a credit card, a file off his disk.
The agent is not undecided, it is stopped.

That difference matters because the failure mode is different. An undecided agent picks
something reasonable and moves on; a stopped agent silently does nothing, run after run,
and the pipeline looks alive while producing drafts that can never ship. The print-station
audit (2026-09-13) is the cautionary tale: SHOPIFY_STOREFRONT_TOKEN and LUMAPRINTS_API_KEY
were never set, so the flagship product could neither be bought nor fulfilled, and nothing
in the system ever said so. Five weekly strategy meetings each recorded "0 orders, $0
revenue" without once naming the two empty strings that caused it. A blocker nobody is
told about is indistinguishable from a business that simply is not working.

So a request here is a first-class object with an owner, a reason, and a resolution:

  raise_request()  an agent states what it needs and what that unblocks. Idempotent on
                   `name`, because agents re-run on a schedule and would otherwise file
                   the same ask every cycle until the board is unreadable.
  provide()        Jack supplies the value from the UI. Secrets land in owner_supplies
                   and are never read back out over the API -- only their fingerprint.
  secret()         how an agent actually consumes what he provided, at use time rather
                   than at process start, so a token supplied at noon works at 12:01
                   without restarting the service.

Nothing here reaches outward or spends money; it is a ledger of asks and answers.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

# What kind of thing is being asked for. This drives the input the UI renders and how the
# value is stored, so it is a closed set rather than free text.
#   secret   -- an API token/password. Stored, never echoed back, masked in every read.
#   url      -- a link (a store admin page, a shared drive folder).
#   text     -- a plain value: an account id, a shop name, a chosen handle.
#   file     -- something on his disk to import (the model catalogue images).
#   account  -- "go sign up for this", where the deliverable is usually a secret after.
#   purchase -- costs money and needs a human to agree to the spend.
KINDS = ("secret", "url", "text", "file", "account", "purchase")

# open      -- the team is waiting.
# provided  -- Jack answered; the value is stored and the agent has not consumed it yet.
# resolved  -- the agent confirmed it worked (a token that authenticates, a file imported).
# rejected  -- Jack declined, deliberately. The agent must stop asking and route around it.
STATUSES = ("open", "provided", "resolved", "rejected")

SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    -- Which pipeline this belongs to, so the board reads as a project manager's view
    -- rather than one undifferentiated to-do list across every business Jarvis runs.
    project_id INTEGER,
    -- Who is stopped. Needed to notify the right agent when the answer lands, and to
    -- show him whose work he is holding up.
    agent_key TEXT,
    title TEXT NOT NULL,
    -- Stable machine name (printify_api_key). The dedupe key, and what secret() looks up.
    -- NULL is allowed for one-off asks that no agent will read back programmatically.
    name TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('secret','url','text','file','account','purchase')),
    -- In the agent's own words: what becomes possible once this exists. This is the field
    -- that makes the board worth reading -- "Printify API key" is a chore, "without this
    -- no order can ever be fulfilled" is a priority.
    why TEXT,
    -- Concrete steps to get it, written when the request is raised. He should never have
    -- to go research how to obtain the thing the team asked for.
    instructions TEXT,
    -- What stays stopped until this is answered, for ordering the board by consequence.
    blocks TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','provided','resolved','rejected')),
    priority INTEGER NOT NULL DEFAULT 2,       -- 1 highest
    note TEXT,                                  -- his reply when rejecting, or any aside
    -- Ready-to-paste generation prompts, as a JSON list. When the team wants a new
    -- catalogue model it cannot make one itself (Leonardo is web-UI only here), so the
    -- ask is worthless unless it arrives with the exact prompts to paste. Storing them
    -- structured rather than buried in `instructions` is what lets the board render one
    -- copy button per prompt.
    prompts TEXT,
    created_at TEXT NOT NULL,
    provided_at TEXT,
    resolved_at TEXT,
    -- When the blocked agent was last told this became available, so an answer is
    -- delivered exactly once rather than re-announced on every run.
    notified_at TEXT
);

CREATE TABLE IF NOT EXISTS owner_supplies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES owner_requests(id),
    -- Kept in its own table rather than a column on the request so that a value can be
    -- replaced (a rotated token) without losing the history of the ask, and so that no
    -- ordinary SELECT over the board can accidentally return a secret.
    value TEXT,
    is_secret INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_owner_requests_open
    ON owner_requests(owner_user_id, status, priority);
CREATE INDEX IF NOT EXISTS idx_owner_supplies_req ON owner_supplies(request_id);
"""


def init_owner_requests(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(owner_requests)")}
        if "prompts" not in cols:
            conn.execute("ALTER TABLE owner_requests ADD COLUMN prompts TEXT")
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask(value: str | None) -> str | None:
    """A secret rendered so he can tell WHICH key is stored without the key leaving the
    box. Short values are hidden entirely -- revealing 3 of 6 characters is not a mask."""
    if not value:
        return None
    return f"...{value[-4:]}" if len(value) > 10 else "(set)"


def raise_request(db_path: str, owner_user_id: int, title: str, kind: str, *,
                  name: str | None = None, why: str | None = None,
                  instructions: str | None = None, blocks: str | None = None,
                  agent_key: str | None = None, project_id: int | None = None,
                  priority: int = 2, prompts: list[str] | None = None) -> int:
    """File an ask, or return the existing one.

    Idempotent on `name` against anything not yet rejected, because the agents raising
    these run on a schedule: a product pipeline that needs a Printify token needs it on
    every single run until it exists, and a board that grows one identical row per run is
    a board he stops reading. Re-raising instead refreshes the reasoning, which is the
    part that legitimately improves as the agent learns more about what it is blocked on.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown request kind {kind!r}; expected one of {KINDS}")
    with closing(_connect(db_path)) as conn:
        if name:
            row = conn.execute(
                """SELECT id FROM owner_requests
                       WHERE owner_user_id = ? AND name = ? AND status IN ('open','provided')
                       ORDER BY id LIMIT 1""",
                (owner_user_id, name)).fetchone()
            if row is not None:
                conn.execute(
                    """UPDATE owner_requests
                           SET why = COALESCE(?, why), instructions = COALESCE(?, instructions),
                               blocks = COALESCE(?, blocks), priority = MIN(priority, ?),
                               prompts = COALESCE(?, prompts)
                         WHERE id = ?""",
                    (why, instructions, blocks, priority,
                     json.dumps(prompts) if prompts else None, row["id"]))
                conn.commit()
                return int(row["id"])
        cur = conn.execute(
            """INSERT INTO owner_requests (owner_user_id, project_id, agent_key, title, name,
                                           kind, why, instructions, blocks, priority,
                                           prompts, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (owner_user_id, project_id, agent_key, title, name, kind, why, instructions,
             blocks, priority, json.dumps(prompts) if prompts else None, _now()))
        conn.commit()
        return int(cur.lastrowid)


def list_requests(db_path: str, owner_user_id: int, status: str | None = None,
                  project_id: int | None = None) -> list[dict]:
    """The board. Never returns a stored secret -- only whether one exists and its mask,
    so this is safe to hand to the browser."""
    sql = ["""SELECT r.*, s.value AS _v, s.is_secret AS _sec, s.at AS supplied_at
                FROM owner_requests r
                LEFT JOIN owner_supplies s ON s.id = (
                    SELECT id FROM owner_supplies WHERE request_id = r.id ORDER BY id DESC LIMIT 1)
               WHERE r.owner_user_id = ?"""]
    args: list = [owner_user_id]
    if status:
        sql.append("AND r.status = ?")
        args.append(status)
    if project_id is not None:
        sql.append("AND r.project_id = ?")
        args.append(project_id)
    # Open work first, then by stated consequence, then oldest -- the order he should
    # work down the list in.
    sql.append("""ORDER BY CASE r.status WHEN 'open' THEN 0 WHEN 'provided' THEN 1 ELSE 2 END,
                           r.priority, r.id""")
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(" ".join(sql), args).fetchall()

    out = []
    for row in rows:
        item = {k: row[k] for k in row.keys() if not k.startswith("_") and k != "supplied_at"}
        item["has_value"] = row["_v"] is not None
        item["is_secret"] = bool(row["_sec"])
        item["supplied_at"] = row["supplied_at"]
        # A secret is masked; anything else is shown back, since the point of a url or a
        # file path is that he can see what the team is now working from.
        item["value"] = _mask(row["_v"]) if row["_sec"] else row["_v"]
        # Decoded here so the board never has to parse JSON in the browser, and a
        # malformed blob degrades to "no prompts" rather than breaking the whole view.
        try:
            item["prompts"] = json.loads(item["prompts"]) if item.get("prompts") else []
        except (TypeError, ValueError):
            item["prompts"] = []
        out.append(item)
    return out


def provide(db_path: str, owner_user_id: int, request_id: int, value: str,
            is_secret: bool | None = None) -> bool:
    """He answers. The value is stored and the request moves to 'provided' -- not
    'resolved', because only the agent that was stopped can confirm the thing actually
    works. A token that is present but wrong is the same outage as a token that is
    missing, and this is the distinction that print-station never drew."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT kind FROM owner_requests WHERE id = ? AND owner_user_id = ?",
                           (request_id, owner_user_id)).fetchone()
        if row is None:
            return False
        secret_value = row["kind"] == "secret" if is_secret is None else bool(is_secret)
        now = _now()
        conn.execute("INSERT INTO owner_supplies (request_id, value, is_secret, at) VALUES (?,?,?,?)",
                     (request_id, value, 1 if secret_value else 0, now))
        # notified_at is cleared so a re-supplied value (a rotated token) is announced to
        # the blocked agent again rather than being swallowed by the earlier notice.
        conn.execute("""UPDATE owner_requests
                           SET status = 'provided', provided_at = ?, notified_at = NULL
                         WHERE id = ?""", (now, request_id))
        conn.commit()
        return True


def set_status(db_path: str, owner_user_id: int, request_id: int, status: str,
               note: str | None = None) -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """UPDATE owner_requests
                   SET status = ?, note = COALESCE(?, note),
                       resolved_at = CASE WHEN ? IN ('resolved','rejected') THEN ? ELSE resolved_at END
                 WHERE id = ? AND owner_user_id = ?""",
            (status, note, status, _now(), request_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0


def secret(db_path: str, name: str, default: str | None = None) -> str | None:
    """How an agent reads what he supplied.

    Resolved at call time, not at process start: a token provided through the UI at noon
    has to work on the 12:05 run without restarting a Windows service. Values supplied
    here deliberately win over config.json, so replacing a dead credential is something
    he can do from the board rather than by editing a file he has never opened.
    """
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT s.value FROM owner_supplies s
                 JOIN owner_requests r ON r.id = s.request_id
                WHERE r.name = ? AND r.status IN ('provided','resolved')
                ORDER BY s.id DESC LIMIT 1""", (name,)).fetchone()
    return row["value"] if row and row["value"] else default


def pending_unblocks(db_path: str, agent_key: str) -> list[dict]:
    """Answers this agent has not been told about yet. The caller marks them delivered
    with mark_notified(), so an unblock is announced once rather than every run."""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT id, title, name, kind, blocks FROM owner_requests
                   WHERE agent_key = ? AND status = 'provided' AND notified_at IS NULL
                   ORDER BY priority, id""", (agent_key,)).fetchall()
        return [dict(r) for r in rows]


def mark_notified(db_path: str, request_ids: list[int]) -> None:
    if not request_ids:
        return
    with closing(_connect(db_path)) as conn:
        conn.executemany("UPDATE owner_requests SET notified_at = ? WHERE id = ?",
                         [(_now(), rid) for rid in request_ids])
        conn.commit()


def blocked_summary(db_path: str, owner_user_id: int) -> dict:
    """One line for the board: how much work is stopped, and on what. Shown on the
    Command Center so a stalled pipeline is visible without opening anything."""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT status, COUNT(*) n FROM owner_requests
                   WHERE owner_user_id = ? GROUP BY status""", (owner_user_id,)).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        top = conn.execute(
            """SELECT title, blocks FROM owner_requests
                   WHERE owner_user_id = ? AND status = 'open'
                   ORDER BY priority, id LIMIT 1""", (owner_user_id,)).fetchone()
    return {"open": counts.get("open", 0), "provided": counts.get("provided", 0),
            "resolved": counts.get("resolved", 0),
            "top": dict(top) if top else None}


def unblock_notice(db_path: str, agent_key: str, mark: bool = True) -> str:
    """A line for a blocked employee's prompt telling it what just became available.

    Strictly speaking this is a courtesy: secret() resolves at use time, so an agent that
    asks for a credential every run will simply find it present and carry on without being
    told. But an employee that reported "blocked pending the Printify token" last run and
    is handed no acknowledgement this run tends to re-report the same blocker rather than
    retry it, so saying it plainly is what actually gets the work restarted.

    Marking is the default because the caller that renders this is the one delivering it.
    """
    try:
        pending = pending_unblocks(db_path, agent_key)
    except sqlite3.Error:
        # This notice is a courtesy bolted onto the front of a real work assignment. A
        # database that predates these tables -- or any other storage fault -- must cost
        # the employee its preamble, never its job: a briefing addition that can raise is
        # a briefing addition that can cancel the work it was meant to restart.
        return ""
    if not pending:
        return ""
    lines = ["", "UNBLOCKED SINCE YOUR LAST RUN -- the owner has supplied these, so retry "
                 "the work you reported blocked rather than asking again:"]
    for item in pending:
        detail = f" (read it as `{item['name']}`)" if item["name"] else ""
        blocked = f" -- this was blocking {item['blocks']}" if item["blocks"] else ""
        lines.append(f"  * {item['title']}{detail}{blocked}")
    lines.append("If it still does not work, say so specifically -- a wrong credential is a "
                 "different report from a missing one, and only you can tell them apart.")
    if mark:
        mark_notified(db_path, [item["id"] for item in pending])
    return "\n".join(lines)
