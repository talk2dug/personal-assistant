"""Covers /api/agents/status — the single call the pixel office renders itself from.

The office draws one coherent scene per frame, so this endpoint has to describe a single
consistent moment: who is working, who is queued for the GPU and in what order, and
whether the machine is available at all. A character walking to a machine that is
actually reserved, or queueing in the wrong order, is a picture that was never true.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import business_db, db
from assistant.core.agents import AGENT_ROSTER
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


class FakeBridge:
    def __init__(self, mode="available", running=0, queued=0, jobs=None):
        self._mode = mode
        self._running = running
        self._queued = queued
        self._jobs = jobs or []

    def status(self):
        return {
            "reachable": True, "mode": self._mode, "reason": "racing" if self._mode == "reserved" else None,
            "queued": self._queued, "running": self._running, "vram_free_gb": 9.5, "vram_used_gb": 6.5,
            "loaded_models": [{"model": "qwen3-vl:8b", "vram_gb": 6.5}],
            "comfyui": {"reachable": True},
            "task_routing": {"vision": {"model": "qwen3-vl:8b", "engine": "ollama", "available": True}},
        }

    def jobs(self, status=None, limit=25):
        return [j for j in self._jobs if j["status"] == status][:limit]


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def cfg(db_path):
    return FakeConfig(
        db_path=db_path,
        users=[UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw")],
    )


def make_client(cfg, bridge=None):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, bridge=bridge, static_dir=None)
    client = TestClient(app)
    assert client.post("/api/login", json={"name": "Dug", "password": "pw"}).status_code == 200
    return client


def test_requires_login(cfg):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    assert TestClient(app).get("/api/agents/status").status_code == 401


def test_reports_the_whole_roster_even_before_anyone_has_run(cfg):
    body = make_client(cfg).get("/api/agents/status").json()
    assert [a["key"] for a in body["agents"]] == [m["key"] for m in AGENT_ROSTER]
    assert all(a["status"] == "idle" for a in body["agents"])
    # Each needs a sprite and a desk position, or the office can't seat them.
    assert all(isinstance(a["character"], int) for a in body["agents"])


def test_a_running_agent_shows_as_working(cfg, db_path):
    business_db.start_agent_run(db_path, "market_finder")
    body = make_client(cfg).get("/api/agents/status").json()
    market = next(a for a in body["agents"] if a["key"] == "market_finder")
    assert market["status"] == "working"


def test_a_finished_run_shows_briefly_then_reads_as_idle(cfg, db_path):
    run_id = business_db.start_agent_run(db_path, "trend_scout")
    business_db.finish_agent_run(db_path, run_id, "ok", "Trend scan: 3 new ideas.")
    agents = make_client(cfg).get("/api/agents/status").json()["agents"]
    scout = next(a for a in agents if a["key"] == "trend_scout")
    assert scout["status"] == "just_finished"
    assert "3 new ideas" in scout["detail"]


def test_a_failed_run_is_distinguishable(cfg, db_path):
    run_id = business_db.start_agent_run(db_path, "research")
    business_db.finish_agent_run(db_path, run_id, "error", "Research queue failed: boom")
    agents = make_client(cfg).get("/api/agents/status").json()["agents"]
    assert next(a for a in agents if a["key"] == "research")["status"] == "failed"


def test_gpu_work_outranks_a_desk_run(cfg, db_path):
    """An agent on the GPU is standing at the machine, not sitting at its desk — the
    office can only draw it in one place, so the endpoint has to pick."""
    business_db.start_agent_run(db_path, "art_director")
    bridge = FakeBridge(running=1, jobs=[
        {"id": 1, "agent": "art_director", "task_type": "image_generation", "status": "running",
         "model": None, "queued_at": "2026-09-04T00:00:00"},
    ])
    agents = make_client(cfg, bridge).get("/api/agents/status").json()["agents"]
    art = next(a for a in agents if a["key"] == "art_director")
    assert art["status"] == "on_gpu"
    assert "image_generation" in art["detail"]


def test_queue_order_is_preserved_so_the_line_matches_reality(cfg):
    bridge = FakeBridge(queued=3, jobs=[
        {"id": 5, "agent": "store_manager", "task_type": "vision", "status": "queued",
         "model": "qwen3-vl:8b", "queued_at": "2026-09-04T00:00:01"},
        {"id": 6, "agent": "social_director", "task_type": "vision", "status": "queued",
         "model": "qwen3-vl:8b", "queued_at": "2026-09-04T00:00:02"},
        {"id": 7, "agent": "trend_scout", "task_type": "vision", "status": "queued",
         "model": "qwen3-vl:8b", "queued_at": "2026-09-04T00:00:03"},
    ])
    body = make_client(cfg, bridge).get("/api/agents/status").json()
    assert [j["agent"] for j in body["gpu"]["jobs"]] == [
        "store_manager", "social_director", "trend_scout"]
    waiting = [a["key"] for a in body["agents"] if a["status"] == "waiting_gpu"]
    assert set(waiting) == {"store_manager", "social_director", "trend_scout"}


def test_reserved_mode_is_surfaced(cfg):
    body = make_client(cfg, FakeBridge(mode="reserved")).get("/api/agents/status").json()
    assert body["gpu"]["mode"] == "reserved"
    assert body["gpu"]["reason"] == "racing"


def test_missing_bridge_degrades_instead_of_erroring(cfg):
    """simrig can be off. The office should still draw the staff."""
    body = make_client(cfg).get("/api/agents/status").json()
    assert body["gpu"]["configured"] is False
    assert body["gpu"]["reachable"] is False
    assert len(body["agents"]) == len(AGENT_ROSTER)


def test_in_tray_counts_come_through(cfg, db_path):
    owner_id = db.get_user_by_chat_id(db_path, "111")["id"]
    business_db.upsert_market_lead(db_path, owner_id, "Maker Fest", "2026-10-04", "RVA", None, None, 80, "fit")
    business_db.create_product_concept(db_path, owner_id, "RVA Decal", "sticker", "d")
    body = make_client(cfg).get("/api/agents/status").json()
    assert body["pipeline"]["market_leads"] == 1
    assert body["pipeline"]["concepts"] == 1
