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

Everything here is read-only. The dashboard is a window onto the business, not a control
panel: the rate dial is changed by talking to Jarvis and the blockers are cleared on the
Projects board, both of which already have their own places.
"""
import logging

from fastapi import APIRouter, HTTPException, Request

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
    }


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
    live = [x for x in listings if (x.get("status") or "") in ("approved", "published", "live")]
    return {
        "listings_total": len(listings),
        "listings_live": len(live),
        "recent": listings[:12],
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
