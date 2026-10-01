"""The long view of a coin: its 4-hour, daily and weekly chart, and what they say about it.

Until 2026-09-30 the day trader had never seen more than fifteen hours of one coin. It
was shown 15-minute candles built from market_history, which only keeps three days,
so "has this been sliding for a month?", "is it near the bottom of its year?" and "what
does this coin usually do after a drop like this?" had no answer at all. That is how Jack
used to trade by hand -- a few coins, watched over a day, a week, a month and a year until
their habits were familiar -- and it is the view the desk was missing while it lost money
over 25 days in which BTC rose 5% and SOL 15%.

Candles come from Kraken's public OHLC endpoint: free, no key, up to 720 candles per call
(two years of dailies, fourteen of weeklies, four months of 4-hour). Stored locally in
market_candles and topped up on a schedule, so a briefing never waits on the network.

Like technicals.py, this measures and does not decide. The one judgement-shaped output is
the dip/rally study, and it always carries its sample size: "recovered 7 of 9 times" is a
fact about this coin's past; it is up to the desk how much to lean on it.

The watchlist is the other half. The desk used to screen all ~270 tracked coins every
run, which is breadth without depth. It now trades a short list it can actually know,
stored in desk_watchlist so it can change without a deploy.
"""
import logging
import sqlite3
import statistics
import time
from contextlib import closing
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

KRAKEN = "https://api.kraken.com/0/public"
INTERVALS = {"4h": 240, "1d": 1440, "1w": 10080}
# Kraken's public API allows roughly one call a second before it starts refusing.
CALL_SPACING_SEC = 1.1

