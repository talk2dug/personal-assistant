"""Covers employee_work_history's tool handler: it must reflect live state, including
a job still sitting 'queued' in work_queue that hasn't been claimed yet -- otherwise
"what's happening with X" can go unanswered even though the real state (queued, not
started) is known. See work_queue.py's docstring for the incident (task 9) that
introduced the queue this closes the remaining visibility gap on.
"""
import pytest

from assistant.core import business_db, staff, work_queue
from assistant.core.business_tools import BusinessClient


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "biz.db")
    business_db.init_business_db(path)
    staff.init_staff_db(path)
    work_queue.init_work_queue_db(path)
    return path


@pytest.fixture
def queue(db_path):
    return work_queue.WorkQueue(db_path)


@pytest.fixture
def client(db_path, queue):
    return BusinessClient(db_path, owner_user_id=1, llm=object(), work_queue=queue)


def _hire(db_path, title="Developer"):
    return staff.hire(db_path, title, "Ten years of full-stack experience.")["key"]


class TestEmployeeWorkHistoryIncludesQueuedWork:
    def test_a_queued_but_unclaimed_job_shows_up_as_queued(self, client, db_path, queue):
        key = _hire(db_path)
        queue.submit(key, "a task nobody has started yet", owner_user_id=1)

        result = client.call_tool("employee_work_history", {})

        assert result["work"] == []  # nothing claimed yet, so no staff_work row exists
        assert len(result["queued"]) == 1
        assert result["queued"][0]["employee"] == key
        assert "a task nobody has started yet" in result["queued"][0]["assignment"]

    def test_filtering_by_employee_also_filters_the_queued_list(self, client, db_path, queue):
        key_a = _hire(db_path, "Developer")
        key_b = staff.hire(db_path, "Analyst", "Ten years of research experience.")["key"]
        queue.submit(key_a, "task for dev", owner_user_id=1)
        queue.submit(key_b, "task for analyst", owner_user_id=1)

        result = client.call_tool("employee_work_history", {"employee": key_a})

        assert len(result["queued"]) == 1
        assert result["queued"][0]["employee"] == key_a

    def test_no_work_queue_configured_returns_an_empty_queued_list_not_an_error(self, db_path):
        client = BusinessClient(db_path, owner_user_id=1, llm=object(), work_queue=None)
        result = client.call_tool("employee_work_history", {})
        assert result["queued"] == []

    def test_a_claimed_job_is_no_longer_reported_as_queued(self, client, db_path, queue):
        key = _hire(db_path)
        queue.submit(key, "a task about to start", owner_user_id=1)
        queue._claim_next()  # now 'running', not 'queued'

        result = client.call_tool("employee_work_history", {})

        assert result["queued"] == []
