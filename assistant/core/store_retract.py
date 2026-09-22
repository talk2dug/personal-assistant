"""Pulling a product off the store, and making the reason count for something.

Jack, 2026-09-22: *"I dont need to aprove but I can retract something meaning remove it
from being sold, but i need to give a reason why so it continues to learn."*

This is the store's only human control and it is deliberately the opposite shape from an
approval queue. An approval queue asks him to judge a description of a thing that does
not exist yet, before anyone knows whether it sells; it costs him attention on every item
including the good ones, and it stops the line whenever he is busy. This costs him
attention only on the ones he actually objects to, and the judgement is about a real
product he can see.

THE REASON IS NOT BOOKKEEPING. It is the entire value of the interaction. A pull with no
reason teaches nothing and the team makes the same thing again next week; that is the
difference between a store that learns and a store that annoys him at a constant rate.
So the reason is required, and `lessons()` feeds it straight back into the prompts of the
four agents that could have prevented it -- which is the part that is easy to leave out
and makes the rest pointless. See [[feedback_jarvis_must_own_outcomes]].

WHAT RETRACTING DOES. Unpublishes from Shopify first, because that is the one that stops
a stranger buying it, then deletes the Printify product. Shopify first on purpose: if the
second step fails, the product is already unbuyable and the failure is a tidy-up job
rather than a thing still for sale under his name.
"""
import logging
from contextlib import closing
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS store_retractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    listing_id INTEGER,
    concept_id INTEGER,
    -- Kept as text rather than only as ids: the listing may be edited or the concept
    -- retired, and a lesson that says "he pulled listing 34" teaches nothing to a model
    -- that cannot look it up.
    title TEXT,
    product_type TEXT,
    external_id TEXT,
    reason TEXT NOT NULL,
    retracted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_store_retractions_owner
    ON store_retractions(owner_user_id, id DESC);
"""

# How many past pulls to put in front of an agent. Enough to show a pattern, few enough
# that the prompt is still mostly about the job in hand.
LESSON_LIMIT = 12


def init_retractions(db_path: str) -> None:
    import sqlite3

    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str):
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _unpublish(listing: dict, shop_id=None, printify=None, shopify=None) -> list[str]:
    """Take it off sale. Returns what was done, for the record and for him.

    Shopify first, because that is the one that stops a stranger buying it. If the
    Printify delete then fails, the product is already unbuyable and what is left is a
    tidy-up rather than a thing still for sale under his name.
    """
    done = []
    title = listing.get("title") or ""

    if shopify is not None and title:
        try:
            product = shopify.find_product_by_title(title)
            if product:
                # Archived, not deleted: an archived Shopify product keeps its order
                # history, and a product he pulled may still have sales to account for.
                shopify.set_product_status(product["id"], "archived")
                done.append("archived on Shopify")
        except Exception:                                           # noqa: BLE001
            logger.exception("could not archive %r on Shopify", title)

    external = (listing.get("external_id") or "").strip()
    if printify is not None and external and shop_id is not None:
        try:
            printify.delete_product(shop_id, external)
            done.append("deleted from Printify")
        except Exception:                                           # noqa: BLE001
            logger.exception("could not delete Printify product %s", external)
    return done


def retract(db_path: str, owner_user_id: int, listing_id: int, reason: str,
            printify=None, shopify=None) -> dict:
    """Pull one product off the store and record why.

    The reason is required. A pull with no reason removes the product and teaches
    nothing, so the team makes the same thing again next week -- which is the failure
    this whole mechanism exists to avoid.
    """
    from . import business_db, store_publish

    reason = (reason or "").strip()
    if len(reason) < 3:
        return {"ok": False, "why": "a retraction needs a reason — without one the team "
                                    "will make the same thing again next week"}

    listing = business_db.get_store_listing(db_path, owner_user_id, listing_id)
    if listing is None:
        return {"ok": False, "why": f"no listing {listing_id}"}

    concept = (business_db.get_concept(db_path, owner_user_id, listing["concept_id"])
               if listing.get("concept_id") else None) or {}

    shop = None
    if printify is not None:
        try:
            shop = store_publish.resolve_shop(db_path, printify)
        except Exception:                                           # noqa: BLE001
            logger.exception("could not resolve the Printify shop to retract from")
    done = _unpublish(listing, shop_id=shop, printify=printify, shopify=shopify)

    business_db.update_store_listing(db_path, owner_user_id, listing_id,
                                     status="delisted")
    init_retractions(db_path)
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO store_retractions
                   (owner_user_id, listing_id, concept_id, title, product_type,
                    external_id, reason, retracted_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (owner_user_id, listing_id, listing.get("concept_id"), listing.get("title"),
             concept.get("product_type"), listing.get("external_id"), reason, _now()))
        conn.commit()

    return {"ok": True, "listing_id": listing_id, "title": listing.get("title"),
            "reason": reason, "did": done or ["recorded only — it was not on sale yet"]}


def recent(db_path: str, owner_user_id: int, limit: int = LESSON_LIMIT) -> list[dict]:
    init_retractions(db_path)
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM store_retractions WHERE owner_user_id = ?
                ORDER BY id DESC LIMIT ?""", (owner_user_id, limit))]


def lessons(db_path: str, owner_user_id: int, limit: int = LESSON_LIMIT) -> str:
    """What he has pulled and why, as a block to prepend to an agent's prompt.

    Returns "" when there is nothing, so every caller can concatenate unconditionally.
    This is the half that makes recording the reason worth doing at all: without it the
    table is a log nobody reads, and the store repeats the mistake at whatever rate it
    produces.
    """
    pulled = recent(db_path, owner_user_id, limit)
    if not pulled:
        return ""
    lines = [
        "",
        "WHAT JACK HAS PULLED OFF THE STORE, AND WHY. He does not approve anything here "
        "before it goes up, so this list is the only steer you get from him. Every line "
        "is a product that was made, listed, and then taken down -- treat each as a rule "
        "about what not to make again, not as a one-off:",
    ]
    for row in pulled:
        kind = f" ({row['product_type']})" if row.get("product_type") else ""
        lines.append(f'- "{row["title"]}"{kind} — {row["reason"]}')
    lines.append(
        "If something you are about to make is close to one of these, either do not make "
        "it or say plainly in your reasoning why this one is different.")
    return "\n".join(lines) + "\n"
