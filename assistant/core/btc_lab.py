"""The BTC lab: one employee, one coin, $1,000, and nothing but the chart.

Jack's experiment, 2026-10-01: "spin up an agent and have it day trade on just bitcoin.
Start fresh, $1000, nothing previously learned. Just give it the charts for the past two
years and have it figure out the pattern... using candles and patterns to know when to
buy and sell. Let it make as many buys and sells as it wants. All on paper."

So this is deliberately NOT the main crypto desk:

  * Its own ledger (btc_lab_* tables), not paper_trades. The main desk's rules -- a
    mandatory stop and target, the 2:1 floor, mechanical-only exits, cooldowns, the
    watchlist -- are lessons that desk learned, and this agent is meant to start without
    any of them. Here it may buy any dollar amount and sell any amount, whenever.
  * Stops and targets are optional. If it sets them, check_levels() enforces them every
    minute between its runs; if it doesn't, nothing closes a position but its own sell.
  * No news, no movers list, no colleague briefing, no shared journal. Its briefing is
    its own account and BTC's candles: weekly and daily for two years, 4-hour for a
    month, hourly for three days, 15-minute for a day. No indicators are computed for
    it -- reading the candles is the experiment.
  * Long only (cash or BTC), and the same 0.1% fee per fill as the main desk so the two
    results can be compared fairly.

It is judged the same way as the desk: against simply holding BTC from its first day.
"""
import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STARTING_CASH = 1000.0
FEE_PCT = 0.1
CODE = "BTC"
STAFF_KEY = "btc_pattern_trader"

SCHEMA = """
CREATE TABLE IF NOT EXISTS btc_lab_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    started_at TEXT NOT NULL,
    starting_cash REAL NOT NULL,
    btc_start_price REAL,
    cash REAL NOT NULL,
    btc REAL NOT NULL DEFAULT 0,
    cost_basis REAL NOT NULL DEFAULT 0,      -- dollars paid for the BTC held, fees included
    stop_loss REAL,
    take_profit REAL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS btc_lab_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    btc REAL NOT NULL,
    price REAL NOT NULL,
    usd REAL NOT NULL,
    fee REAL NOT NULL,
    realized REAL,
    cash_after REAL NOT NULL,
    btc_after REAL NOT NULL,
    reason TEXT,
    trigger TEXT NOT NULL DEFAULT 'agent',   -- agent | stop_loss | take_profit
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS btc_lab_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    with closing(_connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def price(db_path: str) -> float | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT rate FROM market_coins WHERE code = ?", (CODE,)).fetchone()
    return float(row["rate"]) if row and row["rate"] else None


def open_account(db_path: str, starting_cash: float = STARTING_CASH) -> dict:
    """Start the experiment. Refuses to restart one already running -- a fresh start
    wipes its record, and that should be a deliberate act (reset=True on start())."""
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        if conn.execute("SELECT 1 FROM btc_lab_account").fetchone():
            raise ValueError("the BTC lab account already exists")
        now = _now()
        conn.execute("INSERT INTO btc_lab_account (id, started_at, starting_cash, btc_start_price, "
                     "cash, updated_at) VALUES (1, ?, ?, ?, ?, ?)",
                     (now, starting_cash, price(db_path), starting_cash, now))
        conn.commit()
    return account(db_path)


def account(db_path: str) -> dict | None:
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM btc_lab_account WHERE id = 1").fetchone()
    if row is None:
        return None
    a = dict(row)
    p = price(db_path)
    a["price"] = p
    a["btc_value"] = a["btc"] * p if p else None
    a["equity"] = a["cash"] + (a["btc_value"] or 0)
    a["unrealized"] = (a["btc_value"] - a["cost_basis"]) if a["btc"] and p else 0.0
    if a.get("btc_start_price") and p:
        a["hold_equity"] = a["starting_cash"] * p / a["btc_start_price"]
        a["vs_hold"] = a["equity"] - a["hold_equity"]
    return a


def performance(db_path: str) -> dict:
    """The fields crypto_journal's RECORD line reads."""
    a = account(db_path) or {}
    with closing(_connect(db_path)) as conn:
        sells = [r["realized"] for r in conn.execute(
            "SELECT realized FROM btc_lab_trades WHERE side = 'sell' AND realized IS NOT NULL")]
        fees = conn.execute("SELECT COALESCE(SUM(fee), 0) FROM btc_lab_trades").fetchone()[0]
        buys = conn.execute("SELECT COUNT(*) FROM btc_lab_trades WHERE side = 'buy'").fetchone()[0]
    wins = [r for r in sells if r > 0]
    return {
        "closed_trades": len(sells), "wins": len(wins),
        "win_rate_pct": round(len(wins) / len(sells) * 100, 1) if sells else None,
        "realized_pnl": round(sum(sells), 2), "fees_paid": round(fees, 2),
        "equity": round(a.get("equity") or 0, 2), "buys": buys,
    }


