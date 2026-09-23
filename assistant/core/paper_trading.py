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
from datetime import datetime, timedelta, timezone

# Taker fee per side. Roughly a retail exchange's rate; the point is that it is not zero.
DEFAULT_FEE_PCT = 0.10
DEFAULT_STARTING_CASH = 10_000.0

# A single order may not exceed this share of total equity. The simulation is meant to
# show whether the strategy reads the market, not whether one all-in bet happened to land.
#
# Lowered from 25% so a full book is several names rather than three or four. At 25% the
# desk could not have run six positions even if it wanted to, and in practice it ran one:
# every closed round-trip in the ledger to 2026-09-16 was a single position opened and
# closed with nothing else on the book beside it.
MAX_ORDER_PCT_OF_EQUITY = 15.0

# And no single coin may grow past this share, however many orders build it. The per-order
# cap alone does not bound a position -- three add-ons to one ticker clear it one at a
# time and still end up as the whole book, which is exactly the concentration the slot
# count below is meant to break up.
MAX_POSITION_PCT_OF_EQUITY = 20.0

# How many coins the desk is expected to have working at once. Not enforced as a hard
# ceiling -- it is what the briefing counts free slots against, and what tells the model
# that a book of one is an under-filled book rather than a normal state of affairs.
#
# The reason it ran one at a time was never a rule saying so. It was that its screen only
# ever showed it two or three charts (see technicals.desk_briefing), so a single qualifying
# setup was a good run. With the whole tracked universe screened, several names qualifying
# at once is the ordinary case, and nothing should imply it must pick just one of them.
# More concurrent names is also more independent samples per day, which is the only way a
# desk measured on payoff ratio learns anything at this cadence.
TARGET_CONCURRENT_POSITIONS = 6

# How long after a stop-loss exit a coin is off-limits for a fresh buy. A real, confirmed
# incident: the same ticker bought, stopped out, and immediately re-bought minutes later
# on a "fresh" momentum call several times in one session -- each round-trip pays fees
# twice and re-risks capital on a thesis that just failed. A genuinely new catalyst can
# still win an exception once the window passes; this only stops the immediate whipsaw.
STOP_LOSS_COOLDOWN_HOURS = 2.0

# --- mechanical exits ---------------------------------------------------------
#
# Measured over the 92 closed round-trips of the run archived on 2026-09-13: a 42.4% win
# rate, average win +$2.39, average loss -$2.85 -- a payoff ratio of 0.84 where 1.36 was
# needed just to break even. Expectancy -$0.63 a trade, which is a guaranteed bleed no
# amount of better coin-picking fixes.
#
# The cause was not selection. 90 of those 92 exits were the model closing the position
# itself rather than a stop firing, and it held winners a median of 2.0 hours against
# 8.7 hours for losers -- it banked gains early and sat on losses hoping they came back.
# Textbook disposition effect, and it inverts the payoff ratio all on its own.
#
# So the exit decision is no longer the model's to make. It commits a stop and a target
# at entry and the position leaves on one of them, or on the clock. The model still
# chooses what to buy, when, and how much -- it just cannot snatch a winner back.
MIN_REWARD_RISK = 2.0

# A thesis that has not worked in two days is not going to be rescued by a third day of
# the model looking at it. This also bounds how long capital can sit in a position that
# is drifting sideways, neither stopping out nor reaching its target.
MAX_HOLD_HOURS = 48.0

# How close behind the current price a raised stop may sit, as a fraction of the risk the
# position originally took (entry - initial stop). A trail is only a trail if it leaves the
# trade room to breathe: without a floor, "raise the stop to a hair under spot" is just
# selling at market, which is the disposition effect wearing the trail's coat -- that is
# the abuse the 2026-09-19 freeze was reaching for, and this is the narrow version of it.
# At 0.5 the stop follows about half the original risk behind, so a normal pullback does
# not end the trade but a real reversal does.
MIN_TRAIL_RISK_FRACTION = 0.5

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
    -- The numeric exit levels set at entry, actually persisted (not just stated in prose
    -- and forgotten) -- see check_stops(), which enforces these mechanically rather than
    -- waiting on the model to notice and re-decide every cycle. NULL means no committed
    -- level yet; a position can hold either, both, or neither.
    stop_loss REAL,
    take_profit REAL,
    -- The stop as first committed, never revised. stop_loss above may ratchet UP behind a
    -- winner; this keeps the original risk (entry - initial_stop) available afterward, so
    -- the minimum trail distance is measured against the risk actually taken rather than
    -- against an already-tightened stop, which would let the trail creep to spot.
    initial_stop_loss REAL,
    -- When this position was FIRST opened (adding to it does not reset this), so a
    -- maximum hold can be enforced. A thesis that has not worked in two days is not
    -- going to be rescued by the model staring at it for a third.
    opened_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, code)
);

