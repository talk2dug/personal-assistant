"""Hiring — agents defined by a job description rather than by code.

The seven agents in agents.py are hardcoded: each has a hand-written system prompt and a
`run_*` function. That was right while the roster was fixed and business-shaped, but it
means adding a colleague is a code change. This module makes the roster data, so the
owner can hire in chat the way a person actually hires: write the job description, and
the job description *is* the configuration.

The mapping is deliberate, not decorative:

    seniority + skills   -> voice and depth of the system prompt
    responsibilities     -> what the employee is asked to produce
    department           -> which capability tier it gets
    standards            -> the self-check it applies before handing work back

**Employees produce, they do not act.** This keeps agents.py's rule, and for the same
reason: an unattended timer job must not be able to send mail, spend money, or change the
house. A developer here writes code into a work record for review; it does not touch the
repo, run a deploy, or commit. Everything actionable stays a proposal until the owner
approves it in chat, where the existing confirmation gate applies.

Capability tiers exist so a job description cannot quietly grant power. A tier is chosen
from the department at hire time and is the ceiling on what that employee may ever do.
"""
import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    department TEXT NOT NULL,
    seniority TEXT,
    -- The job description exactly as written. Kept verbatim rather than only in its
    -- compiled form so it can be re-read, edited and recompiled later.
    job_description TEXT NOT NULL,
    -- Structured fields pulled out of the description at hire time.
    skills TEXT,
    responsibilities TEXT,
    standards TEXT,
    -- The compiled operating prompt. Derived, but stored: recompiling on every run would
    -- make an employee's behaviour drift silently as the composer changes.
    system_prompt TEXT NOT NULL,
    capability_tier TEXT NOT NULL,
    -- What a scheduled employee does each time their cadence comes round. An employee on
    -- a cadence with nothing standing has no work to do, so the scheduler skips them
    -- rather than inventing a task for them.
    standing_assignment TEXT,
    cadence TEXT NOT NULL DEFAULT 'on_demand',
    -- A shift: how often to work, and between which hours. daily/weekly cadence can't
    -- express "every 15 minutes while the market matters", which is most of what a
    -- monitoring role actually is.
    --
    -- interval_minutes drives the repeat; shift_start/shift_end bound it to a local
    -- time window (null = around the clock, which is the honest default for crypto);
    -- shift_days is a comma list of weekday numbers, Monday=0.
    interval_minutes INTEGER,
    shift_start TEXT,
    shift_end TEXT,
    shift_days TEXT,
    -- What would make this worth interrupting the owner for, in his own words, and how
    -- eagerly to do it. An employee that alerts on everything trains him to ignore it.
    alert_condition TEXT,
    alert_policy TEXT NOT NULL DEFAULT 'never'
        CHECK (alert_policy IN ('never', 'on_alert', 'always')),
    alert_cooldown_min INTEGER NOT NULL DEFAULT 30,
    -- Live data to put in front of this employee on every run, comma separated.
    -- Employees have no tools by design (they gather, they never act), so a monitoring
    -- role cannot fetch anything itself -- the data has to arrive in its context or it
    -- will web-search for a price and report something stale as current.
    data_feeds TEXT,
    -- Colleagues whose latest delivered work is handed to this employee on every run,
    -- comma separated keys. Employees run in isolation with no shared memory, so two who
    -- are meant to work together -- an analyst that flags coins and a trader that trades
    -- them -- otherwise have no channel at all, and the downstream one correctly refuses
    -- to act on a recommendation it was never shown.
    briefing_from TEXT,
    last_alert_at TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'released')),
    character INTEGER NOT NULL DEFAULT 0,
    hired_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_staff_status ON staff(status);

