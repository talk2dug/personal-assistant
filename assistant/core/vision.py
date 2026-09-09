"""Camera watching: who is in a room, and whether the movement is the dog.

The point of this module is house logic, not surveillance footage. It answers three
questions -- is anyone there, who is it, and was that motion a person or a pet -- and emits
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

That later stage is what this module's identity section (bottom of the file) actually
turns on: InsightFace-based matching against known_people, an unknown-face dedup/ask
cadence, and apply_face_review_decision -- the one function that ever creates or grows a
known person, and it only runs after an owner decision on the Review page (see
assistant/core/vision_runtime.py for the pipeline that calls into all of this, and
assistant/web/routes/review.py for the write-through).
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

# Cosine-similarity thresholds and review cadence. These are *defaults* only -- every
# real deployment should override them via config (assistant/config.py's
# vision_face_match_threshold etc.) once calibrated against its own cameras and faces.
# A number picked in the abstract for InsightFace's buffalo_l model is a starting point
# for owner testing, not a promise that it's right for any particular hallway.
FACE_MATCH_THRESHOLD = 0.38
UNKNOWN_FACE_DEDUP_THRESHOLD = 0.55
UNKNOWN_FACE_ASK_AFTER = 3
MAX_EMBEDDINGS_PER_PERSON = 8


def init_vision_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE migrations for columns added after the initial
    CREATE TABLE IF NOT EXISTS -- same pattern as db.py's _migrate, safe to call on
    every startup against a fresh or already-populated database."""
    people_cols = {row[1] for row in conn.execute("PRAGMA table_info(known_people)")}
    if "linked_user_id" not in people_cols:
        # Ties a known face to an actual Jarvis account (owner/partner), so presence can
        # answer not just "who is this" but "is this someone who should get personal or
        # business data" -- a known household member who isn't a registered user (a
        # child, a regular guest) stays NULL here and is treated the same as unknown for
        # that gate, on purpose.
        conn.execute("ALTER TABLE known_people ADD COLUMN linked_user_id INTEGER REFERENCES users(id)")
    camera_cols = {row[1] for row in conn.execute("PRAGMA table_info(cameras)")}
    if "device_id" not in camera_cols:
        # Which voice terminal (device/jarvis_device.py's device_id) this camera is
        # colocated with, if any -- what lets devices.py's /turn gate a terminal's
        # answers on who is actually standing in front of it.
        conn.execute("ALTER TABLE cameras ADD COLUMN device_id TEXT")


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


# =============================================================================
# Identity: enrollment, matching, and the presence gate other modules use
# =============================================================================
#
# Everything below is what turns "a person" (detector.py's YOLO output) into "which
# person" (InsightFace embeddings matched against known_people). The one rule that
# matters: nothing here ever creates or grows a known_people row on its own.
# apply_face_review_decision is the sole write path into known_people, and it only runs
# after an owner decision on the Review page (assistant/web/routes/review.py). Detection
# code (assistant/core/vision_runtime.py) only ever *proposes*, via unknown_faces.

def _cosine(a: list, b: list) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def _slugify(name: str) -> str:
    cleaned = "".join(c if c.isalnum() else " " for c in name.lower())
    return "-".join(cleaned.split()) or "person"


def get_known_people(db_path: str) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM known_people ORDER BY name")]


