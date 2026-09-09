"""Camera watching: who is in a room, and whether the movement is the dog.

The point of this module is house logic, not surveillance footage. It answers three
questions — is anyone there, who is it, and was that motion a person or a pet — and emits
those as events. Video is not kept unless a recording rule says to, and the default rule
is "no".

Three design decisions worth knowing:

**Motion gates everything.** Frame differencing on a downscaled grayscale image costs
microseconds; a detector costs tens of milliseconds. Running the detector continuously on
an empty room is what makes these systems hot, slow and useless, so nothing runs until
the pixels actually change.

**Cameras are abstract from the first line.** Today they are MJPEG endpoints served by
uStreamer on the Pi terminals. RTSP cameras are coming, and adding one should be a config
entry rather than a rewrite, so everything downstream works on frames and never knows
where they came from.

**Identity is a separate, later stage.** Detection says "a person"; recognition says
"which person", and only runs when there is a face to look at. An unknown face becomes a
question for the owner rather than a guess, because a confidently wrong name is worse
than an honest "I don't know who that is".

A fourth thing, added alongside the Room Presence & Identity gating work: **a terminal is
not a camera**. `terminal_cameras` maps a voice terminal's device_id (touch1, laptop1, ...)
to the camera_key that watches the room it sits in, so presence.py can ask "who does the
camera near THIS terminal currently see" without vision.py needing to know terminals
exist at all beyond that one lookup table.
"""
import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    -- 'mjpeg' (uStreamer on a terminal) or 'rtsp' (the cameras being added later).
    kind TEXT NOT NULL DEFAULT 'mjpeg' CHECK (kind IN ('mjpeg', 'rtsp')),
    url TEXT NOT NULL,
    location TEXT,
    -- Rooms differ: a hallway camera watching a doorway needs a lower motion threshold
    -- than a kitchen one pointed at a window with trees behind it.
    motion_threshold REAL NOT NULL DEFAULT 0.012,
    enabled INTEGER NOT NULL DEFAULT 1,
    -- Whether this camera may ever record, independent of the situational rules. A
    -- bedroom can be left permanently unable to record no matter what else is true.
    recordable INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- What was seen, and when. One row per meaningful change, not per frame -- a camera
-- watching an empty room all night should produce nothing at all.
CREATE TABLE IF NOT EXISTS vision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_key TEXT NOT NULL,
    kind TEXT NOT NULL,          -- motion | person | pet | identified | unknown_person | cleared
    label TEXT,                  -- person / dog / cat, or the identified name
    confidence REAL,
    person_key TEXT,             -- set when identity is known
    thumbnail_path TEXT,
    detail TEXT,
    at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vision_events_cam ON vision_events(camera_key, id DESC);
CREATE INDEX IF NOT EXISTS idx_vision_events_kind ON vision_events(kind, id DESC);

-- People the house knows, and the face embeddings that identify them. Enrolled by the
-- owner answering "who is this?", never inferred.
CREATE TABLE IF NOT EXISTS known_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    relationship TEXT,           -- household | family | friend | service | other
    -- JSON list of embeddings. More than one because a face at the door in daylight and
    -- the same face indoors at night are far apart in embedding space.
    embeddings TEXT NOT NULL DEFAULT '[]',
    sample_count INTEGER NOT NULL DEFAULT 0,
    -- Whether this person's presence should suppress or enable recording; the owner's
    -- policy is per-person, not global.
    recording_preference TEXT NOT NULL DEFAULT 'inherit'
        CHECK (recording_preference IN ('inherit', 'never', 'always')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Faces seen that nobody has named yet. These become the question Jarvis asks.
CREATE TABLE IF NOT EXISTS unknown_faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_key TEXT NOT NULL,
    embedding TEXT NOT NULL,
    thumbnail_path TEXT,
    asked INTEGER NOT NULL DEFAULT 0,
    resolved_person_key TEXT,
    seen_count INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

-- Which camera watches the room a given voice terminal sits in. One row per terminal
-- that has camera coverage at all -- a terminal absent from this table simply has no
-- way to confirm anyone's identity, which presence.py treats as "not confirmed" rather
-- than an error. Day one this is touch1 and laptop1; adding terminal #3 later is one
-- more INSERT, not a schema change.
CREATE TABLE IF NOT EXISTS terminal_cameras (
    device_id TEXT PRIMARY KEY,
    camera_key TEXT NOT NULL REFERENCES cameras(key),
    created_at TEXT NOT NULL
);
"""


def init_vision_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE migrations for columns added after the initial
    CREATE TABLE IF NOT EXISTS — same discipline as db.py's own _migrate."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(known_people)")}
    if "access_level" not in cols:
        # No CHECK constraint on the ALTER itself -- SQLite's ADD COLUMN doesn't reliably
        # support one, and db.py's own migration history (see its docstring) already
        # chose "validate in the Python layer instead" for exactly this reason. Enforced
        # by set_person_access_level below.
        conn.execute(
            "ALTER TABLE known_people ADD COLUMN access_level TEXT NOT NULL DEFAULT 'guest'"
        )


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Valid known_people.access_level values -- what presence.py's authorization check reads.
# 'owner': the account holder(s); 'household': lives here but not automatically trusted
# with e.g. the crypto account; 'guest': recognised (so Jarvis can greet them by name) but
# never gets personal/financial information. New enrollments default to 'guest' -- being
# recognised is not the same as being authorized, and that has to be true even for a
# familiar face until the owner explicitly says otherwise.
ACCESS_LEVELS = ("owner", "household", "guest")


# --- cameras ------------------------------------------------------------------

def add_camera(db_path: str, key: str, name: str, url: str, kind: str = "mjpeg",
               location: str = "", motion_threshold: float = 0.012,
               recordable: bool = True) -> dict:
    if kind not in ("mjpeg", "rtsp"):
        raise ValueError("kind must be mjpeg or rtsp")
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO cameras (key, name, kind, url, location, motion_threshold,
                                    enabled, recordable, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   name = excluded.name, kind = excluded.kind, url = excluded.url,
                   location = excluded.location,
                   motion_threshold = excluded.motion_threshold,
                   recordable = excluded.recordable""",
            (key, name, kind, url, location, motion_threshold,
             1 if recordable else 0, _now()))
        conn.commit()
        return dict(conn.execute("SELECT * FROM cameras WHERE key = ?", (key,)).fetchone())


