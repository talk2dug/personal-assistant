"""Storage and execution for the ops-plan workflow: the owner's explicit requirement that
a systems-engineer employee propose a whole plan of real server changes -- what it'll do,
how it tests the change, how it verifies the system still works, and what it does if it
fails -- and get ONE approval for the whole thing, rather than a yes/no per command.

A "plan" is a single ordered list of steps, each tagged with a phase (change/test/verify/
rollback) rather than four separate free-text fields. This is deliberate: keeping one
structured list as the single source of truth means what the owner reviews on the Review
page is *exactly* what will execute, with no chance of the prose description drifting from
the real commands. change/test/verify steps run in the order given; if any of them fails,
every rollback step runs (also in order) and nothing after that ever executes.

A sibling of business_db.py/staff.py/personal_db.py -- its own schema, applied to the same
shared jarvis.db file, same _connect/_now/_rows helper pattern throughout this project.
"""
import json
import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PHASES = ("change", "test", "verify", "rollback")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'approved', 'rejected', 'running', 'succeeded', 'failed')),
    review_item_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ops_plan_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES ops_plans(id),
    step_index INTEGER NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('change', 'test', 'verify', 'rollback')),
    host TEXT NOT NULL,
    command TEXT NOT NULL,
    purpose TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')),
    output TEXT,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_ops_plan_steps_plan ON ops_plan_steps(plan_id, step_index);
"""


def init_ops_plans_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_steps(steps: list[dict]) -> None:
    if not steps:
        raise ValueError("a plan needs at least one step")
    for i, step in enumerate(steps):
        missing = [k for k in ("phase", "host", "command") if not step.get(k)]
        if missing:
            raise ValueError(f"step {i}: missing {', '.join(missing)}")
        if step["phase"] not in PHASES:
            raise ValueError(f"step {i}: phase must be one of {', '.join(PHASES)}, got {step['phase']!r}")
    if not any(s["phase"] == "rollback" for s in steps):
        raise ValueError(
            "a plan needs at least one rollback step -- what happens if the change, "
            "test, or verification fails is not optional"
        )


def create_plan(db_path: str, owner_user_id: int, summary: str, steps: list[dict]) -> int:
    validate_steps(steps)
    now = _now()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO ops_plans (owner_user_id, summary, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (owner_user_id, summary, now, now),
        )
        plan_id = cur.lastrowid
        for i, step in enumerate(steps):
            conn.execute(
                """INSERT INTO ops_plan_steps (plan_id, step_index, phase, host, command, purpose)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (plan_id, i, step["phase"], step["host"], step["command"], step.get("purpose")),
            )
        conn.commit()
        return plan_id


def get_plan(db_path: str, plan_id: int) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM ops_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            return None
        plan = dict(row)
        plan["steps"] = [dict(r) for r in conn.execute(
            "SELECT * FROM ops_plan_steps WHERE plan_id = ? ORDER BY step_index", (plan_id,))]
        return plan


def list_plans(db_path: str, owner_user_id: int, status: str | None = None, limit: int = 20) -> list[dict]:
    query = "SELECT * FROM ops_plans WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(query, params)]


def set_plan_status(db_path: str, plan_id: int, status: str) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE ops_plans SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), plan_id))
        conn.commit()


def set_review_item_id(db_path: str, plan_id: int, review_item_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE ops_plans SET review_item_id = ?, updated_at = ? WHERE id = ?",
                     (review_item_id, _now(), plan_id))
        conn.commit()


