"""Covers the identity-schema wiring added for Room Presence & Identity:
known_people/unknown_faces/terminal_cameras CRUD, and identity_on_camera -- the single
query presence.py's gating decision reads."""
import pytest

from assistant.core import vision


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    vision.init_vision_db(path)
    return path


def _camera(db_path, key="touch1_cam"):
    return vision.add_camera(db_path, key, "Touch1 camera", "http://touch1.local:8080")


# --- known people ----------------------------------------------------------------

def test_enroll_defaults_to_guest_access(db_path):
    person = vision.enroll_known_person(db_path, "dug", "Dug")
    assert person["access_level"] == "guest"
    assert person["sample_count"] == 0


def test_enroll_with_embedding_records_one_sample(db_path):
    person = vision.enroll_known_person(db_path, "dug", "Dug", embedding=[0.1, 0.2, 0.3])
    assert person["sample_count"] == 1


def test_invalid_access_level_rejected(db_path):
    with pytest.raises(ValueError):
        vision.enroll_known_person(db_path, "dug", "Dug", access_level="superuser")


def test_set_person_access_level(db_path):
    vision.enroll_known_person(db_path, "dug", "Dug")
    assert vision.set_person_access_level(db_path, "dug", "owner") is True
    assert vision.get_known_person(db_path, "dug")["access_level"] == "owner"


def test_set_access_level_for_missing_person_returns_false(db_path):
    assert vision.set_person_access_level(db_path, "nobody", "owner") is False


def test_add_face_embedding_sample_caps_at_max(db_path):
    vision.enroll_known_person(db_path, "dug", "Dug", embedding=[0.0])
    for i in range(20):
        vision.add_face_embedding_sample(db_path, "dug", [float(i)], max_samples=5)
    person = vision.get_known_person(db_path, "dug")
    assert person["sample_count"] == 5


def test_list_known_people_and_delete(db_path):
    vision.enroll_known_person(db_path, "dug", "Dug")
    vision.enroll_known_person(db_path, "gf", "GF")
    assert {p["key"] for p in vision.list_known_people(db_path)} == {"dug", "gf"}
    assert vision.delete_known_person(db_path, "dug") is True
    assert {p["key"] for p in vision.list_known_people(db_path)} == {"gf"}


# --- unknown faces -----------------------------------------------------------------

def test_unknown_face_lifecycle(db_path):
    face_id = vision.record_unknown_face(db_path, "touch1_cam", [0.1, 0.2])
    assert len(vision.list_unknown_faces(db_path)) == 1

    vision.mark_unknown_face_asked(db_path, face_id)
    vision.resolve_unknown_face(db_path, face_id, "dug")

    assert vision.list_unknown_faces(db_path, unresolved_only=True) == []
    assert vision.list_unknown_faces(db_path, unresolved_only=False)[0]["resolved_person_key"] == "dug"


# --- terminal <-> camera mapping ---------------------------------------------------

def test_terminal_camera_roundtrip(db_path):
    _camera(db_path)
    vision.set_terminal_camera(db_path, "touch1", "touch1_cam")
    assert vision.get_terminal_camera(db_path, "touch1") == "touch1_cam"


def test_unassigned_terminal_has_no_camera(db_path):
    assert vision.get_terminal_camera(db_path, "jarvisaudio1") is None


def test_set_terminal_camera_is_upsert(db_path):
    _camera(db_path, "cam_a")
    _camera(db_path, "cam_b")
    vision.set_terminal_camera(db_path, "touch1", "cam_a")
    vision.set_terminal_camera(db_path, "touch1", "cam_b")
    assert vision.get_terminal_camera(db_path, "touch1") == "cam_b"
    assert len(vision.list_terminal_cameras(db_path)) == 1


# --- identity_on_camera: what presence.py actually reads ---------------------------

def test_no_events_means_unconfirmed(db_path):
    _camera(db_path)
    assert vision.identity_on_camera(db_path, "touch1_cam") is None


def test_identified_event_resolves_to_the_known_person_row(db_path):
    _camera(db_path)
    vision.enroll_known_person(db_path, "dug", "Dug", access_level="owner")
    vision.record_event(db_path, "touch1_cam", "identified", label="Dug", person_key="dug", confidence=0.9)

    identity = vision.identity_on_camera(db_path, "touch1_cam")
    assert identity is not None
    assert identity["key"] == "dug"
    assert identity["access_level"] == "owner"


def test_unknown_person_after_identified_invalidates_immediately(db_path):
    _camera(db_path)
    vision.enroll_known_person(db_path, "dug", "Dug", access_level="owner")
    vision.record_event(db_path, "touch1_cam", "identified", person_key="dug")
    vision.record_event(db_path, "touch1_cam", "unknown_person")

    assert vision.identity_on_camera(db_path, "touch1_cam") is None


def test_cleared_event_invalidates_identity(db_path):
    _camera(db_path)
    vision.enroll_known_person(db_path, "dug", "Dug", access_level="owner")
    vision.record_event(db_path, "touch1_cam", "identified", person_key="dug")
    vision.record_event(db_path, "touch1_cam", "cleared")

    assert vision.identity_on_camera(db_path, "touch1_cam") is None


def test_stale_identified_event_outside_window_is_unconfirmed(db_path):
    import sqlite3
    from contextlib import closing
    from datetime import datetime, timedelta, timezone

    _camera(db_path)
    vision.enroll_known_person(db_path, "dug", "Dug", access_level="owner")
    event_id = vision.record_event(db_path, "touch1_cam", "identified", person_key="dug")

    stale_at = (datetime.now(timezone.utc) - timedelta(seconds=999)).isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE vision_events SET at = ? WHERE id = ?", (stale_at, event_id))
        conn.commit()

    assert vision.identity_on_camera(db_path, "touch1_cam", within_seconds=45) is None


def test_different_cameras_are_independent(db_path):
    _camera(db_path, "cam_a")
    _camera(db_path, "cam_b")
    vision.enroll_known_person(db_path, "dug", "Dug", access_level="owner")
    vision.record_event(db_path, "cam_a", "identified", person_key="dug")

    assert vision.identity_on_camera(db_path, "cam_a") is not None
    assert vision.identity_on_camera(db_path, "cam_b") is None
