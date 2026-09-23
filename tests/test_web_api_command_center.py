"""Covers /api/command-center/snapshot -- the one aggregated read the board polls.

Two things this endpoint has to get right, and both are tested here rather than left to
the UI:

  1. It must never lie by omission. A readout with no data behind it renders '—', not a
     zero, because a board that shows "0 flagged" when the mail integration isn't wired
     up is actively misleading.
  2. It must survive its own dependencies. Home Assistant lives on the far side of the
     LAN and the board polls this every few seconds -- an HA that is rebooting has to
     degrade to `home.available: false`, never to a 500 that blanks the whole screen.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import business_db, db, kitchen_db, personal_db, staff, vision, work_queue
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None
    device_api_key: str | None = None


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


class FakeHA:
    """Stands in for HomeAssistantContext -- the client behind .mcp_client."""
    def __init__(self, states=None, fail=None):
        self.mcp_client = self
        self._states = states or []
        self._fail = fail

    def raw_states(self, max_age_seconds=4.0):
        if self._fail:
            raise self._fail
        return self._states


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    personal_db.init_personal_db(path)
    staff.init_staff_db(path)
    work_queue.init_work_queue_db(path)
    vision.init_vision_db(path)
    kitchen_db.init_kitchen_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


def _client(db_path, home_assistant=None, role="owner"):
    cfg = FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role=role, web_password="pw"),
    ])
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None,
                     home_assistant=home_assistant)
    c = TestClient(app)
    assert c.post("/api/login", json={"name": "Dug", "password": "pw"}).status_code == 200
    return c


def _snapshot(client):
    res = client.get("/api/command-center/snapshot")
    assert res.status_code == 200, res.text
    return res.json()


def _readout(snap, label):
    return next(r for r in snap["readouts"] if r["label"] == label)


# --- access ------------------------------------------------------------------

def test_requires_login(db_path):
    cfg = FakeConfig(db_path=db_path)
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    assert TestClient(app).get("/api/command-center/snapshot").status_code == 401


def test_owner_only(db_path):
    """This is the owner's whole life on one screen -- same gate as Finance and Credit."""
    db.upsert_user(db_path, "222", "Guest", "guest")
    cfg = FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="222", display_name="Guest", role="guest", web_password="pw"),
    ])
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    c = TestClient(app)
    assert c.post("/api/login", json={"name": "Guest", "password": "pw"}).status_code == 200
    assert c.get("/api/command-center/snapshot").status_code == 403


# --- shape -------------------------------------------------------------------

def test_an_empty_install_still_returns_every_section(db_path):
    snap = _snapshot(_client(db_path))
    assert set(snap) == {"server_time", "home", "tasks", "pipelines", "events", "readouts"}
    assert snap["tasks"]["items"] == []
    assert snap["events"] == []
    assert len(snap["pipelines"]) == 2
    # Nine since "Needs you" joined them: an ask that is blocking the team is
    # front-screen news, not something he has to open a section to discover.
    assert len(snap["readouts"]) == 9


# --- resilience --------------------------------------------------------------

def test_a_dead_home_assistant_does_not_fail_the_request(db_path):
    snap = _snapshot(_client(db_path, home_assistant=FakeHA(fail=OSError("no route to host"))))
    assert snap["home"]["available"] is False
    assert "no route to host" in snap["home"]["error"]
    # Everything else still came back.
    assert snap["readouts"] and snap["pipelines"]


def test_no_home_assistant_configured_is_also_fine(db_path):
    snap = _snapshot(_client(db_path, home_assistant=None))
    assert snap["home"]["available"] is False


# --- tasks -------------------------------------------------------------------

