"""Radio awareness: what the two SDR nodes hear, kept where Jarvis can read it.

Three sources land here, all of them local, none of them cloud:

  * the NOAA weather stream (jarvishackrf2, 162.475 MHz) -- transcribed continuously
    on this box, and the spoken current conditions pulled out of it every few minutes;
  * the Fire/EMS scanner stream -- each squelch-opened transmission transcribed and
    classified for whether the owner would want to know;
  * the SAME/EAS alert headers the weather station sends before every warning
    (decoded deterministically on the Pi by fm_node/eas_watch.py) and the RF baseline
    report from jarvishackrf (sensor_node/rf_baseline.py) -- polled in by the worker.

`radio_items` is the single "things worth knowing" table: an EAS warning, a flagged
scanner call, a vehicle the sensor node keeps hearing. Each has a stable `key`, so the
same alert arriving three times (the SAME header literally repeats, and the RF report
re-lists a vehicle every run) is one row. `deliver_pending` is the ONLY path from here
to the owner's phone and it is driven by JarvisCore's scheduler, through the same
`notify` funnel everything else uses -- the worker process that fills these tables cannot
notify on its own authority.

Feed health is first-class (feed_status), same reasoning as market_data: a silent
scanner and a dead stream look identical from the tables alone, and "no alerts" from a
worker that stopped an hour ago is the wrong kind of quiet.

Schema/queries live in this module, LLM prompts too, so tests can pin the parsing without
audio, a radio, or a model in the loop.
"""
import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

STREAM_WEATHER = "weather"
STREAM_SCANNER = "scanner"
SEVERITIES = ("urgent", "notice", "info")

SCHEMA = """
CREATE TABLE IF NOT EXISTS radio_transcripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stream      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    duration_s  REAL,
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_radio_transcripts_stream ON radio_transcripts(stream, started_at DESC);

CREATE TABLE IF NOT EXISTS radio_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,          -- eas | weather_hazard | scanner | rf_vehicle | rf_new | rf_traffic
    severity    TEXT NOT NULL,          -- urgent | notice | info
    stream      TEXT,
    at          TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,
    meta        TEXT,                   -- JSON
    notified_at TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_radio_items_at ON radio_items(at DESC);
CREATE INDEX IF NOT EXISTS idx_radio_items_pending ON radio_items(notified_at, severity);

CREATE TABLE IF NOT EXISTS weather_conditions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL,
    source          TEXT NOT NULL,
    temperature_f   REAL,
    humidity_pct    REAL,
    wind            TEXT,
    pressure_in     REAL,
    pressure_trend  TEXT,
    sky             TEXT,
    heat_index_f    REAL,
    summary         TEXT,
    hazards         TEXT,
    forecast        TEXT,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_weather_conditions_at ON weather_conditions(at DESC);

CREATE TABLE IF NOT EXISTS radio_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);
"""

# How old a heartbeat may be before the feed is reported stale. Audio frames arrive
# continuously (the scanner pacer pads silence), so a gap this long means the stream or
# the worker is down, not that nobody is talking.
AUDIO_STALE_SECONDS = 180
EAS_POLL_STALE_SECONDS = 300
RF_POLL_STALE_SECONDS = 2400


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_radio_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()


# --- transcripts -------------------------------------------------------------------

def record_transcript(db_path: str, stream: str, started_at: datetime, ended_at: datetime,
                      text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO radio_transcripts (stream, started_at, ended_at, duration_s, text) "
            "VALUES (?,?,?,?,?)",
            (stream, _iso(started_at), _iso(ended_at),
             round((ended_at - started_at).total_seconds(), 1), text))
        conn.commit()
        return cur.lastrowid


def recent_transcripts(db_path: str, stream: str, minutes: int = 30, limit: int = 200,
                       now: datetime | None = None) -> list[dict]:
    since = _iso((now or _now()) - timedelta(minutes=minutes))
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT id, stream, started_at, ended_at, duration_s, text FROM radio_transcripts "
            "WHERE stream = ? AND started_at >= ? ORDER BY started_at DESC LIMIT ?",
            (stream, since, limit)).fetchall()
    return [dict(r) for r in rows]


def transcript_text(db_path: str, stream: str, minutes: int, now: datetime | None = None) -> str:
    rows = recent_transcripts(db_path, stream, minutes, limit=500, now=now)
    return " ".join(r["text"] for r in reversed(rows))


