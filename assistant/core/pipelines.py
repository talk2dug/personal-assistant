"""A product concept and everything the business team made from it, as one object.

The problem this solves, in the owner's words: "I'm drowning in reviews." The team
produces work in a chain -- a trend becomes a concept, the concept gets art direction,
the art becomes a listing, the listing becomes social posts -- but every step arrived in
the Review queue as its own unrelated card. Twenty-eight pending approvals with no
indication that nine of them are the same product at different stages.

Nothing new had to be recorded to fix that. The chain was always in the schema:
`art_briefs`, `store_listings` and `social_posts` all carry `concept_id`, and
`product_concepts.trend_lead_id` points back at the idea that started it. A pipeline is
therefore not a new entity -- it is a concept read together with its descendants, which
is what this module assembles.

STAGES are fixed and ordered, because the value of the view is seeing where a thing has
got to and what is blocking it. A pipeline with art approved and no listing is stalled at
a different place than one with no art at all, and that difference is invisible in a flat
queue.
"""
import json
import logging
import sqlite3
from contextlib import closing

logger = logging.getLogger(__name__)

# The chain, in order. Each stage names the table it reads and the review `kind` that
# gates it, so the UI can render one shape rather than five special cases.
STAGES = ("idea", "concept", "art", "listing", "social")

REVIEW_KIND_BY_STAGE = {
    "concept": "concept",
    "art": "art",
    "listing": "listing",
    "social": "post",
}

# How a concept is sold, defaulted from how it is MADE.
#
# Laser, 3D and metal are things the owner physically produces and takes to Richmond
# vendor markets; apparel and stickers are print-on-demand and can run without him
# touching them. That is a starting guess from `product_type`, not a rule -- it is stored
# per concept and editable, because the same design can change route once it proves
# itself, and the owner's own note says the business is still working out what sells here.
LOCAL_PRODUCT_TYPES = {"laser", "3d", "metal"}
AUTOMATED_PRODUCT_TYPES = {"apparel", "sticker"}

MARKETS = ("local", "automated")

# The same rule as default_market(), expressed for SQLite.
#
# It has to exist twice -- the backfill and the filter run in the database, the card label
# is computed in Python -- but it must not be *written* twice. An earlier version hand-wrote
# both, and they disagreed the moment the team created a concept after startup: the column
# was still NULL, so the card said "automated" from the Python rule while the filter said
# "local" from a COALESCE default, and a new apparel design listed itself under the wrong
# track. Deriving the SQL from the same set makes that particular drift impossible.
_AUTOMATED_SQL_LIST = ", ".join(f"'{t}'" for t in sorted(AUTOMATED_PRODUCT_TYPES))
MARKET_SQL = (f"CASE WHEN LOWER(COALESCE(product_type, '')) IN ({_AUTOMATED_SQL_LIST}) "
              f"THEN 'automated' ELSE 'local' END")