def _in(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_tasks_are_bucketed_and_urgent_ones_sort_first(db_path):
    personal_db.create_task(db_path, 1, "someday thing")
    personal_db.create_task(db_path, 1, "next week", due_at=_in(4))
    personal_db.create_task(db_path, 1, "already late", due_at=_in(-2))

    snap = _snapshot(_client(db_path))
    tasks = snap["tasks"]
    assert tasks["buckets"] == {"overdue": 1, "week": 1, "someday": 1}
    assert [t["text"] for t in tasks["items"]] == ["already late", "next week", "someday thing"]
    assert tasks["items"][0]["bucket"] == "overdue"


def test_done_and_dropped_tasks_are_not_open_work(db_path):
    task_id = personal_db.create_task(db_path, 1, "finished")
    personal_db.update_task(db_path, 1, task_id, status="done")
    personal_db.create_task(db_path, 1, "still open")

    tasks = _snapshot(_client(db_path))["tasks"]
    assert [t["text"] for t in tasks["items"]] == ["still open"]
    assert tasks["open"] == 1


def test_overdue_count_reaches_the_readout_strip(db_path):
    personal_db.create_task(db_path, 1, "late", due_at=_in(-1))
    snap = _snapshot(_client(db_path))
    readout = _readout(snap, "Tasks")
    assert readout["value"] == "1"
    assert readout["sub"] == "1 overdue"
    assert readout["k"] == "warn"


# --- pipelines ---------------------------------------------------------------

def test_pipeline_stages_are_real_row_counts(db_path):
    business_db.create_task(db_path, 1, "build the thing")
    business_db.create_task(db_path, 1, "another")
    snap = _snapshot(_client(db_path))
    dev = next(p for p in snap["pipelines"] if p["name"].startswith("Development"))
    backlog = next(s for s in dev["stages"] if s["label"] == "Backlog")
    assert backlog["count"] == 2


# --- events ------------------------------------------------------------------

def test_events_merge_sources_newest_first(db_path):
    now = datetime.now(timezone.utc)
    with db._connect(db_path) as conn:
        conn.execute(
            "INSERT INTO agent_runs (agent, started_at, finished_at, status, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            ("market_finder", (now - timedelta(hours=2)).isoformat(),
             (now - timedelta(hours=2)).isoformat(), "ok", "found three leads"))
        conn.commit()
    personal_db.create_task(db_path, 1, "tidy the garage")
    task_id = personal_db.create_task(db_path, 1, "book the vet")
    personal_db.update_task(db_path, 1, task_id, status="done")

    events = _snapshot(_client(db_path))["events"]
    texts = [e["text"] for e in events]
    assert any("found three leads" in t for t in texts)
    assert any("book the vet" in t for t in texts)
    # Newest first.
    assert events == sorted(events, key=lambda e: e["at"], reverse=True)


def test_a_row_with_an_unparseable_timestamp_is_skipped_not_fatal(db_path):
    with db._connect(db_path) as conn:
        conn.execute(
            "INSERT INTO agent_runs (agent, started_at, finished_at, status, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            ("market_finder", "not-a-date", "not-a-date", "ok", "should be skipped"))
        conn.commit()
    assert _snapshot(_client(db_path))["events"] == []


# --- readouts ----------------------------------------------------------------

def test_a_readout_with_no_data_shows_a_dash_not_a_zero(db_path):
    """The distinction the whole strip rests on: '—' means nothing is recorded, '0'
    means nothing is outstanding. They are not the same news."""
    snap = _snapshot(_client(db_path))
    assert _readout(snap, "Credit score")["value"] == "—"
    assert _readout(snap, "Credit score")["sub"] == "no entries"
    assert _readout(snap, "Meal plan")["value"] == "—"
    # Nothing outstanding, by contrast, is a real zero.
    assert _readout(snap, "Review queue")["value"] == "0"


def test_credit_readout_uses_the_most_recent_entry(db_path):
    personal_db.create_credit_score_entry(db_path, 1, "experian", 690, "2026-01-01")
    personal_db.create_credit_score_entry(db_path, 1, "equifax", 712, "2026-06-01")
    readout = _readout(_snapshot(_client(db_path)), "Credit score")
    assert readout["value"] == "712"
    assert readout["sub"] == "Equifax"


def test_pantry_counts_an_item_low_against_its_own_threshold(db_path):
    with db._connect(db_path) as conn:
        conn.execute(
            "INSERT INTO kitchen_inventory (owner_user_id, item, normalized_item, quantity,"
            " unit, low_threshold, updated_at) VALUES (?,?,?,?,?,?,?)",
            (1, "Flour", "flour", 1.0, "count", 2.0, datetime.now(timezone.utc).isoformat()))
        conn.execute(
            "INSERT INTO kitchen_inventory (owner_user_id, item, normalized_item, quantity,"
            " unit, low_threshold, updated_at) VALUES (?,?,?,?,?,?,?)",
            (1, "Sugar", "sugar", 5.0, "count", 2.0, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    readout = _readout(_snapshot(_client(db_path)), "Pantry")
    assert readout["value"] == "1/2"
    assert readout["sub"] == "1 low or out"


def test_a_failed_run_is_flagged_amber_in_the_log(db_path):
    """The log is tinted for exactly one reason -- so a failure or a decision waiting on
    you is visible without reading every line. A failure logged as 'base' is invisible."""
    now = datetime.now(timezone.utc).isoformat()
    with db._connect(db_path) as conn:
        conn.execute(
            "INSERT INTO agent_runs (agent, started_at, finished_at, status, summary) "
            "VALUES (?,?,?,?,?)", ("trend_scout", now, now, "error", "scraper timed out"))
        conn.execute(
            "INSERT INTO agent_runs (agent, started_at, finished_at, status, summary) "
            "VALUES (?,?,?,?,?)", ("market_finder", now, now, "ok", "all good"))
        conn.commit()

    events = {e["text"].split(":")[0]: e["kind"] for e in _snapshot(_client(db_path))["events"]}
    assert events["trend scout"] == "warn"
    assert events["market finder"] == "base"
