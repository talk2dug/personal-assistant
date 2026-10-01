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


class DeadController:
    def list_devices(self):
        raise TimeoutError("timed out")

    def list_clients(self):
        raise TimeoutError("timed out")


def test_an_unreachable_controller_is_reported_once_and_its_return_too(db_path):
    """2026-09-30: the controller was unplugged ~9h and nobody was told."""
    notified = []
    say = lambda chat_id, text: notified.append(text)
    first = scheduler.run_omada_health_tick(db_path, DeadController(), say, "111")
    assert first and first[0]["transition"] == "down"
    assert any("controller isn't answering" in t for t in notified)
    notified.clear()
    assert scheduler.run_omada_health_tick(db_path, DeadController(), say, "111") == []
    assert notified == []
    back = scheduler.run_omada_health_tick(
        db_path, FakeOmadaClient(devices=[{"mac": "AA", "name": "front AP", "status": 1, "type": "ap"}]),
        say, "111")
    assert {"name": "omada controller", "transition": "recovered", "told": True} in back
    # and an unreachable controller never marked the devices themselves offline
    assert omada_health.feed_status(db_path)["devices_online"] == 1


def test_a_device_heard_from_recently_is_online_whatever_status_says(db_path):
    import time
    now_ms = time.time() * 1000
    client = FakeOmadaClient(devices=[
        {"mac": "AA", "name": "main AP", "status": 0, "type": "ap", "lastSeen": now_ms - 5000},
        {"mac": "BB", "name": "front AP", "status": 0, "type": "ap", "lastSeen": now_ms - 3_600_000},
    ])
    results = {r["name"]: r["reachable"] for r in omada_health.check_devices(client)}
    assert results == {"main AP": True, "front AP": False}


def test_the_tick_stores_a_snapshot_the_network_feed_renders(db_path, monkeypatch):
    monkeypatch.setattr(omada_health, "ping_sweep", lambda targets: [
        {"name": n, "ip": ip, "loss_pct": 0.0 if n != "touch2" else 20.0, "avg_ms": 3.0, "max_ms": 9.0}
        for n, ip in targets.items()])
    client = FakeOmadaClient(
        devices=[{"mac": "AA", "name": "main AP", "status": 1, "type": "ap", "ip": "192.168.0.115"}],
        clients=[{"mac": "C1", "name": "touch2", "ip": "192.168.0.136", "wireless": True,
                  "apName": "main AP", "radioId": 0, "rssi": -78, "snr": 15, "channel": 6}])
    scheduler.run_omada_health_tick(db_path, client, lambda *a: None, "111",
                                    ping_targets={"touch2": "192.168.0.136"})
    snap = omada_health.latest_snapshot(db_path)
    assert {p["name"] for p in snap["ping"]} == {"touch2", "main AP"}
    text = omada_health.render_snapshot(snap, omada_health.controller_state(db_path))
    assert "touch2 192.168.0.136: loss 20.0%" in text and "<-- problem" in text
    assert "rssi -78" in text and "2.4GHz" in text


def test_the_network_feed_reaches_an_employee_briefing(db_path, monkeypatch):
    from assistant.core import staff
    monkeypatch.setattr(omada_health, "ping_sweep", lambda targets: [])
    scheduler.run_omada_health_tick(
        db_path, FakeOmadaClient(devices=[{"mac": "AA", "name": "main AP", "status": 1, "type": "ap"}]),
        lambda *a: None, "111")
    out = staff.build_feed_briefing(db_path, "network")
    assert "NETWORK (Omada controller + ping sweep)" in out and "main AP" in out
