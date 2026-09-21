"""The company dashboard.

Four questions in one screen, and the one that carries the request is the last: "i want to
see their thoughts on the market research and how they are coming to the conclusions."
That cannot be answered from a status column, so it is read from the agents' journals --
and these tests pin the two ways such a panel lies: showing zeros for data it does not
have, and showing nothing when it simply could not read.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import agent_notes, business_db, db, owner_requests, store_policy
from assistant.core.obsidian_client import ObsidianClient
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
    generated_media_path: str
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
    path = str(tmp_path / "test.db")
    db.init_db(path)
    business_db.init_business_db(path)
    owner_requests.init_owner_requests(path)
    db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return ObsidianClient(str(root))


def _client(db_path, tmp_path, obsidian=None):
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    cfg = FakeConfig(db_path=db_path, generated_media_path=str(media),
                     users=[UserConfig(telegram_chat_id="111", display_name="Dug",
                                       role="owner", web_password="pw")])
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None,
                     obsidian=obsidian)
    c = TestClient(app)
    assert c.post("/api/login", json={"name": "Dug", "password": "pw"}).status_code == 200
    return c


def test_requires_login(db_path, tmp_path):
    media = tmp_path / "m"; media.mkdir()
    cfg = FakeConfig(db_path=db_path, generated_media_path=str(media), users=[])
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    assert TestClient(app).get("/api/company").status_code == 401


def test_the_rate_dial_is_shown_with_its_reason(db_path, tmp_path):
    store_policy.set_products_per_day(db_path, 3, reason="pushing for the holiday run")
    body = _client(db_path, tmp_path).get("/api/company").json()
    assert body["policy"]["products_per_day"] == 3
    assert body["policy"]["last_change"]["reason"] == "pushing for the holiday run"


def test_a_paused_store_says_so(db_path, tmp_path):
    store_policy.set_products_per_day(db_path, 0, reason="pausing")
    assert _client(db_path, tmp_path).get("/api/company").json()["policy"]["paused"] is True


def test_the_free_only_marketing_stance_is_visible(db_path, tmp_path):
    """He should be able to see at a glance that nothing is spending his money."""
    body = _client(db_path, tmp_path).get("/api/company").json()
    assert body["policy"]["marketing_is_free_only"] is True
    assert body["policy"]["marketing_budget_cents"] == 0


class TestEngagement:
    def test_missing_engagement_is_absent_not_zero(self, db_path, tmp_path):
        """A dashboard showing '0 views' for a product nobody has ever posted reads as
        'nobody engaged' rather than 'nothing has been posted'. One is a business problem
        and the other is a plumbing problem, and they must not look the same."""
        made = _client(db_path, tmp_path).get("/api/company").json()["made"]
        assert made["engagement"] is None
        assert "posting access" in made["engagement_source"]


class TestTheirThinking:
    def test_agent_journals_are_surfaced(self, db_path, tmp_path, vault):
        agent_notes.write_journal(vault, "trend_scout", "Found 3 leads",
                                  reasoning="Ruled out anything already extended.")
        body = _client(db_path, tmp_path, obsidian=vault).get("/api/company").json()
        agents = [j["agent"] for j in body["thinking"]["journals"]]
        assert "trend_scout" in agents
        assert "already extended" in body["thinking"]["journals"][0]["notes"]

    def test_no_vault_says_so_rather_than_showing_nothing(self, db_path, tmp_path):
        """'No notes' and 'could not read the notes' are different states, and only one of
        them is a problem to go and fix."""
        thinking = _client(db_path, tmp_path).get("/api/company").json()["thinking"]
        assert thinking["journals"] == []
        assert "not wired" in (thinking["unavailable"] or "").lower()

    def test_an_empty_vault_is_not_reported_as_broken(self, db_path, tmp_path, vault):
        thinking = _client(db_path, tmp_path, obsidian=vault).get("/api/company").json()["thinking"]
        assert thinking["journals"] == [] and thinking["unavailable"] is None

    def test_only_store_agents_appear(self, db_path, tmp_path, vault):
        """The crypto desk journals too, and it is not this business."""
        agent_notes.write_journal(vault, "crypto_day_trader", "bought", reasoning="momentum")
        agent_notes.write_journal(vault, "store_manager", "listed", reasoning="margin held")
        body = _client(db_path, tmp_path, obsidian=vault).get("/api/company").json()
        assert [j["agent"] for j in body["thinking"]["journals"]] == ["store_manager"]


def test_blockers_are_shown_so_it_never_reads_healthier_than_it_is(db_path, tmp_path):
    owner_requests.raise_request(db_path, 1, title="TikTok posting access", kind="account",
                                 name="tiktok_access_token", priority=1,
                                 blocks="all organic TikTok posting")
    blocked = _client(db_path, tmp_path).get("/api/company").json()["blocked"]
    assert blocked["open"] == 1
    assert blocked["items"][0]["blocks"] == "all organic TikTok posting"


def test_the_dashboard_renders_on_an_empty_business(db_path, tmp_path):
    """It will be empty for a while. Empty must not mean broken."""
    body = _client(db_path, tmp_path).get("/api/company").json()
    assert body["made"]["listings_total"] == 0
    assert body["pipeline"]["next_up"] == []
    assert body["catalog"]["personas"] == 0
