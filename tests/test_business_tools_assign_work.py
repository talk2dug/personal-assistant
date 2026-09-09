"""Covers assign_work's tool handler specifically: it must enqueue and return
immediately rather than block the calling turn on staff.assign() (see
work_queue.py's docstring for the incident -- task 9 -- this closes), while still
catching an obviously bad employee key or a non-active employee synchronously, since
that's a mistake in the tool call itself and costs nothing to check up front.
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


class TestAssignWorkEnqueues:
    def test_a_valid_assignment_is_queued_not_run_inline(self, client, db_path, queue):
        key = _hire(db_path)
        result = client.call_tool("assign_work", {"employee": key, "assignment": "fix the bug"})

        assert result["ok"] is True
        assert result["queued"] is True
        assert "queue_id" in result
        row = queue.job(result["queue_id"])
        assert row["status"] == "queued"
        assert row["employee_key"] == key
        assert row["owner_user_id"] == 1

    def test_the_message_tells_the_model_not_to_invent_a_result(self, client, db_path):
        key = _hire(db_path)
        result = client.call_tool("assign_work", {"employee": key, "assignment": "fix the bug"})
        assert "NOT done" in result["message"]

    def test_an_unknown_employee_is_rejected_without_touching_the_queue(self, client, db_path, queue):
        result = client.call_tool("assign_work", {"employee": "nobody", "assignment": "x"})
        assert result["ok"] is False
        assert "no employee" in result["error"]
        assert queue.jobs() == []

    def test_a_paused_employee_is_rejected_without_touching_the_queue(self, client, db_path, queue):
        key = _hire(db_path)
        staff.set_status(db_path, key, "paused")
        result = client.call_tool("assign_work", {"employee": key, "assignment": "x"})
        assert result["ok"] is False
        assert "paused" in result["error"]
        assert queue.jobs() == []

    def test_no_llm_configured_is_rejected_before_touching_the_queue(self, db_path, queue):
        client = BusinessClient(db_path, owner_user_id=1, llm=None, work_queue=queue)
        key = _hire(db_path)
        result = client.call_tool("assign_work", {"employee": key, "assignment": "x"})
        assert result["ok"] is False
        assert queue.jobs() == []

    def test_missing_work_queue_falls_back_to_the_old_synchronous_call(self, db_path):
        """A BusinessClient built without a queue (e.g. an older test fixture) must not
        silently drop the assignment."""
        calls = []

        class FakeLLM:
            def research(self, prompt, system_prompt=None, timeout=None, **kwargs):
                calls.append(prompt)
                return "done synchronously"

        client = BusinessClient(db_path, owner_user_id=1, llm=FakeLLM(), work_queue=None)
        key = _hire(db_path)
        result = client.call_tool("assign_work", {"employee": key, "assignment": "fix the bug"})

        assert result["ok"] is True
        assert result.get("queued") is None
        assert calls == ["fix the bug"]