def prune(db_path: str, weather_days: int = 2, scanner_days: int = 14,
          conditions_days: int = 30, items_days: int = 90) -> None:
    """Transcripts are bulk; the weather one in particular repeats every few minutes and
    is worthless after a day. Items and conditions are small and are the history."""
    now = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute("DELETE FROM radio_transcripts WHERE stream = ? AND started_at < ?",
                     (STREAM_WEATHER, _iso(now - timedelta(days=weather_days))))
        conn.execute("DELETE FROM radio_transcripts WHERE stream = ? AND started_at < ?",
                     (STREAM_SCANNER, _iso(now - timedelta(days=scanner_days))))
        conn.execute("DELETE FROM weather_conditions WHERE at < ?",
                     (_iso(now - timedelta(days=conditions_days)),))
        conn.execute("DELETE FROM radio_items WHERE at < ?",
                     (_iso(now - timedelta(days=items_days)),))
        conn.commit()


# --- items (the "worth knowing" table) --------------------------------------------

def add_item(db_path: str, key: str, kind: str, severity: str, title: str, body: str | None = None,
             stream: str | None = None, at: datetime | None = None, meta: dict | None = None) -> bool:
    """Insert if the key is new. Returns True when a row was created -- the caller's
    signal that this is a genuinely new thing, not the third repeat of a SAME header."""
    if severity not in SEVERITIES:
        severity = "notice"
    ts = _iso(at or _now())
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO radio_items (key, kind, severity, stream, at, title, body, meta, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (key, kind, severity, stream, ts, title, body,
             json.dumps(meta, separators=(",", ":")) if meta else None, _iso(_now())))
        conn.commit()
        return cur.rowcount == 1


# --- weather hazards -------------------------------------------------------------
#
# The hazard line comes back from a local model reading a twelve-minute transcript, so
# one STANDING hazard is worded differently on every single pass: "Coastal Flood watches
# have been issued for a large majority of the tidal area", then "Coastal flood watch for
# a large majority of the tidal area starting Wednesday". Keying an item on a hash of
# that sentence makes every rewording a brand-new hazard, which is how a single coastal
# flood watch became nineteen texts in three hours. So dedupe on what the sentence is
# ABOUT, not on how this pass happened to word it.

# Words that carry phrasing rather than substance. Weekdays and "issued" are in here on
# purpose: "watch issued Wednesday" and "watch in effect through Thursday" are the same
# watch being described from two points in the same broadcast.
_HAZARD_NOISE = frozenset("""
a an and are as at be been by for from had has have in into is it its of on or that the
this to until with will was were through starting start begin begins beginning effect
expected area time today tonight tomorrow morning afternoon evening night late early
these those large majority issue issued issues remain remains continue continues
monday tuesday wednesday thursday friday saturday sunday
""".split())

# "No hazardous weather is expected at this time" is an all-clear. It was reaching his
# phone as a hazard notice because the only emptiness check was the literal strings
# "none", "null" and "n/a".
_NO_HAZARD = re.compile(
    r"\bno\s+(?:hazardous|significant|hazards?)\b"
    r"|\bno\s+\w+\s+(?:weather|hazards?)\s+(?:is|are)\s+expected"
    r"|\bnot\s+expected\b"
    r"|\bnone\s+expected\b", re.I)


def hazard_is_real(hazards: str | None) -> bool:
    """False when the forecast is saying there is nothing wrong."""
    text = (hazards or "").strip()
    if not text or text.lower().rstrip(".") in ("none", "null", "n/a", "na", "-"):
        return False
    return not _NO_HAZARD.search(text)


def _stem(word: str) -> str:
    if word.endswith("es") and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    return word


def hazard_signature(hazards: str | None) -> list[str]:
    """The meaningful words: what the hazard is about, minus how it was phrased.

    Sorted rather than a set so it round-trips through JSON into an item's meta, where
    the next pass reads it back to compare against.
    """
    words = re.findall(r"[a-z]+", (hazards or "").lower())
    return sorted({_stem(w) for w in words if len(w) > 2 and _stem(w) not in _HAZARD_NOISE})


