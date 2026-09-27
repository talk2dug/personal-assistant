"""Covers scheduler.run_omada_health_tick -- the wiring that turns omada_health.py's
measurements into owner notifications, mirroring test_scheduler_host_health.py's suite.
No auto-fix here (unlike host_health's whitelisted-fix path): a network device that won't
come back on its own needs the owner, not a guessed remediation.
"""
import pytest

from assistant.core import attention, omada_health, scheduler


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "jarvis.db")
    omada_health.init_omada_health_db(path)
    attention.init_attention_db(path)
    return path


class FakeOmadaClient:
    def __init__(self, devices=None, clients=None):
        self._devices = devices or []
        self._clients = clients or []

    def list_devices(self):
        return self._devices

    def list_clients(self):
        return self._clients


def test_run_omada_health_tick_records_checks_with_no_notification_when_all_up(db_path):
    notified = []
    client = FakeOmadaClient(devices=[{"mac": "AA", "name": "front AP", "status": 1, "type": "ap"}])

    outcomes = scheduler.run_omada_health_tick(
        db_path, client, lambda chat_id, text: notified.append((chat_id, text)), "111")

    assert outcomes == []
    assert notified == []
    assert omada_health.feed_status(db_path)["devices_online"] == 1


def test_run_omada_health_tick_notifies_on_a_fresh_device_outage(db_path):
    notified = []
    client = FakeOmadaClient(devices=[{"mac": "AA", "name": "front AP", "status": 0, "type": "ap"}])

    outcomes = scheduler.run_omada_health_tick(
        db_path, client, lambda chat_id, text: notified.append((chat_id, text)), "111")

    assert len(outcomes) == 1
    assert outcomes[0]["transition"] == "down"
    assert outcomes[0]["told"] is True
    assert len(notified) == 1
    assert notified[0][0] == "111"
    assert "front AP" in notified[0][1]
    assert "offline" in notified[0][1]


def test_run_omada_health_tick_reports_device_recovery(db_path):
    notified = []
    down = FakeOmadaClient(devices=[{"mac": "AA", "name": "front AP", "status": 0, "type": "ap"}])
    scheduler.run_omada_health_tick(db_path, down, lambda c, t: notified.append((c, t)), "111")
    notified.clear()

    up = FakeOmadaClient(devices=[{"mac": "AA", "name": "front AP", "status": 1, "type": "ap"}])
    outcomes = scheduler.run_omada_health_tick(db_path, up, lambda c, t: notified.append((c, t)), "111")

    assert outcomes[0]["transition"] == "recovered"
    assert "back online" in notified[0][1]


def test_run_omada_health_tick_notifies_on_a_brand_new_client(db_path):
    notified = []
    client = FakeOmadaClient(clients=[{"mac": "11:22", "name": "iPad", "ip": "192.168.0.50"}])

    outcomes = scheduler.run_omada_health_tick(
        db_path, client, lambda chat_id, text: notified.append((chat_id, text)), "111")

    assert len(outcomes) == 1
    assert outcomes[0]["transition"] == "new_client"
    assert len(notified) == 1
    assert "iPad" in notified[0][1]
    assert "new device" in notified[0][1].lower()


def test_run_omada_health_tick_does_not_re_notify_an_already_known_client(db_path):
    notified = []
    client = FakeOmadaClient(clients=[{"mac": "11:22", "name": "iPad", "ip": "192.168.0.50"}])
    scheduler.run_omada_health_tick(db_path, client, lambda c, t: notified.append((c, t)), "111")
    notified.clear()

    outcomes = scheduler.run_omada_health_tick(db_path, client, lambda c, t: notified.append((c, t)), "111")

    assert outcomes == []
    assert notified == []


def test_run_omada_health_tick_never_calls_notify_without_an_owner_chat_id(db_path):
    client = FakeOmadaClient(devices=[{"mac": "AA", "name": "front AP", "status": 0, "type": "ap"}])
    scheduler.run_omada_health_tick(
        db_path, client, lambda chat_id, text: pytest.fail("should not notify with no owner"), None)