-- What an employee produced, and what happened to it. Work is never applied anywhere by
-- the employee; this table is the hand-off point to the owner's review.
CREATE TABLE IF NOT EXISTS staff_work (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    assignment TEXT NOT NULL,
    output TEXT,
    artifact_kind TEXT,
    status TEXT NOT NULL DEFAULT 'delivered'
        CHECK (status IN ('running', 'delivered', 'failed', 'accepted', 'rejected')),
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_staff_work_staff ON staff_work(staff_id, id DESC);
"""

# What each department is allowed to do. The ceiling, set at hire time from the
# department, so no wording in a job description can widen it.
#
#   research  - web search, reports findings
#   authoring - produces text or code as a deliverable; writes nothing outside staff_work
#   media     - may additionally request image/video generation on the GPU bridge
#   execute   - real git/SSH tools (branch, push, PR, and later ops-plan/SSH); see below
#
# No tier grants mail, Home Assistant, or money movement -- those stay with the owner's
# own confirmed tool calls. "execute" is the one tier that can touch a real repo or
# server, and it is deliberately NOT reachable via department inference below (see
# set_capability_override) -- the same lesson infer_data_feeds's "paper" comment
# documents: inferring a real capability from job-description wording let an employee
# pick it up from an unrelated line describing who it reports to. Granting "execute"
# is an explicit, separate action the owner takes on a specific employee, never a side
# effect of how its job description happens to be worded.
CAPABILITY_TIERS = {
    "engineering": "authoring",
    "design": "media",
    "creative": "media",
    "research": "research",
    "marketing": "authoring",
    "commerce": "authoring",
    "operations": "research",
    "general": "research",
}

TIER_DESCRIPTIONS = {
    "research": "search the web and report findings",
    "authoring": "search the web and write text or code as a deliverable",
    "media": "search the web, write copy, and request image or video generation",
    "execute": "propose and, once approved, carry out real changes to code repositories and servers",
}

# Sprite indices the office UI has art for.
CHARACTER_COUNT = 6


def init_staff_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Idempotent migration, same pattern as db.py: CREATE TABLE IF NOT EXISTS won't
        # add a column to a table that already exists from an earlier version.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(staff)")}
        for name, ddl in (
            ("standing_assignment", "TEXT"),
            ("interval_minutes", "INTEGER"),
            ("shift_start", "TEXT"),
            ("shift_end", "TEXT"),
            ("shift_days", "TEXT"),
            ("alert_condition", "TEXT"),
            # No CHECK on the added column: SQLite cannot add a constrained column to an
            # existing table, and the value is validated in hire()/set_shift anyway.
            ("alert_policy", "TEXT NOT NULL DEFAULT 'never'"),
            ("alert_cooldown_min", "INTEGER NOT NULL DEFAULT 30"),
            ("last_alert_at", "TEXT"),
            ("data_feeds", "TEXT"),
            ("briefing_from", "TEXT"),
            # Explicit, separate override for capability_tier -- see set_capability_override
            # and the note above CAPABILITY_TIERS. NULL means "use whatever department
            # inference gives it," same as every employee hired before this column existed.
            ("capability_override", "TEXT"),
        ):
            if name not in cols:
                conn.execute(f"ALTER TABLE staff ADD COLUMN {name} {ddl}")
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_key(title: str, existing: set[str] | None = None) -> str:
    """A stable identifier from a job title. Collides gracefully: a second Developer
    becomes developer_2 rather than failing the hire."""
    base = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "employee"
    existing = existing or set()
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def infer_department(title: str, job_description: str) -> str:
    """Pick a department from the role, which in turn picks the capability ceiling.

    Keyword matching rather than a model call: this decides what an employee is allowed
    to do, and that decision should be inspectable and identical every time, not
    something a language model improvises during a hire.
    """
    text = f"{title} {job_description}".lower()
    signals = [
        ("engineering", ("developer", "engineer", "programmer", "architect", "devops",
                         "backend", "back-end", "full stack", "full-stack", "sre",
                         "javascript", "python", "typescript", "c++", "nodejs", "node.js")),
        ("design", ("designer", "ux", "ui ", "front end", "front-end", "frontend",
                    "visual", "brand", "typography")),
        ("creative", ("artist", "illustrat", "creative", "copywriter", "photograph",
                      "video", "art director")),
        ("marketing", ("marketing", "seo", "social media", "growth", "campaign", "ads")),
        ("commerce", ("sales", "store", "listing", "ecommerce", "e-commerce", "pricing",
                      "merchandis")),
        ("research", ("research", "analyst", "analysis", "market", "trend", "intelligence")),
        ("operations", ("operations", "ops", "logistics", "supply", "inventory",
                        "scheduling", "admin")),
    ]
    # Word boundaries matter here. A plain substring test matched "ads" inside "reads",
    # which filed a crypto research analyst under marketing and handed it the authoring
    # tier instead of research -- a real capability difference decided by a coincidence
    # of spelling. Trailing +/#/. are kept so "c++", "c#" and "node.js" still match.
    for dept, words in signals:
        for w in words:
            pattern = r"(?<![a-z0-9])" + re.escape(w.strip()) + r"(?![a-z0-9])"
            if re.search(pattern, text):
                return dept
    return "general"


def infer_seniority(job_description: str) -> str:
    text = job_description.lower()
    m = re.search(r"(\d{1,2})\+?\s*years?", text)
    years = int(m.group(1)) if m else None
    if years is not None:
        if years >= 15:
            return "principal"
        if years >= 8:
            return "senior"
        if years >= 3:
            return "mid"
        return "junior"
    for word, level in (("principal", "principal"), ("staff engineer", "principal"),
                        ("lead", "senior"), ("senior", "senior"), ("head of", "principal"),
                        ("junior", "junior"), ("entry", "junior")):
        if word in text:
            return level
    return "mid"


def extract_skills(job_description: str) -> list[str]:
    """Pull named technologies and disciplines out of the description.

    Only things actually written down — a job description that says C++ gets C++, and one
    that doesn't, doesn't. Inventing skills would make the employee claim expertise the
    owner never asked for.
    """
    known = [
        "javascript", "typescript", "python", "node.js", "nodejs", "react", "vue",
        "svelte", "c++", "c#", "rust", "go", "golang", "java", "kotlin", "swift",
        "php", "ruby", "sql", "postgres", "sqlite", "mysql", "mongodb", "redis",
        "docker", "kubernetes", "aws", "azure", "gcp", "terraform", "linux", "bash",
        "html", "css", "tailwind", "figma", "photoshop", "illustrator", "blender",
        "fastapi", "django", "flask", "express", "graphql", "rest", "grpc",
        "accessibility", "responsive design", "design systems", "typography",
        "seo", "copywriting", "branding", "animation", "3d", "cad",
    ]
    text = job_description.lower()
    found = []
    for k in known:
        # Word-ish boundary so "go" doesn't match "going" and "c#" survives punctuation.
        pattern = r"(?<![a-z0-9+#])" + re.escape(k) + r"(?![a-z0-9+#])"
        if re.search(pattern, text):
            found.append(k)
    return found


def compile_system_prompt(title: str, job_description: str, department: str,
                          seniority: str, skills: list[str], tier: str) -> str:
    """Turn a job description into the employee's operating prompt.

    The description supplies who they are and what good looks like; this adds the house
    rules that apply to everyone, chiefly that they hand work back rather than acting on
    it themselves.
    """
    skill_line = ""
    if skills:
        skill_line = ("Your working knowledge specifically covers: "
                      + ", ".join(skills) + ".\n")

    seniority_line = {
        "principal": ("You have deep, long-range experience. You think about how a "
                      "decision ages, name the trade-offs, and say plainly when the "
                      "brief itself is the problem."),
        "senior": ("You are experienced and pragmatic. You choose boring, proven "
                   "approaches over clever ones and explain why."),
        "mid": "You are competent and careful, and you ask when something is ambiguous.",
        "junior": ("You are early in your career: you follow established patterns and "
                   "flag anything you are unsure about rather than guessing."),
    }.get(seniority, "You are competent and careful.")

    return (
        f"You are {title}, working for the owner of a small maker business.\n\n"
        f"YOUR ROLE, as hired:\n{job_description.strip()}\n\n"
        f"{seniority_line}\n{skill_line}\n"
        "HOW YOU WORK:\n"
        f"- You may {TIER_DESCRIPTIONS.get(tier, 'search the web and report findings')}.\n"
        "- You produce work and hand it back for review. You never deploy, publish, "
        "send, purchase, or change any live system yourself, and you never claim to "
        "have done so.\n"
        "- You state what you actually did. If you could not verify something, you say "
        "so rather than presenting a guess as fact. A confident wrong answer costs more "
        "than an honest 'I don't know'.\n"
        "- You are concise. The owner is busy and reads everything you write.\n"
        "- If the assignment is underspecified in a way that changes the answer, say "
        "which detail you need instead of inventing it."
    )


# --- hiring -------------------------------------------------------------------

MARKET_SIGNALS = ("crypto", "bitcoin", "btc", "ethereum", "altcoin", "token",
                  "trading", "trader", "market cap", "coin", "defi", "exchange")

# "paper" is deliberately absent from inference. It hands out a ledger that can be
# traded, and this module's whole premise is that a job description must not be able to
# grant its own powers -- inferring it from wording would let any employee that merely
# *mentions* the trading desk start taking positions. It was tried, and the research
# analyst picked it up from a line describing who it reports to. Grant it explicitly with
# set_data_feeds.


def infer_data_feeds(title: str, job_description: str, standing: str = "") -> str:
    """Which live feeds an employee should be handed on every run.

    Inferred rather than configured because the alternative is hiring a crypto analyst
    that cannot see the crypto feed, which is what happened the first time. Keyword-based
    and therefore inspectable; `data_feeds` can be set explicitly to override it.
    """
    text = f"{title} {job_description} {standing}".lower()
    feeds = []  # read-only context only; anything that can act is granted by hand
    if any(w in text for w in MARKET_SIGNALS):
        feeds.append("market")
    return ",".join(feeds)


def hire(db_path: str, title: str, job_description: str, cadence: str = "on_demand",
         department: str | None = None, character: int | None = None,
         standing_assignment: str | None = None, interval_minutes: int | None = None,
         shift_start: str | None = None, shift_end: str | None = None,
         shift_days: str | None = None, alert_condition: str | None = None,
         alert_policy: str = "never", alert_cooldown_min: int = 30,
         data_feeds: str | None = None) -> dict:
    """Create an employee from a job description."""
    if not title.strip():
        raise ValueError("a job title is required")
    if len(job_description.strip()) < 20:
        raise ValueError("the job description is too thin to configure an employee from; "
                         "describe the experience, skills and responsibilities")

    if cadence not in ("on_demand", "interval", "daily", "weekly"):
        raise ValueError("cadence must be on_demand, interval, daily or weekly")
    if cadence == "interval" and not (interval_minutes and interval_minutes > 0):
        raise ValueError("an interval cadence needs interval_minutes, e.g. 15")
    if alert_policy not in ("never", "on_alert", "always"):
        raise ValueError("alert_policy must be never, on_alert or always")
    if alert_policy != "never" and not (alert_condition or "").strip():
        raise ValueError("alerting needs alert_condition: what is worth interrupting him for")

    department = department or infer_department(title, job_description)
    tier = CAPABILITY_TIERS.get(department, "research")
    seniority = infer_seniority(job_description)
    skills = extract_skills(job_description)
    prompt = compile_system_prompt(title, job_description, department, seniority, skills, tier)

    with closing(_connect(db_path)) as conn:
        existing = {r["key"] for r in conn.execute("SELECT key FROM staff")}
        key = make_key(title, existing)
        count = conn.execute("SELECT COUNT(*) c FROM staff").fetchone()["c"]
        char = character if character is not None else count % CHARACTER_COUNT
        conn.execute(
            """INSERT INTO staff (key, title, department, seniority, job_description,
                                  skills, responsibilities, standards, system_prompt,
                                  capability_tier, standing_assignment, cadence, status,
                                  character, hired_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
            (key, title.strip(), department, seniority, job_description.strip(),
             json.dumps(skills), None, None, prompt, tier,
             (standing_assignment or "").strip() or None, cadence, char,
             _now(), _now()))
        conn.execute(
            """UPDATE staff SET interval_minutes = ?, shift_start = ?, shift_end = ?,
                   shift_days = ?, alert_condition = ?, alert_policy = ?,
                   alert_cooldown_min = ? WHERE key = ?""",
            (interval_minutes, shift_start, shift_end, shift_days,
             (alert_condition or "").strip() or None, alert_policy,
             alert_cooldown_min, key))
        feeds = data_feeds if data_feeds is not None else infer_data_feeds(
            title, job_description, standing_assignment or "")
        conn.execute("UPDATE staff SET data_feeds = ? WHERE key = ?",
                     (feeds or None, key))
        conn.commit()
        return dict(conn.execute("SELECT * FROM staff WHERE key = ?", (key,)).fetchone())


def list_staff(db_path: str, include_released: bool = False) -> list[dict]:
    sql = "SELECT * FROM staff"
    if not include_released:
        sql += " WHERE status != 'released'"
    sql += " ORDER BY hired_at"
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql)]


