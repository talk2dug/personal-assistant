"""Omada controller settings as ops-plan steps, so the systems engineer can change the Wi-Fi.

Until 2026-10-01 every Omada-side fix the engineer found -- AP transmit power, channels,
band steering -- ended as "here are exact values for Jack to click in by hand", because
ops plans could only run shell commands on SSH hosts and the controller is not one.

This adds the controller as one more plan host, named `omada`. A step whose host is
`omada` carries a small JSON action instead of a shell command, and OpsRouter sends it
to the controller's Open API instead of SSH. Everything else about ops plans is
unchanged and is the point: one owner approval for the whole plan, change -> test ->
verify in order, and on the first failure every rollback step runs. A plan that turns
the front AP's power down therefore also carries the step that reads it back, and the
step that puts the old value back if anything goes wrong.

Deliberately narrow. The only writable fields are channel, channel width and transmit
power on an AP radio, and the site's band-steering mode. Turning a radio OFF, SSIDs,
passwords, VLANs, the router and firewall are not reachable from here at all: a plan
that could disable Wi-Fi would also be a plan that cuts off the hosts it must verify.

Field meanings (Omada Open API, controller 5.14): txPowerLevel 0 Low, 1 Medium, 2 High,
3 Custom (uses txPower, dBm), 4 Auto. channel "0" is Auto. Band steering mode 0
Disabled, 1 Prefer 5GHz, 2 Balance. Read-back of the live radio (actual dBm, actual
channel) is what proves a change took, so `check_radio` can assert either.
"""
import json
import logging
import time

logger = logging.getLogger(__name__)

OMADA_HOST = "omada"
BANDS = {"2g": "radioSetting2g", "5g": "radioSetting5g"}
LIVE_BANDS = {"2g": "wp2g", "5g": "wp5g"}
WRITABLE = {"channel", "channelWidth", "txPowerLevel", "txPower"}
ACTIONS = {"set_radio", "check_radio", "set_band_steering", "check_band_steering", "status"}

ACTION_HELP = (
    'Steps on host "omada" take a JSON object as their command, one of: '
    '{"action":"set_radio","ap":"front AP","band":"2g","set":{"txPowerLevel":1}} '
    '(writable: channel, channelWidth, txPowerLevel, txPower; txPowerLevel 0 Low/1 Medium/'
    '2 High/3 Custom/4 Auto; channel "0" = Auto) | '
    '{"action":"check_radio","ap":"front AP","band":"2g","expect":{"txPowerLevel":1},'
    '"expect_live":{"txPower_max":20}} (fails the step if any value differs; expect_live checks '
    'the radio\'s actual state: txPower_max/txPower_min in dBm, channel) | '
    '{"action":"set_band_steering","mode":1} (0 Disabled, 1 Prefer 5GHz, 2 Balance) | '
    '{"action":"check_band_steering","mode":1} | {"action":"status"}. '
    "Radios cannot be turned off and SSIDs/router/firewall are not reachable. Read current "
    "values with omada_radio_status first and use them in your rollback steps.")


class OmadaActionError(ValueError):
    pass


