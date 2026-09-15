"""The crypto price feed: schema migrations, the Kraken supplemental poller, and the
disappeared-listing diff that must not confuse a supplemental row for a departing one.

Real, confirmed gap: TAO, WLD, AERO, MNT, ETHFI, TIA are not on LiveCoinWatch's feed at
any rank, and its "JUP" resolves to an unrelated micro-coin, not the real Jupiter DEX
token -- see market_data.py's own module docstring and refresh_supplemental() for the
full story. These tests use a mocked HTTP layer throughout; no real network calls.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from io import BytesIO

import pytest

from assistant.core import market_data


def _now():
    return datetime.now(timezone.utc).isoformat()


class FakeHTTPResponse:
    """Stands in for what urllib.request.urlopen returns: a context manager whose body
    is the readable, matching this codebase's existing convention (see
    tests/test_web_api_cameras.py's FakeUpstream)."""

    def __init__(self, payload: dict):
        self._buf = BytesIO(json.dumps(payload).encode())

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


KRAKEN_TICKER_FIXTURE = {
    "error": [],
    "result": {
        "TAOUSD": {"c": ["237.7680", "0.18959"], "o": "232.8905"},
        "WLDUSD": {"c": ["0.3960", "320.61"], "o": "0.3989"},
        "JUPUSD": {"c": ["0.23795", "132.89"], "o": "0.24287"},
    },
}


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "market.db")
    market_data.init_market_db(path)
    return path


