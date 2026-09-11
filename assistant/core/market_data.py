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
        previous = {r["code"] for r in
                    conn.execute("SELECT code FROM market_coins WHERE present = 1")}
        had_any = bool(conn.execute("SELECT 1 FROM market_coins LIMIT 1").fetchone())

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
                    present, first_seen, last_seen, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                   name=excluded.name, rank=excluded.rank, rate=excluded.rate,
                   volume=excluded.volume, market_cap=excluded.market_cap,
                   liquidity=excluded.liquidity, delta_hour=excluded.delta_hour,
                   delta_day=excluded.delta_day, delta_week=excluded.delta_week,
                   delta_month=excluded.delta_month, all_time_high=excluded.all_time_high,
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
