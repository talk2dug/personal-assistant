"""The paper ledger's arithmetic and its refusals.

Worth testing precisely because the employee proposing the trades cannot be trusted with
either: a model will happily "sell" a coin it never bought and report the profit.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

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


def _fenced(orders):
    """Orders the way an employee emits them -- a fenced JSON block in prose, which is
    what _apply_paper_orders parses."""
    import json

    return ("Here is the plan.\n\n```json\n"
            + json.dumps(orders) + "\n```\n")


def buy(code, usd, stop=None, target=None, **extra):
    """A well-formed buy. Since exits became mechanical every buy must carry both levels
    and a target at least MIN_REWARD_RISK x the stop distance, so tests that only need a
    position open say so once here rather than restating the plan each time."""
    price = {"BTC": 80_000.0, "SOL": 200.0, "PEPE": 0.000012}[code]
    order = {"side": "buy", "code": code, "usd": usd,
             "stop_loss": stop if stop is not None else price * 0.9,
             "take_profit": target if target is not None else price * 1.3}
    order.update(extra)
    return order


def close(db, code, qty="all"):
    """Force a position closed the way check_stops does. The model itself can no longer
    place a sell, so a test exercising fill arithmetic has to use the exit authority."""
    return paper_trading.execute_orders(
        db, [{"side": "sell", "code": code, "qty": qty}], allow_exit=True)


def test_buy_deducts_cash_and_fee_and_opens_a_position(db):
    r = paper_trading.execute_orders(db, [buy("SOL", 1000)])
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
    paper_trading.execute_orders(db, [buy("SOL", 1000)])
    r = close(db, "SOL")
    assert r["fills"][0]["realized"] == pytest.approx(-2.0, abs=0.01)   # two 0.1% fees
    p = r["portfolio"]
    assert p["positions"] == []
    assert p["equity"] == pytest.approx(9998.0, abs=0.01)


def test_realized_profit_when_price_rises(db):
    paper_trading.execute_orders(db, [buy("SOL", 1000)])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 400.0 WHERE code = 'SOL'")   # doubles
    conn.commit(); conn.close()
    r = close(db, "SOL")
    # 5 SOL * $400 = $2000 gross, less $2 fee, less $1001 basis.
    assert r["fills"][0]["realized"] == pytest.approx(997.0, abs=0.01)
    assert r["portfolio"]["realized_pnl"] == pytest.approx(997.0, abs=0.01)


def test_cannot_spend_cash_it_does_not_have(db):
    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "BTC", "usd": 50_000}])
    assert not r["fills"]
    assert r["portfolio"]["cash"] == 10_000.0
    assert "cap" in r["rejections"][0]["reason"] or "insufficient" in r["rejections"][0]["reason"]


def test_cannot_sell_what_it_does_not_hold(db):
    r = paper_trading.execute_orders(db, [{"side": "sell", "code": "BTC", "qty": 1}],
                                     allow_exit=True)
    assert not r["fills"]
    assert "no BTC position" in r["rejections"][0]["reason"]


def test_cannot_oversell_a_position(db):
    paper_trading.execute_orders(db, [buy("SOL", 1000)])
    r = paper_trading.execute_orders(db, [{"side": "sell", "code": "SOL", "qty": 500}],
                                     allow_exit=True)
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
    r = paper_trading.execute_orders(db, [buy("PEPE", 100)])
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


def test_an_add_on_cannot_widen_the_committed_stop(db):
    """Giving a loser more room is half the disposition effect and stays refused: the stop
    only ever moves toward the price, never away from it."""
    paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=260.0)])
    r = paper_trading.execute_orders(db, [buy("SOL", 500, stop=170.0, target=260.0)])
    assert not r["fills"]
    assert "cannot widen it" in r["rejections"][0]["reason"]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT stop_loss FROM paper_positions WHERE code='SOL'").fetchone()
    conn.close()
    assert row["stop_loss"] == 180.0, "the committed stop must survive the restate attempt"


def test_an_add_on_cannot_pull_the_target_in(db):
    """The other half: banking a winner early by restating a nearer target. Refused before
    any cash moves, so the churn stops too."""
    paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=260.0)])
    cash_before = paper_trading.portfolio(db)["cash"]
    r = paper_trading.execute_orders(db, [buy("SOL", 0.01, stop=180.0, target=205.0)])
    assert not r["fills"]
    assert "cannot" in r["rejections"][0]["reason"]
    assert paper_trading.portfolio(db)["cash"] == pytest.approx(cash_before)


def test_a_genuine_add_on_that_does_not_move_the_levels_still_fills(db):
    """Adding real capital without touching the plan is untouched -- levels may be omitted
    or restated identically, and the position grows on its committed stop and target."""
    paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=260.0)])
    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 300}])
    assert r["fills"] and not r["rejections"]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT stop_loss, take_profit FROM paper_positions WHERE code='SOL'").fetchone()
    conn.close()
    assert row["stop_loss"] == 180.0 and row["take_profit"] == 260.0


class TestTheStopRatchet:
    """The stop trails up behind a winner; the target never moves.

    This asymmetry is the whole strategy, and it was briefly lost. Freezing BOTH levels
    (2026-09-19) to close a "restate the stop" exploit also removed the trail that was
    earning the money: the desk had won 78% of 54 round-trips for +$61.83 over 09-17/18
    with 30 of its 44 stop exits closing ABOVE entry, and went to a 44% win rate and a
    flat book for the two days it was frozen. Raising a stop cuts risk without touching
    the upside -- it is not the disposition effect, and these tests pin that apart.
    """

    def _move(self, db, code, rate):
        conn = sqlite3.connect(db)
        conn.execute("UPDATE market_coins SET rate = ? WHERE code = ?", (rate, code))
        conn.commit(); conn.close()

    def _stop(self, db, code="SOL"):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT stop_loss, initial_stop_loss FROM paper_positions WHERE code=?",
            (code,)).fetchone()
        conn.close()
        return row

    def test_a_stop_can_be_raised_behind_a_winner(self, db):
        paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=260.0)])
        r = paper_trading.execute_orders(
            db, [{"side": "raise_stop", "code": "SOL", "stop_loss": 189.0}])
        assert r["fills"] and not r["rejections"]
        assert self._stop(db)["stop_loss"] == 189.0

    def test_raising_a_stop_costs_no_cash_and_files_no_trade(self, db):
        """It is a risk decision, not a transaction. Expressed as a token add-on it paid a
        fee and left a phantom buy in the ledger; its own side does neither."""
        paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=260.0)])
        cash_before = paper_trading.portfolio(db)["cash"]
        conn = sqlite3.connect(db)
        trades_before = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        conn.close()
        paper_trading.execute_orders(
            db, [{"side": "raise_stop", "code": "SOL", "stop_loss": 189.0}])
        conn = sqlite3.connect(db)
        trades_after = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        raises = conn.execute("SELECT from_stop, to_stop FROM paper_stop_raises").fetchall()
        conn.close()
        assert paper_trading.portfolio(db)["cash"] == pytest.approx(cash_before)
        assert trades_after == trades_before
        assert raises == [(180.0, 189.0)], "the raise is audited even though it is not a trade"

    def test_a_stop_can_never_be_lowered(self, db):
        paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=260.0)])
        paper_trading.execute_orders(
            db, [{"side": "raise_stop", "code": "SOL", "stop_loss": 189.0}])
        r = paper_trading.execute_orders(
            db, [{"side": "raise_stop", "code": "SOL", "stop_loss": 185.0}])
        assert not r["fills"] and "only ever moves up" in r["rejections"][0]["reason"]
        assert self._stop(db)["stop_loss"] == 189.0

    def test_a_stop_pinned_under_spot_is_refused_as_a_disguised_sell(self, db):
        """SOL marks at 200 with an original risk of 20, so the trail must stay at least
        10 back: 195 is selling at market with extra steps."""
        paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=260.0)])
        r = paper_trading.execute_orders(
            db, [{"side": "raise_stop", "code": "SOL", "stop_loss": 195.0}])
        assert not r["fills"]
        assert "too close" in r["rejections"][0]["reason"]
        assert self._stop(db)["stop_loss"] == 180.0

    def test_the_ceiling_does_not_creep_up_as_the_stop_is_tightened(self, db):
        """Measured off the ORIGINAL risk, not the current stop. Otherwise each raise
        narrows the gap it is checked against and the trail walks itself to spot."""
        paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=260.0)])
        paper_trading.execute_orders(
            db, [{"side": "raise_stop", "code": "SOL", "stop_loss": 189.0}])
        assert self._stop(db)["initial_stop_loss"] == 180.0
        r = paper_trading.execute_orders(
            db, [{"side": "raise_stop", "code": "SOL", "stop_loss": 194.0}])
        assert not r["fills"], "the ceiling is still 189.9, measured from the entry risk"

    def test_an_add_on_may_state_the_already_raised_stop(self, db):
        """Adding real capital after a trail should not have to pretend the stop never
        moved -- but it still cannot move the target."""
        paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=260.0)])
        paper_trading.execute_orders(
            db, [{"side": "raise_stop", "code": "SOL", "stop_loss": 189.0}])
        r = paper_trading.execute_orders(db, [buy("SOL", 300, stop=189.0, target=260.0)])
        assert r["fills"] and not r["rejections"]
        assert self._stop(db)["initial_stop_loss"] == 180.0, "an add-on does not reset the risk"

    def test_a_raise_on_a_coin_not_held_is_refused(self, db):
        r = paper_trading.execute_orders(
            db, [{"side": "raise_stop", "code": "SOL", "stop_loss": 189.0}])
        assert not r["fills"] and "no SOL position" in r["rejections"][0]["reason"]

    def test_a_trailed_position_exits_on_the_raised_stop_above_its_entry(self, db):
        """The point of the whole mechanism, end to end: SOL is bought at 200, runs to 240,
        the stop trails up to 220, and the pullback closes the trade at a PROFIT rather
        than back at the 180 it was opened with. That is how 30 of those 44 stop exits
        finished green, and it is exactly what the freeze made impossible."""
        paper_trading.execute_orders(db, [buy("SOL", 1000, stop=180.0, target=260.0)])
        self._move(db, "SOL", 240.0)
        r = paper_trading.execute_orders(
            db, [{"side": "raise_stop", "code": "SOL", "stop_loss": 220.0}])
        assert r["fills"], r["rejections"]
        self._move(db, "SOL", 215.0)
        paper_trading.check_stops(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT exit_kind, realized FROM paper_trades "
                           "WHERE side='sell' ORDER BY id DESC LIMIT 1").fetchone()
        open_rows = conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE code='SOL'").fetchone()[0]
        conn.close()
        assert open_rows == 0
        assert row["exit_kind"] == "stop_loss"
        assert row["realized"] > 0, "a stop exit ABOVE entry is the trail paying for itself"


class TestReportingARaisedStop:
    """The ratchet is not a trade, and everything that reads the fill list must know it.

    This is the bug that cost the desk two days. A raise_stop fill carries no qty, usd or
    fee -- deliberately, so it pays no fee and leaves no phantom buy -- and the execution
    report formatted every fill as a trade. From the first raise on 2026-09-20 to the fix,
    83 of the day trader's runs died with KeyError: 'qty', every one of them in the same
    minute as a stop raise. The ratchet itself worked the whole time: the raise commits
    before the report is built, so the money-making behaviour survived and only the run
    reporting it was lost. That is precisely why it stayed invisible.
    """

    def _raised(self, db):
        paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=260.0)])
        return paper_trading.execute_orders(
            db, [{"side": "raise_stop", "code": "SOL", "stop_loss": 189.0,
                  "reason": "higher low holding"}])

    def test_the_execution_report_survives_a_raised_stop(self, db):
        from assistant.core import staff

        paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=260.0)])
        note, result = staff._apply_paper_orders(
            db, _fenced([{"side": "raise_stop", "code": "SOL", "stop_loss": 189.0,
                          "reason": "higher low holding"}]),
            staff_key="crypto_day_trader_paper_trading")
        assert result is not None and result["fills"], note
        assert "STOP RAISED" in note
        assert "EXECUTION FAILED" not in note

    def test_the_report_says_where_the_stop_moved_to(self, db):
        """A raise the employee cannot see in the report is one it proposes again next
        run, and then reads the 'only ever moves up' rejection as the desk refusing it."""
        from assistant.core import staff

        self._raised(db)
        note, _ = staff._apply_paper_orders(
            db, _fenced([{"side": "raise_stop", "code": "SOL", "stop_loss": 194.0}]),
            staff_key="crypto_day_trader_paper_trading")
        assert "189" in note and "194" in note

    def test_the_journal_does_not_file_it_as_a_zero_quantity_trade(self, db):
        from assistant.core import crypto_journal

        fills = self._raised(db)["fills"]
        lines = crypto_journal._fill_lines(fills, "Crypto Day Trader", db)
        body = "\n".join(lines)
        assert "STOP RAISED" in body
        assert "RAISE_STOP 0" not in body, "printed as a trade it reads as a trade"


def test_check_stops_closes_a_position_that_breached_its_stop_loss(db):
    paper_trading.execute_orders(db, [buy("SOL", 1000, stop=180.0, target=260.0)])
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
    paper_trading.execute_orders(db, [buy("SOL", 1000, stop=190.0, target=220.0)])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 230.0 WHERE code = 'SOL'")
    conn.commit(); conn.close()

    r = paper_trading.check_stops(db)

    assert len(r["fills"]) == 1
    assert paper_trading.recent_trades(db, limit=1)[0]["exit_kind"] == "take_profit"


def test_check_stops_leaves_a_position_alone_when_nothing_is_breached(db):
    paper_trading.execute_orders(db, [buy("SOL", 1000, stop=180.0, target=240.0)])
    r = paper_trading.check_stops(db)
    assert r["fills"] == []
    assert len(r["portfolio"]["positions"]) == 1


def test_a_forced_exit_with_no_levels_is_still_classified_discretionary(db):
    """Only reachable through allow_exit now -- the model cannot get here."""
    paper_trading.execute_orders(db, [buy("SOL", 1000)])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE paper_positions SET stop_loss = NULL, take_profit = NULL")
    conn.commit(); conn.close()
    close(db, "SOL")
    assert paper_trading.recent_trades(db, limit=1)[0]["exit_kind"] == "discretionary"


def test_a_coin_stopped_out_cannot_be_rebought_during_the_cooldown(db):
    paper_trading.execute_orders(db, [buy("SOL", 1000, stop=180.0, target=260.0)])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 170.0 WHERE code = 'SOL'")
    conn.commit(); conn.close()
    paper_trading.check_stops(db)  # stops out, exit_kind='stop_loss'

    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 100}])

    assert not r["fills"]
    assert "cooldown" in r["rejections"][0]["reason"]


def test_a_coin_closed_at_take_profit_can_be_rebought_immediately(db):
    paper_trading.execute_orders(db, [buy("SOL", 1000, stop=190.0, target=220.0)])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 230.0 WHERE code = 'SOL'")
    conn.commit(); conn.close()
    paper_trading.check_stops(db)  # closes at take-profit, not a stop-loss

    r = paper_trading.execute_orders(
        db, [buy("SOL", 100, stop=220.0, target=260.0)])   # a plan against the new price

    assert r["fills"]


def test_cooldown_expires_after_the_configured_window(db, monkeypatch):
    paper_trading.execute_orders(db, [buy("SOL", 1000, stop=180.0, target=260.0)])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 170.0 WHERE code = 'SOL'")
    conn.commit(); conn.close()
    paper_trading.check_stops(db)

    monkeypatch.setattr(paper_trading, "STOP_LOSS_COOLDOWN_HOURS", 0.0)
    r = paper_trading.execute_orders(
        db, [buy("SOL", 100, stop=160.0, target=200.0)])   # priced off the new $170

    assert r["fills"]


def test_order_instructions_template_survives_formatting():
    """It embeds JSON, which is exactly what broke the verdict template."""
    out = paper_trading.render_order_instructions()
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
        paper_trading.execute_orders(db, [buy("SOL", 1000)])
        # An open position has no outcome yet. Counting its paper mark here would let an
        # unrealised loss masquerade as a track record.
        assert paper_trading.per_coin_performance(db) == []

    def test_losses_and_wins_are_split_per_coin(self, db):
        paper_trading.execute_orders(db, [buy("SOL", 1000)])
        self._move(db, "SOL", 100.0)                       # halves
        close(db, "SOL")
        paper_trading.execute_orders(db, [buy("BTC", 1000)])
        self._move(db, "BTC", 160_000.0)                   # doubles
        close(db, "BTC")

        by_code = {c["code"]: c for c in paper_trading.per_coin_performance(db)}
        assert by_code["SOL"]["realized"] < 0
        assert by_code["SOL"]["wins"] == 0 and by_code["SOL"]["losses"] == 1
        assert by_code["BTC"]["realized"] > 0
        assert by_code["BTC"]["wins"] == 1 and by_code["BTC"]["win_rate_pct"] == 100.0

    def test_worst_coin_comes_first(self, db):
        for code, crash in (("SOL", 100.0), ("BTC", 8_000.0)):
            paper_trading.execute_orders(db, [buy(code, 1000)])
            self._move(db, code, crash)
            close(db, code)
        # BTC lost 90%, SOL 50% -- the biggest loser leads, because that is the one the
        # employee most needs in front of it before it proposes buying again.
        assert [c["code"] for c in paper_trading.per_coin_performance(db)][0] == "BTC"

    def test_repeated_losses_on_one_ticker_accumulate(self, db):
        for _ in range(3):
            paper_trading.execute_orders(db, [buy("SOL", 500)])
            self._move(db, "SOL", 100.0)
            conn = sqlite3.connect(db)
            conn.execute("UPDATE paper_positions SET stop_loss = NULL, take_profit = NULL")
            conn.commit(); conn.close()
            close(db, "SOL")          # discretionary, so no stop-loss cooldown applies
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
        paper_trading.execute_orders(db, [buy("SOL", 500)])
        opened = paper_trading.last_buy_at(db, "SOL")
        assert opened and opened.startswith("20")
        close(db, "SOL")
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


# --- incoherent exit levels ---------------------------------------------------
#
# A long position's plan only makes sense as stop_loss < fill price < take_profit.
# _parse_level checked only "is this a positive number", so a stop ABOVE the entry was
# accepted and check_stops() then closed the position on its next tick at a price that
# had not moved. This is not hypothetical: on 2026-09-14 TAO was bought at $235.13 with
# a stop of $250 and was stopped out in the same minute for the cost of two fees.

def test_a_stop_above_the_entry_is_refused_rather_than_instantly_stopping_out(db):
    r = paper_trading.execute_orders(db, [
        {"side": "buy", "code": "SOL", "usd": 1000, "stop_loss": 250.0},   # entry is $200
    ])
    assert not r["fills"]
    assert "at or above" in r["rejections"][0]["reason"]
    # And crucially the money never left: an order refused after the cash was debited
    # would silently burn the balance.
    assert r["portfolio"]["cash"] == pytest.approx(10_000.0)
    assert r["portfolio"]["positions"] == []


def test_a_target_below_the_entry_is_refused_too(db):
    r = paper_trading.execute_orders(db, [
        {"side": "buy", "code": "SOL", "usd": 1000, "take_profit": 150.0},  # entry is $200
    ])
    assert not r["fills"]
    assert "at or below" in r["rejections"][0]["reason"]
    assert r["portfolio"]["cash"] == pytest.approx(10_000.0)


def test_a_coherent_plan_still_fills_and_stores_both_levels(db):
    r = paper_trading.execute_orders(db, [
        {"side": "buy", "code": "SOL", "usd": 1000, "stop_loss": 180.0, "take_profit": 240.0},
    ])
    assert not r["rejections"]
    pos = r["portfolio"]["positions"][0]
    assert pos["stop_loss"] == 180.0
    assert pos["take_profit"] == 240.0


def test_the_refusal_is_measured_against_the_fill_price_not_the_previous_stop(db):
    """Adding to a position inherits its stored levels. If the price has since run past
    the old stop, the inherited level is now incoherent and the top-up must be refused
    rather than opening a bigger position that closes on the next tick."""
    paper_trading.execute_orders(db, [buy("SOL", 1000, stop=180.0, target=260.0)])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = 170.0 WHERE code = 'SOL'")   # fell below the stop
    conn.commit(); conn.close()

    r = paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 500}])
    assert not r["fills"]
    assert "at or above" in r["rejections"][0]["reason"]


# --- mechanical exits ---------------------------------------------------------
#
# Measured over the 92 closed round-trips archived on 2026-09-13: 90 of those exits were
# the model closing the position itself, and it held winners a median of 2.0h against
# 8.7h for losers -- a 0.84 payoff ratio where 1.36 was needed to break even. The exit
# decision is no longer the model's; it commits a plan at entry and the position leaves
# on one of those levels or on the clock.

class TestMechanicalExits:
    def test_the_model_cannot_close_a_position_it_opened(self, db):
        paper_trading.execute_orders(db, [buy("SOL", 1000)])
        r = paper_trading.execute_orders(db, [{"side": "sell", "code": "SOL", "qty": "all"}])
        assert not r["fills"]
        assert "exits are mechanical" in r["rejections"][0]["reason"]
        assert len(r["portfolio"]["positions"]) == 1, "the position must survive the refusal"

    def test_the_refusal_is_persisted_so_the_employee_reads_it_next_run(self, db):
        paper_trading.execute_orders(db, [buy("SOL", 1000)])
        paper_trading.execute_orders(db, [{"side": "sell", "code": "SOL", "qty": "all"}],
                                     staff_key="day_trader")
        assert "exits are mechanical" in paper_trading.recent_rejections(db)[0]["reason"]

    def test_check_stops_still_has_the_authority_to_close(self, db):
        paper_trading.execute_orders(db, [buy("SOL", 1000, stop=180.0, target=260.0)])
        conn = sqlite3.connect(db)
        conn.execute("UPDATE market_coins SET rate = 170.0 WHERE code = 'SOL'")
        conn.commit(); conn.close()
        r = paper_trading.check_stops(db)
        assert len(r["fills"]) == 1
        assert r["portfolio"]["positions"] == []

    def test_a_buy_missing_either_level_is_refused(self, db):
        only_stop = paper_trading.execute_orders(
            db, [{"side": "buy", "code": "SOL", "usd": 500, "stop_loss": 180.0}])
        assert "take_profit is missing" in only_stop["rejections"][0]["reason"]
        only_target = paper_trading.execute_orders(
            db, [{"side": "buy", "code": "SOL", "usd": 500, "take_profit": 260.0}])
        assert "stop_loss is missing" in only_target["rejections"][0]["reason"]
        assert paper_trading.portfolio(db)["cash"] == pytest.approx(10_000.0)

    def test_a_target_worth_less_than_twice_the_risk_is_refused(self, db):
        # $200 entry, $20 of risk, only $20 of reward.
        r = paper_trading.execute_orders(
            db, [buy("SOL", 500, stop=180.0, target=220.0)])
        assert not r["fills"]
        assert "1.00x the risk" in r["rejections"][0]["reason"]

    def test_exactly_two_to_one_is_accepted(self, db):
        r = paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=240.0)])
        assert not r["rejections"] and r["fills"]

    def test_a_position_past_the_maximum_hold_is_closed_on_the_clock(self, db):
        paper_trading.execute_orders(db, [buy("SOL", 1000, stop=180.0, target=260.0)])
        conn = sqlite3.connect(db)
        old = (datetime.now(timezone.utc) - timedelta(hours=paper_trading.MAX_HOLD_HOURS + 1)).isoformat()
        conn.execute("UPDATE paper_positions SET opened_at = ?", (old,))
        conn.commit(); conn.close()

        r = paper_trading.check_stops(db)
        assert len(r["fills"]) == 1
        trade = paper_trading.recent_trades(db, limit=1)[0]
        assert trade["exit_kind"] == "timeout"
        assert "time exit" in trade["reason"]

    def test_a_timeout_does_not_trigger_the_stop_loss_cooldown(self, db):
        """A position that merely ran out of clock never proved its thesis wrong, so it
        is not the failed-thesis case the re-entry cooldown exists for."""
        paper_trading.execute_orders(db, [buy("SOL", 1000, stop=180.0, target=260.0)])
        conn = sqlite3.connect(db)
        old = (datetime.now(timezone.utc) - timedelta(hours=paper_trading.MAX_HOLD_HOURS + 1)).isoformat()
        conn.execute("UPDATE paper_positions SET opened_at = ?", (old,))
        conn.commit(); conn.close()
        paper_trading.check_stops(db)

        r = paper_trading.execute_orders(db, [buy("SOL", 100, stop=180.0, target=260.0)])
        assert r["fills"], "a timed-out coin should be re-buyable"

    def test_topping_up_a_position_does_not_restart_its_clock(self, db):
        """Otherwise a position could be kept past the maximum hold forever by adding
        a dollar to it."""
        paper_trading.execute_orders(db, [buy("SOL", 500, stop=180.0, target=260.0)])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        old = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
        conn.execute("UPDATE paper_positions SET opened_at = ?", (old,))
        conn.commit()

        paper_trading.execute_orders(db, [buy("SOL", 100, stop=180.0, target=260.0)])
        row = conn.execute("SELECT opened_at FROM paper_positions WHERE code='SOL'").fetchone()
        conn.close()
        assert row["opened_at"] == old

    def test_a_position_inside_the_hold_window_is_left_alone(self, db):
        paper_trading.execute_orders(db, [buy("SOL", 1000, stop=180.0, target=260.0)])
        r = paper_trading.check_stops(db)
        assert r["fills"] == []
        assert len(r["portfolio"]["positions"]) == 1

    def test_the_new_rules_reach_the_employees_prompt(self, db):
        out = paper_trading.render_order_instructions()
        assert "YOU ONLY DECIDE ENTRIES" in out
        assert "cannot close a position" in out
        # The model is told the numbers behind the rule, not just the rule.
        assert "2.0 hours and losers 8.7 hours" in out


class TestRunningABook:
    """The desk held one position at a time and nothing said it should not.

    Nothing in the ledger required that -- the cause was upstream, in a screen that only
    ever showed it two or three charts (see test_technicals) -- but with the screen widened
    there had to be something bounding concentration too, or a wider funnel just means a
    bigger single bet. These are the two caps and the slot count that replace it.
    """

    def test_a_single_order_is_capped_at_a_slot_not_the_account(self, db):
        # $10,000 equity, 15% cap: $1,500 is the most one order may be.
        r = paper_trading.execute_orders(db, [buy("SOL", 2_000)])
        assert not r["fills"]
        assert "per-order cap" in r["rejections"][0]["reason"]

        r = paper_trading.execute_orders(db, [buy("SOL", 1_400)])
        assert r["fills"]

    def test_add_ons_cannot_walk_one_coin_past_the_per_coin_cap(self, db):
        """The per-order cap alone never bounded a POSITION: three compliant add-ons to
        one ticker clear it one at a time and still end up as the whole book."""
        assert paper_trading.execute_orders(db, [buy("SOL", 1_400)])["fills"]
        # $1,400 held + $1,400 more = $2,800, past the 20% ($2,000) per-coin cap.
        r = paper_trading.execute_orders(db, [buy("SOL", 1_400)])
        assert not r["fills"]
        reason = r["rejections"][0]["reason"]
        assert "per-coin cap" in reason
        assert "different name" in reason

    def test_a_second_coin_is_not_blocked_by_the_first(self, db):
        """The per-coin cap must bound concentration, never total deployment -- the whole
        point is that refused capital goes into another name."""
        assert paper_trading.execute_orders(db, [buy("SOL", 1_400)])["fills"]
        assert paper_trading.execute_orders(db, [buy("BTC", 1_400)])["fills"]
        assert len(paper_trading.portfolio(db)["positions"]) == 2

    def test_several_buys_in_one_block_all_fill(self, db):
        """Filling four slots in one run is the ordinary case now, not an edge case."""
        r = paper_trading.execute_orders(
            db, [buy("SOL", 900), buy("BTC", 900), buy("PEPE", 900)])
        assert len(r["fills"]) == 3
        assert len(r["rejections"]) == 0

    def test_slots_are_counted_in_names_not_dollars(self, db):
        empty = paper_trading.book_slots(db)
        assert empty["open"] == 0
        assert empty["free"] == paper_trading.TARGET_CONCURRENT_POSITIONS

        paper_trading.execute_orders(db, [buy("SOL", 900), buy("BTC", 900)])
        held = paper_trading.book_slots(db)
        assert held["open"] == 2
        assert held["free"] == paper_trading.TARGET_CONCURRENT_POSITIONS - 2
        # A slot is sized off equity, and deployable is bounded by actual cash -- a desk
        # told it has four free slots and no cash has been told something useless.
        assert held["slot_size"] > 0
        assert held["deployable"] <= held["cash"] + 1e-9

    def test_the_book_framing_reaches_the_prompt(self, db):
        out = paper_trading.render_order_instructions()
        assert "YOU RUN A BOOK" in out
        assert str(paper_trading.TARGET_CONCURRENT_POSITIONS) in out
        # The bar itself must survive the push for volume -- this is the line that says so.
        assert "never from lowering it" in out
        # And the old doubled "an empty list is often correct" thumb on the scale is gone.
        assert "often correct" not in out
