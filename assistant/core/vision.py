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
than an honest "I don't know who that is". (Chunk 2: that stage is face_recognizer.py +
camera_watch.py + identity_gate.py, and the known_people/unknown_faces functions below.)
"""
import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone

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
"""


def init_vision_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate_vision(conn)
        conn.commit()


def _migrate_vision(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE migrations for columns added after the initial CREATE
    TABLE IF NOT EXISTS — safe to call on every startup, on a fresh or already-populated
    database. Same convention as db.py's own _migrate."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(cameras)")}
    if "kiosk_device_id" not in existing:
        # Which voice terminal (device_id from device.json) this camera watches, if any.
        # NULL means "just a house camera" -- presence/pet detection only, never face
        # recognition. This is the concrete mechanism behind the kiosk-only-cameras
        # locked decision: identity_gate.py and camera_watch.py both key off this column,
        # never off camera location/name/heuristics.
        conn.execute("ALTER TABLE cameras ADD COLUMN kiosk_device_id TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cameras_kiosk_device "
            "ON cameras(kiosk_device_id) WHERE kiosk_device_id IS NOT NULL"
        )


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def get_camera(db_path: str, camera_key: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM cameras WHERE key = ?", (camera_key,)).fetchone()
        return dict(row) if row else None


def link_camera_to_device(db_path: str, camera_key: str, device_id: str) -> dict | None:
    """Marks a camera as the one watching a given kiosk terminal -- what makes it
    eligible for face recognition at all. Returns the updated camera, or None if no
    camera has that key (e.g. a typo from the Review page)."""
    with closing(_connect(db_path)) as conn:
        cur = conn.execute("UPDATE cameras SET kiosk_device_id = ? WHERE key = ?",
                           (device_id, camera_key))
        conn.commit()
        if cur.rowcount == 0:
            return None
        return dict(conn.execute("SELECT * FROM cameras WHERE key = ?", (camera_key,)).fetchone())


def unlink_camera_device(db_path: str, camera_key: str) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute("UPDATE cameras SET kiosk_device_id = NULL WHERE key = ?", (camera_key,))
        conn.commit()
        return cur.rowcount > 0


def camera_for_device(db_path: str, device_id: str) -> dict | None:
    """The camera linked to a voice terminal, if any and if it's enabled. This is the
    only lookup identity_gate.py ever does -- there is no fallback to "the nearest
    camera" or "any kiosk camera", by design."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM cameras WHERE kiosk_device_id = ? AND enabled = 1", (device_id,)
        ).fetchone()
        return dict(row) if row else None


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
        the honest placeholder until the first RTSP camera actually exists to test on."""
        try:
            import cv2
        except ImportError:
            return None
        cap = cv2.VideoCapture(self.url)
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
    from datetime import timedelta
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


# --- known people (Chunk 2: enrollment + matching) -----------------------------

# How many embedding samples to keep per person. Enough to cover real lighting/angle
# variation without the match scan (which checks every sample of every person) growing
# without bound -- FaceRecognizer.match() takes the max similarity across all of them,
# so older, worse samples are simply never the one that wins once better ones exist.
MAX_FACE_SAMPLES = 12


def upsert_known_person(db_path: str, key: str, name: str, relationship: str = "household",
                        recording_preference: str = "inherit") -> dict:
    """Creates or updates a known person's identity (name/relationship), independent of
    their face samples -- add_face_sample is the only thing that touches embeddings."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO known_people (key, name, relationship, embeddings, sample_count,
                                         recording_preference, created_at, updated_at)
               VALUES (?, ?, ?, '[]', 0, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   name = excluded.name, relationship = excluded.relationship,
                   recording_preference = excluded.recording_preference,
                   updated_at = excluded.updated_at""",
            (key, name, relationship, recording_preference, _now(), _now()))
        conn.commit()
        return dict(conn.execute("SELECT * FROM known_people WHERE key = ?", (key,)).fetchone())


def add_face_sample(db_path: str, key: str, embedding_json: str) -> dict:
    """Appends one embedding sample (as produced by face_recognizer.embed_to_json) to a
    known person, keeping at most the most recent MAX_FACE_SAMPLES. Raises if the person
    doesn't exist yet -- callers enroll (upsert_known_person) before sampling, never the
    other way around, so a stray embedding can never end up attached to nobody."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT embeddings FROM known_people WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise ValueError(f"no known person with key {key!r} -- call upsert_known_person first")
        samples = json.loads(row["embeddings"] or "[]")
        samples.append(json.loads(embedding_json))
        samples = samples[-MAX_FACE_SAMPLES:]
        conn.execute(
            "UPDATE known_people SET embeddings = ?, sample_count = ?, updated_at = ? WHERE key = ?",
            (json.dumps(samples), len(samples), _now(), key))
        conn.commit()
        return dict(conn.execute("SELECT * FROM known_people WHERE key = ?", (key,)).fetchone())


def get_known_person(db_path: str, key: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM known_people WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None


def list_known_people(db_path: str) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM known_people ORDER BY name")]


def delete_known_person(db_path: str, key: str) -> bool:
    """Clears an enrollment entirely -- the honest way to start over rather than let a
    bad batch of samples (wrong lighting rig, someone else briefly in frame) linger and
    quietly lower match quality forever."""
    with closing(_connect(db_path)) as conn:
        cur = conn.execute("DELETE FROM known_people WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0


# --- unknown faces (Chunk 2: logged for the owner, not auto-resolved) ----------

def record_unknown_face(db_path: str, camera_key: str, embedding_json: str,
                        thumbnail_path: str = "") -> int:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO unknown_faces (camera_key, embedding, thumbnail_path, seen_count,
                                          first_seen, last_seen)
               VALUES (?, ?, ?, 1, ?, ?)""",
            (camera_key, embedding_json, thumbnail_path or None, _now(), _now()))
        conn.commit()
        return cur.lastrowid


def bump_unknown_face(db_path: str, unknown_id: int) -> None:
    """The same unrecognized face was seen again -- counted rather than logged again, so
    one lingering stranger doesn't fill the table with near-duplicate rows."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE unknown_faces SET seen_count = seen_count + 1, last_seen = ? WHERE id = ?",
            (_now(), unknown_id))
        conn.commit()


def get_unknown_face(db_path: str, unknown_id: int) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM unknown_faces WHERE id = ?", (unknown_id,)).fetchone()
        return dict(row) if row else None


def list_unknown_faces(db_path: str, resolved: bool = False, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM unknown_faces WHERE resolved_person_key IS " + ("NOT NULL" if resolved else "NULL")
    sql += " ORDER BY last_seen DESC LIMIT ?"
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql, (limit,))]


def mark_unknown_face_asked(db_path: str, unknown_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE unknown_faces SET asked = 1 WHERE id = ?", (unknown_id,))
        conn.commit()


def resolve_unknown_face(db_path: str, unknown_id: int, person_key: str) -> bool:
    """Not called anywhere yet -- the schema and this function exist so the future
    enroll-on-recognition-failure feature has a real place to land instead of another
    migration. Left here deliberately unused rather than wired to a UI action, per the
    single-user-enrollment locked decision for this chunk."""
    with closing(_connect(db_path)) as conn:
        cur = conn.execute("UPDATE unknown_faces SET resolved_person_key = ? WHERE id = ?",
                           (person_key, unknown_id))
        conn.commit()
        return cur.rowcount > 0
