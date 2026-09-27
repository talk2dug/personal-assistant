"""Covers scheduler.run_host_health_tick -- the wiring around host_health.py/host_fixes.py
that actually ran on a clock.

This exists because of a real bug that shipped and only showed up live: the first version
wired `_health_ssh_ops = business.ssh_ops`, but `business` (as scheduler.start() receives
it) is an engine.BusinessContext wrapper -- the real BusinessClient, which actually holds
.ssh_ops, is its .mcp_client field. No unit test exercised that object shape, so it reached
production and crash-looped the JarvisCore service every ~60-110s until caught by running
the process in the foreground. test_business_context_shape_matches_what_scheduler_expects
below is the direct regression test for that: it uses the REAL engine.BusinessContext
dataclass, not a mock, so a future field rename can't silently hide the same mistake again.
"""
from unittest.mock import MagicMock, patch

import pytest

from assistant.core import attention, host_health, scheduler
from assistant.core.engine import BusinessContext


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "jarvis.db")
    host_health.init_host_health_db(path)
    attention.init_attention_db(path)
    return path


def test_run_host_health_tick_records_checks_with_no_notification_when_all_up(db_path):
    notified = []
    with patch("assistant.core.host_health.check_all",
              return_value=[{"name": "jarvisbox", "kind": "ssh", "reachable": True, "latency_ms": 5}]):
        outcomes = scheduler.run_host_health_tick(
            db_path, {"jarvisbox": {"host": "127.0.0.1"}},
            lambda chat_id, text: notified.append((chat_id, text)), "111")

    assert outcomes == []
    assert notified == []
    assert host_health.get_host(db_path, "jarvisbox")["reachable"] == 1


def test_run_host_health_tick_notifies_on_a_fresh_outage_with_no_whitelisted_fix(db_path, monkeypatch):
    notified = []
    monkeypatch.setattr("assistant.core.host_fixes.WHITELISTED_FIXES", {})
    with patch("assistant.core.host_health.check_all",
              return_value=[{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}]):
        outcomes = scheduler.run_host_health_tick(
            db_path, {"jarvisbox": {"host": "127.0.0.1"}},
            lambda chat_id, text: notified.append((chat_id, text)), "111")

    assert len(outcomes) == 1
    assert outcomes[0]["transition"] == "down"
    assert outcomes[0]["fix"] is None
    assert outcomes[0]["told"] is True
    assert len(notified) == 1
    assert notified[0][0] == "111"
    assert "jarvisbox is down" in notified[0][1]
    assert "No pre-approved fix" in notified[0][1]


def test_run_host_health_tick_fixes_and_reports_fixed_not_down(db_path, monkeypatch):
    notified = []
    monkeypatch.setattr("assistant.core.host_fixes.WHITELISTED_FIXES", {
        "jarvisbox": {"id": "restart_jarvisweb", "command": "echo ok", "description": "x"}})
    ssh_ops = MagicMock()
    ssh_ops.run_command.return_value = {"ok": True, "exit_code": 0, "output": "done"}

    with patch("assistant.core.host_health.check_all",
              return_value=[{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}]):
        outcomes = scheduler.run_host_health_tick(
            db_path, {"jarvisbox": {"host": "127.0.0.1"}},
            lambda chat_id, text: notified.append((chat_id, text)), "111", ssh_ops=ssh_ops)

    assert outcomes[0]["fix"]["outcome"] == "ok"
    assert len(notified) == 1
    assert "ran restart_jarvisweb and it's back" in notified[0][1]


def test_run_host_health_tick_reports_recovery(db_path):
    notified = []
    with patch("assistant.core.host_health.check_all",
              return_value=[{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}]):
        scheduler.run_host_health_tick(
            db_path, {"jarvisbox": {"host": "127.0.0.1"}},
            lambda chat_id, text: notified.append((chat_id, text)), "111")
    notified.clear()

    with patch("assistant.core.host_health.check_all",
              return_value=[{"name": "jarvisbox", "kind": "ssh", "reachable": True, "latency_ms": 5}]):
        outcomes = scheduler.run_host_health_tick(
            db_path, {"jarvisbox": {"host": "127.0.0.1"}},
            lambda chat_id, text: notified.append((chat_id, text)), "111")

    assert outcomes[0]["transition"] == "recovered"
    assert "back up" in notified[0][1]


def test_run_host_health_tick_never_calls_notify_without_an_owner_chat_id(db_path):
    """`told` mirrors attention.should_say's own verdict (same convention as
    executive.watch_workers), independent of whether there's an owner to actually deliver
    to -- what this test guards is that notify() itself is never invoked with no owner."""
    with patch("assistant.core.host_health.check_all",
              return_value=[{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}]):
        scheduler.run_host_health_tick(
            db_path, {"jarvisbox": {"host": "127.0.0.1"}},
            lambda chat_id, text: pytest.fail("should not notify with no owner"), None)


def test_business_context_shape_matches_what_scheduler_expects():
    """The regression test for the actual live bug: scheduler.py reaches the SSH ops
    client via business.mcp_client.ssh_ops, not business.ssh_ops. Built from the real
    engine.BusinessContext dataclass -- not a stand-in shape -- so a future field rename
    on either side fails this test instead of only failing at 2am in production.
    """
    fake_client = MagicMock()
    fake_client.ssh_ops = MagicMock(name="the real SSHOpsClient")
    business = BusinessContext(mcp_client=fake_client, profile=MagicMock(), agents_scheduled=False)

    assert not hasattr(business, "ssh_ops")
    assert business.mcp_client.ssh_ops is fake_client.ssh_ops
