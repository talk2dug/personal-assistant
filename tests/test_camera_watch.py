"""core/camera_watch.py's CameraWatcher: the dedup and enrollment-trigger logic, driven
with a fake Detector/FaceRecognizer/frame source so this runs without torch/insightface
(those stay isolated in .venv-vision -- see requirements-vision.txt).
"""
import numpy as np
import pytest

from assistant.core import business_db, camera_watch, vision
from assistant.core.detector import Detection
from assistant.core.face_recognition import Face


class FakeDetector:
    """Hands back whatever detections the test queued, instead of running YOLO."""

    def __init__(self):
        self.queue: list[list[Detection]] = []
        self.device = "cpu"

    def detect(self, frame):
        return self.queue.pop(0) if self.queue else []

    def summarise(self, detections):
        people = [d for d in detections if d.kind == "person"]
        pets = [d for d in detections if d.kind == "pet"]
        return {
            "person_count": len(people), "pet_count": len(pets),
            "pets": sorted({d.label for d in pets}),
            "has_person": bool(people), "has_pet": bool(pets),
            "top_person_confidence": max((d.confidence for d in people), default=0.0),
        }


class FakeRecognizer:
    """Hands back whatever face (or None) the test queued for the next crop."""

    def __init__(self):
        self.queue: list[Face | None] = []

    def best_face(self, crop):
        return self.queue.pop(0) if self.queue else None


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    vision.init_vision_db(path)
    business_db.init_business_db(path)
    return path


@pytest.fixture
def camera():
    return {"key": "touch1", "name": "Touch1", "kind": "local", "url": "0", "motion_threshold": 0.012}


def _watcher(db_path, camera, tmp_path, detector=None, recognizer=None):
    return camera_watch.CameraWatcher(
        camera, db_path, owner_user_id=1, detector=detector or FakeDetector(),
        recognizer=recognizer, media_dir=str(tmp_path),
    )


def _frame():
    return np.random.randint(0, 255, (64, 64, 3), dtype=np.uint8)


def _person(conf=0.9):
    return Detection("person", "person", conf, (0, 0, 20, 20))


def test_identify_with_a_known_match_records_identified_once(db_path, camera, tmp_path):
    vision.enroll_known_person(db_path, "Dug", [1.0, 0.0, 0.0])
    recognizer = FakeRecognizer()
    recognizer.queue = [Face([1.0, 0.0, 0.0], (0, 0, 10, 10), 0.95)]
    watcher = _watcher(db_path, camera, tmp_path, recognizer=recognizer)

    key, unknown_id = watcher._identify(_frame(), _person())
    assert key == "dug" and unknown_id is None
    events = vision.recent_events(db_path, kind="identified")
    assert len(events) == 1 and events[0]["person_key"] == "dug"


def test_identify_with_no_face_returns_nothing(db_path, camera, tmp_path):
    recognizer = FakeRecognizer()
    recognizer.queue = [None]
    watcher = _watcher(db_path, camera, tmp_path, recognizer=recognizer)
    assert watcher._identify(_frame(), _person()) == (None, None)
    assert vision.recent_events(db_path) == []


def test_repeated_unknown_sightings_trigger_an_enrollment_review_item(db_path, camera, tmp_path):
    recognizer = FakeRecognizer()
    # Same embedding each time (a real stranger standing still across ticks) so
    # upsert_unknown_face merges them into one row's seen_count.
    recognizer.queue = [Face([0.0, 1.0, 0.0], (0, 0, 10, 10), 0.9) for _ in range(3)]
    watcher = _watcher(db_path, camera, tmp_path, recognizer=recognizer)

    for _ in range(3):
        watcher._identify(_frame(), _person())

    items = business_db.list_review_items(db_path, owner_user_id=1)
    assert len(items) == 1
    assert items[0]["ref_table"] == "unknown_faces"


def test_a_single_unknown_sighting_does_not_yet_trigger_a_review_item(db_path, camera, tmp_path):
    recognizer = FakeRecognizer()
    recognizer.queue = [Face([0.0, 1.0, 0.0], (0, 0, 10, 10), 0.9)]
    watcher = _watcher(db_path, camera, tmp_path, recognizer=recognizer)
    watcher._identify(_frame(), _person())
    assert business_db.list_review_items(db_path, owner_user_id=1) == []


def test_unknown_event_is_only_recorded_once_per_occupancy_session(db_path, camera, tmp_path):
    """Ticking on the same lingering stranger must not spam vision_events -- 'one row per
    meaningful change', per vision.py's own docstring. tick() carries the previous pass's
    unknown-face ids forward into _last_unknown_ids before the next _identify call; this
    simulates that directly since it drives _identify without a full tick()."""
    recognizer = FakeRecognizer()
    same_face = Face([0.0, 1.0, 0.0], (0, 0, 10, 10), 0.9)
    recognizer.queue = [same_face, same_face]
    watcher = _watcher(db_path, camera, tmp_path, recognizer=recognizer)

    _, unknown_id = watcher._identify(_frame(), _person())
    watcher._last_unknown_ids = {unknown_id}
    watcher._identify(_frame(), _person())

    events = vision.recent_events(db_path, kind="unknown_person")
    assert len(events) == 1


def test_no_recognizer_configured_is_a_safe_no_op(db_path, camera, tmp_path):
    watcher = _watcher(db_path, camera, tmp_path, recognizer=None)
    assert watcher._identify(_frame(), _person()) == (None, None)
