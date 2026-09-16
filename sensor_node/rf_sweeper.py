#!/usr/bin/env python3
"""HackRF wideband sweeper for jarvishackrf -- the Tier-2 "what's around us" survey.

Every interval it runs hackrf_sweep across the configured span, finds the frequency
buckets standing above the noise floor, and folds them into rf_signals (rf_store.py) with
first-seen / last-seen / how-many-surveys. Over time that table becomes the RF baseline:
what is ALWAYS here (the neighbourhood repeater, Wi-Fi), what is NEW, and what came and
went. A watching agent reads it later to call expected vs anomalous -- this program only
observes and records; it makes no judgements.

Separate radio from the collector on purpose: the collector owns the RTL-SDR for decoding
433 sensors; this owns the HackRF for the survey. They do not contend.

Accepted limitation (Jack, 2026-09-16): the HackRF must be in "HackRF mode" on the
PortaPack. If a sweep finds no device, this logs it and tries again next interval rather
than crashing -- so a period in another mode is just a gap in the record, not an outage.
RX only: sweeping is passive listening. This never transmits.
"""
import argparse
import os
import subprocess
import time

import rf_store

# The span to survey. 300-450 MHz is the low-UHF band where remotes, sensors, fobs and
# land-mobile live -- the richest "who is transmitting near me" range and where this house
# already shows a repeater at ~451 and the 433 sensors. Widen later once the baseline for
# this span is understood; a bigger span is just a longer sweep.
DEFAULT_RANGE = "300:450"
DEFAULT_BIN_HZ = 100_000        # 100 kHz buckets: fine enough to separate signals, coarse
                                # enough that the table does not explode
DEFAULT_INTERVAL = 300          # seconds between surveys
DEFAULT_DWELL = 12              # seconds of sweeping per survey (many passes -> stable peaks)
DEFAULT_OVER_FLOOR = 15.0       # a bucket must beat the sweep's own noise floor by this
                                # many dB to count as "a signal", not noise wobble
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rf.db")


def log(msg: str) -> None:
    print(f"{rf_store.now()} {msg}", flush=True)


def one_sweep(freq_range: str, bin_hz: int, dwell: int, over_floor: float) -> dict[float, float]:
    """Run hackrf_sweep for `dwell` seconds and return {freq_mhz: peak_db} for buckets that
    beat the noise floor by `over_floor` dB. Empty dict if the radio is unavailable."""
    cmd = ["hackrf_sweep", "-f", freq_range, "-w", str(bin_hz), "-l", "32", "-g", "20"]
    try:
        proc = subprocess.run(["timeout", str(dwell)] + cmd, capture_output=True, text=True)
    except FileNotFoundError:
        log("ERROR: hackrf_sweep not found (apt install hackrf)")
        return {}

    peak: dict[float, float] = {}
    allvals: list[float] = []
    for line in proc.stdout.splitlines():
        parts = line.split(", ")
        if len(parts) < 7:
            continue
        try:
            lo = int(parts[2]); bw = float(parts[4]); dbs = [float(x) for x in parts[6:]]
        except ValueError:
            continue
        for i, db in enumerate(dbs):
            f = round((lo + (i + 0.5) * bw) / 1e6, 2)
            allvals.append(db)
            if f not in peak or db > peak[f]:
                peak[f] = db

    if not allvals:
        # No output at all = the HackRF did not sweep (not in HackRF mode, unplugged, busy).
        err = (proc.stderr or "").strip().splitlines()
        log("no sweep data -- HackRF not available "
            + (f"({err[-1]})" if err else "(is the PortaPack in HackRF mode?)"))
        return {}

    allvals.sort()
    floor = allvals[len(allvals) // 2]
    threshold = floor + over_floor
    return {f: db for f, db in peak.items() if db >= threshold}


def run(db_path: str, freq_range: str, bin_hz: int, interval: int, dwell: int,
        over_floor: float) -> None:
    rf_store.init_db(db_path)
    log(f"rf_sweeper starting: db={db_path} range={freq_range}MHz bin={bin_hz}Hz "
        f"interval={interval}s dwell={dwell}s over_floor={over_floor}dB")
    while True:
        start = time.time()
        present = one_sweep(freq_range, bin_hz, dwell, over_floor)
        if present:
            conn = rf_store.connect(db_path)
            try:
                updated, new = rf_store.record_sweep(conn, present, over_floor)
            finally:
                conn.close()
            log(f"survey: {len(present)} active bucket(s), {new} new, {updated} known")
        # Sleep the remainder of the interval (never negative if a sweep ran long).
        time.sleep(max(5, interval - (time.time() - start)))


def main() -> None:
    ap = argparse.ArgumentParser(description="HackRF wideband sweeper (survey -> rf_signals)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--range", default=DEFAULT_RANGE, help=f"MHz low:high (default {DEFAULT_RANGE})")
    ap.add_argument("--bin-hz", type=int, default=DEFAULT_BIN_HZ)
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    ap.add_argument("--dwell", type=int, default=DEFAULT_DWELL)
    ap.add_argument("--over-floor", type=float, default=DEFAULT_OVER_FLOOR)
    ap.add_argument("--once", action="store_true", help="one survey then exit (for testing)")
    args = ap.parse_args()

    if args.once:
        rf_store.init_db(args.db)
        present = one_sweep(args.range, args.bin_hz, args.dwell, args.over_floor)
        if present:
            conn = rf_store.connect(args.db)
            try:
                updated, new = rf_store.record_sweep(conn, present, args.over_floor)
            finally:
                conn.close()
            log(f"survey: {len(present)} active bucket(s), {new} new, {updated} known")
        return
    try:
        run(args.db, args.range, args.bin_hz, args.interval, args.dwell, args.over_floor)
    except KeyboardInterrupt:
        log("rf_sweeper stopped")


if __name__ == "__main__":
    main()