def get_staff(db_path: str, key: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM staff WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None


def set_status(db_path: str, key: str, status: str) -> bool:
    if status not in ("active", "paused", "released"):
        raise ValueError("status must be active, paused or released")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE staff SET status = ?, updated_at = ?, released_at = ? WHERE key = ?",
            (status, _now(), _now() if status == "released" else None, key))
        conn.commit()
        return cur.rowcount > 0


def revise_job_description(db_path: str, key: str, job_description: str) -> dict | None:
    """Rewrite the description and recompile the prompt — a change of duties, not a
    re-hire, so the employee keeps its key, history and seat."""
    emp = get_staff(db_path, key)
    if emp is None:
        return None
    department = infer_department(emp["title"], job_description)
    # An explicitly granted capability_override survives a description rewrite -- without
    # this, revising an execute-tier employee's duties would silently strip the grant back
    # to whatever department inference gives it, since inference never produces "execute".
    tier = emp["capability_override"] or CAPABILITY_TIERS.get(department, "research")
    seniority = infer_seniority(job_description)
    skills = extract_skills(job_description)
    prompt = compile_system_prompt(emp["title"], job_description, department, seniority,
                                   skills, tier)
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """UPDATE staff SET job_description = ?, department = ?, seniority = ?,
                   skills = ?, system_prompt = ?, capability_tier = ?, updated_at = ?
               WHERE key = ?""",
            (job_description.strip(), department, seniority, json.dumps(skills),
             prompt, tier, _now(), key))
        conn.commit()
    return get_staff(db_path, key)


