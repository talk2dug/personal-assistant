"""Live reachability for every device Jarvis is supposed to know about, recorded over
time -- not just a point-in-time probe. ssh_health.py already answers "is this box up
right now" for the dashboard; this is the piece that didn't exist: something that
remembers the last check, so "just went down" and "still down since this morning" are
different facts instead of the same undifferentiated red dot.

Reused, not rebuilt: ssh_health.check_all_hosts() for the 11 SSH hosts, and
GPUBridge.reachable() for simrig's Ollama/ComfyUI backend -- both already do a real,
lightweight liveness check. The only genuinely new probes here are Phone MCP and
Orpheus, neither of which had one.

Pure measurement. This module never fixes anything and never notifies anyone -- see
host_fixes.py for the whitelisted-repair half and scheduler.py's host_health_tick for
the loop that ties measurement, repair and notification together.
"""
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import httpx

from . import ssh_health

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 3.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS host_status (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    reachable INTEGER NOT NULL,
    latency_ms INTEGER,
    last_checked_at TEXT NOT NULL,
    last_up_at TEXT,
    last_down_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_fix_id TEXT,
    last_fix_at TEXT,
    last_fix_result TEXT
);
"""


def init_host_health_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_http(name: str, url: str | None, timeout: float = HTTP_TIMEOUT) -> dict:
    """A plain reachability probe for a service that has no SSH port to check --
    same shape as ssh_health.check_host's result so both feed record_check identically.
    Any response at all (even a 404/405) proves the service is up and answering; only a
    connection failure means it is not.
    """
    if not url:
        return {"name": name, "kind": "http", "reachable": False, "latency_ms": None}
    import time
    start = time.monotonic()
    try:
        httpx.get(url, timeout=timeout)
        reachable = True
    except Exception:
        reachable = False
    latency_ms = round((time.monotonic() - start) * 1000) if reachable else None
    return {"name": name, "kind": "http", "reachable": reachable, "latency_ms": latency_ms}


def check_all(ssh_hosts: dict, bridge=None, orpheus_url: str | None = None) -> list[dict]:
    """Every device Jarvis is meant to know about, checked right now.

    Merges ssh_health's 11 registered SSH hosts with the handful of non-SSH services
    that matter: simrig's Ollama/ComfyUI backend (via the bridge that already knows how
    to reach it), Phone MCP, and Orpheus. Anything not configured is simply absent from
    the result rather than reported down -- an unconfigured service isn't an outage.
    """
    results = [{**r, "kind": "ssh"} for r in ssh_health.check_all_hosts(ssh_hosts)]

    if bridge is not None:
        results.append({
            "name": "simrig_ollama", "kind": "http",
            "reachable": bridge.reachable(), "latency_ms": None,
        })
        if bridge.comfy is not None:
            results.append({
                "name": "simrig_comfyui", "kind": "http",
                "reachable": bridge.comfy.reachable(), "latency_ms": None,
            })

    if orpheus_url:
        results.append(_check_http("orpheus", orpheus_url))

    return results


def record_check(db_path: str, results: list[dict], now: str | None = None) -> list[dict]:
    """Upserts every result, returns only what changed -- a fresh down, a fresh recovery,
    or a host that's still down. Unchanged-and-still-up hosts are the common case and are
    deliberately left out, so the caller (host_health_tick) isn't re-deciding on all 15+
    devices every 15 minutes when nothing happened.
    """
    now = now or _now()
    changed = []
    with closing(_connect(db_path)) as conn:
        for r in results:
            prior = conn.execute(
                "SELECT * FROM host_status WHERE name = ?", (r["name"],)).fetchone()
            reachable = bool(r["reachable"])
            was_reachable = bool(prior["reachable"]) if prior else None

            if reachable:
                consecutive = 0
                last_up_at = now
                last_down_at = prior["last_down_at"] if prior else None
            else:
                consecutive = (prior["consecutive_failures"] if prior else 0) + 1
                last_up_at = prior["last_up_at"] if prior else None
                last_down_at = now if was_reachable is not False else (prior["last_down_at"] if prior else now)

            conn.execute(
                """INSERT INTO host_status (name, kind, reachable, latency_ms, last_checked_at,
                                            last_up_at, last_down_at, consecutive_failures)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       kind = excluded.kind, reachable = excluded.reachable,
                       latency_ms = excluded.latency_ms, last_checked_at = excluded.last_checked_at,
                       last_up_at = excluded.last_up_at, last_down_at = excluded.last_down_at,
                       consecutive_failures = excluded.consecutive_failures""",
                (r["name"], r["kind"], int(reachable), r.get("latency_ms"), now,
                 last_up_at, last_down_at, consecutive))

            if was_reachable is None and not reachable:
                changed.append({**r, "transition": "down", "consecutive_failures": consecutive})
            elif was_reachable is True and not reachable:
                changed.append({**r, "transition": "down", "consecutive_failures": consecutive})
            elif was_reachable is False and reachable:
                changed.append({**r, "transition": "recovered", "consecutive_failures": 0})
            elif was_reachable is False and not reachable:
                changed.append({**r, "transition": "still_down", "consecutive_failures": consecutive})
        conn.commit()
    return changed


def record_fix_attempt(db_path: str, name: str, fix_id: str, result: str, now: str | None = None) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE host_status SET last_fix_id = ?, last_fix_at = ?, last_fix_result = ? WHERE name = ?",
            (fix_id, now or _now(), result, name))
        conn.commit()


def get_host(db_path: str, name: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM host_status WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


def all_hosts(db_path: str) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM host_status ORDER BY name")]
