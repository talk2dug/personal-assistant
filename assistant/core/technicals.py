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


def briefing(db_path: str, codes: list[str], bucket: str = "15m") -> str:
    """The technical block for the crypto feed: the chart the desk never had."""
    if not codes:
        return ""
    lines = [f"TECHNICALS ({bucket} candles built from the local tick cache — these are "
             f"measurements, not instructions; judge them against your own record):"]
    for code in codes:
        try:
            lines.append(render(read(db_path, code, bucket)))
        except Exception as e:
            logger.warning("technicals failed for %s: %s", code, e)
            lines.append(f"    {code}: unavailable ({type(e).__name__})")
    lines.append("    Reading them: an uptrend with RSI under 70 and volume holding is a "
                 "trend worth staying with. 'rising but tiring' means higher highs on "
                 "smaller legs and thinner volume — that is the exit signal you did not "
                 "have before, and it is why so many of your wins were closed early.")
    return "\n".join(lines)
