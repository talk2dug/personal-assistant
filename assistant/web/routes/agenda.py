"""One calendar over everything with a date on it.

Bills, paydays, tasks, reminders, dispute deadlines and his real calendar each already had
a screen. None of them answered "what is coming", which is the only question that needs all
six at once -- rent, a payday and a payoff deadline landing in the same week only matter
relative to each other.

Read-only. Every entry keeps its home screen for editing, so this cannot get out of step
with the tables it reads.
"""
import asyncio
import functools

from fastapi import APIRouter, Request

from ...core import agenda
from ..auth import require_owner

router = APIRouter(prefix="/api/agenda", tags=["agenda"])


@router.get("")
async def upcoming(request: Request, days: int = 45, back_days: int = 7):
    """Everything dated in the window, grouped by day.

    Off the event loop: one of the sources is a CalDAV server over the network, and a slow
    one would otherwise hang every other request on the box rather than just this screen.
    """
    owner = require_owner(request)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(
        agenda.upcoming, request.app.state.cfg.db_path, owner["id"],
        days=max(1, min(days, 180)), back_days=max(0, min(back_days, 60)),
        calendar_ctx=getattr(request.app.state, "calendar", None)))
