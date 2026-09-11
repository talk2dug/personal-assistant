"""Covers /api/active-work -- the chat window's "what's Jarvis doing right now" strip
(personal dashboard task 17). Pulls from four independent background systems (delegated
research, the built-in agents, hired staff, ops plans) and the async work queue, so most
of this is pinning that each source surfaces correctly and that a piece of work claimed
by an employee doesn't get counted twice (once as a running work_queue job, once as its
own staff_work row).
"""
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import business_db, db, ops_plans, personal_db, staff, work_queue
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ago(**kwargs) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(**kwargs))


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    personal_db.init_personal_db(path)
    ops_plans.init_ops_plans_db(path)
    work_queue.init_work_queue_db(path)
    staff.init_staff_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


def _client(db_path, business=None):
    cfg = FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw"),
    ])
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None, business=business)
    c = TestClient(app)
    assert c.post("/api/login", json={"name": "Dug", "password": "pw"}).status_code == 200
    return c


def test_requires_login(db_path):
    cfg = FakeConfig(db_path=db_path)
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    assert TestClient(app).get("/api/active-work").status_code == 401


def test_empty_when_nothing_is_happening(db_path):
    c = _client(db_path)
    assert c.get("/api/active-work").json() == {"items": []}


def test_queued_research_shows_up(db_path):
    personal_db.create_research(db_path, 1, "Boarding kennels near Asheville")
    c = _client(db_path)
    items = c.get("/api/active-work").json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "research"
    assert items[0]["status"] == "queued"
    assert "Boarding kennels" in items[0]["label"]


def test_a_running_agent_run_with_no_history_has_no_eta(db_path):
    business_db.start_agent_run(db_path, "market_finder")
    c = _client(db_path)
    items = c.get("/api/active-work").json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "agent_run" and items[0]["status"] == "running"
    assert items[0]["eta_label"] is None


def test_a_running_agent_run_gets_an_eta_from_recent_history(db_path):
    # Three prior 'ok' runs of the same agent, each took ~10 minutes.
    for _ in range(3):
        run_id = business_db.start_agent_run(db_path, "market_finder")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE agent_runs SET started_at = ?, finished_at = ?, status = 'ok' WHERE id = ?",
                (_ago(minutes=40), _ago(minutes=30), run_id),
            )
    # A new one, started 2 minutes ago -- should have ~8 minutes left against a 10-minute median.
    run_id = business_db.start_agent_run(db_path, "market_finder")
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", (_ago(minutes=2), run_id))

    c = _client(db_path)
    items = c.get("/api/active-work").json()["items"]
    running = [i for i in items if i["status"] == "running"]
    assert len(running) == 1
    assert running[0]["eta_label"] == "~8m left"


def test_a_running_staff_work_row_shows_up_once_not_via_work_queue_too(db_path):
    key = staff.hire(db_path, "Tau", "Ten years of full-stack experience.")["key"]
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        staff_id = conn.execute("SELECT id FROM staff WHERE key = ?", (key,)).fetchone()["id"]
        cur = conn.execute(
            "INSERT INTO staff_work (staff_id, assignment, status, started_at) VALUES (?, ?, 'running', ?)",
            (staff_id, "Draft the Q3 report", _ago(minutes=1)),
        )
        work_id = cur.lastrowid
        # The work_queue row for this same piece of work also exists and is 'running' --
        # this must NOT produce a second item, since staff_work already represents it.
        conn.execute(
            "INSERT INTO work_queue (employee_key, assignment, status, staff_work_id, created_at, started_at)"
            " VALUES (?, ?, 'running', ?, ?, ?)",
            (key, "Draft the Q3 report", work_id, _ago(minutes=1), _ago(minutes=1)),
        )
        conn.commit()

    @dataclass
    class FakeBusiness:
        work_queue: object

    c = _client(db_path, business=FakeBusiness(work_queue=work_queue.WorkQueue(db_path)))
    items = c.get("/api/active-work").json()["items"]
    staff_items = [i for i in items if i["kind"] == "staff_work"]
    queued_items = [i for i in items if i["kind"] == "queued_work"]
    assert len(staff_items) == 1
    assert "Draft the Q3 report" in staff_items[0]["label"]
    assert queued_items == []


def test_a_queued_not_yet_claimed_work_queue_job_shows_up(db_path):
    key = staff.hire(db_path, "Tau", "Ten years of full-stack experience.")["key"]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO work_queue (employee_key, assignment, status, created_at) VALUES (?, ?, 'queued', ?)",
            (key, "Draft the Q3 report", _ago(minutes=1)),
        )
        conn.commit()

    @dataclass
    class FakeBusiness:
        work_queue: object

    c = _client(db_path, business=FakeBusiness(work_queue=work_queue.WorkQueue(db_path)))
    items = c.get("/api/active-work").json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "queued_work" and items[0]["status"] == "queued"


def test_ops_plans_approved_and_running_both_show_up(db_path):
    steps = [
        {"phase": "test", "host": "jarvisbox", "command": "echo hi", "purpose": "smoke"},
        {"phase": "rollback", "host": "jarvisbox", "command": "echo undo"},
    ]
    approved_id = ops_plans.create_plan(db_path, 1, "Restart the vision worker", steps)
    ops_plans.set_plan_status(db_path, approved_id, "approved")

    running_id = ops_plans.create_plan(db_path, 1, "Roll out the mail fix", steps)
    ops_plans.set_plan_status(db_path, running_id, "running")

    c = _client(db_path)
    items = c.get("/api/active-work").json()["items"]
    by_status = {i["status"]: i for i in items if i["kind"] == "ops_plan"}
    assert "Restart the vision worker" in by_status["queued"]["label"]
    assert "Roll out the mail fix" in by_status["running"]["label"]
    assert "step 1/2" in by_status["running"]["label"]


def test_running_items_sort_before_queued(db_path):
    personal_db.create_research(db_path, 1, "Find a vet")
    business_db.start_agent_run(db_path, "market_finder")
    c = _client(db_path)
    items = c.get("/api/active-work").json()["items"]
    statuses = [i["status"] for i in items]
    assert statuses == sorted(statuses, key=lambda s: s != "running")
