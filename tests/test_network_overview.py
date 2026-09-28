"""Covers core/network_overview.py -- the join between recorded host checks and what the
Omada controller sees -- and the /api/infra/network route that serves it.

The two cross-reference findings are pinned to the real 2026-09-27 cases they were built
from: simrig's check dialling an unresolvable .local name while Omada saw it online, and
phone_mcp's configured IP being taken by an unrelated IoT client.
"""
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, host_health, network_overview, omada_health, ops_plans
from assistant.web.app import create_app

NOW = datetime(2026, 9, 27, 23, 30, tzinfo=timezone.utc)


@dataclass
class FakeConfig:
    db_path: str
    ssh_hosts: dict = field(default_factory=dict)
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None
    orpheus_url: str | None = None
    omada_controller_url: str | None = None


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    db.upsert_user(path, "222", "Partner", "partner")
    host_health.init_host_health_db(path)
    omada_health.init_omada_health_db(path)
    ops_plans.init_ops_plans_db(path)
    return path


def _clients(db_path, rows):
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executemany(
            "INSERT INTO omada_clients (mac, name, ip, first_seen_at, last_seen_at, online) VALUES (?,?,?,?,?,?)",
            rows)
        conn.commit()


def _hosts(db_path, results):
    host_health.record_check(db_path, results, now=NOW.isoformat())


def _cfg(db_path, **kw):
    return FakeConfig(db_path=db_path, **kw)


FIRST_SWEEP = (NOW - timedelta(hours=18)).isoformat()


def test_down_check_on_a_name_omada_sees_online_is_flagged_as_a_suspect_check(db_path):
    _hosts(db_path, [{"name": "simrig", "kind": "ssh", "reachable": False, "latency_ms": None}])
    _clients(db_path, [("AA", "simrig", "192.168.0.139", FIRST_SWEEP, NOW.isoformat(), 1)])
    cfg = _cfg(db_path, ssh_hosts={"simrig": {"host": "simrig.local"}})

    out = network_overview.build(db_path, cfg, 1, now=NOW)

    host = out["hosts"][0]
    assert host["omada_ip"] == "192.168.0.139"
    assert host["finding"]["kind"] == "check_suspect"
    assert "192.168.0.139" in host["finding"]["text"]


def test_down_service_whose_ip_now_belongs_to_another_client_is_flagged_as_moved(db_path):
    _hosts(db_path, [{"name": "oldnas", "kind": "ssh", "reachable": False, "latency_ms": None}])
    _clients(db_path, [("BB", "lwip0", "192.168.0.143", FIRST_SWEEP, NOW.isoformat(), 1)])
    cfg = _cfg(db_path, ssh_hosts={"oldnas": {"host": "192.168.0.143"}})

    host = network_overview.build(db_path, cfg, 1, now=NOW)["hosts"][0]

    assert host["target"] == "192.168.0.143"
    assert host["finding"]["kind"] == "ip_taken"
    assert '"lwip0"' in host["finding"]["text"]


def test_down_host_whose_own_machine_is_online_reads_as_service_down(db_path):
    _hosts(db_path, [{"name": "homeassistant", "kind": "ssh", "reachable": False, "latency_ms": None}])
    _clients(db_path, [("CC", "homeassistant", "192.168.0.32", FIRST_SWEEP, NOW.isoformat(), 1)])
    cfg = _cfg(db_path, ssh_hosts={"homeassistant": {"host": "192.168.0.32"}})

    host = network_overview.build(db_path, cfg, 1, now=NOW)["hosts"][0]

    assert host["finding"]["kind"] == "service_down"


def test_healthy_hosts_and_loopback_targets_carry_no_finding(db_path):
    _hosts(db_path, [
        {"name": "jarvisbox", "kind": "ssh", "reachable": True, "latency_ms": 0},
        {"name": "orpheus", "kind": "http", "reachable": False, "latency_ms": None},
    ])
    _clients(db_path, [("DD", "DESKTOP", "192.168.0.148", FIRST_SWEEP, NOW.isoformat(), 1)])
    cfg = _cfg(db_path, ssh_hosts={"jarvisbox": {"host": "127.0.0.1"}}, orpheus_url="http://127.0.0.1:8130/tts")

    by_name = {h["name"]: h for h in network_overview.build(db_path, cfg, 1, now=NOW)["hosts"]}

    assert by_name["jarvisbox"]["finding"] is None
    # Loopback has no network-side view -- no guess, just the plain "down".
    assert by_name["orpheus"]["finding"] is None
    assert by_name["orpheus"]["omada_ip"] is None


def test_only_clients_after_the_first_sweep_count_as_new(db_path):
    later = (NOW - timedelta(hours=3)).isoformat()
    _clients(db_path, [
        ("EE", "iPad", "192.168.0.126", FIRST_SWEEP, NOW.isoformat(), 1),
        ("FF", "F0-4F-E0-31-D4-E4", "192.168.0.157", later, NOW.isoformat(), 1),
    ])
    clients = network_overview.build(db_path, _cfg(db_path), 1, now=NOW)["omada"]["clients"]
    new = {c["mac"]: c["is_new"] for c in clients}
    assert new == {"EE": False, "FF": True}


def test_empty_database_reads_as_empty_not_an_error(tmp_path):
    path = str(tmp_path / "fresh.db")
    db.init_db(path)
    out = network_overview.build(path, _cfg(path), 1, now=NOW)
    assert out["hosts"] == [] and out["omada"]["clients"] == [] and out["plans"] == []


def _web(db_path, login_as="Dug", **kw):
    cfg = _cfg(db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw"),
        UserConfig(telegram_chat_id="222", display_name="Partner", role="partner", web_password="pw2"),
    ], **kw)
    c = TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))
    assert c.post("/api/login", json={"name": login_as, "password": "pw" if login_as == "Dug" else "pw2"}).status_code == 200
    return c


def test_route_is_owner_only(db_path):
    assert _web(db_path, login_as="Partner").get("/api/infra/network").status_code == 403


def test_route_returns_hosts_omada_and_plans(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        owner_id = conn.execute("SELECT id FROM users WHERE role = 'owner'").fetchone()[0]
    _hosts(db_path, [{"name": "pinas001", "kind": "ssh", "reachable": True, "latency_ms": 16}])
    ops_plans.create_plan(db_path, owner_id, "Diagnostic-only probe", [
        {"phase": "test", "host": "jarvisbox", "command": "echo hi", "purpose": "say hi"},
        {"phase": "rollback", "host": "jarvisbox", "command": "true", "purpose": "nothing"},
    ])
    resp = _web(db_path).get("/api/infra/network")
    assert resp.status_code == 200
    body = resp.json()
    assert [h["name"] for h in body["hosts"]] == ["pinas001"]
    assert body["omada"]["configured"] is False
    assert body["plans"][0]["summary"] == "Diagnostic-only probe"
    assert len(body["plans"][0]["steps"]) == 2
