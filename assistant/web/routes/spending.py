"""The checking account, item by item, so it can be sorted into required and extra.

Merchant-first by design (see core/spending.py): 845 transactions across 96 merchants, so
the screen leads with merchants ordered by what they cost. Sorting the top ten settles most
of the money; sorting 845 rows is a job nobody finishes.

Reads are cheap and local -- everything is already in SQLite. Only /sync reaches Era, and
it runs in a worker thread because it pages a live API and blocking the event loop here is
the mistake that once froze the whole web server (PR #33).
"""
import asyncio
import functools
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...core import spending
from ..auth import require_owner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/spending", tags=["spending"])


class Classification(BaseModel):
    necessity: str
    note: str | None = None


class SyncRequest(BaseModel):
    account_key: str | None = None


def _era_call(request: Request):
    """A plain call_tool(name, args) -> dict over the Era MCP client.

    core/spending takes this rather than the client itself, so the sync is testable
    without a network and without a live bank connection.
    """
    era = getattr(request.app.state, "era", None)
    client = getattr(era, "mcp_client", None) if era else None
    if client is None:
        raise HTTPException(503, "Era is not connected, so transactions cannot be synced")

    def call(name, args=None):
        result = client.call_tool(name, args or {})
        content = result["content"][0] if isinstance(result, dict) and result.get("content") else result
        if isinstance(content, str):
            try:
                return json.loads(content)
            except ValueError:
                # Era answers a bad request with a plain-text complaint rather than JSON.
                raise HTTPException(502, f"Era refused the request: {content[:200]}")
        return content
    return call


@router.get("")
def overview(request: Request, since: str | None = None, unreviewed: bool = False):
    """Everything the screen needs in one call: the split, and the work list."""
    require_owner(request)
    db_path = request.app.state.cfg.db_path
    return {
        "summary": spending.summary(db_path, since=since),
        "merchants": spending.merchants(db_path, since=since, only_unreviewed=unreviewed),
    }


@router.get("/transactions")
def list_transactions(request: Request, merchant: str | None = None,
                      since: str | None = None, necessity: str | None = None,
                      limit: int = 200):
    """The individual items, for when a merchant is genuinely both things."""
    require_owner(request)
    return {"transactions": spending.transactions(
        request.app.state.cfg.db_path, merchant=merchant, since=since,
        necessity=necessity, limit=min(limit, 500))}


@router.post("/merchants/classify")
def classify_merchant(body: dict, request: Request):
    """Decide once for a merchant. Applies to everything it has ever sold him."""
    require_owner(request)
    merchant = (body.get("merchant") or "").strip()
    necessity = (body.get("necessity") or "").strip()
    if not merchant:
        raise HTTPException(400, "a merchant is required")
    try:
        return spending.set_merchant_rule(
            request.app.state.cfg.db_path, merchant, necessity, body.get("note"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/transactions/{era_id}/classify")
def classify_transaction(era_id: str, body: Classification, request: Request):
    """One item, decided by hand. Outranks the merchant rule, now and on every later sync."""
    require_owner(request)
    try:
        ok = spending.classify_transaction(
            request.app.state.cfg.db_path, era_id, body.necessity, body.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, "no such transaction")
    return {"ok": True}


@router.post("/sync")
async def sync(body: SyncRequest, request: Request):
    """Pull the latest transactions from Era.

    Off the event loop: this pages a live API, and blocking here would hang every other
    request on the server rather than just this one.
    """
    require_owner(request)
    call = _era_call(request)
    account_key = body.account_key
    if not account_key:
        accounts = call("accounts__list_financial_accounts")
        checking = next((a for a in accounts.get("accounts", [])
                         if (a.get("type") or "").lower() == "checking"), None)
        if checking is None:
            raise HTTPException(404, "no checking account is connected to Era")
        account_key = checking["account_group_key"]

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, functools.partial(spending.sync_from_era,
                                request.app.state.cfg.db_path, call, account_key))
    return result


@router.get("/accounts")
def accounts(request: Request):
    """Which accounts Era can see, so the UI can name the one it is showing."""
    require_owner(request)
    call = _era_call(request)
    data = call("accounts__list_financial_accounts")
    return {"accounts": [
        {"key": a.get("account_group_key"), "name": a.get("name"),
         "institution": a.get("institution"), "type": a.get("type"),
         "mask": a.get("account_mask"),
         "balance": (a.get("balance") or {}).get("current")}
        for a in data.get("accounts", [])],
        "warnings": data.get("warnings") or []}
