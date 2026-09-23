"""paper_expectancy on BusinessClient (assistant/core/business_tools.py).

expectancy() itself is tested thoroughly in test_paper_trading.py; this only pins the
dispatch -- that the tool is registered and that calling it through BusinessClient reaches
paper_trading.expectancy() with the right db_path and passes `days` through.
"""
import sqlite3

import pytest

from assistant.core import business_tools, market_data, paper_trading


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "biz.db")


@pytest.fixture
def client(db_path):
    return business_tools.BusinessClient(db_path, owner_user_id=1)


def test_registered_in_the_schema():
    names = {t["function"]["name"] for t in business_tools.BUSINESS_TOOLS}
    assert "paper_expectancy" in names


def test_with_no_closed_trades_the_tool_says_so(client):
    result = client.call_tool("paper_expectancy", {})
    assert result["closed_trades"] == 0
    assert result["win_rate_pct"] is None


def test_a_closed_trade_reaches_the_tool(client, db_path):
    market_data.init_market_db(db_path)
    paper_trading.ensure_account(db_path)
    conn = sqlite3.connect(db_path)
    now = "2026-09-05T12:00:00+00:00"
    conn.execute(
        """INSERT INTO market_coins (code, name, rank, rate, present, first_seen,
                                     last_seen, updated_at)
           VALUES ('SOL','SOL',1,200.0,1,?,?,?)""", (now, now, now))
    conn.commit(); conn.close()
    paper_trading.execute_orders(
        db_path, [{"side": "buy", "code": "SOL", "usd": 1000, "stop_loss": 180.0,
                   "take_profit": 260.0}])
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE market_coins SET rate = 260.0 WHERE code = 'SOL'")
    conn.commit(); conn.close()
    paper_trading.check_stops(db_path)

    result = client.call_tool("paper_expectancy", {})
    assert result["closed_trades"] == 1
    assert result["win_rate_pct"] == 100.0


def test_days_argument_passes_through(client, db_path):
    paper_trading.ensure_account(db_path)
    result = client.call_tool("paper_expectancy", {"days": 7})
    assert result["closed_trades"] == 0


def test_paper_deposit_tops_up_the_account(client, db_path):
    paper_trading.ensure_account(db_path, starting_cash=500.0)
    result = client.call_tool("paper_deposit", {"amount": 500.0})
    assert result["starting_cash"] == 1000.0
    assert result["cash"] == 1000.0


def test_paper_deposit_refuses_a_bad_amount(client, db_path):
    paper_trading.ensure_account(db_path)
    result = client.call_tool("paper_deposit", {"amount": -50})
    assert "error" in result
