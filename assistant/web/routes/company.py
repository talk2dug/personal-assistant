"""The company dashboard — everything about the store on one screen.

Jack asked for four things and they are four different questions, so this answers them as
four sections rather than one blended feed:

  * what they have made          -> listings, and what each one cost to make
  * what the engagement looks like -> reach and sales per product
  * what they are thinking for tomorrow -> the next item, before it exists
  * how they reached their conclusions  -> the agents' own journals, in their words

The last one is the reason this route reads Obsidian rather than only the database. The
agents write their reasoning to the vault on every run (agent_notes.write_journal), and a
summary row cannot carry "what I ruled out and why". Answering "how are they coming to the
conclusions" from a status column would mean inventing the answer.

It is a window onto the business with exactly ONE control on it, and the shape of that
control is the point. Jack, 2026-09-22: *"this is the automated store, i should NOT be
approving anything. This is 100% AI driven... I dont need to aprove but I can retract
something meaning remove it from being sold, but i need to give a reason why so it
continues to learn."* So nothing here asks him to let something through; the only button
takes something down, and it will not work without a reason, because the reason is what
the team learns from. The rate dial is still changed by talking to Jarvis and blockers
are still cleared on the Projects board.
"""
import logging
import mimetypes
import pathlib
import sqlite3
from contextlib import closing

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ...core import agent_notes, business_db, model_catalog, owner_requests, store_policy
from ..auth import require_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/company", tags=["company"])

# The agents whose thinking belongs on this dashboard, in pipeline order -- the sequence
# a product actually travels, so reading down the panel reads as the story of a product.
STORE_AGENTS = ("trend_scout", "market_finder", "product_creator", "art_director",
                "store_manager", "social_director")

JOURNAL_AGENTS_SHOWN = 6
RECENT_RUNS = 40


def _owner_id(request: Request) -> int:
    user = require_user(request)
    if user["role"] != "owner":
        raise HTTPException(403, "the company dashboard is owner-only")
    return user["id"]


def _vault(request: Request):
    """The ObsidianClient, however it was wired.

    app.state.obsidian holds an ObsidianContext in production, but the same attribute is
    set to a bare client in some call paths and tests. Accepting both beats a dashboard
    that silently shows no reasoning because it asked for the wrong wrapper.
    """
    context = getattr(request.app.state, "obsidian", None)
    if context is None:
        return None
    client = getattr(context, "mcp_client", None)
    if client is not None:
        return client
    return context if hasattr(context, "read_note") else None


@router.get("")
def dashboard(request: Request):
    uid = _owner_id(request)
    cfg = request.app.state.cfg
    db_path = cfg.db_path

    return {
        "policy": store_policy.current(db_path),
        "made": _made(db_path, uid),
        "pipeline": _pipeline(db_path, uid),
        "thinking": _thinking(db_path, _vault(request)),
        "blocked": _blocked(db_path, uid),
        "catalog": _catalog(db_path),
        "pulled": _pulled(db_path, uid),
    }


def _pulled(db_path: str, uid: int) -> list:
    """What he has taken off the store, and why he said he did.

    On the dashboard rather than buried, because it is the only instruction the team ever
    gets from him about this store -- and seeing it listed is how he can tell whether
    saying it changed anything.
    """
    try:
        from assistant.core import store_retract

        return store_retract.recent(db_path, uid, limit=10)
    except Exception:                                    # noqa: BLE001
        return []


