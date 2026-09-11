"""The paper ledger's arithmetic and its refusals.

Worth testing precisely because the employee proposing the trades cannot be trusted with
either: a model will happily "sell" a coin it never bought and report the profit.
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
    now = "2026-09-05T12:00:00+00:00"
    for code, rate in (("BTC", 80_000.0), ("SOL", 200.0), ("PEPE", 0.000012)):
        conn.execute(
            """INSERT INTO market_coins (code, name, rank, rate, present,
                                         first_seen, last_seen, updated_at)
               VALUES (?,?,1,?,1,?,?,?)""", (code, code, rate, now, now, now))
    conn.commit()
    conn.close()
    paper_trading.ensure_account(path, starting_cash=10_000.0)
    return path


def test_buy_deducts_cash_and_fee_and_opens_a_position(db):
    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 1000}])
    assert not r["rejections"]
    fill = r["fills"][0]
    assert fill["qty"] == pytest.approx(5.0)          # $1000 / $200
    assert fill["fee"] == pytest.approx(1.0)          # 0.10% of 1000
    p = r["portfolio"]
    assert p["cash"] == pytest.approx(8999.0)         # 10000 - 1000 - 1 fee
    assert p["positions"][0]["code"] == "SOL"
    # Fee is in the basis, so the position starts marginally underwater. That is correct:
    # a round trip at an unchanged price loses money.
    assert p["positions"][0]["unrealized"] < 0


def test_round_trip_at_unchanged_price_loses_both_fees(db):
    paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 1000}])
    r = paper_trading.execute_orders(db, [{"side": "sell", "code": "SOL", "qty": "all"}])
    assert r["fills"][0]["realized"] == pytest.approx(-2.0, abs=0.01)   # two 0.1% fees
    p = r["portfolio"]
    assert p["positions"] == []
    assert p["equity"] == pytest.approx(9998.0, abs=0.01)


def test_realized_profit_when_price_rises(db):
    paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 1000}])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 400.0 WHERE code = 'SOL'")   # doubles
    conn.commit(); conn.close()
    r = paper_trading.execute_orders(db, [{"side": "sell", "code": "SOL", "qty": "all"}])
    # 5 SOL * $400 = $2000 gross, less $2 fee, less $1001 basis.
    assert r["fills"][0]["realized"] == pytest.approx(997.0, abs=0.01)
    assert r["portfolio"]["realized_pnl"] == pytest.approx(997.0, abs=0.01)


def test_cannot_spend_cash_it_does_not_have(db):
    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "BTC", "usd": 50_000}])
    assert not r["fills"]
    assert r["portfolio"]["cash"] == 10_000.0
    assert "cap" in r["rejections"][0]["reason"] or "insufficient" in r["rejections"][0]["reason"]


def test_cannot_sell_what_it_does_not_hold(db):
    r = paper_trading.execute_orders(db, [{"side": "sell", "code": "BTC", "qty": 1}])
    assert not r["fills"]
    assert "no BTC position" in r["rejections"][0]["reason"]


def test_cannot_oversell_a_position(db):
    paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 1000}])
    r = paper_trading.execute_orders(db, [{"side": "sell", "code": "SOL", "qty": 500}])
    assert not r["fills"]
    assert "cannot sell" in r["rejections"][0]["reason"]


def test_per_order_cap_blocks_an_all_in_bet(db):
    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "BTC", "usd": 9000}])
    assert not r["fills"]
    assert "cap" in r["rejections"][0]["reason"]


def test_unknown_coin_is_rejected_rather_than_priced_at_zero(db):
    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "NOTREAL", "usd": 100}])
    assert not r["fills"]
    assert "not in the tracked price cache" in r["rejections"][0]["reason"]


def test_rejections_are_persisted_so_the_employee_learns_of_them(db):
    paper_trading.execute_orders(db, [{"side": "sell", "code": "BTC", "qty": 1}],
                                 staff_key="day_trader")
    rej = paper_trading.recent_rejections(db)
    assert len(rej) == 1 and rej[0]["staff_key"] == "day_trader"


def test_sub_cent_coins_keep_their_precision(db):
    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "PEPE", "usd": 100}])
    qty = r["fills"][0]["qty"]
    assert qty == pytest.approx(100 / 0.000012)
    assert r["portfolio"]["positions"][0]["qty"] > 8_000_000


class TestParseOrders:
    def test_extracts_a_fenced_orders_block(self):
        text = 'Reasoning here.\n\n```orders\n{"orders": [{"side":"buy","code":"SOL","usd":100}]}\n```'
        assert paper_trading.parse_orders(text) == [{"side": "buy", "code": "SOL", "usd": 100}]

    def test_prose_alone_yields_no_trades(self):
        assert paper_trading.parse_orders("I would buy SOL here, it looks strong.") == []

    def test_malformed_json_yields_no_trades(self):
        # Never guess: a half-parsed order is an invented trade.
        assert paper_trading.parse_orders('```orders\n{"orders": [{"side":\n```') == []

    def test_empty_list_is_a_valid_decision_to_sit_out(self):
        assert paper_trading.parse_orders('```orders\n{"orders": []}\n```') == []

    def test_last_block_wins_when_a_model_restates(self):
        text = ('```orders\n{"orders":[{"side":"buy","code":"BTC","usd":1}]}\n```\n'
                'On reflection:\n```orders\n{"orders":[{"side":"buy","code":"SOL","usd":2}]}\n```')
        assert paper_trading.parse_orders(text) == [{"side": "buy", "code": "SOL", "usd": 2}]


def test_stop_loss_and_take_profit_persist_on_the_position(db):
    r = paper_trading.execute_orders(
        db, [{"side": "buy", "code": "SOL", "usd": 1000, "stop_loss": 180.0, "take_profit": 240.0}])
    assert not r["rejections"]
    pos = r["portfolio"]["positions"][0]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT stop_loss, take_profit FROM paper_positions WHERE code='SOL'").fetchone()
    conn.close()
    assert row["stop_loss"] == 180.0
    assert row["take_profit"] == 240.0


def test_adding_to_a_position_keeps_the_existing_levels_if_none_restated(db):
    paper_trading.execute_orders(
        db, [{"side": "buy", "code": "SOL", "usd": 500, "stop_loss": 180.0, "take_profit": 240.0}])
    paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 500}])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT stop_loss, take_profit FROM paper_positions WHERE code='SOL'").fetchone()
    conn.close()
    assert row["stop_loss"] == 180.0
    assert row["take_profit"] == 240.0


def test_adding_to_a_position_moves_the_level_when_explicitly_restated(db):
    paper_trading.execute_orders(
        db, [{"side": "buy", "code": "SOL", "usd": 500, "stop_loss": 180.0}])
    paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 500, "stop_loss": 190.0}])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT stop_loss FROM paper_positions WHERE code='SOL'").fetchone()
    conn.close()
    assert row["stop_loss"] == 190.0


def test_check_stops_closes_a_position_that_breached_its_stop_loss(db):
    paper_trading.execute_orders(
        db, [{"side": "buy", "code": "SOL", "usd": 1000, "stop_loss": 180.0}])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 170.0 WHERE code = 'SOL'")
    conn.commit(); conn.close()

    r = paper_trading.check_stops(db)

    assert len(r["fills"]) == 1
    assert r["fills"][0]["code"] == "SOL"
    assert r["portfolio"]["positions"] == []
    trades = paper_trading.recent_trades(db, limit=1)
    assert trades[0]["exit_kind"] == "stop_loss"
    assert trades[0]["staff_key"] == "system:check_stops"


def test_check_stops_closes_a_position_that_hit_its_take_profit(db):
    paper_trading.execute_orders(
        db, [{"side": "buy", "code": "SOL", "usd": 1000, "take_profit": 220.0}])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 230.0 WHERE code = 'SOL'")
    conn.commit(); conn.close()

    r = paper_trading.check_stops(db)

    assert len(r["fills"]) == 1
    assert paper_trading.recent_trades(db, limit=1)[0]["exit_kind"] == "take_profit"


def test_check_stops_leaves_a_position_alone_when_nothing_is_breached(db):
    paper_trading.execute_orders(
        db, [{"side": "buy", "code": "SOL", "usd": 1000, "stop_loss": 180.0, "take_profit": 240.0}])
    r = paper_trading.check_stops(db)
    assert r["fills"] == []
    assert len(r["portfolio"]["positions"]) == 1


def test_check_stops_ignores_positions_with_no_levels_set(db):
    paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 1000}])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 1.0 WHERE code = 'SOL'")  # would "breach" anything
    conn.commit(); conn.close()
    r = paper_trading.check_stops(db)
    assert r["fills"] == []


def test_a_discretionary_sell_with_no_levels_is_classified_discretionary(db):
    paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 1000}])
    paper_trading.execute_orders(db, [{"side": "sell", "code": "SOL", "qty": "all"}])
    assert paper_trading.recent_trades(db, limit=1)[0]["exit_kind"] == "discretionary"


def test_a_coin_stopped_out_cannot_be_rebought_during_the_cooldown(db):
    paper_trading.execute_orders(
        db, [{"side": "buy", "code": "SOL", "usd": 1000, "stop_loss": 180.0}])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 170.0 WHERE code = 'SOL'")
    conn.commit(); conn.close()
    paper_trading.check_stops(db)  # stops out, exit_kind='stop_loss'

    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 100}])

    assert not r["fills"]
    assert "cooldown" in r["rejections"][0]["reason"]


def test_a_coin_closed_at_take_profit_can_be_rebought_immediately(db):
    paper_trading.execute_orders(
        db, [{"side": "buy", "code": "SOL", "usd": 1000, "take_profit": 220.0}])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 230.0 WHERE code = 'SOL'")
    conn.commit(); conn.close()
    paper_trading.check_stops(db)  # closes at take-profit, not a stop-loss

    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 100}])

    assert r["fills"]


def test_cooldown_expires_after_the_configured_window(db, monkeypatch):
    paper_trading.execute_orders(
        db, [{"side": "buy", "code": "SOL", "usd": 1000, "stop_loss": 180.0}])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 170.0 WHERE code = 'SOL'")
    conn.commit(); conn.close()
    paper_trading.check_stops(db)

    monkeypatch.setattr(paper_trading, "STOP_LOSS_COOLDOWN_HOURS", 0.0)
    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 100}])

    assert r["fills"]


def test_order_instructions_template_survives_formatting():
    """It embeds JSON, which is exactly what broke the verdict template."""
    out = paper_trading.ORDER_INSTRUCTIONS.format(
        fee_pct=paper_trading.DEFAULT_FEE_PCT, max_pct=paper_trading.MAX_ORDER_PCT_OF_EQUITY,
        cooldown_hours=paper_trading.STOP_LOSS_COOLDOWN_HOURS)
    assert '"side": "buy"' in out and "0.1" in out
