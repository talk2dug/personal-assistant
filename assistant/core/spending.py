"""Every transaction on the checking account, sorted into what is required and what is not.

Jack's ask: a place to see each item and mark it, so the budget is built from what he
actually spends rather than from a guess. Era already supplies the transactions and its own
categories -- "Side hustles and business", "Groceries" -- but a category is not the axis he
needs. Groceries can be required and DoorDash can be extra while both are "food", and only
he knows which of his Amazon orders were which.

The shape that makes this survivable is merchant-first. The checking account holds 845
transactions across **96 distinct merchants**: classifying merchants is 96 decisions,
classifying transactions is 845, and the second is a job nobody finishes. So a decision is
recorded against the merchant, applied to everything it has ever bought, and applied again
to anything that arrives later. A single transaction can still be overridden by hand --
Amazon really is both things -- and a manual decision is never overwritten by a rule.

Four classifications, because two were not enough to describe a real statement:

    required  -- rent, utilities, insurance, the phone bill. The floor a budget sits on.
    extra     -- everything he could stop tomorrow. The part a budget can actually move.
    income    -- money in. Never spending, and counting it as such would flatter the total.
    transfer  -- moving his own money between his own accounts. "Chime" alone appears 432
                 times here; counted as spending it would dwarf everything real.
"""
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 'unreviewed' is the default and is deliberately not a judgement: an unreviewed total has
# to be visible, or a half-classified account silently reads as a small one.
NECESSITY = ("unreviewed", "required", "extra", "income", "transfer")

# Where a classification came from. The distinction has teeth: a rule may overwrite
# anything EXCEPT a decision he made by hand on a single transaction.
SOURCES = ("manual", "rule", "auto")

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Era's own id. The natural key for re-syncing: a transaction that changes (a pending
    -- charge settling at a different amount) updates in place rather than duplicating.
    era_id TEXT NOT NULL UNIQUE,
    account_key TEXT,
    account_name TEXT,
    txn_date TEXT,
    posted_date TEXT,
    amount REAL NOT NULL,
    description TEXT,
    merchant TEXT,
    -- What the bank actually wrote, before Era tidied it. Kept because it is often the
    -- only way to tell two charges from the same merchant apart.
    original_description TEXT,
    era_category TEXT,
    is_outflow INTEGER NOT NULL DEFAULT 1,
    is_pending INTEGER NOT NULL DEFAULT 0,
    necessity TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (necessity IN ('unreviewed','required','extra','income','transfer')),
    -- NULL while unreviewed; 'manual' marks a decision a rule must not overwrite.
    necessity_source TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spend_merchant_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Normalised (see merchant_key): "DOORDASH*ORDER 123" and "DoorDash" are one merchant,
    -- and a rule keyed on the raw string would be a rule per order.
    merchant_key TEXT NOT NULL UNIQUE,
    merchant_label TEXT NOT NULL,
    necessity TEXT NOT NULL
        CHECK (necessity IN ('required','extra','income','transfer')),
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_spend_txn_date ON spend_transactions(txn_date);
CREATE INDEX IF NOT EXISTS idx_spend_txn_merchant ON spend_transactions(merchant);
CREATE INDEX IF NOT EXISTS idx_spend_txn_necessity ON spend_transactions(necessity);
"""


def init_spending_db(db_path: str) -> None:
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


def merchant_key(name: str | None) -> str:
    """One key per merchant, however the bank spelled it that day.

    Card networks append order ids, store numbers and city names to the merchant string,
    so "DOORDASH*ORDER 8837" and "DoorDash" arrive as different text for the same company.
    Keying on the tidied leading words collapses them; without it a "rule per merchant"
    becomes a rule per purchase and the whole idea fails.
    """
    text = (name or "").upper()
    text = re.split(r"[*#]", text)[0]                    # DOORDASH*ORDER 8837 -> DOORDASH
    # Apostrophes are DELETED, not spaced: "MCDONALD'S" and "MCDONALDS" are one merchant,
    # and turning the apostrophe into a space makes them two.
    text = text.replace("'", "").replace("’", "")
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"\b\d{3,}\b", " ", text)              # store and order numbers
    return " ".join(text.split())[:40] or "UNKNOWN"


def sync_from_era(db_path: str, call_tool, account_key: str, *, max_pages: int = 20,
                  page_size: int = 100) -> dict:
    """Pull transactions from Era into the local table.

    `call_tool(name, arguments) -> dict` is passed in rather than an Era client, so this is
    testable without a network and without a live financial account.

    Existing rows are updated, never duplicated, and a classification already made is left
    alone: re-syncing must not undo an afternoon of sorting.
    """
    init_spending_db(db_path)
    added = updated = 0
    seen = 0
    for page in range(1, max_pages + 1):
        payload = call_tool("transactions__list_transactions",
                            {"account_group_key": account_key, "page_size": page_size,
                             "page": page})
        txns = (payload or {}).get("transactions") or []
        if not txns:
            break
        seen += len(txns)
        with closing(_connect(db_path)) as conn:
            for t in txns:
                era_id = t.get("transaction_id")
                if not era_id:
                    continue
                amount = float(t.get("amount") or 0)
                outflow = 1 if t.get("is_cash_outflow", amount < 0) else 0
                row = conn.execute("SELECT id, necessity, necessity_source FROM "
                                   "spend_transactions WHERE era_id = ?", (era_id,)).fetchone()
                now = _now()
                if row is None:
                    conn.execute(
                        """INSERT INTO spend_transactions
                               (era_id, account_key, account_name, txn_date, posted_date,
                                amount, description, merchant, original_description,
                                era_category, is_outflow, is_pending, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (era_id, t.get("account_group_key"), t.get("account_name"),
                         t.get("transaction_date"), t.get("posted_date"), amount,
                         t.get("description"), t.get("merchant_name") or t.get("description"),
                         t.get("original_description"), t.get("category"), outflow,
                         1 if t.get("is_pending") else 0, now, now))
                    added += 1
                else:
                    # Facts refresh; the judgement does not.
                    conn.execute(
                        """UPDATE spend_transactions
                              SET amount = ?, description = ?, merchant = ?,
                                  original_description = ?, era_category = ?,
                                  is_outflow = ?, is_pending = ?, posted_date = ?,
                                  updated_at = ?
                            WHERE era_id = ?""",
                        (amount, t.get("description"),
                         t.get("merchant_name") or t.get("description"),
                         t.get("original_description"), t.get("category"), outflow,
                         1 if t.get("is_pending") else 0, t.get("posted_date"), now, era_id))
                    updated += 1
            conn.commit()
        if len(txns) < page_size:
            break

    applied = apply_rules(db_path)
    auto = _auto_classify_income(db_path)
    return {"seen": seen, "added": added, "updated": updated,
            "rules_applied": applied, "income_auto": auto}


