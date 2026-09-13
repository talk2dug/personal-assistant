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


class TestPerCoinPerformance:
    """The one question this ledger held every row to answer and could not: "have I lost
    money on this ticker before?" The blended win rate in performance() averages that away
    -- which is how the same codes kept being re-bought days after losing money on them.
    """

    def _move(self, db, code, rate):
        conn = sqlite3.connect(db)
        conn.execute("UPDATE market_coins SET rate = ? WHERE code = ?", (rate, code))
        conn.commit(); conn.close()

    def test_no_closed_trades_means_no_record_rather_than_a_zero(self, db):
        paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 1000}])
        # An open position has no outcome yet. Counting its paper mark here would let an
        # unrealised loss masquerade as a track record.
        assert paper_trading.per_coin_performance(db) == []

    def test_losses_and_wins_are_split_per_coin(self, db):
        paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 1000}])
        self._move(db, "SOL", 100.0)                       # halves
        paper_trading.execute_orders(db, [{"side": "sell", "code": "SOL", "qty": "all"}])
        paper_trading.execute_orders(db, [{"side": "buy", "code": "BTC", "usd": 1000}])
        self._move(db, "BTC", 160_000.0)                   # doubles
        paper_trading.execute_orders(db, [{"side": "sell", "code": "BTC", "qty": "all"}])

        by_code = {c["code"]: c for c in paper_trading.per_coin_performance(db)}
        assert by_code["SOL"]["realized"] < 0
        assert by_code["SOL"]["wins"] == 0 and by_code["SOL"]["losses"] == 1
        assert by_code["BTC"]["realized"] > 0
        assert by_code["BTC"]["wins"] == 1 and by_code["BTC"]["win_rate_pct"] == 100.0

    def test_worst_coin_comes_first(self, db):
        for code, crash in (("SOL", 100.0), ("BTC", 8_000.0)):
            paper_trading.execute_orders(db, [{"side": "buy", "code": code, "usd": 1000}])
            self._move(db, code, crash)
            paper_trading.execute_orders(db, [{"side": "sell", "code": code, "qty": "all"}])
        # BTC lost 90%, SOL 50% -- the biggest loser leads, because that is the one the
        # employee most needs in front of it before it proposes buying again.
        assert [c["code"] for c in paper_trading.per_coin_performance(db)][0] == "BTC"

    def test_repeated_losses_on_one_ticker_accumulate(self, db):
        for _ in range(3):
            paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 500}])
            self._move(db, "SOL", 100.0)
            paper_trading.execute_orders(db, [{"side": "sell", "code": "SOL", "qty": "all"}])
            self._move(db, "SOL", 200.0)
        sol = paper_trading.per_coin_performance(db)[0]
        assert sol["code"] == "SOL"
        assert sol["closed_trades"] == 3 and sol["losses"] == 3
        assert sol["win_rate_pct"] == 0.0
        # Fees count every trade in the code, not just the closing ones: churning a
        # ticker is half the reason it lost money.
        assert sol["fees_paid"] > 0

    def test_last_buy_at_finds_the_open_of_the_position_being_closed(self, db):
        assert paper_trading.last_buy_at(db, "SOL") is None
        paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 500}])
        opened = paper_trading.last_buy_at(db, "SOL")
        assert opened and opened.startswith("20")
        paper_trading.execute_orders(db, [{"side": "sell", "code": "SOL", "qty": "all"}])
        # A sell must not become the "opening" timestamp a closing entry links back to.
        assert paper_trading.last_buy_at(db, "SOL") == opened


class TestFencedBlockScanning:
    """A real bug with teeth, found the moment a second fenced block joined the reply.

    The old pattern matched the label inline as ```(?:orders|json)?\\s*\\n. A fence it did
    not recognise did not merely fail to match -- the scan resynchronised on that block's
    CLOSING fence and swallowed the next block whole. One ```journal block above the
    ```orders block was therefore enough to make parse_orders return nothing, silently, on
    every run: no orders, no rejections, nothing in the log, a trading desk that had
    quietly stopped trading.
    """

    ORDERS = '```orders\n{"orders": [{"side": "buy", "code": "SOL", "usd": 100}]}\n```'
    JOURNAL = '```journal\n{"summary": "loading up", "detail": "conviction"}\n```'

    def test_a_journal_block_above_the_orders_does_not_eat_them(self):
        orders = paper_trading.parse_orders(f"prose\n{self.JOURNAL}\n{self.ORDERS}\n")
        assert orders == [{"side": "buy", "code": "SOL", "usd": 100}]

    def test_a_journal_block_below_the_orders_does_not_eat_them(self):
        orders = paper_trading.parse_orders(f"prose\n{self.ORDERS}\n{self.JOURNAL}\n")
        assert orders == [{"side": "buy", "code": "SOL", "usd": 100}]

    def test_an_unrelated_code_fence_is_skipped_not_resynchronised_on(self):
        text = f"prose\n```python\nprint('hi')\n```\n{self.ORDERS}\n"
        assert paper_trading.parse_orders(text) == [{"side": "buy", "code": "SOL", "usd": 100}]

    def test_a_journal_block_is_never_read_as_orders(self):
        assert paper_trading.parse_orders(self.JOURNAL) == []

    def test_unlabelled_and_json_fences_are_still_accepted(self):
        bare = '```\n{"orders": [{"side": "sell", "code": "SOL", "qty": "all"}]}\n```'
        assert paper_trading.parse_orders(bare)[0]["side"] == "sell"
        assert paper_trading.fenced_blocks(bare, {""})
