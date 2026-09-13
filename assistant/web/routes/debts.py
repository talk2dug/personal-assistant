"""REST layer for the debt tracker (personal_db.py's debts + debt_observations).

A sibling of credit.py, which is the closest existing feature -- same db module, same
owner-scoped shape, same conventions (imperative require_owner, no Pydantic, hand-checked
JSON bodies, 400 on a bad value and 404 on a row that isn't his).

The one rule worth stating out loud here, because a REST layer is exactly where it would
get lost: a debt found in his email is a PROPOSAL, not a debt. Every endpoint that totals
or lists debt defaults to tracking_state='tracked' and a proposal has to be asked for
explicitly, so a page that forgets to filter shows him too little rather than reporting a
classifier's guess as money he owes.

Confirming and dismissing proposals deliberately route through PersonalClient rather than
reimplementing the flip here: that path also closes the review card the sweep filed, and
two copies of that would be two chances for a card to dangle on the Review page after the
decision was already made somewhere else.
"""
from fastapi import APIRouter, HTTPException, Request

from ...core import mail_db, personal_db
from ...core.personal_tools import PersonalClient
from ..auth import require_owner

router = APIRouter(prefix="/api/debts", tags=["debts"])


@router.get("")
async def list_debts(request: Request, status: str | None = None, include_proposed: bool = False):
    """His tracked debts with current balance/rate/minimum and provenance attached.

    include_proposed is opt-in on purpose -- see the module docstring.
    """
    user = require_owner(request)
    cfg = request.app.state.cfg
    if status is not None and status not in personal_db.DEBT_STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(personal_db.DEBT_STATUSES)}")
    return personal_db.list_debts(
        cfg.db_path, user["id"],
        tracking_state=None if include_proposed else "tracked", status=status)


@router.get("/summary")
async def debt_summary(request: Request):
    """Totals, the costliest debt, and both suggested payoff orderings. The suggestions are
    suggestions: nothing here writes a priority."""
    user = require_owner(request)
    return personal_db.debt_summary(request.app.state.cfg.db_path, user["id"])


@router.get("/proposals")
async def list_proposals(request: Request):
    """Debts the mail sweep found that he hasn't ruled on yet."""
    user = require_owner(request)
    return personal_db.list_debts(
        request.app.state.cfg.db_path, user["id"], tracking_state="proposed")


@router.get("/sweep-status")
async def sweep_status(request: Request):
    """How far the historical sweep has got through his mail, per folder -- the only honest
    answer to "have you finished looking?", which a resumable backlog pass has to be able
    to give."""
    user = require_owner(request)
    return mail_db.debt_scan_stats(request.app.state.cfg.db_path, user["id"])


@router.post("")
async def create_debt(request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    creditor = (body.get("creditor") or "").strip()
    if not creditor:
        raise HTTPException(400, "creditor is required")
    kind = body.get("kind") or "other"
    if kind not in personal_db.DEBT_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(personal_db.DEBT_KINDS)}")
    due_day = body.get("due_day")
    if due_day is not None and (not isinstance(due_day, int) or not 1 <= due_day <= 31):
        raise HTTPException(400, "due_day must be an integer between 1 and 31")

    debt_id = personal_db.create_debt(
        cfg.db_path, user["id"], creditor, account_last4=body.get("account_last4") or "",
        kind=kind, tracking_state="tracked", origin="manual",
        origin_detail="entered by hand on the Debts page", due_day=due_day,
        notes=body.get("notes"),
    )
    return {"ok": True, "debt_id": debt_id}


@router.put("/{debt_id}")
async def update_debt(debt_id: int, request: Request):
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    due_day = body.get("due_day")
    if due_day is not None and (not isinstance(due_day, int) or not 1 <= due_day <= 31):
        raise HTTPException(400, "due_day must be an integer between 1 and 31")
    try:
        ok = personal_db.update_debt(
            cfg.db_path, user["id"], debt_id, creditor=body.get("creditor"),
            account_last4=body.get("account_last4"), kind=body.get("kind"),
            status=body.get("status"), due_day=due_day, notes=body.get("notes"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, "debt not found, or nothing to change")
    return {"ok": True}


@router.put("/{debt_id}/priority")
async def set_priority(debt_id: int, request: Request):
    """His payoff order. The only writer of debts.priority reachable from the web, and it
    only ever runs because he clicked something -- the suggested orderings on /summary
    never apply themselves."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    priority = body.get("priority")
    if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool) or priority < 1):
        raise HTTPException(400, "priority must be a positive integer, or null to clear it")
    if not personal_db.set_debt_priority(cfg.db_path, user["id"], debt_id, priority):
        raise HTTPException(404, "debt not found")
    return {"ok": True}


@router.get("/{debt_id}/observations")
async def list_observations(debt_id: int, request: Request):
    """Every dated observation behind a debt's current numbers, oldest first -- this is
    the provenance surface: what was seen, when, and whether it came from him or from a
    statement email."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    if personal_db.get_debt(cfg.db_path, user["id"], debt_id) is None:
        raise HTTPException(404, "debt not found")
    return personal_db.list_debt_observations(cfg.db_path, debt_id)


@router.post("/{debt_id}/observations")
async def add_observation(debt_id: int, request: Request):
    """Records a new dated balance/rate/minimum rather than editing the old one, which is
    what keeps the trend real."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    if personal_db.get_debt(cfg.db_path, user["id"], debt_id) is None:
        raise HTTPException(404, "debt not found")

    numbers = {}
    for field in ("balance", "apr", "minimum_payment"):
        value = body.get(field)
        if value is None:
            numbers[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(400, f"{field} must be a number")
        numbers[field] = float(value)
    balance_text = (body.get("balance_text") or "").strip()
    if numbers["balance"] is None and not balance_text and numbers["minimum_payment"] is None:
        raise HTTPException(400, "give a balance, a balance_text, or a minimum_payment to record")

    observation_id = personal_db.add_debt_observation(
        cfg.db_path, debt_id, observed_on=body.get("observed_on"),
        balance_text=balance_text or (f"{numbers['balance']:,.2f}" if numbers["balance"] is not None else ""),
        balance=numbers["balance"],
        apr_text=f"{numbers['apr']}%" if numbers["apr"] is not None else "",
        apr=numbers["apr"],
        minimum_payment_text=(f"{numbers['minimum_payment']:,.2f}"
                              if numbers["minimum_payment"] is not None else ""),
        minimum_payment=numbers["minimum_payment"],
        # Entered by hand on his own page, so it is confirmed by definition -- the
        # distinction the provenance display turns on is inferred-from-email versus
        # stated-by-him, and this is the latter.
        source="manual", source_detail="entered by hand on the Debts page",
        confirmed=True, notes=body.get("notes"),
    )
    return {"ok": True, "observation_id": observation_id}


@router.post("/{debt_id}/resolve")
async def resolve_proposal(debt_id: int, request: Request):
    """His verdict on a debt the mail sweep proposed. Routed through PersonalClient so the
    review card is closed by the same code path the chat tool uses."""
    user = require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    verdict = body.get("verdict")
    if verdict not in ("confirm", "dismiss"):
        raise HTTPException(400, "verdict must be 'confirm' or 'dismiss'")
    result = PersonalClient(cfg.db_path, user["id"]).call_tool(
        "resolve_debt_proposal",
        {"debt_id": debt_id, "verdict": verdict, "note": body.get("note")})
    if "error" in result:
        raise HTTPException(404, result["error"])
    return {"ok": True, "tracking_state": result["tracking_state"]}