def set_capability_override(db_path: str, key: str, tier: str | None) -> dict | None:
    """Explicitly grant (or revoke) a capability tier, independent of what department
    inference would otherwise give this employee. This is the ONLY way an employee ever
    reaches the "execute" tier -- see the note above CAPABILITY_TIERS for why that has to
    be a separate, deliberate action rather than something a job description can trigger
    by wording alone. Pass tier=None to revert to inferred behavior."""
    if tier is not None and tier not in TIER_DESCRIPTIONS:
        raise ValueError(f"unknown capability tier {tier!r}; valid: {', '.join(sorted(TIER_DESCRIPTIONS))}")
    emp = get_staff(db_path, key)
    if emp is None:
        return None
    effective_tier = tier or CAPABILITY_TIERS.get(emp["department"], "research")
    skills = json.loads(emp["skills"] or "[]")
    prompt = compile_system_prompt(
        emp["title"], emp["job_description"], emp["department"], emp["seniority"], skills, effective_tier)
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """UPDATE staff SET capability_override = ?, capability_tier = ?,
                   system_prompt = ?, updated_at = ? WHERE key = ?""",
            (tier, effective_tier, prompt, _now(), key))
        conn.commit()
    return get_staff(db_path, key)


# --- working ------------------------------------------------------------------

def _fmt_price(v) -> str:
    """Enough significant figures to be actionable at any magnitude.

    A fixed 6dp prints a token trading at 4e-9 as "$0.000000", which reads as worthless
    rather than as a number -- and an employee asked to judge a move cannot judge one it
    cannot see.
    """
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


def set_data_feeds(db_path: str, key: str, feeds: str) -> bool:
    """Set which live feeds an employee receives. Explicit by design for anything that
    can act on the world, simulated or not -- see the note on PAPER inference above."""
    valid = {"market", "paper"}
    wanted = [f.strip().lower() for f in (feeds or "").split(",") if f.strip()]
    unknown = [f for f in wanted if f not in valid]
    if unknown:
        raise ValueError(f"unknown feed(s): {', '.join(unknown)}; valid: {', '.join(sorted(valid))}")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute("UPDATE staff SET data_feeds = ?, updated_at = ? WHERE key = ?",
                           (",".join(wanted) or None, _now(), key))
        conn.commit()
        return cur.rowcount > 0


def set_briefing_from(db_path: str, key: str, colleague_keys: str) -> bool:
    """Give an employee sight of named colleagues' latest work on every run."""
    wanted = [k.strip() for k in (colleague_keys or "").split(",") if k.strip()]
    with closing(_connect(db_path)) as conn:
        known = {r["key"] for r in conn.execute("SELECT key FROM staff")}
        unknown = [k for k in wanted if k not in known]
        if unknown:
            raise ValueError(f"no employee with key(s): {', '.join(unknown)}")
        if key in wanted:
            raise ValueError("an employee cannot be briefed from itself")
        cur = conn.execute("UPDATE staff SET briefing_from = ?, updated_at = ? WHERE key = ?",
                           (",".join(wanted) or None, _now(), key))
        conn.commit()
        return cur.rowcount > 0


def build_colleague_briefing(db_path: str, briefing_from: str | None,
                             max_age_hours: int = 6, chars: int = 2500) -> str:
    """The latest delivered work of named colleagues.

    Age-bounded and labelled with its timestamp: a six-hour-old call on a market that
    moves by the minute is context, not an instruction, and the reader has to be able to
    tell the difference. Stale work is omitted rather than passed off as current.
    """
    keys = [k.strip() for k in (briefing_from or "").split(",") if k.strip()]
    if not keys:
        return ""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    blocks = []
    with closing(_connect(db_path)) as conn:
        for key in keys:
            row = conn.execute(
                """SELECT s.title, w.output, w.finished_at
                     FROM staff_work w JOIN staff s ON s.id = w.staff_id
                    WHERE s.key = ? AND w.status = 'delivered' AND w.output IS NOT NULL
                      AND w.finished_at > ?
                    ORDER BY w.id DESC LIMIT 1""", (key, cutoff)).fetchone()
            if row is None:
                blocks.append(f"FROM {key}: nothing delivered in the last {max_age_hours}h. "
                              f"Do not invent their view; act on the raw data or stand down.")
                continue
            body = (row["output"] or "").strip()
            # Their execution report is about their ledger, not yours -- drop it.
            body = body.split("--- EXECUTION REPORT")[0].strip()
            blocks.append(f"FROM {row['title']} (delivered {row['finished_at'][11:16]}Z):\n"
                          + body[:chars])
    return ("\n\n--- COLLEAGUE HANDOFF: their latest work, for your context. It is their "
            "judgement, not an order, and it may already be out of date. ---\n"
            + "\n\n".join(blocks)
            + "\n--- end handoff ---\n")