def _art_for(db_path: str, concept_id) -> dict | None:
    """The rendered image behind a listing, as {brief_id, path}."""
    if not concept_id:
        return None
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT id, media_path FROM art_briefs
                    WHERE concept_id = ? AND media_path IS NOT NULL
                      AND trim(media_path) <> '' ORDER BY id DESC LIMIT 1""",
                (concept_id,)).fetchone()
    except Exception:                                    # noqa: BLE001
        return None
    return {"brief_id": row["id"], "path": row["media_path"]} if row else None


def _posts_for(db_path: str, uid: int, listing_id: int, concept_id) -> list:
    """The campaign written to sell this one product.

    There is no campaign table and there should not be one yet: the campaign IS these
    posts, and an empty object called "campaign" would be a promise the system does not
    keep. Matched on the listing, falling back to the concept, because the social
    director writes against whichever it had.
    """
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, platform, hook, caption, status, scheduled_for, external_id
                     FROM social_posts
                    WHERE owner_user_id = ?
                      AND (listing_id = ? OR (listing_id IS NULL AND concept_id = ?))
                    ORDER BY id""",
                (uid, listing_id, concept_id)).fetchall()
    except Exception:                                    # noqa: BLE001
        return []
    return [dict(r) for r in rows]


def _catalog(db_path: str) -> dict:
    """How many faces are available to front products.

    Guarded like every other panel: a dashboard that 500s because one optional table has
    not been created yet is worse than a dashboard with one empty panel, and this screen
    is the thing he checks to find out whether anything is wrong.
    """
    try:
        return model_catalog.catalog_summary(db_path)
    except Exception:                                    # noqa: BLE001
        return {"personas": 0, "images": 0, "categories": {}}


def _made(db_path: str, uid: int) -> dict:
    """What exists to sell, and what it is doing.

    Engagement is deliberately absent rather than zeroed. There is no analytics source
    wired yet -- that needs TikTok and Meta insights, both of which are still blocked on
    posting access -- and a dashboard showing "0 views" for a product nobody has ever
    posted would be a lie with a number on it. `engagement_source` says so plainly.
    """
    try:
        listings = business_db.list_store_listings(db_path, uid)
    except Exception:
        listings = []
    # LIVE MEANS A STOREFRONT AGREES. 'approved' used to count, and it means only that
    # our own pipeline finished with it -- the same lie the store mission told until it
    # was fixed the same day. A number he reads as "products people can buy" must not be
    # satisfiable by a status this system sets on itself.
    live = [x for x in listings
            if (x.get("status") or "") in ("published", "on_sale")
            and (x.get("external_id") or "").strip()]
    waiting = [x for x in listings if (x.get("status") or "") in ("draft", "approved")]

    cards = []
    for listing in listings[:12]:
        art = _art_for(db_path, listing.get("concept_id"))
        posts = _posts_for(db_path, uid, listing["id"], listing.get("concept_id"))
        cards.append({
            **listing,
            "art_brief_id": (art or {}).get("brief_id"),
            "is_live": bool((listing.get("external_id") or "").strip()
                            and (listing.get("status") or "") in ("published", "on_sale")),
            "posts": posts,
        })

    return {
        "listings_total": len(listings),
        "listings_live": len(live),
        "listings_waiting": len(waiting),
        "recent": cards,
        "engagement": None,
        "engagement_source": (
            "Not wired yet: reach and sales need TikTok and Meta insights, and both are "
            "still waiting on posting access. Showing zeros here would read as 'nobody "
            "engaged' rather than 'nothing has been posted'."
        ),
    }


def _pipeline(db_path: str, uid: int) -> dict:
    """What is coming: the concepts and briefs between an idea and a listing."""
    def count(fn, **kw):
        try:
            return fn(db_path, uid, **kw)
        except Exception:
            return []

    concepts = count(business_db.list_product_concepts)
    briefs = count(business_db.list_art_briefs)
    trends = count(business_db.list_trend_leads)
    return {
        "trend_leads": len(trends),
        "concepts": len(concepts),
        "art_briefs": len(briefs),
        # "What are they thinking for tomorrow's item" is answered by the concepts that
        # exist but have not become listings yet -- that IS tomorrow's item, before it is
        # made. Proposed first: an approved concept is already in motion.
        "next_up": [c for c in concepts if (c.get("status") or "") == "proposed"][:5],
    }


