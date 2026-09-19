"""Storage for the business side of Jarvis — the "second in command" domain.

A deliberate sibling of db.py rather than more functions inside it. db.py's rule is
that nothing else touches the database directly, and that still holds: this module
follows the identical discipline (short-lived WAL connections, every read/write behind
a function, owner id passed explicitly) for a separate bounded context. Splitting on
that boundary keeps db.py readable instead of pushing it past 900 lines with tables
the personal-assistant side never reads.

The schema is salvaged from the Blue Ridge Custom Co project (D:\\Vinyl Stuff) — the
parts that survive a move to a new city. Its market_leads/trend ideas/finance tables
were tied to a business that got 99% of the way to launching in Asheville; the machinery
generalises, the Asheville data does not. Anything market-specific lives in config, not
here.
"""
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS business_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    goal TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'done', 'dropped')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS business_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    project_id INTEGER REFERENCES business_projects(id),
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'doing', 'done', 'dropped')),
    priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high')),
    due_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Output of the research agent, and of ad-hoc "look into X" asks from chat.
CREATE TABLE IF NOT EXISTS business_research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    question TEXT,
    findings TEXT,
    sources TEXT,
    status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested', 'done', 'failed')),
    project_id INTEGER REFERENCES business_projects(id),
    created_at TEXT NOT NULL,
    completed_at TEXT
);

-- Vendor markets / craft fairs / pop-ups found by the market agent.
CREATE TABLE IF NOT EXISTS market_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    event_date TEXT,
    location TEXT,
    url TEXT,
    cost TEXT,
    fit_score INTEGER,
    reasoning TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'interested', 'applied', 'booked', 'rejected', 'passed')),
    found_at TEXT NOT NULL,
    UNIQUE(owner_user_id, name, event_date)
);

-- Product/design ideas from the trend agent.
CREATE TABLE IF NOT EXISTS trend_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    source TEXT,
    score INTEGER,
    product_idea TEXT,
    reasoning TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'making', 'made', 'passed')),
    found_at TEXT NOT NULL,
    UNIQUE(owner_user_id, topic)
);

-- Business money, kept separate from the personal Era-backed finance tables.
CREATE TABLE IF NOT EXISTS business_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    category TEXT,
    spent_on TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS business_equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    purchase_price REAL,
    purchased_on TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'needs_service', 'broken', 'sold')),
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS business_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    unit TEXT,
    reorder_at REAL,
    notes TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_user_id, item)
);

-- The creative pipeline. Product Creator proposes a concept, Art Director gives it
-- artwork direction, E-Store Manager writes the listing, Social Media Director drafts
-- the posts. Each stage has its own status so the owner is a gate between them rather
-- than downstream of a fait accompli.
CREATE TABLE IF NOT EXISTS product_concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    product_type TEXT,
    description TEXT,
    target_customer TEXT,
    price_estimate REAL,
    production_notes TEXT,
    source TEXT,
    trend_lead_id INTEGER REFERENCES trend_leads(id),
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'approved', 'in_production', 'live', 'retired', 'rejected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_user_id, name)
);

CREATE TABLE IF NOT EXISTS art_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    concept_id INTEGER REFERENCES product_concepts(id),
    title TEXT NOT NULL,
    style_direction TEXT,
    image_prompt TEXT,
    negative_prompt TEXT,
    aspect TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rendered', 'rejected')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS store_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    concept_id INTEGER REFERENCES product_concepts(id),
    title TEXT NOT NULL,
    description TEXT,
    seo_tags TEXT,
    price REAL,
    variants TEXT,
    channel TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'approved', 'published', 'on_sale', 'delisted', 'rejected')),
    external_id TEXT,
    metrics TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS social_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    listing_id INTEGER REFERENCES store_listings(id),
    concept_id INTEGER REFERENCES product_concepts(id),
    platform TEXT NOT NULL,
    hook TEXT,
    caption TEXT,
    hashtags TEXT,
    call_to_action TEXT,
    reason TEXT,
    scheduled_for TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'approved', 'posted', 'rejected')),
    external_id TEXT,
    metrics TEXT,
    created_at TEXT NOT NULL
);

-- The review queue: one place for everything the team has made that needs the owner's
-- eyes. Deliberately a table of its own rather than a view over concepts/briefs/listings
-- /posts, because "waiting on a decision" is its own state with its own ordering, and
-- because plenty of things worth showing him (a rendered image, a pick between three
-- variants, an ad-hoc question) don't correspond to a row in any of those tables.
CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other'
        CHECK (kind IN ('concept', 'art', 'listing', 'post', 'media', 'research', 'other')),
    summary TEXT,
    detail TEXT,
    source_agent TEXT,
    -- What this decision is about, when it maps onto a pipeline row, so approving here
    -- can advance the real object rather than just marking a card done.
    ref_table TEXT,
    ref_id INTEGER,
    priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
    decision_note TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    -- Set once the staleness watchdog (scheduler.py's run_review_watchdog) has nudged the
    -- owner that this item is still waiting on a decision, so a slow poll interval can't
    -- notify the same item twice. Same nullable-marker pattern as reminders.sent_at.
    watchdog_notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_items(status, created_at);

-- Options belonging to a review item. Zero or one option means a straight yes/no; two or
-- more makes it a pick-one, which is how "here are three logo treatments" is represented
-- without a second kind of review item.
CREATE TABLE IF NOT EXISTS review_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES review_items(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    description TEXT,
    media_path TEXT,
    body TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    chosen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_review_options_item ON review_options(item_id, position);

-- Audit/journal of every background agent run, so a digest can say what actually
-- happened rather than the model reconstructing it.
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'error', 'skipped')),
    summary TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at);
