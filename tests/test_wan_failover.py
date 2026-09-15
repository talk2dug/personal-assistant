"""WAN failover: moving Jarvis's outbound traffic onto the LTE modem when the house
connection dies.

The rule every one of these encodes is FAIL DIRECT. A failover that misfires must leave
traffic on the normal path, because the thing it protects is the assistant's only way of
answering at all -- a broken failover has to be no worse than no failover. So every
uncertain case (proxy down, proxy up but no bearer behind it, feature disabled) must end
with the proxy NOT set.
"""
import pytest

from assistant.core import db, wan_failover


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


@pytest.fixture(autouse=True)
def clean_env():
    """Never leak a proxy setting between tests -- or out of the suite into the process
    actually running it."""
    wan_failover.clear_proxy()
    yield
    wan_failover.clear_proxy()


PROXY = "http://192.168.0.152:8888"


def _patch(monkeypatch, *, wan, reachable=True, has_internet=True):
    monkeypatch.setattr(wan_failover, "wan_reachable", lambda *a, **k: wan)
    monkeypatch.setattr(wan_failover, "proxy_reachable", lambda *a, **k: reachable)
    monkeypatch.setattr(wan_failover, "internet_via_proxy", lambda *a, **k: has_internet)


class TestHealthyWan:
    def test_a_working_wan_uses_no_proxy(self, db_path, monkeypatch):
        _patch(monkeypatch, wan=True)
        result = wan_failover.evaluate(db_path, PROXY)
        assert result["using_proxy"] is False
        assert wan_failover.proxy_active() is None

    def test_recovery_takes_jarvis_back_off_the_proxy(self, db_path, monkeypatch):
        _patch(monkeypatch, wan=False)
        wan_failover.evaluate(db_path, PROXY)
        assert wan_failover.proxy_active() == PROXY

        _patch(monkeypatch, wan=True)
        result = wan_failover.evaluate(db_path, PROXY)
        assert result["changed"] is True
        assert wan_failover.proxy_active() is None

    def test_recovery_releases_the_cellular_link(self, db_path, monkeypatch):
        """A bearer held up for no reason spends data allowance and battery."""
        _patch(monkeypatch, wan=False)
        wan_failover.evaluate(db_path, PROXY)
        assert wan_failover.link_wanted(db_path) is True

        _patch(monkeypatch, wan=True)
        wan_failover.evaluate(db_path, PROXY)
        assert wan_failover.link_wanted(db_path) is False


class TestWanDown:
    def test_the_cellular_link_is_requested_before_the_proxy_is_tried(self, db_path, monkeypatch):
        """The Pi polls that flag, so the bearer has to be asked for before there is
        anything behind the proxy to test."""
        _patch(monkeypatch, wan=False, reachable=False)
        wan_failover.evaluate(db_path, PROXY)
        assert wan_failover.link_wanted(db_path) is True

    def test_a_dead_wan_with_a_live_proxy_switches_over(self, db_path, monkeypatch):
        _patch(monkeypatch, wan=False)
        result = wan_failover.evaluate(db_path, PROXY)
        assert result["using_proxy"] is True and result["changed"] is True
        assert wan_failover.proxy_active() == PROXY

    def test_the_lan_is_never_proxied(self, db_path, monkeypatch):
        """Home Assistant, Ollama and the terminals are local. During an outage the local
        half is the half that still works -- sending it over cellular and back would be
        absurd."""
        _patch(monkeypatch, wan=False)
        wan_failover.evaluate(db_path, PROXY)
        import os
        assert "192.168.0.0/16" in os.environ["NO_PROXY"]
        assert "127.0.0.1" in os.environ["NO_PROXY"]

    def test_both_upper_and_lower_case_vars_are_set(self, db_path, monkeypatch):
        """httpx, requests, urllib and the Claude CLI subprocess do not agree on which
        spelling they read."""
        _patch(monkeypatch, wan=False)
        wan_failover.evaluate(db_path, PROXY)
        import os
        assert os.environ["HTTPS_PROXY"] == PROXY
        assert os.environ["https_proxy"] == PROXY


class TestFailsDirect:
    def test_an_unreachable_proxy_leaves_traffic_alone(self, db_path, monkeypatch):
        """Better to keep trying a dead WAN than to point Jarvis at a proxy that is not
        there."""
        _patch(monkeypatch, wan=False, reachable=False)
        result = wan_failover.evaluate(db_path, PROXY)
        assert result["using_proxy"] is False
        assert wan_failover.proxy_active() is None
        assert "not reachable" in result["note"]

    def test_a_listening_proxy_with_no_bearer_behind_it_is_not_used(self, db_path, monkeypatch):
        """tinyproxy answers on its port whether or not the modem has a link, so
        'the port is open' is not the same claim as 'this reaches Claude'."""
        _patch(monkeypatch, wan=False, reachable=True, has_internet=False)
        result = wan_failover.evaluate(db_path, PROXY)
        assert result["using_proxy"] is False
        assert wan_failover.proxy_active() is None

    def test_disabled_means_disabled_and_undoes_itself(self, db_path, monkeypatch):
        _patch(monkeypatch, wan=False)
        wan_failover.evaluate(db_path, PROXY)
        assert wan_failover.proxy_active() == PROXY
        result = wan_failover.evaluate(db_path, PROXY, enabled=False)
        assert result["enabled"] is False
        assert wan_failover.proxy_active() is None

    def test_no_configured_proxy_is_a_no_op(self, db_path, monkeypatch):
        _patch(monkeypatch, wan=False)
        assert wan_failover.evaluate(db_path, None)["using_proxy"] is False


class TestWanProbe:
    def test_one_dead_probe_does_not_count_as_an_outage(self, monkeypatch):
        """One operator having a bad day is not the house losing its connection."""
        import socket
        attempted = []

        def fake_connect(addr, timeout=None):
            attempted.append(addr[0])
            if addr[0] == "1.1.1.1":
                raise OSError("unreachable")
            class Sock:
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return Sock()

        monkeypatch.setattr(socket, "create_connection", fake_connect)
        assert wan_failover.wan_reachable() is True
        assert attempted[0] == "1.1.1.1", "it should have tried the failing one first"

    def test_every_probe_failing_is_an_outage(self, monkeypatch):
        import socket
        monkeypatch.setattr(socket, "create_connection",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
        assert wan_failover.wan_reachable() is False
