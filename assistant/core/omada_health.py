"""Live network-layer health, mirroring host_health.py's shape exactly: a plain measurement
of what the Omada controller reports right now, recorded over time so "just went offline"
and "still offline since this morning" are different facts. Pure measurement -- this module
never fixes or notifies anyone; see omada_tools.py for the one gated control action and
scheduler.py's omada_health_tick for the loop that ties measurement and notification
together (the same shape as host_health_tick).

Devices (APs/switches/gateways) are a small, known, fixed set -- reported the same way
host_health.py reports hosts. Clients are different: an OPEN set that grows as new devices
join the network, and a brand-new never-seen-before client is itself worth a notice (the
same "own vs. unknown-new" idea docs/radio-awareness-design.md already uses for RF-baseline
vehicle classification, applied here to network clients instead).
"""
import json
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS omada_devices (
    mac TEXT PRIMARY KEY,
    name TEXT,
    kind TEXT NOT NULL,
    model TEXT,
    ip TEXT,
    online INTEGER NOT NULL,
    last_checked_at TEXT NOT NULL,
    last_online_at TEXT,
    last_offline_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

-- One row per tick: the whole network as the controller and a ping sweep saw it. The
-- systems engineer's `network` feed reads the newest; older rows are kept briefly so a
-- trend ("touch2 has been weak all evening") is visible, then pruned.
CREATE TABLE IF NOT EXISTS network_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    body TEXT NOT NULL
);

