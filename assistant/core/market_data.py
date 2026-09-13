"""Crypto market data: a local cache the agents read, fed by one poller.

Deliberately *not* a client the agents call directly. Two reasons, and the second matters
more than the first:

1. Budget. LiveCoinWatch gives 10,000 credits/day and one call costs one credit
   regardless of how many coins it returns — measured, not assumed. A single poller
   pulling the top 250 coins every minute costs 1,440/day (14%), while a handful of
   agents each querying ad hoc would be unpredictable and eventually noisy.

2. History. A snapshot is nearly useless for the question actually being asked ("has
   anything moved?"). Keeping our own series means an agent can ask "what changed in the
   last hour" and get an answer grounded in observations we made, rather than a single
   number it has to reason about blind.

The poller also watches *which* tokens exist. A coin appearing in the top ranks that was
not there yesterday is itself the signal — that is what "watch the available tokens" is
for, and it cannot be seen from any single response.
"""
import json
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone

BASE_URL = "https://api.livecoinwatch.com"

SCHEMA = """
-- Latest known state per coin. One row per coin, overwritten each poll -- the "what is
-- true right now" table that most questions hit.
CREATE TABLE IF NOT EXISTS market_coins (
    code TEXT PRIMARY KEY,
    name TEXT,
    rank INTEGER,
    rate REAL,
    volume REAL,
    market_cap REAL,
    liquidity REAL,
    -- LCW returns deltas as multipliers (1.02 = +2%); stored as given and converted at
    -- read time, so a change in their convention is visible rather than silently baked in.
    delta_hour REAL,
    delta_day REAL,
    delta_week REAL,
    delta_month REAL,
    all_time_high REAL,
    -- Whether the coin was in the most recent poll's set. Listing events are transitions,
    -- so they need "was it here last time", not "have we ever seen it" -- comparing
    -- against every code ever seen re-logs the same departure on every poll, forever.
    present INTEGER NOT NULL DEFAULT 1,
    -- Which poller last wrote this row. LiveCoinWatch is missing several real, large
    -- tokens at any rank; a supplemental Kraken poller (source='kraken') fills exactly
    -- those gaps. LCW's own disappeared-listing diff must only compare against rows it
    -- owns -- otherwise a Kraken-sourced row it never mentions looks like a departure on
    -- every single LCW cycle. See refresh()'s use of this column below.
    source TEXT NOT NULL DEFAULT 'livecoinwatch',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_coins_rank ON market_coins(rank);

-- Time series. Pruned aggressively: this exists to answer "what changed recently",
-- not to be a research archive, and an unbounded series on 250 coins a minute would be
-- 360k rows a day.
CREATE TABLE IF NOT EXISTS market_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    rate REAL NOT NULL,
    volume REAL,
    market_cap REAL,
    at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_history_code ON market_history(code, id DESC);
CREATE INDEX IF NOT EXISTS idx_market_history_at ON market_history(at);

-- Tokens arriving in or leaving the watched set. The event, not the state -- an agent
-- asking "what is new" wants this, and it is invisible in any single API response.
CREATE TABLE IF NOT EXISTS market_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT,
    event TEXT NOT NULL CHECK (event IN ('appeared', 'disappeared')),
    rank INTEGER,
    rate REAL,
    at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_listings_at ON market_listings(at);

-- Every poll, so credit spend and staleness are observable rather than guessed at.
CREATE TABLE IF NOT EXISTS market_polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coins INTEGER NOT NULL DEFAULT 0,
    credits_remaining INTEGER,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    took_ms INTEGER,
    at TEXT NOT NULL
);
"""


def init_market_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS won't add a column to a table that already exists
        # from an earlier version -- same idempotent migration pattern as db.py.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(market_coins)")}
        if "present" not in cols:
            conn.execute("ALTER TABLE market_coins ADD COLUMN present INTEGER NOT NULL DEFAULT 1")
        if "source" not in cols:
            conn.execute(
                "ALTER TABLE market_coins ADD COLUMN source TEXT NOT NULL DEFAULT 'livecoinwatch'")
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pct(multiplier) -> float | None:
    """LCW deltas are multipliers: 1.0237 means +2.37%. Convert once, at the edge."""
    if multiplier is None:
        return None
    try:
        return round((float(multiplier) - 1.0) * 100.0, 2)
    except (TypeError, ValueError):
        return None


