"""Identity: known people, unknown faces, and the presence gate other modules use to
decide whether personal/sensitive tools are safe to hand over.
"""
import json

import pytest

from assistant.core import vision


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    vision.init_vision_db(path)
    return path


def _embedding(seed: float) -> list:
    return [seed, 1.0 - seed, 0.5]


def test_migration_runs_idempotently(db_path):
    # Every process start calls init_vision_db again; it must not blow up on columns
    # that already exist.
    vision.init_vision_db(db_path)
    vision.init_vision_db(db_path)


def test_enroll_and_match_a_person(db_path):
    key = vision.enroll_person(db_path, "Dug", _embedding(0.9))
    match = vision.match_embedding(db_path, _embedding(0.9))
    assert match is not None
    assert match[0] == key
    assert match[1] == "Dug"


def test_match_embedding_respects_threshold(db_path):
    vision.enroll_person(db_path, "Dug", _embedding(0.9))
    assert vision.match_embedding(db_path, [0.0, 0.0, -1.0], threshold=0.9) is None


def test_add_embedding_grows_a_persons_gallery(db_path):
    key = vision.enroll_person(db_path, "Dug", _embedding(0.9))
    assert vision.add_embedding(db_path, key, _embedding(0.8))
    person = vision.get_person_by_key(db_path, key)
    assert person["sample_count"] == 2
    assert len(json.loads(person["embeddings"])) == 2


def test_add_embedding_caps_the_gallery_size(db_path):
    key = vision.enroll_person(db_path, "Dug", _embedding(0.1))
    for i in range(vision.MAX_EMBEDDINGS_PER_PERSON + 5):
        vision.add_embedding(db_path, key, _embedding(0.1 + i * 0.001))
    person = vision.get_person_by_key(db_path, key)
    assert len(json.loads(person["embeddings"])) == vision.MAX_EMBEDDINGS_PER_PERSON


def test_unknown_face_is_deduplicated_by_similarity(db_path):
    first = vision.upsert_unknown_face(db_path, "front_door", _embedding(0.9), "thumb1.jpg")
    second = vision.upsert_unknown_face(db_path, "front_door", _embedding(0.91), "thumb2.jpg")
    assert first["id"] == second["id"]
    assert second["seen_count"] == 2


def test_unknown_face_on_a_different_camera_is_not_merged(db_path):
    first = vision.upsert_unknown_face(db_path, "front_door", _embedding(0.9), "thumb1.jpg")
    other = vision.upsert_unknown_face(db_path, "kitchen", _embedding(0.9), "thumb2.jpg")
    assert first["id"] != other["id"]


def test_resolved_unknown_face_is_no_longer_merged_into(db_path):
    face = vision.upsert_unknown_face(db_path, "front_door", _embedding(0.9), "thumb1.jpg")
    key = vision.enroll_person(db_path, "Dug", _embedding(0.9))
    vision.resolve_unknown_face(db_path, face["id"], key)
    again = vision.upsert_unknown_face(db_path, "front_door", _embedding(0.9), "thumb2.jpg")
    assert again["id"] != face["id"]


def test_unasked_unknown_faces_respects_the_seen_count_floor(db_path):
    face = vision.upsert_unknown_face(db_path, "front_door", _embedding(0.9), "t.jpg")
    assert vision.unasked_unknown_faces(db_path, min_seen=3) == []
    vision.upsert_unknown_face(db_path, "front_door", _embedding(0.9), "t.jpg")
    vision.upsert_unknown_face(db_path, "front_door", _embedding(0.9), "t.jpg")
    due = vision.unasked_unknown_faces(db_path, min_seen=3)
    assert len(due) == 1 and due[0]["id"] == face["id"]


def test_identify_present_reflects_a_recent_identified_event(db_path):
    key = vision.enroll_person(db_path, "Dug", _embedding(0.9))
    assert vision.identify_present(db_path, "front_door") is None
    vision.record_event(db_path, "front_door", "identified", label="Dug",
                        confidence=0.9, person_key=key)
    present = vision.identify_present(db_path, "front_door")
    assert present is not None
    assert present["person_key"] == key
    assert present["name"] == "Dug"


def test_identify_present_ignores_stale_sightings(db_path):
    key = vision.enroll_person(db_path, "Dug", _embedding(0.9))
    vision.record_event(db_path, "front_door", "identified", label="Dug",
                        confidence=0.9, person_key=key)
    assert vision.identify_present(db_path, "front_door", within_seconds=0) is None


def test_camera_device_link_round_trips(db_path):
    vision.add_camera(db_path, "front_door", "Front Door", "http://cam/snapshot")
    assert vision.camera_for_device(db_path, "kiosk1") is None
    vision.set_camera_device(db_path, "front_door", "kiosk1")
    camera = vision.camera_for_device(db_path, "kiosk1")
    assert camera is not None and camera["key"] == "front_door"