def get_person_by_key(db_path: str, person_key: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM known_people WHERE key = ?", (person_key,)).fetchone()
        return dict(row) if row else None


def enroll_person(db_path: str, name: str, embedding: list, relationship: str | None = None,
                  linked_user_id: int | None = None) -> str:
    """Creates a new known person from a single face embedding. Called only from
    apply_face_review_decision -- i.e. only after the owner has approved a 'New person'
    enrollment on the Review page."""
    key = _slugify(name)
    with closing(_connect(db_path)) as conn:
        if conn.execute("SELECT 1 FROM known_people WHERE key = ?", (key,)).fetchone():
            key = f"{key}-{int(time.time())}"
        conn.execute(
            """INSERT INTO known_people
                   (key, name, relationship, embeddings, sample_count, linked_user_id,
                    created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
            (key, name, relationship, json.dumps([embedding]), linked_user_id, _now(), _now()),
        )
        conn.commit()
    return key


def add_embedding(db_path: str, person_key: str, embedding: list) -> bool:
    """Adds another look of an already-known person -- what happens when the owner
    matches an unfamiliar-face review to someone who already exists, so the gallery
    grows to cover different lighting/angles rather than staying a single snapshot.
    Oldest embeddings are dropped past MAX_EMBEDDINGS_PER_PERSON."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT embeddings, sample_count FROM known_people WHERE key = ?", (person_key,)
        ).fetchone()
        if row is None:
            return False
        embeddings = json.loads(row["embeddings"] or "[]")
        embeddings.append(embedding)
        embeddings = embeddings[-MAX_EMBEDDINGS_PER_PERSON:]
        conn.execute(
            "UPDATE known_people SET embeddings = ?, sample_count = ?, updated_at = ? WHERE key = ?",
            (json.dumps(embeddings), row["sample_count"] + 1, _now(), person_key),
        )
        conn.commit()
        return True


def match_embedding(db_path: str, embedding: list, threshold: float = FACE_MATCH_THRESHOLD):
    """Best known-person match for a face embedding, or None. Returns
    (person_key, name, score) -- score is cosine similarity, higher is closer. Checked
    against every stored embedding for every person (a person may have several looks),
    keeping only the best."""
    best = None
    with closing(_connect(db_path)) as conn:
        for row in conn.execute("SELECT key, name, embeddings FROM known_people"):
            for stored in json.loads(row["embeddings"] or "[]"):
                score = _cosine(embedding, stored)
                if score >= threshold and (best is None or score > best[2]):
                    best = (row["key"], row["name"], score)
    return best


def upsert_unknown_face(db_path: str, camera_key: str, embedding: list, thumbnail_path: str = "",
                        dedup_threshold: float = UNKNOWN_FACE_DEDUP_THRESHOLD) -> dict:
    """Records a face that didn't match anyone known, folding repeat sightings of the
    same unnamed face into one row (per camera) rather than growing one per frame --
    seen_count is what later decides whether it's worth asking the owner about at all.
    Dedup only considers faces not yet resolved to a known person on this camera; once
    resolved, a face should never accumulate further sightings here."""
    with closing(_connect(db_path)) as conn:
        candidates = conn.execute(
            "SELECT * FROM unknown_faces WHERE camera_key = ? AND resolved_person_key IS NULL",
            (camera_key,),
        ).fetchall()
        best_id, best_score = None, 0.0
        for row in candidates:
            score = _cosine(embedding, json.loads(row["embedding"]))
            if score >= dedup_threshold and score > best_score:
                best_id, best_score = row["id"], score
        if best_id is not None:
            conn.execute(
                """UPDATE unknown_faces SET seen_count = seen_count + 1, last_seen = ?,
                       thumbnail_path = COALESCE(?, thumbnail_path) WHERE id = ?""",
                (_now(), thumbnail_path or None, best_id),
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM unknown_faces WHERE id = ?", (best_id,)).fetchone())
        cur = conn.execute(
            """INSERT INTO unknown_faces (camera_key, embedding, thumbnail_path, first_seen, last_seen)
                  VALUES (?, ?, ?, ?, ?)""",
            (camera_key, json.dumps(embedding), thumbnail_path or None, _now(), _now()),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM unknown_faces WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_unknown_face(db_path: str, face_id: int) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM unknown_faces WHERE id = ?", (face_id,)).fetchone()
        return dict(row) if row else None


def mark_unknown_face_asked(db_path: str, face_id: int) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE unknown_faces SET asked = 1 WHERE id = ?", (face_id,))
        conn.commit()


def resolve_unknown_face(db_path: str, face_id: int, person_key: str) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE unknown_faces SET resolved_person_key = ? WHERE id = ?", (person_key, face_id))
        conn.commit()


def unasked_unknown_faces(db_path: str, min_seen: int = UNKNOWN_FACE_ASK_AFTER) -> list[dict]:
    """Unresolved unfamiliar faces seen often enough to be worth a Review-page item, but
    not yet turned into one."""
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM unknown_faces WHERE asked = 0 AND resolved_person_key IS NULL
                  AND seen_count >= ? ORDER BY last_seen""", (min_seen,))]


def apply_face_review_decision(db_path: str, face_id: int, decision: str,
                               chosen_option_body: str | None, note: str | None) -> str | None:
    """Turns the owner's Review-page decision about an unfamiliar face into the actual
    known_people write. Returns a short human-readable description of what happened, or
    None if the face row is gone.

    chosen_option_body is the raw `body` field of whichever review_option the owner
    picked -- JSON of either {"person_key": ...} (an existing match) or
    {"new_person": true} (enroll fresh, using `note` as the name). Deliberately not
    parsed from the option's label text, which is display copy and free to change.
    """
    face = get_unknown_face(db_path, face_id)
    if face is None:
        return None

    if decision != "approved":
        mark_unknown_face_asked(db_path, face_id)
        return f"unknown_faces#{face_id} dismissed ({decision})"

    choice = json.loads(chosen_option_body) if chosen_option_body else {}
    embedding = json.loads(face["embedding"])

    if choice.get("new_person"):
        name = (note or "").strip()
        if not name:
            # No name was supplied -- nothing to enroll under. Leave it unresolved and
            # NOT marked asked, so the owner can redo the decision with a name in the
            # note field rather than silently guessing one or losing the request.
            return (f"unknown_faces#{face_id} not enrolled -- approve again with a name "
                    "in the note field")
        person_key = enroll_person(db_path, name, embedding)
        resolve_unknown_face(db_path, face_id, person_key)
        mark_unknown_face_asked(db_path, face_id)
        return f"unknown_faces#{face_id} -> enrolled as new person '{name}' ({person_key})"

    person_key = choice.get("person_key")
    if person_key and get_person_by_key(db_path, person_key):
        add_embedding(db_path, person_key, embedding)
        resolve_unknown_face(db_path, face_id, person_key)
        mark_unknown_face_asked(db_path, face_id)
        return f"unknown_faces#{face_id} -> linked to existing person {person_key}"

    mark_unknown_face_asked(db_path, face_id)
    return f"unknown_faces#{face_id} dismissed (no valid option chosen)"


# --- camera <-> device linkage (for the identity gate on a voice terminal) ---------

def set_camera_device(db_path: str, camera_key: str, device_id: str | None) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE cameras SET device_id = ? WHERE key = ?", (device_id, camera_key))
        conn.commit()


def camera_for_device(db_path: str, device_id: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM cameras WHERE device_id = ? AND enabled = 1", (device_id,)
        ).fetchone()
        return dict(row) if row else None


def identify_present(db_path: str, camera_key: str, within_seconds: int = 180) -> dict | None:
    """The most recently identified known person seen on this camera within the window,
    or None. None covers every case that must NOT be treated as a known, present person:
    an empty room, motion with nobody recognised, and an unresolved unfamiliar face --
    callers gating personal/sensitive info (see devices.py's _resolve_speaker) must
    default closed on None, never assume it means the owner."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_seconds)).isoformat()
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT * FROM vision_events WHERE camera_key = ? AND kind = 'identified'
                  AND at >= ? ORDER BY id DESC LIMIT 1""", (camera_key, cutoff),
        ).fetchone()
        if row is None:
            return None
        person = conn.execute(
            "SELECT * FROM known_people WHERE key = ?", (row["person_key"],)
        ).fetchone()
        if person is None:
            return None
        return {
            "person_key": person["key"], "name": person["name"],
            "relationship": person["relationship"], "linked_user_id": person["linked_user_id"],
            "confidence": row["confidence"], "at": row["at"],
        }