-- Every accepted stop raise. Position rows are overwritten in place, so without this the
-- trail leaves no trace: an exit off a raised stop would look exactly like an exit off the
-- stop set at entry, and that ambiguity is precisely what got the trail misread as the
-- disposition effect and removed. Refusals land in paper_rejections as usual.
CREATE TABLE IF NOT EXISTS paper_stop_raises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES paper_accounts(id),
    code TEXT NOT NULL,
    from_stop REAL,
    to_stop REAL NOT NULL,
    price REAL NOT NULL,
    reason TEXT,
    staff_key TEXT,
    at TEXT NOT NULL
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
    -- Set on a sell only, by comparing the fill price against the position's own stored
    -- stop_loss/take_profit at the moment of the sell (see _classify_exit) -- never
    -- parsed from prose. 'stop_loss' | 'take_profit' | 'timeout' | 'discretionary' |
    -- NULL (buys). 'discretionary' is now only reachable through an explicit
    -- allow_exit call, since the model can no longer close a position itself.
    -- What the re-entry cooldown (see execute_orders' buy path) actually keys off.
    exit_kind TEXT,
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
next run. You may place SEVERAL orders in one block, and normally should.

```orders
{{"orders": [
  {{"side": "buy", "code": "SOL", "usd": 70, "stop_loss": 130.0, "take_profit": 220.0, "reason": "why, in one line"}},
  {{"side": "buy", "code": "ARB", "usd": 70, "stop_loss": 0.148, "take_profit": 0.191, "reason": "why, in one line"}}
]}}
```

YOU RUN A BOOK OF ABOUT {target_positions} NAMES, NOT ONE TRADE AT A TIME. Your briefing
counts your free slots every run. A free slot is capital doing nothing, and a book of one
position is an under-filled book, not a cautious one -- the risk that matters here is
concentration, and {target_positions} independent positions carry less of it than one
position four times the size. If four setups on your screen each clear the bar, take four.
Assess every one on its own merits; do not rank them against each other and keep only the
best, because you are not choosing one trade, you are filling slots.

The bar itself does not move for any of this. It is the same bar, applied more often --
volume comes from looking at more charts, never from lowering it.

YOU ONLY DECIDE ENTRIES. You cannot close a position -- there is no sell you can place.
A position leaves on the stop_loss or the take_profit you committed when you opened it,
or automatically after {max_hold_hours:g}h. Set the two numbers you actually mean: the
target is fixed from that moment and cannot be pulled in, because taking profits early is
what lost the money the first time.

The stop is different. It may be raised -- never lowered -- behind a position that is
working, with a `raise_stop` order:

  {{"side": "raise_stop", "code": "ARB", "stop_loss": 0.163, "reason": "why, in one line"}}

That costs no fee and does not touch your target or your size; it only moves risk. Two
limits: a stop only ever moves up, and a raised stop must stay at least
{min_trail:g}x the original risk (entry minus your first stop) below the current price.
Raising it to just under spot is not a trail, it is selling at market, and it is refused.

Why, in the desk's own numbers. Choosing your own exits freely lost money: over 92 closed
round-trips you won 42.4% of the time with an average win of +$2.39 against an average
loss of -$2.85, because you held winners a median of 2.0 hours and losers 8.7 hours.
But trailing the stop while the target stayed fixed is the opposite trade and it worked:
over 2026-09-17/18 you won 78% of 54 round-trips for +$61.83, and 30 of your 44 stop exits
closed ABOVE their entry -- losers cut to scratches, winners left alone. When the stop was
frozen as well for two days, the win rate fell to 44% and the book went flat. So: let the
target run, and walk the stop up behind it.

{current_performance}

Rules enforced in code, not by you:
  * Buys are sized in `usd`. No single order may exceed {max_pct}% of total equity --
    that is roughly one slot, and it is the size to work in.
  * No single coin may exceed {max_position_pct}% of equity in total, add-ons included.
    Capital that cannot go into one name should go into another, not into a bigger bet.
  * You cannot spend cash you do not have.
  * Fills use the cached price, not a price you state. Do not predict your fill.
  * Every buy MUST set both `stop_loss` and `take_profit` as real numeric prices (not a
    percentage, not "later"). A buy missing either is refused.
  * `stop_loss` must be below the fill price and `take_profit` above it. A stop above
    your entry closes the position the instant it opens.
  * The target must be at least {min_rr:g}x the distance to the stop. That floor was set
    for a 42% win rate; your actual win rate is in the current-performance line above, and
    if it is well below that, {min_rr:g}:1 is a floor the ratchet has to make up the rest
    of, not a number that alone guarantees a positive book. If a trade is not worth
    {min_rr:g}:1 to you, it is not worth taking -- that is the trade-off, and passing on
    that one is a perfectly good answer.
  * Adding to a position INHERITS its target, which cannot change; an add-on restating a
    different target is refused. It may state a HIGHER stop (the same trail as above, on
    the same terms), never a lower one. Use `raise_stop` rather than a token add-on when
    all you want is the stop moved -- that is what it is for, and it pays no fee.
    (Adding also does NOT restart its {max_hold_hours:g}h clock.)
  * A coin stopped out cannot be re-bought for {cooldown_hours:g}h -- that failed thesis
    needs to cool off, not get re-entered on the next momentum call.
  * Churn costs {fee_pct}% per side. That is an argument against trading the same coin
    repeatedly, not against holding several different ones.

If nothing on the screen clears the bar, an empty list is the right answer and you should
say so in one line. But check the whole screened list before concluding that -- it is
built from the entry patterns you are asked to look for, with the extended charts already
filtered out, so on most runs something on it is worth a position. Report your reasoning
in prose above the block.
"""


# The window _current_performance_line measures over. Short enough to reflect the current
# regime rather than smearing in a dead one (an all-time figure would still be diluted by
# the pre-mechanical-exit era years from now), long enough that a couple of quiet hours
# can't swing it -- see expectancy()'s own docstring on trusting the sample size.
CURRENT_PERFORMANCE_WINDOW_DAYS = 7


def _current_performance_line(db_path: str | None, name: str = "crypto") -> str:
    """The 42.4%/+$61.83/etc figures above are dated history -- true the day they were
    measured, silently wrong once the regime changed. This is the antidote: a
    freshly-computed line every run, from expectancy(), so the model is never reasoning
    from a stale snapshot of its own edge."""
    if db_path is None:
        return ("CURRENT MEASURED PERFORMANCE: not available for this render -- treat the "
                "history above as context, not your current odds.")
    stats = expectancy(db_path, name, days=CURRENT_PERFORMANCE_WINDOW_DAYS)
    if not stats["closed_trades"]:
        return "CURRENT MEASURED PERFORMANCE: no closed round-trips yet -- nothing to measure."
    rr = f"{stats['reward_risk_realized']:.1f}x" if stats["reward_risk_realized"] else "n/a"
    return (
        f"CURRENT MEASURED PERFORMANCE, last {CURRENT_PERFORMANCE_WINDOW_DAYS:g} days: over "
        f"your last {stats['closed_trades']} closed round-trips ({stats['span_days']:.1f} "
        f"days), {stats['win_rate_pct']:g}% win rate, average win +${stats['avg_win']:.2f} "
        f"against average loss ${stats['avg_loss']:.2f} (realized payoff {rr}), expectancy "
        f"${stats['expectancy_per_trade']:.3f}/trade, projected ${stats['projected_daily_pnl']:.2f}/day "
        f"at current frequency. That is what to trust over the history above if the two "
        f"disagree -- the history explains why the rules exist, this is whether they are "
        f"still working."
    )


def render_order_instructions(db_path: str | None = None) -> str:
    """ORDER_INSTRUCTIONS with this module's own limits filled in.

    The caller used to assemble these seven keyword arguments itself, and so did two
    tests, so adding one placeholder to the template broke all three at once with a
    KeyError far from the edit. The values are this module's to know; nobody else should
    have to keep a list of them in sync.

    `db_path` is optional so a caller with no live account yet (a fresh test, a dry render)
    still gets valid instructions -- just without the current-performance line, which needs
    real closed trades to say anything.
    """
    return ORDER_INSTRUCTIONS.format(
        fee_pct=DEFAULT_FEE_PCT,
        max_pct=MAX_ORDER_PCT_OF_EQUITY,
        max_position_pct=MAX_POSITION_PCT_OF_EQUITY,
        target_positions=TARGET_CONCURRENT_POSITIONS,
        cooldown_hours=STOP_LOSS_COOLDOWN_HOURS,
        min_rr=MIN_REWARD_RISK,
        min_trail=MIN_TRAIL_RISK_FRACTION,
        max_hold_hours=MAX_HOLD_HOURS,
        current_performance=_current_performance_line(db_path))


def init_paper_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Idempotent migrations for columns added after the initial CREATE TABLE IF NOT
        # EXISTS -- same pattern as db.py/market_data.py -- safe on a fresh or
        # already-populated database.
        pos_cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_positions)")}
        if "stop_loss" not in pos_cols:
            conn.execute("ALTER TABLE paper_positions ADD COLUMN stop_loss REAL")
        if "take_profit" not in pos_cols:
            conn.execute("ALTER TABLE paper_positions ADD COLUMN take_profit REAL")
        if "initial_stop_loss" not in pos_cols:
            conn.execute("ALTER TABLE paper_positions ADD COLUMN initial_stop_loss REAL")
            # A position already open when the ratchet shipped has only its current stop to
            # go on. Seeding it as the initial one is the conservative read: it understates
            # the original risk, so the minimum trail it must keep is the tighter of the two.
            conn.execute("UPDATE paper_positions SET initial_stop_loss = stop_loss "
                         "WHERE initial_stop_loss IS NULL")
        if "opened_at" not in pos_cols:
            conn.execute("ALTER TABLE paper_positions ADD COLUMN opened_at TEXT")
            # Backfill from updated_at rather than leaving NULL: a position already open
            # when this shipped would otherwise have no age at all, and the choice is
            # between "never times out" and "times out immediately". updated_at is the
            # last time it was touched, which for an untouched position IS its open time.
            conn.execute("UPDATE paper_positions SET opened_at = updated_at "
                         "WHERE opened_at IS NULL")
        trade_cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)")}
        if "exit_kind" not in trade_cols:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN exit_kind TEXT")
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_level(value) -> float | None:
    """A stop_loss/take_profit value from an order, or None if absent/garbage -- never
    raises, since a malformed level should just mean "no committed level" rather than
    failing the whole order over one bad field."""
    if value is None:
        return None
    try:
        level = float(value)
    except (TypeError, ValueError):
        return None
    return level if level > 0 else None


def _trail_ceiling(price: float, entry: float, initial_stop: float | None,
                   committed_stop: float | None) -> float:
    """The highest a stop may be raised to right now: far enough under the price to still
    be a stop rather than a market sell.

    The gap is a fraction of the risk the trade ORIGINALLY took (entry - initial stop), not
    of the distance price has since travelled -- a winner that has run 10% should trail at
    the same respectful distance it always did, not be handed a proportionally wider one.
    Measuring off the initial stop rather than the current one also stops the ceiling
    creeping up with each raise, which would let the trail walk itself to spot.
    """
    basis = initial_stop if initial_stop is not None else committed_stop
    risk = (entry - basis) if basis is not None else 0.0
    if risk <= 0:
        return price                      # no usable risk basis: only the price itself binds
    return price - risk * MIN_TRAIL_RISK_FRACTION


def _level_moved(stated: float | None, committed: float | None) -> bool:
    """Whether an add-on's stated exit level would change the one the position committed
    when it was first opened. An omitted level (None) inherits and is never a move; a
    stated level equal to the committed one (within float tolerance) is not a move either;
    anything else is a move, in either direction. Exits are set at entry and are not
    revised -- see the buy path in execute_orders for why that door had to be closed."""
    if stated is None:
        return False
    if committed is None:
        return True
    return abs(stated - committed) > abs(committed) * 1e-6 + 1e-12


def _fmt_level(value) -> str:
    return "none" if value is None else f"${value:,.6g}"


def _held_hours(opened_at) -> float | None:
    """How long a position has been open, or None if it predates the opened_at column
    and was never backfilled -- in which case the maximum hold simply does not apply,
    rather than a missing timestamp being read as "infinitely old" and force-closing it."""
    opened = _parse_at(opened_at) if opened_at else None
    if opened is None:
        return None
    return (datetime.now(timezone.utc) - opened).total_seconds() / 3600


def _classify_exit(price: float, pos, override: str | None = None) -> str:
    """Whether a sell at `price` was a stop-loss, a take-profit, or a discretionary
    close -- from the position's own persisted levels, never from prose. A position with
    both levels set and a price that (due to a gap) cleared both in one tick is called a
    stop-loss: preserving capital is the one of the two that actually mattered."""
    # A time exit is the one kind that cannot be read off the price: the position left
    # because the clock ran out, at whatever price that happened to be. The caller that
    # forced it says so, rather than this guessing from a price that cleared no level.
    if override:
        return override
    stop_loss = pos["stop_loss"] if "stop_loss" in pos.keys() else None
    take_profit = pos["take_profit"] if "take_profit" in pos.keys() else None
    if stop_loss is not None and price <= stop_loss:
        return "stop_loss"
    if take_profit is not None and price >= take_profit:
        return "take_profit"
    return "discretionary"


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
                "stop_loss": r["stop_loss"], "take_profit": r["take_profit"],
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


def book_slots(db_path: str, name: str = "crypto") -> dict:
    """How full the book is, in names rather than dollars.

    Dollars were always in the briefing; names never were, and "one position, 96% cash"
    reads as a fully-deployed book if you only look at the P&L line. This is what the
    desk counts its free slots against.
    """
    snap = portfolio(db_path, name)
    open_names = len(snap["positions"])
    free = max(0, TARGET_CONCURRENT_POSITIONS - open_names)
    slot = snap["equity"] * MAX_ORDER_PCT_OF_EQUITY / 100
    return {
        "open": open_names,
        "target": TARGET_CONCURRENT_POSITIONS,
        "free": free,
        "cash": snap["cash"],
        "equity": snap["equity"],
        "slot_size": round(slot, 2),
        # What can actually be deployed right now: free slots at a full slot each, bounded
        # by the cash on hand. A desk told it has four free slots and $6 of cash has been
        # told something useless.
        "deployable": round(min(snap["cash"], free * slot), 2),
    }


_FENCE = re.compile(r"```([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.S)


def fenced_blocks(text: str, labels: set[str]) -> list[str]:
    """Bodies of every fenced block carrying one of `labels` (empty string = unlabelled).

    Written this way after a real bug with teeth: the old pattern matched the label
    inline as ```(?:orders|json)?\\s*\\n, so a fence it did NOT recognise did not simply
    fail to match -- the scan resynchronised on that block's *closing* fence and swallowed
    the next block whole. One ```journal block above the ```orders block was enough to
    make parse_orders return nothing at all, silently, on every run: no orders, no
    rejections, no error, a trading desk that had quietly stopped trading.

    So: recognise every fence as a fence, then filter by label. An unknown language tag is
    skipped, not treated as the absence of a fence.
    """
    if not text:
        return []
    return [body for label, body in _FENCE.findall(text) if label.lower() in labels]


def parse_orders(text: str) -> list[dict]:
    """Pull the orders block out of an employee's response.

    Tolerant about the fence label and about a model that emits a bare JSON object, but
    never guesses at intent: anything unparseable yields no orders, because inventing a
    trade from ambiguous text is far worse than missing one.
    """
    if not text:
        return []
    blocks = fenced_blocks(text, {"", "orders", "json"})
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
                   staff_key: str | None = None, allow_exit: bool = False) -> dict:
    """Validate and fill proposed orders. Returns fills and rejections.

    Every rejection carries a reason, and both are persisted: the next run's briefing
    tells the employee what actually happened, which is the only way a simulated trader
    can learn that its sizing is wrong.

    `allow_exit` is what separates a proposal from an enforcement. Exits are mechanical
    (see MIN_REWARD_RISK's note) so a model-proposed sell is refused; only check_stops(),
    which fills a position's own committed levels, passes True. It is a parameter rather
    than a check on staff_key because a caller's authority should be something it states
    at the call site, not something inferred from a string it happens to be labelled with.
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
            if side not in ("buy", "sell", "raise_stop") or not code:
                reject(order, "order needs a side of buy, sell or raise_stop, and a coin code")
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

            if side == "raise_stop":
                # Moving a stop UP behind a winner is the one exit revision that is not the
                # disposition effect: it cuts risk without touching the upside, since the
                # target stays exactly where it was committed. The desk earned +$61.83 over
                # 2026-09-17/18 doing this -- 30 of its 44 stop exits closed ABOVE entry --
                # and freezing it outright on 09-19 took the win rate from 78% to 44% and
                # the book to flat. It has its own side so it costs no fee and leaves no
                # phantom $0.01 "buy" in the ledger, which is how it used to be expressed.
                if pos is None or pos["qty"] <= 0:
                    reject(order, f"no {code} position whose stop could be raised")
                    continue
                stated_stop = _parse_level(order.get("stop_loss"))
                if stated_stop is None:
                    reject(order, "raise_stop needs a numeric `stop_loss` to raise the stop to")
                    continue
                committed = pos["stop_loss"]
                if committed is not None and stated_stop <= committed:
                    reject(order, f"{code} already stops at {_fmt_level(committed)}; a stop "
                                  f"only ever moves up. Giving a loser more room is what "
                                  f"cost the desk its money the first time.")
                    continue
                if stated_stop >= price:
                    reject(order, f"a stop at {_fmt_level(stated_stop)} is at or above "
                                  f"{code}'s current {_fmt_level(price)} -- that is a sell, "
                                  f"not a stop, and exits stay mechanical")
                    continue
                initial = (pos["initial_stop_loss"]
                           if "initial_stop_loss" in pos.keys() else None)
                ceiling = _trail_ceiling(price, pos["avg_cost"], initial, committed)
                if stated_stop > ceiling:
                    reject(order, f"{_fmt_level(stated_stop)} trails {code} too close to its "
                                  f"{_fmt_level(price)}: a raised stop must stay at least "
                                  f"{MIN_TRAIL_RISK_FRACTION:g}x the original risk back, so "
                                  f"{_fmt_level(ceiling)} is as high as it goes right now. "
                                  f"Pinning the stop under spot to bank a gain early is the "
                                  f"disposition effect, not a trail.")
                    continue
                conn.execute("UPDATE paper_positions SET stop_loss = ?, updated_at = ? "
                             "WHERE account_id = ? AND code = ?",
                             (stated_stop, now, acct["id"], code))
                conn.execute(
                    """INSERT INTO paper_stop_raises
                           (account_id, code, from_stop, to_stop, price, reason, staff_key, at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (acct["id"], code, committed, stated_stop, price, reason, staff_key, now))
                conn.commit()
                fills.append({"side": "raise_stop", "code": code, "from_stop": committed,
                              "to_stop": stated_stop, "price": price, "reason": reason})
                continue

            if side == "buy":
                last_stop_out = conn.execute(
                    """SELECT at FROM paper_trades WHERE account_id = ? AND code = ?
                           AND side = 'sell' AND exit_kind = 'stop_loss'
                           ORDER BY id DESC LIMIT 1""",
                    (acct["id"], code)).fetchone()
                if last_stop_out is not None:
                    stopped_at = _parse_at(last_stop_out["at"])
                    elapsed_h = ((datetime.now(timezone.utc) - stopped_at).total_seconds() / 3600
                                 if stopped_at else STOP_LOSS_COOLDOWN_HOURS)
                    if elapsed_h < STOP_LOSS_COOLDOWN_HOURS:
                        remaining_min = round((STOP_LOSS_COOLDOWN_HOURS - elapsed_h) * 60)
                        reject(order, f"{code} was stopped out {elapsed_h * 60:.0f} min ago; "
                                      f"re-entry cooldown active for {remaining_min} more minute(s)")
                        continue

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
                                  f"per-order cap of ${cap:,.2f} -- the book is meant to "
                                  f"run about {TARGET_CONCURRENT_POSITIONS} names at once, "
                                  f"so size for a slot rather than for the whole account")
                    continue
                # Existing exposure counts toward the per-coin cap, so a position cannot
                # be walked past it one compliant add-on at a time.
                existing = (pos["qty"] * price) if pos is not None else 0.0
                pos_cap = equity * MAX_POSITION_PCT_OF_EQUITY / 100
                if existing + usd > pos_cap + 1e-9:
                    reject(order, f"{code} would reach ${existing + usd:,.2f}, past the "
                                  f"{MAX_POSITION_PCT_OF_EQUITY}% per-coin cap of "
                                  f"${pos_cap:,.2f} (already holding ${existing:,.2f}) -- "
                                  f"put the capital into a different name instead")
                    continue
                fee = usd * fee_pct / 100
                if usd + fee > cash + 1e-9:
                    reject(order, f"insufficient cash: need ${usd + fee:,.2f}, have ${cash:,.2f}")
                    continue
                # The TARGET is committed when a position is first opened and is immutable
                # afterward; the STOP may ratchet up. A new entry states both here; an
                # add-on inherits the target and may only tighten the stop.
                #
                # This was briefly a total freeze, on the reading that raising a stop under
                # a winner was the disposition effect returning through the one door left
                # open -- token $0.01 add-ons "restating" a tighter stop, 53% of all buys,
                # with the average win falling from the +$4.40 a real target pays toward
                # +$1.65. The truncation was real, but the conclusion was backwards, and
                # the ledger settled it: under the trail (2026-09-17/18) the desk won 78%
                # of 54 round-trips for +$61.83, with 30 of 44 stop exits closing ABOVE
                # entry. Frozen (09-19/20) it won 44% and made $1.45. Expectancy per trade
                # was +$1.14 trailing against +$0.08 frozen -- a smaller average win on far
                # more winners, which is the trade the arithmetic wants.
                #
                # So the asymmetry is the rule: pulling a target in, or widening a stop,
                # are the two disposition-effect moves and both stay refused. Raising a
                # stop is neither -- it cuts risk and leaves the upside alone -- and it
                # belongs to `raise_stop` above, which needs no fee and no phantom buy.
                # An omitted or unchanged level inherits.
                if pos is not None:
                    stated_stop = _parse_level(order.get("stop_loss"))
                    stated_take = _parse_level(order.get("take_profit"))
                    if _level_moved(stated_take, pos["take_profit"]):
                        reject(order, f"{code} already targets "
                                      f"{_fmt_level(pos['take_profit'])} and an add-on cannot "
                                      f"move it -- pulling a target in to bank early is the "
                                      f"disposition effect that cost the desk its edge. Raise "
                                      f"the stop with a `raise_stop` order instead; the "
                                      f"upside stays where you committed it.")
                        continue
                    # A stop stated LOWER than the committed one is a loser being given more
                    # room, and stays refused. A stop stated higher is the trail, and is
                    # allowed on the same terms as a raise_stop order -- an add-on placed
                    # after the stop has already been trailed should not have to pretend it
                    # has not been.
                    stop_loss = pos["stop_loss"]
                    take_profit = pos["take_profit"]
                    if _level_moved(stated_stop, pos["stop_loss"]):
                        initial = (pos["initial_stop_loss"]
                                   if "initial_stop_loss" in pos.keys() else None)
                        ceiling = _trail_ceiling(price, pos["avg_cost"], initial, pos["stop_loss"])
                        if (pos["stop_loss"] is not None and stated_stop < pos["stop_loss"]):
                            reject(order, f"{code} already stops at "
                                          f"{_fmt_level(pos['stop_loss'])}; an add-on cannot "
                                          f"widen it. Letting a loser run is what lost the "
                                          f"money -- size the add-on or let it stop out.")
                            continue
                        if stated_stop >= price or stated_stop > ceiling:
                            reject(order, f"{_fmt_level(stated_stop)} trails {code} too close "
                                          f"to its {_fmt_level(price)}; "
                                          f"{_fmt_level(ceiling)} is as high as the stop goes "
                                          f"right now")
                            continue
                        stop_loss = stated_stop
                else:
                    stop_loss = _parse_level(order.get("stop_loss"))
                    take_profit = _parse_level(order.get("take_profit"))

                # Every position here is LONG, so a coherent plan is
                # stop_loss < fill price < take_profit. _parse_level only ever checked
                # "is it a positive number", which let a stop ABOVE the entry through --
                # and check_stops() then closed the position on its very next tick, at a
                # price that had not moved, for the cost of two fees.
                #
                # This actually happened: TAO was bought at $235.13 with a stop of $250
                # and was stopped out in the same minute (2026-09-14T07:52), turning a
                # thesis the analyst had researched into a $0.20 round-trip to nowhere.
                #
                # Refused rather than silently repaired: dropping the bad level would
                # leave the position running with no stop at all, and quietly rewriting
                # it to something "sensible" would invent a risk decision the model never
                # made. A rejection is also the only one of the three the model is told
                # about -- rejections are counted back to it in its own briefing.
                if stop_loss is not None and stop_loss >= price:
                    reject(order, f"stop_loss ${stop_loss:,.6g} is at or above the "
                                  f"${price:,.6g} fill price -- a long's stop must sit "
                                  f"below it, or the position is closed the moment it opens")
                    continue
                if take_profit is not None and take_profit <= price:
                    reject(order, f"take_profit ${take_profit:,.6g} is at or below the "
                                  f"${price:,.6g} fill price -- a long's target must sit "
                                  f"above it, or the position is closed the moment it opens")
                    continue

                # Exits are mechanical (see MIN_REWARD_RISK), so BOTH levels are now
                # mandatory rather than "one or the other": a position the model cannot
                # close itself, with only half a plan, has no defined way out on one side.
                if stop_loss is None or take_profit is None:
                    missing = "stop_loss" if stop_loss is None else "take_profit"
                    reject(order, f"every buy needs both stop_loss and take_profit as "
                                  f"numeric prices -- {missing} is missing, and exits are "
                                  f"mechanical now, so an open position with no committed "
                                  f"level on one side has no way out on that side")
                    continue

                # The payoff ratio is set here, at entry, or it is not set at all. At the
                # desk's measured 42.4% win rate a 2:1 target turns a losing book into a
                # winning one on arithmetic alone -- and unlike a win rate, this is
                # something the model can actually be held to.
                risk, reward = price - stop_loss, take_profit - price
                if reward < risk * MIN_REWARD_RISK:
                    reject(order, f"target is only {reward / risk:.2f}x the risk "
                                  f"(${reward:,.6g} up vs ${risk:,.6g} down); "
                                  f"{MIN_REWARD_RISK:g}x is the minimum -- either move the "
                                  f"target out or bring the stop closer, but a trade you "
                                  f"win 42% of the time has to pay more than it risks")
                    continue

                # Cash moves only once the order is known to be fillable -- every refusal
                # above this line must leave the balance untouched.
                qty = usd / price
                new_qty = (pos["qty"] if pos else 0.0) + qty
                # Fees fold into cost basis, so realised P&L is the round-trip result.
                new_cost = ((pos["qty"] * pos["avg_cost"] if pos else 0.0) + usd + fee) / new_qty
                cash -= usd + fee

                # Adding to a position must NOT restart its clock, or a position could be
                # kept alive past the maximum hold indefinitely by topping it up.
                opened_at = pos["opened_at"] if pos is not None and pos["opened_at"] else now
                # The original risk belongs to the position, not to the latest order: an
                # add-on must not reset it, or the trail's minimum distance would be
                # remeasured against an already-tightened stop and could walk to spot.
                initial_stop = None
                if pos is not None and "initial_stop_loss" in pos.keys():
                    initial_stop = pos["initial_stop_loss"]
                if initial_stop is None:
                    initial_stop = pos["stop_loss"] if pos is not None else stop_loss
                conn.execute(
                    """INSERT INTO paper_positions (account_id, code, qty, avg_cost,
                                                     stop_loss, take_profit, initial_stop_loss,
                                                     opened_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(account_id, code) DO UPDATE SET
                           qty = excluded.qty, avg_cost = excluded.avg_cost,
                           stop_loss = excluded.stop_loss, take_profit = excluded.take_profit,
                           initial_stop_loss = excluded.initial_stop_loss,
                           opened_at = excluded.opened_at,
                           updated_at = excluded.updated_at""",
                    (acct["id"], code, new_qty, new_cost, stop_loss, take_profit, initial_stop,
                     opened_at, now))
                conn.execute(
                    """INSERT INTO paper_trades (account_id, code, side, qty, price, fee, gross,
                                                 realized, cash_after, reason, staff_key,
                                                 quote_age_sec, at)
                       VALUES (?,?,'buy',?,?,?,?,NULL,?,?,?,?,?)""",
                    (acct["id"], code, qty, price, fee, usd, cash, reason, staff_key, age, now))
                fills.append({"side": "buy", "code": code, "qty": qty, "price": price,
                              "usd": round(usd, 2), "fee": round(fee, 2), "reason": reason})

            else:
                if not allow_exit:
                    reject(order, "exits are mechanical: a position closes on the "
                                  "stop_loss or take_profit you committed at entry, or "
                                  "after {:g}h, and you cannot close one early. Measured "
                                  "over 92 closed trades, choosing your own exits meant "
                                  "holding winners 2.0h and losers 8.7h, which is what "
                                  "made the book lose money. Set the levels you actually "
                                  "mean when you open the position."
                                  .format(MAX_HOLD_HOURS))
                    continue
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
                # Classified from the position's own stored levels vs. the actual fill
                # price -- never parsed from prose -- so the re-entry cooldown above has
                # something real to key off regardless of whether this sell was the
                # model's own discretionary call or check_stops' automatic one.
                exit_kind = _classify_exit(price, pos, order.get("exit_kind"))
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
                                                 quote_age_sec, exit_kind, at)
                       VALUES (?,?,'sell',?,?,?,?,?,?,?,?,?,?,?)""",
                    (acct["id"], code, qty, price, fee, gross, realized, cash, reason,
                     staff_key, age, exit_kind, now))
                fills.append({"side": "sell", "code": code, "qty": qty, "price": price,
                              "usd": round(gross, 2), "fee": round(fee, 2),
                              "realized": round(realized, 2), "reason": reason})

            conn.execute("UPDATE paper_accounts SET cash = ?, realized_pnl = ?, updated_at = ? "
                         "WHERE id = ?", (cash, realized_total, now, acct["id"]))
            conn.commit()

    return {"fills": fills, "rejections": rejects, "portfolio": portfolio(db_path, name)}


def check_stops(db_path: str, name: str = "crypto") -> dict:
    """Mechanically enforces every open position's stored stop_loss/take_profit against
    the live price cache -- the actual fix for "lack of numeric exit discipline": the
    model states a level once, at entry, and this closes the position the moment it's
    hit, rather than waiting for the next 5-minute cycle to notice (or not) via a
    judgment call reconstructed from scratch. Meant to run on a short interval
    (scheduler.py) relative to the trading cadence.

    A breached position becomes a real, full-size sell order routed through
    execute_orders() -- the exact same validation/fill/fee/exit-classification path a
    model-proposed sell uses, not a second write path that could drift from it.
    """
    acct = ensure_account(db_path, name)
    with closing(_connect(db_path)) as conn:
        # Every open position, not only those carrying a level: since exits became
        # mechanical the maximum hold applies to all of them, and a position that somehow
        # has neither level would otherwise be the one thing that could never be closed.
        positions = conn.execute(
            "SELECT * FROM paper_positions WHERE account_id = ? AND qty > 0",
            (acct["id"],)).fetchall()
        if not positions:
            return {"fills": [], "rejections": [], "portfolio": portfolio(db_path, name)}
        marks = _prices(conn, [p["code"] for p in positions])

    orders = []
    for p in positions:
        mark = marks.get(p["code"])
        if mark is None:
            continue  # can't check what can't be priced; execute_orders would reject it anyway
        price = mark["price"]
        if p["stop_loss"] is not None and price <= p["stop_loss"]:
            orders.append({"side": "sell", "code": p["code"], "qty": "all",
                           "reason": f"Automatic stop-loss: price ${price:,.6g} <= stop ${p['stop_loss']:,.6g}"})
        elif p["take_profit"] is not None and price >= p["take_profit"]:
            orders.append({"side": "sell", "code": p["code"], "qty": "all",
                           "reason": f"Automatic take-profit: price ${price:,.6g} >= target ${p['take_profit']:,.6g}"})
        else:
            held_h = _held_hours(p["opened_at"] if "opened_at" in p.keys() else None)
            if held_h is not None and held_h >= MAX_HOLD_HOURS:
                orders.append({"side": "sell", "code": p["code"], "qty": "all",
                               "exit_kind": "timeout",
                               "reason": f"Automatic time exit: held {held_h:.1f}h, "
                                         f"past the {MAX_HOLD_HOURS:g}h maximum"})

    if not orders:
        return {"fills": [], "rejections": [], "portfolio": portfolio(db_path, name)}
    return execute_orders(db_path, orders, name=name, staff_key="system:check_stops",
                          allow_exit=True)


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


def per_coin_performance(db_path: str, name: str = "crypto") -> list[dict]:
    """Realised outcome per coin, worst first.

    The one question this ledger could never answer, despite holding every row needed to:
    "have I lost money on this ticker before?" `performance()` computes a single blended
    win rate across every coin, which is exactly the number that hides a ticker the desk
    has lost on four times running -- and the trade log shows that happening, with the
    same codes re-bought days apart on a fresh momentum call.

    Grouped over closed round-trips only (a sell carrying a realised figure). An open
    position has no outcome yet, and counting its paper mark here would let an unrealised
    loss masquerade as a track record.
    """
    acct = ensure_account(db_path, name)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT code,
                      COUNT(*) AS closed,
                      SUM(CASE WHEN realized > 0 THEN 1 ELSE 0 END) AS wins,
                      COALESCE(SUM(realized), 0) AS realized
                 FROM paper_trades
                WHERE account_id = ? AND side = 'sell' AND realized IS NOT NULL
                GROUP BY code""", (acct["id"],)).fetchall()
        # Fees are charged on both sides, so they are summed over every trade in the code,
        # not just the closing ones -- the cost of churning a ticker is half the story of
        # why it lost money.
        fees = {r["code"]: r["fees"] for r in conn.execute(
            """SELECT code, COALESCE(SUM(fee), 0) AS fees FROM paper_trades
                WHERE account_id = ? GROUP BY code""", (acct["id"],))}

    out = []
    for r in rows:
        closed, wins = r["closed"], r["wins"] or 0
        out.append({
            "code": r["code"], "closed_trades": closed, "wins": wins,
            "losses": closed - wins, "realized": round(r["realized"], 2),
            "fees_paid": round(fees.get(r["code"], 0.0), 2),
            "win_rate_pct": round(wins / closed * 100, 1) if closed else None,
        })
    out.sort(key=lambda c: c["realized"])
    return out


def last_buy_at(db_path: str, code: str, name: str = "crypto") -> str | None:
    """When the most recent buy of `code` filled, or None. Used only to link a closing
    journal entry back to the day the position was opened."""
    acct = ensure_account(db_path, name)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT at FROM paper_trades WHERE account_id = ? AND code = ? AND side = 'buy'
                ORDER BY id DESC LIMIT 1""", (acct["id"], code)).fetchone()
    return row["at"] if row else None


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


def expectancy(db_path: str, name: str = "crypto", days: int | None = None) -> dict:
    """Win rate, average win/loss, and per-trade expectancy from actual closed round-trips
    -- the number every claim about the desk's edge should be checked against instead of
    trusted from memory.

    Before this, every number ever quoted about the desk's performance (the 42.4%/+$2.39/
    -$2.85 figures baked into ORDER_INSTRUCTIONS, the "+$61.83 over 09-17/18" one) was
    hand-run SQL pasted into a prompt or a commit message once and left there -- accurate
    the day it was measured, silently wrong the moment the regime it described changed.
    This is meant to be called, not remembered.

    A "win" is realized > 0, same convention as performance() above -- NOT
    exit_kind == 'take_profit'. The stop ratchet routinely turns a stop-loss exit into a
    profitable one (the stop rose above entry before a pullback hit it), and counting only
    take_profit exits as wins would undercount exactly the behaviour the ratchet exists to
    produce.

    `days` narrows to a trailing window (e.g. 7 for "the last week"); None uses every
    closed round-trip the account has.
    """
    acct = ensure_account(db_path, name)
    query = ("SELECT realized, at FROM paper_trades WHERE account_id = ? AND side = 'sell' "
             "AND realized IS NOT NULL")
    params: list = [acct["id"]]
    if days:
        query += " AND at >= ?"
        params.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
    with closing(_connect(db_path)) as conn:
        rows = [dict(r) for r in conn.execute(query, params)]

    empty = {
        "closed_trades": 0, "win_rate_pct": None, "avg_win": None, "avg_loss": None,
        "reward_risk_realized": None, "expectancy_per_trade": None,
        "trades_per_day": None, "projected_daily_pnl": None, "span_days": None,
        "first_trade_at": None, "last_trade_at": None,
    }
    if not rows:
        return empty

    wins = [r["realized"] for r in rows if r["realized"] > 0]
    losses = [r["realized"] for r in rows if r["realized"] <= 0]
    total = sum(r["realized"] for r in rows)
    first_at, last_at = min(r["at"] for r in rows), max(r["at"] for r in rows)
    # Floored at an hour so a burst of same-minute closes (e.g. a stop-loss cascade) can't
    # divide by a near-zero span and report an absurd trades-per-day figure.
    span_days = max((datetime.fromisoformat(last_at) - datetime.fromisoformat(first_at))
                     .total_seconds() / 86400, 1 / 24)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    expectancy_per_trade = total / len(rows)
    trades_per_day = len(rows) / span_days

    return {
        "closed_trades": len(rows),
        "win_rate_pct": round(len(wins) / len(rows) * 100, 1),
        "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),
        "reward_risk_realized": round(avg_win / abs(avg_loss), 2) if avg_loss else None,
        "expectancy_per_trade": round(expectancy_per_trade, 3),
        "trades_per_day": round(trades_per_day, 1),
        "projected_daily_pnl": round(expectancy_per_trade * trades_per_day, 2),
        "span_days": round(span_days, 1),
        "first_trade_at": first_at,
        "last_trade_at": last_at,
    }


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