"""


def init_business_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Idempotent migration, same pattern as staff.py/db.py: CREATE TABLE IF NOT EXISTS
        # won't add a column to a table that already exists from an earlier version.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(review_items)")}
        if "watchdog_notified_at" not in cols:
            conn.execute("ALTER TABLE review_items ADD COLUMN watchdog_notified_at TEXT")
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(cursor):
    return [dict(r) for r in cursor.fetchall()]


# --- projects ----------------------------------------------------------------

def create_project(db_path: str, owner_user_id: int, name: str, goal: str | None = None) -> int:
    now = _now()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO business_projects (owner_user_id, name, goal, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (owner_user_id, name, goal, now, now),
        )
        conn.commit()
        return cur.lastrowid


def list_projects(db_path: str, owner_user_id: int, status: str | None = None):
    query = "SELECT * FROM business_projects WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC"
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def update_project(db_path: str, owner_user_id: int, project_id: int, **fields) -> bool:
    allowed = {k: v for k, v in fields.items() if k in ("name", "goal", "status") and v is not None}
    if not allowed:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE business_projects SET {sets}, updated_at = ? WHERE id = ? AND owner_user_id = ?",
            [*allowed.values(), _now(), project_id, owner_user_id],
        )
        conn.commit()
        return cur.rowcount > 0


# --- tasks -------------------------------------------------------------------

def create_task(
    db_path: str, owner_user_id: int, text: str, project_id: int | None = None,
    priority: str = "normal", due_at: str | None = None,
) -> int:
    now = _now()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO business_tasks (owner_user_id, project_id, text, priority, due_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_user_id, project_id, text, priority, due_at, now, now),
        )
        conn.commit()
        return cur.lastrowid


def list_tasks(db_path: str, owner_user_id: int, status: str | None = None, project_id: int | None = None):
    query = (
        "SELECT t.*, p.name AS project_name FROM business_tasks t"
        " LEFT JOIN business_projects p ON p.id = t.project_id"
        " WHERE t.owner_user_id = ?"
    )
    params: list = [owner_user_id]
    if status:
        query += " AND t.status = ?"
        params.append(status)
    if project_id is not None:
        query += " AND t.project_id = ?"
        params.append(project_id)
    query += (
        " ORDER BY CASE t.status WHEN 'doing' THEN 0 WHEN 'open' THEN 1 ELSE 2 END,"
        " CASE t.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, t.created_at"
    )
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def update_task(db_path: str, owner_user_id: int, task_id: int, **fields) -> bool:
    allowed = {
        k: v for k, v in fields.items()
        if k in ("text", "status", "priority", "due_at", "project_id") and v is not None
    }
    if not allowed:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE business_tasks SET {sets}, updated_at = ? WHERE id = ? AND owner_user_id = ?",
            [*allowed.values(), _now(), task_id, owner_user_id],
        )
        conn.commit()
        return cur.rowcount > 0


# --- research ----------------------------------------------------------------

def create_research(
    db_path: str, owner_user_id: int, topic: str, question: str | None = None,
    project_id: int | None = None,
) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO business_research (owner_user_id, topic, question, project_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (owner_user_id, topic, question, project_id, _now()),
        )
        conn.commit()
        return cur.lastrowid


def pending_research(db_path: str, limit: int = 3):
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(
            "SELECT * FROM business_research WHERE status = 'requested' ORDER BY created_at LIMIT ?", (limit,)
        ))


def complete_research(db_path: str, research_id: int, findings: str, sources: str = "", status: str = "done") -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE business_research SET findings = ?, sources = ?, status = ?, completed_at = ? WHERE id = ?",
            (findings, sources, status, _now(), research_id),
        )
        conn.commit()


def list_research(db_path: str, owner_user_id: int, limit: int = 10):
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(
            "SELECT * FROM business_research WHERE owner_user_id = ? ORDER BY created_at DESC LIMIT ?",
            (owner_user_id, limit),
        ))


# --- market leads ------------------------------------------------------------

def normalize_lead_name(name: str) -> str:
    """Collapses a market name to a comparison key.

    Two scans of the same event genuinely come back worded differently — the first live
    run produced both "Richmond Makers Market: Spooktoberfest" and "Richmond Makers
    Market — Spooktoberfest" for one market, because the model reworded a title it read
    off two different pages. Exact-string matching treats those as separate events, so
    the owner sees the same market twice and the digest announces it twice.
    """
    lowered = name.casefold()
    kept = [c if (c.isalnum() or c.isspace()) else " " for c in lowered]
    return " ".join("".join(kept).split())


def upsert_market_lead(
    db_path: str, owner_user_id: int, name: str, event_date: str | None, location: str | None,
    url: str | None, cost: str | None, fit_score: int | None, reasoning: str | None,
) -> bool:
    """Returns True when this is a genuinely new lead, so the digest can report only
    what's actually new rather than re-announcing the same market every scan."""
    key = normalize_lead_name(name)
    with closing(_connect(db_path)) as conn:
        # Matching is done in Python rather than SQL because the normalisation is more
        # than SQL's LOWER()/REPLACE() can express, and the table is small enough that
        # scanning one owner's leads for the same date costs nothing.
        candidates = conn.execute(
            "SELECT id, name FROM market_leads WHERE owner_user_id = ? AND IFNULL(event_date,'') = IFNULL(?,'')",
            (owner_user_id, event_date),
        ).fetchall()
        existing = next((c for c in candidates if normalize_lead_name(c["name"]) == key), None)
        if existing:
            # Refresh the details but never clobber a status the user has set.
            conn.execute(
                "UPDATE market_leads SET location = ?, url = ?, cost = ?, fit_score = ?, reasoning = ? WHERE id = ?",
                (location, url, cost, fit_score, reasoning, existing["id"]),
            )
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO market_leads (owner_user_id, name, event_date, location, url, cost, fit_score, reasoning, found_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_user_id, name, event_date, location, url, cost, fit_score, reasoning, _now()),
        )
        conn.commit()
        return True


