"""core/vision.py: cameras, the motion-gated event log, and the identity layer on top of
it -- known_people matching, unknown_faces dedup, the asked/resolved lifecycle that
gates an enrollment question, and the terminal-camera/identity_on_camera queries Room
Presence & Identity gating (core/presence.py) reads. No GPU/ML dependency here:
everything is plain sqlite plus numpy vector math on hand-written embeddings.
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


def test_get_camera_returns_none_when_missing(db_path):
    assert vision.get_camera(db_path, "nope") is None


def test_get_camera_returns_the_row(db_path):
    vision.add_camera(db_path, "kitchen", "Kitchen", "http://x", location="kitchen")
    assert vision.get_camera(db_path, "kitchen")["name"] == "Kitchen"


def test_find_camera_matches_by_location(db_path):
    vision.add_camera(db_path, "kitchen", "Kitchen", "http://x", location="kitchen")
    found = vision.find_camera(db_path, "kitchen")
    assert found is not None and found["key"] == "kitchen"


def test_find_camera_falls_back_to_name_then_key(db_path):
    vision.add_camera(db_path, "cam1", "Front Porch", "http://x")
    assert vision.find_camera(db_path, "Front Porch")["key"] == "cam1"
    assert vision.find_camera(db_path, "cam1")["key"] == "cam1"


def test_find_camera_with_no_match_returns_none(db_path):
    assert vision.find_camera(db_path, "attic") is None


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


# --- access_level (Room Presence & Identity) --------------------------------------------

def test_enroll_defaults_access_level_to_household(db_path):
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0))
    assert vision.get_known_person(db_path, "dug")["access_level"] == "household"


def test_enroll_accepts_an_explicit_access_level(db_path):
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0), access_level="owner")
    assert vision.get_known_person(db_path, "dug")["access_level"] == "owner"


def test_enroll_rejects_an_invalid_access_level(db_path):
    with pytest.raises(ValueError):
        vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0), access_level="superuser")


def test_re_enrolling_without_access_level_does_not_reset_it(db_path):
    """Approving a second sighting of someone already enrolled must not silently reset a
    previously-granted 'owner' access back down to the default."""
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0, 0.0), access_level="owner")
    vision.enroll_known_person(db_path, "Dug", _vec(0.9, 0.1, 0.0))
    assert vision.get_known_person(db_path, "dug")["access_level"] == "owner"


def test_set_person_access_level(db_path):
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0))
    assert vision.set_person_access_level(db_path, "dug", "owner") is True
    assert vision.get_known_person(db_path, "dug")["access_level"] == "owner"


def test_set_access_level_for_missing_person_returns_false(db_path):
    assert vision.set_person_access_level(db_path, "nobody", "owner") is False


def test_set_access_level_rejects_an_invalid_value(db_path):
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0))
    with pytest.raises(ValueError):
        vision.set_person_access_level(db_path, "dug", "superuser")


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


# --- terminal <-> camera mapping (Room Presence & Identity) ---------------------------

def test_terminal_camera_roundtrip(db_path):
    vision.add_camera(db_path, "touch1_cam", "Touch1 camera", "http://touch1.local:8080")
    vision.set_terminal_camera(db_path, "touch1", "touch1_cam")
    assert vision.get_terminal_camera(db_path, "touch1") == "touch1_cam"


def test_unassigned_terminal_has_no_camera(db_path):
    assert vision.get_terminal_camera(db_path, "jarvisaudio1") is None


def test_set_terminal_camera_is_upsert(db_path):
    vision.set_terminal_camera(db_path, "touch1", "cam_a")
    vision.set_terminal_camera(db_path, "touch1", "cam_b")
    assert vision.get_terminal_camera(db_path, "touch1") == "cam_b"


# --- identity_on_camera: what presence.py actually reads -----------------------------

def test_no_events_means_unconfirmed(db_path):
    assert vision.identity_on_camera(db_path, "touch1_cam") is None


def test_identified_event_resolves_to_the_known_person_row(db_path):
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0), access_level="owner")
    vision.record_event(db_path, "touch1_cam", "identified", label="Dug", person_key="dug", confidence=0.9)

    identity = vision.identity_on_camera(db_path, "touch1_cam")
    assert identity is not None
    assert identity["key"] == "dug"
    assert identity["access_level"] == "owner"


def test_unknown_person_after_identified_invalidates_immediately(db_path):
    """The most recent event wins, not 'was there an identified event in the window' --
    someone the house doesn't recognise stepping into frame after the owner leaves must
    not keep reading as the owner."""
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0), access_level="owner")
    vision.record_event(db_path, "touch1_cam", "identified", person_key="dug")
    vision.record_event(db_path, "touch1_cam", "unknown_person")

    assert vision.identity_on_camera(db_path, "touch1_cam") is None


def test_cleared_event_invalidates_identity(db_path):
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0), access_level="owner")
    vision.record_event(db_path, "touch1_cam", "identified", person_key="dug")
    vision.record_event(db_path, "touch1_cam", "cleared")

    assert vision.identity_on_camera(db_path, "touch1_cam") is None


def test_stale_identified_event_outside_window_is_unconfirmed(db_path):
    import sqlite3
    from contextlib import closing
    from datetime import datetime, timedelta, timezone

    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0), access_level="owner")
    event_id = vision.record_event(db_path, "touch1_cam", "identified", person_key="dug")

    stale_at = (datetime.now(timezone.utc) - timedelta(seconds=999)).isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE vision_events SET at = ? WHERE id = ?", (stale_at, event_id))
        conn.commit()

    assert vision.identity_on_camera(db_path, "touch1_cam", within_seconds=45) is None


def test_different_cameras_are_independent(db_path):
    vision.enroll_known_person(db_path, "Dug", _vec(1.0, 0.0), access_level="owner")
    vision.record_event(db_path, "cam_a", "identified", person_key="dug")

    assert vision.identity_on_camera(db_path, "cam_a") is not None
    assert vision.identity_on_camera(db_path, "cam_b") is None


# --- pending camera view (the "show me X" one-shot handoff to the web UI) ------------

def test_pending_camera_view_is_one_shot(db_path):
    vision.set_pending_camera_view(db_path, 1, {"key": "kitchen", "name": "Kitchen", "location": "kitchen"})
    assert vision.pop_pending_camera_view(db_path, 1) is not None
    assert vision.pop_pending_camera_view(db_path, 1) is None


def test_pending_camera_view_is_per_user(db_path):
    vision.set_pending_camera_view(db_path, 1, {"key": "kitchen"})
    assert vision.pop_pending_camera_view(db_path, 2) is None
    assert vision.pop_pending_camera_view(db_path, 1) is not None


def test_migrates_a_pre_view_json_pending_camera_views_table(tmp_path):
    """Real incident: a db created before pending_camera_views gained view_json (it used
    to be a bare `camera TEXT` column) left `CREATE TABLE IF NOT EXISTS` a permanent
    no-op against the old table, so every single web chat turn 500'd -- routes/chat.py's
    send_message unconditionally calls pop_pending_camera_view on every reply, not just
    ones that used show_camera. init_vision_db must repair this on an existing db, not
    just get it right on a fresh one (which is all the other pending_camera_view tests
    above actually exercise, via the db_path fixture's brand-new database)."""
    import sqlite3

    path = str(tmp_path / "old_vision.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE pending_camera_views (user_id INTEGER PRIMARY KEY, "
            "camera TEXT NOT NULL, created_at TEXT NOT NULL)"
        )

    vision.init_vision_db(path)

    vision.set_pending_camera_view(path, 1, {"key": "kitchen"})
    assert vision.pop_pending_camera_view(path, 1) == {"key": "kitchen"}
