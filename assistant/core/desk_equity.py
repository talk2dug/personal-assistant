"""Equity over time for the two paper books -- the main crypto desk and the BTC lab --
next to what simply holding BTC from each one's start would be worth.

Both accounts only ever knew their value *now*; "is this working?" needs the line, not
the point. Snapshots are taken on the market poll (throttled to one per
SNAPSHOT_EVERY_SEC) and kept indefinitely -- a row per five minutes is ~100k rows a
year, nothing for SQLite. backfill() rebuilds the curve from trades and the minute
price cache for any stretch before snapshots existed; it only reaches as far back as
market_history does (three days), which covers both runs as of 2026-10-01.
"""
import bisect
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

SNAPSHOT_EVERY_SEC = 300
DESK, LAB = "desk", "btc_lab"

SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_equity (
    account TEXT NOT NULL,
    at TEXT NOT NULL,
    equity REAL NOT NULL,
    hold_equity REAL,
    btc_price REAL,
    PRIMARY KEY (account, at)
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    with closing(_connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _current(db_path: str) -> dict:
    """{account: (equity, hold_equity, btc_price)} for whichever books exist."""
    from . import btc_lab, paper_trading
    out = {}
    try:
        bench = paper_trading.benchmark(db_path)
        if bench:
            out[DESK] = (bench["equity"], bench["btc_hold_equity"], None)
    except Exception:  # noqa: BLE001 -- a missing book is simply not charted
        pass
    try:
        a = btc_lab.account(db_path)
        if a and a.get("price"):
            out[LAB] = (a["equity"], a.get("hold_equity"), a["price"])
    except Exception:  # noqa: BLE001
        pass
    return out


def record(db_path: str, now: datetime | None = None, force: bool = False) -> int:
    """Snapshot both books unless the last snapshot is younger than SNAPSHOT_EVERY_SEC."""
    init_db(db_path)
    now = now or _now()
    written = 0
    with closing(_connect(db_path)) as conn:
        for account, (equity, hold, btc) in _current(db_path).items():
            last = conn.execute("SELECT MAX(at) FROM desk_equity WHERE account = ?", (account,)).fetchone()[0]
            if not force and last and (now - _parse(last)).total_seconds() < SNAPSHOT_EVERY_SEC:
                continue
            conn.execute("INSERT OR REPLACE INTO desk_equity (account, at, equity, hold_equity, btc_price) "
                         "VALUES (?,?,?,?,?)", (account, now.isoformat(), equity, hold, btc))
            written += 1
        conn.commit()
    return written


def curve(db_path: str, account: str, since: str | None = None) -> list[dict]:
    init_db(db_path)
    sql = "SELECT at, equity, hold_equity FROM desk_equity WHERE account = ?"
    args: list = [account]
    if since:
        sql += " AND at >= ?"
        args.append(since)
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY at", args)]


# --- backfill from trades + the minute price cache ------------------------------------

class _Prices:
    """Last known price at or before a moment, per code, from market_history."""

    def __init__(self, conn, codes, since: str):
        self.series = {}
        for code in codes:
            rows = conn.execute("SELECT at, rate FROM market_history WHERE code = ? AND at >= ? "
                                "ORDER BY at", (code, since)).fetchall()
            self.series[code] = ([_parse(r["at"]) for r in rows], [r["rate"] for r in rows])

    def at(self, code, when):
        times, rates = self.series.get(code, ([], []))
        i = bisect.bisect_right(times, when) - 1
        return rates[i] if i >= 0 else (rates[0] if rates else None)


def backfill(db_path: str, step_minutes: int = 15) -> dict:
    """Rebuild both curves from each run's start up to the first stored snapshot."""
    from . import btc_lab, paper_trading
    init_db(db_path)
    out = {}
    with closing(_connect(db_path)) as conn:
        # --- the main desk's current run
        run = paper_trading.current_run(db_path)
        if run and run.get("btc_start_price"):
            trades = [dict(r) for r in conn.execute(
                "SELECT code, side, qty, price, fee, gross, at FROM paper_trades "
                "WHERE at >= ? ORDER BY at", (run["started_at"],))]
            codes = {"BTC"} | {t["code"] for t in trades}
            prices = _Prices(conn, codes, run["started_at"][:10])
            out[DESK] = _replay(conn, DESK, run["started_at"], run["starting_cash"], run["btc_start_price"],
                                trades, prices, step_minutes,
                                buy_cost=lambda t: t["gross"] + t["fee"],
                                sell_proceeds=lambda t: t["gross"] - t["fee"])
        # --- the BTC lab
        a = btc_lab.account(db_path)
        if a and a.get("btc_start_price"):
            trades = [{"code": "BTC", "side": t["side"], "qty": t["btc"], "price": t["price"],
                       "fee": t["fee"], "gross": t["usd"], "at": t["at"]}
                      for t in conn.execute("SELECT * FROM btc_lab_trades ORDER BY at")]
            prices = _Prices(conn, {"BTC"}, a["started_at"][:10])
            out[LAB] = _replay(conn, LAB, a["started_at"], a["starting_cash"], a["btc_start_price"],
                               trades, prices, step_minutes,
                               buy_cost=lambda t: t["gross"] + t["fee"],
                               sell_proceeds=lambda t: t["gross"] - t["fee"])
        conn.commit()
    return out


def _replay(conn, account, started_at, cash, btc_start, trades, prices, step_minutes,
            buy_cost, sell_proceeds) -> int:
    start_cash = cash
    first_snap = conn.execute("SELECT MIN(at) FROM desk_equity WHERE account = ?", (account,)).fetchone()[0]
    end = _parse(first_snap) if first_snap else _now()
    t = _parse(started_at)
    held: dict = {}
    i, written = 0, 0
    while t < end:
        while i < len(trades) and _parse(trades[i]["at"]) <= t:
            tr = trades[i]
            if tr["side"] == "buy":
                cash -= buy_cost(tr)
                held[tr["code"]] = held.get(tr["code"], 0) + tr["qty"]
            elif tr["side"] == "sell":
                cash += sell_proceeds(tr)
                held[tr["code"]] = held.get(tr["code"], 0) - tr["qty"]
            i += 1
        value = sum(q * (prices.at(c, t) or 0) for c, q in held.items() if q > 1e-12)
        btc = prices.at("BTC", t)
        hold = start_cash * btc / btc_start if btc else None
        conn.execute("INSERT OR IGNORE INTO desk_equity (account, at, equity, hold_equity, btc_price) "
                     "VALUES (?,?,?,?,?)", (account, t.isoformat(), cash + value, hold, btc))
        written += 1
        t += timedelta(minutes=step_minutes)
    return written

