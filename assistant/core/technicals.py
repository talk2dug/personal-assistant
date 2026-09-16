"""Candles and indicators built from the price cache, so the desk can read a chart.

The trader had none of this. It was shown a spot price, a 1h and 24h percentage change,
and a list of whatever had moved more than 2% — then asked to day-trade. That is not
trading on the curve and it is not trading on news either; it is trading on "something
jumped recently", which is the single worst signal available, because by the time a move
is on a movers list the entry is already gone. Ninety of its first ninety-two exits were
it closing early, and this is the missing input behind that: with no trend and no measure
of momentum, "is this still going up?" had no answer except nerve.

`market_history` already held the answer — 966,000 price points at roughly 1.2-minute
resolution — it had simply never been aggregated into anything a chart reader would
recognise. Nothing new is fetched here. This is arithmetic over data already on disk.

What it deliberately does NOT do: decide. Every function returns measurements with their
own reliability attached, and the employee reads them against its own record. An indicator
that quietly became a rule would just be a worse trader hard-coded in Python.
"""
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# The poll runs about every minute, so these are the windows where a bucket holds enough
# ticks to mean something. Below 5m the candles are mostly one tick and the wicks are noise.
BUCKET_MINUTES = {"5m": 5, "15m": 15, "1h": 60}

# Wilder's default. Not tuned -- a period picked to fit past results would be fitted to
# noise, and the point is to hand over a standard reading the model can reason about.
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0

# Enough candles for the slow average to mean anything. Below this the trend is reported
# as unknown rather than guessed -- a moving average over four points is a rumour.
MIN_CANDLES_FOR_TREND = 20
FAST_MA, SLOW_MA = 7, 21

# A single tick this far from the last one is bad data, not a move. The feed has really
# printed +12,019,772% on a coin; letting that reach an employee as a "mover" is how a
# garbage row becomes a position.
MAX_SANE_TICK_MOVE = 0.5


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def candles(db_path: str, code: str, bucket: str = "15m", limit: int = 60) -> list[dict]:
    """OHLCV candles for one coin, newest last.

    Built by bucketing the tick cache rather than stored: the ticks are already there, and
    a second copy would be one more thing that can silently disagree with the first.
    """
    minutes = BUCKET_MINUTES.get(bucket)
    if minutes is None:
        raise ValueError(f"bucket must be one of {sorted(BUCKET_MINUTES)}")
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes * (limit + 2))).isoformat()

    with closing(_connect(db_path)) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT rate, volume, at FROM market_history"
            " WHERE code = ? AND at >= ? AND rate > 0 ORDER BY at", (code, since))]
    return _bucket(rows, minutes, limit)


