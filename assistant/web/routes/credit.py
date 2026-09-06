"""Credit score history and the credit-report dispute tracker, over the web UI — the
same personal_db rows the chat tools in personal_tools.py read and write, so a score or
dispute entered in chat shows up here and vice versa. Owner-only, like Finance/Crypto/
Personal.

Scores and dispute-item CRUD are plain personal_db reads/writes, same shape as
personal_tasks.py. The two LetterStream-backed routes (draft/mail) follow grocery.py's
established precedent for page-triggered writes: draft only ever preauths (prices and
queues, never sends or charges -- see letterstream_client.py), so it runs directly, same
as the chat tool. Actually releasing postage (POST /letters/{id}/mail) is the one real
write -- the page must show the owner the recipient, the letter text, and the quoted
cost *before* this endpoint is ever called, and clicking that confirm button is the
explicit approval the assignment requires, the same precedent as grocery.py's
recipe/confirm and Kroger's cart writes.
"""
import asyncio
import functools

from fastapi import APIRouter, HTTPException, Request

from ...core import personal_db
from ...core.personal_tools import BUREAU_ADDRESSES
from ..auth import require_owner

router = APIRouter(prefix="/api/credit", tags=["credit"])

_RECIPIENT_FIELDS = (
    "recipient_name", "recipient_address", "recipient_city", "recipient_state", "recipient_zip",
)


def _letterstream(request: Request):
    letterstream = request.app.state.letterstream
    if letterstream is None:
        raise HTTPException(503, "LetterStream is not configured")
    return letterstream


async def _call(letterstream, name: str, arguments: dict) -> dict:
    """LetterStreamTools.call_tool is a plain blocking call (httpx.post) -- routed
    through a thread executor so a slow LetterStream response can't stall uvicorn's
    event loop, same defensive pattern grocery.py uses for Kroger's stdio client."""
    loop = asyncio.get_running_loop()
    call = functools.partial(letterstream.mcp_client.call_tool, name, arguments)
    try:
        return await loop.run_in_executor(None, call)
    except Exception as e:
        raise HTTPException(502, f"LetterStream call failed: {e}")


# --- credit scores -------------------------------------------------------------

@router.get("/scores")
async def list_scores(request: Request, bureau: str | None = None):
    user = require_owner(request)
    cfg = request.app.state.cfg
    return personal_db.list_credit_score_entries(cfg.db_path, user["id"], bureau)


