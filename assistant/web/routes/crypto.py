"""Crypto desk dashboard: what the research analyst recommended and why, what the paper
trader holds and why it traded, in one call.

One endpoint rather than several, same reasoning as agents.py's office snapshot: the page
renders one coherent picture per poll, and stitching a portfolio, a trade log and an
analyst report from three requests that each resolved a beat apart would show a state
that was never true at any single instant.

Owner-only, like finance: this is money (simulated, but the desk's judgement calls are
still not partner-facing) and it is read-only — trading happens on the employees' own
schedule or from chat, never from this page.
"""
from ...core import market_data, paper_trading, staff
from ..auth import require_owner
from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/crypto", tags=["crypto"])


def _has_feed(person: dict, feed: str) -> bool:
    return feed in (person.get("data_feeds") or "").split(",")


def _employee_feed(db_path: str, person: dict, limit: int) -> dict:
    """An employee's identity plus its recent runs, reasoning included.

    The full output text is kept, not just a summary: "why" is the entire point of this
    page, and a model's reasoning lives in its prose, not in a field that could be
    extracted from it without loss.
    """
    runs = staff.recent_work(db_path, key=person["key"], limit=limit)
    return {
        "key": person["key"],
        "title": person["title"],
        "status": person["status"],
        "cadence": person["cadence"],
        "interval_minutes": person["interval_minutes"],
        "standing_assignment": person["standing_assignment"],
        "traded": _has_feed(person, "paper"),
        "runs": [
            {
                "id": r["id"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "status": r["status"],
                "output": r["output"],
                "error": r["error"],
            }
            for r in runs
        ],
    }


@router.get("/dashboard")
async def dashboard(request: Request, runs_limit: int = 8, trades_limit: int = 30):
    """Everyone on the market feed, split into who trades and who only researches.

    Membership is read from data_feeds rather than a fixed pair of keys, so hiring a
    second analyst or a second trading desk shows up here without a code change — the
    same reason the roster itself is data rather than a hardcoded agent list.
    """
    require_owner(request)
    db_path = request.app.state.cfg.db_path

    crypto_staff = [p for p in staff.list_staff(db_path) if _has_feed(p, "market")]
    analysts = [_employee_feed(db_path, p, runs_limit) for p in crypto_staff if not _has_feed(p, "paper")]
    traders = [_employee_feed(db_path, p, runs_limit) for p in crypto_staff if _has_feed(p, "paper")]

    try:
        book = paper_trading.performance(db_path)
    except Exception:
        book = None

    return {
        "feed_status": market_data.feed_status(db_path),
        "analysts": analysts,
        "traders": traders,
        "book": book,
        "trades": paper_trading.recent_trades(db_path, limit=trades_limit) if book else [],
        "rejections": paper_trading.recent_rejections(db_path, limit=10) if book else [],
    }
