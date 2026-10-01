"""chart_history: the long-view measurements the desk reads."""
import sqlite3

from assistant.core import chart_history as ch


def _seed(db, code, interval, closes, spread=0.01):
    ch.init_chart_db(db)
    conn = sqlite3.connect(db)
    step = {"4h": 14400, "1d": 86400, "1w": 604800}[interval]
    conn.executemany(
        "INSERT INTO market_candles VALUES (?,?,?,?,?,?,?,?)",
        [(code, interval, i * step, c, c * (1 + spread), c * (1 - spread), c, 1.0)
         for i, c in enumerate(closes)])
    conn.commit(); conn.close()


def test_watchlist_seeds_the_default_once(tmp_path):
    db = str(tmp_path / "t.db")
    assert ch.watchlist(db) == ch.DEFAULT_WATCHLIST
    conn = sqlite3.connect(db); conn.execute("DELETE FROM desk_watchlist WHERE code='BTC'"); conn.commit(); conn.close()
    assert "BTC" not in ch.watchlist(db)  # an edit is not undone by re-seeding


def test_trend_labels():
    rising = [100 * 1.01 ** i for i in range(80)]
    assert ch._trend(rising, 20, 50, 10) == "up"
    assert ch._trend(list(reversed(rising)), 20, 50, 10) == "down"
    assert ch._trend([100.0] * 80, 20, 50, 10) == "sideways"
    assert ch._trend(rising[:30], 20, 50, 10) == "unknown"


def test_moves_study_counts_non_overlapping_dips():
    # One 7% drop, measured from the day it crosses the threshold, then a slow recovery:
    # a single event (not one per day the 3-day window still spans it), and it recovered.
    closes = [100.0] * 20 + [93.0] + [93 + 0.5 * i for i in range(1, 15)] + [100.0] * 10
    out = ch._moves_study(closes, atr_pct=2.0)
    assert out["dip"]["events"] == 1
    assert out["dip"]["higher_after"] == 1
    assert out["rally"]["events"] == 0
    # and a drop that keeps falling after detection counts as not recovered
    falling = [100.0] * 20 + [93.0] + [93 - i for i in range(1, 15)]
    assert ch._moves_study(falling, atr_pct=2.0)["dip"]["higher_after"] == 0


def test_profile_and_render(tmp_path):
    db = str(tmp_path / "t.db")
    closes = [100 * 1.005 ** i for i in range(400)]
    _seed(db, "SOL", "1d", closes)
    _seed(db, "SOL", "1w", closes[::7])
    _seed(db, "SOL", "4h", closes[-120:])
    p = ch.profile(db, "SOL", btc_30d=5.0)
    assert p["trend"]["daily"] == "up" and p["trend"]["weekly"] == "up"
    assert p["year_position_pct"] > 90
    assert p["atr_daily_pct"] > 0
    assert "vs_btc_30d" in p
    text = ch.render(p)
    assert text.startswith("SOL ") and "typical swing" in text


def test_profile_needs_enough_history(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db, "NEW", "1d", [1.0] * 30)
    assert ch.profile(db, "NEW") is None
    assert "no long-range chart" in ch.briefing(db, ["NEW"])
