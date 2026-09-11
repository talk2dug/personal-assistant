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
"""
import asyncio
import functools

from fastapi import APIRouter, HTTPException, Request

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