def _bucket(rows: list[dict], minutes: int, limit: int) -> list[dict]:
    """Tick rows for ONE code, folded into OHLCV candles, newest last.

    Split out of candles() so scan() can reuse the identical arithmetic: two bucketing
    implementations would be two subtly different charts, and the desk would be reading
    one while its screener ranked on the other.
    """
    if not rows:
        return []

    buckets: dict[str, list[dict]] = {}
    for row in rows:
        stamp = _as_dt(row["at"])
        if stamp is None:
            continue
        floored = stamp.replace(second=0, microsecond=0)
        floored = floored.replace(minute=(floored.minute // minutes) * minutes)
        buckets.setdefault(floored.isoformat(), []).append(row)

    out = []
    for key in sorted(buckets)[-limit:]:
        ticks = buckets[key]
        prices = [t["rate"] for t in ticks]
        volumes = [t["volume"] for t in ticks if t["volume"] is not None]
        out.append({
            "at": key, "open": prices[0], "high": max(prices), "low": min(prices),
            "close": prices[-1], "ticks": len(ticks),
            "volume": round(sum(volumes) / len(volumes), 2) if volumes else None,
        })
    return out


def _as_dt(value):
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def rsi(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    """Relative strength index, Wilder-smoothed.

    None rather than a number when there is not enough history: an RSI computed over four
    candles reads like a signal and is a coin flip, which is worse than an admitted gap.
    """
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for earlier, later in zip(closes, closes[1:]):
        change = later - earlier
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def trend(closes: list[float]) -> dict:
    """Which way it is going, by fast average against slow.

    Reported with the gap between them, because the gap is what says whether the trend is
    strengthening or rolling over -- a fast average still above the slow but converging is
    a rally running out, which is exactly the moment worth catching.
    """
    if len(closes) < MIN_CANDLES_FOR_TREND:
        return {"direction": "unknown", "reason": f"only {len(closes)} candles"}
    fast, slow = sma(closes, FAST_MA), sma(closes, SLOW_MA)
    if fast is None or slow is None or slow == 0:
        return {"direction": "unknown", "reason": "not enough history"}

    spread = (fast - slow) / slow
    previous_fast = sma(closes[:-1], FAST_MA)
    previous_slow = sma(closes[:-1], SLOW_MA)
    widening = None
    if previous_fast is not None and previous_slow not in (None, 0):
        widening = abs(spread) > abs((previous_fast - previous_slow) / previous_slow)

    return {
        "direction": "up" if spread > 0.001 else "down" if spread < -0.001 else "flat",
        "fast": fast, "slow": slow, "spread_pct": round(spread * 100, 2),
        # False on an uptrend means the averages are converging: the move is losing force.
        "strengthening": widening,
    }


def momentum(candle_list: list[dict]) -> dict:
    """Whether a move is still being paid for, or just coasting.

    His actual question -- "knowing when a coin will stop going up" -- has no single
    answer, so this reports the three things that usually turn together: price still making
    higher highs, each leg smaller than the last, and volume falling away under it. Two of
    three agreeing is the honest version of "this is topping".
    """
    if len(candle_list) < 6:
        return {"state": "unknown", "reason": f"only {len(candle_list)} candles"}

    recent, earlier = candle_list[-3:], candle_list[-6:-3]
    higher_highs = max(c["high"] for c in recent) > max(c["high"] for c in earlier)

    def _leg(group):
        return sum(abs(c["close"] - c["open"]) for c in group) / len(group)

    legs_shrinking = _leg(recent) < _leg(earlier)

    recent_vol = [c["volume"] for c in recent if c["volume"] is not None]
    earlier_vol = [c["volume"] for c in earlier if c["volume"] is not None]
    volume_fading = (
        sum(recent_vol) / len(recent_vol) < sum(earlier_vol) / len(earlier_vol)
        if recent_vol and earlier_vol else None)

    fading_signals = sum(1 for flag in (legs_shrinking, volume_fading) if flag)
    if higher_highs and fading_signals >= 2:
        state = "rising but tiring"
    elif higher_highs:
        state = "rising"
    elif not higher_highs and legs_shrinking:
        state = "stalling"
    else:
        state = "falling"

    return {"state": state, "higher_highs": higher_highs,
            "legs_shrinking": legs_shrinking, "volume_fading": volume_fading}


def levels(candle_list: list[dict]) -> dict:
    """Where it has recently turned, and where price sits between those turns."""
    if not candle_list:
        return {}
    highs = [c["high"] for c in candle_list]
    lows = [c["low"] for c in candle_list]
    high, low = max(highs), min(lows)
    close = candle_list[-1]["close"]
    span = high - low
    return {
        "recent_high": high, "recent_low": low, "close": close,
        # 1.0 means sitting on the highs of the window, 0.0 on the lows. A breakout entry
        # and a bounce entry are different trades, and this is what tells them apart.
        "position_in_range": round((close - low) / span, 3) if span > 0 else None,
    }


def read(db_path: str, code: str, bucket: str = "15m", limit: int = 60) -> dict:
    """Everything technical about one coin, as measurements rather than a verdict."""
    candle_list = candles(db_path, code, bucket, limit)
    if not candle_list:
        return {"code": code, "bucket": bucket, "candles": 0,
                "note": "no price history cached for this code"}
    closes = [c["close"] for c in candle_list]
    return {
        "code": code, "bucket": bucket, "candles": len(candle_list),
        "last": closes[-1],
        "rsi": rsi(closes),
        "trend": trend(closes),
        "momentum": momentum(candle_list),
        "levels": levels(candle_list),
    }


# --- screening the whole universe ---------------------------------------------
#
# The desk was told to "look for entries on the curve, not on the movers list" while the
# only coins it was ever handed a chart for WERE the movers list. So on every run it read
# two or three charts, found them all extended (a 1h mover is by definition already up),
# correctly refused to chase, and proposed nothing. 242 tradeable coins, ~3 looked at,
# and its own words on six consecutive runs were "only three coins have technicals this
# run". That is why the book traded six times in four days -- not selectivity, a funnel.
#
# These thresholds are the standing assignment's own entry rules, in code, so the screen
# looks for what the desk is actually asked to buy rather than what happens to be moving.
PULLBACK_RSI = (40.0, 62.0)     # the "RSI in the 40s-50s after a shallow pullback" band
CHASE_RANGE = 0.90              # at/above this much of the range, the move is missed
PULLBACK_RANGE = 0.75           # a pullback has actually pulled back off the highs
LOW_RANGE = 0.25                # the "turning up from the low end of its range" entry


def scan(db_path: str, codes: list[str], bucket: str = "15m",
         limit: int = 60) -> dict[str, dict]:
    """Technical readings for many coins, in one pass over the tick cache.

    Calling read() in a loop costs one query per coin -- 5.6s across the tracked universe,
    on a briefing that is rebuilt every time any market employee wakes. One query filtered
    by time (which is what the `at` index is for) and bucketed per code in Python does the
    same work in well under a second, which is what makes screening all 242 affordable
    enough to do on every run rather than pre-filtering down to a handful first.
    """
    minutes = BUCKET_MINUTES.get(bucket)
    if minutes is None:
        raise ValueError(f"bucket must be one of {sorted(BUCKET_MINUTES)}")
    wanted = {c.upper() for c in codes}
    if not wanted:
        return {}
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes * (limit + 2))).isoformat()

    by_code: dict[str, list[dict]] = {}
    with closing(_connect(db_path)) as conn:
        for row in conn.execute(
                "SELECT code, rate, volume, at FROM market_history"
                " WHERE at >= ? AND rate > 0 ORDER BY code, at", (since,)):
            if row["code"] in wanted:
                by_code.setdefault(row["code"], []).append(dict(row))

    out = {}
    for code in wanted:
        candle_list = _bucket(by_code.get(code, []), minutes, limit)
        if not candle_list:
            out[code] = {"code": code, "bucket": bucket, "candles": 0,
                         "note": "no price history cached for this code"}
            continue
        closes = [c["close"] for c in candle_list]
        out[code] = {
            "code": code, "bucket": bucket, "candles": len(candle_list),
            "last": closes[-1], "rsi": rsi(closes), "trend": trend(closes),
            "momentum": momentum(candle_list), "levels": levels(candle_list),
        }
    return out


def classify_setup(reading: dict) -> tuple[int, str] | None:
    """Which of the desk's entry patterns this chart is, or None if it is neither.

    Returns (score, label) -- deliberately a label rather than a verdict, because the
    trade decision stays the model's. This only answers "is this worth a look", which is
    the question the movers list was answering badly.
    """
    if not reading.get("candles"):
        return None
    rsi_value = reading.get("rsi")
    direction = (reading.get("trend") or {}).get("direction")
    state = (reading.get("momentum") or {}).get("state")
    position = (reading.get("levels") or {}).get("position_in_range")
    if rsi_value is None or position is None or direction == "unknown":
        return None

    # Chasing is the one thing the desk's own record says cost it the most, so an extended
    # chart is filtered out here rather than handed over for the model to refuse again.
    if rsi_value >= RSI_OVERBOUGHT or position >= CHASE_RANGE:
        return None

    lo, hi = PULLBACK_RSI
    if direction == "up" and lo <= rsi_value <= hi and position <= PULLBACK_RANGE:
        return (3, "pullback in uptrend")
    if position <= LOW_RANGE and state == "rising":
        return (3, "turning up off the lows")
    if direction == "up" and state in ("rising", "stalling") and position <= 0.85:
        return (2, "uptrend holding")
    if position <= LOW_RANGE and rsi_value <= RSI_OVERSOLD + 15 and state == "stalling":
        return (1, "basing at the lows, no turn yet")
    return None


def rank_setups(readings: dict[str, dict], exclude: set[str] | None = None,
                order: list[str] | None = None, limit: int = 14) -> list[dict]:
    """The screen's shortlist: charts matching an entry pattern, best first.

    `order` is the feed's own rank ordering, used only to break ties -- between two
    identical-looking setups the more liquid coin is the better paper trade, and without
    it the list would be alphabetical by accident of dict ordering.
    """
    exclude = {c.upper() for c in (exclude or set())}
    rank = {code: i for i, code in enumerate(order or [])}
    scored = []
    for code, reading in readings.items():
        if code in exclude:
            continue
        verdict = classify_setup(reading)
        if verdict is None:
            continue
        score, label = verdict
        scored.append({"code": code, "score": score, "label": label,
                       "reading": reading, "rank": rank.get(code, 10_000)})
    # Best pattern first, then most liquid. A "rising but tiring" chart that still scored
    # sorts below an equally-scored one that is not tiring, since tiring is the desk's own
    # exit signal and a poor thing to enter on.
    scored.sort(key=lambda s: (
        -s["score"],
        (s["reading"].get("momentum") or {}).get("state") == "rising but tiring",
        s["rank"]))
    return scored[:limit]


def sane_move(pct: float | None) -> bool:
    """Whether a reported percentage move is believable.

    The feed has printed +12,019,772% on a coin. Handing that to an employee as a mover is
    how a corrupt row becomes a position.
    """
    return pct is not None and abs(pct) <= MAX_SANE_TICK_MOVE * 100


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.6g}"
    return str(value)