def list_market_leads(db_path: str, owner_user_id: int, status: str | None = None, min_score: int = 0, limit: int = 25):
    query = "SELECT * FROM market_leads WHERE owner_user_id = ? AND IFNULL(fit_score, 0) >= ?"
    params: list = [owner_user_id, min_score]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY IFNULL(fit_score,0) DESC, IFNULL(event_date,'9999') LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def set_market_lead_status(db_path: str, owner_user_id: int, lead_id: int, status: str) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE market_leads SET status = ? WHERE id = ? AND owner_user_id = ?",
            (status, lead_id, owner_user_id),
        )
        conn.commit()
        return cur.rowcount > 0


# --- trend leads -------------------------------------------------------------

def upsert_trend_lead(
    db_path: str, owner_user_id: int, topic: str, source: str | None, score: int | None,
    product_idea: str | None, reasoning: str | None,
) -> bool:
    key = normalize_lead_name(topic)
    with closing(_connect(db_path)) as conn:
        # Same rewording problem as market leads — see normalize_lead_name.
        candidates = conn.execute(
            "SELECT id, topic FROM trend_leads WHERE owner_user_id = ?", (owner_user_id,)
        ).fetchall()
        existing = next((c for c in candidates if normalize_lead_name(c["topic"]) == key), None)
        if existing:
            conn.execute(
                "UPDATE trend_leads SET score = ?, product_idea = ?, reasoning = ? WHERE id = ?",
                (score, product_idea, reasoning, existing["id"]),
            )
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO trend_leads (owner_user_id, topic, source, score, product_idea, reasoning, found_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_user_id, topic, source, score, product_idea, reasoning, _now()),
        )
        conn.commit()
        return True


def list_trend_leads(db_path: str, owner_user_id: int, status: str | None = None, limit: int = 25):
    query = "SELECT * FROM trend_leads WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY IFNULL(score,0) DESC, found_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def set_trend_lead_status(db_path: str, owner_user_id: int, lead_id: int, status: str) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE trend_leads SET status = ? WHERE id = ? AND owner_user_id = ?", (status, lead_id, owner_user_id)
        )
        conn.commit()
        return cur.rowcount > 0


# --- business money ----------------------------------------------------------

