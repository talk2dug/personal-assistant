"""Covers the async work queue behind assign_work (see work_queue.py's docstring for the
incident this closes): staff.assign() must never again run on a live request thread, a
bad assignment must fail visibly rather than hang the queue, and the owner must be told
once a job actually finishes -- especially when it failed, since a silent failure here is
exactly the "Jarvis went quiet and nobody found out" gap task 10 exists to close.
"""
import pytest

from assistant.core import business_db, db, staff, work_queue
from assistant.core.work_queue import WorkQueue


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "queue.db")
    db.init_db(path)
    staff.init_staff_db(path)
    work_queue.init_work_queue_db(path)
    return path


@pytest.fixture
def queue(db_path):
    return WorkQueue(db_path, poll_seconds=1)


@pytest.fixture
def owner_id(db_path):
    db.upsert_user(db_path, "chat123", "Jack", "owner")
    return db.get_user_by_chat_id(db_path, "chat123")["id"]


class FakeLLM:
    def __init__(self, output="deliverable text", raises=None):
        self.output = output
        self.raises = raises
        self.calls = []

    def research(self, prompt, system_prompt=None, timeout=None, **kwargs):
        self.calls.append((prompt, timeout))
        if self.raises:
            raise self.raises
        return self.output


def _hire(db_path, key_title="Developer"):
    return staff.hire(db_path, key_title, "Ten years of full-stack experience.")["key"]


class TestSubmitAndClaim:
    def test_claim_next_returns_none_when_empty(self, queue):
        assert queue._claim_next() is None

    def test_claim_next_takes_the_oldest_queued_row_and_marks_it_running(self, queue, db_path):
        key = _hire(db_path)
        first = queue.submit(key, "task one")
        queue.submit(key, "task two")

        claimed = queue._claim_next()
        assert claimed["id"] == first
        assert claimed["status"] == "queued"  # the dict returned reflects pre-claim state
        assert queue.job(first)["status"] == "running"

    def test_a_claimed_row_cannot_be_claimed_twice(self, queue, db_path):
        key = _hire(db_path)
        queue.submit(key, "task one")
        queue._claim_next()
        assert queue._claim_next() is None  # nothing else queued


class TestExecute:
    def test_successful_assignment_marks_done_and_links_staff_work(self, queue, db_path):
        key = _hire(db_path)
        queue.llm = FakeLLM(output="here is the PR link")
        queue.timeout = 999
        queue_id = queue.submit(key, "fix the bug")

        processed = queue.tick()

        assert processed == 1
        row = queue.job(queue_id)
        assert row["status"] == "done"
        assert row["result"] == "here is the PR link"
        assert row["staff_work_id"] is not None
        # The underlying staff_work row (what the office UI and recent_work read) reflects
        # the same outcome, since _execute goes through staff.assign() unchanged.
        work = staff.recent_work(db_path, key=key, limit=1)[0]
        assert work["status"] == "delivered"

    def test_a_raising_llm_marks_the_row_failed_not_stuck_running(self, queue, db_path):
        key = _hire(db_path)
        queue.llm = FakeLLM(raises=RuntimeError("the request took longer than 10800s"))
        queue_id = queue.submit(key, "a task that times out")

        queue.tick()

        row = queue.job(queue_id)
        assert row["status"] == "failed"
        assert "10800s" in row["error"]

    def test_an_unknown_employee_fails_the_row_rather_than_raising(self, queue, db_path):
        """submit() doesn't validate the employee (that happens at the call site, see
        business_tools.py's assign_work handler) -- the worker must still degrade
        gracefully if a bad key ever reaches the queue directly."""
        queue.llm = FakeLLM()
        queue_id = queue.submit("nobody_hired", "do a thing")

        queue.tick()

        row = queue.job(queue_id)
        assert row["status"] == "failed"
        assert "no employee" in row["error"]

    def test_tick_drains_every_queued_row_in_order(self, queue, db_path):
        key = _hire(db_path)
        queue.llm = FakeLLM()
        ids = [queue.submit(key, f"task {i}") for i in range(3)]

        processed = queue.tick()

        assert processed == 3
        assert all(queue.job(i)["status"] == "done" for i in ids)


