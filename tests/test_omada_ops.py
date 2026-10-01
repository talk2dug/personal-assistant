"""omada_ops: the Omada controller as an ops-plan host."""
import json

import pytest

from assistant.core import business_db, ops_plans, omada_ops


class FakeController:
    """Just enough of OmadaClient: devices, radio-config GET/PATCH, live radios, band steering."""

    def __init__(self):
        self.radio = {"AA": {"radioSetting2g": {"radioEnable": True, "channel": "0", "channelWidth": "4",
                                                 "txPower": 23, "txPowerLevel": 4},
                             "radioSetting5g": {"radioEnable": True, "channel": "0", "channelWidth": "6",
                                                "txPower": 22, "txPowerLevel": 4}}}
        self.steering = 1
        self.patches = []

    def list_devices(self):
        return [{"mac": "AA", "name": "front AP", "type": "ap", "model": "EAP225-Outdoor"},
                {"mac": "RR", "name": "router", "type": "gateway"}]

    def _site_path(self, suffix):
        return suffix

    def _request(self, method, path, json=None):
        if path == "band-steering":
            if method == "PATCH":
                self.steering = json["bandSteeringForMultiBand"]["mode"]
            return {"bandSteeringForMultiBand": {"mode": self.steering}}
        mac = path.split("/")[1]
        if path.endswith("radio-config"):
            if method == "PATCH":
                self.patches.append(json)
                for key, fields in json.items():
                    self.radio[mac][key].update(fields)
            return self.radio[mac]
        if path.endswith("radios"):
            level = self.radio[mac]["radioSetting2g"]["txPowerLevel"]
            return {"wp2g": {"actualChannel": "11  / 2462MHz", "txPower": {1: 17, 4: 23}.get(level, 20)},
                    "wp5g": {"actualChannel": "36  / 5180MHz", "txPower": 22}}
        raise AssertionError(path)


def cmd(**kw):
    return json.dumps(kw)


@pytest.mark.parametrize("bad, why", [
    ("not json", "JSON"),
    (cmd(action="reboot"), "action must be"),
    (cmd(action="set_radio", ap="front AP", band="2g", set={"radioEnable": False}), "not writable"),
    (cmd(action="set_radio", ap="front AP", band="6g", set={"txPowerLevel": 1}), "band"),
    (cmd(action="set_radio", ap="front AP", band="2g", set={"txPowerLevel": 9}), "0-4"),
    (cmd(action="set_radio", ap="front AP", band="2g", set={"txPower": 99}), "dBm"),
    (cmd(action="set_band_steering", mode=7), "mode"),
    (cmd(action="check_radio", ap="front AP", band="2g"), "expect"),
])
def test_parse_refuses_anything_off_the_list(bad, why):
    with pytest.raises(omada_ops.OmadaActionError, match=why):
        omada_ops.parse(bad)


def test_set_check_and_roll_back_a_radio():
    ctl = FakeController()
    ops = omada_ops.OmadaOps(ctl)
    r = ops.run(cmd(action="set_radio", ap="front AP", band="2g", set={"txPowerLevel": 1}))
    assert r["ok"] and "4 -> 1" in r["output"]
    assert ctl.patches == [{"radioSetting2g": {"txPowerLevel": 1}}]   # only the field asked for
    assert ops.run(cmd(action="check_radio", ap="front AP", band="2g", expect={"txPowerLevel": 1},
                       expect_live={"txPower_max": 20}))["ok"]
    failed = ops.run(cmd(action="check_radio", ap="front AP", band="2g", expect={"txPowerLevel": 2}))
    assert not failed["ok"] and "expected 2" in failed["output"]
    assert ops.run(cmd(action="set_radio", ap="front ap", band="2g", set={"txPowerLevel": 4}))["ok"]
    assert ctl.radio["AA"]["radioSetting2g"]["txPowerLevel"] == 4


def test_an_unknown_ap_or_controller_error_fails_the_step_without_raising():
    ops = omada_ops.OmadaOps(FakeController())
    r = ops.run(cmd(action="set_radio", ap="garage AP", band="2g", set={"txPowerLevel": 1}))
    assert not r["ok"] and "no AP called" in r["output"]


def test_band_steering():
    ctl = FakeController()
    ops = omada_ops.OmadaOps(ctl)
    assert ops.run(cmd(action="check_band_steering", mode=1))["ok"]
    assert ops.run(cmd(action="set_band_steering", mode=2))["ok"]
    assert ctl.steering == 2


def test_status_shows_config_and_actual_for_aps_only():
    st = omada_ops.OmadaOps(FakeController()).status()
    assert [a["name"] for a in st["aps"]] == ["front AP"]
    assert st["aps"][0]["actual"]["2g"]["txPower"] == 23