def build_feed_briefing(db_path: str, feeds: str | None) -> str:
    """Compact, current data for an employee that cannot fetch anything itself.

    Deliberately pre-digested rather than a data dump: movers first because that is the
    question a monitoring shift is actually asking, and feed health included because an
    employee must be able to tell a quiet market from a dead feed. Reading the local
    cache costs nothing, so this is attached on every run rather than on request.
    """
    if not feeds:
        return ""
    parts = []
    if "market" in feeds:
        try:
            from . import market_data
            status = market_data.feed_status(db_path)
            if status.get("stale"):
                parts.append(
                    "CRYPTO FEED: STALE — the last poll was "
                    f"{status.get('seconds_since_poll')}s ago. Do NOT report market "
                    "conditions from this data, and say the feed is stale instead.")
            else:
                movers = market_data.movers(db_path, window="hour", min_abs_pct=2.0, limit=10)
                day = market_data.movers(db_path, window="day", min_abs_pct=5.0, limit=10)
                top = market_data.snapshot(db_path, codes=["BTC", "ETH"], limit=2)
                listings = market_data.new_listings(db_path, hours=24, limit=10)

                lines = [f"CRYPTO FEED (live cache, {status['coins_tracked']} coins, "
                         f"{status.get('seconds_since_poll')}s old):"]
                for c in top:
                    lines.append(f"  {c['code']} {_fmt_price(c['price_usd'])} "
                                 f"1h {c['change_1h_pct']}% 24h {c['change_24h_pct']}%")
                if movers:
                    lines.append("  movers, last hour:")
                    for m in movers:
                        lines.append(f"    {m['code']} {m['change_hour_pct']:+.2f}% "
                                     f"({_fmt_price(m['price_usd'])}, rank {m['rank']})")
                if day:
                    lines.append("  movers, 24h:")
                    for m in day:
                        lines.append(f"    {m['code']} {m['change_day_pct']:+.2f}% "
                                     f"({_fmt_price(m['price_usd'])}, rank {m['rank']})")
                if listings:
                    lines.append("  tokens entering/leaving the tracked set (24h): "
                                 + ", ".join(f"{l['code']} {l['event']}" for l in listings))
                parts.append("\n".join(lines))
        except Exception as e:
            parts.append(f"CRYPTO FEED: unavailable ({type(e).__name__}). "
                         "Say so rather than guessing at prices.")
    if "paper" in feeds:
        try:
            from . import paper_trading
            snap = paper_trading.portfolio(db_path)
            lines = [f"PAPER PORTFOLIO '{snap['account']}' — simulated, no real money:",
                     f"  cash ${snap['cash']:,.2f} | holdings ${snap['holdings_value']:,.2f} "
                     f"| equity ${snap['equity']:,.2f} "
                     f"({snap['total_return_pct']:+.2f}% from ${snap['starting_cash']:,.0f})",
                     f"  realised ${snap['realized_pnl']:,.2f} | "
                     f"unrealised ${snap['unrealized_pnl']:,.2f} | {snap['trades']} trades so far"]
            if snap["positions"]:
                lines.append("  open positions:")
                for pos in snap["positions"]:
                    lines.append(
                        f"    {pos['code']} {pos['qty']:.6g} @ avg {_fmt_price(pos['avg_cost'])}"
                        f" now {_fmt_price(pos['price'])} -> {pos['unrealized_pct']:+.2f}%"
                        f" (${pos['unrealized']:+,.2f})")
            else:
                lines.append("  open positions: none — fully in cash")
            if snap["unpriced"]:
                lines.append("  NOT PRICEABLE (left the tracked set): "
                             + ", ".join(snap["unpriced"]))
            # What the ledger did with the last orders. Without this the employee cannot
            # tell a filled trade from a rejected one, and reasons onward from a position
            # it does not actually hold.
            fills = paper_trading.recent_trades(db_path, limit=5)
            if fills:
                lines.append("  recent fills:")
                for f in fills:
                    r = f" realised ${f['realized']:+,.2f}" if f["realized"] is not None else ""
                    lines.append(f"    {f['at'][11:16]}Z {f['side']} {f['qty']:.6g} {f['code']}"
                                 f" @ {_fmt_price(f['price'])}{r}")
            rejects = paper_trading.recent_rejections(db_path, limit=4)
            if rejects:
                lines.append("  orders REJECTED (these did not happen):")
                for r in rejects:
                    lines.append(f"    {r['side']} {r['code']}: {r['reason']}")
            parts.append("\n".join(lines))
        except Exception as e:
            parts.append(f"PAPER PORTFOLIO: unavailable ({type(e).__name__}). "
                         "Do not trade this run.")

    if not parts:
        return ""
    return ("\n\n--- LIVE DATA, captured just now. These figures are exact and "
            "current; do not web-search for prices and do not contradict them. ---\n"
            + "\n\n".join(parts)
            + "\n--- end live data ---\n")


def _apply_paper_orders(db_path: str, output: str, staff_key: str) -> str:
    """Fill any orders the employee proposed, and return a note for the work record."""
    from . import paper_trading
    try:
        orders = paper_trading.parse_orders(output)
        if not orders:
            return ""
        result = paper_trading.execute_orders(db_path, orders, staff_key=staff_key)
    except Exception as e:
        logger.exception("paper order execution failed for %s", staff_key)
        return f"\n\n[EXECUTION FAILED: {type(e).__name__}: {e} — no orders were filled]"

    lines = ["\n\n--- EXECUTION REPORT (by the ledger, not the employee) ---"]
    for f in result["fills"]:
        realized = f" realised ${f['realized']:+,.2f}" if "realized" in f else ""
        lines.append(f"FILLED {f['side']} {f['qty']:.6g} {f['code']} @ {f['price']:.8g} "
                     f"= ${f['usd']:,.2f} (fee ${f['fee']:,.2f}){realized}")
    for r in result["rejections"]:
        lines.append(f"REJECTED {r['order'].get('side')} {r['order'].get('code')}: {r['reason']}")
    p = result["portfolio"]
    lines.append(f"Equity ${p['equity']:,.2f} | cash ${p['cash']:,.2f} | "
                 f"{p['total_return_pct']:+.2f}% since inception")
    logger.info("paper trades by %s: %d filled, %d rejected, equity $%.2f",
                staff_key, len(result["fills"]), len(result["rejections"]), p["equity"])
    return "\n".join(lines)