@router.post("/scores")
async def add_score(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    bureau = body.get("bureau")
    score = body.get("score")
    if bureau not in ("experian", "equifax", "transunion", "other"):
        raise HTTPException(400, "bureau must be experian, equifax, transunion, or other")
    if not isinstance(score, int) or not (300 <= score <= 850):
        raise HTTPException(400, "score must be an integer between 300 and 850")
    entry_id = personal_db.create_credit_score_entry(
        cfg.db_path, user["id"], bureau, score, body.get("recorded_on"), body.get("source"), body.get("notes"))
    return {"ok": True, "entry_id": entry_id}


@router.delete("/scores/{entry_id}")
async def delete_score(entry_id: int, request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    ok = personal_db.delete_credit_score_entry(cfg.db_path, user["id"], entry_id)
    if not ok:
        raise HTTPException(404, "credit score entry not found")
    return {"ok": True}


# --- dispute items ---------------------------------------------------------------

@router.get("/disputes")
async def list_disputes(request: Request, status: str | None = None, bureau: str | None = None):
    user = require_owner(request)
    cfg = request.app.state.cfg
    return personal_db.list_dispute_items(cfg.db_path, user["id"], status, bureau)


@router.post("/disputes")
async def create_dispute(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    bureau = body.get("bureau")
    creditor_name = (body.get("creditor_name") or "").strip()
    item_description = (body.get("item_description") or "").strip()
    reason = (body.get("reason") or "").strip()
    if bureau not in ("experian", "equifax", "transunion", "other"):
        raise HTTPException(400, "bureau must be experian, equifax, transunion, or other")
    if not creditor_name or not item_description or not reason:
        raise HTTPException(400, "creditor_name, item_description, and reason are required")
    dispute_id = personal_db.create_dispute_item(
        cfg.db_path, user["id"], bureau, creditor_name, item_description, reason,
        body.get("account_reference"))
    return {"ok": True, "dispute_item_id": dispute_id}


@router.put("/disputes/{dispute_id}")
async def update_dispute(dispute_id: int, request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    status = body.get("status")
    if status is not None and status not in ("drafted", "mailed", "resolved"):
        raise HTTPException(400, "status must be drafted, mailed, or resolved")
    ok = personal_db.update_dispute_item(
        cfg.db_path, user["id"], dispute_id, status=status, resolution=body.get("resolution"),
        creditor_name=body.get("creditor_name"), item_description=body.get("item_description"),
        reason=body.get("reason"), account_reference=body.get("account_reference"))
    if not ok:
        raise HTTPException(404, "dispute item not found or not permitted")
    return {"ok": True}


# --- dispute letters (LetterStream) ---------------------------------------------

@router.get("/disputes/{dispute_id}/letters")
async def list_letters(dispute_id: int, request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    return personal_db.list_dispute_letters(cfg.db_path, user["id"], dispute_id)


@router.post("/disputes/{dispute_id}/letters/draft")
async def draft_letter(dispute_id: int, request: Request):
    """Quotes a dispute letter through LetterStream's preauth step -- prices and queues
    it, never sends or charges anything (see letterstream_client.py). Safe to call
    freely, same as the chat tool draft_dispute_letter.
    """
    user = require_owner(request)
    cfg = request.app.state.cfg
    letterstream = _letterstream(request)

    item = personal_db.get_dispute_item(cfg.db_path, user["id"], dispute_id)
    if item is None:
        raise HTTPException(404, "dispute item not found")

    body = await request.json()
    letter_text = (body.get("letter_text") or "").strip()
    if not letter_text:
        raise HTTPException(400, "letter_text is required")

    recipient = {f: body.get(f) for f in _RECIPIENT_FIELDS}
    recipient["recipient_address_2"] = body.get("recipient_address_2", "")
    if not recipient["recipient_name"]:
        default = BUREAU_ADDRESSES.get(item["bureau"])
        if default is None:
            raise HTTPException(400, (
                "recipient_name/recipient_address/recipient_city/recipient_state/recipient_zip "
                "are required when the dispute item's bureau is 'other'"
            ))
        recipient = {**default, "recipient_address_2": ""}
    missing = [f for f in _RECIPIENT_FIELDS if not recipient.get(f)]
    if missing:
        raise HTTPException(400, f"missing recipient fields: {', '.join(missing)}")

    mail_type = body.get("mail_type", "certified")
    quote = await _call(letterstream, "letterstream_send_mail", {
        "letter_text": letter_text, "mail_type": mail_type, **recipient,
    })
    letter_id = personal_db.create_dispute_letter(
        cfg.db_path, dispute_id, letter_text=letter_text,
        recipient_name=recipient["recipient_name"], recipient_address=recipient["recipient_address"],
        recipient_address_2=recipient.get("recipient_address_2") or None,
        recipient_city=recipient["recipient_city"], recipient_state=recipient["recipient_state"],
        recipient_zip=recipient["recipient_zip"], mail_type=mail_type,
        quoted_cost=quote.get("cost"), authcode=quote.get("authcode"),
        job_name=quote.get("job"), doc_id=quote.get("doc_id"),
    )
    return {"ok": True, "dispute_letter_id": letter_id, "quote": quote}


@router.post("/letters/{letter_id}/mail")
async def mail_letter(letter_id: int, request: Request):
    """THE hard-confirm step: the one call that spends real money and puts a real,
    unrecallable piece of mail in the USPS system. By the time this endpoint can be
    called, the page has already shown the owner the exact recipient, the exact letter
    text, and the quoted cost from the draft step -- clicking "mail it" here IS the
    explicit approval, same precedent as grocery.py's recipe/confirm. No pending_actions
    row is created (this isn't chat), but the guarantee is the same one
    test_engine_letterstream.py proves for the chat path: nothing reaches
    letterstream_authorize_mail except through an explicit, informed action.
    """
    user = require_owner(request)
    cfg = request.app.state.cfg
    letterstream = _letterstream(request)

    letter = personal_db.get_dispute_letter(cfg.db_path, user["id"], letter_id)
    if letter is None:
        raise HTTPException(404, "dispute letter not found")
    if letter["status"] == "mailed":
        raise HTTPException(409, "this letter has already been mailed")
    if not letter.get("authcode"):
        raise HTTPException(409, "no authcode on file for this letter -- draft it again")

    body = await request.json()
    expected_cost = body.get("expected_cost")
    if expected_cost is not None and str(expected_cost) != str(letter.get("quoted_cost")):
        raise HTTPException(409, "the quoted cost has changed -- refresh and re-confirm before mailing")

    result = await _call(letterstream, "letterstream_authorize_mail", {"authcode": letter["authcode"]})
    personal_db.mark_dispute_letter_mailed(cfg.db_path, user["id"], letter_id)
    return {"ok": True, "result": result}


@router.post("/letters/{letter_id}/track")
async def track_letter(letter_id: int, request: Request):
    """Read-only USPS tracking/status lookup for anything actually mailed."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    letterstream = _letterstream(request)

    letter = personal_db.get_dispute_letter(cfg.db_path, user["id"], letter_id)
    if letter is None:
        raise HTTPException(404, "dispute letter not found")

    result = await _call(letterstream, "letterstream_track_mail", {
        "tracking_number": letter.get("tracking_number"), "doc_id": letter.get("doc_id"), "kind": "track",
    })
    cert = result.get("cert") or result.get("tracking_number")
    if cert and cert != letter.get("tracking_number"):
        personal_db.update_dispute_letter_tracking(cfg.db_path, user["id"], letter_id, cert)
    return {"ok": True, "tracking": result}
