#!/usr/bin/env python3
"""RF sensor collector for jarvishackrf.

Runs rtl_433 on the RTL-SDR continuously and folds every decoded 433 MHz transmission
into the local SQLite registry (rf_store.py). It is the always-on half of the system;
rf_sensorctl.py is the operator half that lists and labels what this finds.

Why a daemon that spawns rtl_433 rather than two piped systemd units: one process that
owns the child is one thing to supervise and restart, with no fragile named pipe between
units that can wedge if one side dies. If rtl_433 exits (a USB glitch, the dongle yanked),
this logs it and respawns after a short back-off rather than going quietly deaf.

Decode, don't survey: this uses the RTL-SDR, which is the radio that actually decodes the
house sensors (the HackRF, via SoapySDR, saw the bursts but decoded none of them -- its
job here is the Tier-2 wideband sweep, a separate program). Keep them apart.
"""
import argparse
import json
import os
import subprocess
import sys
import time

import rf_store

# Where the sensors live. 433.92 MHz is the common ISM sensor frequency and where this
# house's units were found. rtl_433 can watch several frequencies by hopping, but a door
# sensor that fires while it is parked on a different frequency is a MISSED event, so we
# stay on one band for reliable capture and add bands only if a device is found elsewhere.
DEFAULT_FREQ = "433.92M"
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rf.db")

# rtl_433 is native to the RTL-SDR; -M level adds rssi/snr so a labelling session can tell
# a strong local sensor from a faint neighbour's. JSON on stdout, one object per line.
def rtl_433_cmd(freq: str) -> list[str]:
    return ["rtl_433", "-f", freq, "-M", "level", "-F", "json"]

RESPAWN_BACKOFF_SEC = 5


def log(msg: str) -> None:
    # Unbuffered, timestamped, to stdout -> journald picks it up under the systemd unit.
    print(f"{rf_store.now()} {msg}", flush=True)


def run(db_path: str, freq: str) -> None:
    rf_store.init_db(db_path)
    log(f"rf_collector starting: db={db_path} freq={freq}")

    while True:
        cmd = rtl_433_cmd(freq)
        log(f"launching: {' '.join(cmd)}")
        try:
            # rtl_433's own diagnostics (tuner found, tuned frequency, and crucially any
            # "usb_open error" / "No supported devices") go to stderr. Let them flow to
            # the unit's journal rather than /dev/null: when this runs unattended, "it
            # respawned" is useless without the reason, and the startup banner is also the
            # simplest proof the dongle actually opened.
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=None,
                                    text=True, bufsize=1)
        except FileNotFoundError:
            log("ERROR: rtl_433 not found on PATH -- install it (apt install rtl-433). "
                "Sleeping before retry.")
            time.sleep(30)
            continue

        conn = rf_store.connect(db_path)
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue                      # rtl_433 also prints non-JSON noise
                if not isinstance(d, dict) or "model" not in d:
                    continue
                try:
                    fp, is_new = rf_store.record_event(conn, d)
                except Exception as e:            # one bad message must not kill the feed
                    log(f"WARN: could not record event ({type(e).__name__}: {e})")
                    continue
                if is_new:
                    log(f"NEW DEVICE {fp}  (unregistered -- run: rf_sensorctl label ...)")
        finally:
            conn.close()

        code = proc.poll()
        log(f"rtl_433 exited (code {code}); respawning in {RESPAWN_BACKOFF_SEC}s")
        time.sleep(RESPAWN_BACKOFF_SEC)


def main() -> None:
    ap = argparse.ArgumentParser(description="RF sensor collector (rtl_433 -> SQLite registry)")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default {DEFAULT_DB})")
    ap.add_argument("--freq", default=DEFAULT_FREQ, help=f"rtl_433 frequency (default {DEFAULT_FREQ})")
    args = ap.parse_args()
    try:
        run(args.db, args.freq)
    except KeyboardInterrupt:
        log("rf_collector stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
