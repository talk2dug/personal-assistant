"""A simulated trading ledger for the paper-trading employee.

The employee cannot execute anything itself -- it holds no tools, by the same rule that
keeps every hired agent from sending mail or spending money. So it does not "place" a
trade: it ends its run with a block of proposed orders, and this module validates and
fills them against the live price cache.

That split is not just a safety formality, it makes the simulation honest:

  * Fills happen at the cached market price, not at a price the model recalled or
    rounded. A model asked to both pick a trade and state its fill price will produce a
    flattering one.
  * Cash, position size and realised P&L are computed here. Running balances are exactly
    what language models are worst at, and a paper portfolio whose arithmetic drifts is
    worse than no portfolio at all -- it reports profits that were never made.
  * An order that cannot be afforded, or a sell of a coin not held, is rejected and the
    rejection is written into the work record. The employee finds out on its next run
    that the trade did not happen, rather than reasoning onward from an imaginary fill.

Fees are charged on both sides. A zero-fee simulation systematically overstates the
returns of a strategy that trades often, which is precisely the strategy a five-minute
cadence encourages.
"""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

# Taker fee per side. Roughly a retail exchange's rate; the point is that it is not zero.
DEFAULT_FEE_PCT = 0.10
DEFAULT_STARTING_CASH = 10_000.0

# A single order may not exceed this share of total equity. The simulation is meant to
# show whether the strategy reads the market, not whether one all-in bet happened to land.
MAX_ORDER_PCT_OF_EQUITY = 25.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    starting_cash REAL NOT NULL,
    cash REAL NOT NULL,
    fee_pct REAL NOT NULL DEFAULT 0.10,
    -- Realised P&L accumulates here as positions are closed; unrealised is derived from
    -- live prices at read time rather than stored, so it can never go stale.
    realized_pnl REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    account_id INTEGER NOT NULL REFERENCES paper_accounts(id),
    code TEXT NOT NULL,
    qty REAL NOT NULL,
    -- Average cost per unit including fees paid to acquire, so realised P&L on the way
    -- out is the true round-trip result rather than the headline price difference.
    avg_cost REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, code)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES paper_accounts(id),
    code TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    gross REAL NOT NULL,
    realized REAL,
    cash_after REAL NOT NULL,
    reason TEXT,
    staff_key TEXT,
    -- How old the quote was when it filled. A fill against a stale cache is a fill
    -- against a price that no longer existed, and that has to be visible after the fact.
    quote_age_sec INTEGER,
    at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_acct ON paper_trades(account_id, id DESC);

-- Rejected orders are kept too. An employee whose orders are all being refused looks
-- identical, in the trade log, to one that decided to sit on its hands.
CREATE TABLE IF NOT EXISTS paper_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES paper_accounts(id),
    code TEXT,
    side TEXT,
    requested TEXT,
    reason TEXT NOT NULL,
    staff_key TEXT,
    at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_rej_acct ON paper_rejections(account_id, id DESC);
"""

ORDER_INSTRUCTIONS = """