def add_expense(
    db_path: str, owner_user_id: int, description: str, amount: float, spent_on: str,
    category: str | None = None, notes: str | None = None,
) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO business_expenses (owner_user_id, description, amount, category, spent_on, notes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_user_id, description, amount, category, spent_on, notes, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_expenses(db_path: str, owner_user_id: int, since: str | None = None, limit: int = 100):
    query = "SELECT * FROM business_expenses WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if since:
        query += " AND spent_on >= ?"
        params.append(since)
    query += " ORDER BY spent_on DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def expense_total(db_path: str, owner_user_id: int, since: str | None = None) -> float:
    query = "SELECT IFNULL(SUM(amount), 0) AS total FROM business_expenses WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if since:
        query += " AND spent_on >= ?"
        params.append(since)
    with closing(_connect(db_path)) as conn:
        return float(conn.execute(query, params).fetchone()["total"])


def add_equipment(
    db_path: str, owner_user_id: int, name: str, purchase_price: float | None = None,
    purchased_on: str | None = None, notes: str | None = None,
) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO business_equipment (owner_user_id, name, purchase_price, purchased_on, notes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (owner_user_id, name, purchase_price, purchased_on, notes, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_equipment(db_path: str, owner_user_id: int):
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(
            "SELECT * FROM business_equipment WHERE owner_user_id = ? ORDER BY name", (owner_user_id,)
        ))


def set_equipment_status(db_path: str, owner_user_id: int, equipment_id: int, status: str) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE business_equipment SET status = ? WHERE id = ? AND owner_user_id = ?",
            (status, equipment_id, owner_user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def upsert_inventory(
    db_path: str, owner_user_id: int, item: str, quantity: float, unit: str | None = None,
    reorder_at: float | None = None, notes: str | None = None,
) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO business_inventory (owner_user_id, item, quantity, unit, reorder_at, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_user_id, item) DO UPDATE SET
                 quantity = excluded.quantity,
                 unit = COALESCE(excluded.unit, business_inventory.unit),
                 reorder_at = COALESCE(excluded.reorder_at, business_inventory.reorder_at),
                 notes = COALESCE(excluded.notes, business_inventory.notes),
                 updated_at = excluded.updated_at""",
            (owner_user_id, item, quantity, unit, reorder_at, notes, _now()),
        )
        conn.commit()


def list_inventory(db_path: str, owner_user_id: int, low_only: bool = False):
    query = "SELECT * FROM business_inventory WHERE owner_user_id = ?"
    if low_only:
        query += " AND reorder_at IS NOT NULL AND quantity <= reorder_at"
    query += " ORDER BY item"
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, (owner_user_id,)))


# --- creative pipeline -------------------------------------------------------

def _upsert_named(conn, table, name_column, owner_user_id, name, insert_sql, insert_params):
    """Shared new-or-existing check for LLM-populated tables.

    Same reasoning as normalize_lead_name: two runs word the same idea differently, and
    exact-match would let "Blue Ridge Trucker Hat" and "Blue Ridge trucker hat!" both
    through as separate products.
    """
    key = normalize_lead_name(name)
    rows = conn.execute(
        f"SELECT id, {name_column} AS n FROM {table} WHERE owner_user_id = ?", (owner_user_id,)
    ).fetchall()
    match = next((r for r in rows if normalize_lead_name(r["n"]) == key), None)
    if match:
        return match["id"], False
    cur = conn.execute(insert_sql, insert_params)
    return cur.lastrowid, True


def create_product_concept(
    db_path: str, owner_user_id: int, name: str, product_type: str | None = None,
    description: str | None = None, target_customer: str | None = None,
    price_estimate: float | None = None, production_notes: str | None = None,
    source: str | None = None, trend_lead_id: int | None = None,
) -> tuple[int, bool]:
    now = _now()
    with closing(_connect(db_path)) as conn:
        result = _upsert_named(
            conn, "product_concepts", "name", owner_user_id, name,
            """INSERT INTO product_concepts
               (owner_user_id, name, product_type, description, target_customer, price_estimate,
                production_notes, source, trend_lead_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, name, product_type, description, target_customer, price_estimate,
             production_notes, source, trend_lead_id, now, now),
        )
        conn.commit()
        return result


def list_product_concepts(db_path: str, owner_user_id: int, status: str | None = None, limit: int = 40):
    query = "SELECT * FROM product_concepts WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def set_concept_status(db_path: str, owner_user_id: int, concept_id: int, status: str) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE product_concepts SET status = ?, updated_at = ? WHERE id = ? AND owner_user_id = ?",
            (status, _now(), concept_id, owner_user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def concepts_without(db_path: str, owner_user_id: int, table: str, statuses: tuple = ("approved",), limit: int = 5):
    """Approved concepts that don't yet have a row in art_briefs / store_listings — how
    each downstream agent finds its work without needing a queue of its own."""
    placeholders = ",".join("?" for _ in statuses)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(
            f"""SELECT c.* FROM product_concepts c
                WHERE c.owner_user_id = ? AND c.status IN ({placeholders})
                  AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.concept_id = c.id)
                ORDER BY c.updated_at LIMIT ?""",
            (owner_user_id, *statuses, limit),
        ))


def create_art_brief(
    db_path: str, owner_user_id: int, title: str, concept_id: int | None = None,
    style_direction: str | None = None, image_prompt: str | None = None,
    negative_prompt: str | None = None, aspect: str | None = None, notes: str | None = None,
) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO art_briefs
               (owner_user_id, concept_id, title, style_direction, image_prompt, negative_prompt,
                aspect, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, concept_id, title, style_direction, image_prompt, negative_prompt,
             aspect, notes, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_art_briefs(db_path: str, owner_user_id: int, status: str | None = None, limit: int = 25):
    query = (
        "SELECT b.*, c.name AS concept_name FROM art_briefs b"
        " LEFT JOIN product_concepts c ON c.id = b.concept_id WHERE b.owner_user_id = ?"
    )
    params: list = [owner_user_id]
    if status:
        query += " AND b.status = ?"
        params.append(status)
    query += " ORDER BY b.created_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def set_art_brief_status(db_path: str, owner_user_id: int, brief_id: int, status: str) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE art_briefs SET status = ? WHERE id = ? AND owner_user_id = ?",
            (status, brief_id, owner_user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def set_art_brief_prompt(db_path: str, owner_user_id: int, brief_id: int, image_prompt: str) -> bool:
    """Adopts the direction the owner actually picked as the brief's working prompt.

    The art director proposes several directions and renders each one, so the card he
    decides is a pick-one between real images. Without this the pick is decorative: the
    brief would keep whichever prompt happened to be first, and the store and social
    copy written from it downstream would describe a picture he did not choose.
    """
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE art_briefs SET image_prompt = ? WHERE id = ? AND owner_user_id = ?",
            (image_prompt, brief_id, owner_user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def create_store_listing(
    db_path: str, owner_user_id: int, title: str, concept_id: int | None = None,
    description: str | None = None, seo_tags: str | None = None, price: float | None = None,
    variants: str | None = None, channel: str | None = None,
) -> int:
    now = _now()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO store_listings
               (owner_user_id, concept_id, title, description, seo_tags, price, variants, channel,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, concept_id, title, description, seo_tags, price, variants, channel, now, now),
        )
        conn.commit()
        return cur.lastrowid


def list_store_listings(db_path: str, owner_user_id: int, status: str | None = None, limit: int = 40):
    query = "SELECT * FROM store_listings WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def update_store_listing(db_path: str, owner_user_id: int, listing_id: int, **fields) -> bool:
    allowed = {
        k: v for k, v in fields.items()
        if k in ("title", "description", "seo_tags", "price", "status", "external_id", "metrics") and v is not None
    }
    if not allowed:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE store_listings SET {sets}, updated_at = ? WHERE id = ? AND owner_user_id = ?",
            [*allowed.values(), _now(), listing_id, owner_user_id],
        )
        conn.commit()
        return cur.rowcount > 0


def create_social_post(
    db_path: str, owner_user_id: int, platform: str, caption: str | None = None,
    hook: str | None = None, hashtags: str | None = None, call_to_action: str | None = None,
    reason: str | None = None, scheduled_for: str | None = None,
    listing_id: int | None = None, concept_id: int | None = None,
) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO social_posts
               (owner_user_id, listing_id, concept_id, platform, hook, caption, hashtags,
                call_to_action, reason, scheduled_for, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, listing_id, concept_id, platform, hook, caption, hashtags,
             call_to_action, reason, scheduled_for, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_social_posts(db_path: str, owner_user_id: int, status: str | None = None, limit: int = 30):
    query = "SELECT * FROM social_posts WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def update_social_post(db_path: str, owner_user_id: int, post_id: int, **fields) -> bool:
    allowed = {
        k: v for k, v in fields.items()
        if k in ("caption", "hook", "hashtags", "status", "scheduled_for", "external_id", "metrics")
        and v is not None
    }
    if not allowed:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE social_posts SET {sets} WHERE id = ? AND owner_user_id = ?",
            [*allowed.values(), post_id, owner_user_id],
        )
        conn.commit()
        return cur.rowcount > 0


def listings_without_posts(db_path: str, owner_user_id: int, limit: int = 5):
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(
            """SELECT l.* FROM store_listings l
               WHERE l.owner_user_id = ? AND l.status IN ('approved', 'published', 'on_sale')
                 AND NOT EXISTS (SELECT 1 FROM social_posts p WHERE p.listing_id = l.id)
               ORDER BY l.updated_at LIMIT ?""",
            (owner_user_id, limit),
        ))


# --- review queue ------------------------------------------------------------

def create_review_item(
    db_path: str, owner_user_id: int, title: str, kind: str = "other", summary: str | None = None,
    detail: str | None = None, source_agent: str | None = None, ref_table: str | None = None,
    ref_id: int | None = None, priority: str = "normal", options: list[dict] | None = None,
) -> int:
    """Puts something in front of the owner. options is a list of
    {label, description, media_path, body} — none for a yes/no, several for a pick-one."""
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO review_items
               (owner_user_id, title, kind, summary, detail, source_agent, ref_table, ref_id,
                priority, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, title, kind, summary, detail, source_agent, ref_table, ref_id,
             priority, _now()),
        )
        item_id = cur.lastrowid
        for position, option in enumerate(options or []):
            conn.execute(
                """INSERT INTO review_options (item_id, label, description, media_path, body, position)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (item_id, option.get("label") or f"Option {position + 1}", option.get("description"),
                 option.get("media_path"), option.get("body"), position),
            )
        conn.commit()
        return item_id


def add_review_options(db_path: str, owner_user_id: int, item_id: int,
                       options: list[dict]) -> int:
    """Attaches choices to a card that is still pending, after the fact.

    For work that was filed before it could be shown. The art director now renders its
    directions before filing them, but the cards written under the old order are still
    sitting in the queue as text with nothing to look at -- this is how they get their
    pictures without being rewritten or raised again, which would lose their place and
    their age.

    Refuses a decided card: adding a choice to something already ruled on would change
    what the record says he was choosing between.
    """
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status FROM review_items WHERE id = ? AND owner_user_id = ?",
            (item_id, owner_user_id)).fetchone()
        if row is None or row["status"] != "pending":
            return 0
        start = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS n FROM review_options WHERE item_id = ?",
            (item_id,)).fetchone()["n"]
        for offset, option in enumerate(options):
            conn.execute(
                """INSERT INTO review_options (item_id, label, description, media_path, body, position)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (item_id, option.get("label") or f"Option {start + offset + 1}",
                 option.get("description"), option.get("media_path"), option.get("body"),
                 start + offset),
            )
        conn.commit()
        return len(options)


def refresh_review_item_text(
    db_path: str, owner_user_id: int, item_id: int, summary: str | None = None,
    detail: str | None = None,
) -> bool:
    """Rewrites a STILL-PENDING card's summary/detail in place, for a card whose evidence
    keeps accumulating after it was filed.

    The case this exists for is mail_debts.py: a proposed debt's card is filed the first
    time a statement for that account turns up, and every later statement the historical
    sweep finds attaches another balance observation to the same proposal rather than
    filing a second card. Without this, the card he eventually reads would still say what
    was true after one statement while the debt behind it has twelve.

    Deliberately refuses to touch an item he has already decided: rewriting the text of a
    card after the fact would change what he appears to have approved.
    """
    fields = {k: v for k, v in (("summary", summary), ("detail", detail)) if v is not None}
    if not fields:
        return False
    sets = ", ".join(f"{k} = ?" for k in fields)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE review_items SET {sets} WHERE id = ? AND owner_user_id = ? AND status = 'pending'",
            [*fields.values(), item_id, owner_user_id],
        )
        conn.commit()
        return cur.rowcount > 0


