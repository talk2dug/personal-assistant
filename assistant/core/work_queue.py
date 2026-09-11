"""Async work queue for hired-staff assignments -- lets `assign_work` (and, later, the
staff_cadence tick) hand a job to a background worker instead of blocking whoever asked
for it on a synchronous LLM call that can legitimately run for hours.

Closes a real gap: assign_work used to call staff.assign() directly on the request
thread. staff.assign() shells out to a `claude` CLI subprocess with its own, much longer
budget (staff_assignment_timeout_seconds, default 10800s -- see staff.py), completely
decoupled from claude_timeout_seconds, which bounds the owner's own live chat turn (300s
by default). A chat-triggered assign_work for anything beyond a few minutes of real work
therefore died with "the request took longer than 300s" while the actual assignment kept
running, unobserved, for up to three more hours -- exactly the incident behind business
task 9 (a git-tooling fix assigned to the developer employee, tracked as PR-13 tooling in
the design doc this module implements: docs/async-work-queue-design.md).

Same shape as gpu_bridge.py's queue (submit/claim/execute, a single background thread, a
threading.Event used as an interruptible sleep) -- that pattern is already proven in this
codebase for exactly this class of problem (a shared resource, one job at a time, durable
across a restart), so this reuses it rather than inventing a second one. Unlike the GPU
bridge, jobs run strictly one at a time and in order: an employee assignment has no VRAM-
style headroom concept, and staff.assign() already isolates one employee's failure from
another's (see staff.run_due's docstring), so serial FIFO is the right default rather than
gpu_bridge's admission-controlled concurrency.
"""
import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone

