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
uStreamer on the Pi terminals, or a webcam attached directly to this host ('local' —
laptop1's own camera). RTSP cameras are coming too. Adding a new kind is a config entry
plus one FrameSource method rather than a rewrite, so everything downstream works on
frames and never knows where they came from.

**Identity is a separate, later stage.** Detection says "a person"; recognition says
"which person", and only runs when there is a face to look at (core/face_recognition.py,
run from core/camera_watch.py — this module owns the schema and the plain-data queries
over it, not the models). An unknown face becomes a question for the owner, surfaced on
the Review page, rather than a guess — see enroll_known_person / upsert_unknown_face
below. A confidently wrong name is worse than an honest "I don't know who that is".
"""
import json
import re
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
    -- 'mjpeg' (uStreamer on a terminal), 'rtsp' (cameras being added later), or 'local'
    -- (a webcam attached directly to this host, e.g. laptop1's own camera).
    kind TEXT NOT NULL DEFAULT 'mjpeg' CHECK (kind IN ('mjpeg', 'rtsp', 'local')),
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
-- owner answering "who is this?" on the Review page, never inferred.
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
        conn.commit()


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
    if kind not in ("mjpeg", "rtsp", "local"):
        raise ValueError("kind must be mjpeg, rtsp or local")
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
        if self.kind == "local":
            return self._local_snapshot()
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

    def _local_snapshot(self) -> np.ndarray | None:
        """A camera physically attached to this host -- opened by OpenCV device index
        rather than a URL (laptop1's own webcam is 'kind': 'local', 'url': '0'). Same
        open/grab-one-frame/release approach as _rtsp_snapshot, for the same reason:
        this runs behind a motion gate at a few Hz at most, so holding the device open
        between snapshots isn't worth the complexity yet."""
        try:
            import cv2
        except ImportError:
            return None
        index = int(self.url) if self.url.isdigit() else self.url
        cap = cv2.VideoCapture(index)
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


# --- known people (identity) ---------------------------------------------------

def _l2_normalize(vec: list[float]) -> list[float]:
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm == 0:
        return arr.tolist()
    return (arr / norm).tolist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def slugify_person_key(name: str) -> str:
    """Turns a name typed on the Review page into a stable key -- lowercase, ascii,
    hyphenated -- so enrolling "Dug" and later "dug " (a typo re-enrollment, a different
    camera's transcription of the same note) land on the same known_people row instead
    of silently forking one person into two."""
    key = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return key or "person"


def list_known_people(db_path: str) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM known_people ORDER BY name")]
    for r in rows:
        r["embeddings"] = json.loads(r["embeddings"])
    return rows


def get_known_person(db_path: str, key: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM known_people WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    person = dict(row)
    person["embeddings"] = json.loads(person["embeddings"])
    return person


# A face at the door at night and the same face indoors in daylight are far apart in
# embedding space (known_people's own docstring), so more than one sample matters -- but
# nobody needs fifty samples of the same person, and an unbounded list would eventually
# make find_known_match compare against a person's entire life. Oldest samples drop
# first: lighting/angle drift is the thing multiple embeddings exist to track, so the
# most recent samples are the most representative of "what they look like lately".
MAX_EMBEDDINGS_PER_PERSON = 8


def enroll_known_person(db_path: str, name: str, embedding: list[float],
                        relationship: str = "household", key: str | None = None) -> str:
    """Registers a new known identity from one face embedding -- the enrollment step the
    owner triggers by approving an 'unrecognised face' review item and typing a name into
    its note field. Returns the person's key.

    If the key already exists (re-enrolling the same name -- a second sighting the owner
    separately approved before this one's threshold was reached), the embedding is added
    to that person instead of raising: two sightings of the same real person merging is
    the correct outcome, not an error.
    """
    key = key or slugify_person_key(name)
    if get_known_person(db_path, key) is not None:
        add_person_embedding(db_path, key, embedding)
        return key
    now = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO known_people (key, name, relationship, embeddings, sample_count,
                                        created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?)""",
            (key, name, relationship, json.dumps([_l2_normalize(embedding)]), now, now),
        )
        conn.commit()
    return key


def add_person_embedding(db_path: str, key: str, embedding: list[float]) -> bool:
    person = get_known_person(db_path, key)
    if person is None:
        return False
    embeddings = person["embeddings"] + [_l2_normalize(embedding)]
    if len(embeddings) > MAX_EMBEDDINGS_PER_PERSON:
        embeddings = embeddings[-MAX_EMBEDDINGS_PER_PERSON:]
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """UPDATE known_people SET embeddings = ?, sample_count = sample_count + 1,
                                       updated_at = ? WHERE key = ?""",
            (json.dumps(embeddings), _now(), key),
        )
        conn.commit()
    return True


def find_known_match(db_path: str, embedding: list[float], threshold: float = 0.42) -> dict | None:
    """The best known person this embedding could be, or None if nothing clears the
    threshold. Compared against every stored embedding for a person -- not an average,
    which would wash out the very lighting/angle variation multiple samples exist to
    capture -- and the person's single best-matching sample wins.

    0.42 is a starting point for insightface's ArcFace embeddings (cosine similarity,
    L2-normalised): the same person typically scores 0.5-0.8+, different people cluster
    below 0.3. Deliberately conservative, same reasoning as detector.py's MIN_CONFIDENCE:
    a confidently wrong name is the expensive mistake here, not an honest "I don't know".
    """
    best_key, best_score = None, threshold
    for person in list_known_people(db_path):
        if not person["embeddings"]:
            continue
        score = max(cosine_similarity(embedding, e) for e in person["embeddings"])
        if score >= best_score:
            best_key, best_score = person["key"], score
    if best_key is None:
        return None
    person = get_known_person(db_path, best_key)
    return {"key": person["key"], "name": person["name"], "score": round(best_score, 3)}


# --- unknown faces (the queue that becomes a Review-page enrollment question) -------

# Tighter than find_known_match's default threshold: two *unknown* sightings merging
# incorrectly means a real stranger's count grows without the owner ever seeing a
# duplicate row, whereas erring the other way just creates an extra low-count unknown
# row that never reaches ENROLL_AFTER_SIGHTINGS. Wrong-but-visible beats wrong-and-silent.
UNKNOWN_FACE_MATCH_THRESHOLD = 0.5

# A single frame's face could be a bad crop, a photo on a phone screen, or a glare-lit
# grimace -- three separate sightings is a cheap, real gate before the house bothers the
# owner with a question.
ENROLL_AFTER_SIGHTINGS = 3


def upsert_unknown_face(db_path: str, camera_key: str, embedding: list[float],
                        thumbnail_path: str = "") -> dict:
    """Records a face that matched nobody known. If it looks like the same unresolved
    unknown face seen before (anywhere, not just this camera -- someone walks between
    rooms), bumps its seen_count instead of creating a duplicate row for every sighting.
    Resolved faces (already enrolled) are excluded from matching -- a new stranger who
    happens to resemble someone already identified must not silently inherit their row.
    """
    normalized = _l2_normalize(embedding)
    with closing(_connect(db_path)) as conn:
        candidates = conn.execute(
            "SELECT * FROM unknown_faces WHERE resolved_person_key IS NULL"
        ).fetchall()
        best_row, best_score = None, UNKNOWN_FACE_MATCH_THRESHOLD
        for row in candidates:
            score = cosine_similarity(normalized, json.loads(row["embedding"]))
            if score >= best_score:
                best_row, best_score = row, score
        if best_row is not None:
            conn.execute(
                """UPDATE unknown_faces SET seen_count = seen_count + 1, last_seen = ?,
                                            thumbnail_path = COALESCE(?, thumbnail_path)
                   WHERE id = ?""",
                (_now(), thumbnail_path or None, best_row["id"]),
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT * FROM unknown_faces WHERE id = ?", (best_row["id"],)).fetchone())
        now = _now()
        cur = conn.execute(
            """INSERT INTO unknown_faces (camera_key, embedding, thumbnail_path, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?)""",
            (camera_key, json.dumps(normalized), thumbnail_path or None, now, now),
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
        conn.execute(
            "UPDATE unknown_faces SET resolved_person_key = ? WHERE id = ?", (person_key, face_id)
        )
        conn.commit()


def unasked_unknown_faces(db_path: str, min_seen: int = ENROLL_AFTER_SIGHTINGS) -> list[dict]:
    """Unknown faces seen enough times to be worth asking about, and not asked yet --
    what the camera watcher turns into review items (and what a periodic sweep or a
    future chat tool could use to catch anything the live loop missed)."""
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM unknown_faces
               WHERE asked = 0 AND resolved_person_key IS NULL AND seen_count >= ?
               ORDER BY last_seen""", (min_seen,))]


# --- presence + identity (what the rest of the house asks) -------------------------

def known_people_present(db_path: str, within_seconds: int = 120) -> list[dict]:
    """Known people seen recently, across every camera -- layers identity onto
    presence_now()'s per-camera view. This is the query open-mic gating actually needs
    ('is a recognised person in front of this camera right now'), and the shape a future
    "who's home" chat answer would want too."""
    raw = presence_now(db_path, within_seconds=within_seconds)
    keys: set[str] = set()
    for cam in raw.values():
        keys.update(cam["people"])
    if not keys:
        return []
    people = {p["key"]: p for p in list_known_people(db_path)}
    return [
        {"key": k, "name": people[k]["name"], "relationship": people[k]["relationship"]}
        for k in keys if k in people
    ]
