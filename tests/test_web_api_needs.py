"""The Projects board — both lanes, and the guards that keep it honest.

For a long time business_tasks had no screen at all: the Command Center counted them as
"Backlog" and "Building" with nothing to click, and the section labelled "Tasks" showed
personal_tasks, a different table entirely. So the team's own plan was invisible to the
person it was being run for. These tests pin the shape that fixed that, plus the two
things that would quietly corrupt it: a free-text task status, and a secret coming back
down the wire.
"""
import pathlib
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import business_db, db, owner_requests
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    generated_media_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


@pytest.fixture
def media_dir(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    return d


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    owner_requests.init_owner_requests(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner_id(db_path):
    return db.get_user_by_chat_id(db_path, "111")["id"]


@pytest.fixture
def client(db_path, media_dir):
    cfg = FakeConfig(
        db_path=db_path, generated_media_path=str(media_dir),
        users=[UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw")],
    )
    c = TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))
    assert c.post("/api/login", json={"name": "Dug", "password": "pw"}).status_code == 200
    return c


def test_requires_login(db_path, media_dir):
    cfg = FakeConfig(db_path=db_path, generated_media_path=str(media_dir), users=[])
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    assert TestClient(app).get("/api/needs").status_code == 401


def test_both_lanes_arrive_under_one_project(client, db_path, owner_id):
    """The whole point of the board: what the team needs and what it is doing, together.
    Either half alone is misleading -- tasks without blockers read as healthy progress
    right up until you notice nothing ships."""
    pid = business_db.create_project(db_path, owner_id, "Jarvis Store", goal="Sell things")
    business_db.create_task(db_path, owner_id, "Wire up Printify", project_id=pid)
    owner_requests.raise_request(db_path, owner_id, title="Printify token", kind="secret",
                                 name="printify_api_key", project_id=pid,
                                 blocks="all fulfilment", why="cannot fulfil anything")

    body = client.get("/api/needs").json()
    group = next(g for g in body["groups"] if g["project_id"] == pid)
    assert group["name"] == "Jarvis Store" and group["goal"] == "Sell things"
    assert [i["title"] for i in group["items"]] == ["Printify token"]
    assert [t["text"] for t in group["tasks"]] == ["Wire up Printify"]
    assert group["task_counts"]["open"] == 1
    assert body["summary"]["open"] == 1


def test_a_blocked_project_sorts_above_a_merely_busy_one(client, db_path, owner_id):
    busy = business_db.create_project(db_path, owner_id, "Aaa Busy")
    for i in range(4):
        business_db.create_task(db_path, owner_id, f"task {i}", project_id=busy)
    blocked = business_db.create_project(db_path, owner_id, "Zzz Blocked")
    owner_requests.raise_request(db_path, owner_id, title="A token", kind="secret",
                                 name="tok", project_id=blocked)

    ids = [g["project_id"] for g in client.get("/api/needs").json()["groups"]]
    assert ids.index(blocked) < ids.index(busy), "what he can act on comes first"


def test_work_with_no_project_lands_in_an_explicit_bucket_at_the_bottom(client, db_path, owner_id):
    pid = business_db.create_project(db_path, owner_id, "Real Project")
    owner_requests.raise_request(db_path, owner_id, title="Scoped", kind="text",
                                 name="a", project_id=pid)
    business_db.create_task(db_path, owner_id, "orphan task")

    groups = client.get("/api/needs").json()["groups"]
    assert groups[-1]["project_id"] is None and groups[-1]["name"] == "Unassigned"
    assert [t["text"] for t in groups[-1]["tasks"]] == ["orphan task"]


class TestTheWorkLane:
    def test_dropped_work_is_counted_but_never_redisplayed(self, client, db_path, owner_id):
        """He abandoned it deliberately. A board that keeps showing dropped work argues
        with him."""
        pid = business_db.create_project(db_path, owner_id, "P")
        tid = business_db.create_task(db_path, owner_id, "abandoned", project_id=pid)
        business_db.update_task(db_path, owner_id, tid, status="dropped")

        group = client.get("/api/needs").json()["groups"][0]
        assert [t["text"] for t in group["tasks"]] == []
        assert group["task_counts"]["dropped"] == 1

    def test_finished_work_is_capped_so_it_cannot_bury_live_work(self, client, db_path, owner_id):
        pid = business_db.create_project(db_path, owner_id, "P")
        for i in range(9):
            tid = business_db.create_task(db_path, owner_id, f"done {i}", project_id=pid)
            business_db.update_task(db_path, owner_id, tid, status="done")
        business_db.create_task(db_path, owner_id, "still open", project_id=pid)

        group = client.get("/api/needs").json()["groups"][0]
        shown = [t["text"] for t in group["tasks"]]
        assert "still open" in shown, "live work always survives the cap"
        assert sum(1 for t in shown if t.startswith("done")) == 5
        assert group["task_counts"]["done"] == 9, "the count is still the truth"

    def test_he_can_advance_a_task(self, client, db_path, owner_id):
        tid = business_db.create_task(db_path, owner_id, "a task")
        assert client.post(f"/api/needs/tasks/{tid}/status", json={"status": "done"}).status_code == 200
        assert business_db.list_tasks(db_path, owner_id)[0]["status"] == "done"

    def test_a_status_the_table_does_not_use_is_refused(self, client, db_path, owner_id):
        """update_task takes **fields and would happily write 'finished' into the status
        column, where nothing would ever match it again and the row would vanish from
        every count on the board."""
        tid = business_db.create_task(db_path, owner_id, "a task")
        r = client.post(f"/api/needs/tasks/{tid}/status", json={"status": "finished"})
        assert r.status_code == 400
        assert business_db.list_tasks(db_path, owner_id)[0]["status"] == "open"

    def test_advancing_a_task_that_does_not_exist_is_a_404(self, client):
        assert client.post("/api/needs/tasks/9999/status", json={"status": "done"}).status_code == 404


class TestSupplying:
    def test_a_secret_goes_up_and_never_comes_back(self, client, db_path, owner_id):
        rid = owner_requests.raise_request(db_path, owner_id, title="Printify token",
                                           kind="secret", name="printify_api_key")
        r = client.post(f"/api/needs/{rid}/provide", json={"value": "sk_live_abcdef1234567890"})
        assert r.status_code == 200
        assert "abcdef" not in r.text, "the response must not echo what he just sent"
        assert r.json()["item"]["value"] == "...7890"
        # ...but the agent that was blocked reads the real thing.
        assert owner_requests.secret(db_path, "printify_api_key") == "sk_live_abcdef1234567890"
        assert "abcdef" not in client.get("/api/needs").text

    def test_an_empty_supply_is_refused_rather_than_stored(self, client, db_path, owner_id):
        rid = owner_requests.raise_request(db_path, owner_id, title="T", kind="text", name="t")
        assert client.post(f"/api/needs/{rid}/provide", json={"value": "   "}).status_code == 400
        assert owner_requests.secret(db_path, "t") is None

    def test_supplying_something_that_was_never_asked_for_is_a_404(self, client):
        assert client.post("/api/needs/9999/provide", json={"value": "x"}).status_code == 404

    def test_he_can_decline_a_request(self, client, db_path, owner_id):
        rid = owner_requests.raise_request(db_path, owner_id, title="T", kind="account", name="t")
        r = client.post(f"/api/needs/{rid}/status", json={"status": "rejected", "note": "no"})
        assert r.status_code == 200
        assert owner_requests.list_requests(db_path, owner_id)[0]["status"] == "rejected"

    def test_an_unknown_request_status_is_refused(self, client, db_path, owner_id):
        rid = owner_requests.raise_request(db_path, owner_id, title="T", kind="text", name="t")
        assert client.post(f"/api/needs/{rid}/status", json={"status": "banana"}).status_code == 400


def test_generation_prompts_reach_the_browser_as_a_list(client, db_path, owner_id):
    """He generates catalogue models by hand in Leonardo's web UI, so the prompts have to
    arrive as separate strings the board can put a copy button beside."""
    owner_requests.raise_request(
        db_path, owner_id, title="New model", kind="file", name="model_x",
        why="one face per category", prompts=["prompt one", "prompt two"])
    item = client.get("/api/needs").json()["groups"][0]["items"][0]
    assert item["prompts"] == ["prompt one", "prompt two"]