def parse(command: str) -> dict:
    """Validate an `omada` step's command. Raises OmadaActionError with a message an
    employee can act on -- this runs when the plan is proposed, not only when it runs."""
    try:
        action = json.loads(command)
    except (TypeError, json.JSONDecodeError) as e:
        raise OmadaActionError(f"an omada step's command must be a JSON object ({e})")
    if not isinstance(action, dict) or action.get("action") not in ACTIONS:
        raise OmadaActionError(f"omada action must be one of {', '.join(sorted(ACTIONS))}")
    kind = action["action"]
    if kind in ("set_radio", "check_radio"):
        if action.get("band") not in BANDS:
            raise OmadaActionError('band must be "2g" or "5g"')
        if not action.get("ap"):
            raise OmadaActionError("ap (the AP's name or MAC) is required")
    if kind == "set_radio":
        fields = action.get("set") or {}
        if not fields:
            raise OmadaActionError("set_radio needs a non-empty 'set'")
        bad = set(fields) - WRITABLE
        if bad:
            raise OmadaActionError(f"not writable from a plan: {', '.join(sorted(bad))} "
                                   f"(allowed: {', '.join(sorted(WRITABLE))})")
        if "txPowerLevel" in fields and fields["txPowerLevel"] not in (0, 1, 2, 3, 4):
            raise OmadaActionError("txPowerLevel must be 0-4")
        if "txPower" in fields and not (isinstance(fields["txPower"], int) and 1 <= fields["txPower"] <= 30):
            raise OmadaActionError("txPower must be an integer dBm between 1 and 30")
    if kind in ("set_band_steering", "check_band_steering") and action.get("mode") not in (0, 1, 2):
        raise OmadaActionError("band steering mode must be 0, 1 or 2")
    if kind == "check_radio" and not (action.get("expect") or action.get("expect_live")):
        raise OmadaActionError("check_radio needs 'expect' and/or 'expect_live'")
    return action


class OmadaOps:
    def __init__(self, client):
        self.client = client

    def _ap(self, ref: str) -> dict:
        aps = [d for d in self.client.list_devices() if d.get("type") == "ap"]
        for d in aps:
            if ref in (d.get("name"), d.get("mac")) or ref.lower() == (d.get("name") or "").lower():
                return d
        raise OmadaActionError(f"no AP called {ref!r}; APs: {', '.join(d.get('name') for d in aps)}")

    def radio_config(self, mac: str) -> dict:
        return self.client._request("GET", self.client._site_path(f"aps/{mac}/radio-config"))

    def live(self, mac: str) -> dict:
        return self.client._request("GET", self.client._site_path(f"aps/{mac}/radios"))

    def band_steering(self) -> dict:
        return self.client._request("GET", self.client._site_path("band-steering"))

    def status(self) -> dict:
        """Every AP's configured and actual radio settings, plus band steering."""
        out = {"band_steering": self.band_steering(), "aps": []}
        for d in self.client.list_devices():
            if d.get("type") != "ap":
                continue
            live = self.live(d["mac"])
            out["aps"].append({
                "name": d.get("name"), "mac": d.get("mac"), "model": d.get("model"),
                "config": self.radio_config(d["mac"]),
                "actual": {band: {k: v for k, v in (live.get(key) or {}).items()
                                  if k in ("actualChannel", "txPower", "bandWidth", "txUtil",
                                           "rxUtil", "interUtil", "rdMode")}
                           for band, key in LIVE_BANDS.items()},
            })
        return out

    def run(self, command: str) -> dict:
        """Execute one validated action. Same result shape as SSHOpsClient.run_command."""
        try:
            a = parse(command)
            kind = a["action"]
            if kind == "status":
                return _ok(json.dumps(self.status(), indent=1))
            if kind in ("set_band_steering", "check_band_steering"):
                current = self.band_steering()
                mode = (current.get("bandSteeringForMultiBand") or {}).get("mode")
                if kind == "check_band_steering":
                    return _result(mode == a["mode"], f"band steering mode is {mode}, expected {a['mode']}")
                self.client._request("PATCH", self.client._site_path("band-steering"),
                                     json={"bandSteeringForMultiBand": {"mode": a["mode"]}})
                return _ok(f"band steering mode {mode} -> {a['mode']}")
            ap = self._ap(a["ap"])
            key = BANDS[a["band"]]
            if kind == "set_radio":
                before = dict(self.radio_config(ap["mac"]).get(key) or {})
                self.client._request("PATCH", self.client._site_path(f"aps/{ap['mac']}/radio-config"),
                                     json={key: dict(a["set"])})
                after = (self.radio_config(ap["mac"]).get(key) or {})
                changed = {k: f"{before.get(k)} -> {after.get(k)}" for k in a["set"]}
                # The controller can accept a PATCH and keep the old value -- it did for a
                # 5GHz channel on 2026-10-01 and this step reported success for a no-op.
                ignored = [k for k, want in a["set"].items() if str(after.get(k)) != str(want)]
                if ignored:
                    return _result(False, f"{ap['name']} {a['band']}: the controller accepted the "
                                          f"request but did not apply {', '.join(ignored)}: {changed}")
                return _ok(f"{ap['name']} {a['band']}: {changed}")
            # check_radio
            problems = []
            cfg = self.radio_config(ap["mac"]).get(key) or {}
            for k, want in (a.get("expect") or {}).items():
                if str(cfg.get(k)) != str(want):
                    problems.append(f"{k} is {cfg.get(k)}, expected {want}")
            # The controller accepts a change at once but the AP applies it some seconds
            # later; reading the live radio straight away failed a correct change on
            # 2026-10-01 (config Medium/15dBm, radio still 23dBm). Re-read until the radio
            # agrees or the wait runs out.
            el = a.get("expect_live") or {}
            deadline = time.monotonic() + (LIVE_WAIT_SEC if el else 0)
            while True:
                live = self.live(ap["mac"]).get(LIVE_BANDS[a["band"]]) or {}
                live_problems = _live_problems(live, el)
                if not live_problems or time.monotonic() >= deadline:
                    break
                time.sleep(LIVE_POLL_SEC)
            problems += live_problems
            power = live.get("txPower")
            detail = f"{ap['name']} {a['band']}: config {cfg}; actual txPower {power} dBm, channel {live.get('actualChannel')}"
            return _result(not problems, "; ".join(problems) + (" | " if problems else "") + detail)
        except Exception as e:  # noqa: BLE001 -- a failed step is a result, not a crash
            return {"ok": False, "exit_code": 1, "output": f"{type(e).__name__}: {e}"}