class TestOwnerNotification:
    def test_on_demand_success_notifies_the_owner_with_the_deliverable(self, queue, db_path, owner_id):
        sent = []
        queue.llm = FakeLLM(output="branch pushed, PR #21 opened")
        queue.notify = lambda chat_id, text: sent.append((chat_id, text))
        key = _hire(db_path)
        queue.submit(key, "ship it", kind="on_demand", owner_user_id=owner_id)

        queue.tick()

        assert len(sent) == 1
        chat_id, text = sent[0]
        assert chat_id == "chat123"
        assert "done" in text
        assert "PR #21" in text

    def test_failure_also_notifies_the_owner(self, queue, db_path, owner_id):
        """The exact gap in the old synchronous path: a timed-out or crashed assignment
        must reach the owner, not just staff_work/the logs -- same reasoning as run_due's
        own always-notify-on-failure fix."""
        sent = []
        queue.llm = FakeLLM(raises=RuntimeError("the request took longer than 10800s"))
        queue.notify = lambda chat_id, text: sent.append((chat_id, text))
        key = _hire(db_path)
        queue.submit(key, "a doomed task", owner_user_id=owner_id)

        queue.tick()

        assert len(sent) == 1
        assert "failed" in sent[0][1]
        assert "10800s" in sent[0][1]

    def test_cadence_work_never_uses_the_on_demand_notify(self, queue, db_path):
        """A cadence job's owner_user_id is always None by design (see run_due's
        docstring) -- it must go through cadence_notify/handle_cadence_outcome, never
        the plain chat notify on_demand jobs use."""
        sent = []
        queue.llm = FakeLLM()
        queue.notify = lambda chat_id, text: sent.append((chat_id, text))
        key = _hire(db_path)
        queue.submit(key, "scheduled check-in", kind="cadence", owner_user_id=None)

        queue.tick()

        assert sent == []

    def test_a_notify_failure_does_not_stop_the_row_from_being_marked_done(self, queue, db_path, owner_id):
        def boom(chat_id, text):
            raise ConnectionError("telegram is down")

        queue.llm = FakeLLM(output="done anyway")
        queue.notify = boom
        key = _hire(db_path)
        queue_id = queue.submit(key, "task", owner_user_id=owner_id)

        queue.tick()  # must not raise

        assert queue.job(queue_id)["status"] == "done"


class TestStartStopWorker:
    def test_start_worker_is_idempotent(self, queue):
        queue.start_worker(FakeLLM())
        first = queue._worker
        queue.start_worker(FakeLLM())  # must not spawn a second thread
        assert queue._worker is first
        queue.stop_worker()

    def test_stop_worker_lets_the_thread_exit(self, queue):
        queue.start_worker(FakeLLM())
        queue.stop_worker()
        queue._worker.join(timeout=2)
        assert not queue._worker.is_alive()