class TestSchemaMigration:
    def test_fresh_db_gets_a_source_column_defaulted_to_livecoinwatch(self, db):
        with closing(sqlite3.connect(db)) as conn:
            cols = {row[1]: row for row in conn.execute("PRAGMA table_info(market_coins)")}
        assert "source" in cols
        # row layout: (cid, name, type, notnull, dflt_value, pk)
        assert cols["source"][4] == "'livecoinwatch'"
        assert cols["source"][3] == 1  # NOT NULL

    def test_migration_is_idempotent_and_backfills_existing_rows(self, tmp_path):
        """A db created before this column existed must not break on re-init, and rows
        written before the migration must read back with the LCW default -- they were,
        in fact, all LiveCoinWatch rows before source existed at all."""
        path = str(tmp_path / "old.db")
        # Simulate a pre-migration db: same schema minus the source column.
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript("""
                CREATE TABLE market_coins (
                    code TEXT PRIMARY KEY, name TEXT, rank INTEGER, rate REAL,
                    present INTEGER NOT NULL DEFAULT 1,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            conn.execute(
                "INSERT INTO market_coins (code, name, rank, rate, first_seen, last_seen, updated_at) "
                "VALUES ('BTC','Bitcoin',1,80000.0,?,?,?)", (_now(), _now(), _now()))
            conn.commit()

        market_data.init_market_db(path)   # must not raise
        market_data.init_market_db(path)   # and must be safe to run twice

        with closing(sqlite3.connect(path)) as conn:
            row = conn.execute("SELECT source FROM market_coins WHERE code='BTC'").fetchone()
        assert row[0] == "livecoinwatch"


class TestRefreshSupplemental:
    def test_upserts_kraken_rows_as_tradeable(self, db, monkeypatch):
        monkeypatch.setattr(market_data.urllib.request, "urlopen",
                             lambda url, timeout=15: FakeHTTPResponse(KRAKEN_TICKER_FIXTURE))

        result = market_data.refresh_supplemental(
            db, {"TAO": "TAOUSD", "WLD": "WLDUSD", "JUP": "JUPUSD"})

        assert result["ok"] is True
        assert sorted(result["codes"]) == ["JUP", "TAO", "WLD"]

        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = {r["code"]: r for r in conn.execute(
                "SELECT * FROM market_coins WHERE code IN ('TAO','WLD','JUP')")}

        assert set(rows) == {"TAO", "WLD", "JUP"}
        tao = rows["TAO"]
        assert tao["rate"] == pytest.approx(237.768)
        assert tao["present"] == 1
        assert tao["source"] == "kraken"
        assert tao["last_seen"] is not None

    def test_kraken_rows_also_land_in_the_price_series(self, db, monkeypatch):
        """The seven codes this feed exists to cover were priceable but had NO history,
        so change_since() returned None for every one of them and movers() could never
        surface them -- the desk was blind on exactly the coins it had just been given
        permission to trade. TAO and ETHFI were both bought in that state."""
        monkeypatch.setattr(market_data.urllib.request, "urlopen",
                             lambda url, timeout=15: FakeHTTPResponse(KRAKEN_TICKER_FIXTURE))

        result = market_data.refresh_supplemental(db, {"TAO": "TAOUSD", "WLD": "WLDUSD"})
        assert result["history_points"] == 2

        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = {r["code"]: r for r in conn.execute(
                "SELECT code, rate, market_cap FROM market_history")}
        assert set(rows) == {"TAO", "WLD"}
        assert rows["TAO"]["rate"] == pytest.approx(237.768)
        # A ticker response carries no market cap; inventing one would be worse than null.
        assert rows["TAO"]["market_cap"] is None

    def test_change_since_works_for_a_kraken_coin_once_two_polls_have_run(self, db, monkeypatch):
        first = {"error": [], "result": {"TAOUSD": {"c": ["200.0", "1"], "o": "199.0",
                                                     "v": ["10", "5000"]}}}
        second = {"error": [], "result": {"TAOUSD": {"c": ["220.0", "1"], "o": "199.0",
                                                      "v": ["12", "6000"]}}}
        monkeypatch.setattr(market_data.urllib.request, "urlopen",
                             lambda url, timeout=15: FakeHTTPResponse(first))
        market_data.refresh_supplemental(db, {"TAO": "TAOUSD"})
        monkeypatch.setattr(market_data.urllib.request, "urlopen",
                             lambda url, timeout=15: FakeHTTPResponse(second))
        market_data.refresh_supplemental(db, {"TAO": "TAOUSD"})

        change = market_data.change_since(db, "TAO", minutes=60)
        assert change is not None, "a Kraken-sourced coin must be answerable here"
        assert change["change_pct"] == pytest.approx(10.0)
        # Kraken's volume is [today, last 24h] -- the 24h figure is the comparable one.
        with closing(sqlite3.connect(db)) as conn:
            vols = [r[0] for r in conn.execute(
                "SELECT volume FROM market_history WHERE code='TAO' ORDER BY id")]
        assert vols == [5000.0, 6000.0]

    def test_a_code_missing_from_the_kraken_response_is_simply_skipped(self, db, monkeypatch):
        """If a pair name goes stale (renamed/delisted on Kraken), that one code should be
        skipped rather than the whole refresh failing -- the other requested codes still
        came back with real data."""
        monkeypatch.setattr(market_data.urllib.request, "urlopen",
                             lambda url, timeout=15: FakeHTTPResponse(KRAKEN_TICKER_FIXTURE))

        result = market_data.refresh_supplemental(
            db, {"TAO": "TAOUSD", "GHOST": "GHOSTUSD"})

        assert result["ok"] is True
        assert result["codes"] == ["TAO"]
        assert market_data.list_tracked_codes(db) == ["TAO"]

    def test_all_codes_missing_is_a_reported_failure_not_a_silent_noop(self, db, monkeypatch):
        monkeypatch.setattr(market_data.urllib.request, "urlopen",
                             lambda url, timeout=15: FakeHTTPResponse({"error": [], "result": {}}))

        result = market_data.refresh_supplemental(db, {"GHOST": "GHOSTUSD"})
        assert result["ok"] is False
        assert "error" in result

    def test_a_kraken_api_error_is_reported_not_raised(self, db, monkeypatch):
        monkeypatch.setattr(
            market_data.urllib.request, "urlopen",
            lambda url, timeout=15: FakeHTTPResponse({"error": ["EQuery:Unknown asset pair"], "result": {}}))

        result = market_data.refresh_supplemental(db, {"TAO": "TAOUSD"})
        assert result["ok"] is False
        assert "Unknown asset pair" in result["error"]

    def test_network_failure_is_reported_not_raised(self, db, monkeypatch):
        def _boom(url, timeout=15):
            raise OSError("Connection refused")
        monkeypatch.setattr(market_data.urllib.request, "urlopen", _boom)

        result = market_data.refresh_supplemental(db, {"TAO": "TAOUSD"})
        assert result["ok"] is False
        assert "Connection refused" in result["error"]

    def test_delta_day_is_derived_using_lcws_own_multiplier_convention(self, db, monkeypatch):
        """LCW stores deltas as multipliers (1.02 == +2%, read at the edge by pct()).
        Kraken gives today's open (o) and last trade (c[0]); last/open is already in that
        exact convention, so no further conversion is needed -- this pins that fact."""
        monkeypatch.setattr(market_data.urllib.request, "urlopen",
                             lambda url, timeout=15: FakeHTTPResponse(KRAKEN_TICKER_FIXTURE))

        market_data.refresh_supplemental(db, {"TAO": "TAOUSD"})

        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute("SELECT delta_day FROM market_coins WHERE code='TAO'").fetchone()
        expected_multiplier = 237.7680 / 232.8905
        assert row[0] == pytest.approx(expected_multiplier)
        assert market_data.pct(row[0]) == pytest.approx(2.09, abs=0.01)

    def test_a_second_poll_updates_rather_than_duplicates(self, db, monkeypatch):
        monkeypatch.setattr(market_data.urllib.request, "urlopen",
                             lambda url, timeout=15: FakeHTTPResponse(KRAKEN_TICKER_FIXTURE))
        market_data.refresh_supplemental(db, {"TAO": "TAOUSD"})

        moved = {"error": [], "result": {"TAOUSD": {"c": ["250.00", "1.0"], "o": "232.89"}}}
        monkeypatch.setattr(market_data.urllib.request, "urlopen",
                             lambda url, timeout=15: FakeHTTPResponse(moved))
        market_data.refresh_supplemental(db, {"TAO": "TAOUSD"})

        with closing(sqlite3.connect(db)) as conn:
            rows = conn.execute("SELECT rate FROM market_coins WHERE code='TAO'").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == pytest.approx(250.00)

    def test_an_lcw_owned_row_is_never_downgraded_or_overwritten_by_kraken(self, db, monkeypatch):
        """If LiveCoinWatch itself ever starts covering one of these codes, its row must
        stay the single, authoritative, richer-fielded source -- the two pollers must not
        alternate overwriting one row with two different exchanges' quotes."""
        now = _now()
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                """INSERT INTO market_coins (code, name, rank, rate, present, source,
                                             first_seen, last_seen, updated_at)
                   VALUES ('TAO','Bittensor',42,999.0,1,'livecoinwatch',?,?,?)""",
                (now, now, now))
            conn.commit()

        monkeypatch.setattr(market_data.urllib.request, "urlopen",
                             lambda url, timeout=15: FakeHTTPResponse(KRAKEN_TICKER_FIXTURE))
        market_data.refresh_supplemental(db, {"TAO": "TAOUSD"})

        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT rate, source, rank FROM market_coins WHERE code='TAO'").fetchone()
        assert row == (999.0, "livecoinwatch", 42)   # completely untouched


