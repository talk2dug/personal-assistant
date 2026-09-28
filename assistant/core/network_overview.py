"""One read of the whole network for the Command Center's Network section: every host
check host_health.py records, what the Omada controller reports (devices + clients), and
the sys-admin agent's recent ops plans -- joined, so a failing check can be read against
what the network itself sees.

That join is the point. Two real failures on 2026-09-27 were only legible with both views
side by side: simrig's SSH check had failed 168 times while Omada showed simrig online
(the check used simrig.local, which jarvisbox can't resolve), and phone_mcp's configured
IP had been taken by an unrelated IoT client. host_status alone reads both as "down".

Pure read -- no probing, no network calls. The 15-minute scheduler ticks
(host_health_tick / omada_health_tick) do the measuring; this only reports what they
recorded, so the page can never block on an unreachable box.
"""
import ipaddress
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import host_health, omada_health, ops_plans

# A client first seen within this window is flagged "new". Matches the cadence at which
# Jack would plausibly notice and ask "what is that?".
NEW_CLIENT_WINDOW = timedelta(hours=24)
RECENT_PLANS = 12
STEP_OUTPUT_CHARS = 600


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _is_ip(value: str | None) -> bool:
    try:
        ipaddress.ip_address(value or "")
        return True
    except ValueError:
        return False


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _check_targets(cfg) -> dict[str, str]:
    """What address each named check actually dials, as config states it."""
    targets = {name: (spec or {}).get("host") for name, spec in (cfg.ssh_hosts or {}).items()}
    orpheus_url = getattr(cfg, "orpheus_url", None)
    if orpheus_url:
        targets["orpheus"] = urlparse(orpheus_url).hostname
    return {k: v for k, v in targets.items() if v}


def _same_thing(a: str, b: str) -> bool:
    """Loose name match between a check name and an Omada client name ("jarvishackrf2"
    vs "jarvisHackRF2", "laptop1" vs nothing). Substring either way, after stripping
    punctuation, so "homeassistant" matches "home-assistant" but "phonemcp" never
    matches "lwip0"."""
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a in b or b in a)


def _find_client(target: str | None, check_name: str, by_ip: dict, by_name: dict) -> tuple[dict | None, str]:
    """(client, how) -- how is 'ip' or 'name'. Loopback targets (jarvisbox dialling
    itself, orpheus on 127.0.0.1) have no network-side view to compare against."""
    if target and _is_ip(target):
        if ipaddress.ip_address(target).is_loopback:
            return None, ""
        return by_ip.get(target), "ip"
    host_label = (target or "").split(".")[0]
    for key in (_norm(host_label), _norm(check_name)):
        if key and key in by_name:
            return by_name[key], "name"
    return None, ""


def _annotate_hosts(hosts: list[dict], targets: dict, clients: list[dict]) -> list[dict]:
    online = [c for c in clients if c["online"]]
    by_ip = {c["ip"]: c for c in online if c.get("ip")}
    by_name = {}
    for c in online:
        by_name.setdefault(_norm(c.get("name")), c)

    out = []
    for h in hosts:
        target = targets.get(h["name"])
        client, how = _find_client(target, h["name"], by_ip, by_name)
        finding = None
        if not h["reachable"] and client:
            if how == "name":
                finding = {
                    "kind": "check_suspect",
                    "text": (f"Omada sees {client['name']} online at {client['ip']}, but the check "
                             f"dials {target}. The name may not resolve from jarvisbox; "
                             f"pointing the check at {client['ip']} would settle it."),
                }
            elif not _same_thing(h["name"], client.get("name")):
                finding = {
                    "kind": "ip_taken",
                    "text": (f"{target} now belongs to \"{client['name']}\" on Omada, not "
                             f"{h['name']}. The service has probably moved to a new IP; "
                             f"a DHCP reservation would keep it put."),
                }
            else:
                finding = {
                    "kind": "service_down",
                    "text": (f"The machine is on the network at {client['ip']} but the "
                             f"service isn't answering."),
                }
        out.append({
            **h,
            "target": target,
            "omada_ip": client["ip"] if client else None,
            "omada_name": client["name"] if client else None,
            "finding": finding,
        })
    return out


def _read_omada(db_path: str) -> tuple[list[dict], list[dict]]:
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        devices = [dict(r) for r in conn.execute("SELECT * FROM omada_devices ORDER BY kind, name")]
        clients = [dict(r) for r in conn.execute("SELECT * FROM omada_clients ORDER BY online DESC, ip")]
    return devices, clients


def _recent_plans(db_path: str, owner_user_id: int) -> list[dict]:
    plans = []
    for p in ops_plans.list_plans(db_path, owner_user_id, limit=RECENT_PLANS):
        full = ops_plans.get_plan(db_path, p["id"]) or p
        steps = [{
            "step_index": s["step_index"], "phase": s["phase"], "host": s["host"],
            "status": s["status"], "purpose": s["purpose"],
            "output": (s.get("output") or "")[:STEP_OUTPUT_CHARS] or None,
        } for s in full.get("steps", [])]
        plans.append({
            "id": p["id"], "summary": p["summary"], "status": p["status"],
            "review_item_id": p.get("review_item_id"),
            "created_at": p["created_at"], "updated_at": p["updated_at"], "steps": steps,
        })
    return plans


def build(db_path: str, cfg, owner_user_id: int, now: datetime | None = None) -> dict:
    # Idempotent CREATE IF NOT EXISTS: a fresh DB (or one whose ticks haven't run yet)
    # reads as empty rather than erroring.
    host_health.init_host_health_db(db_path)
    omada_health.init_omada_health_db(db_path)
    ops_plans.init_ops_plans_db(db_path)

    now = now or datetime.now(timezone.utc)
    devices, clients = _read_omada(db_path)
    for c in clients:
        first = _parse(c.get("first_seen_at"))
        c["is_new"] = bool(first and now - first <= NEW_CLIENT_WINDOW)

    # Clients first seen in the very first sweep aren't "new" -- they were already on the
    # network when tracking began. Without this, day one flags all 49.
    earliest = min((c["first_seen_at"] for c in clients), default=None)
    for c in clients:
        if c["first_seen_at"] == earliest:
            c["is_new"] = False

    hosts = _annotate_hosts(host_health.all_hosts(db_path), _check_targets(cfg), clients)
    plans = _recent_plans(db_path, owner_user_id)

    def latest(rows, key):
        return max((r[key] for r in rows if r.get(key)), default=None)

    return {
        "generated_at": now.isoformat(),
        "hosts": hosts,
        "hosts_checked_at": latest(hosts, "last_checked_at"),
        "omada": {
            "configured": bool(getattr(cfg, "omada_controller_url", None)),
            "controller_url": getattr(cfg, "omada_controller_url", None),
            "checked_at": latest(devices, "last_checked_at"),
            "tracking_since": earliest,
            "devices": devices,
            "clients": clients,
        },
        "plans": plans,
    }
