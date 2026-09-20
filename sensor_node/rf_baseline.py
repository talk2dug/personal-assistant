#!/usr/bin/env python3
"""RF baseline / anomaly analysis for jarvishackrf -- "what is normal here, and what isn't".

Reads the collector's SQLite log (rf_store.py) and turns raw sightings into a situation
report Jarvis can relay or answer questions from:

  * every transmitter classified: own sensor / neighbour fixture / passing vehicle /
    regular vehicle / NEW vehicle that keeps coming back (the "check that car out" case)
  * vehicle traffic by hour of day, baseline vs the last 24 h
  * a short list of ALERTS with stable keys, so the caller can de-duplicate

    python3 rf_baseline.py             # human report
    python3 rf_baseline.py --json      # machine report (what Jarvis reads over SSH)
    python3 rf_baseline.py --days 30   # how far back the baseline looks

Why vehicles are per-sensor and not per-car: a car has four TPMS sensors, but from the
kerb this antenna usually catches ONE of them per pass (see the event log -- almost every
TPMS id is a single hit). Clustering ids into cars is therefore unreliable; recurrence of
the *same id* across days is the signal that the same vehicle is back, and that is what
we key on. rtl_433 tags these with "type":"TPMS" regardless of the protocol family it
guessed (Renault/Citroen/Hyundai-VDO are decoder names, not the actual make).

Pure stdlib -- runs on the Pi's system Python like the rest of sensor_node/.
"""
import argparse
import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import rf_store

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rf.db")

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/New_York")
except Exception:                                   # no tzdata on the box
    LOCAL_TZ = timezone.utc

# Two sightings closer than this are the same "visit" (a car idling / sensor re-sending).
VISIT_GAP = timedelta(minutes=10)
# A vehicle first heard within this window is "new" for classification purposes.
NEW_WINDOW = timedelta(days=14)
# A new vehicle becomes WATCH-worthy once it has this many separate visits on this many days.
WATCH_MIN_VISITS = 3
WATCH_MIN_DAYS = 2
# Non-vehicle unregistered transmitters present on this many days are someone's fixture
# (a neighbour's alarm, garage opener, weather station) rather than something new.
FIXTURE_MIN_DAYS = 3


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)


def _is_vehicle(model: str, raw: dict) -> bool:
    return raw.get("type") == "TPMS" or "TPMS" in model


def _visits(times: list[datetime]) -> list[tuple[datetime, datetime, int]]:
    """Group sorted timestamps into (start, end, n_events) visits separated by VISIT_GAP."""
    out = []
    for t in times:
        if out and t - out[-1][1] <= VISIT_GAP:
            s, _, n = out[-1]
            out[-1] = (s, t, n + 1)
        else:
            out.append((t, t, 1))
    return out


