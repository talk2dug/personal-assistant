"""The identity gate a voice terminal's /turn uses to decide whether personal/business
context is safe to hand over -- see devices.py's _resolve_speaker docstring for the
full policy this exercises.
"""
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from assistant.config import UserConfig
from assistant.core import db, vision
from assistant.web.routes.devices import _resolve_speaker


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    vision_enabled: bool = True
    vision_presence_window_seconds: int = 180


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    vision.init_vision_db(path)
    return path


@pytest.fixture
def owner_id(db_path):
    return db.upsert_user(db_path, "111", "Dug", "owner")


def _fake_request(cfg):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cfg=cfg)))


def _owner_user_config():
    return UserConfig(telegram_chat_id="111", display_name="Dug", role="owner")


def test_no_camera_linked_falls_back_to_owner_with_full_access(db_path, owner_id):
    cfg = FakeConfig(db_path=db_path, users=[_owner_user_config()])
    user_id, sensitive_ok, name = _resolve_speaker(_fake_request(cfg), "kiosk1")
    assert user_id == owner_id
    assert sensitive_ok is True
    assert name is None


def test_vision_disabled_always_behaves_like_before(db_path, owner_id):
    cfg = FakeConfig(db_path=db_path, users=[_owner_user_config()], vision_enabled=False)
    vision.add_camera(db_path, "front_door", "Front Door", "http://cam/snapshot")
    vision.set_camera_device(db_path, "front_door", "kiosk1")

    user_id, sensitive_ok, name = _resolve_speaker(_fake_request(cfg), "kiosk1")
    assert (user_id, sensitive_ok) == (owner_id, True)


def test_camera_linked_but_nobody_identified_blocks_sensitive_context(db_path, owner_id):
    cfg = FakeConfig(db_path=db_path, users=[_owner_user_config()])
    vision.add_camera(db_path, "front_door", "Front Door", "http://cam/snapshot")
    vision.set_camera_device(db_path, "front_door", "kiosk1")

    user_id, sensitive_ok, name = _resolve_speaker(_fake_request(cfg), "kiosk1")
    assert sensitive_ok is False
    assert name is None


def test_owner_identified_at_the_camera_gets_full_access(db_path, owner_id):
    cfg = FakeConfig(db_path=db_path, users=[_owner_user_config()])
    vision.add_camera(db_path, "front_door", "Front Door", "http://cam/snapshot")
    vision.set_camera_device(db_path, "front_door", "kiosk1")
    key = vision.enroll_person(db_path, "Dug", [0.1, 0.2, 0.3], linked_user_id=owner_id)
    vision.record_event(db_path, "front_door", "identified", label="Dug",
                        confidence=0.9, person_key=key)

    user_id, sensitive_ok, name = _resolve_speaker(_fake_request(cfg), "kiosk1")
    assert user_id == owner_id
    assert sensitive_ok is True
    assert name == "Dug"


def test_a_known_but_unlinked_person_does_not_get_sensitive_context(db_path, owner_id):
    cfg = FakeConfig(db_path=db_path, users=[_owner_user_config()])
    vision.add_camera(db_path, "front_door", "Front Door", "http://cam/snapshot")
    vision.set_camera_device(db_path, "front_door", "kiosk1")
    key = vision.enroll_person(db_path, "Guest", [0.4, 0.4, 0.4])  # not linked to any account
    vision.record_event(db_path, "front_door", "identified", label="Guest",
                        confidence=0.9, person_key=key)

    user_id, sensitive_ok, name = _resolve_speaker(_fake_request(cfg), "kiosk1")
    assert sensitive_ok is False
    assert name == "Guest"


def test_a_stale_identification_is_treated_as_nobody_present(db_path, owner_id):
    cfg = FakeConfig(db_path=db_path, users=[_owner_user_config()], vision_presence_window_seconds=0)
    vision.add_camera(db_path, "front_door", "Front Door", "http://cam/snapshot")
    vision.set_camera_device(db_path, "front_door", "kiosk1")
    key = vision.enroll_person(db_path, "Dug", [0.1, 0.2, 0.3], linked_user_id=owner_id)
    vision.record_event(db_path, "front_door", "identified", label="Dug",
                        confidence=0.9, person_key=key)

    _, sensitive_ok, _ = _resolve_speaker(_fake_request(cfg), "kiosk1")
    assert sensitive_ok is False
