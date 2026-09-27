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
import sqlite3
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
        "reachable": d.get("status") == DEVICE_STATUS_ONLINE,
    } for d in devices]


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