# --- orders --------------------------------------------------------------------------

def parse_orders(text: str) -> list[dict]:
    """The last ```orders fenced block, as {"orders": [...]} or a bare list."""
    blocks = re.findall(r"```orders\s*\n(.*?)```", text or "", flags=re.S)
    if not blocks:
        return []
    try:
        body = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return [{"_invalid": blocks[-1][:200]}]
    orders = body.get("orders") if isinstance(body, dict) else body
    return orders if isinstance(orders, list) else []


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fill(conn, a: dict, side: str, btc: float, px: float, reason: str, trigger: str) -> dict:
    usd = btc * px
    fee = usd * FEE_PCT / 100
    realized = None
    if side == "buy":
        cash = a["cash"] - usd - fee
        held = a["btc"] + btc
        basis = a["cost_basis"] + usd + fee
    else:
        share = btc / a["btc"]
        basis_out = a["cost_basis"] * share
        realized = usd - fee - basis_out
        cash = a["cash"] + usd - fee
        held = a["btc"] - btc
        basis = a["cost_basis"] - basis_out
        if held < 1e-10:
            held, basis = 0.0, 0.0
    now = _now()
    levels = "" if held else ", stop_loss = NULL, take_profit = NULL"
    conn.execute(f"UPDATE btc_lab_account SET cash = ?, btc = ?, cost_basis = ?, updated_at = ?{levels} "
                 "WHERE id = 1", (cash, held, basis, now))
    conn.execute("INSERT INTO btc_lab_trades (side, btc, price, usd, fee, realized, cash_after, "
                 "btc_after, reason, trigger, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (side, btc, px, usd, fee, realized, cash, held, reason, trigger, now))
    a.update(cash=cash, btc=held, cost_basis=basis)
    fill = {"side": side, "code": CODE, "qty": btc, "price": px, "usd": round(usd, 2),
            "fee": round(fee, 4), "reason": reason, "trigger": trigger}
    if realized is not None:
        fill["realized"] = round(realized, 2)
    return fill


def execute_orders(db_path: str, orders: list[dict]) -> dict:
    """Fill what can be filled at the live price; every refusal carries its reason."""
    fills, rejections = [], []
    px = price(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM btc_lab_account WHERE id = 1").fetchone()
        if row is None:
            raise ValueError("the BTC lab account has not been opened")
        a = dict(row)

        def reject(order, why):
            rejections.append({"order": order, "reason": why})
            conn.execute("INSERT INTO btc_lab_rejections (order_json, reason, at) VALUES (?,?,?)",
                         (json.dumps(order)[:500], why, _now()))

        for order in orders:
            if not isinstance(order, dict) or "_invalid" in order:
                reject(order, "the orders block was not valid JSON")
                continue
            side = str(order.get("side", "")).lower()
            reason = str(order.get("reason") or "")[:300]
            if px is None:
                reject(order, "no live BTC price right now")
                continue
            if side == "buy":
                if str(order.get("usd")).lower() == "all":
                    usd = a["cash"] / (1 + FEE_PCT / 100)
                else:
                    usd = _num(order.get("usd"))
                if not usd or usd <= 0:
                    reject(order, "buy needs a positive `usd` amount (or \"all\")")
                    continue
                if usd * (1 + FEE_PCT / 100) > a["cash"] + 1e-6:
                    reject(order, f"not enough cash: ${usd:,.2f} plus fee, have ${a['cash']:,.2f}")
                    continue
                fills.append(_fill(conn, a, "buy", usd / px, px, reason, "agent"))
            elif side == "sell":
                raw = order.get("btc", order.get("qty"))
                if str(raw).lower() == "all":
                    qty = a["btc"]
                elif order.get("usd") is not None:
                    qty = (_num(order.get("usd")) or 0) / px
                else:
                    qty = _num(raw)
                if not qty or qty <= 0 or a["btc"] <= 0:
                    reject(order, "nothing to sell" if a["btc"] <= 0 else
                           "sell needs `btc` (an amount or \"all\") or `usd`")
                    continue
                qty = min(qty, a["btc"])
                fills.append(_fill(conn, a, "sell", qty, px, reason, "agent"))
            elif side == "levels":
                # Optional resting exits for whatever it holds; null clears one.
                stop, target = order.get("stop_loss"), order.get("take_profit")
                if a["btc"] <= 0:
                    reject(order, "no BTC held to set exit levels on")
                    continue
                s, t = _num(stop), _num(target)
                if stop is not None and s is not None and s >= px:
                    reject(order, f"a stop at ${s:,.0f} is at or above the price ${px:,.0f}")
                    continue
                if target is not None and t is not None and t <= px:
                    reject(order, f"a target at ${t:,.0f} is at or below the price ${px:,.0f}")
                    continue
                if "stop_loss" in order:
                    conn.execute("UPDATE btc_lab_account SET stop_loss = ? WHERE id = 1", (s,))
                if "take_profit" in order:
                    conn.execute("UPDATE btc_lab_account SET take_profit = ? WHERE id = 1", (t,))
                fills.append({"side": "levels", "code": CODE, "stop_loss": s, "take_profit": t,
                              "price": px, "reason": reason})
            else:
                reject(order, "side must be buy, sell or levels")
        conn.commit()
    return {"fills": fills, "rejections": rejections, "account": account(db_path)}


def check_levels(db_path: str) -> dict | None:
    """Mechanical exits between runs: sell everything if price crosses a level it set."""
    a = account(db_path)
    if not a or a["btc"] <= 0 or a["price"] is None:
        return None
    px = a["price"]
    trigger = None
    if a.get("stop_loss") and px <= a["stop_loss"]:
        trigger = "stop_loss"
    elif a.get("take_profit") and px >= a["take_profit"]:
        trigger = "take_profit"
    if trigger is None:
        return None
    with closing(_connect(db_path)) as conn:
        row = dict(conn.execute("SELECT * FROM btc_lab_account WHERE id = 1").fetchone())
        fill = _fill(conn, row, "sell", row["btc"], px, f"{trigger} hit at ${px:,.0f}", trigger)
        conn.commit()
    return fill


# --- the briefing ----------------------------------------------------------------------

def _rows(candles: list[dict], fmt) -> str:
    return "\n".join(fmt(c) for c in candles)


def _day(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _hour(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M")


def briefing(db_path: str) -> str:
    from . import chart_history, technicals

    chart_history.init_chart_db(db_path)
    a = account(db_path)
    if a is None:
        return "BTC LAB: account not opened yet."
    perf = performance(db_path)
    lines = ["--- YOUR ACCOUNT (BTC lab, paper) ---",
             f"Started {a['started_at'][:16]}Z with ${a['starting_cash']:,.2f}. "
             f"BTC price now ${a['price']:,.2f}." if a["price"] else "BTC price unavailable right now.",
             f"Cash ${a['cash']:,.2f} | BTC {a['btc']:.8f} (worth ${a['btc_value'] or 0:,.2f}, "
             f"cost ${a['cost_basis']:,.2f}, unrealized ${a['unrealized']:+,.2f}) | "
             f"EQUITY ${a['equity']:,.2f}"]
    if a.get("hold_equity") is not None:
        lines.append(f"Simply holding BTC from your start would be ${a['hold_equity']:,.2f} "
                     f"-- you are ${a['vs_hold']:+,.2f} against that.")
    if a["btc"] > 0:
        lines.append(f"Resting exits: stop {a['stop_loss'] or 'none'}, target {a['take_profit'] or 'none'}.")
    lines.append(f"Record: {perf['buys']} buys, {perf['closed_trades']} sells, "
                 f"{perf['wins']} profitable, realized ${perf['realized_pnl']:+,.2f}, "
                 f"fees ${perf['fees_paid']:,.2f}.")
    with closing(_connect(db_path)) as conn:
        recent = [dict(r) for r in conn.execute(
            "SELECT * FROM btc_lab_trades ORDER BY id DESC LIMIT 15")]
        rej = [dict(r) for r in conn.execute(
            "SELECT * FROM btc_lab_rejections ORDER BY id DESC LIMIT 3")]
    if recent:
        lines.append("Your last trades (newest first):")
        for t in recent:
            r = f", realized ${t['realized']:+,.2f}" if t["realized"] is not None else ""
            lines.append(f"  {t['at'][:16]}Z {t['side'].upper()} {t['btc']:.6f} BTC @ ${t['price']:,.2f} "
                         f"(${t['usd']:,.2f}{r}) [{t['trigger']}] {t['reason'] or ''}")
    if rej:
        lines.append("Recently refused: " + "; ".join(f"{r['reason']}" for r in rej))

    lines.append("\n--- THE BTC CHART (UTC; open high low close, volume where shown) ---")
    weekly = chart_history.load(db_path, CODE, "1w", limit=106)
    daily = chart_history.load(db_path, CODE, "1d", limit=731)
    h4 = chart_history.load(db_path, CODE, "4h", limit=180)
    lines.append(f"WEEKLY, {len(weekly)} weeks (week starting):")
    lines.append(_rows(weekly, lambda c: f"{_day(c['ts'])} {c['open']:.0f} {c['high']:.0f} "
                                         f"{c['low']:.0f} {c['close']:.0f}"))
    lines.append(f"DAILY, {len(daily)} days:")
    lines.append(_rows(daily, lambda c: f"{_day(c['ts'])} {c['open']:.0f} {c['high']:.0f} "
                                        f"{c['low']:.0f} {c['close']:.0f}"))
    lines.append(f"4-HOUR, last {len(h4)} candles:")
    lines.append(_rows(h4, lambda c: f"{_hour(c['ts'])} {c['open']:.0f} {c['high']:.0f} "
                                     f"{c['low']:.0f} {c['close']:.0f} v{c['volume']:.0f}"))
    for bucket, limit, label in (("1h", 72, "1-HOUR, last 3 days"), ("15m", 96, "15-MINUTE, last 24 hours")):
        try:
            cs = technicals.candles(db_path, CODE, bucket, limit)
        except Exception as e:  # noqa: BLE001
            lines.append(f"{label}: unavailable ({type(e).__name__})")
            continue
        lines.append(f"{label} ({len(cs)} candles):")
        lines.append(_rows(cs, lambda c: f"{str(c['at'])[5:16].replace('T', ' ')} "
                                         f"{c['open']:.0f} {c['high']:.0f} {c['low']:.0f} {c['close']:.0f}"))
    lines.append(ORDER_INSTRUCTIONS)
    return "\n".join(lines)


ORDER_INSTRUCTIONS = """
--- HOW TO TRADE ---
You trade one thing: BTC against cash. Long only -- you either hold BTC or you don't. Every
fill is at the live price and costs a 0.1% fee. Trade as often as you like, any size.
End your reply with a fenced ```orders block (omit it, or send an empty list, to do nothing):

```orders
{"orders": [
  {"side": "buy", "usd": 400, "reason": "one line: the pattern you are acting on"},
  {"side": "sell", "btc": "all", "reason": "..."},
  {"side": "levels", "stop_loss": 81500, "take_profit": 86000, "reason": "..."}
]}
```
  * buy: `usd` is the dollars to spend (or "all").
  * sell: `btc` is an amount or "all" (or give `usd` to sell that much).
  * levels: OPTIONAL resting exits for what you hold. Between your runs the system checks
    every minute and sells everything if price crosses one. Use null to clear one. Selling
    everything clears both.
You run on a schedule and see only what is in this briefing. There are no other inputs --
no news, no other coins, no indicators. The candles are the whole game.
"""


def apply_orders(db_path: str, output: str) -> tuple[str, dict | None]:
    """staff.assign's hook: fill the employee's orders and report what really happened."""
    try:
        orders = parse_orders(output)
        if not orders:
            return "", None
        result = execute_orders(db_path, orders)
    except Exception as e:
        logger.exception("BTC lab order execution failed")
        return f"\n\n[EXECUTION FAILED: {type(e).__name__}: {e} -- nothing was filled]", None
    lines = ["\n\n--- EXECUTION REPORT (by the ledger, not the employee) ---"]
    for f in result["fills"]:
        if f["side"] == "levels":
            lines.append(f"LEVELS SET stop {f['stop_loss']} target {f['take_profit']}")
            continue
        r = f" realized ${f['realized']:+,.2f}" if "realized" in f else ""
        lines.append(f"FILLED {f['side']} {f['qty']:.8f} BTC @ ${f['price']:,.2f} = ${f['usd']:,.2f} "
                     f"(fee ${f['fee']:.2f}){r}")
    for r in result["rejections"]:
        lines.append(f"REFUSED {json.dumps(r['order'])[:120]}: {r['reason']}")
    acct = result["account"]
    lines.append(f"Equity ${acct['equity']:,.2f} | cash ${acct['cash']:,.2f} | BTC {acct['btc']:.8f}")
    return "\n".join(lines), result
