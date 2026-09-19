"""Shared SQLite store for the RF sensor node (jarvishackrf).

The collector daemon and the sensorctl CLI both talk to the same database, so the schema
and the fingerprinting rule live here once rather than in two files that could quietly
drift apart -- a registry keyed one way by the writer and queried another by the reader
would "lose" devices that are actually right there.

Pure stdlib on purpose: this runs on a Raspberry Pi with the system Python and no venv,
so it must not need anything pip has to fetch.

Two tables, because a sensor and a sighting are different questions:

  * rf_devices  -- the REGISTRY. One row per distinct transmitter ever heard. This is
                   what you label ("Front Door") and what Home Assistant/Jarvis read.
                   A device is born UNREGISTERED the first time it transmits, which is
                   the whole "trigger it and it appears" workflow -- discovery is just
                   "an unregistered row showed up".
  * rf_events   -- the LOG. Every decoded transmission, raw JSON kept. Feeds "what fired
                   in the last minute" during labelling, and later the Tier-2 awareness
                   history (how often/when a signal is present).
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

# Identity of a physical transmitter. rtl_433 emits a different field set per protocol,
# so we pick the first stable per-device identifier present, in priority order. For the
# tristate PT2262/EV1527 sensors in this house that is `id` (the address bits, fixed per
# unit); `cmd`/state bits vary with which button/contact fired and are therefore an event
# attribute, NOT part of the device identity -- otherwise one door sensor would split into
# a separate "device" for open and for close.
_ID_FIELDS = ("id", "unit", "address", "channel", "sensor_id", "code")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rf_devices (
    fingerprint   TEXT PRIMARY KEY,   -- e.g. "Generic-Remote/id=63320"
    model         TEXT NOT NULL,
    id_field      TEXT,               -- which field gave the identity (id/unit/...)
    id_value      TEXT,
    label         TEXT,               -- friendly name, NULL until registered
    location      TEXT,               -- room/placement
    device_type   TEXT,               -- door | motion | smoke | remote | unknown
    registered    INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    event_count   INTEGER NOT NULL DEFAULT 0,
    seen_cmds     TEXT,               -- JSON list of distinct cmd/state signatures seen
    last_rssi     REAL,
    last_snr      REAL,
    freq_mhz      REAL,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS rf_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT NOT NULL,
    at            TEXT NOT NULL,
    cmd           TEXT,
    state         TEXT,
    rssi          REAL,
    snr           REAL,
    freq_mhz      REAL,
    raw           TEXT NOT NULL       -- the full rtl_433 JSON line, so nothing is lost
);
CREATE INDEX IF NOT EXISTS idx_rf_events_fp ON rf_events(fingerprint, id DESC);
CREATE INDEX IF NOT EXISTS idx_rf_events_at ON rf_events(at);

-- Tier-2: the wideband SURVEY, from the HackRF's hackrf_sweep. Not decoded devices --
-- raw spectrum occupancy. One row per frequency bucket ever seen with meaningful energy,
-- carrying exactly what the operator asked for: what is there, when it first appeared,
-- when it was last present, and how long/often it has been around. A watching agent reads
-- this to sort "expected" (here every sweep, forever) from "new" from "came and went".
CREATE TABLE IF NOT EXISTS rf_signals (
    freq_mhz      REAL PRIMARY KEY,   -- bucket centre, rounded to the sweep resolution
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    sweeps_seen   INTEGER NOT NULL DEFAULT 1,   -- how many surveys it was present in
    peak_db       REAL,               -- strongest it has ever been
    last_db       REAL,               -- how strong last time
    label         TEXT,               -- optional: "UHF repeater", "Wi-Fi", named by agent/op
    classification TEXT,              -- expected | new | intermittent | anomaly (agent-set)
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_rf_signals_last ON rf_signals(last_seen);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()


def fingerprint(d: dict) -> tuple[str, str, str | None, str | None]:
    """(fingerprint, model, id_field, id_value) for one decoded message.

    Never raises on a weird message: a decode with no recognisable id still gets a stable
    fingerprint from its model so it is logged and visible, rather than silently dropped.
    """
    model = str(d.get("model") or "Unknown")
    for field in _ID_FIELDS:
        val = d.get(field)
        if val not in (None, ""):
            return f"{model}/{field}={val}", model, field, str(val)
    return f"{model}/nofield", model, None, None


def _state_signature(d: dict) -> str | None:
    """A short human-readable signature of what a message MEANT (button, contact, motion),
    used only to show the operator 'this device sends these kinds of events' while
    labelling. Not identity -- see the note on _ID_FIELDS."""
    parts = []
    for k in ("cmd", "state", "motion", "contact", "closed", "opened", "event",
              "command", "alarm", "tamper", "tristate", "learn"):
        if k in d and d[k] not in (None, ""):
            parts.append(f"{k}={d[k]}")
    return " ".join(parts) if parts else None


def record_event(conn: sqlite3.Connection, d: dict) -> tuple[str, bool]:
    """Upsert the device and append the event. Returns (fingerprint, is_new_device).

    is_new_device is what the collector logs as a discovery -- the first time a
    transmitter is ever heard it is created unregistered, which is the signal that there
    is something new to name.
    """
    fp, model, id_field, id_value = fingerprint(d)
    sig = _state_signature(d)
    ts = now()
    rssi = d.get("rssi")
    snr = d.get("snr")
    freq = d.get("freq")

    row = conn.execute("SELECT seen_cmds, event_count FROM rf_devices WHERE fingerprint = ?",
                        (fp,)).fetchone()
    is_new = row is None
    if is_new:
        conn.execute(
            """INSERT INTO rf_devices
                   (fingerprint, model, id_field, id_value, registered,
                    first_seen, last_seen, event_count, seen_cmds,
                    last_rssi, last_snr, freq_mhz)
               VALUES (?,?,?,?,0,?,?,1,?,?,?,?)""",
            (fp, model, id_field, id_value, ts, ts,
             json.dumps([sig] if sig else []), rssi, snr, freq))
    else:
        cmds = json.loads(row["seen_cmds"] or "[]")
        if sig and sig not in cmds:
            cmds.append(sig)
            cmds = cmds[-12:]   # a device with many buttons should not grow unbounded
        conn.execute(
            """UPDATE rf_devices SET last_seen = ?, event_count = event_count + 1,
                   seen_cmds = ?, last_rssi = ?, last_snr = ?, freq_mhz = ?
               WHERE fingerprint = ?""",
            (ts, json.dumps(cmds), rssi, snr, freq, fp))

    conn.execute(
        """INSERT INTO rf_events (fingerprint, at, cmd, state, rssi, snr, freq_mhz, raw)
           VALUES (?,?,?,?,?,?,?,?)""",
        (fp, ts, str(d.get("cmd")) if d.get("cmd") is not None else None,
         sig, rssi, snr, freq, json.dumps(d, separators=(",", ":"))))
    conn.commit()
    return fp, is_new


def record_sweep(conn: sqlite3.Connection, present: dict[float, float],
                 min_over_floor_db: float) -> tuple[int, int]:
    """Fold one completed survey into rf_signals. `present` maps freq_mhz -> peak dB for
    the buckets that stood above the noise floor this sweep. Returns (updated, new).

    A survey is the unit of time here, not the wall clock: 'how long has it been there' is
    measured in surveys seen, so a signal present in every sweep for a day and one that
    blinked once are distinguishable regardless of how often the sweeper runs. sweeps that
    a bucket is absent from simply don't touch its row -- last_seen is the truth about when
    it was last actually heard.
    """
    ts = now()
    updated = new = 0
    for freq, db in present.items():
        row = conn.execute("SELECT peak_db FROM rf_signals WHERE freq_mhz = ?", (freq,)).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO rf_signals (freq_mhz, first_seen, last_seen, sweeps_seen,
                                           peak_db, last_db, classification)
                   VALUES (?,?,?,1,?,?,'new')""", (freq, ts, ts, db, db))
            new += 1
        else:
            peak = max(row["peak_db"] if row["peak_db"] is not None else db, db)
            conn.execute(
                """UPDATE rf_signals SET last_seen = ?, sweeps_seen = sweeps_seen + 1,
                       peak_db = ?, last_db = ? WHERE freq_mhz = ?""",
                (ts, peak, db, freq))
            updated += 1
    conn.commit()
    return updated, new


def resolve(conn: sqlite3.Connection, selector: str) -> list[sqlite3.Row]:
    """Devices matching a selector: an exact fingerprint, a bare id value, or a substring.

    So the operator can type `label 63320 "Front Door"` without quoting the whole
    "Generic-Remote/id=63320" -- the short id is what they can read off the CLI.
    """
    exact = conn.execute("SELECT * FROM rf_devices WHERE fingerprint = ?", (selector,)).fetchall()
    if exact:
        return exact
    return conn.execute(
        "SELECT * FROM rf_devices WHERE id_value = ? OR fingerprint LIKE ? ORDER BY last_seen DESC",
        (selector, f"%{selector}%")).fetchall()
