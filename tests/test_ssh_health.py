"""ssh_health.py: a plain TCP reachability check for every registered SSH host, no
credentials involved -- distinct from ssh_ops.py's real (paramiko, authenticated)
connections, which this module never touches."""
from unittest.mock import MagicMock, patch

from assistant.core import ssh_health


def test_check_host_reports_reachable_and_latency_on_success():
    with patch("socket.create_connection") as mock_connect:
        mock_connect.return_value.__enter__ = MagicMock()
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        result = ssh_health.check_host("jarvisbox", {"host": "127.0.0.1"})

    assert result["name"] == "jarvisbox"
    assert result["host"] == "127.0.0.1"
    assert result["reachable"] is True
    assert isinstance(result["latency_ms"], int)
    mock_connect.assert_called_once_with(("127.0.0.1", 22), timeout=ssh_health.DEFAULT_TIMEOUT)


def test_check_host_reports_unreachable_on_connection_failure():
    with patch("socket.create_connection", side_effect=OSError("no route to host")):
        result = ssh_health.check_host("simrig", {"host": "simrig.local"})

    assert result["reachable"] is False
    assert result["latency_ms"] is None


def test_check_host_reports_unreachable_on_timeout():
    with patch("socket.create_connection", side_effect=TimeoutError("timed out")):
        result = ssh_health.check_host("touch1", {"host": "192.168.0.135"})

    assert result["reachable"] is False


def test_check_host_surfaces_is_jarvis_host_and_purpose():
    with patch("socket.create_connection", side_effect=OSError()):
        result = ssh_health.check_host(
            "homeassistant", {"host": "192.168.0.32", "is_jarvis_host": False, "purpose": "The HA server."})

    assert result["is_jarvis_host"] is False
    assert result["purpose"] == "The HA server."


def test_check_host_defaults_when_metadata_is_missing():
    with patch("socket.create_connection", side_effect=OSError()):
        result = ssh_health.check_host("legacy", {"host": "1.2.3.4"})

    assert result["is_jarvis_host"] is False
    assert result["purpose"] == ""


def test_check_all_hosts_checks_every_registered_host():
    hosts = {
        "a": {"host": "1.1.1.1"},
        "b": {"host": "2.2.2.2"},
    }
    with patch("socket.create_connection", side_effect=OSError()):
        results = ssh_health.check_all_hosts(hosts)

    assert {r["name"] for r in results} == {"a", "b"}
    assert all(r["reachable"] is False for r in results)


def test_check_all_hosts_with_empty_registry_returns_empty_list():
    assert ssh_health.check_all_hosts({}) == []