-- The controller itself. Its own outage used to be invisible: on 2026-09-30 it lost power
-- for ~9 hours and the only trace was a ConnectTimeout traceback every 15 minutes in the
-- core log. One row, id=1.
CREATE TABLE IF NOT EXISTS omada_controller_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    reachable INTEGER NOT NULL,
    since TEXT NOT NULL,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS omada_clients (
    mac TEXT PRIMARY KEY,
    name TEXT,
    ip TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    online INTEGER NOT NULL DEFAULT 1
);
"""


def init_omada_health_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# TP-Link's own status code: 1 means online, everything else (0, or absent) does not.
DEVICE_STATUS_ONLINE = 1


def check_devices(client) -> list[dict]:
    """Every AP/switch/gateway, translated into host_health.py's plain reachable-shaped
    dict so the two feel identical to a caller."""
    devices = client.list_devices()
    return [{
        "mac": d["mac"], "name": d.get("name") or d["mac"], "kind": d.get("type", "unknown"),
        "model": d.get("model"), "ip": d.get("ip"),
        "reachable": _device_online(d),
    } for d in devices]


# A device the controller heard from this recently is up, whatever `status` says. Right
# after the controller rebooted on 2026-09-30, all three devices reported status 0 for
# many minutes while heartbeating every few seconds and the site overview counted them
# connected -- trusting `status` alone would have announced the whole network offline.
LAST_SEEN_FRESH_SEC = 180


def _device_online(d: dict, now_ms: float | None = None) -> bool:
    if d.get("status") == DEVICE_STATUS_ONLINE:
        return True
    seen = d.get("lastSeen")
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    return bool(seen) and (now_ms - seen) / 1000 <= LAST_SEEN_FRESH_SEC


def record_device_check(db_path: str, results: list[dict], now: str | None = None) -> list[dict]:
    """Same contract as host_health.record_check: upserts every device, returns only what
    changed (down, recovered, still down) so a 15-minute tick isn't re-deciding on every
    unchanged device."""
    now = now or _now()
    changed = []
    with closing(_connect(db_path)) as conn:
        for d in results:
            prior = conn.execute("SELECT * FROM omada_devices WHERE mac = ?", (d["mac"],)).fetchone()
            reachable = bool(d["reachable"])
            was_reachable = bool(prior["online"]) if prior else None

            if reachable:
                consecutive = 0
                last_online_at = now
                last_offline_at = prior["last_offline_at"] if prior else None
            else:
                consecutive = (prior["consecutive_failures"] if prior else 0) + 1
                last_online_at = prior["last_online_at"] if prior else None
                last_offline_at = now if was_reachable is not False else (prior["last_offline_at"] if prior else now)

            conn.execute(
                """INSERT INTO omada_devices (mac, name, kind, model, ip, online, last_checked_at,
                                              last_online_at, last_offline_at, consecutive_failures)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mac) DO UPDATE SET
                       name = excluded.name, kind = excluded.kind, model = excluded.model,
                       ip = excluded.ip, online = excluded.online,
                       last_checked_at = excluded.last_checked_at,
                       last_online_at = excluded.last_online_at,
                       last_offline_at = excluded.last_offline_at,
                       consecutive_failures = excluded.consecutive_failures""",
                (d["mac"], d["name"], d["kind"], d.get("model"), d.get("ip"), int(reachable), now,
                 last_online_at, last_offline_at, consecutive))

            if was_reachable is True and not reachable:
                changed.append({**d, "transition": "down", "consecutive_failures": consecutive})
            elif was_reachable is False and reachable:
                changed.append({**d, "transition": "recovered", "consecutive_failures": 0})
            elif was_reachable is False and not reachable:
                changed.append({**d, "transition": "still_down", "consecutive_failures": consecutive})
            # A first-ever sighting of a down device is reported (unlike host_health.py's
            # hosts, which are pre-registered and expected) -- devices are also a small,
            # known set here, so seeing one for the first time already offline is worth a
            # notice too, not just silently recorded.
            elif was_reachable is None and not reachable:
                changed.append({**d, "transition": "down", "consecutive_failures": consecutive})
        conn.commit()
    return changed


def check_and_record_clients(db_path: str, client, now: str | None = None) -> list[dict]:
    """Clients are an open set -- this both records who's currently on the network AND
    returns any client seen for the very first time, which is the network-layer analog of
    host_health's device transitions: not 'did a known thing go down', but 'did something
    NEW just show up'."""
    now = now or _now()
    seen_macs = set()
    new_clients = []
    with closing(_connect(db_path)) as conn:
        for c in client.list_clients():
            mac = c.get("mac")
            if not mac:
                continue
            seen_macs.add(mac)
            existing = conn.execute("SELECT mac FROM omada_clients WHERE mac = ?", (mac,)).fetchone()
            conn.execute(
                """INSERT INTO omada_clients (mac, name, ip, first_seen_at, last_seen_at, online)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(mac) DO UPDATE SET
                       name = excluded.name, ip = excluded.ip, last_seen_at = excluded.last_seen_at,
                       online = 1""",
                (mac, c.get("name") or c.get("hostName"), c.get("ip"), now, now))
            if existing is None:
                new_clients.append({"mac": mac, "name": c.get("name") or c.get("hostName"), "ip": c.get("ip")})
        # Anyone not seen this pass is no longer on the network -- mark them offline rather
        # than deleting the row, so "first_seen"/history survives a client disconnecting.
        conn.execute("UPDATE omada_clients SET online = 0 WHERE online = 1 AND mac NOT IN ({})".format(
            ",".join("?" for _ in seen_macs) or "''"), list(seen_macs))
        conn.commit()
    return new_clients


def feed_status(db_path: str) -> dict:
    """Device/client counts, for the staff feed and chat tools -- mirrors
    host_health.all_hosts()'s role."""
    with closing(_connect(db_path)) as conn:
        devices = [dict(r) for r in conn.execute("SELECT * FROM omada_devices ORDER BY name")]
        online_clients = conn.execute("SELECT count(*) FROM omada_clients WHERE online = 1").fetchone()[0]
    return {
        "devices": devices,
        "devices_online": sum(1 for d in devices if d["online"]),
        "devices_total": len(devices),
        "clients_online": online_clients,
    }


# --- the controller itself ---------------------------------------------------------------

