"""desk_equity: the equity-vs-holding-BTC curves behind the Crypto modal's charts."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from assistant.core import btc_lab, desk_equity, market_data


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "j.db")
    market_data.init_market_db(path)
    conn = sqlite3.connect(path)
    conn.execute("""INSERT INTO market_coins (code, name, rank, rate, present, first_seen, last_seen, updated_at)
                    VALUES ('BTC','Bitcoin',1,80000,1,'x','x','x')""")
    conn.commit(); conn.close()
    return path


def test_record_throttles_to_one_snapshot_per_interval(db):
    btc_lab.open_account(db, 1000.0)
    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    assert desk_equity.record(db, now=t0) == 1
    assert desk_equity.record(db, now=t0 + timedelta(minutes=2)) == 0
    assert desk_equity.record(db, now=t0 + timedelta(minutes=6)) == 1
    assert len(desk_equity.curve(db, desk_equity.LAB)) == 2


def test_backfill_replays_trades_against_the_price_history(db):
    btc_lab.open_account(db, 1000.0)
    start = datetime.fromisoformat(btc_lab.account(db)["started_at"])
    conn = sqlite3.connect(db)
    # BTC flat at 80k, then 88k from 30 minutes in
    for m, rate in ((0, 80000), (30, 88000)):
        conn.execute("INSERT INTO market_history (code, rate, at) VALUES ('BTC', ?, ?)",
                     (rate, (start + timedelta(minutes=m)).isoformat()))
    conn.commit(); conn.close()
    btc_lab.execute_orders(db, [{"side": "buy", "usd": 500}])           # 0.00625 BTC at 80k, fee 0.50
    desk_equity.record(db, now=start + timedelta(minutes=46), force=True)  # backfill stops here
    desk_equity.backfill(db, step_minutes=15)
    pts = {p["at"][:16]: p for p in desk_equity.curve(db, desk_equity.LAB)}
    at45 = pts[(start + timedelta(minutes=45)).isoformat()[:16]]
    assert at45["equity"] == pytest.approx(499.5 + 0.00625 * 88000)     # cash + BTC at 88k
    assert at45["hold_equity"] == pytest.approx(1100.0)


def test_a_missing_book_is_simply_not_charted(db):
    assert desk_equity.record(db) == 0
    assert desk_equity.curve(db, desk_equity.DESK) == []
