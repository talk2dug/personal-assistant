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

A fourth decision, added alongside face recognition (core/face_recognizer.py,
core/vision_watch.py, core/identity.py): **a camera's `role` decides whether it's ever
asked who someone is.** 'kiosk' cameras (the voice terminals) are the only ones face
recognition ever runs against; 'house' cameras keep doing exactly what they did before
— person/pet detection, no identity. See core/identity.py's docstring for why.
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

-- A request to enroll someone, created by the owner (POST /api/vision/people/enroll)
-- and fulfilled by the camera-watch process, which is the only process InsightFace is
-- actually loaded in. Decoupled through the database rather than a direct call because
-- the web process and the camera-watch process are deliberately separate .venvs (see
-- face_recognizer.py's docstring) on possibly separate machines.
CREATE TABLE IF NOT EXISTS enrollment_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_key TEXT NOT NULL,
    person_name TEXT NOT NULL,
    person_key TEXT NOT NULL,
    relationship TEXT NOT NULL DEFAULT 'household',
    samples_wanted INTEGER NOT NULL DEFAULT 5,
    samples_collected INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'cancelled')),
    requested_by INTEGER,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_enrollment_requests_camera
    ON enrollment_requests(camera_key, status);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE migrations, same pattern as core/db.py's _migrate -- safe
    to call on every startup, on a fresh or already-populated database."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(cameras)")}
    if "role" not in cols:
        # Default 'house': every camera that existed before this migration keeps doing
        # exactly what it did (person/pet detection, no identity) until someone
        # explicitly re-registers it as 'kiosk'.
        conn.execute("ALTER TABLE cameras ADD COLUMN role TEXT NOT NULL DEFAULT 'house'")
    if "device_id" not in cols:
        conn.execute("ALTER TABLE cameras ADD COLUMN device_id TEXT")


def init_vision_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- cameras ------------------------------------------------------------------

CAMERA_ROLES = ("house", "kiosk")