class LiveCoinWatch:
    """Thin HTTP client. Every method is one credit."""

    def __init__(self, api_key: str, timeout: float = 25.0):
        self.api_key = api_key
        self.timeout = timeout

    def _post(self, path: str, payload: dict | None = None):
        req = urllib.request.Request(
            BASE_URL + path, method="POST",
            data=json.dumps(payload or {}).encode(),
            headers={"content-type": "application/json", "x-api-key": self.api_key})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def credits(self) -> dict:
        return self._post("/credits")

    def coins(self, limit: int = 250, currency: str = "USD", meta: bool = False) -> list:
        return self._post("/coins/list", {
            "currency": currency, "sort": "rank", "order": "ascending",
            "offset": 0, "limit": limit, "meta": meta})

    def coin(self, code: str, currency: str = "USD") -> dict:
        return self._post("/coins/single", {"currency": currency, "code": code, "meta": True})


KRAKEN_BASE_URL = "https://api.kraken.com/0/public"


class KrakenGapFeed:
    """Thin HTTP client for Kraken's public market-data endpoint -- no API key, no
    secret, because ticker data needs neither. Exists only to fill a handful of real,
    large tokens LiveCoinWatch does not list at any rank (see refresh_supplemental
    below). This is a completely separate code path from the credentialed CCXT client
    used for real trading (setup.py's build_ccxt_context) -- this class never touches
    an API key/secret and never places an order, it only reads public ticker prices.
    """

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def ticker(self, pairs: list[str]) -> dict:
        url = f"{KRAKEN_BASE_URL}/Ticker?pair={','.join(pairs)}"
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            return json.loads(resp.read())


# --- polling ------------------------------------------------------------------

def refresh(db_path: str, api_key: str, limit: int = 250,
            keep_history_hours: int = 72) -> dict:
    """One poll: fetch, upsert, record history, and notice listing changes.

    Returns a summary rather than raising, so a scheduled job that hits a network blip
    records the failure and tries again next tick instead of taking down the scheduler.
    """
    started = time.perf_counter()
    client = LiveCoinWatch(api_key)
    try:
        # meta=True is what makes `rank` and `name` present in the response, and it
        # costs the same single credit as without -- measured, not assumed.
        coins = client.coins(limit=limit, meta=True)
        if not isinstance(coins, list):
            raise ValueError(f"unexpected response: {str(coins)[:120]}")
    except Exception as e:
        with closing(_connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO market_polls (coins, ok, error, took_ms, at) VALUES (0,0,?,?,?)",
                (f"{type(e).__name__}: {e}"[:300],
                 int((time.perf_counter() - started) * 1000), _now()))
            conn.commit()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    now = _now()
    seen_codes = set()
    with closing(_connect(db_path)) as conn:
        # Scoped to this poller's own rows -- a Kraken-sourced supplemental row (see
        # refresh_supplemental below) is never mentioned in an LCW response and must not
        # be compared against it, or it looks like a fresh departure every LCW cycle.
        previous = {r["code"] for r in conn.execute(
            "SELECT code FROM market_coins WHERE present = 1 AND source = 'livecoinwatch'")}
        had_any = bool(conn.execute(
            "SELECT 1 FROM market_coins WHERE source = 'livecoinwatch' LIMIT 1").fetchone())

        rows, history = [], []
        for c in coins:
            code = c.get("code")
            if not code:
                continue
            seen_codes.add(code)
            rows.append((
                code, c.get("name"), c.get("rank"), c.get("rate"), c.get("volume"),
                c.get("cap"), c.get("liquidity"),
                (c.get("delta") or {}).get("hour"), (c.get("delta") or {}).get("day"),
                (c.get("delta") or {}).get("week"), (c.get("delta") or {}).get("month"),
                c.get("allTimeHighUSD"), now, now, now))
            history.append((code, c.get("rate"), c.get("volume"), c.get("cap"), now))

        conn.executemany(
            """INSERT INTO market_coins
                   (code, name, rank, rate, volume, market_cap, liquidity,
                    delta_hour, delta_day, delta_week, delta_month, all_time_high,
                    present, source, first_seen, last_seen, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,'livecoinwatch',?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                   name=excluded.name, rank=excluded.rank, rate=excluded.rate,
                   volume=excluded.volume, market_cap=excluded.market_cap,
                   liquidity=excluded.liquidity, delta_hour=excluded.delta_hour,
                   delta_day=excluded.delta_day, delta_week=excluded.delta_week,
                   delta_month=excluded.delta_month, all_time_high=excluded.all_time_high,
                   source='livecoinwatch',
                   last_seen=excluded.last_seen, updated_at=excluded.updated_at""",
            rows)
        conn.executemany(
            "INSERT INTO market_history (code, rate, volume, market_cap, at) VALUES (?,?,?,?,?)",
            history)

        # Listing changes. Only meaningful once there is a previous set to compare
        # against -- on the very first poll everything would look "new", which is noise.
        appeared, disappeared = [], []
        if had_any:
            by_code = {c.get("code"): c for c in coins}
            appeared = sorted(seen_codes - previous)
            disappeared = sorted(previous - seen_codes)
            conn.executemany(
                "INSERT INTO market_listings (code, name, event, rank, rate, at) VALUES (?,?,?,?,?,?)",
                [(code, (by_code.get(code) or {}).get("name"), "appeared",
                  (by_code.get(code) or {}).get("rank"), (by_code.get(code) or {}).get("rate"), now)
                 for code in appeared])
            conn.executemany(
                "INSERT INTO market_listings (code, name, event, rank, rate, at) VALUES (?,?,?,?,?,?)",
                [(code, None, "disappeared", None, None, now) for code in disappeared])
            # Mark the departures absent so the next poll compares against reality and
            # does not report them leaving a second time.
            conn.executemany("UPDATE market_coins SET present = 0 WHERE code = ?",
                             [(code,) for code in disappeared])

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=keep_history_hours)).isoformat()
        conn.execute("DELETE FROM market_history WHERE at < ?", (cutoff,))

        remaining = None
        try:
            remaining = client.credits().get("dailyCreditsRemaining")
        except Exception:
            pass
        conn.execute(
            "INSERT INTO market_polls (coins, credits_remaining, ok, took_ms, at) VALUES (?,?,1,?,?)",
            (len(rows), remaining, int((time.perf_counter() - started) * 1000), now))
        conn.commit()

    return {"ok": True, "coins": len(rows), "appeared": appeared,
            "disappeared": disappeared, "credits_remaining": remaining,
            "took_ms": int((time.perf_counter() - started) * 1000)}


