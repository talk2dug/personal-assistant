"""Tests for the tracked-universe helpers that ground crypto recommendations in what
LiveCoinWatch is actually feeding, rather than a model's general knowledge of "big"
coins -- the gap that let TAO/AERO/WLD/IOST through as unfillable recommendations
(roughly a 40% rejection rate on the day-trader's proposed orders).
"""
import sqlite3

import pytest

from assistant.core import market_data


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "market.db")
    market_data.init_market_db(path)
    return path


def _add_coin(db_path, code, present=1):
    conn = sqlite3.connect(db_path)
    now = "2026-09-11T00:00:00+00:00"
    conn.execute(
        """INSERT INTO market_coins (code, name, rank, rate, present,
                                     first_seen, last_seen, updated_at)
           VALUES (?,?,1,1.0,?,?,?,?)""", (code, code, present, now, now, now))
    conn.commit()
    conn.close()


def test_tracked_codes_only_includes_present_coins(db):
    _add_coin(db, "BTC")
    _add_coin(db, "SOL")
    _add_coin(db, "GONE", present=0)
    assert market_data.tracked_codes(db) == ["BTC", "SOL"]


def test_empty_universe_gives_a_do_not_trade_notice(db):
    out = market_data.tracked_universe_brief(db)
    assert "empty" in out.lower()
    assert "do not recommend or trade" in out.lower()


def test_brief_lists_every_tracked_code_and_the_hard_rule(db):
    _add_coin(db, "BTC")
    _add_coin(db, "SOL")
    out = market_data.tracked_universe_brief(db)
    assert "BTC" in out and "SOL" in out
    assert "2 coins" in out
    assert "Hard rule" in out


def test_untracked_coins_from_the_incident_are_absent_when_not_polled(db):
    """TAO, AERO, WLD and IOST are real, well-known tokens -- the point is that a model
    must not assume they're tracked just because it recognises them."""
    _add_coin(db, "BTC")
    out = market_data.tracked_universe_brief(db)
    for code in ("TAO", "AERO", "WLD", "IOST"):
        assert code not in out


def test_long_universe_is_truncated_with_a_count(db):
    for i in range(5):
        _add_coin(db, f"C{i}")
    out = market_data.tracked_universe_brief(db, max_list=3)
    assert "+2 more not shown" in out