def _thinking(db_path: str, vault) -> dict:
    """The agents' own reasoning, in their words, newest first.

    Read from the vault rather than the database on purpose: agent_runs.summary is one
    line of outcome, and the question here is *why*. When the vault is unreachable this
    says so instead of returning an empty list, because "no notes" and "could not read the
    notes" are different states and only one of them is a problem to fix.
    """
    entries = []
    unavailable = None
    if vault is None:
        unavailable = ("The Obsidian vault is not wired this session, so the agents' "
                       "written reasoning cannot be read. Their runs still happened.")
    else:
        for agent in STORE_AGENTS[:JOURNAL_AGENTS_SHOWN]:
            try:
                block = agent_notes.read_journal(vault, agent, entries=2)
            except Exception as exc:                      # noqa: BLE001
                logger.warning("could not read %s journal: %s", agent, exc)
                continue
            if block.strip():
                entries.append({"agent": agent, "notes": block})

    runs = []
    try:
        for run in business_db.recent_agent_runs(db_path, limit=RECENT_RUNS):
            if run["agent"] in STORE_AGENTS:
                runs.append({"agent": run["agent"], "status": run["status"],
                             "summary": run["summary"], "at": run["started_at"]})
    except Exception:
        pass

    return {"journals": entries, "recent_runs": runs[:15], "unavailable": unavailable}


def _blocked(db_path: str, uid: int) -> dict:
    """What is stopping the business, so the dashboard never reads healthier than it is."""
    try:
        summary = owner_requests.blocked_summary(db_path, uid)
        items = [r for r in owner_requests.list_requests(db_path, uid, status="open")]
    except Exception:
        return {"open": 0, "items": []}
    return {"open": summary.get("open", 0),
            "items": [{"id": r["id"], "title": r["title"], "blocks": r["blocks"],
                       "priority": r["priority"], "kind": r["kind"]} for r in items[:6]]}


@router.get("/art/{brief_id}")
async def art(brief_id: int, request: Request):
    """The rendered image for one art brief.

    Served through the app for the same reason as routes/review.py's media endpoint:
    generated art lives outside the web root, and the path comes from a database row, so
    it is resolved and then checked to be inside generated_media_path before anything is
    opened.
    """
    uid = _owner_id(request)
    cfg = request.app.state.cfg

    with closing(sqlite3.connect(cfg.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT b.media_path FROM art_briefs b
                 LEFT JOIN product_concepts c ON c.id = b.concept_id
                WHERE b.id = ? AND (b.owner_user_id = ? OR c.owner_user_id = ?)""",
            (brief_id, uid, uid)).fetchone()
    if row is None or not row["media_path"]:
        raise HTTPException(404, "no art for that brief")

    root = pathlib.Path(cfg.generated_media_path).resolve()
    path = pathlib.Path(row["media_path"]).resolve()
    if not path.is_file():
        raise HTTPException(404, "the file is no longer on disk")
    if root not in path.parents:
        raise HTTPException(403, "that file is outside the generated media directory")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.post("/retract")
async def retract(request: Request):
    """Pull a product off the store. The reason is required, and it teaches the team.

    His only control over this store, by his own instruction -- he approves nothing going
    up and pulls what he does not want. The reason is not bookkeeping: it goes straight
    into the prompts of the four agents that could have prevented it, so the same thing is
    not made again next week.
    """
    from assistant.core import store_retract

    uid = _owner_id(request)
    cfg = request.app.state.cfg
    body = await request.json()
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "a reason is required — without one nothing is learned")

    printify = shopify = None
    try:
        from assistant.core.printify_client import PrintifyClient

        printify = PrintifyClient(cfg.db_path)
    except Exception:                                    # noqa: BLE001
        pass
    try:
        from assistant.core.shopify_client import ShopifyClient

        shopify = ShopifyClient(cfg.db_path)
    except Exception:                                    # noqa: BLE001
        pass

    return store_retract.retract(cfg.db_path, uid, int(body["listing_id"]), reason,
                                 printify=printify, shopify=shopify)
