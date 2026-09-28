"""Covers /api/inventory: owner-only, and the overview -> folder -> tag round trip against a
small inventory.db written the way scripts/inventory_drives.py writes one."""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db
from assistant.core import drive_inventory as inv
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    ssh_hosts: dict = field(default_factory=dict)
    users: list = field(default_factory=list)
    timezone: str = "America/New_York"
    web_session_secret: str = "test-secret"
    claude_tools_api_key: str | None = None
    ha_conversation_api_key: str | None = None


class FakeLLM:
    def chat(self, messages, tools=None, think=False):
        return {"role": "assistant", "content": "hi"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "jarvis.db")
    db.init_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    db.upsert_user(path, "222", "Partner", "partner")
    inv_path = inv.default_db_path(path)
    inv.init_db(inv_path)
    host = inv.upsert_host(inv_path, "jarvisbox", "127.0.0.1", "windows")
    vol = inv.upsert_volume(inv_path, host, "winserial:ABCD", "F:\\", label="TranferDSK")
    w = inv.ScanWriter(inv_path, vol, inv.start_scan(inv_path, vol))
    w.add("f", 5000, 1.5e9, "Old Laptop/Pictures/IMG_0001.jpg")
    w.add("f", 900, 1.5e9, "Old Laptop/Etsy/logo.svg")
    w.finish()
    return path


def _client(db_path, who="Dug"):
    cfg = FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="pw"),
        UserConfig(telegram_chat_id="222", display_name="Partner", role="partner", web_password="pw2"),
    ])
    c = TestClient(create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None))
    assert c.post("/api/login", json={"name": who, "password": "pw" if who == "Dug" else "pw2"}).status_code == 200
    return c


def test_partner_is_refused(db_path):
    assert _client(db_path, "Partner").get("/api/inventory/overview").status_code == 403


def test_overview_folder_and_tag_round_trip(db_path):
    c = _client(db_path)
    ov = c.get("/api/inventory/overview").json()
    vol = ov["volumes"][0]
    assert vol["label"] == "TranferDSK" and vol["by_category"] == {"photos": 5000, "side_hustle": 900}

    root = c.get(f"/api/inventory/folders/{vol['root_id']}").json()
    laptop = root["children"][0]
    assert laptop["name"] == "Old Laptop" and laptop["total_bytes"] == 5900

    resp = c.post(f"/api/inventory/folders/{laptop['id']}/tag", json={"category": "documents"})
    assert resp.status_code == 200
    kids = c.get(f"/api/inventory/folders/{laptop['id']}").json()["children"]
    assert {k["name"]: k["category_source"] for k in kids} == {"Pictures": "inherited_tag", "Etsy": "inherited_tag"}

    assert c.post(f"/api/inventory/folders/{laptop['id']}/tag", json={"category": "nonsense"}).status_code == 400


def test_search_finds_folders_by_name(db_path):
    results = _client(db_path).get("/api/inventory/search?q=etsy").json()["results"]
    assert [r["name"] for r in results] == ["Etsy"]