class TestCadenceExecution:
    """The flipped staff_cadence path (§8 of the design doc): a 'cadence' job's alert
    decision must go through staff.handle_cadence_outcome exactly as run_due's own
    synchronous branch does, using cadence_notify -- not the plain on_demand notify."""

    def test_a_failed_cadence_job_notifies_via_cadence_notify_regardless_of_policy(self, queue, db_path):
        alerts = []
        queue.llm = FakeLLM(raises=RuntimeError("backend down"))
        queue.cadence_notify = lambda headline, body, urgency, person: alerts.append(headline)
        key = staff.hire(db_path, "Omicron", "Ten years of full-stack experience.",
                         alert_policy="never")["key"]
        queue.submit(key, "scheduled check-in", kind="cadence")

        queue.tick()

        assert alerts and "failed" in alerts[0].lower()

    def test_a_quiet_successful_cadence_job_with_policy_never_does_not_alert(self, queue, db_path):
        alerts = []
        queue.llm = FakeLLM(output='all good\n{"alert": false, "urgency": "low", "headline": "fine"}')
        queue.cadence_notify = lambda headline, body, urgency, person: alerts.append(headline)
        key = staff.hire(db_path, "Pi", "Ten years of full-stack experience.",
                         alert_policy="never")["key"]
        queue.submit(key, "scheduled check-in", kind="cadence")

        queue.tick()

        assert alerts == []

    def test_an_alerting_cadence_job_notifies_via_cadence_notify(self, queue, db_path):
        alerts = []
        queue.llm = FakeLLM(output='dumped\n{"alert": true, "urgency": "high", "headline": "BTC -10%"}')
        queue.cadence_notify = lambda headline, body, urgency, person: alerts.append(headline)
        key = staff.hire(db_path, "Rho", "Ten years of full-stack experience.",
                         alert_policy="on_alert", alert_condition="a big move")["key"]
        queue.submit(key, "scheduled check-in", kind="cadence")

        queue.tick()

        assert alerts == ["BTC -10%"]

    def test_cadence_kind_still_marks_the_row_done_and_links_staff_work(self, queue, db_path):
        """The row bookkeeping (status/result/staff_work_id) must not depend on which
        branch of _execute ran -- only the notification path differs by kind."""
        queue.llm = FakeLLM(output="fine")
        key = staff.hire(db_path, "Sigma", "Ten years of full-stack experience.")["key"]
        queue_id = queue.submit(key, "scheduled check-in", kind="cadence")

        queue.tick()

        row = queue.job(queue_id)
        assert row["status"] == "done"
        assert row["staff_work_id"] is not None

    def test_a_deleted_employee_row_is_not_alerted_on(self, queue, db_path):
        """staff.get_staff() can return None if the employee row is gone by the time the
        worker picks the job up -- nothing in the current API deletes a staff row (a
        release just flips status), but _execute's guard against it is still worth
        pinning: handle_cadence_outcome needs a real person dict (title, policy,
        cooldown), so a missing one must skip the alert decision rather than crash."""
        import sqlite3

        alerts = []
        queue.llm = FakeLLM(output="fine")
        queue.cadence_notify = lambda headline, body, urgency, person: alerts.append(headline)
        key = staff.hire(db_path, "Tau", "Ten years of full-stack experience.")["key"]
        queue_id = queue.submit(key, "scheduled check-in", kind="cadence")
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM staff WHERE key = ?", (key,))

        queue.tick()  # must not raise

        assert alerts == []
        assert queue.job(queue_id)["status"] in ("done", "failed")


class TestReconcileOrphaned:
    """A row still 'running' after a restart is not "in progress" -- this queue drains
    strictly one job at a time, so nothing legitimately survives a crash mid-job. A real
    incident: 10 staff_work rows sat 'running' for 3-6 days because nothing ever
    reconciled them."""

    def test_reconcile_fails_a_row_stuck_running(self, queue, db_path):
        key = _hire(db_path)
        queue_id = queue.submit(key, "task")
        queue._claim_next()  # marks it 'running', simulating a crash mid-job
        assert queue.job(queue_id)["status"] == "running"

        n = work_queue.reconcile_orphaned(db_path)

        assert n == 1
        row = queue.job(queue_id)
        assert row["status"] == "failed"
        assert "orphaned" in row["error"]

    def test_reconcile_leaves_queued_and_done_rows_alone(self, queue, db_path):
        key = _hire(db_path)
        queue.llm = FakeLLM()
        already_done = queue.submit(key, "task two")
        queue.tick()  # drains and completes it before the next one is even submitted
        still_queued = queue.submit(key, "task one")

        n = work_queue.reconcile_orphaned(db_path)

        assert n == 0
        assert queue.job(still_queued)["status"] == "queued"
        assert queue.job(already_done)["status"] == "done"