def render(reading: dict) -> str:
    """One coin's technical picture, compactly, for the feed."""
    if not reading.get("candles"):
        return f"    {reading['code']}: {reading.get('note', 'no data')}"

    trend_part = reading["trend"]
    momentum_part = reading["momentum"]
    level_part = reading["levels"]
    rsi_value = reading["rsi"]

    rsi_text = "RSI —"
    if rsi_value is not None:
        tag = (" OVERBOUGHT" if rsi_value >= RSI_OVERBOUGHT
               else " oversold" if rsi_value <= RSI_OVERSOLD else "")
        rsi_text = f"RSI {rsi_value}{tag}"

    trend_text = trend_part.get("direction", "unknown")
    if trend_part.get("spread_pct") is not None:
        trend_text += f" {trend_part['spread_pct']:+.2f}%"
        if trend_part.get("strengthening") is False:
            trend_text += " (converging — losing force)"

    position = level_part.get("position_in_range")
    where = "" if position is None else f", {position:.0%} of range"

    return (f"    {reading['code']} [{reading['bucket']}, {reading['candles']} candles] "
            f"{_fmt(reading['last'])} | trend {trend_text} | {rsi_text} | "
            f"{momentum_part.get('state')}{where} "
            f"(hi {_fmt(level_part.get('recent_high'))} / lo {_fmt(level_part.get('recent_low'))})")


