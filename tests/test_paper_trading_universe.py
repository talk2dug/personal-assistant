"""Ties the paper-trading order mechanics to the live tracked universe, so an employee
is handed the real coin list before it ever proposes a trade -- the missing piece that
let recommendations for untracked coins (TAO, AERO, WLD, IOST) reach the ledger only to
be rejected there instead of being grounded upstream.
"""
import sqlite3

import pytest

from assistant.core import market_data, paper_trading


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "paper.db")
    market_data.init_market_db(path)
    paper_trading.init_paper_db(path)
    conn = sqlite3.connect(path)
    now = "2026-09-11T00:00:00+00:00"
    conn.execute(
        """INSERT INTO market_coins (code, name, rank, rate, present,
                                     first_seen, last_seen, updated_at)
           VALUES ('SOL','Solana',5,200.0,1,?,?,?)""", (now, now, now))
    conn.commit()
    conn.close()
    paper_trading.ensure_account(path, starting_cash=10_000.0)
    return path


def test_order_instructions_includes_the_live_tracked_universe(db):
    out = paper_trading.order_instructions(db)
    assert "SOL" in out
    assert "TRACKED COIN UNIVERSE" in out
    assert '"side": "buy"' in out   # the underlying ORDER_INSTRUCTIONS survives intact


def test_order_instructions_still_formats_fee_and_cap(db):
    out = paper_trading.order_instructions(db, fee_pct=0.5, max_pct=10)
    assert "0.5" in out and "10" in out


def test_order_instructions_with_no_tracked_coins_warns_rather_than_invites_a_trade(tmp_path):
    empty_db = str(tmp_path / "empty.db")
    market_data.init_market_db(empty_db)
    paper_trading.init_paper_db(empty_db)
    paper_trading.ensure_account(empty_db)
    out = paper_trading.order_instructions(empty_db)
    assert "do not recommend or trade" in out.lower()


def test_rejection_for_an_untracked_coin_names_the_universe_size(db):
    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "TAO", "usd": 100}])
    assert not r["fills"]
    reason = r["rejections"][0]["reason"]
    assert "not in the tracked price cache" in reason   # unchanged, existing contract
    assert "1-coin tracked universe" in reason


def test_existing_paper_trading_behaviour_is_unaffected(db):
    """The import of market_data and the new order_instructions() must not change how
    execute_orders fills or rejects an ordinary order."""
    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 1000}])
    assert not r["rejections"]
    assert r["fills"][0]["qty"] == pytest.approx(5.0)