# Overlap at which two hazard lines are "the same hazard, said differently". Tuned
# against the real duplicates: the flood-watch rewordings score 0.75-1.0.
HAZARD_SAME_RATIO = 0.6

# The NWS severity ladder. A watch becoming a WARNING is the single most important thing
# the weather radio can say, and word overlap does not protect it on its own: "Coastal
# Flood Watch for the tidal area" and "Coastal Flood Warning for the tidal area" differ
# by one token and score 0.6, dead on the threshold. So the level is compared first and
# exactly, and a hazard at a different level is never a repeat of one already filed.
HAZARD_LEVELS = frozenset((
    "warning", "watch", "advisory", "advisori", "emergency", "statement", "outlook"))

# What the hazard actually IS, separated from the words that decorate it. Comparing whole
# sentences cannot do this: across three passes the same coastal flood watch arrived as
# four words, as twelve with the tide detail, and once with a Gale watch added -- the
# first two are one hazard said twice, the third is genuine news. Both readings are
# invisible to word overlap and obvious once you ask which hazards are named.
HAZARD_KINDS = {
    "flood": "flood", "flooding": "flood",
    "gale": "gale", "tornado": "tornado", "hurricane": "tropical", "tropical": "tropical",
    "thunderstorm": "thunderstorm", "storm": "thunderstorm", "hail": "thunderstorm",
    "wind": "wind", "gust": "wind",
    "heat": "heat", "freeze": "freeze", "frost": "freeze", "cold": "freeze",
    "snow": "winter", "ice": "winter", "winter": "winter", "sleet": "winter",
    "blizzard": "winter", "fog": "fog", "surf": "surf", "rip": "surf",
    "fire": "fire", "smoke": "fire", "marine": "marine", "smallcraft": "marine",
    "drought": "drought", "dust": "dust", "avalanche": "avalanche",
}


def _levels(signature) -> frozenset:
    return frozenset(w for w in signature if w in HAZARD_LEVELS)


def _kinds(signature) -> frozenset:
    return frozenset(HAZARD_KINDS[w] for w in signature if w in HAZARD_KINDS)


def hazard_already_filed(db_path: str, hazards: str, hours: float = 12,
                         now: datetime | None = None) -> bool:
    """Whether this hazard is one already filed recently, however it is worded now.

    Quiet only when there is genuinely nothing new: same severity level, and every
    hazard named here was already named. A line that adds a hazard always gets through,
    even if it repeats one he has heard -- "flood watch, and now a gale watch too" is
    news, and the flood half being old does not make the gale half old.
    """
    sig = set(hazard_signature(hazards))
    if not sig:
        return True
    level, kind = _levels(sig), _kinds(sig)
    for it in recent_items(db_path, hours=hours, kinds=("weather_hazard",),
                           limit=50, now=now):
        prior = set(it["meta"].get("signature") or ())
        if not prior or _levels(prior) != level:
            continue
        if kind:
            # Named hazards are the reliable signal; use them and ignore the prose.
            if kind <= _kinds(prior):
                return True
        elif len(sig & prior) / len(sig | prior) >= HAZARD_SAME_RATIO:
            # Nothing recognisable named, so fall back to how much wording they share.
            return True
    return False


def recent_items(db_path: str, hours: float = 24, kinds: tuple[str, ...] | None = None,
                 limit: int = 50, now: datetime | None = None) -> list[dict]:
    since = _iso((now or _now()) - timedelta(hours=hours))
    sql = "SELECT * FROM radio_items WHERE at >= ?"
    params: list = [since]
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        params.extend(kinds)
    sql += " ORDER BY at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_item_dict(r) for r in rows]


def _item_dict(r) -> dict:
    d = dict(r)
    try:
        d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
    except (TypeError, ValueError):
        d["meta"] = {}
    return d


def active_eas(db_path: str, now: datetime | None = None) -> list[dict]:
    """EAS warnings/watches that have not expired (SAME carries a purge time)."""
    now = now or _now()
    out = []
    for it in recent_items(db_path, hours=12, kinds=("eas",), limit=50, now=now):
        exp = _parse(it["meta"].get("expires_at"))
        if exp is None or exp > now:
            out.append(it)
    return out


def pending_notifications(db_path: str) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM radio_items WHERE notified_at IS NULL ORDER BY at ASC LIMIT 100").fetchall()
    return [_item_dict(r) for r in rows]


