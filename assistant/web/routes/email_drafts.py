"""LLM-drafted email replies, over the web UI -- the same email_drafts rows
mail_triage.py's scheduled scan writes, so a draft made in the background shows up here
for the owner to read, edit, or dismiss. Owner-only, like Finance/Crypto/Personal:
reading someone's inbox and draft replies is not something a partner account should see.

Nothing here ever sends anything -- editing a draft's subject/body just rewrites the
row. Actually sending stays on the existing send_email chat tool, which is already
behind the pending_actions confirmation gate (see test_engine_mail.py).
"""
from fastapi import APIRouter, HTTPException, Request

from ...core import mail_db, mail_triage
from ..auth import require_owner

router = APIRouter(prefix="/api/email-drafts", tags=["email-drafts"])


@router.get("")
async def list_drafts(request: Request, status: str | None = None, limit: int = 50):
    user = require_owner(request)
    cfg = request.app.state.cfg
    mail_db.init_mail_db(cfg.db_path)
    return {
        "drafts": mail_db.list_drafts(cfg.db_path, user["id"], status=status, limit=limit),
        "pending": mail_db.count_pending_drafts(cfg.db_path, user["id"]),
    }


@router.get("/{draft_id}")
async def get_draft(draft_id: int, request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    mail_db.init_mail_db(cfg.db_path)
    draft = mail_db.get_draft(cfg.db_path, user["id"], draft_id)
    if draft is None:
        raise HTTPException(404, "no draft with that id")
    return draft


@router.put("/{draft_id}")
async def update_draft(draft_id: int, request: Request):
    """Edits a draft's subject/body before it's sent, or marks it dismissed/sent.
    'sent' here only records that the owner sent it themselves elsewhere -- this
    endpoint never sends anything on its own."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    mail_db.init_mail_db(cfg.db_path)
    body = await request.json()
    status = body.get("status")
    if status is not None and status not in ("drafted", "edited", "dismissed", "sent"):
        raise HTTPException(400, "status must be one of drafted, edited, dismissed, sent")
    ok = mail_db.update_draft(
        cfg.db_path, user["id"], draft_id,
        draft_subject=body.get("draft_subject"), draft_body=body.get("draft_body"), status=status,
    )
    if not ok:
        raise HTTPException(404, "no draft with that id, or nothing to update")
    return {"ok": True, "draft": mail_db.get_draft(cfg.db_path, user["id"], draft_id)}


@router.post("/scan")
async def scan(request: Request, limit: int = 15):
    """Triggers a triage pass on demand, on top of whatever cadence the scheduler runs
    it at (assistant/core/scheduler.py) -- useful right after wiring this up, or to
    check for new replies without waiting for the next tick."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    mail = request.app.state.mail
    llm = request.app.state.llm
    if mail is None:
        raise HTTPException(503, "mail is not configured on this deployment")
    if llm is None:
        raise HTTPException(503, "no LLM backend is configured")
    return mail_triage.run_mail_triage_once(cfg.db_path, llm, mail.mcp_client, user["id"], limit=limit)
