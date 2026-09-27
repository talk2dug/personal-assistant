"""One calendar over everything with a date on it.

Bills, paydays, tasks, reminders, dispute deadlines and his real calendar each already had
a screen. None of them answered "what is coming", which is the only question that needs all
six at once -- rent, a payday and a payoff deadline landing in the same week only matter
relative to each other.

Read-only. Every entry keeps its home screen for editing, so this cannot get out of step
with the tables it reads.
"""
import asyncio
from datetime import date, timedelta
import functools

from fastapi import APIRouter, Request

from ...core import agenda
from ..auth import require_owner

router = APIRouter(prefix="/api/agenda", tags=["agenda"])


def _as_date(value: str | None):
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


@router.get("")
async def upcoming(request: Request, days: int = 45, back_days: int = 7,
                   start: str | None = None, end: str | None = None,
                   exclude: str | None = None):
    """Everything dated in the window, grouped by day.

    `start`/`end` (YYYY-MM-DD) pin an absolute range, which is what the calendar's month
    and week views need; without them the relative days/back_days window applies.

    `exclude` is a comma-separated list of kinds to leave out — the Schedule panel passes
    `reminder`, because a reminder is a nudge rather than a commitment with a place in the
    day, and showing both made his schedule read busier than it is.
    """
    owner = require_owner(request)
    loop = asyncio.get_running_loop()

    window_start, window_end = _as_date(start), _as_date(end)
    # A range is capped the same way the relative window is: this walks six sources, one of
    # them a network calendar, so "give me the year 2019" is a request to refuse politely
    # rather than serve slowly.
    if window_start and window_end and (window_end - window_start).days > 400:
        window_end = window_start + timedelta(days=400)

    kinds = tuple(k.strip() for k in (exclude or "").split(",") if k.strip())
    return await loop.run_in_executor(None, functools.partial(
        agenda.upcoming, request.app.state.cfg.db_path, owner["id"],
        days=max(1, min(days, 180)), back_days=max(0, min(back_days, 60)),
        start=window_start, end=window_end, exclude_kinds=kinds,
        calendar_ctx=getattr(request.app.state, "calendar", None)))
