"""Covers /api/kitchen/recipes: the recipe catalog's CRUD routes."""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, kitchen_db
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    generated_media_path: str = "generated"


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    kitchen_db.init_kitchen_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    db.upsert_user(path, "222", "Partner", "partner")
    return path


@pytest.fixture
def cfg(db_path, tmp_path):
    return FakeConfig(
        db_path=db_path,
        users=[
            UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="ownerpass"),
            UserConfig(telegram_chat_id="222", display_name="Partner", role="partner", web_password="partnerpass"),
        ],
        generated_media_path=str(tmp_path / "generated"),
    )


@pytest.fixture
def client(cfg):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    c = TestClient(app)
    c.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    return c


def test_requires_login(cfg):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    assert TestClient(app).get("/api/kitchen/recipes").status_code == 401


def test_requires_owner_role(cfg):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    client = TestClient(app)
    client.post("/api/login", json={"name": "Partner", "password": "partnerpass"})
    assert client.get("/api/kitchen/recipes").status_code == 403


def test_create_list_get_recipe(client):
    body = {
        "title": "Weeknight Chili",
        "servings": 6,
        "ingredients": [{"name": "ground beef", "quantity": "1", "unit": "lb"}],
        "steps": ["Brown the beef.", "Simmer 20 minutes."],
    }
    resp = client.post("/api/kitchen/recipes", json=body)
    assert resp.status_code == 200
    recipe_id = resp.json()["recipe_id"]

    listed = client.get("/api/kitchen/recipes").json()
    assert len(listed) == 1
    assert listed[0]["title"] == "Weeknight Chili"

    fetched = client.get(f"/api/kitchen/recipes/{recipe_id}").json()
    assert fetched["ingredients"] == body["ingredients"]
    assert fetched["steps"] == body["steps"]


def test_create_recipe_requires_title_ingredients_steps(client):
    assert client.post("/api/kitchen/recipes", json={"ingredients": [{"name": "x"}], "steps": ["a"]}).status_code == 400
    assert client.post("/api/kitchen/recipes", json={"title": "X", "steps": ["a"]}).status_code == 400
    assert client.post("/api/kitchen/recipes", json={"title": "X", "ingredients": [{"name": "x"}]}).status_code == 400


def test_search_by_title(client):
    client.post("/api/kitchen/recipes", json={
        "title": "Weeknight Chili", "ingredients": [{"name": "beef"}], "steps": ["cook"]})
    client.post("/api/kitchen/recipes", json={
        "title": "Pancakes", "ingredients": [{"name": "flour"}], "steps": ["mix"]})

    results = client.get("/api/kitchen/recipes?query=chili").json()
    assert len(results) == 1
    assert results[0]["title"] == "Weeknight Chili"


def test_update_and_delete_recipe(client):
    recipe_id = client.post("/api/kitchen/recipes", json={
        "title": "Pancakes", "ingredients": [{"name": "flour"}], "steps": ["mix"]}).json()["recipe_id"]

    resp = client.put(f"/api/kitchen/recipes/{recipe_id}", json={"servings": 4})
    assert resp.status_code == 200
    assert client.get(f"/api/kitchen/recipes/{recipe_id}").json()["servings"] == 4

    resp = client.delete(f"/api/kitchen/recipes/{recipe_id}")
    assert resp.status_code == 200
    assert client.get(f"/api/kitchen/recipes/{recipe_id}").status_code == 404


def test_get_update_delete_unknown_recipe_is_404(client):
    assert client.get("/api/kitchen/recipes/999").status_code == 404
    assert client.put("/api/kitchen/recipes/999", json={"servings": 2}).status_code == 404
    assert client.delete("/api/kitchen/recipes/999").status_code == 404


def test_recipes_are_owner_scoped(client, cfg):
    # A recipe created by one owner's db row must not be visible to a different
    # owner_user_id -- exercised directly since this fixture only has one owner login.
    other_owner_id = db.upsert_user(cfg.db_path, "333", "OtherOwner", "owner")
    kitchen_db.create_recipe(cfg.db_path, other_owner_id, "Secret Recipe", [{"name": "x"}], ["step"])

    listed = client.get("/api/kitchen/recipes").json()
    assert all(r["title"] != "Secret Recipe" for r in listed)


