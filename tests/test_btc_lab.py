"""btc_lab: the one-coin, chart-only paper experiment."""
import sqlite3

import pytest

from assistant.core import btc_lab, chart_history, market_data, staff


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "j.db")
    market_data.init_market_db(path)
    conn = sqlite3.connect(path)
    conn.execute("""INSERT INTO market_coins (code, name, rank, rate, present, first_seen, last_seen, updated_at)
                    VALUES ('BTC','Bitcoin',1,80000,1,'x','x','x')""")
    conn.commit(); conn.close()
    staff.init_staff_db(path)
    btc_lab.open_account(path, 1000.0)
    return path


def set_price(db, p):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE market_coins SET rate = ? WHERE code = 'BTC'", (p,))
    conn.commit(); conn.close()


def test_buy_then_sell_at_a_profit_books_realized_after_both_fees(db):
    r = btc_lab.execute_orders(db, [{"side": "buy", "usd": 500, "reason": "test"}])
    assert r["fills"][0]["qty"] == pytest.approx(500 / 80000)
    assert r["account"]["cash"] == pytest.approx(1000 - 500 - 0.5)
    set_price(db, 88000)
    r = btc_lab.execute_orders(db, [{"side": "sell", "btc": "all"}])
    f = r["fills"][0]
    assert f["realized"] == pytest.approx(550 - 0.55 - 500.5, abs=0.01)
    assert r["account"]["btc"] == 0 and r["account"]["cost_basis"] == 0
    perf = btc_lab.performance(db)
    assert perf["closed_trades"] == 1 and perf["wins"] == 1 and perf["fees_paid"] == pytest.approx(1.05)


def test_partial_sells_split_the_cost_basis(db):
    btc_lab.execute_orders(db, [{"side": "buy", "usd": 800}])
    held = btc_lab.account(db)["btc"]
    btc_lab.execute_orders(db, [{"side": "sell", "btc": held / 2}])
    a = btc_lab.account(db)
    assert a["btc"] == pytest.approx(held / 2)
    assert a["cost_basis"] == pytest.approx(800.8 / 2)


def test_buy_all_spends_cash_including_the_fee(db):
    r = btc_lab.execute_orders(db, [{"side": "buy", "usd": "all"}])
    assert r["fills"] and r["account"]["cash"] == pytest.approx(0, abs=1e-6)


@pytest.mark.parametrize("order, why", [
    ({"side": "buy", "usd": 5000}, "not enough cash"),
    ({"side": "buy"}, "positive"),
    ({"side": "sell", "btc": "all"}, "nothing to sell"),
    ({"side": "short", "usd": 100}, "buy, sell or levels"),
])
def test_refusals_carry_reasons_and_are_kept(db, order, why):
    r = btc_lab.execute_orders(db, [order])
    assert not r["fills"] and why in r["rejections"][0]["reason"]
    assert "Recently refused" in btc_lab.briefing(db)


def test_optional_levels_close_the_position_between_runs(db):
    btc_lab.execute_orders(db, [{"side": "buy", "usd": 500},
                                {"side": "levels", "stop_loss": 78000, "take_profit": 85000}])
    set_price(db, 79000)
    assert btc_lab.check_levels(db) is None
    set_price(db, 77900)
    fill = btc_lab.check_levels(db)
    assert fill["trigger"] == "stop_loss" and btc_lab.account(db)["btc"] == 0
    a = btc_lab.account(db)
    assert a["stop_loss"] is None and a["take_profit"] is None   # cleared with the position


def test_a_stop_above_the_price_is_refused(db):
    btc_lab.execute_orders(db, [{"side": "buy", "usd": 100}])
    r = btc_lab.execute_orders(db, [{"side": "levels", "stop_loss": 90000}])
    assert "at or above the price" in r["rejections"][0]["reason"]


def test_the_account_cannot_be_reopened_by_accident(db):
    with pytest.raises(ValueError):
        btc_lab.open_account(db)


def test_benchmark_against_holding(db):
    set_price(db, 88000)
    a = btc_lab.account(db)
    assert a["hold_equity"] == pytest.approx(1100) and a["vs_hold"] == pytest.approx(-100)


def test_briefing_has_only_the_account_and_btc_candles(db):
    chart_history.init_chart_db(db)
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO market_candles VALUES ('BTC','1d',?,?,?,?,?,1)",
                     [(i * 86400, 70000 + i, 70100 + i, 69900 + i, 70050 + i) for i in range(800)])
    conn.commit(); conn.close()
    text = btc_lab.briefing(db)
    assert "YOUR ACCOUNT" in text and "DAILY, 731 days" in text and "HOW TO TRADE" in text
    assert "ETH" not in text and "movers" not in text.lower()


class FakeLLM:
    def __init__(self, output):
        self.output, self.kwargs = output, None

    def research(self, prompt, system_prompt=None, timeout=None, **kwargs):
        self.kwargs, self.prompt = kwargs, prompt
        return self.output


def test_an_employee_with_the_feed_trades_the_lab_without_web_search(db):
    emp = staff.hire(db, "BTC Pattern Trader", "Day trades bitcoin on paper from the chart alone.",
                     data_feeds="btc_lab")
    llm = FakeLLM('I see a higher low.\n```orders\n{"orders":[{"side":"buy","usd":250,"reason":"higher low"}]}\n```')
    out = staff.assign(db, llm, emp["key"], "trade")["output"]
    assert llm.kwargs.get("web_search") is False
    assert "YOUR ACCOUNT (BTC lab" in llm.prompt
    assert "FILLED buy" in out and btc_lab.account(db)["btc"] > 0


def test_other_employees_keep_web_search(db):
    emp = staff.hire(db, "Market Researcher", "Researches markets and writes findings for the owner.")
    llm = FakeLLM("done")
    staff.assign(db, llm, emp["key"], "research")
    assert "web_search" not in llm.kwargs