def assign(db_path: str, llm, key: str, assignment: str, timeout: int = 10800) -> dict:
    """Give an employee a piece of work and record what came back.

    Every tier but "execute" runs through llm.research(), which carries web search and,
    since a real incident showed the gap, exactly one more tool: request_capability —
    the same path the background agents use, plus the one way an employee can ask for
    more instead of guessing or stubbing around a missing capability. "execute" is the
    one tier that can act (see the note above CAPABILITY_TIERS) — it runs through
    llm.engineer() instead, with real but narrowly-scoped tools (git, including reading
    the actual repo, and SSH/ops-plan), never the owner's full catalog.

    `timeout` is the ClaudeCLIClient subprocess's hard ceiling (config's
    staff_assignment_timeout_seconds) — real dev-team/coding assignments can legitimately
    run much longer than a chat turn, and a real incident showed a too-short default here
    silently kills an in-progress job via subprocess.TimeoutExpired, losing the work and
    only ever recording it as a generic failure.
    """
    emp = get_staff(db_path, key)
    if emp is None:
        return {"ok": False, "error": f"no employee with key {key!r}"}
    if emp["status"] != "active":
        return {"ok": False, "error": f"{emp['title']} is {emp['status']}, not active"}

    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO staff_work (staff_id, assignment, status, started_at)
               VALUES (?, ?, 'running', ?)""", (emp["id"], assignment, _now()))
        conn.commit()
        work_id = cur.lastrowid

    try:
        # Employees have no tools, so any live data they need must be in the prompt.
        feeds = emp.get("data_feeds") or ""
        briefing = build_feed_briefing(db_path, feeds)
        prompt = assignment + build_colleague_briefing(db_path, emp.get("briefing_from")) + briefing
        if "paper" in feeds:
            from . import paper_trading
            prompt += paper_trading.ORDER_INSTRUCTIONS.format(
                fee_pct=paper_trading.DEFAULT_FEE_PCT,
                max_pct=paper_trading.MAX_ORDER_PCT_OF_EQUITY)

        if emp["capability_tier"] == "execute":
            if not hasattr(llm, "engineer"):
                raise RuntimeError(
                    "this LLM backend has no engineer() method, so execute-tier "
                    "employees cannot be given real tool access on it")
            from .git_tools import GIT_TOOLS
            from .business_tools import OPS_PLAN_TOOLS, REQUEST_CAPABILITY_TOOLS
            output = llm.engineer(
                prompt, system_prompt=emp["system_prompt"],
                tools=GIT_TOOLS + OPS_PLAN_TOOLS + REQUEST_CAPABILITY_TOOLS, timeout=timeout,
                employee_key=emp["key"])
        else:
            from .business_tools import REQUEST_CAPABILITY_TOOLS
            output = llm.research(
                prompt, system_prompt=emp["system_prompt"], timeout=timeout,
                tools=REQUEST_CAPABILITY_TOOLS, employee_key=emp["key"])
        status, error = "delivered", None

        if "paper" in feeds and output:
            # The employee proposed; the ledger decides. What actually happened is
            # appended to the stored output, so the work record reflects the fills rather
            # than the intentions -- a model that says "bought SOL" after a rejected order
            # would otherwise leave a false trade history behind it.
            output += _apply_paper_orders(db_path, output, emp["key"])
    except Exception as e:
        output, status, error = None, "failed", f"{type(e).__name__}: {e}"

    with closing(_connect(db_path)) as conn:
        conn.execute(
            """UPDATE staff_work SET output = ?, status = ?, error = ?, finished_at = ?
               WHERE id = ?""", (output, status, error, _now(), work_id))
        conn.commit()

    return {"ok": status == "delivered", "work_id": work_id, "employee": emp["title"],
            "output": output, "error": error}


def recent_work(db_path: str, key: str | None = None, limit: int = 20) -> list[dict]:
    sql = """SELECT w.*, s.title, s.key FROM staff_work w
             JOIN staff s ON s.id = w.staff_id"""
    params: list = []
    if key:
        sql += " WHERE s.key = ?"
        params.append(key)
    sql += " ORDER BY w.id DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


CADENCE_HOURS = {"daily": 24, "weekly": 168}

# What a monitoring employee has to append so the scheduler can tell "nothing to report"
# from "wake him up". Free text can't be acted on, and asking the model to decide whether
# to send a push directly would give an unattended job a way to reach the owner's phone
# on its own judgement alone.
VERDICT_INSTRUCTIONS = (
    "\n\nWhen you have finished, end your reply with one line of JSON on its own, "
    "exactly in this form and nothing after it:\n"
    '{"alert": true or false, "urgency": "low" or "normal" or "high", "headline": "one short sentence"}\n'
    "Set alert to true ONLY if this condition is genuinely met: __CONDITION__\n"
    "Everything else is false. A false alarm costs the owner's attention and teaches him "
    "to ignore you, which is worse than missing one move. If the data you needed was "
    "unavailable or stale, say so in the body and set alert to false -- never alert on a "
    "guess, and never present an inference as a confirmed price or event."
)


def build_verdict_instructions(condition: str) -> str:
    """Substitute the owner's alert condition into the verdict block.

    str.replace, deliberately not str.format: this template contains a literal JSON
    example, and format() reads `{"alert": ...}` as a field name and raises KeyError.
    That is not hypothetical -- it silently stopped every scheduled employee from running
    for over an hour. Any prompt template holding JSON has the same hazard.
    """
    return VERDICT_INSTRUCTIONS.replace("__CONDITION__", condition)


def _interval_minutes(row) -> float | None:
    """How often this employee should run, in minutes, or None if not on a schedule."""
    if row["cadence"] == "interval":
        return float(row["interval_minutes"] or 0) or None
    if row["cadence"] in CADENCE_HOURS:
        return CADENCE_HOURS[row["cadence"]] * 60.0
    return None


def _parse_hhmm(value: str | None):
    if not value:
        return None
    try:
        hh, mm = value.strip().split(":")
        return int(hh) % 24, int(mm) % 60
    except (ValueError, AttributeError):
        return None


def on_shift(row, local_now: datetime) -> bool:
    """Is this employee within its working window right now?

    A null window means around the clock, which is the right default for a market that
    never closes. Windows that wrap midnight (22:00-06:00) are supported, because a
    monitoring shift crossing midnight is ordinary rather than exotic.
    """
    days = (row["shift_days"] or "").strip()
    if days:
        try:
            allowed = {int(d) for d in days.replace(" ", "").split(",") if d != ""}
            if local_now.weekday() not in allowed:
                return False
        except ValueError:
            pass

    start, end = _parse_hhmm(row["shift_start"]), _parse_hhmm(row["shift_end"])
    if start is None or end is None:
        return True
    now_m = local_now.hour * 60 + local_now.minute
    start_m, end_m = start[0] * 60 + start[1], end[0] * 60 + end[1]
    if start_m == end_m:
        return True
    if start_m < end_m:
        return start_m <= now_m < end_m
    return now_m >= start_m or now_m < end_m       # wraps midnight


def due_for_cadence(db_path: str, tz_name: str = "UTC") -> list[dict]:
    """Active employees whose standing assignment is due again, and who are on shift.

    Due-ness is measured from the last time they actually finished work, not from a fixed
    timetable. A service restart therefore doesn't re-run everyone, and an employee whose
    last run overran doesn't get a second one queued behind it -- the same reasoning as
    _first_run_at in scheduler.py.
    """
    from datetime import timedelta
    try:
        from zoneinfo import ZoneInfo
        local_now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        local_now = datetime.now(timezone.utc)

    now = datetime.now(timezone.utc)
    due = []
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT * FROM staff
                WHERE status = 'active'
                  AND cadence != 'on_demand'
                  AND standing_assignment IS NOT NULL""").fetchall()
        for row in rows:
            every = _interval_minutes(row)
            if every is None or not on_shift(row, local_now):
                continue
            last = conn.execute(
                """SELECT finished_at FROM staff_work
                    WHERE staff_id = ? AND status IN ('delivered', 'failed')
                    ORDER BY id DESC LIMIT 1""", (row["id"],)).fetchone()
            if last is None or not last["finished_at"]:
                due.append(dict(row))
                continue
            try:
                finished = datetime.fromisoformat(last["finished_at"])
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=timezone.utc)
            except ValueError:
                due.append(dict(row))
                continue
            if now - finished >= timedelta(minutes=every):
                due.append(dict(row))
    return due