def record_controller(db_path: str, reachable: bool, error: str | None = None,
                      now: str | None = None) -> str | None:
    """Returns 'down' or 'recovered' when the controller's state changed, else None."""
    now = now or _now()
    with closing(_connect(db_path)) as conn:
        prior = conn.execute("SELECT * FROM omada_controller_state WHERE id = 1").fetchone()
        if prior is None:
            conn.execute("INSERT INTO omada_controller_state (id, reachable, since, last_error) "
                         "VALUES (1, ?, ?, ?)", (int(reachable), now, error))
            conn.commit()
            return None if reachable else "down"
        if bool(prior["reachable"]) == reachable:
            if error:
                conn.execute("UPDATE omada_controller_state SET last_error = ? WHERE id = 1", (error,))
                conn.commit()
            return None
        conn.execute("UPDATE omada_controller_state SET reachable = ?, since = ?, last_error = ? "
                     "WHERE id = 1", (int(reachable), now, error))
        conn.commit()
        return "recovered" if reachable else "down"


def controller_state(db_path: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM omada_controller_state WHERE id = 1").fetchone()
    return dict(row) if row else None


# --- the snapshot ------------------------------------------------------------------------

SNAPSHOT_KEEP = 192          # two days at the 15-minute cadence
PING_COUNT = 10


def ping(ip: str, count: int = PING_COUNT) -> dict:
    """Loss and latency to one address, using the OS ping (this runs on Windows)."""
    win = sys.platform.startswith("win")
    cmd = ["ping", "-n" if win else "-c", str(count), "-w" if win else "-W", "1000" if win else "1", ip]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=count * 2 + 5).stdout
    except Exception as e:  # noqa: BLE001
        return {"ip": ip, "loss_pct": None, "avg_ms": None, "max_ms": None, "error": type(e).__name__}
    times = [float(t) for t in re.findall(r"time[=<]([\d.]+)\s*ms", out)]
    loss = re.search(r"(\d+(?:\.\d+)?)% (?:packet )?loss", out)
    return {"ip": ip,
            "loss_pct": float(loss.group(1)) if loss else (0.0 if len(times) == count else None),
            "avg_ms": round(sum(times) / len(times), 1) if times else None,
            "max_ms": max(times) if times else None}


def ping_sweep(targets: dict) -> list:
    with ThreadPoolExecutor(max_workers=24) as ex:
        results = list(ex.map(lambda item: {"name": item[0], **ping(item[1])}, targets.items()))
    return sorted(results, key=lambda r: r["name"])


_BAND = {0: "2.4GHz", 1: "5GHz", 2: "5GHz-2", 3: "6GHz"}


def _client_row(c: dict) -> dict:
    return {
        "name": c.get("name") or c.get("hostName") or c.get("mac"),
        "mac": c.get("mac"), "ip": c.get("ip"),
        "wireless": bool(c.get("wireless")),
        "ap": c.get("apName"), "ssid": c.get("ssid"),
        "band": _BAND.get(c.get("radioId"), c.get("radioId")),
        "channel": c.get("channel"),
        "rssi": c.get("rssi"), "snr": c.get("snr"),
        "rx_rate": c.get("rxRate"), "tx_rate": c.get("txRate"),
        "uptime_s": c.get("uptime"),
    }


def take_snapshot(client, ping_targets: dict | None = None) -> dict:
    """The whole network right now. Raises if the controller can't be read -- the caller
    records that as the controller being down rather than as an empty network."""
    devices = client.list_devices()
    try:
        overview = client._request("GET", client._site_path("dashboard/overview-diagram"))
    except Exception:  # noqa: BLE001 -- overview is a nicety; devices/clients are the facts
        overview = None
    clients = [_client_row(c) for c in client.list_clients()]
    now_ms = time.time() * 1000
    dev_rows = [{
        "name": d.get("name") or d.get("mac"), "model": d.get("model"), "ip": d.get("ip"),
        "type": d.get("type"), "firmware": d.get("firmwareVersion"),
        "online": _device_online(d, now_ms),
        "last_seen_s": round((now_ms - d["lastSeen"]) / 1000) if d.get("lastSeen") else None,
    } for d in devices]
    targets = dict(ping_targets or {})
    for d in dev_rows:
        if d["ip"]:
            targets.setdefault(d["name"], d["ip"])
    return {"at": _now(), "overview": overview, "devices": dev_rows, "clients": clients,
            "ping": ping_sweep(targets) if targets else []}