def _attach_options(conn, items: list[dict]) -> list[dict]:
    if not items:
        return items
    ids = tuple(i["id"] for i in items)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM review_options WHERE item_id IN ({placeholders}) ORDER BY item_id, position",
        ids,
    ).fetchall()
    by_item: dict[int, list] = {}
    for row in rows:
        by_item.setdefault(row["item_id"], []).append(dict(row))
    for item in items:
        item["options"] = by_item.get(item["id"], [])
    return items


# --- pipeline lanes + urgency -------------------------------------------------
#
# review_items has no pipeline or due-date column of its own -- the Review page used to
# be one flat list, so nothing needed one. Restructuring it into lanes (business/dev_ops/
# mail/personal/other) and real urgency ordering is computed here, from data already on
# the item, rather than adding columns that would need backfilling for every item ever
# created. Grounded in every real create_review_item/create_pending_action_and_review
# call site in the codebase today (business_tools.py, engine.py, camera_watch.py,
# mail_triage.py, vision_runtime.py) -- see classify_pipeline's docstring for the mapping.

# Mirror config.py's mail_sensitive_tools default and setup.py's GIT_SENSITIVE_TOOLS.
# Duplicated here (not imported) on purpose: business_db sits low in the import graph --
# engine.py and setup.py both import *it*, so the reverse import isn't available -- and
# these are just tool-name literals, not behavior, so a copy carries no real drift risk.
_MAIL_ACTION_TOOLS = {"send_email", "archive_email", "delete_email"}
_DEV_OPS_ACTION_TOOLS = {"git_merge_pr"}


