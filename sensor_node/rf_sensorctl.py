#!/usr/bin/env python3
"""Operator CLI for the RF sensor registry on jarvishackrf.

The whole point of this file is that adding a device is trivial and identical whether the
device already existed or you just installed it: trigger it, it shows up under `pending`,
you `label` it. Do it over SSH from a phone; it is plain text in, plain text out.

  rf_sensorctl pending                 # transmitters seen but not yet named -> the queue
  rf_sensorctl recent --minutes 2      # what fired just now (trigger a sensor, watch it)
  rf_sensorctl label 63320 "Front Door" --location "Entry" --type door
  rf_sensorctl list                    # everything you have named
  rf_sensorctl show 63320              # one device, with the events it has sent
  rf_sensorctl rename 63320 "Front Door (garage side)"
  rf_sensorctl forget 63320            # drop a device (e.g. a neighbour's, or a mistake)
  rf_sensorctl export                  # registered devices as JSON, for Home Assistant/Jarvis

`export` is deliberately the machine-readable seam: Home Assistant automations and Jarvis
read the labelled registry from here rather than anyone hand-maintaining a second copy.
"""
import argparse
import json
import os

import rf_store

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rf.db")
VALID_TYPES = ("door", "motion", "smoke", "remote", "water", "temperature", "unknown")


def _fmt_age(iso: str | None) -> str:
    if not iso:
        return "never"
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 90:
        return f"{secs:.0f}s ago"
    if secs < 5400:
        return f"{secs / 60:.0f}m ago"
    if secs < 172800:
        return f"{secs / 3600:.0f}h ago"
    return f"{secs / 86400:.0f}d ago"


def _device_line(r) -> str:
    name = r["label"] or "(unregistered)"
    loc = f" @ {r['location']}" if r["location"] else ""
    typ = f" [{r['device_type']}]" if r["device_type"] else ""
    sigs = ", ".join(json.loads(r["seen_cmds"] or "[]")) or "-"
    return (f"  {r['id_value'] or r['fingerprint']:<14} {name}{loc}{typ}\n"
            f"      {r['model']}  x{r['event_count']}  last {_fmt_age(r['last_seen'])}"
            f"  rssi {r['last_rssi']}  snr {r['last_snr']}\n"
            f"      sends: {sigs}")


def cmd_pending(conn, args) -> int:
    rows = conn.execute(
        "SELECT * FROM rf_devices WHERE registered = 0 ORDER BY last_seen DESC").fetchall()
    if not rows:
        print("No unregistered devices. Trigger a sensor and it will appear here.")
        return 0
    print(f"{len(rows)} unregistered device(s) -- name them with: rf_sensorctl label <id> \"Name\"\n")
    for r in rows:
        print(_device_line(r))
    return 0


def cmd_list(conn, args) -> int:
    rows = conn.execute(
        "SELECT * FROM rf_devices WHERE registered = 1 ORDER BY location, label").fetchall()
    if not rows:
        print("No registered devices yet.")
        return 0
    print(f"{len(rows)} registered device(s):\n")
    for r in rows:
        print(_device_line(r))
    return 0


def cmd_recent(conn, args) -> int:
    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(minutes=args.minutes)).isoformat()
    rows = conn.execute(
        """SELECT e.at, e.fingerprint, e.state, e.rssi, e.snr, d.label
             FROM rf_events e LEFT JOIN rf_devices d ON d.fingerprint = e.fingerprint
            WHERE e.at >= ? ORDER BY e.id DESC LIMIT ?""",
        (since, args.limit)).fetchall()
    if not rows:
        print(f"No events in the last {args.minutes} min.")
        return 0
    print(f"Events in the last {args.minutes} min (newest first):\n")
    for r in rows:
        who = r["label"] or r["fingerprint"]
        print(f"  {r['at'][11:19]}  {who:<28} {r['state'] or ''}  rssi {r['rssi']} snr {r['snr']}")
    return 0


def _resolve_one(conn, selector):
    matches = rf_store.resolve(conn, selector)
    if not matches:
        print(f"No device matches {selector!r}. Try `pending` or `list` to see ids.")
        return None
    if len(matches) > 1:
        print(f"{selector!r} is ambiguous -- {len(matches)} matches:")
        for m in matches:
            print(f"  {m['fingerprint']}  {m['label'] or '(unregistered)'}")
        print("Use the full fingerprint.")
        return None
    return matches[0]


def cmd_label(conn, args) -> int:
    dev = _resolve_one(conn, args.selector)
    if dev is None:
        return 1
    dtype = args.type or "unknown"
    if dtype not in VALID_TYPES:
        print(f"warning: --type {dtype!r} is not one of {', '.join(VALID_TYPES)} (stored anyway)")
    conn.execute(
        """UPDATE rf_devices SET label = ?, location = ?, device_type = ?, registered = 1,
               notes = COALESCE(?, notes) WHERE fingerprint = ?""",
        (args.name, args.location, dtype, args.notes, dev["fingerprint"]))
    conn.commit()
    print(f"Labelled {dev['fingerprint']} -> \"{args.name}\""
          + (f" @ {args.location}" if args.location else "") + f" [{dtype}]")
    return 0