from . import business_db, db, staff
from .github_client import summarize_checks

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_key TEXT NOT NULL,
    assignment TEXT NOT NULL,
    -- on_demand = a chat-triggered assign_work call; cadence = a scheduled standing
    -- assignment. Both flow through the same table/worker; kind is for reporting only.
    kind TEXT NOT NULL DEFAULT 'on_demand' CHECK (kind IN ('on_demand', 'cadence')),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    -- Who to notify once this finishes, if anyone was waiting on it live -- a scheduled
    -- cadence run has nobody waiting on chat, so this stays NULL for that kind.
    owner_user_id INTEGER,
    staff_work_id INTEGER,
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_work_queue_status ON work_queue(status, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_work_queue_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Idempotent migration, same pattern as db.py/business_db.py: added after the
        # initial CREATE TABLE, so a column check guards it on an already-populated db.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(work_queue)")}
        if "attempt" not in cols:
            conn.execute("ALTER TABLE work_queue ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def reconcile_orphaned(db_path: str) -> int:
    """Fails any row still stuck 'running' -- this queue drains strictly one job at a
    time (see module docstring), so nothing can legitimately still be 'running' after
    a restart. Call once at startup, before start_worker, so a crash mid-job doesn't
    leave it silently invisible forever (the on_demand/cadence failure notification
    below only ever fires for a job the worker actually finished running)."""
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """UPDATE work_queue SET status = 'failed',
                   error = 'orphaned: process restarted while this was running',
                   finished_at = ? WHERE status = 'running'""",
            (_now(),))
        conn.commit()
        if cur.rowcount:
            logger.warning("reconciled %d orphaned work_queue row(s) stuck at 'running'", cur.rowcount)
        return cur.rowcount


class WorkQueue:
    """Holds the queue's storage and, once started, its single worker thread.

    llm/notify/timeout arrive at start_worker(), not __init__: BusinessClient is built in
    main.py before the Telegram notifier exists (see setup.build_business_context), but
    it only ever needs submit() -- the worker itself is started later, once notify is
    available, the same way gpu_bridge/vision's worker threads are started separately
    from where those objects are constructed.
    """

    def __init__(self, db_path: str, poll_seconds: int = 12):
        self.db_path = db_path
        self.poll_seconds = poll_seconds
        self.llm = None
        # notify(chat_id, text) -- delivers an on_demand job's result. cadence_notify
        # (headline, body, urgency, employee) -- passed straight through to
        # staff.handle_cadence_outcome for a 'cadence' job, same shape run_due's
        # synchronous path has always used, so alert_policy/cooldown behave identically
        # regardless of which path actually ran the assignment.
        self.notify = None
        self.cadence_notify = None
        self.timeout = 10800
        # Set at start_worker() alongside notify/cadence_notify -- used only to verify a
        # PR's real CI/merge state before reporting a coding task "done" instead of
        # trusting the employee's own claim (see _pr_status_line). None (the default,
        # and always the case in tests that don't set it) just means that verification
        # step is skipped and the raw outcome text is reported instead.
        self.git_ops_client = None
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # -- producer ----------------------------------------------------------------

    def submit(self, employee_key: str, assignment: str, kind: str = "on_demand",
               owner_user_id: int | None = None, attempt: int = 1) -> int:
        with closing(_connect(self.db_path)) as conn:
            cur = conn.execute(
                """INSERT INTO work_queue (employee_key, assignment, kind, owner_user_id, attempt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (employee_key, assignment, kind, owner_user_id, attempt, _now()),
            )
            conn.commit()
            return cur.lastrowid

    def job(self, queue_id: int) -> dict | None:
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute("SELECT * FROM work_queue WHERE id = ?", (queue_id,)).fetchone()
            return dict(row) if row else None

    def jobs(self, status: str | None = None, limit: int = 25) -> list[dict]:
        query = "SELECT * FROM work_queue"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with closing(_connect(self.db_path)) as conn:
            return [dict(r) for r in conn.execute(query, params)]

    def has_pending(self, employee_key: str) -> bool:
        """Whether this employee already has a job sitting in the queue or being worked
        -- run_due uses this to skip re-enqueueing someone whose last cadence assignment
        hasn't drained yet (see run_due's own docstring for why that matters)."""
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT 1 FROM work_queue WHERE employee_key = ? AND status IN ('queued', 'running') LIMIT 1",
                (employee_key,),
            ).fetchone()
            return row is not None

    # -- worker ------------------------------------------------------------------

    def _claim_next(self) -> dict | None:
        """Takes the oldest queued row, marking it running in the same UPDATE so two
        workers can never claim the same one -- same conditional-UPDATE-then-rowcount
        pattern as gpu_bridge.py's _claim_next. Only one worker ever runs in this process,
        but the check costs nothing and keeps the table itself safe if that ever changes.
        """
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM work_queue WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                "UPDATE work_queue SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                (_now(), row["id"]),
            )
            conn.commit()
            return dict(row) if cur.rowcount else None

    def _notify_owner(self, item: dict, text: str) -> None:
        """Delivers an already-composed, short status line -- the behavior change the
        owner actually notices: instead of chat going quiet for up to three hours (or,
        with the retry chain below, up to twelve) and then reporting nothing, he gets
        this once the chain actually resolves. Best-effort, same as every other notify()
        call site in this codebase (e.g. run_due's alerting): a delivery failure is
        logged, not raised, since there is no live request left to report it to."""
        if item["owner_user_id"] is None or self.notify is None:
            return
        user = db.get_user_by_id(self.db_path, item["owner_user_id"])
        if user is None:
            return
        try:
            self.notify(user["telegram_chat_id"], text.strip())
        except Exception:
            logger.exception("work queue: notifying owner about job %s failed", item["id"])

    def _pr_status_line(self, item: dict) -> str | None:
        """If this task opened a real PR (tracked as a git_pull_requests-ref review
        item -- engine.py logs one for every git_open_pr call, employee-triggered or
        not), report its actual current CI state instead of trusting the employee's
        own "done, tests pass"-style self-report. Returns None (caller falls back to
        the raw outcome text) when no git_ops client is wired up, no PR was involved,
        or its status can't be fetched -- never fabricates a status it didn't verify.
        """
        if self.git_ops_client is None or item.get("owner_user_id") is None:
            return None
        try:
            recent = business_db.list_review_items(
                self.db_path, item["owner_user_id"], status=None, limit=50)
        except Exception:
            return None
        started = item.get("started_at") or ""
        prs = [r for r in recent
               if r.get("ref_table") == "git_pull_requests" and (r.get("created_at") or "") >= started]
        if not prs:
            return None
        pr = max(prs, key=lambda r: r.get("created_at") or "")
        status = self.git_ops_client.get_pr_status(pr["ref_id"])
        if not status.get("ok"):
            return f"opened PR #{pr['ref_id']} (could not verify its CI status)"
        return f"opened PR #{pr['ref_id']} — CI {summarize_checks(status.get('checks') or [])}"

    def _success_message(self, title: str, item: dict, outcome: dict) -> str:
        pr_line = self._pr_status_line(item)
        if pr_line is not None:
            return f"{title}: done — {pr_line}"
        text = (outcome.get("output") or "").strip()
        if not text:
            return f"{title}: done — no output recorded."
        return f"{title}: done — {text[:300]}{'…' if len(text) > 300 else ''}"

    def _failure_message(self, title: str, outcome: dict, attempt: int) -> str:
        error = (outcome.get("error") or "no error detail recorded").strip()
        extra = " (a contractor also tried and could not finish it)" if attempt >= 4 else ""
        return (f"{title}: task failed after {attempt} attempt(s){extra} — {error[:150]}. "
                "Ask me for details if you want the full run.")

    def _hire_contractor(self, emp: dict, assignment: str) -> dict | None:
        """Brings in one temporary employee to take a fresh look at a task the regular
        employee has failed 3 times in a row -- the "don't bottleneck, spin up more
        help" behavior the owner asked for, rather than a task just sitting dead.
        Named/departmented like a real hand-off so it's visible on the roster, not a
        silent duplicate. Best-effort: if hiring itself fails, the caller falls back to
        escalating to the owner instead."""
        try:
            return staff.hire(
                self.db_path,
                title=f"{emp['title']} (Contractor)",
                job_description=(
                    f"Temporary contractor brought in because {emp['title']} could not "
                    f"complete this specific task after repeated attempts: "
                    f"{assignment[:300]}. Same responsibilities as {emp['title']}: "
                    f"{(emp.get('job_description') or '')[:500]}"
                ),
                department=emp["department"],
            )
        except Exception:
            logger.exception("work queue: could not hire a contractor to take over a stuck task")
            return None

    def _requeue_after_failure(self, item: dict, employee_key: str, attempt: int,
                                failure_note: str | None, handoff: bool) -> None:
        label = "HANDOFF to a fresh contractor" if handoff else "RETRY"
        note = (
            f"\n\n--- {label} (this is attempt {attempt}) ---\n"
            f"A previous attempt at this exact task failed with: "
            f"{(failure_note or 'no error detail recorded').strip()[:500]}\n"
            "Diagnose the actual problem and fix it properly -- do not just repeat the "
            "same approach and hope."
        )
        self.submit(employee_key, item["assignment"] + note, kind="on_demand",
                    owner_user_id=item["owner_user_id"], attempt=attempt)

    def _execute(self, item: dict) -> None:
        emp = staff.get_staff(self.db_path, item["employee_key"])
        title = emp["title"] if emp else item["employee_key"]
        try:
            outcome = staff.assign(
                self.db_path, self.llm, item["employee_key"], item["assignment"], timeout=self.timeout)
        except Exception as e:
            outcome = {"ok": False, "error": f"{type(e).__name__}: {e}"}

        ok = bool(outcome.get("ok"))
        status = "done" if ok else "failed"
        with closing(_connect(self.db_path)) as conn:
            conn.execute(
                """UPDATE work_queue SET status = ?, result = ?, error = ?, staff_work_id = ?,
                       finished_at = ? WHERE id = ?""",
                (status, outcome.get("output"), outcome.get("error"), outcome.get("work_id"),
                 _now(), item["id"]),
            )
            conn.commit()

        if item["kind"] == "cadence":
            # Same alert_policy/cooldown decision run_due's synchronous path has always
            # made -- just made here, once, after the async assignment actually finishes,
            # instead of inline right after a blocking assign() call. A released employee
            # still has a real staff row (get_staff doesn't filter by status) and gets
            # alerted on normally, e.g. "run failed" for the now-inactive assignment; emp
            # is only None if the row itself is gone, which nothing in the current API
            # does -- defensive, not a reachable case today. Cadence work is deliberately
            # never retried here -- it already gets a fresh attempt on its next scheduled
            # tick, and auto-hiring a contractor for a 10-minute monitoring check would
            # be wrong.
            if emp is not None:
                staff.handle_cadence_outcome(self.db_path, emp, outcome, self.cadence_notify)
            return

        if ok:
            self._notify_owner(item, self._success_message(title, item, outcome))
            return

        # on_demand failure: never just let it sit there. Retry the same employee (with
        # the failure appended as context -- the exact pattern the owner already did by
        # hand once) up to 3 total attempts, then hand off to one temporary contractor
        # for a final try, then stop and escalate -- bounded so a genuinely impossible
        # task can't retry forever burning real API time, but nothing silently stalls
        # on the first wall either.
        attempt = item.get("attempt") or 1
        if emp is not None and attempt < 3:
            self._requeue_after_failure(item, item["employee_key"], attempt + 1,
                                        outcome.get("error"), handoff=False)
            return
        if emp is not None and attempt == 3:
            contractor = self._hire_contractor(emp, item["assignment"])
            if contractor is not None:
                self._requeue_after_failure(item, contractor["key"], attempt + 1,
                                            outcome.get("error"), handoff=True)
                return

        self._notify_owner(item, self._failure_message(title, outcome, attempt))

    def tick(self) -> int:
        """One pass: claims and runs jobs serially until the queue is empty. Returns how
        many it processed -- called both by the worker loop and directly by tests."""
        processed = 0
        while True:
            item = self._claim_next()
            if item is None:
                return processed
            try:
                self._execute(item)
            except Exception:
                # staff.assign() already catches its own failures; reaching here means a
                # bug in this module itself. Fail the row rather than let it hang at
                # 'running' forever with no worker left to ever pick it up again.
                logger.exception("work queue: job %s raised outside staff.assign()", item["id"])
                with closing(_connect(self.db_path)) as conn:
                    conn.execute(
                        "UPDATE work_queue SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                        ("internal error processing this job -- see logs", _now(), item["id"]),
                    )
                    conn.commit()
            processed += 1

    def start_worker(self, llm, notify=None, cadence_notify=None, timeout: int = 10800,
                      git_ops_client=None) -> None:
        if self._worker and self._worker.is_alive():
            return
        self.llm, self.notify, self.cadence_notify, self.timeout = llm, notify, cadence_notify, timeout
        self.git_ops_client = git_ops_client

        def _loop():
            while not self._stop.wait(self.poll_seconds):
                try:
                    self.tick()
                except Exception:
                    logger.exception("work queue worker tick failed")

        self._stop.clear()
        self._worker = threading.Thread(target=_loop, daemon=True, name="work-queue-worker")
        self._worker.start()
        logger.info("work queue worker started, poll=%ss, per-job timeout=%ss", self.poll_seconds, timeout)

    def stop_worker(self) -> None:
        self._stop.set()