# --- photo upload / serving ---------------------------------------------------

class FakeBridge:
    def __init__(self, job):
        self._job = job

    def run_sync(self, agent, task_type, prompt, images=None, options=None, timeout=900):
        return self._job


def _client_with_bridge(cfg, bridge):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None, bridge=bridge)
    c = TestClient(app)
    c.post("/api/login", json={"name": "Dug", "password": "ownerpass"})
    return c


def test_from_photo_requires_a_configured_bridge(cfg):
    client = _client_with_bridge(cfg, bridge=None)
    resp = client.post("/api/kitchen/recipes/from-photo", files={"photo": ("card.jpg", b"fake-bytes", "image/jpeg")})
    assert resp.status_code == 503


def test_from_photo_returns_an_unsaved_draft(cfg):
    bridge = FakeBridge({"status": "done", "result": (
        '{"title": "Weeknight Chili", "servings": 6, '
        '"ingredients": [{"name": "ground beef", "quantity": "1", "unit": "lb"}], '
        '"steps": ["Brown the beef.", "Simmer 20 minutes."]}'
    )})
    client = _client_with_bridge(cfg, bridge)

    resp = client.post("/api/kitchen/recipes/from-photo", files={"photo": ("card.jpg", b"fake-bytes", "image/jpeg")})
    assert resp.status_code == 200
    draft = resp.json()
    assert draft["parsed"] is True
    assert draft["title"] == "Weeknight Chili"
    assert draft["photo_path"]

    # Nothing was saved to the catalog -- from-photo only ever returns a draft.
    assert client.get("/api/kitchen/recipes").json() == []

    # The photo itself was written to disk under generated_media_path.
    import pathlib
    assert pathlib.Path(draft["photo_path"]).is_file()


def test_from_photo_reports_a_failed_parse_without_losing_the_photo(cfg):
    bridge = FakeBridge({"status": "done", "result": "couldn't read the handwriting"})
    client = _client_with_bridge(cfg, bridge)

    resp = client.post("/api/kitchen/recipes/from-photo", files={"photo": ("card.jpg", b"fake-bytes", "image/jpeg")})
    assert resp.status_code == 200
    draft = resp.json()
    assert draft["parsed"] is False
    assert draft["photo_path"]
    import pathlib
    assert pathlib.Path(draft["photo_path"]).is_file()


def test_recipe_photo_is_served_when_present(client, cfg):
    import pathlib
    photo_dir = pathlib.Path(cfg.generated_media_path) / "kitchen_photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    photo_path = photo_dir / "test.jpg"
    photo_path.write_bytes(b"fake-jpeg-bytes")

    recipe_id = client.post("/api/kitchen/recipes", json={
        "title": "Pancakes", "ingredients": [{"name": "flour"}], "steps": ["mix"],
        "photo_path": str(photo_path),
    }).json()["recipe_id"]

    resp = client.get(f"/api/kitchen/recipes/{recipe_id}/photo")
    assert resp.status_code == 200
    assert resp.content == b"fake-jpeg-bytes"


def test_recipe_photo_404s_without_one(client):
    recipe_id = client.post("/api/kitchen/recipes", json={
        "title": "Pancakes", "ingredients": [{"name": "flour"}], "steps": ["mix"]}).json()["recipe_id"]
    assert client.get(f"/api/kitchen/recipes/{recipe_id}/photo").status_code == 404


def test_recipe_photo_refuses_a_path_outside_generated_media(client, cfg, tmp_path):
    outside_file = tmp_path / "outside.jpg"
    outside_file.write_bytes(b"not allowed")

    recipe_id = client.post("/api/kitchen/recipes", json={
        "title": "Pancakes", "ingredients": [{"name": "flour"}], "steps": ["mix"],
        "photo_path": str(outside_file),
    }).json()["recipe_id"]

    resp = client.get(f"/api/kitchen/recipes/{recipe_id}/photo")
    assert resp.status_code == 403