def cmd_rename(conn, args) -> int:
    dev = _resolve_one(conn, args.selector)
    if dev is None:
        return 1
    conn.execute("UPDATE rf_devices SET label = ?, registered = 1 WHERE fingerprint = ?",
                 (args.name, dev["fingerprint"]))
    conn.commit()
    print(f"Renamed {dev['fingerprint']} -> \"{args.name}\"")
    return 0


def cmd_show(conn, args) -> int:
    dev = _resolve_one(conn, args.selector)
    if dev is None:
        return 1
    print(_device_line(dev))
    ev = conn.execute(
        "SELECT at, state, rssi, snr FROM rf_events WHERE fingerprint = ? ORDER BY id DESC LIMIT ?",
        (dev["fingerprint"], args.limit)).fetchall()
    print(f"\n  last {len(ev)} event(s):")
    for e in ev:
        print(f"    {e['at'][11:19]}  {e['state'] or ''}  rssi {e['rssi']} snr {e['snr']}")
    return 0


def cmd_forget(conn, args) -> int:
    dev = _resolve_one(conn, args.selector)
    if dev is None:
        return 1
    conn.execute("DELETE FROM rf_events WHERE fingerprint = ?", (dev["fingerprint"],))
    conn.execute("DELETE FROM rf_devices WHERE fingerprint = ?", (dev["fingerprint"],))
    conn.commit()
    print(f"Forgot {dev['fingerprint']} ({dev['label'] or 'unregistered'}) and its events.")
    return 0


def cmd_signals(conn, args) -> int:
    """The Tier-2 survey table: what the HackRF sweep has seen, and how persistent it is."""
    rows = conn.execute(
        "SELECT * FROM rf_signals ORDER BY sweeps_seen DESC, peak_db DESC LIMIT ?",
        (args.limit,)).fetchall()
    if not rows:
        print("No survey data yet. The sweeper (rf_sweeper.py) populates this.")
        return 0
    print(f"{len(rows)} signal bucket(s), most-persistent first:\n")
    print(f"  {'freq(MHz)':>10}  {'seen':>5}  {'peak':>6}  {'last':>6}  {'since':<9}  class / label")
    for r in rows:
        lbl = " ".join(x for x in (r["classification"] or "", r["label"] or "") if x)
        print(f"  {r['freq_mhz']:>10.2f}  {r['sweeps_seen']:>5}  {r['peak_db']:>6.1f}  "
              f"{r['last_db']:>6.1f}  {_fmt_age(r['last_seen']):<9}  {lbl}")
    return 0


def cmd_export(conn, args) -> int:
    rows = conn.execute(
        "SELECT * FROM rf_devices WHERE registered = 1 ORDER BY location, label").fetchall()
    out = [{
        "fingerprint": r["fingerprint"], "model": r["model"],
        "id_field": r["id_field"], "id_value": r["id_value"],
        "label": r["label"], "location": r["location"], "device_type": r["device_type"],
        "event_count": r["event_count"], "last_seen": r["last_seen"],
    } for r in rows]
    print(json.dumps(out, indent=2))
    return 0


def cmd_replay(conn, args) -> int:
    """Feed a saved rtl_433 JSON-lines file through the recorder -- for seeding the DB from
    an earlier capture, and for testing the pipeline without a live radio."""
    n = new = 0
    with open(args.path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and "model" in d:
                _, is_new = rf_store.record_event(conn, d)
                n += 1
                new += 1 if is_new else 0
    print(f"Replayed {n} event(s); {new} new device(s). See `pending`.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="RF sensor registry control")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default {DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pending", help="devices seen but not yet named")
    sub.add_parser("list", help="registered devices")

    p = sub.add_parser("recent", help="recent events")
    p.add_argument("--minutes", type=int, default=2)
    p.add_argument("--limit", type=int, default=40)

    p = sub.add_parser("label", help="name/register a device")
    p.add_argument("selector"); p.add_argument("name")
    p.add_argument("--location", default=None); p.add_argument("--type", default=None)
    p.add_argument("--notes", default=None)

    p = sub.add_parser("rename", help="change a device's name")
    p.add_argument("selector"); p.add_argument("name")

    p = sub.add_parser("show", help="one device and its recent events")
    p.add_argument("selector"); p.add_argument("--limit", type=int, default=15)

    p = sub.add_parser("forget", help="delete a device and its events")
    p.add_argument("selector")

    p = sub.add_parser("signals", help="Tier-2 HackRF survey: what's around and how persistent")
    p.add_argument("--limit", type=int, default=40)

    sub.add_parser("export", help="registered devices as JSON")

    p = sub.add_parser("replay", help="seed the DB from a saved rtl_433 json-lines file")
    p.add_argument("path")

    args = ap.parse_args()
    rf_store.init_db(args.db)
    conn = rf_store.connect(args.db)
    try:
        return {
            "pending": cmd_pending, "list": cmd_list, "recent": cmd_recent,
            "label": cmd_label, "rename": cmd_rename, "show": cmd_show,
            "forget": cmd_forget, "export": cmd_export, "replay": cmd_replay,
            "signals": cmd_signals,
        }[args.cmd](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