LIVE_WAIT_SEC = 90
LIVE_POLL_SEC = 5


def _live_problems(live: dict, el: dict) -> list:
    problems = []
    power = live.get("txPower")
    if "txPower_max" in el and (power is None or power > el["txPower_max"]):
        problems.append(f"actual txPower {power} dBm is above {el['txPower_max']}")
    if "txPower_min" in el and (power is None or power < el["txPower_min"]):
        problems.append(f"actual txPower {power} dBm is below {el['txPower_min']}")
    if "channel" in el and not str(live.get("actualChannel", "")).strip().startswith(str(el["channel"])):
        problems.append(f"actual channel {live.get('actualChannel')}, expected {el['channel']}")
    return problems


def _ok(output: str) -> dict:
    return {"ok": True, "exit_code": 0, "output": output}


def _result(ok: bool, output: str) -> dict:
    return {"ok": ok, "exit_code": 0 if ok else 1, "output": output}


class OpsRouter:
    """Stands in for SSHOpsClient wherever ops plans are proposed or run: SSH hosts go to
    SSH, the `omada` host goes to the controller. Anything else on SSHOpsClient is
    passed through untouched."""

    def __init__(self, ssh, omada_client):
        self.ssh = ssh
        self.omada = OmadaOps(omada_client)

    def list_hosts(self) -> list:
        return (list(self.ssh.list_hosts()) if self.ssh is not None else []) + [OMADA_HOST]

    def describe_hosts(self) -> list:
        hosts = list(self.ssh.describe_hosts()) if self.ssh is not None else []
        return hosts + [{"name": OMADA_HOST, "purpose": "The Omada Wi-Fi controller (OC200). " + ACTION_HELP}]

    def run_command(self, host: str, command: str, *args, **kwargs) -> dict:
        if host == OMADA_HOST:
            return self.omada.run(command)
        if self.ssh is None:
            return {"ok": False, "exit_code": 1, "output": f"no SSH hosts configured for {host}"}
        return self.ssh.run_command(host, command, *args, **kwargs)

    def __getattr__(self, name):
        if self.ssh is None:
            raise AttributeError(name)
        return getattr(self.ssh, name)
