"""Covers presence.py -- the gating decision itself."""
import pytest

from assistant.core import db, presence, vision


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    vision.init_vision_db(path)
    return path


def _confirm_owner_on_camera(db_path, device_id="touch1", camera_key="touch1_cam",
                             access_level="owner"):
    vision.add_camera(db_path, camera_key, "cam", "http://x/snapshot")
    vision.set_terminal_camera(db_path, device_id, camera_key)
    vision.enroll_known_person(db_path, "Dug", [1.0, 0.0, 0.0], access_level=access_level)
    vision.record_event(db_path, camera_key, "identified", person_key="dug", label="Dug")


# --- evaluate(): the three ways to end up unconfirmed, and the one way to pass -----

def test_terminal_with_no_camera_is_never_authorized(db_path):
    result = presence.evaluate(db_path, "jarvisaudio1")
    assert result.authorized is False
    assert result.camera_key is None
    assert "no camera" in result.reason


def test_camera_with_nobody_identified_is_not_authorized(db_path):
    vision.add_camera(db_path, "touch1_cam", "cam", "http://x/snapshot")
    vision.set_terminal_camera(db_path, "touch1", "touch1_cam")
    result = presence.evaluate(db_path, "touch1")
    assert result.authorized is False
    assert result.identity is None


def test_recognised_but_unauthorized_access_level_is_not_authorized(db_path):
    _confirm_owner_on_camera(db_path, access_level="guest")
    result = presence.evaluate(db_path, "touch1")
    assert result.authorized is False
    assert result.identity is not None       # recognised...
    assert "not authorized" in result.reason  # ...but not authorized


def test_owner_confirmed_on_camera_is_authorized(db_path):
    _confirm_owner_on_camera(db_path, access_level="owner")
    result = presence.evaluate(db_path, "touch1")
    assert result.authorized is True
    assert result.identity["key"] == "dug"


def test_authorized_access_levels_is_configurable(db_path):
    _confirm_owner_on_camera(db_path, access_level="household")
    default_result = presence.evaluate(db_path, "touch1")
    assert default_result.authorized is False

    widened_result = presence.evaluate(db_path, "touch1", authorized_access_levels={"owner", "household"})
    assert widened_result.authorized is True


# --- gate_contexts(): strips exactly the sensitive keys ----------------------------

def test_authorized_result_passes_contexts_through_unchanged(db_path):
    _confirm_owner_on_camera(db_path)
    result = presence.evaluate(db_path, "touch1")
    contexts = {"era": "ERA", "personal": "PERSONAL", "phone": "PHONE", "calendar": "CAL"}
    assert presence.gate_contexts(contexts, result) == contexts


def test_unauthorized_result_strips_only_sensitive_keys(db_path):
    result = presence.evaluate(db_path, "jarvisaudio1")  # no camera -> unauthorized
    contexts = {
        "era": "ERA", "personal": "PERSONAL", "mail": "MAIL", "business": "BIZ",
        "ccxt": "CCXT", "kroger": "KROGER", "letterstream": "LETTER",
        "phone": "PHONE", "calendar": "CAL", "home_assistant": "HA", "obsidian": "OBS",
    }
    gated = presence.gate_contexts(contexts, result)

    for key in presence.SENSITIVE_CONTEXT_KEYS:
        assert gated[key] is None
    for key in ("phone", "calendar", "home_assistant", "obsidian"):
        assert gated[key] == contexts[key]


def test_gate_contexts_does_not_mutate_the_input_dict(db_path):
    result = presence.evaluate(db_path, "jarvisaudio1")
    contexts = {"era": "ERA"}
    presence.gate_contexts(contexts, result)
    assert contexts == {"era": "ERA"}


# --- guest_user_id(): per-device isolation ------------------------------------------

def test_guest_user_id_is_stable_per_device(db_path):
    first = presence.guest_user_id(db_path, "touch1")
    second = presence.guest_user_id(db_path, "touch1")
    assert first == second


def test_guest_user_id_differs_across_devices(db_path):
    touch1 = presence.guest_user_id(db_path, "touch1")
    laptop1 = presence.guest_user_id(db_path, "laptop1")
    assert touch1 != laptop1


def test_guest_user_id_is_never_the_owner(db_path):
    owner_id = db.upsert_user(db_path, "111", "Dug", "owner")
    guest_id = presence.guest_user_id(db_path, "touch1")
    assert guest_id != owner_id
    assert db.get_user_by_id(db_path, guest_id)["role"] == "guest"