class TestOnDemandRetryAndContractorHandoff:
    """The "never say die" mechanism: an on_demand failure must not just sit there --
    it retries the same employee (with the failure appended as context), then hands off
    to a temporary contractor, and only escalates to the owner once that's exhausted."""

    def test_retries_then_hires_a_contractor_then_escalates_once(self, queue, db_path, owner_id):
        sent = []
        queue.llm = FakeLLM(raises=RuntimeError("boom"))
        queue.notify = lambda chat_id, text: sent.append(text)
        key = _hire(db_path, "Senior Developer")
        queue.submit(key, "a hard task", owner_user_id=owner_id)

        queue.tick()

        # attempt 1 (orig) -> retry 2 -> retry 3 -> hire contractor -> attempt 4 -> escalate.
        # Four real runs recorded (one per attempt, contractor's included), one notification.
        assert len(staff.recent_work(db_path)) == 4
        assert len(sent) == 1
        assert "failed after 4 attempt" in sent[0]
        assert "contractor" in sent[0]

    def test_a_contractor_is_hired_with_the_same_department_after_the_third_failure(self, queue, db_path, owner_id):
        queue.llm = FakeLLM(raises=RuntimeError("boom"))
        key = _hire(db_path, "Senior Developer")
        queue.submit(key, "a hard task", owner_user_id=owner_id)

        queue.tick()

        contractors = [e for e in staff.list_staff(db_path) if "(Contractor)" in e["title"]]
        assert len(contractors) == 1
        assert contractors[0]["department"] == staff.get_staff(db_path, key)["department"]

    def test_succeeding_on_a_retry_notifies_success_not_failure(self, queue, db_path, owner_id):
        sent = []
        attempts = {"n": 0}

        class FlakyLLM:
            def research(self, prompt, system_prompt=None, timeout=None, **kwargs):
                attempts["n"] += 1
                if attempts["n"] < 2:
                    raise RuntimeError("transient failure")
                return "fixed it on the second try"

        queue.llm = FlakyLLM()
        queue.notify = lambda chat_id, text: sent.append(text)
        key = _hire(db_path)
        queue.submit(key, "a flaky task", owner_user_id=owner_id)

        queue.tick()

        assert len(sent) == 1
        assert "done" in sent[0]
        assert "fixed it on the second try" in sent[0]

    def test_cadence_jobs_are_never_retried_even_on_failure(self, queue, db_path):
        key = staff.hire(db_path, "Upsilon", "Ten years of full-stack experience.",
                         alert_policy="never")["key"]
        queue.llm = FakeLLM(raises=RuntimeError("backend down"))
        queue.submit(key, "scheduled check-in", kind="cadence")

        queue.tick()

        assert len(staff.recent_work(db_path, key=key)) == 1

    def test_an_unknown_employee_is_never_retried(self, queue, db_path):
        """No employee to copy a department from or requeue against -- retrying would
        just fail identically forever, so this must escalate immediately instead."""
        queue.llm = FakeLLM()
        queue.submit("nobody_hired", "do a thing")

        queue.tick()

        assert work_queue.WorkQueue(db_path).jobs()[0]["status"] == "failed"
        assert staff.list_staff(db_path, include_released=True) == []


class TestPRVerifiedNotification:
    """A coding task's success message must reflect the PR's real, live CI state --
    never the employee's own unverified "done, tests pass" claim."""

    def test_reports_the_pr_s_real_ci_status_not_the_employee_s_claim(self, queue, db_path, owner_id):
        business_db.init_business_db(db_path)
        sent = []

        class FakeGitOps:
            def get_pr_status(self, pr_number):
                return {"ok": True, "checks": [
                    {"name": "backend-tests", "status": "completed", "conclusion": "failure"}]}

        queue.git_ops_client = FakeGitOps()
        queue.llm = FakeLLM(output="all done, tests pass, ready to merge!")
        queue.notify = lambda chat_id, text: sent.append(text)
        key = _hire(db_path)
        queue.submit(key, "ship it", owner_user_id=owner_id)
        item = queue._claim_next()
        # Simulate the employee having opened a real PR mid-run, the same way engine.py
        # logs a git_pull_requests-ref review item for every git_open_pr call.
        business_db.create_review_item(
            db_path, owner_id, title="PR #99", kind="other", source_agent="git",
            ref_table="git_pull_requests", ref_id=99)

        queue._execute(item)

        assert len(sent) == 1
        assert "PR #99" in sent[0]
        assert "failure" in sent[0]
        assert "tests pass" not in sent[0]  # the employee's own unverified claim never ships


class TestHasPending:
    def test_false_when_nothing_is_queued_for_this_employee(self, queue, db_path):
        key = _hire(db_path)
        assert queue.has_pending(key) is False

    def test_true_while_queued(self, queue, db_path):
        key = _hire(db_path)
        queue.submit(key, "task")
        assert queue.has_pending(key) is True

    def test_true_while_running(self, queue, db_path):
        key = _hire(db_path)
        queue.submit(key, "task")
        queue._claim_next()
        assert queue.has_pending(key) is True

    def test_false_once_done(self, queue, db_path):
        queue.llm = FakeLLM()
        key = _hire(db_path)
        queue.submit(key, "task")
        queue.tick()
        assert queue.has_pending(key) is False

    def test_pending_is_scoped_to_the_employee_key(self, queue, db_path):
        key_a = _hire(db_path, "Developer")
        key_b = staff.hire(db_path, "Analyst", "Ten years of research experience.")["key"]
        queue.submit(key_a, "task")
        assert queue.has_pending(key_a) is True
        assert queue.has_pending(key_b) is False
