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

from . import db, staff

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
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


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
        self.notify = None
        self.timeout = 10800
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # -- producer ----------------------------------------------------------------

    def submit(self, employee_key: str, assignment: str, kind: str = "on_demand",
               owner_user_id: int | None = None) -> int:
        with closing(_connect(self.db_path)) as conn:
            cur = conn.execute(
                """INSERT INTO work_queue (employee_key, assignment, kind, owner_user_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (employee_key, assignment, kind, owner_user_id, _now()),
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

    def _notify_owner(self, item: dict, employee_title: str, ok: bool, text: str) -> None:
        """Delivers the result as a follow-up message -- the behavior change the owner
        actually notices: instead of chat going quiet for up to three hours and then
        reporting nothing, he gets this once the job actually finishes. Best-effort, same
        as every other notify() call site in this codebase (e.g. run_due's alerting):
        a delivery failure is logged, not raised, since there is no live request left to
        report it to."""
        if item["owner_user_id"] is None or self.notify is None:
            return
        user = db.get_user_by_id(self.db_path, item["owner_user_id"])
        if user is None:
            return
        prefix = f"{employee_title}: " + ("done" if ok else "failed")
        try:
            self.notify(user["telegram_chat_id"], f"{prefix}\n\n{(text or '').strip()[:1500]}".strip())
        except Exception:
            logger.exception("work queue: notifying owner about job %s failed", item["id"])

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

        body = outcome.get("output") if ok else (outcome.get("error") or "no error detail recorded")
        self._notify_owner(item, title, ok, body or "")

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

    def start_worker(self, llm, notify=None, timeout: int = 10800) -> None:
        if self._worker and self._worker.is_alive():
            return
        self.llm, self.notify, self.timeout = llm, notify, timeout

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
