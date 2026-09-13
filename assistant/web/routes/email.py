"""The Email page: browse/search/read the owner's iCloud inbox and send a new message,
over the web UI. Owner-only, same rule as every other personal-data route.

This is deliberately separate from email_drafts.py, which is narrowly "review/edit
AI-drafted replies to messages mail_triage.py already scanned" -- it has no general
list/search/read/send surface of its own. This module calls the same MailClient
(request.app.state.mail.mcp_client) every other mail surface (chat, email_drafts.py,
mail_triage.py) already goes through, via the same call_tool(name, arguments) dispatch,
so there is exactly one place IMAP/SMTP logic lives.

Send is the one call with a real, external, other-facing consequence, so it follows
credit.py's page-triggered-write precedent rather than chat's pending_actions flow: no
server-side "quote" step exists for an outgoing email the way LetterStream's does for
postage, so the frontend's confirm modal -- showing the exact to/subject/body before this
endpoint is ever called -- IS the explicit approval. Reading (list/search/one message)
has no such gate, same as list_emails/search_emails/read_email are ungated in chat.

mark-read/archive/delete follow the exact same page-triggered-write precedent as send:
a direct click through one of these endpoints IS the confirmation, no pending_actions
gate here -- that gate only exists on the chat tool surface (engine.py's
mail.sensitive_tools), where the model itself can't be trusted to always ask first. The
frontend still puts an are-you-sure step in front of archive/delete (see
ConfirmMailActionModal.jsx) even though this endpoint has no server-side gate of its own,
same as ConfirmSendModal does for send with no further gate behind it either.

junk-log is pure visibility into the *already-autonomous* junk-scan (scheduler.py's
run_mail_junk_scan/record_junk_scan_results) -- it changes nothing and stages nothing,
just reads back what that unattended job has already done.
"""
import asyncio
import functools

from fastapi import APIRouter, HTTPException, Request

from ...core import mail_db
from ..auth import require_owner

router = APIRouter(prefix="/api/email", tags=["email"])


def _mail(request: Request):
    mail = request.app.state.mail
    if mail is None:
        raise HTTPException(503, "mail is not configured on this deployment")
    return mail


async def _call(mail, name: str, arguments: dict) -> dict:
    """MailClient's IMAP/SMTP calls are blocking socket I/O -- routed through a thread
    executor so a slow mail server can't stall uvicorn's event loop, same defensive
    pattern credit.py's _call() uses for LetterStream."""
    loop = asyncio.get_running_loop()
    call = functools.partial(mail.mcp_client.call_tool, name, arguments)
    try:
        return await loop.run_in_executor(None, call)
    except Exception as e:
        raise HTTPException(502, f"mail request failed: {e}")


@router.get("/messages")
async def list_messages(request: Request, folder: str = "INBOX", limit: int = 20, query: str | None = None):
    require_owner(request)
    mail = _mail(request)
    if query:
        return await _call(mail, "search_emails", {"query": query, "folder": folder, "limit": limit})
    return await _call(mail, "list_emails", {"folder": folder, "limit": limit})


@router.get("/messages/{uid}")
async def get_message(uid: str, request: Request, folder: str = "INBOX"):
    require_owner(request)
    mail = _mail(request)
    result = await _call(mail, "read_email", {"uid": uid, "folder": folder})
    if result.get("error"):
        raise HTTPException(404, result["error"])
    return result


@router.post("/send")
async def send_message(request: Request):
    """THE send step -- a real message leaves the account, to a real address, and cannot
    be recalled. By the time this can be called, the page has already shown the owner
    the exact to/subject/body from the compose form; clicking "send" in the confirm
    modal IS the explicit approval, the same precedent as credit.py's mail_letter."""
    require_owner(request)
    mail = _mail(request)
    body = await request.json()
    to = (body.get("to") or "").strip()
    subject = (body.get("subject") or "").strip()
    text = body.get("body") or ""
    if not to:
        raise HTTPException(400, "to is required")
    if "@" not in to:
        raise HTTPException(400, "to does not look like an email address")
    if not subject:
        raise HTTPException(400, "subject is required")
    if not text.strip():
        raise HTTPException(400, "body is required")
    return await _call(mail, "send_email", {"to": to, "subject": subject, "body": text})


@router.post("/messages/{uid}/read")
async def mark_message_read(uid: str, request: Request, folder: str = "INBOX"):
    """Non-destructive and trivially reversible (just the \\Seen flag), so unlike
    archive/delete below this executes on a plain click with no confirm step in the UI --
    matches mark_email_read being ungated in chat too."""
    require_owner(request)
    mail = _mail(request)
    result = await _call(mail, "mark_email_read", {"uid": uid, "folder": folder})
    if result.get("error"):
        raise HTTPException(404, result["error"])
    return result


@router.post("/messages/{uid}/archive")
async def archive_message(uid: str, request: Request, folder: str = "INBOX"):
    """Moves a message into the account's real Archive folder. A direct click through
    this endpoint IS the confirmation (the frontend still shows an are-you-sure step
    first, see ConfirmMailActionModal.jsx) -- no pending_actions gate here, only on the
    chat tool surface."""
    require_owner(request)
    mail = _mail(request)
    result = await _call(mail, "archive_email", {"uid": uid, "folder": folder})
    if result.get("error"):
        raise HTTPException(404, result["error"])
    return result


@router.post("/messages/{uid}/delete")
async def delete_message(uid: str, request: Request, folder: str = "INBOX"):
    """Moves a message to the account's real Trash folder -- copy-then-expunge, never a
    permanent wipe (see mail_client.delete_message's docstring) -- but the frontend still
    requires an explicit are-you-sure step before this is ever called, since it disappears
    from wherever the owner currently sees it without warning otherwise."""
    require_owner(request)
    mail = _mail(request)
    result = await _call(mail, "delete_email", {"uid": uid, "folder": folder})
    if result.get("error"):
        raise HTTPException(404, result["error"])
    return result


@router.get("/junk-log")
async def junk_log(request: Request, limit: int = 50):
    """Read-only audit trail of what the autonomous junk-scan has actually done (see
    scheduler.record_junk_scan_results) -- purely informational, changes nothing."""
    require_owner(request)
    _mail(request)  # 503s the same as every other mail route when mail isn't configured
    cfg = request.app.state.cfg
    mail_db.init_mail_db(cfg.db_path)
    return {"entries": mail_db.list_junk_log(cfg.db_path, limit=limit)}