def mark_notified(db_path: str, ids: list[int], now: datetime | None = None) -> None:
    if not ids:
        return
    ts = _iso(now or _now())
    with closing(_connect(db_path)) as conn:
        conn.executemany("UPDATE radio_items SET notified_at = ? WHERE id = ?",
                         [(ts, i) for i in ids])
        conn.commit()


def format_item(it: dict) -> str:
    prefix = {"urgent": "URGENT: ", "notice": "", "info": "FYI: "}.get(it["severity"], "")
    body = f" {it['body']}" if it.get("body") else ""
    return f"{prefix}{it['title']}.{body}".strip()


def deliver_pending(db_path: str, notify, now: datetime | None = None) -> int:
    """Send what is waiting and mark it sent. Called from JarvisCore's scheduler.

    Urgent items go one per message, immediately -- a tornado warning must not sit
    behind a digest. Notices are batched into one message per tick so a busy scanner
    hour is one text, not twelve. Info items are never pushed; they are for the
    Command Center and for the owner asking, so they are marked as handled here rather
    than left to pile up as "pending" forever. Returns the number of messages sent.

    An item older than a day that was never delivered (the service was down) is marked
    without sending: a stale warning pushed as if new is worse than none.
    """
    now = now or _now()
    items = pending_notifications(db_path)
    if not items:
        return 0
    sent = 0
    done: list[int] = []
    batch: list[dict] = []
    for it in items:
        at = _parse(it["at"]) or now
        if now - at > timedelta(hours=24) or it["severity"] == "info":
            done.append(it["id"])
            continue
        if it["severity"] == "urgent":
            try:
                notify(format_item(it))
                sent += 1
                done.append(it["id"])
            except Exception:
                logger.exception("radio: urgent notification failed for item %s", it["id"])
            continue
        batch.append(it)
    if batch:
        text = "\n".join(format_item(it) for it in batch)
        if len(batch) > 1:
            text = f"Radio watch, {len(batch)} items:\n" + text
        try:
            notify(text)
            sent += 1
            done.extend(it["id"] for it in batch)
        except Exception:
            logger.exception("radio: batched notification failed")
    mark_notified(db_path, done, now)
    return sent


# --- weather conditions --------------------------------------------------------------

_COND_FIELDS = ("temperature_f", "humidity_pct", "wind", "pressure_in", "pressure_trend", "sky",
                "heat_index_f", "summary", "hazards", "forecast")


def record_conditions(db_path: str, cond: dict, source: str = "noaa", at: datetime | None = None) -> int:
    vals = [_num(cond.get("temperature_f")), _num(cond.get("humidity_pct")), _s(cond.get("wind")),
            _num(cond.get("pressure_in")), _s(cond.get("pressure_trend")), _s(cond.get("sky")),
            _num(cond.get("heat_index_f")), _s(cond.get("summary")), _s(cond.get("hazards")),
            _s(cond.get("forecast"))]
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO weather_conditions (at, source, " + ", ".join(_COND_FIELDS) + ", raw) "
            "VALUES (?,?," + ",".join("?" * len(_COND_FIELDS)) + ",?)",
            [_iso(at or _now()), source, *vals, json.dumps(cond, separators=(",", ":"))])
        conn.commit()
        return cur.lastrowid


