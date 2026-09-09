"""The per-camera pipeline: motion gates detection, YOLO gates face identification, and
a repeatedly-seen unfamiliar face becomes a Review-page item rather than a silent
enrollment. Uses fake detector/face-identifier stand-ins -- real YOLO/InsightFace need a
GPU-capable .venv-vision this test suite doesn't require.
"""
import numpy as np
import pytest

from assistant.core import business_db, db, vision
from assistant.core.vision_runtime import VisionRuntime


class FakeDetection:
    def __init__(self, kind, confidence, box=(0, 0, 10, 10), label="person"):
        self.kind = kind
        self.confidence = confidence
        self.box = box
        self.label = label


class FakeDetector:
    def __init__(self, detections):
        self._detections = detections

    def detect(self, frame):
        return self._detections

    def summarise(self, detections):
        people = [d for d in detections if d.kind == "person"]
        pets = [d for d in detections if d.kind == "pet"]
        return {
            "person_count": len(people), "pet_count": len(pets),
            "pets": sorted({d.label for d in pets}), "has_person": bool(people),
            "has_pet": bool(pets),
            "top_person_confidence": max((d.confidence for d in people), default=0.0),
        }


class FakeFace:
    def __init__(self, embedding, box=(0, 0, 10, 10), det_score=0.9):
        self.embedding = embedding
        self.box = box
        self.det_score = det_score


class FakeFaceIdentifier:
    def __init__(self, faces):
        self._faces = faces

    def embed_faces(self, frame):
        return self._faces


class _AlwaysMoved:
    def update(self, frame):
        return True, 1.0


class _SimpleFrameSource:
    def __init__(self, camera):
        pass

    def snapshot(self, timeout=5.0):
        return np.zeros((20, 20, 3), dtype=np.uint8)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    vision.init_vision_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    vision.add_camera(path, "front_door", "Front Door", "http://cam/snapshot")
    return path


@pytest.fixture
def owner_id(db_path):
    return db.get_user_by_chat_id(db_path, "111")["id"]


@pytest.fixture(autouse=True)
def fake_frame_source(monkeypatch):
    monkeypatch.setattr(vision, "FrameSource", _SimpleFrameSource)


def _runtime(db_path, owner_id, detections, faces, tmp_path, **kwargs):
    runtime = VisionRuntime(
        db_path, owner_id, FakeDetector(detections), FakeFaceIdentifier(faces), str(tmp_path), **kwargs,
    )
    # Skip the real motion gate so a single fake frame always reads as motion -- the
    # detector/identifier fakes are what this test is really exercising.
    runtime._gate_for = lambda camera: _AlwaysMoved()
    return runtime


def test_an_identified_person_is_recorded(db_path, owner_id, tmp_path):
    key = vision.enroll_person(db_path, "Dug", [0.9, 0.1, 0.0])
    runtime = _runtime(
        db_path, owner_id, [FakeDetection("person", 0.9)], [FakeFace([0.9, 0.1, 0.0])], tmp_path,
        face_match_threshold=0.5,
    )
    camera = vision.list_cameras(db_path)[0]
    runtime.process_camera(camera)

    events = vision.recent_events(db_path, "front_door", "identified")
    assert len(events) == 1
    assert events[0]["person_key"] == key


def test_an_unmatched_face_becomes_an_unknown_face_and_eventually_a_review_item(db_path, owner_id, tmp_path):
    runtime = _runtime(
        db_path, owner_id, [FakeDetection("person", 0.9)], [FakeFace([0.1, 0.1, 0.1])], tmp_path,
        face_match_threshold=0.99, unknown_face_ask_after=2,
    )
    camera = vision.list_cameras(db_path)[0]

    runtime.process_camera(camera)
    assert business_db.count_pending_reviews(db_path, owner_id) == 0  # seen once, not yet asked

    runtime.process_camera(camera)
    assert business_db.count_pending_reviews(db_path, owner_id) == 1


def test_a_person_with_no_visible_face_still_records_presence(db_path, owner_id, tmp_path):
    runtime = _runtime(db_path, owner_id, [FakeDetection("person", 0.9)], [], tmp_path)
    camera = vision.list_cameras(db_path)[0]

    runtime.process_camera(camera)
    assert len(vision.recent_events(db_path, "front_door", "person")) == 1


def test_a_pet_is_recorded_without_touching_face_identification(db_path, owner_id, tmp_path):
    runtime = _runtime(db_path, owner_id, [FakeDetection("pet", 0.9, label="dog")], [], tmp_path)
    camera = vision.list_cameras(db_path)[0]

    runtime.process_camera(camera)
    assert len(vision.recent_events(db_path, "front_door", "pet")) == 1


def test_no_motion_means_no_detection_call_at_all(db_path, owner_id, tmp_path):
    class _NeverMoved:
        def update(self, frame):
            return False, 0.0

    class _ExplodingDetector:
        def detect(self, frame):
            raise AssertionError("detect() should not run when there is no motion")

    runtime = VisionRuntime(db_path, owner_id, _ExplodingDetector(), None, str(tmp_path))
    runtime._gate_for = lambda camera: _NeverMoved()
    camera = vision.list_cameras(db_path)[0]

    runtime.process_camera(camera)  # must not raise
    assert vision.recent_events(db_path, "front_door") == []