def classify_pipeline(item: dict, pending_tool_name: str | None = None) -> str:
    """Which real lane this review item belongs on:

      business  - the creative/product pipeline. Every kind other than 'other' (concept,
                  art, listing, post, media, research) is exactly that pipeline's own
                  output -- see the review_items CHECK constraint -- whether or not it
                  carries a ref_table row yet (a research brief or a rendered image often
                  doesn't).
      dev_ops   - an opened PR (ref_table='git_pull_requests'), a proposed ops/MCP-install
                  plan ('ops_plans'), an employee's capability request
                  ('capability_requests'), or a pending_actions confirmation for
                  git_merge_pr.
      mail      - mail_triage's drafted replies ('email_drafts') and a pending_actions
                  confirmation for send_email/archive_email/delete_email.
      personal  - a camera/vision enrollment ask ('unknown_faces'), a bill detected in
                  the owner's own email ('email_bills' -- mail_bills.py; it arrives BY
                  mail, but it's his money and his due date, so it belongs in the lane
                  his finances already live in rather than the mail-hygiene lane a
                  drafted reply sits in), a provisionally-important email awaiting his
                  verdict ('email_importance_flags' -- mail_importance.py; same
                  reasoning again, and more so: the card asks about his finances,
                  relationships and personal business, not about the mailbox), a debt
                  proposed from his mail history ('debts' -- mail_debts.py; a tracking_
                  state='proposed' row awaiting his confirmation before it counts as his),
                  a linked personal
                  task or credit-dispute item/letter ('personal_tasks', 'dispute_items',
                  'dispute_letters' -- no real call site sets these ref_tables today, but
                  the mapping is here for when one does), and every other pending_actions
                  confirmation: phone, Era (financial), Kroger cart/order writes, CCXT
                  trades, LetterStream (credit-dispute letters), and Home Assistant
                  lock/cover/alarm changes -- the owner's own life, not the business.
      other     - kind='other' with no ref_table Jarvis recognises (a plain ad-hoc
                  yes/no question with nothing to write through to).

    pending_tool_name is the tool_name off the linked pending_actions row. Callers that
    hold a db connection already (list_review_items, get_review_item) resolve it in bulk
    via _attach_pipeline; business_db doesn't own the pending_actions table itself (that's
    db.py's), so this function stays a pure mapping rather than reaching for it directly.
    Not knowing the tool name still lands the item in 'personal' rather than 'other',
    since every sensitive-tool confirmation is personal-life by default except the two
    carve-outs above.
    """
    if (item.get("kind") or "other") != "other":
        return "business"

    ref_table = item.get("ref_table")
    if ref_table in ("git_pull_requests", "ops_plans", "capability_requests"):
        return "dev_ops"
    if ref_table == "email_drafts":
        return "mail"
    if ref_table in ("unknown_faces", "email_bills", "email_importance_flags",
                     "personal_tasks", "dispute_items", "dispute_letters", "debts"):
        return "personal"
    if ref_table == "pending_actions":
        if pending_tool_name in _DEV_OPS_ACTION_TOOLS:
            return "dev_ops"
        if pending_tool_name in _MAIL_ACTION_TOOLS:
            return "mail"
        return "personal"
    return "other"


def _parse_iso(value: str | None):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pending_action_tool_names(conn, ids: list[int]) -> dict:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"SELECT id, tool_name FROM pending_actions WHERE id IN ({placeholders})", ids
        ).fetchall()
    except sqlite3.OperationalError:
        # pending_actions is db.py's table, not business_db's own -- same db file in the
        # real app (see db.py/business_db.py's shared cfg.db_path), but not every test or
        # caller of this module has run db.init_db first. Missing the table just means
        # "can't resolve which tool this was", handled the same as any other unknown.
        return {}
    return {row["id"]: row["tool_name"] for row in rows}


