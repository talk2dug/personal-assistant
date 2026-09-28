"""Covers host_health.py: the recorded-over-time half of device monitoring. ssh_health.py
already proves the raw TCP probe works (test_ssh_health.py); this covers what's new --
merging in the non-SSH services, and turning a stream of point-in-time checks into
up/down/recovered transitions.
"""
from unittest.mock import MagicMock, patch

import pytest

from assistant.core import host_health


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "jarvis.db")
    host_health.init_host_health_db(path)
    return path


def test_check_all_merges_ssh_hosts_and_http_services():
    with patch("socket.create_connection") as mock_connect:
        mock_connect.return_value.__enter__ = MagicMock()
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        with patch("httpx.get") as mock_get:
            mock_get.return_value = MagicMock()
            results = host_health.check_all(
                {"jarvisbox": {"host": "127.0.0.1"}},
                orpheus_url="http://127.0.0.1:8130/tts")

    names = {r["name"] for r in results}
    assert names == {"jarvisbox", "orpheus"}
    assert all(r["reachable"] for r in results)


def test_check_all_reports_unreachable_http_service_on_connection_failure():
    with patch("socket.create_connection", side_effect=OSError()):
        with patch("httpx.get", side_effect=Exception("connection refused")):
            results = host_health.check_all(
                {"jarvisbox": {"host": "127.0.0.1"}}, orpheus_url="http://127.0.0.1:8130/tts")

    orpheus = next(r for r in results if r["name"] == "orpheus")
    assert orpheus["reachable"] is False
    assert orpheus["latency_ms"] is None


def test_check_all_includes_gpu_bridge_when_given():
    bridge = MagicMock()
    bridge.reachable.return_value = True
    bridge.comfy = None
    with patch("socket.create_connection", side_effect=OSError()):
        results = host_health.check_all({}, bridge=bridge)

    assert {"name": "simrig_ollama", "kind": "http", "reachable": True, "latency_ms": None} in results


def test_check_all_skips_unconfigured_services():
    with patch("socket.create_connection", side_effect=OSError()):
        results = host_health.check_all({})
    assert results == []


def test_record_check_first_sighting_of_a_down_host_is_a_down_transition(db_path):
    changed = host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}])

    assert len(changed) == 1
    assert changed[0]["transition"] == "down"
    assert changed[0]["consecutive_failures"] == 1


def test_record_check_first_sighting_of_an_up_host_is_not_reported(db_path):
    changed = host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": True, "latency_ms": 12}])
    assert changed == []


def test_record_check_reports_recovery(db_path):
    host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}])
    changed = host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": True, "latency_ms": 8}])

    assert len(changed) == 1
    assert changed[0]["transition"] == "recovered"
    assert changed[0]["consecutive_failures"] == 0


def test_record_check_reports_still_down_with_growing_failure_count(db_path):
    host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}])
    changed = host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}])

    assert changed[0]["transition"] == "still_down"
    assert changed[0]["consecutive_failures"] == 2


def test_record_check_unchanged_up_host_is_not_reported(db_path):
    host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": True, "latency_ms": 10}])
    changed = host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": True, "latency_ms": 9}])
    assert changed == []


def test_get_host_and_all_hosts_reflect_recorded_state(db_path):
    host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": True, "latency_ms": 10},
                  {"name": "simrig", "kind": "ssh", "reachable": False, "latency_ms": None}])

    assert host_health.get_host(db_path, "jarvisbox")["reachable"] == 1
    assert host_health.get_host(db_path, "missing") is None
    assert {h["name"] for h in host_health.all_hosts(db_path)} == {"jarvisbox", "simrig"}


def test_record_fix_attempt_is_visible_on_the_host_row(db_path):
    host_health.record_check(
        db_path, [{"name": "jarvisbox", "kind": "ssh", "reachable": False, "latency_ms": None}])
    host_health.record_fix_attempt(db_path, "jarvisbox", "restart_jarvisweb", "ok")

    row = host_health.get_host(db_path, "jarvisbox")
    assert row["last_fix_id"] == "restart_jarvisweb"
    assert row["last_fix_result"] == "ok"