def analyse(conn: sqlite3.Connection, days: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    devices = {r["fingerprint"]: dict(r) for r in conn.execute("SELECT * FROM rf_devices")}
    events = conn.execute(
        "SELECT fingerprint, at, raw, rssi FROM rf_events WHERE at >= ? ORDER BY at",
        (since.isoformat(),)).fetchall()

    per_dev: dict[str, list[datetime]] = defaultdict(list)
    dev_raw: dict[str, dict] = {}
    for e in events:
        t = _parse(e["at"])
        per_dev[e["fingerprint"]].append(t)
        if e["fingerprint"] not in dev_raw:
            try:
                dev_raw[e["fingerprint"]] = json.loads(e["raw"])
            except (TypeError, ValueError):
                dev_raw[e["fingerprint"]] = {}

    report_devices = []
    alerts = []
    vehicle_visit_times: list[datetime] = []

    for fp, times in per_dev.items():
        d = devices.get(fp, {})
        raw = dev_raw.get(fp, {})
        model = d.get("model") or fp.split("/")[0]
        vehicle = _is_vehicle(model, raw)
        times.sort()
        visits = _visits(times)
        days_seen = sorted({_local(t).date().isoformat() for t in times})
        hours = Counter(_local(v[0]).hour for v in visits)
        first_seen = _parse(d["first_seen"]) if d.get("first_seen") else times[0]
        last_seen = times[-1]
        span_days = max((last_seen - first_seen).days, 0)
        is_new = (now - first_seen) <= NEW_WINDOW
        last24 = [v for v in visits if now - v[0] <= timedelta(hours=24)]
        # Per-device last-24h detail the Command Center needs: how many times it was
        # seen today and, for a vehicle, the gaps between those visits ("back every ~40
        # min" vs "once this morning"). Intervals are minutes between consecutive visit
        # STARTS, so a single visit has none and a car that keeps returning has a series.
        last24_starts = [v[0] for v in last24]
        intervals_min = [round((last24_starts[i] - last24_starts[i - 1]).total_seconds() / 60.0, 1)
                         for i in range(1, len(last24_starts))]

        if d.get("registered"):
            cls = "own"
        elif vehicle:
            vehicle_visit_times.extend(v[0] for v in visits)
            if len(visits) == 1:
                cls = "vehicle-passing"
            elif is_new and len(visits) >= WATCH_MIN_VISITS and len(days_seen) >= WATCH_MIN_DAYS:
                cls = "vehicle-watch"
            elif len(days_seen) >= 3 and span_days >= 7:
                cls = "vehicle-regular"
            else:
                cls = "vehicle-repeat"
        else:
            if len(days_seen) >= FIXTURE_MIN_DAYS:
                cls = "fixture-neighbour"
            elif is_new:
                cls = "unknown-new"
            else:
                cls = "unknown"

        try:
            seen_cmds = json.loads(d["seen_cmds"]) if d.get("seen_cmds") else []
        except (TypeError, ValueError):
            seen_cmds = []
        entry = {
            "fingerprint": fp, "model": model, "label": d.get("label"),
            "class": cls, "vehicle": vehicle,
            # Registry fields so the UI can label a named device and offer the short id
            # as the selector when flagging an unregistered one.
            "registered": bool(d.get("registered")),
            "location": d.get("location"), "device_type": d.get("device_type"),
            "id_field": d.get("id_field"), "id_value": d.get("id_value"),
            "seen_cmds": seen_cmds,
            "last_rssi": d.get("last_rssi"), "last_snr": d.get("last_snr"),
            "first_seen": first_seen.isoformat(), "last_seen": last_seen.isoformat(),
            "events": len(times), "visits": len(visits), "days_seen": len(days_seen),
            "span_days": span_days,
            "hours_local": sorted(hours.items()),
            "visits_last_24h": len(last24),
            "typical_hours": [h for h, _ in hours.most_common(3)],
            "last_24h": {
                "visits": len(last24),
                "events": sum(v[2] for v in last24),
                "first_seen": last24_starts[0].isoformat() if last24_starts else None,
                "last_seen": last24[-1][1].isoformat() if last24 else None,
                "interval_minutes": intervals_min,
                "mean_interval_minutes": (round(sum(intervals_min) / len(intervals_min), 1)
                                          if intervals_min else None),
            },
        }
        report_devices.append(entry)

        if cls == "vehicle-watch":
            hrs = ", ".join(f"{h:02d}:00" for h, _ in sorted(hours.items()))
            alerts.append({
                "key": f"vehicle-watch:{fp}",
                "severity": "notice",
                "kind": "vehicle-watch",
                "fingerprint": fp,
                "escalation": len(visits),
                "text": (f"Unfamiliar vehicle (tyre sensor {model} {fp.split('=')[-1]}) first heard "
                         f"{_local(first_seen):%a %d %b %H:%M}, now {len(visits)} separate visits on "
                         f"{len(days_seen)} days, around {hrs}. Worth a look if it isn't a neighbour."),
            })
        elif cls == "unknown-new" and (now - first_seen) <= timedelta(hours=24):
            alerts.append({
                "key": f"unknown-new:{fp}",
                "severity": "info",
                "kind": "unknown-new",
                "fingerprint": fp,
                "escalation": len(times),
                "text": f"New non-vehicle transmitter nearby: {model} ({fp.split('=')[-1]}), "
                        f"first heard {_local(first_seen):%a %H:%M}, {len(times)} transmission(s).",
            })

    # Vehicle traffic: visits per local hour-of-day, averaged per day of baseline, vs last 24 h.
    n_days = max(1, min(days, (now - since).days))
    hour_base = Counter(_local(t).hour for t in vehicle_visit_times)
    last24_times = [t for t in vehicle_visit_times if now - t <= timedelta(hours=24)]
    # Passing cars in the last 12 h: one summary number for the Command Center, so the
    # table no longer needs a row per one-off car. A visit start is one car driving past
    # (vehicles are keyed per TPMS sensor -- see the module docstring), so this counts
    # EVERY vehicle sighting in the window, repeat vehicles included, not just the
    # single-pass class: it answers "how many cars drove by in the last 12 h".
    last12_times = [t for t in vehicle_visit_times if now - t <= timedelta(hours=12)]
    hour_24 = Counter(_local(t).hour for t in last24_times)
    baseline_per_day = len(vehicle_visit_times) / n_days
    traffic = {
        "baseline_days": n_days,
        "vehicle_visits_total": len(vehicle_visit_times),
        "vehicle_visits_per_day": round(baseline_per_day, 2),
        "vehicle_visits_last_24h": len(last24_times),
        "vehicle_visits_last_12h": len(last12_times),
        "passing_12h": len(last12_times),
        "busiest_hours_local": [h for h, _ in hour_base.most_common(4)],
        "by_hour_baseline_per_day": {h: round(c / n_days, 2) for h, c in sorted(hour_base.items())},
        "by_hour_last_24h": dict(sorted(hour_24.items())),
    }
    if n_days >= 3 and len(last24_times) >= 6 and len(last24_times) > 2.5 * baseline_per_day:
        alerts.append({
            "key": f"traffic-spike:{now:%Y-%m-%d}",
            "severity": "info", "kind": "traffic-spike", "escalation": len(last24_times),
            "text": (f"Vehicle traffic past 24 h is {len(last24_times)} sensor visits vs a usual "
                     f"{baseline_per_day:.1f}/day."),
        })

    counts = Counter(e["class"] for e in report_devices)
    order = ["vehicle-watch", "unknown-new", "vehicle-regular", "vehicle-repeat",
             "fixture-neighbour", "own", "vehicle-passing", "unknown"]
    report_devices.sort(key=lambda e: (order.index(e["class"]) if e["class"] in order else 99,
                                       -e["visits"]))
    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "counts": dict(counts),
        "traffic": traffic,
        "alerts": alerts,
        "devices": report_devices,
    }