# The largest liquid, non-stablecoin coins Kraken lists -- the coins with the longest,
# cleanest history to learn from. A starting point, editable in desk_watchlist.
DEFAULT_WATCHLIST = ["BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "ADA", "AVAX", "LTC", "DOT"]

# Kraken's own names for a few assets.
_KRAKEN_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_candles (
    code TEXT NOT NULL,
    interval TEXT NOT NULL,
    ts INTEGER NOT NULL,          -- candle open, epoch seconds UTC
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (code, interval, ts)
);
CREATE TABLE IF NOT EXISTS desk_watchlist (
    code TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    note TEXT
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_chart_db(db_path: str) -> None:
    with closing(_connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM desk_watchlist").fetchone()[0] == 0:
            now = datetime.now(timezone.utc).isoformat()
            conn.executemany("INSERT INTO desk_watchlist (code, added_at, note) VALUES (?,?,?)",
                             [(c, now, "default") for c in DEFAULT_WATCHLIST])
        conn.commit()


def watchlist(db_path: str) -> list[str]:
    init_chart_db(db_path)
    with closing(_connect(db_path)) as conn:
        return [r["code"] for r in conn.execute("SELECT code FROM desk_watchlist ORDER BY rowid")]


# --- fetching -----------------------------------------------------------------

_pair_cache: dict = {"at": 0.0, "map": {}}


def kraken_pairs(client: httpx.Client | None = None) -> dict[str, str]:
    """code -> Kraken USD pair name, cached for a day."""
    if _pair_cache["map"] and time.monotonic() - _pair_cache["at"] < 86400:
        return _pair_cache["map"]
    c = client or httpx.Client(timeout=20)
    body = c.get(f"{KRAKEN}/AssetPairs").json()
    if body.get("error"):
        raise RuntimeError(f"Kraken AssetPairs: {body['error']}")
    out = {}
    for name, p in body["result"].items():
        alt = p.get("altname", "")
        if p.get("quote") not in ("ZUSD", "USD") or not alt.endswith("USD") or ".d" in name:
            continue
        if p.get("status", "online") != "online":
            continue
        code = alt[:-3]
        out[_KRAKEN_ALIASES.get(code, code)] = name
    _pair_cache.update(at=time.monotonic(), map=out)
    return out


def refresh(db_path: str, codes: list[str], intervals=("4h", "1d", "1w")) -> dict:
    """Top up stored candles for these coins. Returns counts and any per-coin errors.

    Kraken returns the most recent 720 candles; the last one is still forming, and is
    overwritten on the next refresh (hence INSERT OR REPLACE rather than skip)."""
    init_chart_db(db_path)
    report = {"stored": 0, "errors": {}, "missing": []}
    with httpx.Client(timeout=20) as client:
        pairs = kraken_pairs(client)
        with closing(_connect(db_path)) as conn:
            for code in codes:
                pair = pairs.get(code.upper())
                if pair is None:
                    report["missing"].append(code)
                    continue
                for iv in intervals:
                    try:
                        body = client.get(f"{KRAKEN}/OHLC",
                                          params={"pair": pair, "interval": INTERVALS[iv]}).json()
                        if body.get("error"):
                            raise RuntimeError(body["error"])
                        rows = next(v for k, v in body["result"].items() if k != "last")
                        conn.executemany(
                            """INSERT OR REPLACE INTO market_candles
                                   (code, interval, ts, open, high, low, close, volume)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            [(code.upper(), iv, int(r[0]), float(r[1]), float(r[2]),
                              float(r[3]), float(r[4]), float(r[6])) for r in rows])
                        conn.commit()
                        report["stored"] += len(rows)
                    except Exception as e:  # noqa: BLE001 -- one coin must not stop the rest
                        report["errors"][f"{code}/{iv}"] = f"{type(e).__name__}: {e}"[:160]
                    time.sleep(CALL_SPACING_SEC)
    return report


def load(db_path: str, code: str, interval: str, limit: int = 800) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT ts, open, high, low, close, volume FROM market_candles
                WHERE code = ? AND interval = ? ORDER BY ts DESC LIMIT ?""",
            (code.upper(), interval, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


# --- measuring ----------------------------------------------------------------

def _sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def _atr_pct(candles: list[dict], n: int = 14) -> float | None:
    if len(candles) < n + 1:
        return None
    trs = []
    for prev, c in zip(candles[-n - 1:-1], candles[-n:]):
        trs.append(max(c["high"] - c["low"], abs(c["high"] - prev["close"]),
                       abs(c["low"] - prev["close"])))
    return sum(trs) / n / candles[-1]["close"] * 100


def _change(closes, back):
    if len(closes) <= back or not closes[-1 - back]:
        return None
    return (closes[-1] / closes[-1 - back] - 1) * 100


def _trend(closes, fast, slow, slope_back):
    """up / down / sideways from two moving averages and the slow one's slope."""
    f, s = _sma(closes, fast), _sma(closes, slow)
    if f is None or s is None or len(closes) < slow + slope_back:
        return "unknown"
    s_then = sum(closes[-slow - slope_back:-slope_back]) / slow
    slope = (s / s_then - 1) * 100
    price = closes[-1]
    if price > s and f > s and slope > 0.5:
        return "up"
    if price < s and f < s and slope < -0.5:
        return "down"
    return "sideways"


def _levels(daily: list[dict], atr_abs: float, lookback: int = 120, wing: int = 3) -> dict:
    """Support and resistance from daily swing highs/lows, clustered within one ATR."""
    d = daily[-lookback:]
    pivots = []
    for i in range(wing, len(d) - wing):
        window = d[i - wing:i + wing + 1]
        if d[i]["low"] == min(c["low"] for c in window):
            pivots.append(d[i]["low"])
        if d[i]["high"] == max(c["high"] for c in window):
            pivots.append(d[i]["high"])
    pivots.sort()
    clusters: list[list[float]] = []
    for p in pivots:
        if clusters and p - clusters[-1][-1] <= atr_abs:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    zones = [(sum(c) / len(c), len(c)) for c in clusters]
    price = daily[-1]["close"]
    below = [z for z in zones if z[0] < price]
    above = [z for z in zones if z[0] > price]
    sup = max(below, key=lambda z: z[0]) if below else None
    res = min(above, key=lambda z: z[0]) if above else None
    return {
        "support": round(sup[0], 8) if sup else None, "support_touches": sup[1] if sup else 0,
        "resistance": round(res[0], 8) if res else None, "resistance_touches": res[1] if res else 0,
    }


def _moves_study(closes: list[float], atr_pct: float, horizon: int = 7, span: int = 3) -> dict:
    """What this coin did after a sharp drop or rally over `span` days, historically.

    A move counts when the `span`-day change is beyond 2.5 daily ATRs (at least 6%).
    Events are not allowed to overlap -- one crash is one event, not three."""
    threshold = max(2.5 * atr_pct, 6.0)
    out = {}
    for kind, sign in (("dip", -1), ("rally", 1)):
        results = []
        i = span
        while i < len(closes) - horizon:
            move = (closes[i] / closes[i - span] - 1) * 100
            if move * sign >= threshold:
                results.append((closes[i + horizon] / closes[i] - 1) * 100)
                i += horizon
            else:
                i += 1
        higher = sum(1 for r in results if r > 0)
        out[kind] = {
            "threshold_pct": round(threshold, 1), "events": len(results),
            "higher_after": higher,
            "median_after_pct": round(statistics.median(results), 1) if results else None,
        }
    return out


def profile(db_path: str, code: str, live_price: float | None = None,
            btc_30d: float | None = None) -> dict | None:
    daily = load(db_path, code, "1d")
    weekly = load(db_path, code, "1w")
    h4 = load(db_path, code, "4h")
    if len(daily) < 60:
        return None
    if live_price:
        daily[-1] = {**daily[-1], "close": live_price}
    closes = [c["close"] for c in daily]
    price = closes[-1]
    year = daily[-365:]
    hi, lo = max(c["high"] for c in year), min(c["low"] for c in year)
    d_atr = _atr_pct(daily)
    w_closes = [c["close"] for c in weekly]
    h4_closes = [c["close"] for c in h4]
    p = {
        "code": code.upper(), "price": price,
        "change": {"1d": _change(closes, 1), "7d": _change(closes, 7), "30d": _change(closes, 30),
                   "90d": _change(closes, 90), "1y": _change(closes, 365)},
        "year_high": hi, "year_low": lo,
        "year_position_pct": round((price - lo) / (hi - lo) * 100) if hi > lo else None,
        "trend": {"weekly": _trend(w_closes, 5, 10, 4), "daily": _trend(closes, 20, 50, 10),
                  "4h": _trend(h4_closes, 20, 50, 6)},
        "atr_daily_pct": round(d_atr, 2) if d_atr else None,
        "atr_4h_pct": round(_atr_pct(h4), 2) if _atr_pct(h4) else None,
        "levels": _levels(daily, (d_atr or 3) / 100 * price) if d_atr else {},
        "moves": _moves_study(closes, d_atr) if d_atr else {},
        "history_days": len(daily),
    }
    if btc_30d is not None and p["change"]["30d"] is not None and code.upper() != "BTC":
        p["vs_btc_30d"] = round(p["change"]["30d"] - btc_30d, 1)
    return p


def _pct(v):
    return "n/a" if v is None else f"{v:+.1f}%"


def _px(v):
    if v is None:
        return "n/a"
    return f"${v:,.2f}" if v >= 1 else f"${v:.4f}" if v >= 0.01 else f"${v:.8f}"


def render(p: dict) -> str:
    t, ch, lv, mv = p["trend"], p["change"], p.get("levels") or {}, p.get("moves") or {}
    lines = [
        f"{p['code']} {_px(p['price'])} | trend weekly {t['weekly']}, daily {t['daily']}, 4h {t['4h']}",
        f"  change 1d {_pct(ch['1d'])} · 7d {_pct(ch['7d'])} · 30d {_pct(ch['30d'])} · "
        f"90d {_pct(ch['90d'])} · 1y {_pct(ch['1y'])}"
        + (f" · vs BTC 30d {p['vs_btc_30d']:+.1f} pts" if "vs_btc_30d" in p else ""),
        f"  year range {_px(p['year_low'])}-{_px(p['year_high'])}, now at "
        f"{p['year_position_pct']}% of it | typical swing: {p['atr_daily_pct']}%/day, "
        f"{p['atr_4h_pct']}%/4h",
    ]
    if lv:
        lines.append(
            f"  support {_px(lv.get('support'))} ({lv.get('support_touches')} touches) · "
            f"resistance {_px(lv.get('resistance'))} ({lv.get('resistance_touches')} touches)")
    habits = []
    for kind, verb in (("dip", "recovered"), ("rally", "kept rising")):
        m = mv.get(kind)
        if m and m["events"]:
            habits.append(f"after a {m['threshold_pct']:g}%+ 3-day {kind}, {verb} within a week "
                          f"{m['higher_after']} of {m['events']} times (median {_pct(m['median_after_pct'])})")
    if habits:
        lines.append("  habits: " + "; ".join(habits))
    return "\n".join(lines)


def briefing(db_path: str, codes: list[str], live_prices: dict | None = None) -> str:
    """The long view for each coin, BTC first as the market's own weather."""
    live_prices = live_prices or {}
    btc = profile(db_path, "BTC", live_prices.get("BTC"))
    btc_30d = btc["change"]["30d"] if btc else None
    blocks = []
    for code in codes:
        p = btc if code.upper() == "BTC" and btc else profile(db_path, code, live_prices.get(code.upper()), btc_30d)
        if p:
            blocks.append(render(p))
        else:
            blocks.append(f"{code.upper()}: no long-range chart stored yet")
    return "\n".join(blocks)