def briefing(db_path: str, codes: list[str], bucket: str = "15m",
             readings: dict[str, dict] | None = None) -> str:
    """The technical block for the crypto feed: the chart the desk never had.

    `readings` lets a caller that has already scanned pass its results in rather than
    paying for the same queries a second time.
    """
    if not codes:
        return ""
    lines = [f"TECHNICALS ({bucket} candles built from the local tick cache — these are "
             f"measurements, not instructions; judge them against your own record):"]
    for code in codes:
        try:
            lines.append(render((readings or {}).get(code) or read(db_path, code, bucket)))
        except Exception as e:
            logger.warning("technicals failed for %s: %s", code, e)
            lines.append(f"    {code}: unavailable ({type(e).__name__})")
    lines.append("    Reading them: an uptrend with RSI under 70 and volume holding is a "
                 "trend worth staying with. 'rising but tiring' means higher highs on "
                 "smaller legs and thinner volume — that is the exit signal you did not "
                 "have before, and it is why so many of your wins were closed early.")
    return "\n".join(lines)


def desk_briefing(db_path: str, tracked: list[str], held: list[str],
                  bucket: str = "15m", shortlist: int = 14) -> str:
    """Charts for everything held, plus a screened shortlist from the whole tracked set.

    Replaces a `held + top-few-1h-movers` focus list that quietly guaranteed no entries.
    Every coin the desk could see a chart for was on that list BECAUSE it had just moved,
    a coin that has just moved is extended, and the rules (rightly) forbid chasing an
    extended chart -- so the screen and the rule cancelled out and nothing was ever
    buyable. The desk said so itself, run after run: "only three coins have technicals
    this run". The screen now looks for the patterns it is actually told to buy, across
    every tracked code, which scan() makes cheap enough to redo on every run.
    """
    held = [c.upper() for c in held]
    codes = list(dict.fromkeys(held + [c.upper() for c in tracked]))
    if not codes:
        return ""
    try:
        readings = scan(db_path, codes, bucket)
    except Exception as e:
        logger.warning("technical scan failed: %s", e)
        return briefing(db_path, held[:8], bucket)

    parts = []
    if held:
        parts.append("YOUR POSITIONS ON THE CHART -- read these before looking at "
                     "anything new:\n"
                     + briefing(db_path, held, bucket, readings))

    picks = rank_setups(readings, exclude=set(held), order=tracked, limit=shortlist)
    if picks:
        lines = [f"SCREENED FOR ENTRIES: {len(picks)} of {len(codes)} tracked coins match "
                 f"an entry pattern you are asked to look for. Charts that are already "
                 f"extended (RSI {RSI_OVERBOUGHT:.0f}+, or {CHASE_RANGE:.0%}+ of range) "
                 f"are filtered OUT before this list is built, so nothing here is a "
                 f"chase. The label is what the numbers say, not a recommendation, and "
                 f"the list is a starting point for your own judgement rather than a "
                 f"queue to work through:"]
        for pick in picks:
            lines.append(f"  [{pick['label']}]")
            lines.append(render(pick["reading"]))
        parts.append("\n".join(lines))
    else:
        parts.append(f"SCREENED FOR ENTRIES: none of the {len(codes)} tracked coins "
                     f"currently matches an entry pattern -- every chart is extended, "
                     f"falling, or short of history. That is a real market condition and "
                     f"not a missing feed, so standing down this run is the right answer.")
    return "\n\n".join(parts)