def latest_conditions(db_path: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM weather_conditions ORDER BY at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        m = re.search(r"-?\d+(\.\d+)?", str(v))
        return float(m.group(0)) if m else None


def _s(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = "; ".join(str(x) for x in v if x)
    v = str(v).strip()
    return v or None


# --- state / feed health ---------------------------------------------------------------

def set_state(db_path: str, key: str, value, now: datetime | None = None) -> None:
    if not isinstance(value, str):
        value = json.dumps(value, separators=(",", ":"))
    with closing(_connect(db_path)) as conn:
        conn.execute("INSERT INTO radio_state (key, value, updated_at) VALUES (?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (key, value, _iso(now or _now())))
        conn.commit()


def get_state(db_path: str, key: str) -> tuple[str | None, datetime | None]:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT value, updated_at FROM radio_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None, None
    return row["value"], _parse(row["updated_at"])


def get_state_json(db_path: str, key: str):
    value, _ = get_state(db_path, key)
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def _age(db_path: str, key: str, now: datetime) -> int | None:
    _, at = get_state(db_path, key)
    return int((now - at).total_seconds()) if at else None


def feed_status(db_path: str, now: datetime | None = None) -> dict:
    now = now or _now()
    ages = {
        "weather_audio_age_s": _age(db_path, f"audio:{STREAM_WEATHER}", now),
        "scanner_audio_age_s": _age(db_path, f"audio:{STREAM_SCANNER}", now),
        "eas_poll_age_s": _age(db_path, "poll:eas", now),
        "rf_poll_age_s": _age(db_path, "poll:rf", now),
    }

    def stale(age, limit):
        return age is None or age > limit

    return {
        **ages,
        "weather_stale": stale(ages["weather_audio_age_s"], AUDIO_STALE_SECONDS),
        "scanner_stale": stale(ages["scanner_audio_age_s"], AUDIO_STALE_SECONDS),
        "eas_stale": stale(ages["eas_poll_age_s"], EAS_POLL_STALE_SECONDS),
        "rf_stale": stale(ages["rf_poll_age_s"], RF_POLL_STALE_SECONDS),
    }


def rf_report(db_path: str) -> dict | None:
    return get_state_json(db_path, "rf_baseline")


# --- LLM prompts + parsing (no model in this module; the worker supplies one) --------

def parse_json_object(text: str | None) -> dict | None:
    """Models wrap JSON in prose or a fence even when told not to; take the first {...}.
    None on anything unexpected -- silence is the safe failure, never a fabricated alert."""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _content(msg) -> str | None:
    """The ollama client returns a Message model, not a dict (it happens to support .get,
    but an isinstance(dict) guard silently threw every real reply away -- found live)."""
    if msg is None:
        return None
    if isinstance(msg, dict):
        return msg.get("content")
    return getattr(msg, "content", None)


CONDITIONS_SYSTEM = (
    "You read transcripts of a NOAA Weather Radio broadcast (an automated voice; expect "
    "transcription errors like 'Pan over' for Hanover or 'Chester Field' for Chesterfield). "
    "Extract the CURRENT conditions for the primary station (Richmond) and the short-range "
    "forecast. Reply with ONE JSON object and nothing else, keys: temperature_f (number or null), "
    "humidity_pct (number or null), wind (string or null, e.g. 'north 12 mph'), pressure_in "
    "(number or null), pressure_trend ('rising'|'falling'|'steady'|null), sky (string or null), "
    "heat_index_f (number or null), summary (one plain sentence of the current conditions), "
    "hazards (string: any warning, watch, advisory or hazardous weather outlook mentioned, "
    "verbatim-ish, or empty string if none), forecast (one or two sentences for the next 24 h "
    "or empty string). Never invent a value that is not in the transcript; use null."
)

SCANNER_SYSTEM = (
    "You triage one transmission from a Richmond, Virginia Fire/EMS radio scanner for a "
    "homeowner. The transcript is short and noisy. Decide whether it is something the "
    "homeowner would want to be told about: structure fires, serious crashes, hazmat, "
    "shootings or stabbings, missing persons, evacuations, road closures, severe weather "
    "reports, anything on or near the streets listed as HOME AREA, or anything unusually large "
    "(multiple alarms, mass casualty). Routine dispatches, radio checks, unit status, and "
    "garbled fragments are NOT important. Reply with ONE JSON object and nothing else, keys: "
    "important (true|false), severity ('urgent' only for immediate danger near the home area, "
    "'notice' for things worth a heads-up, 'info' otherwise), category (fire|ems|police|"
    "weather|traffic|hazmat|other), summary (one plain sentence), location (string or null)."
)


def extract_conditions(llm, transcript: str, home_area: str = "") -> dict | None:
    if not transcript or len(transcript) < 80:
        return None
    try:
        msg = llm.chat(messages=[
            {"role": "system", "content": CONDITIONS_SYSTEM},
            {"role": "user", "content": f"TRANSCRIPT:\n{transcript[-6000:]}"},
        ])
    except Exception:
        logger.exception("radio: conditions extraction failed")
        return None
    parsed = parse_json_object(_content(msg))
    if not parsed:
        return None
    if all(parsed.get(k) in (None, "", []) for k in ("temperature_f", "summary", "sky", "hazards")):
        return None
    return parsed


def classify_scanner(llm, text: str, home_area: str = "") -> dict | None:
    if not text or len(text.split()) < 3:
        return None
    try:
        msg = llm.chat(messages=[
            {"role": "system", "content": SCANNER_SYSTEM},
            {"role": "user", "content": f"HOME AREA: {home_area or '(not specified)'}\nTRANSMISSION: {text[:1500]}"},
        ])
    except Exception:
        logger.exception("radio: scanner classification failed")
        return None
    parsed = parse_json_object(_content(msg))
    if not parsed or not isinstance(parsed.get("important"), bool):
        return None
    if parsed.get("severity") not in SEVERITIES:
        parsed["severity"] = "info"
    return parsed


# --- the employee feed ----------------------------------------------------------------

def _fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    return f"{seconds / 3600:.1f}h ago"


def briefing(db_path: str, now: datetime | None = None) -> str:
    """Pre-digested text for an employee with 'radio' in its data_feeds. Health first --
    an employee must be able to tell a quiet night from a dead radio."""
    now = now or _now()
    st = feed_status(db_path, now)
    lines = ["RADIO / RF AWARENESS FEED:"]
    health = []
    for name, stale, age in (("weather stream", st["weather_stale"], st["weather_audio_age_s"]),
                             ("scanner stream", st["scanner_stale"], st["scanner_audio_age_s"]),
                             ("EAS decoder", st["eas_stale"], st["eas_poll_age_s"]),
                             ("RF sensor node", st["rf_stale"], st["rf_poll_age_s"])):
        health.append(f"{name} {'STALE' if stale else 'ok'} ({_fmt_age(age)})")
    lines.append("  health: " + "; ".join(health))
    if all((st["weather_stale"], st["scanner_stale"], st["eas_stale"], st["rf_stale"])):
        lines.append("  Every feed is stale: the radio worker is not running. Do NOT describe "
                     "conditions or say the area is quiet; say the radio feed is down.")
        return "\n".join(lines)

    cond = latest_conditions(db_path)
    if cond and not st["weather_stale"]:
        age = _age_of(cond["at"], now)
        parts = [f"NOAA current conditions ({_fmt_age(age)}): {cond.get('summary') or ''}".rstrip()]
        if cond.get("hazards"):
            parts.append(f"  hazards mentioned: {cond['hazards']}")
        if cond.get("forecast"):
            parts.append(f"  forecast: {cond['forecast']}")
        lines.extend("  " + p for p in parts)
    eas = active_eas(db_path, now)
    if eas:
        lines.append("  ACTIVE EAS ALERTS: " + "; ".join(
            f"{e['title']} ({'affects home counties' if e['meta'].get('affects_me') else 'elsewhere'})"
            for e in eas))
    flagged = recent_items(db_path, hours=6, kinds=("scanner",), limit=8, now=now)
    if flagged:
        lines.append("  scanner, flagged in the last 6h:")
        lines.extend(f"    {f['at'][11:16]}Z [{f['severity']}] {f['title']}" for f in flagged)
    elif not st["scanner_stale"]:
        n = len(recent_transcripts(db_path, STREAM_SCANNER, minutes=360, now=now))
        lines.append(f"  scanner: {n} transmission(s) heard in the last 6h, none flagged.")
    rep = rf_report(db_path)
    if rep and not st["rf_stale"]:
        c = rep.get("counts", {})
        t = rep.get("traffic", {})
        lines.append(f"  RF sensor node: {c.get('own', 0)} own sensors, "
                     f"{c.get('fixture-neighbour', 0)} neighbour fixtures, vehicles regular "
                     f"{c.get('vehicle-regular', 0)} / passing {c.get('vehicle-passing', 0)} / "
                     f"WATCH {c.get('vehicle-watch', 0)}; traffic {t.get('vehicle_visits_last_24h', 0)} "
                     f"visits last 24h vs {t.get('vehicle_visits_per_day', 0)}/day baseline.")
        for a in rep.get("alerts", [])[:5]:
            lines.append(f"    [{a.get('severity')}] {a.get('text')}")
    return "\n".join(lines)


def _age_of(iso: str, now: datetime) -> int | None:
    dt = _parse(iso)
    return int((now - dt).total_seconds()) if dt else None