def _update_step(db_path: str, step_id: int, status: str, output: str | None = None,
                  started: bool = False, finished: bool = False) -> None:
    fields, values = ["status = ?"], [status]
    if output is not None:
        fields.append("output = ?")
        values.append(output)
    if started:
        fields.append("started_at = ?")
        values.append(_now())
    if finished:
        fields.append("finished_at = ?")
        values.append(_now())
    values.append(step_id)
    with closing(_connect(db_path)) as conn:
        conn.execute(f"UPDATE ops_plan_steps SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()


PHASE_HEADINGS = {
    "change": "What will be done",
    "test": "How the change will be tested",
    "verify": "How the system will be verified to still be fully functional",
    "rollback": "Plan of attack if anything above fails",
}


def render_plan_detail(summary: str, steps: list[dict]) -> str:
    """Composes the Review-page detail text, grouped by phase in the order the owner
    asked for -- what/test/verify/rollback -- from the exact same structured steps that
    will actually execute, so there's no chance of the description drifting from reality."""
    sections = [summary.strip(), ""]
    for phase in PHASES:
        phase_steps = [s for s in steps if s["phase"] == phase]
        if not phase_steps:
            continue
        sections.append(f"## {PHASE_HEADINGS[phase]}")
        for s in phase_steps:
            purpose = f" — {s['purpose']}" if s.get("purpose") else ""
            sections.append(f"- [{s['host']}] `{s['command']}`{purpose}")
        sections.append("")
    return "\n".join(sections).strip()


def run_plan(db_path: str, plan_id: int, ssh_client) -> dict:
    """Executes an approved plan: change/test/verify steps in order, stopping at the
    first failure and running every rollback step if one occurs. Never called directly
    from a tool -- only after the owning Review item has been approved (see
    business_tools.py's decide_review_item), same "propose, then a human gate, then
    execute" shape as everything else in this codebase with real consequence."""
    plan = get_plan(db_path, plan_id)
    if plan is None:
        return {"ok": False, "error": f"no such plan {plan_id}"}

    set_plan_status(db_path, plan_id, "running")
    failed = False
    for step in plan["steps"]:
        if step["phase"] == "rollback":
            continue
        _update_step(db_path, step["id"], "running", started=True)
        try:
            result = ssh_client.run_command(step["host"], step["command"])
            _update_step(db_path, step["id"], "succeeded" if result["ok"] else "failed",
                        output=result["output"], finished=True)
            if not result["ok"]:
                logger.warning("ops plan %s: step %s failed on %s (exit %s)",
                               plan_id, step["step_index"], step["host"], result["exit_code"])
                failed = True
                break
        except Exception as e:
            logger.exception("ops plan %s: step %s raised", plan_id, step["step_index"])
            _update_step(db_path, step["id"], "failed", output=str(e), finished=True)
            failed = True
            break

    if failed:
        for step in plan["steps"]:
            if step["phase"] != "rollback":
                continue
            _update_step(db_path, step["id"], "running", started=True)
            try:
                result = ssh_client.run_command(step["host"], step["command"])
                _update_step(db_path, step["id"], "succeeded" if result["ok"] else "failed",
                            output=result["output"], finished=True)
            except Exception as e:
                logger.exception("ops plan %s: rollback step %s raised", plan_id, step["step_index"])
                _update_step(db_path, step["id"], "failed", output=str(e), finished=True)
        set_plan_status(db_path, plan_id, "failed")
        return {"ok": False, "plan_id": plan_id, "status": "failed"}

    # Rollback steps were never touched -- mark them skipped so the record reads
    # correctly rather than showing them stuck at "pending" forever.
    for step in plan["steps"]:
        if step["phase"] == "rollback":
            _update_step(db_path, step["id"], "skipped")
    set_plan_status(db_path, plan_id, "succeeded")
    return {"ok": True, "plan_id": plan_id, "status": "succeeded"}


def run_plan_async(db_path: str, plan_id: int, ssh_client) -> None:
    """Fire-and-forget entry point for decide_review_item -- a multi-step SSH sequence
    has no business blocking a chat turn or an HTTP request, same reasoning as
    business_tools.py's _run_agent_async."""
    threading.Thread(target=run_plan, args=(db_path, plan_id, ssh_client), daemon=True,
                     name=f"ops-plan-{plan_id}").start()
