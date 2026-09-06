"""Covers /api/crypto/dashboard — the one call the crypto tab renders itself from,
same "single coherent snapshot" reasoning as /api/agents/status.

Membership in the dashboard is read from data_feeds rather than a fixed pair of employee
keys, so these tests hire employees with varying feeds rather than assuming names.
"""
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from assistant.config import UserConfig
from assistant.core import db, market_data, paper_trading, staff
from assistant.web.app import create_app


@dataclass
class FakeConfig:
    db_path: str
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
    staff.init_staff_db(path)
    market_data.init_market_db(path)
    paper_trading.init_paper_db(path)
    db.upsert_user(path, "111", "Dug", "owner")
    db.upsert_user(path, "222", "Partner", "partner")
    return path


@pytest.fixture
def cfg(db_path):
    return FakeConfig(db_path=db_path, users=[
        UserConfig(telegram_chat_id="111", display_name="Dug", role="owner", web_password="ownerpass"),
        UserConfig(telegram_chat_id="222", display_name="Partner", role="partner", web_password="partnerpass"),
    ])


@pytest.fixture
def client(cfg):
    app = create_app(cfg, FakeLLM(), era=None, calendar=None, static_dir=None)
    return TestClient(app)


def _login(client, password):
    r = client.post("/api/login", json={"name": "Dug" if password == "ownerpass" else "Partner",
                                        "password": password})
    assert r.status_code == 200
    return client


def test_requires_login(client):
    assert client.get("/api/crypto/dashboard").status_code == 401


def test_partner_is_refused_owner_only_financial_data(client):
    _login(client, "partnerpass")
    assert client.get("/api/crypto/dashboard").status_code == 403


def test_empty_roster_returns_empty_lists_not_an_error(client, cfg):
    _login(client, "ownerpass")
    r = client.get("/api/crypto/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert body["analysts"] == []
    assert body["traders"] == []
    assert body["trades"] == []
    assert "feed_status" in body


def test_membership_comes_from_data_feeds_not_a_fixed_pair_of_names(client, cfg):
    """Hiring a second analyst under any title must show up here without a code change —
    the dashboard must not be reading two hardcoded employee keys."""
    a1 = staff.hire(cfg.db_path, "Macro Analyst",
                    "Fifteen years of macro and on-chain research experience.")["key"]
    a2 = staff.hire(cfg.db_path, "Altcoin Scout",
                    "Ten years scouting emerging tokens and DeFi protocols.")["key"]
    trader = staff.hire(cfg.db_path, "Swing Trader",
                        "Twelve years of active trading across volatile markets.")["key"]
    staff.set_data_feeds(cfg.db_path, a1, "market")
    staff.set_data_feeds(cfg.db_path, a2, "market")
    staff.set_data_feeds(cfg.db_path, trader, "market,paper")

    _login(client, "ownerpass")
    body = client.get("/api/crypto/dashboard").json()
    assert {a["title"] for a in body["analysts"]} == {"Macro Analyst", "Altcoin Scout"}
    assert {t["title"] for t in body["traders"]} == {"Swing Trader"}
    assert body["traders"][0]["traded"] is True
    assert body["analysts"][0]["traded"] is False


def test_an_employee_without_the_market_feed_does_not_appear(client, cfg):
    staff.hire(cfg.db_path, "Front End Designer", "Ten years of React and CSS experience.")
    _login(client, "ownerpass")
    body = client.get("/api/crypto/dashboard").json()
    assert body["analysts"] == [] and body["traders"] == []


def test_analyst_reasoning_is_returned_in_full_not_summarized(client, cfg):
    key = staff.hire(cfg.db_path, "Research Analyst",
                     "Fifteen years of markets research experience.",
                     cadence="interval", interval_minutes=15,
                     standing_assignment="watch the majors")["key"]
    staff.set_data_feeds(cfg.db_path, key, "market")

    class RecommendingLLM:
        def research(self, prompt, system_prompt=None, timeout=None, **kwargs):
            return ("BTC: HOLD, flat and range-bound.\n"
                    'DASH: WATCH, up 30% on a Grayscale note.\n'
                    '{"alert": false, "urgency": "low", "headline": "quiet"}')

    staff.assign(cfg.db_path, RecommendingLLM(), key, "watch the majors")

    _login(client, "ownerpass")
    body = client.get("/api/crypto/dashboard").json()
    output = body["analysts"][0]["runs"][0]["output"]
    assert "Grayscale note" in output, "the full reasoning must survive, not a summary of it"


def test_trades_and_rejections_carry_their_stated_reason(client, cfg):
    market_data.init_market_db(cfg.db_path)
    import sqlite3
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("""INSERT INTO market_coins (code, name, rank, rate, present,
                                              first_seen, last_seen, updated_at)
                    VALUES ('SOL','Solana',5,200.0,1,'2026-01-01T00:00:00+00:00',
                            '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""")
    conn.commit(); conn.close()

    paper_trading.execute_orders(
        cfg.db_path, [{"side": "buy", "code": "SOL", "usd": 100, "reason": "breakout confirmed"}],
        staff_key="trader")
    paper_trading.execute_orders(
        cfg.db_path, [{"side": "sell", "code": "SOL", "qty": 999, "reason": "take profit"}],
        staff_key="trader")

    _login(client, "ownerpass")
    body = client.get("/api/crypto/dashboard").json()
    assert body["trades"][0]["reason"] == "breakout confirmed"
    assert "cannot sell" in body["rejections"][0]["reason"]
    assert body["book"]["equity"] == pytest.approx(9999.9, abs=0.01)