def parse_verdict(output: str | None) -> dict:
    """Pull the trailing JSON verdict out of a deliverable.

    Tolerant on purpose: a model that wraps the line in a code fence, or adds a stray
    sentence after it, should still be understood. A verdict that cannot be parsed at all
    is treated as 'no alert' -- failing closed, because the failure mode of guessing is a
    3am push notification about nothing.
    """
    if not output:
        return {"alert": False, "urgency": "low", "headline": "", "parsed": False}
    for line in reversed([l.strip().strip("`") for l in output.strip().splitlines()]):
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if "alert" in data:
            return {
                "alert": bool(data.get("alert")),
                "urgency": str(data.get("urgency", "normal")).lower(),
                "headline": str(data.get("headline", ""))[:300],
                "parsed": True,
            }
    return {"alert": False, "urgency": "low", "headline": "", "parsed": False}


def _mark_alerted(db_path: str, key: str) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE staff SET last_alert_at = ? WHERE key = ?", (_now(), key))
        conn.commit()


def _alert_allowed(row, cooldown_min: int) -> bool:
    """Rate-limit alerts per employee, so a persistent condition doesn't buzz every run."""
    if not row["last_alert_at"]:
        return True
    from datetime import timedelta
    try:
        last = datetime.fromisoformat(row["last_alert_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last >= timedelta(minutes=cooldown_min)


def format_alert_text(headline: str, body: str, urgency: str, person: dict) -> str:
    """The exact text one employee escalation becomes on the owner's phone.

    Shared so the message reads identically regardless of which caller actually delivers
    it: the synchronous run_due path below, scheduler.py's _staff_alert (used when
    run_due is called with no work_queue), and work_queue.py's cadence-result handler
    (used when it is) all format through this one function rather than three copies that
    could quietly drift apart.
    """
    prefix = {"high": "URGENT", "normal": "", "low": "FYI"}.get(urgency, "")
    title = f"{person['title']}: {headline}".strip()
    return f"{prefix + ' - ' if prefix else ''}{title}\n\n{(body or '').strip()[:1200]}"


def handle_cadence_outcome(db_path: str, person: dict, outcome: dict, notify=None) -> dict:
    """What to do once one scheduled employee's assignment has actually finished:
    decide whether it's worth alerting the owner, respecting alert_policy and the
    per-employee cooldown, then record the verdict either way.

    Called synchronously by run_due today for every due employee in turn; called once,
    asynchronously, by work_queue.py's worker for a 'cadence' job once staff.assign()
    (run by the worker, not this function) actually returns. Either way this is the
    single place that turns an assign() outcome into an alert-or-not decision, so the
    two paths can never silently diverge on when the owner gets told.

    `notify(headline, body, urgency, employee)` is invoked in two cases: the employee's
    own verdict said the owner's stated condition was met (subject to alert_policy and
    the cooldown), OR the run itself failed to deliver at all (crash, timeout, bad
    output) -- that second case ignores alert_policy entirely and only respects the
    cooldown, since a run failing to happen is not the kind of thing "never alert me on
    findings" was meant to silence. A failed/timed-out run that never notified regardless
    of policy was a real, previously-shipped gap -- a coding job could die mid-task and
    the owner would never find out short of checking manually.
    """
    verdict = parse_verdict(outcome.get("output"))
    alerted = False
    cooldown = int(person["alert_cooldown_min"] or 30)

    if not outcome.get("ok"):
        if notify is not None and _alert_allowed(person, cooldown):
            try:
                notify(f"{person['title']} run failed",
                       outcome.get("error") or "no error detail recorded",
                       "normal", person)
                _mark_alerted(db_path, person["key"])
                alerted = True
            except Exception:
                logger.exception("failure-alerting for %s failed", person["key"])
    else:
        policy = person["alert_policy"] or "never"
        wants = (policy == "always") or (policy == "on_alert" and verdict["alert"])
        if wants and notify is not None and _alert_allowed(person, cooldown):
            try:
                notify(verdict["headline"] or f"{person['title']} has something",
                       outcome.get("output") or "", verdict["urgency"], person)
                _mark_alerted(db_path, person["key"])
                alerted = True
            except Exception:
                logger.exception("alerting for %s failed", person["key"])

    return {"employee": person["key"], "ok": outcome.get("ok", False),
            "alert": verdict["alert"], "alerted": alerted, "headline": verdict["headline"]}


def run_due(db_path: str, llm, tz_name: str = "UTC", notify=None, timeout: int = 10800,
            work_queue=None) -> list[dict]:
    """Run every employee who is due and on shift. Called by the scheduler.

    With work_queue given, each due assignment is handed to it (kind='cadence') and this
    returns immediately per employee -- staff_cadence's own tick becomes as fast as N
    enqueues, and the work queue's single worker is what actually calls assign() and,
    once it returns, handle_cadence_outcome() above. Without one (no queue wired, or a
    caller -- including every existing test -- that predates it), this falls back to the
    original synchronous behavior: call assign() and decide on the alert inline, before
    moving to the next employee. Never silently drops an assignment either way, same
    fallback reasoning as business_tools.py's assign_work handler.

    A due employee already sitting in the queue (queued or running) is skipped rather
    than enqueued again -- due_for_cadence has no visibility into the queue, so without
    this check a job that outlives one 5-minute tick (routine for a real coding
    assignment) would get re-enqueued on every tick until the first copy finally drains.
    """
    results = []
    for person in due_for_cadence(db_path, tz_name):
        # Each employee is isolated. One colleague failing must not stop the rest of the
        # roster from being checked -- an uncaught error here took every scheduled
        # employee offline at once, and looked from the outside like nothing was due.
        try:
            assignment = person["standing_assignment"]
            if person["alert_condition"]:
                assignment += build_verdict_instructions(person["alert_condition"])

            if work_queue is not None:
                if work_queue.has_pending(person["key"]):
                    results.append({"employee": person["key"], "ok": None, "alert": None,
                                    "alerted": False, "headline": None, "queued": False,
                                    "skipped": "already queued or running"})
                    continue
                queue_id = work_queue.submit(person["key"], assignment, kind="cadence")
                results.append({"employee": person["key"], "ok": None, "alert": None,
                                "alerted": False, "headline": None, "queued": True,
                                "queue_id": queue_id})
                continue

            outcome = assign(db_path, llm, person["key"], assignment, timeout=timeout)
            results.append(handle_cadence_outcome(db_path, person, outcome, notify))
        except Exception as e:
            logger.exception("employee %s failed to run", person["key"])
            results.append({"employee": person["key"], "ok": False, "alert": False,
                            "alerted": False, "headline": None,
                            "error": f"{type(e).__name__}: {e}"})
    return results


def set_standing_assignment(db_path: str, key: str, assignment: str) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE staff SET standing_assignment = ?, updated_at = ? WHERE key = ?",
            (assignment.strip() or None, _now(), key))
        conn.commit()
        return cur.rowcount > 0


def set_shift(db_path: str, key: str, cadence: str | None = None,
              interval_minutes: int | None = None, shift_start: str | None = None,
              shift_end: str | None = None, shift_days: str | None = None,
              alert_condition: str | None = None, alert_policy: str | None = None,
              alert_cooldown_min: int | None = None) -> dict | None:
    """Change when an employee works and what is worth waking the owner for."""
    if alert_policy is not None and alert_policy not in ("never", "on_alert", "always"):
        raise ValueError("alert_policy must be never, on_alert or always")
    if cadence is not None and cadence not in ("on_demand", "interval", "daily", "weekly"):
        raise ValueError("cadence must be on_demand, interval, daily or weekly")
    if cadence == "interval" and not (interval_minutes and interval_minutes > 0):
        raise ValueError("an interval cadence needs interval_minutes")

    fields, values = [], []
    for column, value in (("cadence", cadence), ("interval_minutes", interval_minutes),
                          ("shift_start", shift_start), ("shift_end", shift_end),
                          ("shift_days", shift_days), ("alert_condition", alert_condition),
                          ("alert_policy", alert_policy),
                          ("alert_cooldown_min", alert_cooldown_min)):
        if value is not None:
            fields.append(f"{column} = ?")
            values.append(value)
    if not fields:
        return get_staff(db_path, key)
    values += [_now(), key]
    with closing(_connect(db_path)) as conn:
        conn.execute(f"UPDATE staff SET {', '.join(fields)}, updated_at = ? WHERE key = ?",
                     values)
        conn.commit()
    return get_staff(db_path, key)


def roster_entries(db_path: str) -> list[dict]:
    """Hired staff in the shape the office UI expects, so dynamic employees appear
    beside the hardcoded agents rather than in a separate list."""
    return [
        {"key": e["key"], "title": e["title"], "role": e["department"],
         "character": e["character"], "hired": True, "status": e["status"]}
        for e in list_staff(db_path)
    ]