def _personal_task_due_dates(conn, ids: list[int]) -> dict:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"SELECT id, due_at FROM personal_tasks WHERE id IN ({placeholders})", ids
        ).fetchall()
    except sqlite3.OperationalError:
        # Same reasoning as _pending_action_tool_names -- personal_tasks is
        # personal_db.py's table; not every caller has run personal_db.init_personal_db.
        return {}
    return {row["id"]: row["due_at"] for row in rows if row["due_at"]}


def resolve_due_at(db_path: str, item: dict) -> str | None:
    """The real due date behind a review item, when one exists. review_items itself has
    no due-date column -- most items never have one -- so this reaches through
    ref_table/ref_id to whatever pipeline row might carry a real deadline.

    personal_tasks.due_at is the only case wired up today: checked against every real
    create_review_item/create_pending_action_and_review call site in the codebase, and
    it's the clearest real due date that exists anywhere near a review item. Everything
    else either has no due-date concept at all (a PR, an ops plan, a mail draft) or, for
    credit-dispute letters specifically, genuinely has no deadline column on dispute_items
    /dispute_letters to read -- so this returns None for those rather than inventing a
    deadline that doesn't exist, and compute_urgency falls back to priority + age instead.
    """
    ref_table, ref_id = item.get("ref_table"), item.get("ref_id")
    if ref_table != "personal_tasks" or not ref_id:
        return None
    with closing(_connect(db_path)) as conn:
        return _personal_task_due_dates(conn, [ref_id]).get(ref_id)


def compute_urgency(item: dict, due_at: str | None = None, as_of: datetime | None = None) -> float:
    """Higher means more urgent. Three signals, in the order they should matter:

      1. A real due date (from resolve_due_at) -- overdue beats everything, and urgency
         ramps up sharply as the deadline approaches.
      2. priority -- the explicit high/normal/low call, the tie-breaker for anything
         without a due date.
      3. age (time since created_at) -- so something that's simply sat waiting a long
         time visually rises even at normal/low priority with no deadline, rather than
         starving forever behind a stream of fresh high-priority items. This is the
         fallback the task calls for explicitly: don't pretend every item has a due date
         it doesn't have, but don't let "no due date" mean "never rises" either. Capped,
         so a years-old low-priority item still can't outrank something genuinely due
         soon.

    Pure function (no I/O) so a whole list can be scored cheaply, and deterministic for
    tests via `as_of` instead of depending on the real clock.
    """
    now = as_of or datetime.now(timezone.utc)
    created = _parse_iso(item.get("created_at")) or now
    age_hours = max(0.0, (now - created).total_seconds() / 3600)

    score = {"high": 100.0, "normal": 50.0, "low": 10.0}.get(item.get("priority"), 50.0)
    score += min(age_hours / 2, 60.0)  # up to +60 after ~5 days waiting, then flat

    due = _parse_iso(due_at)
    if due is not None:
        hours_until_due = (due - now).total_seconds() / 3600
        if hours_until_due <= 0:
            score += 200.0 + min(-hours_until_due, 200.0)  # overdue -- worse the longer
        elif hours_until_due <= 24:
            score += 150.0
        elif hours_until_due <= 72:
            score += 80.0
        elif hours_until_due <= 24 * 7:
            score += 30.0
    return round(score, 2)


def _attach_pipeline(conn, items: list[dict]) -> list[dict]:
    """Adds 'pipeline', a resolved 'due_at' (may be None), and 'urgency_score' to each
    item -- at most two extra batched queries for the whole list, same reasoning as
    _attach_options."""
    if not items:
        return items
    pending_ids = [i["ref_id"] for i in items if i.get("ref_table") == "pending_actions" and i.get("ref_id")]
    task_ids = [i["ref_id"] for i in items if i.get("ref_table") == "personal_tasks" and i.get("ref_id")]
    tool_names = _pending_action_tool_names(conn, pending_ids)
    due_dates = _personal_task_due_dates(conn, task_ids)
    now = datetime.now(timezone.utc)
    for item in items:
        tool_name = tool_names.get(item.get("ref_id")) if item.get("ref_table") == "pending_actions" else None
        item["pipeline"] = classify_pipeline(item, pending_tool_name=tool_name)
        due_at = due_dates.get(item.get("ref_id")) if item.get("ref_table") == "personal_tasks" else None
        item["due_at"] = due_at
        item["urgency_score"] = compute_urgency(item, due_at, as_of=now)
    return items


def list_review_items(
    db_path: str, owner_user_id: int, status: str | None = "pending", limit: int = 50,
    pipeline: str | None = None,
):
    query = "SELECT * FROM review_items WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    # High priority first, then oldest — so the stack drains in the order it should.
    # (The Review page itself now re-sorts within each pipeline lane by urgency_score;
    # this is still the base order for every caller that doesn't group into lanes, e.g.
    # the chat-facing list_review_queue tool.)
    query += (
        " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,"
        " CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at LIMIT ?"
    )
    # pipeline is computed, not a column, so it can't be filtered in SQL. When it's
    # requested, fetch a bounded multiple of `limit` up front (same ordering) so
    # filtering afterward still has a real shot at returning `limit` matches instead of
    # starving on whatever the plain SQL LIMIT happened to grab first -- bounded rather
    # than unlimited so a pipeline filter on a large decided-history query can't turn
    # into an unbounded table scan.
    params.append(limit * 6 if pipeline else limit)
    with closing(_connect(db_path)) as conn:
        items = _attach_pipeline(conn, _attach_options(conn, _rows(conn.execute(query, params))))
    if pipeline:
        items = [i for i in items if i["pipeline"] == pipeline][:limit]
    return items