def store_snapshot(db_path: str, snap: dict) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("INSERT INTO network_snapshots (at, body) VALUES (?, ?)",
                     (snap["at"], json.dumps(snap)))
        conn.execute("DELETE FROM network_snapshots WHERE id NOT IN "
                     "(SELECT id FROM network_snapshots ORDER BY id DESC LIMIT ?)", (SNAPSHOT_KEEP,))
        conn.commit()


def latest_snapshot(db_path: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT body FROM network_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row["body"]) if row else None


def _ping_is_bad(p: dict) -> bool:
    return p.get("avg_ms") is None or (p.get("loss_pct") or 0) > 0 or (p.get("avg_ms") or 0) > 30


def render_snapshot(snap: dict, controller: dict | None = None) -> str:
    """The `network` feed's text. Problems first, detail after."""
    lines = []
    if controller and not controller["reachable"]:
        lines.append(f"OMADA CONTROLLER UNREACHABLE since {controller['since'][:16]}Z "
                     f"({(controller.get('last_error') or '')[:100]}). Device/client data below "
                     f"is from the last good snapshot at {snap['at'][:16]}Z.")
    ov = snap.get("overview") or {}
    if ov:
        lines.append(f"SITE: gateways {ov.get('connectedGatewayNum')}/{ov.get('totalGatewayNum')} "
                     f"connected, switches {ov.get('connectedSwitchNum')}/{ov.get('totalSwitchNum')}, "
                     f"APs {ov.get('connectedApNum')}/{ov.get('totalApNum')} (isolated "
                     f"{ov.get('isolatedApNum')}), clients {ov.get('totalClientNum')} "
                     f"({ov.get('wirelessClientNum')} wireless)")
    lines.append(f"DEVICES (snapshot {snap['at'][:16]}Z):")
    for d in snap["devices"]:
        lines.append(f"  {'UP  ' if d['online'] else 'DOWN'} {d['name']} {d['model']} {d['ip']} "
                     f"fw {d['firmware']} (last heard {d['last_seen_s']}s ago)")
    bad = [p for p in snap["ping"] if _ping_is_bad(p)]
    lines.append(f"PING SWEEP ({PING_COUNT} pings each from jarvisbox, which is wired): "
                 f"{len(snap['ping']) - len(bad)} of {len(snap['ping'])} clean")
    for p in sorted(snap["ping"], key=lambda p: (not _ping_is_bad(p), p["name"])):
        flag = "  <-- problem" if _ping_is_bad(p) else ""
        lines.append(f"  {p['name']} {p['ip']}: loss {p.get('loss_pct')}%, avg {p.get('avg_ms')}ms, "
                     f"max {p.get('max_ms')}ms{flag}")
    wl = [c for c in snap["clients"] if c["wireless"]]
    if wl:
        per_ap: dict = {}
        for c in wl:
            per_ap.setdefault(f"{c['ap']} {c['band']}", []).append(c)
        lines.append("WIRELESS CLIENTS per AP/band: "
                     + ", ".join(f"{k}: {len(v)}" for k, v in sorted(per_ap.items())))
        lines.append("  weakest signal first (rssi dBm; below -70 is poor, below -80 unusable):")
        for c in sorted(wl, key=lambda c: c["rssi"] if c["rssi"] is not None else 0)[:15]:
            lines.append(f"  {c['name']} {c['ip']} rssi {c['rssi']} snr {c['snr']} on {c['ap']} "
                         f"{c['band']} ch {c['channel']}, rate rx {c['rx_rate']}/tx {c['tx_rate']}")
    else:
        lines.append("WIRELESS CLIENTS: the controller reported none this snapshot (just after a "
                     "controller restart this can be a reporting gap -- check the ping sweep "
                     "before concluding Wi-Fi is empty).")
    return "\n".join(lines)