def init_pipelines(db_path: str) -> None:
    """Add the market column and backfill it. Idempotent, same pattern as the rest."""
    with closing(sqlite3.connect(db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(product_concepts)")}
        if "market" not in cols:
            conn.execute("ALTER TABLE product_concepts ADD COLUMN market TEXT")
            logger.info("pipelines: added product_concepts.market")
        # Backfill rather than leaving concepts unclassified: an empty filter is useless,
        # and a wrong-but-editable guess is easier to correct than a blank. Run every
        # start, not only when the column is created, so concepts the team made since the
        # last restart get classified too -- reads resolve NULL anyway, this just means
        # the stored value is real and the owner can override it.
        conn.execute(f"UPDATE product_concepts SET market = {MARKET_SQL} WHERE market IS NULL")
        conn.commit()


def default_market(product_type: str | None) -> str:
    """How a concept sells, guessed from how it is made. Everything that is not
    print-on-demand -- LOCAL_PRODUCT_TYPES and anything unrecognised -- is local."""
    kind = (product_type or "").strip().lower()
    if kind in AUTOMATED_PRODUCT_TYPES:
        return "automated"
    return "local"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _loads(raw, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def set_market(db_path: str, owner_user_id: int, concept_id: int, market: str) -> bool:
    if market not in MARKETS:
        raise ValueError(f"market must be one of {MARKETS}")
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE product_concepts SET market = ? WHERE id = ? AND owner_user_id = ?",
            (market, concept_id, owner_user_id))
        conn.commit()
        return cur.rowcount > 0


def _pending_reviews(conn, owner_user_id: int) -> dict:
    """Every pending review card, indexed by (ref_table, ref_id).

    Read once for the whole listing rather than per pipeline: the flat queue is small but
    this view asks about it repeatedly, and a per-stage query would be N+1 across five
    stages and every concept on the board.
    """
    out = {}
    for row in conn.execute(
            "SELECT id, title, kind, summary, detail, ref_table, ref_id, created_at, priority "
            "FROM review_items WHERE owner_user_id = ? AND status = 'pending'",
            (owner_user_id,)):
        out.setdefault((row["ref_table"], row["ref_id"]), []).append(dict(row))
    return out


def _options_for(conn, item_ids: list[int]) -> dict:
    """Review options (the pick-one choices, and the only place artwork lives)."""
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    out = {}
    for row in conn.execute(
            f"SELECT id, item_id, label, description, media_path, body, position "
            f"FROM review_options WHERE item_id IN ({marks}) ORDER BY item_id, position",
            item_ids):
        out.setdefault(row["item_id"], []).append({
            "id": row["id"], "label": row["label"], "description": row["description"],
            "body": row["body"], "position": row["position"],
            # The path itself never reaches the browser -- it is a real filesystem path
            # on the server. The UI asks for /api/review/media/<option_id>, which
            # re-resolves it under generated_media_path before serving.
            "has_image": bool(row["media_path"]),
        })
    return out


def _chosen_options(conn, owner_user_id: int) -> dict:
    """The option the owner picked on each already-decided card, by what it was about.

    Pending cards are only half the board. Once he approves an art direction the card
    leaves the queue, and without this the artwork he chose leaves with it -- the stage
    would go back to reading "approved" and nothing else, which is the text-only view
    this was built to replace. Looking back at a finished pipeline should show the
    picture that won.
    """
    out = {}
    for row in conn.execute(
            "SELECT i.ref_table, i.ref_id, i.status, i.decided_at, "
            "       o.id, o.label, o.description, o.body, o.media_path "
            "FROM review_items i JOIN review_options o ON o.item_id = i.id "
            "WHERE i.owner_user_id = ? AND i.status != 'pending' AND o.chosen = 1",
            (owner_user_id,)):
        out[(row["ref_table"], row["ref_id"])] = {
            "id": row["id"], "label": row["label"], "description": row["description"],
            "body": row["body"], "has_image": bool(row["media_path"]),
            "decision": row["status"], "decided_at": row["decided_at"],
        }
    return out


def list_pipelines(db_path: str, owner_user_id: int, market: str | None = None,
                   limit: int = 200) -> list[dict]:
    """Every concept as a pipeline summary, newest first.

    Summary only -- enough to draw the rail and decide what needs attention. The full
    chain for one pipeline comes from get_pipeline().
    """
    # One connection, one pass. This feeds a sidebar, so it has to be quick: the first
    # version opened two connections and shipped every concept's full description --
    # 1.4s and 71KB to draw a list of names.
    with closing(_connect(db_path)) as conn:
        query = ("SELECT id, name, product_type, status, market, price_estimate, "
                 "       trend_lead_id, created_at, updated_at "
                 "FROM product_concepts WHERE owner_user_id = ?")
        params: list = [owner_user_id]
        if market in MARKETS:
            # COALESCE against the same rule the card label uses, so a concept created
            # since the last backfill filters to where it says it is.
            query += f" AND COALESCE(market, {MARKET_SQL}) = ?"
            params.append(market)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        concepts = [dict(r) for r in conn.execute(query, params)]
        if not concepts:
            return []

        ids = [c["id"] for c in concepts]
        marks = ",".join("?" * len(ids))

        def tally(table):
            counts = {}
            for row in conn.execute(
                    f"SELECT concept_id, COUNT(*) n FROM {table} "
                    f"WHERE concept_id IN ({marks}) GROUP BY concept_id", ids):
                counts[row["concept_id"]] = row["n"]
            return counts

        arts, listings, posts = tally("art_briefs"), tally("store_listings"), tally("social_posts")
        pending = _pending_reviews(conn, owner_user_id)

        # Which concept each downstream row belongs to, so a pending review card can be
        # counted against its pipeline without a query per stage per concept.
        owner_of = {}
        for table in ("art_briefs", "store_listings", "social_posts"):
            for row in conn.execute(f"SELECT id, concept_id FROM {table} "
                                    f"WHERE concept_id IN ({marks})", ids):
                owner_of[(table, row["id"])] = row["concept_id"]

    waiting_by_concept: dict = {}
    for (table, ref_id), items in pending.items():
        concept_id = (ref_id if table == "product_concepts"
                      else owner_of.get((table, ref_id)))
        if concept_id is not None:
            waiting_by_concept[concept_id] = waiting_by_concept.get(concept_id, 0) + len(items)

    for concept in concepts:
        concept["market"] = concept.get("market") or default_market(concept["product_type"])
        concept["counts"] = {
            "art": arts.get(concept["id"], 0),
            "listing": listings.get(concept["id"], 0),
            "social": posts.get(concept["id"], 0),
        }
        concept["pending_reviews"] = waiting_by_concept.get(concept["id"], 0)
        concept["stage"] = _furthest_stage(concept)
    return concepts


def _furthest_stage(concept: dict) -> str:
    """How far down the chain this pipeline has actually got.

    Reported as the furthest point reached rather than "what is blocked", because the
    rail is for orientation -- the blocking detail belongs on the card the owner opens.
    """
    counts = concept["counts"]
    if counts["social"]:
        return "social"
    if counts["listing"]:
        return "listing"
    if counts["art"]:
        return "art"
    return "concept"


def get_pipeline(db_path: str, owner_user_id: int, concept_id: int) -> dict | None:
    """One pipeline, start to finish, with every artefact and every pending approval."""
    with closing(_connect(db_path)) as conn:
        concept = conn.execute(
            "SELECT * FROM product_concepts WHERE id = ? AND owner_user_id = ?",
            (concept_id, owner_user_id)).fetchone()
        if concept is None:
            return None
        concept = dict(concept)
        concept["market"] = concept.get("market") or default_market(concept.get("product_type"))

        lead = None
        if concept.get("trend_lead_id"):
            row = conn.execute("SELECT * FROM trend_leads WHERE id = ?",
                               (concept["trend_lead_id"],)).fetchone()
            lead = dict(row) if row else None

        briefs = [dict(r) for r in conn.execute(
            "SELECT * FROM art_briefs WHERE concept_id = ? ORDER BY id", (concept_id,))]
        listings = [dict(r) for r in conn.execute(
            "SELECT * FROM store_listings WHERE concept_id = ? ORDER BY id", (concept_id,))]
        posts = [dict(r) for r in conn.execute(
            "SELECT * FROM social_posts WHERE concept_id = ? ORDER BY id", (concept_id,))]

        for listing in listings:
            listing["variants"] = _loads(listing.get("variants"), [])
            listing["metrics"] = _loads(listing.get("metrics"), {})
        for post in posts:
            post["metrics"] = _loads(post.get("metrics"), {})

        pending = _pending_reviews(conn, owner_user_id)
        review_ids = [i["id"] for items in pending.values() for i in items]
        options = _options_for(conn, review_ids)
        chosen = _chosen_options(conn, owner_user_id)

    def reviews_for(table, ref_id):
        items = [dict(i) for i in pending.get((table, ref_id), [])]
        for item in items:
            item["options"] = options.get(item["id"], [])
        return items

    def decorate(table, row):
        """One artefact with both what is waiting on it and what was already decided."""
        return {**row, "reviews": reviews_for(table, row["id"]),
                "chosen": chosen.get((table, row["id"]))}

    stages = [
        {"stage": "idea", "items": [lead] if lead else [],
         "reviews": []},
        {"stage": "concept",
         "items": [{**concept, "chosen": chosen.get(("product_concepts", concept_id))}],
         "reviews": reviews_for("product_concepts", concept_id)},
        {"stage": "art",
         "items": [decorate("art_briefs", b) for b in briefs],
         "reviews": [r for b in briefs for r in reviews_for("art_briefs", b["id"])]},
        {"stage": "listing",
         "items": [decorate("store_listings", l) for l in listings],
         "reviews": [r for l in listings for r in reviews_for("store_listings", l["id"])]},
        {"stage": "social",
         "items": [decorate("social_posts", p) for p in posts],
         "reviews": [r for p in posts for r in reviews_for("social_posts", p["id"])]},
    ]

    return {
        "concept": concept,
        "lead": lead,
        "stages": stages,
        "pending_reviews": sum(len(s["reviews"]) for s in stages),
    }