def add_camera(db_path: str, key: str, name: str, url: str, kind: str = "mjpeg",
               location: str = "", motion_threshold: float = 0.012,
               recordable: bool = True, role: str = "house",
               device_id: str | None = None) -> dict:
    if kind not in ("mjpeg", "rtsp"):
        raise ValueError("kind must be mjpeg or rtsp")
    if role not in CAMERA_ROLES:
        raise ValueError(f"role must be one of {CAMERA_ROLES}")
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO cameras (key, name, kind, url, location, motion_threshold,
                                    enabled, recordable, role, device_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   name = excluded.name, kind = excluded.kind, url = excluded.url,
                   location = excluded.location,
                   motion_threshold = excluded.motion_threshold,
                   recordable = excluded.recordable,
                   role = excluded.role, device_id = excluded.device_id""",
            (key, name, kind, url, location, motion_threshold,
             1 if recordable else 0, role, device_id, _now()))
        conn.commit()
        return dict(conn.execute("SELECT * FROM cameras WHERE key = ?", (key,)).fetchone())


def list_cameras(db_path: str, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM cameras"
    if enabled_only:
        sql += " WHERE enabled = 1"
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY key")]


def get_camera(db_path: str, key: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM cameras WHERE key = ?", (key,)).fetchone()
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


def current_identity(db_path: str, camera_key: str, within_seconds: int = 45) -> dict:
    """Fail-closed identity read for exactly one camera: the most recent identity-
    bearing event within the window, or 'nobody recognized' if there isn't one.

    Deliberately a short window -- someone identified two minutes ago and then walked
    away must not keep unlocking personal/financial tools for whoever is standing there
    now. This is the one function core/identity.py calls; it never sees vision_events
    directly, so a future change to how identity is recorded only has to keep this
    return shape stable.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_seconds)).isoformat()
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT * FROM vision_events
               WHERE camera_key = ? AND at >= ? AND kind IN ('identified', 'unknown_person')
               ORDER BY id DESC LIMIT 1""",
            (camera_key, cutoff),
        ).fetchone()
    if row is None or row["kind"] != "identified" or not row["person_key"]:
        return {"recognized": False, "camera_key": camera_key}
    return {
        "recognized": True, "camera_key": camera_key, "person_key": row["person_key"],
        "label": row["label"], "confidence": row["confidence"], "at": row["at"],
    }


# --- known people / face identity ----------------------------------------------

def make_person_key(name: str) -> str:
    """A stable, storable id derived from a display name -- 'Doug' -> 'doug'. Not
    checked for uniqueness beyond the table's own UNIQUE(key): today there is exactly
    one person ever enrolled this way, and the moment a second is added the caller is
    expected to pass an actually-unique key rather than rely on this collapsing two
    different names to the same thing."""
    key = "".join(c if c.isalnum() else "_" for c in name.strip().lower())
    key = "_".join(filter(None, key.split("_")))
    return key or "person"


def enroll_person(db_path: str, name: str, embedding: np.ndarray | None, key: str | None = None,
                  relationship: str = "household") -> dict:
    """Creates a known person from their first sample, or -- if the key already exists
    -- adds another sample to them. This is the ONE way a face becomes 'known'; it is
    always the result of the owner acting (the enrollment endpoint, or approving a
    Review-page enrollment item), never inferred from repeated sightings alone."""
    key = key or make_person_key(name)
    with closing(_connect(db_path)) as conn:
        existing = conn.execute("SELECT * FROM known_people WHERE key = ?", (key,)).fetchone()
        if existing:
            if embedding is None:
                return dict(existing)
            conn.close()
            return add_face_sample(db_path, key, embedding)
        now = _now()
        emb_list = [embedding.tolist()] if embedding is not None else []
        conn.execute(
            """INSERT INTO known_people (key, name, relationship, embeddings, sample_count,
                                        created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (key, name, relationship, json.dumps(emb_list), len(emb_list), now, now),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM known_people WHERE key = ?", (key,)).fetchone())


def add_face_sample(db_path: str, person_key: str, embedding: np.ndarray) -> dict:
    """Adds one more embedding sample to an already-enrolled person -- see
    known_people.embeddings' own schema comment for why more than one sample matters
    (daylight at the door vs. indoor evening light are far apart in embedding space)."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM known_people WHERE key = ?", (person_key,)).fetchone()
        if row is None:
            raise ValueError(f"no known person with key {person_key!r}")
        samples = json.loads(row["embeddings"])
        samples.append(embedding.tolist() if hasattr(embedding, "tolist") else list(embedding))
        conn.execute(
            "UPDATE known_people SET embeddings = ?, sample_count = ?, updated_at = ? WHERE key = ?",
            (json.dumps(samples), len(samples), _now(), person_key),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM known_people WHERE key = ?", (person_key,)).fetchone())


def list_known_people(db_path: str) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM known_people ORDER BY name")]


def get_known_person(db_path: str, key: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM known_people WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None


def delete_known_person(db_path: str, key: str) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute("DELETE FROM known_people WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0


def match_face(db_path: str, embedding: np.ndarray, threshold: float | None = None) -> dict | None:
    """Best-matching known person for one embedding, or None below threshold -- 'None'
    is the fail-closed answer, not 'guess the closest one anyway'. Compared against
    every stored sample for a person (not an average of them), since averaging two
    genuinely different lighting conditions can land in no-man's-land between both.
    """
    from .face_recognizer import MATCH_THRESHOLD, cosine_similarity
    threshold = MATCH_THRESHOLD if threshold is None else threshold
    best, best_score = None, -1.0
    with closing(_connect(db_path)) as conn:
        for row in conn.execute("SELECT * FROM known_people"):
            for sample in json.loads(row["embeddings"]):
                score = cosine_similarity(embedding, np.asarray(sample, dtype=np.float32))
                if score > best_score:
                    best_score, best = score, dict(row)
    if best is None or best_score < threshold:
        return None
    return {**best, "match_score": best_score}


# --- unknown faces (future: prompt-to-enroll) -----------------------------------
#
# Recorded so a repeatedly-seen unrecognised face becomes a decision for the owner
# instead of either a guess or something silently ignored forever. Nothing calls
# faces_ready_to_ask() automatically yet -- see this chunk's report / docs for why
# (single-user enrollment is the locked decision for now) -- these exist so wiring a
# scheduler tick that turns a ready face into a Review-page item is a ~10-line addition
# later, not a schema change.
UNKNOWN_FACE_MATCH_THRESHOLD = 0.50   # 'is this sighting the same unresolved face as before'
ASK_AFTER_SIGHTINGS = 5


def record_unknown_face(db_path: str, camera_key: str, embedding: np.ndarray,
                        thumbnail_path: str = "") -> dict:
    """Folds a sighting into an existing unresolved unknown face at this camera if it's
    a close match, otherwise starts a new one. Done in Python (like business_db's
    normalize_lead_name matching) since 512-d cosine comparison isn't something SQL
    expresses."""
    from .face_recognizer import cosine_similarity
    now = _now()
    with closing(_connect(db_path)) as conn:
        candidates = conn.execute(
            "SELECT * FROM unknown_faces WHERE resolved_person_key IS NULL AND camera_key = ?",
            (camera_key,),
        ).fetchall()
        for row in candidates:
            score = cosine_similarity(embedding, np.asarray(json.loads(row["embedding"]), dtype=np.float32))
            if score >= UNKNOWN_FACE_MATCH_THRESHOLD:
                conn.execute(
                    """UPDATE unknown_faces SET seen_count = seen_count + 1, last_seen = ?,
                       thumbnail_path = COALESCE(?, thumbnail_path) WHERE id = ?""",
                    (now, thumbnail_path or None, row["id"]),
                )
                conn.commit()
                return dict(conn.execute("SELECT * FROM unknown_faces WHERE id = ?", (row["id"],)).fetchone())
        cur = conn.execute(
            """INSERT INTO unknown_faces (camera_key, embedding, thumbnail_path, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?)""",
            (camera_key, json.dumps(embedding.tolist()), thumbnail_path or None, now, now),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM unknown_faces WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_unknown_face(db_path: str, face_id: int) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM unknown_faces WHERE id = ?", (face_id,)).fetchone()
        return dict(row) if row else None


def list_unknown_faces(db_path: str, unresolved_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM unknown_faces"
    if unresolved_only:
        sql += " WHERE resolved_person_key IS NULL"
    sql += " ORDER BY last_seen DESC"
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql)]


def faces_ready_to_ask(db_path: str, min_sightings: int = ASK_AFTER_SIGHTINGS) -> list[dict]:
    """Unknown faces seen often enough, and not yet asked about. NOT polled by anything
    today -- see this module's 'unknown faces' section docstring."""
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM unknown_faces WHERE resolved_person_key IS NULL AND asked = 0
               AND seen_count >= ? ORDER BY seen_count DESC""", (min_sightings,))]


def mark_unknown_face_asked(db_path: str, face_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE unknown_faces SET asked = 1 WHERE id = ?", (face_id,))
        conn.commit()


def resolve_unknown_face(db_path: str, face_id: int, person_key: str | None) -> dict | None:
    """person_key=None means 'not worth enrolling' -- resolved so it stops being asked
    about, without ever pretending it matched somebody."""
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE unknown_faces SET resolved_person_key = ?, asked = 1 WHERE id = ?",
            (person_key or "ignored", face_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return dict(conn.execute("SELECT * FROM unknown_faces WHERE id = ?", (face_id,)).fetchone())


# --- enrollment requests (owner-initiated; fulfilled by the camera-watch process) ---

def create_enrollment_request(db_path: str, camera_key: str, person_name: str,
                              samples_wanted: int = 5, relationship: str = "household",
                              requested_by: int | None = None, person_key: str | None = None) -> dict:
    key = person_key or make_person_key(person_name)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO enrollment_requests
                   (camera_key, person_name, person_key, relationship, samples_wanted,
                    requested_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (camera_key, person_name, key, relationship, samples_wanted, requested_by, _now()),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM enrollment_requests WHERE id = ?", (cur.lastrowid,)).fetchone())


def pending_enrollment_request(db_path: str, camera_key: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT * FROM enrollment_requests WHERE camera_key = ? AND status = 'pending'
               ORDER BY id DESC LIMIT 1""",
            (camera_key,),
        ).fetchone()
        return dict(row) if row else None


def get_enrollment_request(db_path: str, request_id: int) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM enrollment_requests WHERE id = ?", (request_id,)).fetchone()
        return dict(row) if row else None


def cancel_enrollment_request(db_path: str, request_id: int) -> bool:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE enrollment_requests SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (request_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def record_enrollment_sample(db_path: str, request_id: int, embedding: np.ndarray) -> dict | None:
    """Called by the camera-watch loop, once per frame it decides is a usable sample.
    Marks the request done once enough samples are in -- the caller (vision_watch.py)
    just keeps calling this until pending_enrollment_request() stops returning it."""
    request = get_enrollment_request(db_path, request_id)
    if request is None or request["status"] != "pending":
        return request
    enroll_person(db_path, request["person_name"], embedding,
                  key=request["person_key"], relationship=request["relationship"])
    with closing(_connect(db_path)) as conn:
        collected = request["samples_collected"] + 1
        done = collected >= request["samples_wanted"]
        conn.execute(
            """UPDATE enrollment_requests SET samples_collected = ?, status = ?, completed_at = ?
               WHERE id = ?""",
            (collected, "done" if done else "pending", _now() if done else None, request_id),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM enrollment_requests WHERE id = ?", (request_id,)).fetchone())