def get_review_item(db_path: str, owner_user_id: int, item_id: int):
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM review_items WHERE id = ? AND owner_user_id = ?", (item_id, owner_user_id)
        ).fetchone()
        if row is None:
            return None
        return _attach_pipeline(conn, _attach_options(conn, [dict(row)]))[0]


def decide_review_item(
    db_path: str, owner_user_id: int, item_id: int, decision: str,
    option_id: int | None = None, note: str | None = None,
) -> dict | None:
    """Records the owner's call. Returns the decided item, including which option won, so
    the caller can advance whatever pipeline row it referenced."""
    if decision not in ("approved", "rejected", "cancelled"):
        raise ValueError("decision must be approved, rejected or cancelled")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """UPDATE review_items SET status = ?, decision_note = ?, decided_at = ?
               WHERE id = ? AND owner_user_id = ? AND status = 'pending'""",
            (decision, note, _now(), item_id, owner_user_id),
        )
        if cur.rowcount == 0:
            conn.commit()
            return None
        if option_id is not None:
            # Exactly one winner, even if this item is decided more than once.
            conn.execute("UPDATE review_options SET chosen = 0 WHERE item_id = ?", (item_id,))
            conn.execute(
                "UPDATE review_options SET chosen = 1 WHERE id = ? AND item_id = ?", (option_id, item_id)
            )
        conn.commit()
    return get_review_item(db_path, owner_user_id, item_id)


def list_verdict_examples(
    db_path: str, owner_user_id: int, source_agent: str, status: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """The owner's decided cards for one agent, most recent first.

    Deliberately the same shape as mail_db.list_importance_examples: this is only the
    query, and review_examples.select_examples owns the actual selection strategy
    (recency, balance, asymmetric backfill, cap). Keeping the split identical means the
    one proven learning loop in this codebase and this new one cannot drift apart.

    Ordered by decided_at rather than created_at -- what matters is when he ruled, not
    when the card was raised, and a card he left sitting for a week then rejected is a
    fresher signal than one he answered instantly the day before.
    """
    query = ("SELECT id, title, summary, status, decision_note, decided_at FROM review_items "
             "WHERE owner_user_id = ? AND source_agent = ? AND status != 'pending'")
    params: list = [owner_user_id, source_agent]
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY decided_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))


def get_review_item_by_ref(db_path: str, owner_user_id: int, ref_table: str, ref_id: int):
    """Looks up the review item standing in for a specific pipeline row -- used to keep
    a card in sync when its underlying decision gets made somewhere other than this
    page (e.g. a pending action confirmed in chat), so the same thing isn't left
    dangling as still-pending on the Review page."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT * FROM review_items WHERE owner_user_id = ? AND ref_table = ? AND ref_id = ?
               ORDER BY id DESC LIMIT 1""",
            (owner_user_id, ref_table, ref_id),
        ).fetchone()
        if row is None:
            return None
        return _attach_pipeline(conn, _attach_options(conn, [dict(row)]))[0]


def get_review_option_media(db_path: str, owner_user_id: int, option_id: int) -> str | None:
    """The file path for one option, scoped to its owner so an id from elsewhere can't
    be used to read someone else's media."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT o.media_path FROM review_options o
               JOIN review_items i ON i.id = o.item_id
               WHERE o.id = ? AND i.owner_user_id = ?""",
            (option_id, owner_user_id),
        ).fetchone()
        return row["media_path"] if row else None


def count_pending_reviews(db_path: str, owner_user_id: int) -> int:
    with closing(_connect(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM review_items WHERE owner_user_id = ? AND status = 'pending'",
            (owner_user_id,),
        ).fetchone()["n"]


def stale_review_items(db_path: str, hours: float = 2.0, as_of: str | None = None):
    """Pending Review items that have sat without a decision for longer than `hours`
    and haven't already been flagged to the owner -- the watchdog's way of catching
    "he never came back to this" instead of only ever showing what's currently pending
    to whoever happens to open the Review page.
    """
    cutoff = (
        datetime.fromisoformat(as_of) if as_of
        else datetime.now(timezone.utc)
    ) - timedelta(hours=hours)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(
            "SELECT * FROM review_items WHERE status = 'pending' AND watchdog_notified_at IS NULL"
            " AND created_at <= ? ORDER BY created_at",
            (cutoff.isoformat(),),
        ))


def mark_review_item_watchdog_notified(db_path: str, item_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE review_items SET watchdog_notified_at = ? WHERE id = ?", (_now(), item_id))
        conn.commit()


# --- agent runs --------------------------------------------------------------

def start_agent_run(db_path: str, agent: str) -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO agent_runs (agent, started_at, status) VALUES (?, ?, 'running')", (agent, _now())
        )
        conn.commit()
        return cur.lastrowid


def finish_agent_run(db_path: str, run_id: int, status: str, summary: str = "", detail: str = "") -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE agent_runs SET finished_at = ?, status = ?, summary = ?, detail = ? WHERE id = ?",
            (_now(), status, summary, detail, run_id),
        )
        conn.commit()


def recent_agent_runs(db_path: str, since: str | None = None, limit: int = 20):
    query = "SELECT * FROM agent_runs"
    params: list = []
    if since:
        query += " WHERE started_at >= ?"
        params.append(since)
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return _rows(conn.execute(query, params))
