"""Covers host_fixes.py's whitelist dispatch -- the pre-approved-only escape hatch from
ops_plans.py's normal one-at-a-time human review. The critical property under test is
negative: nothing on this path may ever touch ops_plans or the review queue, since a
whitelisted fix is pre-approved by definition and routing it through review would defeat
the point.
"""
from unittest.mock import MagicMock

from assistant.core import host_fixes, host_health


def test_attempt_fix_does_nothing_for_a_host_with_no_whitelist_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(host_fixes, "WHITELISTED_FIXES", {})
    db_path = str(tmp_path / "jarvis.db")
    host_health.init_host_health_db(db_path)

    result = host_fixes.attempt_fix(db_path, MagicMock(), "jarvisbox")

    assert result is None


def test_attempt_fix_does_nothing_without_an_ssh_client(tmp_path, monkeypatch):
    monkeypatch.setattr(host_fixes, "WHITELISTED_FIXES", {
        "jarvisbox": {"id": "restart_jarvisweb", "command": "echo ok", "description": "x"}})
    db_path = str(tmp_path / "jarvis.db")
    host_health.init_host_health_db(db_path)

    assert host_fixes.attempt_fix(db_path, None, "jarvisbox") is None


def test_attempt_fix_runs_the_whitelisted_command_and_records_success(tmp_path, monkeypatch):
    monkeypatch.setattr(host_fixes, "WHITELISTED_FIXES", {
        "jarvisbox": {"id": "restart_jarvisweb", "command": "Restart-Service JarvisWeb",
                      "description": "restarts the web service"}})
    db_path = str(tmp_path / "jarvis.db")
    host_health.init_host_health_db(db_path)
    host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}])

    ssh_ops = MagicMock()
    ssh_ops.run_command.return_value = {"ok": True, "exit_code": 0, "output": "done"}

    result = host_fixes.attempt_fix(db_path, ssh_ops, "jarvisbox")

    ssh_ops.run_command.assert_called_once_with("jarvisbox", "Restart-Service JarvisWeb")
    assert result["outcome"] == "ok"
    assert host_health.get_host(db_path, "jarvisbox")["last_fix_id"] == "restart_jarvisweb"
    assert host_health.get_host(db_path, "jarvisbox")["last_fix_result"] == "ok"


def test_attempt_fix_records_a_failed_command_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(host_fixes, "WHITELISTED_FIXES", {
        "jarvisbox": {"id": "restart_jarvisweb", "command": "Restart-Service JarvisWeb",
                      "description": "x"}})
    db_path = str(tmp_path / "jarvis.db")
    host_health.init_host_health_db(db_path)
    host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}])

    ssh_ops = MagicMock()
    ssh_ops.run_command.return_value = {"ok": False, "exit_code": 1, "output": "denied"}

    result = host_fixes.attempt_fix(db_path, ssh_ops, "jarvisbox")

    assert result["outcome"] == "failed (exit 1)"
    assert host_health.get_host(db_path, "jarvisbox")["last_fix_result"] == "failed (exit 1)"


def test_attempt_fix_records_an_errored_connection_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(host_fixes, "WHITELISTED_FIXES", {
        "jarvisbox": {"id": "restart_jarvisweb", "command": "Restart-Service JarvisWeb",
                      "description": "x"}})
    db_path = str(tmp_path / "jarvis.db")
    host_health.init_host_health_db(db_path)
    host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}])

    ssh_ops = MagicMock()
    ssh_ops.run_command.side_effect = Exception("connection refused")

    result = host_fixes.attempt_fix(db_path, ssh_ops, "jarvisbox")

    assert result["outcome"] == "errored"


def test_attempt_fix_never_touches_ops_plans(tmp_path, monkeypatch):
    """The property that actually matters: a whitelisted fix is pre-approved, so it must
    never create a review item or go anywhere near ops_plans -- that would defeat the
    entire reason the whitelist exists as a SEPARATE path from propose_ops_plan."""
    from assistant.core import ops_plans

    monkeypatch.setattr(host_fixes, "WHITELISTED_FIXES", {
        "jarvisbox": {"id": "restart_jarvisweb", "command": "Restart-Service JarvisWeb",
                      "description": "x"}})
    db_path = str(tmp_path / "jarvis.db")
    host_health.init_host_health_db(db_path)
    ops_plans.init_ops_plans_db(db_path)
    host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}])

    ssh_ops = MagicMock()
    ssh_ops.run_command.return_value = {"ok": True, "exit_code": 0, "output": "done"}

    host_fixes.attempt_fix(db_path, ssh_ops, "jarvisbox")

    assert ops_plans.list_plans(db_path, owner_user_id=1) == []


def test_whitelisted_fixes_ships_empty():
    """The whitelist starts with nothing pre-approved -- what belongs on it is Jack's
    call, not something this build should guess at. See the module docstring."""
    assert host_fixes.WHITELISTED_FIXES == {}