--- PLACING PAPER TRADES ---
You do not execute trades. End your response with a fenced ```orders block containing
JSON, and the system will fill it against the live price cache and report back to you
next run. An empty list is a legitimate and often correct answer -- you are not required
to trade on every run, and churning costs {fee_pct}% per side.

```orders
{{"orders": [
  {{"side": "buy",  "code": "SOL", "usd": 500, "reason": "why, in one line"}},
  {{"side": "sell", "code": "DASH", "qty": "all", "reason": "why, in one line"}}
]}}
```

Rules that are enforced in code, not by you:
  * Buys are sized in `usd`; sells in `qty` (a number, or "all" to close the position).
  * You cannot spend cash you do not have, or sell a coin you do not hold.
  * No single order may exceed {max_pct}% of total equity.
  * Fills use the cached price, not a price you state. Do not predict your fill.
Report your reasoning in prose above the block. If you are not trading, say why.
"""


def init_paper_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_account(db_path: str, name: str = "crypto",
                   starting_cash: float = DEFAULT_STARTING_CASH,
                   fee_pct: float = DEFAULT_FEE_PCT) -> dict:
    init_paper_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM paper_accounts WHERE name = ?", (name,)).fetchone()
        if row is None:
            now = _now()
            conn.execute(
                """INSERT INTO paper_accounts (name, starting_cash, cash, fee_pct,
                                               created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (name, starting_cash, starting_cash, fee_pct, now, now))
            conn.commit()
            row = conn.execute("SELECT * FROM paper_accounts WHERE name = ?", (name,)).fetchone()
        return dict(row)


def _prices(conn, codes=None) -> dict:
    """Current marks from the live cache, with the age of each quote."""
    sql = "SELECT code, rate, last_seen FROM market_coins WHERE present = 1"
    args = []
    if codes:
        sql += f" AND code IN ({','.join('?' * len(codes))})"
        args = list(codes)
    out = {}
    now = datetime.now(timezone.utc)
    for r in conn.execute(sql, args):
        if not r["rate"]:
            continue
        try:
            seen = datetime.fromisoformat(r["last_seen"])
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            age = int((now - seen).total_seconds())
        except (ValueError, TypeError):
            age = None
        out[r["code"]] = {"price": float(r["rate"]), "age": age}
    return out


def portfolio(db_path: str, name: str = "crypto") -> dict:
    """Cash, holdings marked to the live cache, and equity."""
    acct = ensure_account(db_path, name)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM paper_positions WHERE account_id = ? AND qty > 0",
            (acct["id"],)).fetchall()
        marks = _prices(conn, [r["code"] for r in rows]) if rows else {}

        positions, holdings_value, stale = [], 0.0, []
        for r in rows:
            mark = marks.get(r["code"])
            price = mark["price"] if mark else None
            if price is None:
                # Delisted or dropped out of the tracked set. Valued at cost rather than
                # zero, and flagged: silently marking it to zero would invent a loss.
                stale.append(r["code"])
                value = r["qty"] * r["avg_cost"]
            else:
                value = r["qty"] * price
            cost = r["qty"] * r["avg_cost"]
            holdings_value += value
            positions.append({
                "code": r["code"], "qty": r["qty"], "avg_cost": r["avg_cost"],
                "price": price, "value": round(value, 2), "cost": round(cost, 2),
                "unrealized": round(value - cost, 2),
                "unrealized_pct": round((value - cost) / cost * 100, 2) if cost else 0.0,
                "quote_age_sec": mark["age"] if mark else None,
            })
        positions.sort(key=lambda p: p["value"], reverse=True)

        equity = acct["cash"] + holdings_value
        total_return = equity - acct["starting_cash"]
        n_trades = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE account_id = ?", (acct["id"],)).fetchone()[0]

    return {
        "account": name,
        "cash": round(acct["cash"], 2),
        "holdings_value": round(holdings_value, 2),
        "equity": round(equity, 2),
        "starting_cash": acct["starting_cash"],
        "total_return": round(total_return, 2),
        "total_return_pct": round(total_return / acct["starting_cash"] * 100, 2)
        if acct["starting_cash"] else 0.0,
        "realized_pnl": round(acct["realized_pnl"], 2),
        "unrealized_pnl": round(sum(p["unrealized"] for p in positions), 2),
        "fee_pct": acct["fee_pct"],
        "positions": positions,
        "trades": n_trades,
        "unpriced": stale,
    }


def parse_orders(text: str) -> list[dict]:
    """Pull the orders block out of an employee's response.

    Tolerant about the fence label and about a model that emits a bare JSON object, but
    never guesses at intent: anything unparseable yields no orders, because inventing a
    trade from ambiguous text is far worse than missing one.
    """
    if not text:
        return []
    blocks = re.findall(r"```(?:orders|json)?\s*\n(.*?)```", text, re.S | re.I)
    # Last block wins: a model that restates its orders puts the final answer last.
    for block in reversed(blocks):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("orders"), list):
            return [o for o in data["orders"] if isinstance(o, dict)]
        if isinstance(data, list):
            return [o for o in data if isinstance(o, dict)]
    return []


def execute_orders(db_path: str, orders: list[dict], name: str = "crypto",
                   staff_key: str | None = None) -> dict:
    """Validate and fill proposed orders. Returns fills and rejections.

    Every rejection carries a reason, and both are persisted: the next run's briefing
    tells the employee what actually happened, which is the only way a simulated trader
    can learn that its sizing is wrong.
    """
    acct = ensure_account(db_path, name)
    fills, rejects = [], []
    if not orders:
        return {"fills": fills, "rejections": rejects, "portfolio": portfolio(db_path, name)}

    with closing(_connect(db_path)) as conn:
        cash = float(acct["cash"])
        fee_pct = float(acct["fee_pct"])
        realized_total = float(acct["realized_pnl"])
        now = _now()

        def reject(order, reason):
            rejects.append({"order": order, "reason": reason})
            conn.execute(
                """INSERT INTO paper_rejections
                       (account_id, code, side, requested, reason, staff_key, at)
                   VALUES (?,?,?,?,?,?,?)""",
                (acct["id"], str(order.get("code") or "")[:20], str(order.get("side") or "")[:10],
                 json.dumps(order)[:500], reason, staff_key, now))
            # Committed here, not with the fills: a run in which every order is refused
            # reaches the end of the loop without touching the fill path, and would
            # otherwise roll back the very record that tells the employee it was refused.
            conn.commit()

        for order in orders[:20]:      # a runaway list is a bug, not a strategy
            code = str(order.get("code") or "").strip().upper()
            side = str(order.get("side") or "").strip().lower()
            reason = str(order.get("reason") or "")[:300]
            if side not in ("buy", "sell") or not code:
                reject(order, "order needs a side of buy or sell and a coin code")
                continue

            mark = _prices(conn, [code]).get(code)
            if mark is None or mark["price"] <= 0:
                reject(order, f"{code} is not in the tracked price cache, so it cannot be priced")
                continue
            price, age = mark["price"], mark["age"]

            pos = conn.execute(
                "SELECT * FROM paper_positions WHERE account_id = ? AND code = ?",
                (acct["id"], code)).fetchone()

            # Equity is recomputed per order so a sequence of orders in one run cannot
            # collectively exceed the cap by each measuring against the starting figure.
            snapshot = portfolio(db_path, name)
            equity = snapshot["equity"]

            if side == "buy":
                try:
                    usd = float(order.get("usd") if order.get("usd") is not None
                                else float(order.get("qty", 0)) * price)
                except (TypeError, ValueError):
                    reject(order, "buy needs a numeric `usd` amount")
                    continue
                if usd <= 0:
                    reject(order, "buy amount must be positive")
                    continue
                cap = equity * MAX_ORDER_PCT_OF_EQUITY / 100
                if usd > cap:
                    reject(order, f"${usd:,.2f} exceeds the {MAX_ORDER_PCT_OF_EQUITY}% "
                                  f"per-order cap of ${cap:,.2f}")
                    continue
                fee = usd * fee_pct / 100
                if usd + fee > cash + 1e-9:
                    reject(order, f"insufficient cash: need ${usd + fee:,.2f}, have ${cash:,.2f}")
                    continue
                qty = usd / price
                new_qty = (pos["qty"] if pos else 0.0) + qty
                # Fees fold into cost basis, so realised P&L is the round-trip result.
                new_cost = ((pos["qty"] * pos["avg_cost"] if pos else 0.0) + usd + fee) / new_qty
                cash -= usd + fee
                conn.execute(
                    """INSERT INTO paper_positions (account_id, code, qty, avg_cost, updated_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(account_id, code) DO UPDATE SET
                           qty = excluded.qty, avg_cost = excluded.avg_cost,
                           updated_at = excluded.updated_at""",
                    (acct["id"], code, new_qty, new_cost, now))
                conn.execute(
                    """INSERT INTO paper_trades (account_id, code, side, qty, price, fee, gross,
                                                 realized, cash_after, reason, staff_key,
                                                 quote_age_sec, at)
                       VALUES (?,?,'buy',?,?,?,?,NULL,?,?,?,?,?)""",
                    (acct["id"], code, qty, price, fee, usd, cash, reason, staff_key, age, now))
                fills.append({"side": "buy", "code": code, "qty": qty, "price": price,
                              "usd": round(usd, 2), "fee": round(fee, 2), "reason": reason})

            else:
                if pos is None or pos["qty"] <= 0:
                    reject(order, f"no {code} position to sell")
                    continue
                raw = order.get("qty", "all")
                if isinstance(raw, str) and raw.strip().lower() in ("all", "max", "everything"):
                    qty = pos["qty"]
                else:
                    try:
                        qty = float(raw)
                    except (TypeError, ValueError):
                        reject(order, "sell needs a numeric `qty` or \"all\"")
                        continue
                if qty <= 0:
                    reject(order, "sell quantity must be positive")
                    continue
                if qty > pos["qty"] + 1e-12:
                    reject(order, f"holds {pos['qty']:.8f} {code}, cannot sell {qty:.8f}")
                    continue
                gross = qty * price
                fee = gross * fee_pct / 100
                realized = gross - fee - qty * pos["avg_cost"]
                cash += gross - fee
                realized_total += realized
                remaining = pos["qty"] - qty
                if remaining <= 1e-12:
                    conn.execute("DELETE FROM paper_positions WHERE account_id = ? AND code = ?",
                                 (acct["id"], code))
                else:
                    conn.execute(
                        "UPDATE paper_positions SET qty = ?, updated_at = ? "
                        "WHERE account_id = ? AND code = ?", (remaining, now, acct["id"], code))
                conn.execute(
                    """INSERT INTO paper_trades (account_id, code, side, qty, price, fee, gross,
                                                 realized, cash_after, reason, staff_key,
                                                 quote_age_sec, at)
                       VALUES (?,?,'sell',?,?,?,?,?,?,?,?,?,?)""",
                    (acct["id"], code, qty, price, fee, gross, realized, cash, reason,
                     staff_key, age, now))
                fills.append({"side": "sell", "code": code, "qty": qty, "price": price,
                              "usd": round(gross, 2), "fee": round(fee, 2),
                              "realized": round(realized, 2), "reason": reason})

            conn.execute("UPDATE paper_accounts SET cash = ?, realized_pnl = ?, updated_at = ? "
                         "WHERE id = ?", (cash, realized_total, now, acct["id"]))
            conn.commit()

    return {"fills": fills, "rejections": rejects, "portfolio": portfolio(db_path, name)}


def recent_trades(db_path: str, name: str = "crypto", limit: int = 20) -> list[dict]:
    acct = ensure_account(db_path, name)
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM paper_trades WHERE account_id = ? ORDER BY id DESC LIMIT ?",
            (acct["id"], limit))]


def recent_rejections(db_path: str, name: str = "crypto", limit: int = 10) -> list[dict]:
    acct = ensure_account(db_path, name)
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM paper_rejections WHERE account_id = ? ORDER BY id DESC LIMIT ?",
            (acct["id"], limit))]


def performance(db_path: str, name: str = "crypto") -> dict:
    """Headline numbers, plus the win rate over closed trades."""
    snap = portfolio(db_path, name)
    acct = ensure_account(db_path, name)
    with closing(_connect(db_path)) as conn:
        closed = conn.execute(
            "SELECT realized FROM paper_trades WHERE account_id = ? AND side = 'sell' "
            "AND realized IS NOT NULL", (acct["id"],)).fetchall()
        fees = conn.execute(
            "SELECT COALESCE(SUM(fee), 0) FROM paper_trades WHERE account_id = ?",
            (acct["id"],)).fetchone()[0]
        rejected = conn.execute(
            "SELECT COUNT(*) FROM paper_rejections WHERE account_id = ?", (acct["id"],)).fetchone()[0]
    wins = [c["realized"] for c in closed if c["realized"] > 0]
    snap.update({
        "closed_trades": len(closed),
        "wins": len(wins),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "fees_paid": round(fees, 2),
        "orders_rejected": rejected,
    })
    return snap


def reset(db_path: str, name: str = "crypto",
          starting_cash: float = DEFAULT_STARTING_CASH) -> dict:
    """Wipe the account back to cash. Destructive, so it is never called automatically."""
    acct = ensure_account(db_path, name)
    now = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute("DELETE FROM paper_positions WHERE account_id = ?", (acct["id"],))
        conn.execute("DELETE FROM paper_trades WHERE account_id = ?", (acct["id"],))
        conn.execute("DELETE FROM paper_rejections WHERE account_id = ?", (acct["id"],))
        conn.execute("UPDATE paper_accounts SET cash = ?, starting_cash = ?, realized_pnl = 0, "
                     "updated_at = ? WHERE id = ?", (starting_cash, starting_cash, now, acct["id"]))
        conn.commit()
    return portfolio(db_path, name)