def refresh_supplemental(db_path: str, id_map: dict[str, str]) -> dict:
    """Fill a handful of real, large tokens LiveCoinWatch does not list at any rank, from
    Kraken's public (keyless) Ticker endpoint -- the same venue this deployment already
    trusts for real trading, so a future move off paper prices the same instruments off
    the same source they would actually fill at.

    `id_map` is {our code: Kraken pair}, e.g. {"TAO": "TAOUSD"}. One call regardless of
    how many pairs are requested -- there is no per-call credit budget here (it's free
    and keyless), but no reason to make more requests than needed either.

    Only `rate`, `present`, and `last_seen` are load-bearing for paper_trading._prices();
    the other LCW fields (rank/market_cap/liquidity/etc.) simply stay null for a
    Kraken-sourced row. For the delta fields, Kraken's ticker gives today's opening price
    (`o`) and last trade price (`c[0]`), from which a same-day change is derived as a
    reasonable best-effort -- stored as delta_day using LCW's own multiplier convention
    (last/open, e.g. 1.02 = +2%) so `pct()` reads it the same way regardless of source.
    delta_hour/week/month are left null for these rows; they're used for movers()/briefing
    color, not pricing, so leaving them unset here is a deliberate simplification, not an
    oversight.

    Upserted with source='kraken' so LiveCoinWatch's own disappeared-listing diff (scoped
    to source='livecoinwatch') never mistakes these for vanishing on an LCW-only cycle. If
    a code is already source='livecoinwatch' -- i.e. LCW itself has started covering it --
    that row is left completely untouched here (guarded by the DO UPDATE's WHERE clause
    below): LCW stays the single, authoritative, richer-fielded source for anything it
    actually tracks, rather than the two pollers alternately overwriting one row with two
    different exchanges' quotes every few minutes.
    """
    started = time.perf_counter()
    client = KrakenGapFeed()
    pairs = list(id_map.values())
    try:
        data = client.ticker(pairs)
        if data.get("error"):
            raise ValueError(f"kraken error: {data['error']}")
        result = data.get("result") or {}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    now = _now()
    rows = []
    for code, pair in id_map.items():
        info = result.get(pair)
        if not info:
            continue
        try:
            last_price = float(info["c"][0])
            open_price = float(info["o"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        delta_day = (last_price / open_price) if open_price else None
        rows.append((code, code, last_price, delta_day, now, now, now))

    if not rows:
        return {"ok": False, "error": "no supplemental pairs returned usable data"}

    with closing(_connect(db_path)) as conn:
        conn.executemany(
            """INSERT INTO market_coins
                   (code, name, rate, delta_day, present, source, first_seen, last_seen, updated_at)
               VALUES (?,?,?,?,1,'kraken',?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                   rate=excluded.rate, delta_day=excluded.delta_day, present=1,
                   source=excluded.source,
                   last_seen=excluded.last_seen, updated_at=excluded.updated_at
               WHERE market_coins.source != 'livecoinwatch'""",
            rows)
        conn.commit()

    return {"ok": True, "codes": [r[0] for r in rows],
            "took_ms": int((time.perf_counter() - started) * 1000)}


# --- reads (what the agents and tools use) ------------------------------------

def list_tracked_codes(db_path: str) -> list[str]:
    """Every ticker this feed can currently price, sorted by rank. The ground truth for
    "can I actually trade this" -- an employee has no market-data tool to check with
    itself (research-tier grants only WebSearch), so this has to be handed to it directly
    rather than assumed from "top 250 by market cap" reasoning, which real coverage
    doesn't match (LiveCoinWatch is missing several real, large tokens entirely)."""
    with closing(_connect(db_path)) as conn:
        return [r["code"] for r in conn.execute(
            "SELECT code FROM market_coins WHERE present = 1 ORDER BY rank")]


def snapshot(db_path: str, codes: list[str] | None = None, limit: int = 25) -> list[dict]:
    sql = "SELECT * FROM market_coins"
    params: list = []
    if codes:
        sql += f" WHERE code IN ({','.join('?' * len(codes))})"
        params += [c.upper() for c in codes]
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    with closing(_connect(db_path)) as conn:
        out = []
        for r in conn.execute(sql, params):
            out.append({
                "code": r["code"], "name": r["name"], "rank": r["rank"],
                "price_usd": r["rate"], "volume_24h": r["volume"],
                "market_cap": r["market_cap"],
                "change_1h_pct": pct(r["delta_hour"]),
                "change_24h_pct": pct(r["delta_day"]),
                "change_7d_pct": pct(r["delta_week"]),
                "as_of": r["updated_at"],
            })
        return out


def movers(db_path: str, window: str = "hour", min_abs_pct: float = 3.0,
           limit: int = 15) -> list[dict]:
    """Coins that actually moved. The question an alerting agent should be asking, rather
    than pulling a price list and eyeballing it."""
    column = {"hour": "delta_hour", "day": "delta_day",
              "week": "delta_week", "month": "delta_month"}.get(window, "delta_hour")
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            f"""SELECT code, name, rank, rate, volume, {column} AS d, updated_at
                  FROM market_coins WHERE {column} IS NOT NULL
                 ORDER BY ABS({column} - 1.0) DESC LIMIT ?""", (limit * 3,)).fetchall()
    out = []
    for r in rows:
        change = pct(r["d"])
        if change is None or abs(change) < min_abs_pct:
            continue
        out.append({"code": r["code"], "name": r["name"], "rank": r["rank"],
                    "price_usd": r["rate"], "volume_24h": r["volume"],
                    f"change_{window}_pct": change, "as_of": r["updated_at"]})
        if len(out) >= limit:
            break
    return out


def change_since(db_path: str, code: str, minutes: int = 60) -> dict | None:
    """Price change measured from our own series, not from a vendor's delta field.

    This is the part a single API response cannot give: it answers "since I last looked",
    on our clock, which is what a monitoring employee running every 15 minutes needs.
    """
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    code = code.upper()
    with closing(_connect(db_path)) as conn:
        first = conn.execute(
            "SELECT rate, at FROM market_history WHERE code=? AND at>=? ORDER BY id ASC LIMIT 1",
            (code, since)).fetchone()
        last = conn.execute(
            "SELECT rate, at FROM market_history WHERE code=? ORDER BY id DESC LIMIT 1",
            (code,)).fetchone()
    if not first or not last or not first["rate"]:
        return None
    change = (last["rate"] - first["rate"]) / first["rate"] * 100.0
    return {"code": code, "from_price": first["rate"], "to_price": last["rate"],
            "change_pct": round(change, 2), "window_minutes": minutes,
            "from_at": first["at"], "to_at": last["at"]}


def new_listings(db_path: str, hours: int = 24, limit: int = 40) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            """SELECT code, name, event, rank, rate, at FROM market_listings
                WHERE at >= ? ORDER BY id DESC LIMIT ?""", (since, limit))]


def feed_status(db_path: str) -> dict:
    """Is the data fresh, and how much budget is left? An agent should be able to tell
    the difference between 'nothing is moving' and 'the feed stopped an hour ago'."""
    with closing(_connect(db_path)) as conn:
        last = conn.execute(
            "SELECT * FROM market_polls ORDER BY id DESC LIMIT 1").fetchone()
        coins = conn.execute("SELECT COUNT(*) c FROM market_coins").fetchone()["c"]
        points = conn.execute("SELECT COUNT(*) c FROM market_history").fetchone()["c"]
        fails = conn.execute(
            "SELECT COUNT(*) c FROM market_polls WHERE ok=0 AND at >= datetime('now','-1 hour')"
        ).fetchone()["c"]
    age = None
    if last and last["at"]:
        try:
            t = datetime.fromisoformat(last["at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            age = int((datetime.now(timezone.utc) - t).total_seconds())
        except ValueError:
            pass
    return {
        "coins_tracked": coins,
        "history_points": points,
        "last_poll_at": last["at"] if last else None,
        "seconds_since_poll": age,
        "stale": age is None or age > 600,
        "credits_remaining": last["credits_remaining"] if last else None,
        "failed_polls_last_hour": fails,
    }
