"""The business team's work, grouped by the product it belongs to.

The Review queue answers "what needs a decision"; it cannot answer "what is this part
of". Twenty-eight pending cards turned out to be nine products at different stages, and
nothing in the UI said so. These endpoints read the chain that was already in the schema
(see core/pipelines.py) so the board can show a product from idea to social post as one
thing.

Owner-only throughout: this is the business's unreleased product line, its pricing and
its unpublished artwork.
"""
from fastapi import APIRouter, HTTPException, Request

from ...core import pipelines
from ..auth import require_owner

router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])


def _owner_id(request: Request) -> int:
    from ...core import db as core_db
    cfg = request.app.state.cfg
    owner = next((u for u in cfg.users if u.role == "owner"), None)
    if owner is None:
        raise HTTPException(503, "no owner configured")
    row = core_db.get_user_by_chat_id(cfg.db_path, owner.telegram_chat_id)
    if row is None:
        raise HTTPException(503, "no owner user")
    return row["id"]


@router.get("")
async def list_all(request: Request, market: str | None = None, limit: int = 200):
    """Every pipeline, newest first, with enough to draw the rail and spot what is
    waiting. Summaries only -- the full chain is one more call, because loading ninety
    products' artwork and listings to render a sidebar would be absurd."""
    require_owner(request)
    cfg = request.app.state.cfg
    rows = pipelines.list_pipelines(cfg.db_path, _owner_id(request), market=market, limit=limit)
    return {
        "pipelines": rows,
        "markets": list(pipelines.MARKETS),
        "stages": list(pipelines.STAGES),
        "totals": {
            "all": len(rows),
            "waiting": sum(1 for r in rows if r["pending_reviews"]),
            "local": sum(1 for r in rows if r["market"] == "local"),
            "automated": sum(1 for r in rows if r["market"] == "automated"),
        },
    }


@router.get("/{concept_id}")
async def detail(concept_id: int, request: Request):
    """One pipeline start to finish: the trend that started it, the concept, every art
    brief, listing and social post, and every approval still waiting at each stage."""
    require_owner(request)
    cfg = request.app.state.cfg
    found = pipelines.get_pipeline(cfg.db_path, _owner_id(request), concept_id)
    if found is None:
        raise HTTPException(404, "no such pipeline")
    return found


@router.put("/{concept_id}/market")
async def set_market(concept_id: int, request: Request):
    """Move a product between the local-market and automated tracks.

    The initial split was guessed from how a thing is made (see core/pipelines.py), which
    is a starting point and not a rule -- a design can change route once it proves
    itself, so this has to be editable from the board rather than only in the database.
    """
    require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    market = (body.get("market") or "").strip().lower()
    if market not in pipelines.MARKETS:
        raise HTTPException(400, f"market must be one of {list(pipelines.MARKETS)}")
    if not pipelines.set_market(cfg.db_path, _owner_id(request), concept_id, market):
        raise HTTPException(404, "no such pipeline")
    return {"ok": True, "market": market}
