"""core/vision.py: cameras, the motion-gated event log, and the identity layer on top of
it -- known_people matching, unknown_faces dedup, and the asked/resolved lifecycle that
gates an enrollment question. No GPU/ML dependency here: everything is plain sqlite plus
numpy vector math on hand-written embeddings.
"""
import pytest

from assistant.core import vision


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "vision.db")
    vision.init_vision_db(path)
    return path


# --- cameras -------------------------------------------------------------------------

def test_add_camera_accepts_local_kind(db_path):
    """laptop1's own webcam, not mjpeg/rtsp."""
    cam = vision.add_camera(db_path, "laptop1", "laptop1 webcam", "0", kind="local")
    assert cam["kind"] == "local" and cam["url"] == "0"


def test_add_camera_rejects_a_bad_kind(db_path):
    with pytest.raises(ValueError):
        vision.add_camera(db_path, "cam1", "Cam", "http://x", kind="bogus")


def test_add_camera_is_an_upsert(db_path):
    vision.add_camera(db_path, "touch1", "Touch1", "http://old")
    vision.add_camera(db_path, "touch1", "Touch1", "http://new")
    cams = vision.list_cameras(db_path)
    assert len(cams) == 1 and cams[0]["url"] == "http://new"


# --- cosine similarity -----------------------------------------------------------------

def test_cosine_similarity_of_identical_vectors_is_one():
    v = [1.0, 2.0, 3.0, 4.0]
    assert vision.cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_of_orthogonal_vectors_is_zero():
    assert vision.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_handles_a_zero_vector():
    assert vision.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


# --- known people / matching -----------------------------------------------------------

def _vec(*vals):
    """A small hand-written 'embedding' -- real ones are 512-d, but the matching logic
    only cares about cosine geometry, which these exercise identically."""
    return list(vals)


def test_enroll_and_match_the_same_person(db_path):
    key = vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0, 0.0))
    assert key == "dug"
    match = vision.find_known_match(db_path, _vec(0.99, 0.01, 0.0))
    assert match and match["key"] == "dug" and match["name"] == "Dug"


def test_find_known_match_returns_none_below_threshold(db_path):
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0, 0.0))
    assert vision.find_known_match(db_path, _vec(0.0, 1.0, 0.0)) is None


def test_re_enrolling_the_same_name_adds_a_sample_instead_of_erroring(db_path):
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0, 0.0))
    vision.enroll_known_person(db_path, "dug", _vec(0.9, 0.1, 0.0))  # different case/spacing
    person = vision.get_known_person(db_path, "dug")
    assert person["sample_count"] == 2 and len(person["embeddings"]) == 2


def test_embeddings_are_capped_per_person(db_path):
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0))
    for i in range(20):
        vision.add_person_embedding(db_path, "dug", _vec(1.0, i * 0.001))
    person = vision.get_known_person(db_path, "dug")
    assert len(person["embeddings"]) == vision.MAX_EMBEDDINGS_PER_PERSON


def test_slugify_is_stable_across_case_and_spacing():
    assert vision.slugify_person_key("Dug") == vision.slugify_person_key(" dug ")


# --- unknown faces ---------------------------------------------------------------------

def test_upsert_unknown_face_creates_a_row(db_path):
    row = vision.upsert_unknown_face(db_path, "touch1", _vec(1.0, 0.0, 0.0))
    assert row["seen_count"] == 1 and row["camera_key"] == "touch1"


def test_upsert_unknown_face_merges_a_repeat_sighting(db_path):
    first = vision.upsert_unknown_face(db_path, "touch1", _vec(1.0, 0.0, 0.0))
    again = vision.upsert_unknown_face(db_path, "laptop1", _vec(0.99, 0.01, 0.0))
    assert again["id"] == first["id"]
    assert vision.get_unknown_face(db_path, first["id"])["seen_count"] == 2


def test_upsert_unknown_face_does_not_merge_a_different_stranger(db_path):
    first = vision.upsert_unknown_face(db_path, "touch1", _vec(1.0, 0.0, 0.0))
    other = vision.upsert_unknown_face(db_path, "touch1", _vec(0.0, 1.0, 0.0))
    assert other["id"] != first["id"]


def test_resolved_unknown_faces_are_excluded_from_dedup_matching(db_path):
    """A new stranger who happens to resemble someone already enrolled must not silently
    inherit that old unresolved row."""
    row = vision.upsert_unknown_face(db_path, "touch1", _vec(1.0, 0.0, 0.0))
    vision.resolve_unknown_face(db_path, row["id"], "someone")
    new_row = vision.upsert_unknown_face(db_path, "touch1", _vec(0.99, 0.01, 0.0))
    assert new_row["id"] != row["id"]


def test_unasked_unknown_faces_respects_the_sighting_floor(db_path):
    row = vision.upsert_unknown_face(db_path, "touch1", _vec(1.0, 0.0, 0.0))
    assert vision.unasked_unknown_faces(db_path, min_seen=3) == []
    vision.upsert_unknown_face(db_path, "touch1", _vec(0.99, 0.0, 0.0))
    vision.upsert_unknown_face(db_path, "touch1", _vec(0.98, 0.0, 0.0))
    due = vision.unasked_unknown_faces(db_path, min_seen=3)
    assert len(due) == 1 and due[0]["id"] == row["id"]


def test_marking_asked_removes_it_from_the_unasked_list(db_path):
    row = vision.upsert_unknown_face(db_path, "touch1", _vec(1.0, 0.0))
    vision.mark_unknown_face_asked(db_path, row["id"])
    assert vision.unasked_unknown_faces(db_path, min_seen=1) == []


# --- presence + identity ----------------------------------------------------------------

def test_known_people_present_joins_identity_onto_presence(db_path):
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0))
    vision.record_event(db_path, "touch1", "identified", label="Dug", person_key="dug")
    present = vision.known_people_present(db_path)
    assert present == [{"key": "dug", "name": "Dug", "relationship": "household"}]


def test_known_people_present_is_empty_with_no_recent_events(db_path):
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0))
    assert vision.known_people_present(db_path) == []


def test_presence_now_still_counts_unknown_people(db_path):
    vision.record_event(db_path, "touch1", "unknown_person", detail="unknown_face:1")
    cams = vision.presence_now(db_path)
    assert cams["touch1"]["unknown_people"] == 1