def list_cameras(db_path: str, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM cameras"
    if enabled_only:
        sql += " WHERE enabled = 1"
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY key")]


# --- terminal <-> camera mapping ------------------------------------------------

def set_terminal_camera(db_path: str, device_id: str, camera_key: str) -> None:
    """Declares that `camera_key` watches the room voice terminal `device_id` sits in.

    Config-driven at startup (see vision_main.py/web_main.py), the same way cfg.users
    seeds the users table -- so day-one coverage (touch1, laptop1) is a config entry,
    and terminal #3 later is one more line there, not a migration.
    """
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO terminal_cameras (device_id, camera_key, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(device_id) DO UPDATE SET camera_key = excluded.camera_key""",
            (device_id, camera_key, _now()),
        )
        conn.commit()


def get_terminal_camera(db_path: str, device_id: str) -> str | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT camera_key FROM terminal_cameras WHERE device_id = ?", (device_id,)
        ).fetchone()
        return row["camera_key"] if row else None


def list_terminal_cameras(db_path: str) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM terminal_cameras ORDER BY device_id")]


# --- known people (identity enrollment) -----------------------------------------

def enroll_known_person(db_path: str, key: str, name: str, relationship: str | None = None,
                        access_level: str = "guest", embedding: list | None = None) -> dict:
    """Creates or updates a known person. Never inferred -- this is the "who is this?"
    answer, given by the owner, per vision.py's module docstring."""
    if access_level not in ACCESS_LEVELS:
        raise ValueError(f"access_level must be one of {ACCESS_LEVELS}")
    embeddings_json = json.dumps([embedding] if embedding is not None else [])
    now = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO known_people
                   (key, name, relationship, embeddings, sample_count, access_level,
                    created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   name = excluded.name, relationship = excluded.relationship,
                   updated_at = excluded.updated_at""",
            (key, name, relationship, embeddings_json, 1 if embedding is not None else 0,
             access_level, now, now),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM known_people WHERE key = ?", (key,)).fetchone())


def add_face_embedding_sample(db_path: str, key: str, embedding: list, max_samples: int = 12) -> bool:
    """Appends one more embedding sample for an already-enrolled person.

    Capped rather than unbounded: a face at the door in daylight and the same face
    indoors at night are the variation multiple samples exist to cover, not every frame
    a camera has ever seen them in. Oldest sample drops first once the cap is hit, which
    keeps the set representative of recent appearance (haircuts, glasses) rather than
    anchored to the day they were enrolled.
    """
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT embeddings FROM known_people WHERE key = ?", (key,)).fetchone()
        if row is None:
            return False
        samples = json.loads(row["embeddings"] or "[]")
        samples.append(embedding)
        samples = samples[-max_samples:]
        conn.execute(
            """UPDATE known_people SET embeddings = ?, sample_count = ?, updated_at = ?
               WHERE key = ?""",
            (json.dumps(samples), len(samples), _now(), key),
        )
        conn.commit()
        return True


def get_known_person(db_path: str, key: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM known_people WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None


def list_known_people(db_path: str) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM known_people ORDER BY name")]


def set_person_access_level(db_path: str, key: str, access_level: str) -> bool:
    """The owner's explicit call on what a recognised face is allowed to hear -- being
    enrolled (recognised by name) and being authorized (access_level='owner' or
    'household') are deliberately different questions; see ACCESS_LEVELS."""
    if access_level not in ACCESS_LEVELS:
        raise ValueError(f"access_level must be one of {ACCESS_LEVELS}")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE known_people SET access_level = ?, updated_at = ? WHERE key = ?",
            (access_level, _now(), key),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_known_person(db_path: str, key: str) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute("DELETE FROM known_people WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0


# --- unknown faces (the "who is this?" queue) -----------------------------------

def record_unknown_face(db_path: str, camera_key: str, embedding: list,
                        thumbnail_path: str = "") -> int:
    """Logs a face nobody could match. Always inserts a new row rather than trying to
    dedupe against previous unknown sightings by embedding similarity here -- that
    matching judgment belongs in assistant/core/identity.py (the same place that already
    does it for known_people), not duplicated with a second, looser threshold in here.
    A management UI can merge/resolve these; see resolve_unknown_face."""
    now = _now()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO unknown_faces (camera_key, embedding, thumbnail_path,
                                          seen_count, first_seen, last_seen)
               VALUES (?, ?, ?, 1, ?, ?)""",
            (camera_key, json.dumps(embedding), thumbnail_path or None, now, now),
        )
        conn.commit()
        return cur.lastrowid


def list_unknown_faces(db_path: str, unresolved_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM unknown_faces"
    if unresolved_only:
        sql += " WHERE resolved_person_key IS NULL"
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY last_seen DESC")]


def mark_unknown_face_asked(db_path: str, face_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE unknown_faces SET asked = 1 WHERE id = ?", (face_id,))
        conn.commit()


def resolve_unknown_face(db_path: str, face_id: int, person_key: str) -> None:
    """The owner answered "who is this?" -- links the sighting to a known person. Does
    NOT itself add the embedding to known_people; call add_face_embedding_sample with the
    same embedding if the caller wants this sighting to improve future matching."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE unknown_faces SET resolved_person_key = ? WHERE id = ?",
            (person_key, face_id),
        )
        conn.commit()


# --- frame sources ------------------------------------------------------------

class FrameSource:
    """A camera, reduced to 'give me the current frame as a numpy array'.

    Deliberately pull-based rather than a callback: the motion gate decides how often it
    wants a frame, and a source that pushes at the camera's rate would put us back to
    processing 30fps of an empty room.
    """

    def __init__(self, camera: dict):
        self.key = camera["key"]
        self.kind = camera["kind"]
        self.url = camera["url"]

    def snapshot(self, timeout: float = 5.0) -> np.ndarray | None:
        if self.kind == "mjpeg":
            return self._mjpeg_snapshot(timeout)
        return self._rtsp_snapshot(timeout)

    def _mjpeg_snapshot(self, timeout: float) -> np.ndarray | None:
        """uStreamer exposes /snapshot, which hands back one JPEG. Far cheaper than
        holding the MJPEG stream open and discarding almost every frame."""
        import urllib.request
        url = self.url.rstrip("/")
        if not url.endswith("/snapshot"):
            url += "/snapshot"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = resp.read()
        except Exception:
            return None
        return decode_jpeg(data)

    def _rtsp_snapshot(self, timeout: float) -> np.ndarray | None:
        """RTSP needs a decoder; OpenCV is the pragmatic one. Opening a connection per
        frame is wasteful, so real RTSP support should hold the capture open -- this is
        the honest placeholder until the first RTSP camera actually exists to test on.

        Also doubles as the path for a *local* camera (a built-in/USB webcam, as on
        laptop1): cv2.VideoCapture happily accepts an integer device index wherever it
        accepts an RTSP URL string, so a camera row whose url is "0" opens the laptop's
        own webcam rather than a network stream. No separate 'kind' was worth adding for
        that -- OpenCV already treats both the same way.
        """
        try:
            import cv2
        except ImportError:
            return None
        source = int(self.url) if str(self.url).lstrip("-").isdigit() else self.url
        cap = cv2.VideoCapture(source)
        try:
            ok, frame = cap.read()
            return frame[:, :, ::-1].copy() if ok else None      # BGR -> RGB
        finally:
            cap.release()


def decode_jpeg(data: bytes) -> np.ndarray | None:
    """JPEG bytes -> RGB uint8 array, using whichever decoder is available."""
    try:
        import cv2
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        return None if arr is None else arr[:, :, ::-1].copy()
    except ImportError:
        pass
    try:
        import io

        from PIL import Image
        return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    except Exception:
        return None


# --- motion -------------------------------------------------------------------

@dataclass
class MotionGate:
    """Decides whether a frame is worth showing a detector.

    Frame differencing on a small grayscale image: a few hundred microseconds against
    tens of milliseconds for detection. The threshold is per-camera because a hallway and
    a window with trees behind it are not the same problem.

    `cooldown_frames` keeps the gate open briefly after motion stops, so someone standing
    still mid-frame doesn't flicker between present and absent.
    """
    threshold: float = 0.012
    downscale: int = 8
    cooldown_frames: int = 6
    _prev: np.ndarray | None = field(default=None, repr=False)
    _cooldown: int = 0

    def update(self, frame: np.ndarray) -> tuple[bool, float]:
        small = frame[::self.downscale, ::self.downscale]
        gray = small.mean(axis=2).astype(np.float32) / 255.0
        if self._prev is None:
            self._prev = gray
            return False, 0.0
        score = float(np.abs(gray - self._prev).mean())
        self._prev = gray
        if score >= self.threshold:
            self._cooldown = self.cooldown_frames
            return True, score
        if self._cooldown > 0:
            self._cooldown -= 1
            return True, score
        return False, score


# --- events -------------------------------------------------------------------

def record_event(db_path: str, camera_key: str, kind: str, label: str = "",
                 confidence: float = 0.0, person_key: str = "",
                 thumbnail_path: str = "", detail: str = "") -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO vision_events
                   (camera_key, kind, label, confidence, person_key,
                    thumbnail_path, detail, at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (camera_key, kind, label or None, confidence, person_key or None,
             thumbnail_path or None, detail or None, _now()))
        conn.commit()
        return cur.lastrowid


def recent_events(db_path: str, camera_key: str = "", kind: str = "",
                  limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM vision_events WHERE 1=1"
    params: list = []
    if camera_key:
        sql += " AND camera_key = ?"
        params.append(camera_key)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def presence_now(db_path: str, within_seconds: int = 120) -> dict:
    """Who and what each camera has seen recently -- the query house logic actually asks.

    Derived from the event log rather than kept as separate mutable state, so it cannot
    drift out of step with what was actually observed.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_seconds)).isoformat()
    out: dict[str, dict] = {}
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT camera_key, kind, label, person_key, at FROM vision_events
                WHERE at >= ? AND kind IN ('person', 'pet', 'identified', 'unknown_person')
                ORDER BY id""", (cutoff,))
        for r in rows:
            cam = out.setdefault(r["camera_key"], {
                "person": False, "pet": False, "people": [], "unknown_people": 0,
                "last_seen": None})
            cam["last_seen"] = r["at"]
            if r["kind"] == "pet":
                cam["pet"] = True
            else:
                cam["person"] = True
            if r["kind"] == "identified" and r["person_key"]:
                if r["person_key"] not in cam["people"]:
                    cam["people"].append(r["person_key"])
            if r["kind"] == "unknown_person":
                cam["unknown_people"] += 1
    return out


# --- identity resolution (what presence.py's gating decision reads) -------------

def identity_on_camera(db_path: str, camera_key: str, within_seconds: int = 45) -> dict | None:
    """The known_people row for whoever camera_key *currently* shows, or None.

    "Currently" is deliberately about the single most recent relevant event, not "was
    anyone matching seen at all in the window": keyed off whichever of
    identified/unknown_person/cleared happened last, so someone identified two minutes
    ago who has since been replaced by a stranger (or by an empty room, via a 'cleared'
    event from vision_worker.py) correctly reads as unconfirmed on the very next call,
    rather than lingering as "identified" until the whole window quietly expires.

    Returns None (never raises) for: no camera coverage's worth of events yet, the latest
    signal being unknown_person or cleared, or a stale identified event that has aged out
    of within_seconds -- presence.py treats all of these identically, as "not confirmed".
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_seconds)).isoformat()
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT kind, person_key FROM vision_events
               WHERE camera_key = ? AND at >= ?
                 AND kind IN ('identified', 'unknown_person', 'cleared')
               ORDER BY id DESC LIMIT 1""",
            (camera_key, cutoff),
        ).fetchone()
        if row is None or row["kind"] != "identified" or not row["person_key"]:
            return None
        person = conn.execute(
            "SELECT * FROM known_people WHERE key = ?", (row["person_key"],)
        ).fetchone()
        return dict(person) if person else None
