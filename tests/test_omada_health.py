"""Covers omada_health.py: the recorded-over-time half of network monitoring, mirroring
test_host_health.py's exact style. Devices are a small fixed set (host_health-shaped);
clients are an open set, so check_and_record_clients gets its own "brand new client"
coverage host_health has no equivalent of.
"""
import pytest

from assistant.core import omada_health


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "jarvis.db")
    omada_health.init_omada_health_db(path)
    return path


class FakeOmadaClient:
    def __init__(self, devices=None, clients=None):
        self._devices = devices or []
        self._clients = clients or []

    def list_devices(self):
        return self._devices

    def list_clients(self):
        return self._clients


def device(mac="AA:BB", name="front AP", status=1, kind="ap", model="EAP225", ip="192.168.0.119"):
    return {"mac": mac, "name": name, "status": status, "type": kind, "model": model, "ip": ip}


def test_check_devices_maps_status_1_to_reachable():
    client = FakeOmadaClient(devices=[device(status=1), device(mac="CC:DD", status=0)])
    results = omada_health.check_devices(client)
    assert results[0]["reachable"] is True
    assert results[1]["reachable"] is False


def test_check_devices_falls_back_to_mac_when_name_missing():
    client = FakeOmadaClient(devices=[{"mac": "AA:BB", "status": 1}])
    results = omada_health.check_devices(client)
    assert results[0]["name"] == "AA:BB"
    assert results[0]["kind"] == "unknown"


def test_record_device_check_first_sighting_of_a_down_device_is_a_down_transition(db_path):
    changed = omada_health.record_device_check(
        db_path, [{"mac": "AA:BB", "name": "front AP", "kind": "ap", "model": None, "ip": None, "reachable": False}])
    assert len(changed) == 1
    assert changed[0]["transition"] == "down"
    assert changed[0]["consecutive_failures"] == 1


def test_record_device_check_first_sighting_of_an_up_device_is_not_reported(db_path):
    changed = omada_health.record_device_check(
        db_path, [{"mac": "AA:BB", "name": "front AP", "kind": "ap", "model": None, "ip": None, "reachable": True}])
    assert changed == []


def test_record_device_check_reports_recovery(db_path):
    down = {"mac": "AA:BB", "name": "front AP", "kind": "ap", "model": None, "ip": None, "reachable": False}
    up = {**down, "reachable": True}
    omada_health.record_device_check(db_path, [down])
    changed = omada_health.record_device_check(db_path, [up])
    assert changed[0]["transition"] == "recovered"
    assert changed[0]["consecutive_failures"] == 0


def test_record_device_check_reports_still_down_with_growing_failure_count(db_path):
    down = {"mac": "AA:BB", "name": "front AP", "kind": "ap", "model": None, "ip": None, "reachable": False}
    omada_health.record_device_check(db_path, [down])
    changed = omada_health.record_device_check(db_path, [down])
    assert changed[0]["transition"] == "still_down"
    assert changed[0]["consecutive_failures"] == 2


def test_record_device_check_unchanged_up_device_is_not_reported(db_path):
    up = {"mac": "AA:BB", "name": "front AP", "kind": "ap", "model": None, "ip": None, "reachable": True}
    omada_health.record_device_check(db_path, [up])
    changed = omada_health.record_device_check(db_path, [up])
    assert changed == []


def test_check_and_record_clients_reports_a_brand_new_client(db_path):
    client = FakeOmadaClient(clients=[{"mac": "11:22", "name": "iPad", "ip": "192.168.0.50"}])
    new = omada_health.check_and_record_clients(db_path, client)
    assert new == [{"mac": "11:22", "name": "iPad", "ip": "192.168.0.50"}]


def test_check_and_record_clients_does_not_re_report_an_already_known_client(db_path):
    client = FakeOmadaClient(clients=[{"mac": "11:22", "name": "iPad", "ip": "192.168.0.50"}])
    omada_health.check_and_record_clients(db_path, client)
    new = omada_health.check_and_record_clients(db_path, client)
    assert new == []


def test_check_and_record_clients_marks_a_disconnected_client_offline(db_path):
    client = FakeOmadaClient(clients=[{"mac": "11:22", "name": "iPad", "ip": "192.168.0.50"}])
    omada_health.check_and_record_clients(db_path, client)

    empty_client = FakeOmadaClient(clients=[])
    omada_health.check_and_record_clients(db_path, empty_client)

    feed = omada_health.feed_status(db_path)
    assert feed["clients_online"] == 0


def test_check_and_record_clients_uses_hostname_when_name_missing(db_path):
    client = FakeOmadaClient(clients=[{"mac": "11:22", "hostName": "iPad-2", "ip": "192.168.0.50"}])
    new = omada_health.check_and_record_clients(db_path, client)
    assert new[0]["name"] == "iPad-2"


def test_check_and_record_clients_skips_entries_with_no_mac(db_path):
    client = FakeOmadaClient(clients=[{"name": "ghost", "ip": "192.168.0.99"}])
    new = omada_health.check_and_record_clients(db_path, client)
    assert new == []


def test_feed_status_reports_device_and_client_counts(db_path):
    omada_health.record_device_check(
        db_path, [{"mac": "AA:BB", "name": "front AP", "kind": "ap", "model": None, "ip": None, "reachable": True},
                  {"mac": "CC:DD", "name": "router", "kind": "gateway", "model": None, "ip": None, "reachable": False}])
    omada_health.check_and_record_clients(db_path, FakeOmadaClient(
        clients=[{"mac": "11:22", "name": "iPad", "ip": "192.168.0.50"}]))

    feed = omada_health.feed_status(db_path)
    assert feed["devices_total"] == 2
    assert feed["devices_online"] == 1
    assert feed["clients_online"] == 1
