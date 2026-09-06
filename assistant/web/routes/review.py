"""The review queue — everything the team has made that needs the owner's decision.

Approving here doesn't just tick a card: when a review item references a pipeline row
(a product concept, an art brief, a listing, a post), the decision is written through to
that row as well. Otherwise the office would show an approved design that the Art
Director still can't see, which is exactly the kind of quietly-wrong state that makes a
dashboard untrustworthy.
"""
import mimetypes
import pathlib

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ...core import business_db, db
from ...core.business_tools import apply_review_decision
from ...core.engine import execute_pending_action
from ..auth import require_user

router = APIRouter(prefix="/api/review", tags=["review"])


def _owner_id(request: Request) -> int:
    user = require_user(request)
    if user["role"] != "owner":
        raise HTTPException(403, "the review queue is owner-only")
    return user["id"]


@router.get("/items")
async def list_items(request: Request, status: str = "pending", limit: int = 50):
    owner = _owner_id(request)
    cfg = request.app.state.cfg
    return {
        "items": business_db.list_review_items(cfg.db_path, owner, status=status or None, limit=limit),
        "pending": business_db.count_pending_reviews(cfg.db_path, owner),
    }


@router.post("/items/{item_id}/decide")
async def decide(item_id: int, request: Request):
    owner = _owner_id(request)
    cfg = request.app.state.cfg
    body = await request.json()
    decision = body.get("decision")
    if decision not in ("approved", "rejected", "cancelled"):
        raise HTTPException(400, "decision must be approved, rejected or cancelled")

    item = business_db.decide_review_item(
        cfg.db_path, owner, item_id, decision,
        option_id=body.get("option_id"), note=body.get("note"),
    )
    if item is None:
        # Either it doesn't exist or it was already decided — both mean "not yours to
        # decide now", and distinguishing them would leak other users' item ids.
        raise HTTPException(404, "no pending review item with that id")

    ref_table, ref_id = item.get("ref_table"), item.get("ref_id")
    written_through = None

    if ref_table == "pending_actions" and ref_id:
        # A Kroger cart write, a CCXT trade, a mail release, an HA lock/alarm change, or
        # a chat-initiated PR merge — same confirmation gate as always, just resolvable
        # from this page instead of only from the conversation that raised it.
        pending = db.get_pending_action_by_id(cfg.db_path, ref_id)
        if pending is not None and pending["status"] == "awaiting_confirmation":
            db.resolve_pending_action(
                cfg.db_path, pending["id"], "confirmed" if decision == "approved" else "cancelled")
            if decision == "approved":
                try:
                    result = execute_pending_action(
                        pending, era=request.app.state.era, phone=request.app.state.phone,
                        mail=request.app.state.mail, home_assistant=request.app.state.home_assistant,
                        kroger=request.app.state.kroger, ccxt=request.app.state.ccxt,
                        letterstream=request.app.state.letterstream, git_ops=request.app.state.git_ops,
                    )
                    written_through = f"pending_actions#{ref_id} -> executed {pending['tool_name']} ({result})"
                except Exception as e:
                    written_through = f"pending_actions#{ref_id} -> failed: {e}"
            else:
                written_through = f"pending_actions#{ref_id} -> cancelled, nothing happened"
    elif ref_table == "git_pull_requests" and ref_id:
        git_ops = request.app.state.git_ops
        if decision == "approved" and git_ops is not None:
            try:
                result = git_ops.mcp_client.merge_pr(ref_id)
                written_through = (
                    f"git_pull_requests#{ref_id} -> merged" if result.get("ok")
                    else f"git_pull_requests#{ref_id} -> merge failed: {result.get('error')}"
                )
            except Exception as e:
                written_through = f"git_pull_requests#{ref_id} -> merge failed: {e}"
        else:
            written_through = f"git_pull_requests#{ref_id} -> left open on GitHub"
    else:
        business = request.app.state.business
        ssh_ops = getattr(business.mcp_client, "ssh_ops", None) if business is not None else None
        written_through = apply_review_decision(cfg.db_path, owner, item, decision, ssh_ops=ssh_ops)

    return {"item": item, "written_through": written_through}


@router.get("/media/{option_id}")
async def media(option_id: int, request: Request):
    """Serves a generated file belonging to a review option.

    Served through the app rather than a static mount because generated media lives
    outside the web root, and because the path comes from a database row: it is resolved
    and then checked to be inside generated_media_path, so a bad or crafted row can't
    turn this into an arbitrary-file read.
    """
    owner = _owner_id(request)
    cfg = request.app.state.cfg

    stored = business_db.get_review_option_media(cfg.db_path, owner, option_id)
    if not stored:
        raise HTTPException(404, "no media for that option")

    root = pathlib.Path(cfg.generated_media_path).resolve()
    path = pathlib.Path(stored).resolve()
    if not path.is_file():
        raise HTTPException(404, "the file is no longer on disk")
    if root not in path.parents:
        raise HTTPException(403, "that file is outside the generated media directory")

    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)