class FakeSSH:
    def __init__(self):
        self.ran = []

    def list_hosts(self):
        return ["touch2"]

    def describe_hosts(self):
        return [{"name": "touch2"}]

    def run_command(self, host, command, timeout=120):
        self.ran.append((host, command))
        return {"ok": True, "exit_code": 0, "output": "ran"}


def test_router_sends_omada_to_the_controller_and_everything_else_to_ssh():
    ssh, ctl = FakeSSH(), FakeController()
    router = omada_ops.OpsRouter(ssh, ctl)
    assert router.list_hosts() == ["touch2", "omada"]
    assert "omada" in [h["name"] for h in router.describe_hosts()]
    router.run_command("touch2", "uptime")
    router.run_command("omada", cmd(action="set_band_steering", mode=0))
    assert ssh.ran == [("touch2", "uptime")] and ctl.steering == 0


def test_a_failed_omada_check_rolls_the_plan_back(tmp_path):
    db = str(tmp_path / "j.db")
    ops_plans.init_ops_plans_db(db)
    ctl = FakeController()
    plan = ops_plans.create_plan(db, 1, "front AP 2.4 to medium", [
        {"phase": "change", "host": "omada",
         "command": cmd(action="set_radio", ap="front AP", band="2g", set={"txPowerLevel": 1})},
        {"phase": "test", "host": "omada",   # deliberately wrong expectation
         "command": cmd(action="check_radio", ap="front AP", band="2g", expect={"txPowerLevel": 2})},
        {"phase": "rollback", "host": "omada",
         "command": cmd(action="set_radio", ap="front AP", band="2g", set={"txPowerLevel": 4})},
    ])
    out = ops_plans.run_plan(db, plan, omada_ops.OpsRouter(FakeSSH(), ctl))
    assert out["status"] == "failed"
    assert ctl.radio["AA"]["radioSetting2g"]["txPowerLevel"] == 4   # put back


def test_propose_refuses_a_malformed_omada_step(tmp_path):
    from assistant.core.business_tools import BusinessClient
    db = str(tmp_path / "j.db")
    business_db.init_business_db(db)
    ops_plans.init_ops_plans_db(db)
    client = BusinessClient(db, 1, ssh_ops=omada_ops.OpsRouter(FakeSSH(), FakeController()))
    r = client.call_tool("propose_ops_plan", {"summary": "x", "steps": [
        {"phase": "change", "host": "omada",
         "command": cmd(action="set_radio", ap="front AP", band="2g", set={"radioEnable": False})},
        {"phase": "rollback", "host": "omada", "command": cmd(action="status")}]})
    assert not r["ok"] and "not writable" in r["error"]
    st = client.call_tool("omada_radio_status", {})
    assert st["ok"] and st["band_steering"]["bandSteeringForMultiBand"]["mode"] == 1


def test_check_radio_waits_for_the_ap_to_apply_a_change(monkeypatch):
    """The controller accepts at once; the radio follows seconds later."""
    monkeypatch.setattr(omada_ops, "LIVE_POLL_SEC", 0)
    ctl = FakeController()
    ops = omada_ops.OmadaOps(ctl)
    ops.run(cmd(action="set_radio", ap="front AP", band="2g", set={"txPowerLevel": 1}))
    reads = {"n": 0}
    real = ctl._request

    def lagging(method, path, json=None):
        out = real(method, path, json)
        if path.endswith("radios"):
            reads["n"] += 1
            if reads["n"] < 3:                       # first two reads: not applied yet
                out = {**out, "wp2g": {**out["wp2g"], "txPower": 23}}
        return out
    ctl._request = lagging
    r = ops.run(cmd(action="check_radio", ap="front AP", band="2g", expect_live={"txPower_max": 20}))
    assert r["ok"] and reads["n"] == 3


def test_check_radio_still_fails_when_the_radio_never_follows(monkeypatch):
    monkeypatch.setattr(omada_ops, "LIVE_POLL_SEC", 0)
    monkeypatch.setattr(omada_ops, "LIVE_WAIT_SEC", 0)
    ops = omada_ops.OmadaOps(FakeController())
    r = ops.run(cmd(action="check_radio", ap="front AP", band="2g", expect_live={"txPower_max": 20}))
    assert not r["ok"] and "above 20" in r["output"]


def test_a_setting_the_controller_silently_ignores_fails_the_step():
    ctl = FakeController()
    real = ctl._request

    def ignoring(method, path, json=None):
        if method == "PATCH" and path.endswith("radio-config"):
            return ctl.radio[path.split("/")[1]]          # accepted, nothing changed
        return real(method, path, json)
    ctl._request = ignoring
    r = omada_ops.OmadaOps(ctl).run(cmd(action="set_radio", ap="front AP", band="5g", set={"channel": "149"}))
    assert not r["ok"] and "did not apply channel" in r["output"]
