"""The mail-photo endpoint, over HTTP.

Mostly about the door. This is the one endpoint in the system a phone posts to from
outside the house, it carries photographs of his post, and it authenticates with a shared
key rather than a session -- so what is tested here is that the key is actually required,
and required BEFORE anything else happens.
"""
import io
import json
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.core import db as core_db, mail_photo, personal_db

KEY = "test-device-key-12345"

GOOD = json.dumps({
    "sender": "Internal Revenue Service", "kind": "tax",
    "summary": "Notice CP14: balance due for tax year 2023.",
    "amount": 1240.00, "due_date": "2026-10-15", "account_ref": "6789",
    "action": "Pay or dispute the balance", "deadline_risk": True,
    "confidence": "high", "unreadable": [],
})


class FakeBridge:
    def __init__(self, result=GOOD, status="done"):
        self.result, self.status = result, status

    def run_sync(self, lane, kind, prompt, images=None, options=None, fmt=None):
        # The reader must CONSTRAIN the reply to JSON, not ask for it: the same
        # photo once produced 8000 characters of prose with no brace in it.
        assert fmt == "json", "the mail reader must demand json"
        return {"status": self.status, "result": self.result, "error": None}


@dataclass
class FakeConfig:
    db_path: str
    generated_media_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    device_api_key: str = KEY
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None


@pytest.fixture
def client(tmp_path):
    from assistant.config import UserConfig
    from assistant.web.app import create_app

    db_path = str(tmp_path / "web.db")
    core_db.init_db(db_path)
    personal_db.init_personal_db(db_path)
    mail_photo.init_mail_photo_db(db_path)
    core_db.upsert_user(db_path, "111", "Jack", "owner")

    cfg = FakeConfig(
        db_path=db_path,
        generated_media_path=str(tmp_path / "media"),
        users=[UserConfig(telegram_chat_id="111", display_name="Jack",
                          role="owner", web_password="pw")])
    app = create_app(cfg, None, era=None, calendar=None, bridge=FakeBridge(),
                     static_dir=None)
    yield TestClient(app), db_path


def _post(client, key=KEY, field="photo", data=b"jpegbytes"):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    files = {field: ("letter.jpg", io.BytesIO(data), "image/jpeg")}
    return client.post("/api/mail-photo", files=files, headers=headers)


class TestTheDoor:
    def test_no_key_is_refused(self, client):
        c, _ = client
        assert _post(c, key=None).status_code == 401

    def test_a_wrong_key_is_refused(self, client):
        c, _ = client
        assert _post(c, key="not-the-key").status_code == 401

    def test_the_key_is_checked_before_the_upload_is_validated(self, client):
        """Called inside the handler this answered 401-worthy requests with 422
        ("photo field required"), which leaks the shape and makes a bad field name and a
        bad key indistinguishable while setting the Shortcut up."""
        c, _ = client
        assert c.post("/api/mail-photo").status_code == 401
        assert c.post("/api/mail-photo",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401

    def test_a_wrong_field_name_with_a_good_key_is_a_422(self, client):
        """Once past the door, THIS is the error that should point at the Shortcut."""
        c, _ = client
        assert _post(c, field="image").status_code == 422

    def test_the_key_also_works_as_a_query_param(self, client):
        """Some Shortcut setups cannot set a header cleanly."""
        c, _ = client
        files = {"photo": ("a.jpg", io.BytesIO(b"x"), "image/jpeg")}
        assert c.post(f"/api/mail-photo?key={KEY}", files=files).status_code == 200


class TestPostingALetter:
    def test_the_upload_returns_before_the_model_has_read_anything(self, client):
        """The phone must not hold the connection for a twenty-second GPU call on top of
        a cellular upload -- that is what timed out the first real photo."""
        c, db_path = client
        out = _post(c).json()
        assert out["status"] == "received" and out["id"]
        assert "sender" not in out, "nothing is known yet at this point"

    def test_a_letter_is_read_filed_and_raises_a_task(self, client):
        """TestClient runs background tasks once the response is sent, so by here the
        reading has happened -- exactly as it will on the server a moment later."""
        c, db_path = client
        out = _post(c).json()
        piece = mail_photo.recent(db_path, 1)[0]
        assert piece["id"] == out["id"]
        assert piece["sender"] == "Internal Revenue Service"
        assert piece["status"] == "new", "no longer pending once read"
        assert piece["task_id"], "a tax notice with a deadline should raise a task"

    def test_the_photo_is_kept_on_disk(self, client):
        import pathlib

        c, _ = client
        out = _post(c).json()
        assert pathlib.Path(out["photo_path"]).read_bytes() == b"jpegbytes"

    def test_a_reading_that_explodes_does_not_lose_the_letter(self, client):
        """The background task runs with nobody listening. If it raised, the photo would
        vanish silently -- the exact failure this feature exists to prevent."""
        c, db_path = client

        class Exploding:
            def run_sync(self, *a, **k):
                raise RuntimeError("gpu on fire")

        c.app.state.bridge = Exploding()
        out = _post(c).json()
        assert out["status"] == "received"
        assert mail_photo.recent(db_path, 1)[0]["id"] == out["id"]

    def test_an_empty_photo_is_refused(self, client):
        c, _ = client
        assert _post(c, data=b"").status_code == 400

    def test_a_non_image_suffix_is_refused(self, client):
        c, _ = client
        files = {"photo": ("letter.exe", io.BytesIO(b"MZ"), "application/octet-stream")}
        r = c.post("/api/mail-photo", files=files,
                   headers={"Authorization": f"Bearer {KEY}"})
        assert r.status_code == 400

    def test_an_unreadable_photo_is_still_filed_with_a_task(self, client):
        """The letter must not vanish between the doormat and the desk."""
        c, db_path = client
        c.app.state.bridge = FakeBridge(result="I cannot read this")
        out = _post(c).json()
        piece = mail_photo.recent(db_path, 1)[0]
        assert piece["id"] == out["id"] and piece["task_id"], "a task saying so"
        assert piece["photo_path"], "and the photo is still kept"

    def test_junk_mail_is_filed_without_cluttering_his_task_list(self, client):
        c, db_path = client
        c.app.state.bridge = FakeBridge(result=json.dumps({
            "sender": "Local Pizza", "kind": "junk", "summary": "Coupons",
            "action": "Recycle it", "confidence": "high"}))
        _post(c)
        piece = mail_photo.recent(db_path, 1)[0]
        assert piece["kind"] == "junk" and piece["task_id"] is None
        assert len(mail_photo.recent(db_path, 1)) == 1, "still filed, just not a task"
