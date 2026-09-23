"""Candles and indicators, and the one question they exist to answer.

The desk had no chart at all. It saw a spot price, a 1h and 24h percentage change, and a
list of whatever had jumped — then was asked to day-trade. Ninety of its first ninety-two
exits were it closing a winner early, which is what happens when "is this still going up?"
has no answer except nerve.

So the tests that matter here are not "does RSI compute". They are: does a rally running
out actually read as running out, does a clean trend read as worth holding, and does the
module refuse to produce a confident number from four data points.
"""
import math
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from assistant.core import db as core_db, technicals


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "market.db")
    core_db.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS market_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, rate REAL,
            volume REAL, market_cap REAL, at TEXT)""")
    return path


def _seed(db_path, code, prices, volumes=None, minutes_apart=1):
    """Ticks ending now, one a minute, like the real poller writes them."""
    start = datetime.now(timezone.utc) - timedelta(minutes=minutes_apart * len(prices))
    with sqlite3.connect(db_path) as conn:
        for i, price in enumerate(prices):
            conn.execute(
                "INSERT INTO market_history (code, rate, volume, market_cap, at)"
                " VALUES (?, ?, ?, ?, ?)",
                (code, price, (volumes[i] if volumes else 1000.0), 0.0,
                 (start + timedelta(minutes=minutes_apart * i)).isoformat()))


class TestCandles:
    def test_ticks_become_ohlc(self, db_path):
        _seed(db_path, "BTC", [100, 110, 90, 105] * 10)
        candles = technicals.candles(db_path, "BTC", "5m")
        assert candles
        for candle in candles:
            assert candle["high"] >= candle["open"] >= candle["low"]
            assert candle["high"] >= candle["close"] >= candle["low"]
        # Checked across all candles rather than the first: buckets are aligned to the
        # wall clock, so whichever one the run happens to start in is partial. Asserting
        # on candles[0] passed alone and failed in the suite purely on what minute it was.
        assert max(c["high"] for c in candles) == 110
        assert min(c["low"] for c in candles) == 90

    def test_a_coin_with_no_history_returns_nothing_rather_than_zeros(self, db_path):
        assert technicals.candles(db_path, "NOPE", "5m") == []

    def test_an_unknown_bucket_is_refused(self, db_path):
        with pytest.raises(ValueError):
            technicals.candles(db_path, "BTC", "3d")

    def test_bigger_buckets_hold_more_ticks(self, db_path):
        _seed(db_path, "BTC", list(range(100, 160)))
        five = technicals.candles(db_path, "BTC", "5m")
        fifteen = technicals.candles(db_path, "BTC", "15m")
        assert max(c["ticks"] for c in fifteen) > max(c["ticks"] for c in five)

    def test_zero_prices_are_ignored(self, db_path):
        """A zero print is a bad row, and it would drag every low to zero."""
        _seed(db_path, "BTC", [100, 0, 102, 101] * 10)
        assert all(c["low"] > 0 for c in technicals.candles(db_path, "BTC", "5m"))


class TestRSI:
    def test_a_steady_climb_reads_overbought(self):
        assert technicals.rsi([100 + i for i in range(40)]) > technicals.RSI_OVERBOUGHT

    def test_a_steady_fall_reads_oversold(self):
        assert technicals.rsi([200 - i for i in range(40)]) < technicals.RSI_OVERSOLD

    def test_too_little_history_returns_nothing_rather_than_a_guess(self):
        """An RSI over four candles reads like a signal and is a coin flip, which is
        worse than an admitted gap."""
        assert technicals.rsi([100, 101, 102]) is None


class TestTrend:
    def test_a_rising_series_trends_up(self):
        assert technicals.trend([100 + i * 2 for i in range(40)])["direction"] == "up"

    def test_a_falling_series_trends_down(self):
        assert technicals.trend([200 - i * 2 for i in range(40)])["direction"] == "down"

    def test_a_flat_series_is_flat(self):
        assert technicals.trend([100.0] * 40)["direction"] == "flat"

    def test_too_few_candles_is_unknown_not_flat(self):
        """A moving average over four points is a rumour, and 'flat' would read as a
        finding rather than an absence."""
        result = technicals.trend([100, 101, 102])
        assert result["direction"] == "unknown" and "candles" in result["reason"]

    def test_converging_averages_show_a_move_losing_force(self):
        """A fast average still above the slow but closing on it is a rally running out —
        the moment worth catching, and invisible from direction alone."""
        climbing = [100 + i * 3 for i in range(30)]
        flattening = climbing + [climbing[-1] + 0.05 * i for i in range(12)]
        assert technicals.trend(flattening)["strengthening"] is False


class TestMomentumAnswersTheRealQuestion:
    """"Knowing when a coin will stop going up" — his words, and the whole point."""

    def _candles(self, legs, volumes=None):
        out, price = [], 100.0
        for i, leg in enumerate(legs):
            out.append({"open": price, "close": price + leg,
                        "high": max(price, price + leg), "low": min(price, price + leg),
                        "volume": (volumes[i] if volumes else 1000.0), "ticks": 5})
            price += leg
        return out

    def test_a_healthy_rally_reads_as_rising(self):
        candles = self._candles([5, 5, 5, 5, 5, 5], [1000] * 6)
        assert technicals.momentum(candles)["state"] == "rising"

    def test_a_rally_on_shrinking_legs_and_thinning_volume_reads_as_tiring(self):
        """Higher highs, but each push smaller and less volume under it. This is the exit
        signal the desk never had."""
        candles = self._candles([10, 10, 10, 3, 2, 1], [2000, 2000, 2000, 700, 600, 500])
        assert technicals.momentum(candles)["state"] == "rising but tiring"

    def test_a_decline_reads_as_falling(self):
        assert technicals.momentum(self._candles([-5] * 6))["state"] == "falling"

    def test_too_few_candles_is_unknown(self):
        assert technicals.momentum(self._candles([1, 2]))["state"] == "unknown"


class TestLevels:
    def test_it_says_where_price_sits_between_recent_turns(self):
        """A breakout entry and a bounce entry are different trades, and this is what
        tells them apart."""
        candles = [{"high": 110, "low": 90, "open": 95, "close": 109, "volume": 1}]
        assert technicals.levels(candles)["position_in_range"] == pytest.approx(0.95)

    def test_a_flat_range_has_no_position(self):
        candles = [{"high": 100, "low": 100, "open": 100, "close": 100, "volume": 1}]
        assert technicals.levels(candles)["position_in_range"] is None


class TestBadDataNeverReachesTheDesk:
    def test_an_impossible_move_is_rejected(self):
        """The feed has really printed +12,019,772% on a coin. Handing that to a trader as
        'in motion' is how a corrupt row becomes a position."""
        assert technicals.sane_move(12019772.67) is False

    def test_a_real_move_is_kept(self):
        assert technicals.sane_move(22.4) is True
        assert technicals.sane_move(-25.08) is True

    def test_a_missing_move_is_not_sane(self):
        assert technicals.sane_move(None) is False


class TestTheBriefing:
    def test_it_reads_as_measurements_not_instructions(self, db_path):
        _seed(db_path, "BTC", [100 + i for i in range(80)])
        text = technicals.briefing(db_path, ["BTC"])
        assert "measurements, not instructions" in text
        assert "BTC" in text

    def test_a_coin_with_no_data_says_so_rather_than_being_omitted(self, db_path):
        """Silently dropping it would read as 'nothing notable' instead of 'unknown'."""
        assert "no price history" in technicals.briefing(db_path, ["GHOST"])

    def test_an_overbought_reading_is_labelled(self, db_path):
        # Enough ticks to fill 15+ fifteen-minute candles: RSI needs 15 closes, and with
        # fewer it correctly reports nothing rather than a number.
        _seed(db_path, "HOT", [100 + i * 0.5 for i in range(400)])
        assert "OVERBOUGHT" in technicals.briefing(db_path, ["HOT"])

    def test_a_short_history_reports_no_rsi_rather_than_a_made_up_one(self, db_path):
        _seed(db_path, "NEW", [100 + i for i in range(40)])
        assert "RSI —" in technicals.briefing(db_path, ["NEW"])

    def test_no_codes_produces_nothing(self, db_path):
        assert technicals.briefing(db_path, []) == ""

    def test_one_broken_coin_does_not_lose_the_rest(self, db_path, monkeypatch):
        _seed(db_path, "BTC", [100 + i for i in range(80)])
        real_read = technicals.read

        def flaky(path, code, *a, **k):
            if code == "BAD":
                raise RuntimeError("boom")
            return real_read(path, code, *a, **k)

        monkeypatch.setattr(technicals, "read", flaky)
        text = technicals.briefing(db_path, ["BAD", "BTC"])
        assert "unavailable" in text and "BTC" in text


def _pullback(scale=1.0):
    """A clean uptrend that has pulled back off its highs.

    This is the setup the desk is asked to look for and the one its old movers-only screen
    could never show it -- by the time a coin reaches a movers list it is at the top of its
    range, not backed off it. The sine wobble matters: a perfectly straight rise pins RSI
    at 100, which is not a chart any real coin prints.
    """
    rise = [(100 + i * 0.4 + 1.5 * math.sin(i / 6.0)) * scale for i in range(200)]
    return rise + [rise[-1] - i * 0.9 * scale for i in range(30)]


def _bounce(scale=1.0):
    """A clean downtrend that has bounced off its lows -- the short-side mirror of
    _pullback(): the setup a short-the-bounce entry is asked to look for."""
    fall = [(100 - i * 0.4 - 1.5 * math.sin(i / 6.0)) * scale for i in range(200)]
    return fall + [fall[-1] + i * 0.9 * scale for i in range(30)]


def _extended(scale=1.0):
    """Straight up into the close: RSI pinned, sitting on the highs. This is what a coin on
    the 1h movers list looks like, and it is exactly what the rules forbid buying."""
    return [(100 + i * 0.6) * scale for i in range(200)]


class TestScan:
    def test_scan_matches_read_coin_for_coin(self, db_path):
        """One batched pass has to produce the same chart as the per-coin read, or the
        screen would rank on numbers the model never sees."""
        _seed(db_path, "AAA", _pullback())
        _seed(db_path, "BBB", _extended())
        scanned = technicals.scan(db_path, ["AAA", "BBB"], "5m")
        for code in ("AAA", "BBB"):
            assert scanned[code] == technicals.read(db_path, code, "5m")

    def test_a_coin_with_no_history_is_reported_not_dropped(self, db_path):
        _seed(db_path, "AAA", _pullback())
        scanned = technicals.scan(db_path, ["AAA", "NOPE"], "5m")
        assert scanned["NOPE"]["candles"] == 0
        assert "no price history" in scanned["NOPE"]["note"]

    def test_an_unknown_bucket_is_refused(self, db_path):
        with pytest.raises(ValueError):
            technicals.scan(db_path, ["AAA"], "3d")


class TestScreening:
    """The funnel that stopped the desk trading.

    It was told "look for entries on the curve, not on the movers list", and the only
    charts it was ever handed WERE the movers list. Every one was extended, it correctly
    refused to chase all of them, and it proposed nothing -- six trades in four days with
    242 coins tradeable, writing "only three coins have technicals this run" as it went.
    These pin the two halves: an extended chart never reaches the shortlist, and a
    pulled-back one does.
    """

    def test_an_extended_chart_is_filtered_out_before_the_desk_sees_it(self, db_path):
        _seed(db_path, "HOT", _extended())
        assert technicals.classify_setup(technicals.read(db_path, "HOT", "5m")) is None

    def test_a_pullback_in_an_uptrend_is_what_gets_through(self, db_path):
        _seed(db_path, "CALM", _pullback())
        verdict = technicals.classify_setup(technicals.read(db_path, "CALM", "5m"))
        assert verdict is not None
        assert verdict[1] == "pullback in uptrend"

    def test_a_chart_too_short_to_read_is_not_guessed_at(self, db_path):
        _seed(db_path, "NEW", [100, 101, 102])
        assert technicals.classify_setup(technicals.read(db_path, "NEW", "5m")) is None

    def test_the_shortlist_ranks_setups_and_drops_the_rest(self, db_path):
        _seed(db_path, "CALM", _pullback())
        _seed(db_path, "HOT", _extended())
        readings = technicals.scan(db_path, ["CALM", "HOT"], "5m")
        picks = technicals.rank_setups(readings, order=["CALM", "HOT"])
        assert [p["code"] for p in picks] == ["CALM"]

    def test_what_is_already_held_is_not_offered_as_a_new_entry(self, db_path):
        _seed(db_path, "CALM", _pullback())
        readings = technicals.scan(db_path, ["CALM"], "5m")
        assert technicals.rank_setups(readings, exclude={"CALM"}) == []

    def test_ties_break_towards_the_more_liquid_coin(self, db_path):
        _seed(db_path, "BIG", _pullback())
        _seed(db_path, "SMALL", _pullback())
        readings = technicals.scan(db_path, ["BIG", "SMALL"], "5m")
        picks = technicals.rank_setups(readings, order=["BIG", "SMALL"])
        assert [p["code"] for p in picks] == ["BIG", "SMALL"]


class TestShortScreening:
    """The mirror of TestScreening, for the short side added alongside it. Same shape of
    proof: a chart extended on the long side is not automatically disqualified on the
    short side, and a genuine bounce-in-downtrend is what gets through."""

    def test_a_bounce_in_a_downtrend_is_what_gets_through(self, db_path):
        _seed(db_path, "FALL", _bounce())
        verdict = technicals.classify_setup(technicals.read(db_path, "FALL", "5m"))
        assert verdict is not None
        assert verdict[1] == "bounce in downtrend"
        assert verdict[2] == "short"

    def test_long_branches_still_report_long(self, db_path):
        _seed(db_path, "CALM", _pullback())
        verdict = technicals.classify_setup(technicals.read(db_path, "CALM", "5m"))
        assert verdict[2] == "long"

    def test_a_crashed_chart_is_filtered_out_of_the_short_side_too(self, db_path):
        """The mirror of the chase filter: a coin already at the bottom of its range with
        no bounce yet is not a short candidate -- that decline is spent, not starting. (It
        may still be a valid LONG basing setup, which is a separate, pre-existing branch
        this isn't testing -- only that it never comes back labelled "short".)"""
        crashed = [100 - i * 0.5 for i in range(200)]  # straight down, pinned at the lows
        _seed(db_path, "DEAD", crashed)
        verdict = technicals.classify_setup(technicals.read(db_path, "DEAD", "5m"))
        assert verdict is None or verdict[2] != "short"

    def test_the_shortlist_carries_direction_and_counts_both_sides(self, db_path):
        _seed(db_path, "CALM", _pullback())
        _seed(db_path, "FALL", _bounce())
        readings = technicals.scan(db_path, ["CALM", "FALL"], "5m")
        picks = technicals.rank_setups(readings, order=["CALM", "FALL"])
        by_code = {p["code"]: p["direction"] for p in picks}
        assert by_code == {"CALM": "long", "FALL": "short"}


class TestTrendBaseDown:
    def test_base_down_mirrors_base_up_on_a_falling_chart(self, db_path):
        _seed(db_path, "FALL", _bounce())
        reading = technicals.read(db_path, "FALL", "5m")
        trend = reading["trend"]
        assert trend["base_down"] is True
        assert trend["base_up"] is False

    def test_base_down_is_false_on_a_rising_chart(self, db_path):
        _seed(db_path, "CALM", _pullback())
        reading = technicals.read(db_path, "CALM", "5m")
        assert reading["trend"]["base_down"] is False


class TestDeskBriefing:
    def test_positions_and_candidates_both_appear(self, db_path):
        _seed(db_path, "HELD", _pullback(scale=0.5))
        _seed(db_path, "CALM", _pullback())
        _seed(db_path, "HOT", _extended())
        out = technicals.desk_briefing(db_path, ["HELD", "CALM", "HOT"], ["HELD"], "5m")
        assert "YOUR POSITIONS ON THE CHART" in out
        assert "SCREENED FOR ENTRIES" in out
        assert "CALM" in out
        # A held coin belongs in the positions block, never in the entry shortlist.
        assert out.index("HELD") < out.index("SCREENED FOR ENTRIES")
        assert "HELD" not in out[out.index("SCREENED FOR ENTRIES"):]

    def test_an_empty_screen_says_so_as_a_market_condition(self, db_path):
        """The distinction the desk could never draw: nothing qualifying is a real state of
        the market; two charts out of 242 is a broken funnel. It saw the second and
        reported the first, on run after run."""
        _seed(db_path, "HOT", _extended())
        out = technicals.desk_briefing(db_path, ["HOT"], [], "5m")
        assert "none of the" in out
        assert "real market condition" in out

    def test_a_flat_book_still_gets_a_screen(self, db_path):
        _seed(db_path, "CALM", _pullback())
        out = technicals.desk_briefing(db_path, ["CALM"], [], "5m")
        assert "YOUR POSITIONS ON THE CHART" not in out
        assert "CALM" in out

    def test_short_candidates_are_labelled_and_counted_separately(self, db_path):
        _seed(db_path, "CALM", _pullback())
        _seed(db_path, "FALL", _bounce())
        out = technicals.desk_briefing(db_path, ["CALM", "FALL"], [], "5m")
        assert "[pullback in uptrend · LONG]" in out
        assert "[bounce in downtrend · SHORT]" in out
        assert "1 long, 1 short" in out