def render(rep: dict) -> str:
    c = rep["counts"]
    t = rep["traffic"]
    lines = [
        f"RF baseline ({rep['window_days']}d window, generated {rep['generated_at'][:16]}Z)",
        "",
        f"  own sensors {c.get('own', 0)} | neighbour fixtures {c.get('fixture-neighbour', 0)} | "
        f"vehicles: regular {c.get('vehicle-regular', 0)}, repeat {c.get('vehicle-repeat', 0)}, "
        f"passing {c.get('vehicle-passing', 0)}, WATCH {c.get('vehicle-watch', 0)} | "
        f"unknown new {c.get('unknown-new', 0)}",
        f"  vehicle traffic: {t['vehicle_visits_per_day']}/day baseline over {t['baseline_days']}d, "
        f"{t['vehicle_visits_last_24h']} in last 24h, {t.get('passing_12h', 0)} passing in last 12h; "
        f"busiest hours {t['busiest_hours_local']}",
        "",
    ]
    if rep["alerts"]:
        lines.append("ALERTS:")
        for a in rep["alerts"]:
            lines.append(f"  [{a['severity']}] {a['text']}")
        lines.append("")
    lines.append("Devices:")
    for e in rep["devices"]:
        name = e["label"] or e["fingerprint"]
        lines.append(f"  {e['class']:<18} {name:<34} visits {e['visits']:>3} on {e['days_seen']:>2}d "
                     f"(span {e['span_days']}d)  hrs {e['typical_hours']}  last {e['last_seen'][5:16]}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="RF baseline / anomaly report")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    rf_store.init_db(args.db)
    conn = rf_store.connect(args.db)
    try:
        rep = analyse(conn, args.days)
    finally:
        conn.close()
    print(json.dumps(rep, indent=1) if args.json else render(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