class TestDisappearedDiffScoping:
    """The real bug this project's tracker flagged: upserting Kraken rows with
    present=1 into the same market_coins table used to make LiveCoinWatch's own
    refresh() treat them as disappearing on every single LCW poll, because its
    previously-present-vs-seen-this-poll diff wasn't scoped by source."""

    def test_a_kraken_sourced_row_survives_an_lcw_poll_that_never_mentions_it(self, db, monkeypatch):
        now = _now()
        with closing(sqlite3.connect(db)) as conn:
            # Seed one Kraken-sourced row (a gap coin) and one ordinary LCW row, mimicking
            # a real running system: both present=1 before the LCW poll below runs.
            conn.execute(
                """INSERT INTO market_coins (code, name, rate, present, source,
                                             first_seen, last_seen, updated_at)
                   VALUES ('TAO','Bittensor',237.5,1,'kraken',?,?,?)""", (now, now, now))
            conn.execute(
                """INSERT INTO market_coins (code, name, rank, rate, present, source,
                                             first_seen, last_seen, updated_at)
                   VALUES ('BTC','Bitcoin',1,80000.0,1,'livecoinwatch',?,?,?)""",
                (now, now, now))
            # A prior successful poll, so refresh() below treats this as a steady-state
            # tick (had_any=True) rather than a first-ever poll with nothing to compare.
            conn.execute("INSERT INTO market_polls (ok, coins, at) VALUES (1, 1, ?)", (now,))
            conn.commit()

        lcw_payload = [{"code": "BTC", "name": "Bitcoin", "rank": 1, "rate": 81000.0,
                        "volume": 1e9, "cap": 1.6e12, "liquidity": 1e8,
                        "delta": {"hour": 1.01, "day": 1.02, "week": 1.05, "month": 1.1},
                        "allTimeHighUSD": 90000.0}]

        def fake_urlopen(req, timeout=25):
            # LiveCoinWatch.coins() posts to /coins/list; .credits() posts to /credits.
            if req.full_url.endswith("/coins/list"):
                return FakeHTTPResponse(lcw_payload)
            return FakeHTTPResponse({"dailyCreditsRemaining": 8000})

        monkeypatch.setattr(market_data.urllib.request, "urlopen", fake_urlopen)

        result = market_data.refresh(db, api_key="fake-key", limit=250)

        assert result["ok"] is True
        assert "TAO" not in result["disappeared"]

        with closing(sqlite3.connect(db)) as conn:
            tao = conn.execute(
                "SELECT present FROM market_coins WHERE code='TAO'").fetchone()
            events = conn.execute(
                "SELECT code FROM market_listings WHERE event='disappeared'").fetchall()

        assert tao[0] == 1   # still present -- never marked absent
        assert ("TAO",) not in events   # no spurious disappearance event written

    def test_an_lcw_sourced_row_absent_from_a_new_poll_is_still_correctly_flagged(self, db, monkeypatch):
        """The fix must scope the diff, not disable it -- a genuine LCW delisting still
        has to be detected and recorded."""
        now = _now()
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                """INSERT INTO market_coins (code, name, rank, rate, present, source,
                                             first_seen, last_seen, updated_at)
                   VALUES ('DOGE','Dogecoin',10,0.1,1,'livecoinwatch',?,?,?)""",
                (now, now, now))
            conn.execute(
                """INSERT INTO market_coins (code, name, rank, rate, present, source,
                                             first_seen, last_seen, updated_at)
                   VALUES ('BTC','Bitcoin',1,80000.0,1,'livecoinwatch',?,?,?)""",
                (now, now, now))
            conn.execute("INSERT INTO market_polls (ok, coins, at) VALUES (1, 2, ?)", (now,))
            conn.commit()

        # This poll mentions BTC only -- DOGE has genuinely dropped out of LCW's response.
        lcw_payload = [{"code": "BTC", "name": "Bitcoin", "rank": 1, "rate": 81000.0,
                        "volume": 1e9, "cap": 1.6e12, "liquidity": 1e8,
                        "delta": {}, "allTimeHighUSD": 90000.0}]

        def fake_urlopen(req, timeout=25):
            if req.full_url.endswith("/coins/list"):
                return FakeHTTPResponse(lcw_payload)
            return FakeHTTPResponse({"dailyCreditsRemaining": 8000})

        monkeypatch.setattr(market_data.urllib.request, "urlopen", fake_urlopen)

        result = market_data.refresh(db, api_key="fake-key", limit=250)

        assert "DOGE" in result["disappeared"]
        with closing(sqlite3.connect(db)) as conn:
            doge = conn.execute("SELECT present FROM market_coins WHERE code='DOGE'").fetchone()
        assert doge[0] == 0


class TestListTrackedCodesIncludesSupplementalRows:
    def test_kraken_sourced_codes_are_tradeable_alongside_lcw_codes(self, db, monkeypatch):
        """list_tracked_codes() is the ground truth paper_trading and the employee
        briefing both read -- it must include a Kraken-sourced gap coin exactly like any
        LCW code, since it filters on present=1 regardless of source."""
        now = _now()
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                """INSERT INTO market_coins (code, name, rank, rate, present, source,
                                             first_seen, last_seen, updated_at)
                   VALUES ('BTC','Bitcoin',1,80000.0,1,'livecoinwatch',?,?,?)""",
                (now, now, now))
            conn.commit()

        monkeypatch.setattr(market_data.urllib.request, "urlopen",
                             lambda url, timeout=15: FakeHTTPResponse(KRAKEN_TICKER_FIXTURE))
        market_data.refresh_supplemental(db, {"TAO": "TAOUSD", "JUP": "JUPUSD"})

        tracked = market_data.list_tracked_codes(db)
        assert set(tracked) == {"BTC", "TAO", "JUP"}