def _auto_classify_income(db_path: str) -> int:
    """Money coming IN is not spending.

    Marked automatically because leaving deposits 'unreviewed' buries the real work under
    rows there is nothing to decide about. Recorded as source 'auto' so a refund he would
    rather call something else is still his to change, and so a rule can still override it.
    """
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """UPDATE spend_transactions
                  SET necessity = 'income', necessity_source = 'auto', updated_at = ?
                WHERE is_outflow = 0 AND amount > 0 AND necessity = 'unreviewed'""",
            (_now(),))
        conn.commit()
        return cur.rowcount


def set_merchant_rule(db_path: str, merchant: str, necessity: str,
                      note: str | None = None) -> dict:
    """Decide once for a merchant, and have it stick for everything it ever sold him."""
    if necessity not in ("required", "extra", "income", "transfer"):
        raise ValueError(f"necessity must be one of required/extra/income/transfer, not {necessity!r}")
    key = merchant_key(merchant)
    now = _now()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO spend_merchant_rules
                   (merchant_key, merchant_label, necessity, note, created_at, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(merchant_key) DO UPDATE SET
                   necessity = excluded.necessity, merchant_label = excluded.merchant_label,
                   note = COALESCE(excluded.note, spend_merchant_rules.note),
                   updated_at = excluded.updated_at""",
            (key, merchant, necessity, note, now, now))
        conn.commit()
    return {"merchant_key": key, "necessity": necessity, "applied": apply_rules(db_path)}


def apply_rules(db_path: str) -> int:
    """Push every merchant rule onto its transactions.

    A rule never overwrites a 'manual' decision. That exception is the whole reason both
    fields exist: Amazon is genuinely both things, and a rule that undid his per-order
    corrections every sync would make the corrections pointless.
    """
    changed = 0
    with closing(_connect(db_path)) as conn:
        rules = {r["merchant_key"]: r["necessity"]
                 for r in conn.execute("SELECT merchant_key, necessity FROM spend_merchant_rules")}
        if not rules:
            return 0
        rows = conn.execute(
            """SELECT id, merchant, necessity, necessity_source FROM spend_transactions
                WHERE necessity_source IS NULL OR necessity_source != 'manual'""").fetchall()
        now = _now()
        for row in rows:
            wanted = rules.get(merchant_key(row["merchant"]))
            if wanted and row["necessity"] != wanted:
                conn.execute(
                    "UPDATE spend_transactions SET necessity = ?, necessity_source = 'rule', "
                    "updated_at = ? WHERE id = ?", (wanted, now, row["id"]))
                changed += 1
        conn.commit()
    return changed


def classify_transaction(db_path: str, era_id: str, necessity: str,
                         note: str | None = None) -> bool:
    """One transaction, decided by hand. Outranks any merchant rule, now and later."""
    if necessity not in NECESSITY:
        raise ValueError(f"unknown necessity {necessity!r}")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """UPDATE spend_transactions
                  SET necessity = ?, necessity_source = 'manual',
                      note = COALESCE(?, note), updated_at = ?
                WHERE era_id = ?""", (necessity, note, _now(), era_id))
        conn.commit()
        return cur.rowcount > 0


def merchants(db_path: str, since: str | None = None, only_unreviewed: bool = False) -> list:
    """The work list: one row per merchant, biggest spender first.

    Ordered by what it has cost him rather than alphabetically or by date, because the
    point of the screen is to spend the first ten minutes on the merchants that decide the
    budget rather than on the ones that do not.
    """
    where = ["1=1"]
    args: list = []
    if since:
        where.append("txn_date >= ?")
        args.append(since)
    sql = f"""SELECT merchant,
                     COUNT(*) AS txns,
                     SUM(CASE WHEN is_outflow = 1 THEN ABS(amount) ELSE 0 END) AS spent,
                     MIN(txn_date) AS first_seen,
                     MAX(txn_date) AS last_seen,
                     SUM(CASE WHEN necessity = 'unreviewed' THEN 1 ELSE 0 END) AS unreviewed
                FROM spend_transactions
               WHERE {' AND '.join(where)}
               GROUP BY merchant"""
    with closing(_connect(db_path)) as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
        rules = {r["merchant_key"]: dict(r)
                 for r in conn.execute("SELECT * FROM spend_merchant_rules")}
    for row in rows:
        rule = rules.get(merchant_key(row["merchant"]))
        row["merchant_key"] = merchant_key(row["merchant"])
        row["necessity"] = rule["necessity"] if rule else "unreviewed"
        row["note"] = rule["note"] if rule else None
    if only_unreviewed:
        rows = [r for r in rows if r["necessity"] == "unreviewed"]
    return sorted(rows, key=lambda r: -(r["spent"] or 0))


def transactions(db_path: str, merchant: str | None = None, since: str | None = None,
                 necessity: str | None = None, limit: int = 200) -> list:
    where, args = ["1=1"], []
    if merchant:
        where.append("merchant = ?")
        args.append(merchant)
    if since:
        where.append("txn_date >= ?")
        args.append(since)
    if necessity:
        where.append("necessity = ?")
        args.append(necessity)
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            f"""SELECT * FROM spend_transactions WHERE {' AND '.join(where)}
                 ORDER BY txn_date DESC, id DESC LIMIT {int(limit)}""", args)]


def summary(db_path: str, since: str | None = None) -> dict:
    """Required versus extra, with the unreviewed remainder shown rather than hidden.

    A half-sorted account that reports only what has been classified reads as a much
    cheaper life than it is, so the unclassified total is returned alongside and the UI is
    expected to show it.
    """
    where, args = ["1=1"], []
    if since:
        where.append("txn_date >= ?")
        args.append(since)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            f"""SELECT necessity,
                       COUNT(*) AS txns,
                       SUM(CASE WHEN is_outflow = 1 THEN ABS(amount) ELSE 0 END) AS outflow,
                       SUM(CASE WHEN is_outflow = 0 THEN amount ELSE 0 END) AS inflow
                  FROM spend_transactions WHERE {' AND '.join(where)}
                 GROUP BY necessity""", args).fetchall()
        span = conn.execute(
            f"SELECT MIN(txn_date) a, MAX(txn_date) b FROM spend_transactions "
            f"WHERE {' AND '.join(where)}", args).fetchone()

    by = {r["necessity"]: {"txns": r["txns"], "outflow": round(r["outflow"] or 0, 2),
                           "inflow": round(r["inflow"] or 0, 2)} for r in rows}
    spend = lambda k: by.get(k, {}).get("outflow", 0.0)      # noqa: E731
    required, extra, unreviewed = spend("required"), spend("extra"), spend("unreviewed")
    classified = required + extra
    return {
        "from": span["a"], "to": span["b"],
        "by_necessity": by,
        "required": round(required, 2),
        "extra": round(extra, 2),
        "unreviewed": round(unreviewed, 2),
        "income": round(by.get("income", {}).get("inflow", 0.0), 2),
        "transfers": round(spend("transfer"), 2),
        # Only meaningful once most of it is classified, so the caller is told how much of
        # the spend the split actually covers rather than being handed a bare percentage.
        "extra_share": round(extra / classified, 4) if classified else None,
        "reviewed_share": round(classified / (classified + unreviewed), 4)
                          if (classified + unreviewed) else None,
    }
