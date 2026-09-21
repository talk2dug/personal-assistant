"""How fast the store ships, and who gets to decide.

Jack set the terms: *"I only want one product a day for right now, but allow me to tell
jarvis to increase or decrease that number as I see fit"* and *"I dont need to approve what
they make or sell."* So the rate is a dial he turns by talking, and the approval gate is
off by default. Both live here rather than in config.json, because a number he changes by
saying "make it three a day" cannot require an editor and a service restart.

Every change is recorded with who asked and why. That is not bookkeeping for its own sake:
the dashboard has to be able to answer "why did we ship four things on Tuesday", and a bare
setting cannot. A rate that changed without explanation is indistinguishable from a bug.
"""
import json
import logging
from datetime import datetime, timezone

from . import db as core_db

logger = logging.getLogger(__name__)

RATE_KEY = "store.products_per_day"
AUTOPUBLISH_KEY = "store.autopublish"
HISTORY_KEY = "store.policy_history"

DEFAULT_PER_DAY = 1

# A ceiling, not a limit he cannot pass -- but a request for 500 a day is a
# misunderstanding or a typo, and the products are real listings against a real fulfiller.
# Refusing loudly costs one sentence; a silent typo costs a catalogue full of junk.
MAX_PER_DAY = 20

# Kept short deliberately: this is the "why has the rate changed lately" panel, not an
# audit log, and an unbounded list in a settings row grows forever.
HISTORY_LIMIT = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def products_per_day(db_path: str) -> int:
    """How many products the store should launch today. 0 means paused."""
    raw = core_db.get_setting(db_path, RATE_KEY)
    if raw is None:
        return DEFAULT_PER_DAY
    try:
        return max(0, min(MAX_PER_DAY, int(raw)))
    except (TypeError, ValueError):
        # A corrupt setting must not stop the store or, worse, let it run away. Falling
        # back to the default is the only reading that is safe in both directions.
        logger.warning("unreadable %s (%r); falling back to %d", RATE_KEY, raw, DEFAULT_PER_DAY)
        return DEFAULT_PER_DAY


def set_products_per_day(db_path: str, value: int, *, changed_by: str = "owner",
                         reason: str | None = None) -> dict:
    """Turn the dial. Raises ValueError on anything that is not a sane rate."""
    try:
        target = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a number of products per day")
    if target < 0:
        raise ValueError("a negative number of products per day means nothing; use 0 to pause")
    if target > MAX_PER_DAY:
        raise ValueError(
            f"{target} a day is past the {MAX_PER_DAY} ceiling. These are real listings "
            f"against a real fulfiller, so a number this large is usually a typo -- say it "
            f"again if you mean it and the ceiling can be raised.")

    previous = products_per_day(db_path)
    core_db.set_setting(db_path, RATE_KEY, str(target))
    _record(db_path, {"at": _now(), "field": "products_per_day", "from": previous,
                      "to": target, "by": changed_by, "reason": reason})
    return {"previous": previous, "products_per_day": target, "paused": target == 0}


def autopublish(db_path: str) -> bool:
    """Whether the team may list a product without Jack approving it first.

    Defaults True on his explicit instruction. The approval queue still exists and is
    still where genuinely ambiguous calls go; this only governs the ordinary case of
    "we made a thing, it meets the rules, it goes up".
    """
    raw = core_db.get_setting(db_path, AUTOPUBLISH_KEY)
    if raw is None:
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def set_autopublish(db_path: str, enabled: bool, *, changed_by: str = "owner",
                    reason: str | None = None) -> dict:
    previous = autopublish(db_path)
    core_db.set_setting(db_path, AUTOPUBLISH_KEY, "1" if enabled else "0")
    _record(db_path, {"at": _now(), "field": "autopublish", "from": previous,
                      "to": bool(enabled), "by": changed_by, "reason": reason})
    return {"previous": previous, "autopublish": bool(enabled)}


def _record(db_path: str, entry: dict) -> None:
    history = policy_history(db_path)
    history.insert(0, entry)
    core_db.set_setting(db_path, HISTORY_KEY, json.dumps(history[:HISTORY_LIMIT]))


def policy_history(db_path: str) -> list[dict]:
    raw = core_db.get_setting(db_path, HISTORY_KEY)
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, list) else []
    except (TypeError, ValueError):
        return []


def current(db_path: str) -> dict:
    """Everything the dashboard needs to explain the store's current pace."""
    rate = products_per_day(db_path)
    history = policy_history(db_path)
    budget = marketing_budget_cents(db_path)
    return {
        "products_per_day": rate,
        "paused": rate == 0,
        "autopublish": autopublish(db_path),
        "max_per_day": MAX_PER_DAY,
        "marketing_budget_cents": budget,
        "marketing_is_free_only": budget <= 0,
        "last_change": history[0] if history else None,
        "history": history,
    }


# --- marketing money -------------------------------------------------------------------
#
# Jack: "i dont want to use any of my own cash to fund marketing... Once we start
# generating revenue then we can take a portion of the profits to use towards marketing
# funds, if the marketing plan makes sense and can show we will receive an ROI of X."
#
# So the budget starts at zero and is not a number any agent can raise. This is a hard
# guard rather than a line in a prompt, because a prompt is a request and a function that
# returns False is a rule -- and the thing on the other side of it is his bank account.
# The route to a budget is deliberately the same board he already reads: a `purchase`
# request carrying the projected return, which he grants or refuses like anything else.

BUDGET_KEY = "store.marketing_budget_cents"
DEFAULT_BUDGET_CENTS = 0


def marketing_budget_cents(db_path: str) -> int:
    raw = core_db.get_setting(db_path, BUDGET_KEY)
    if raw is None:
        return DEFAULT_BUDGET_CENTS
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        # Unreadable budget means no budget. The failure has to fall toward not spending.
        logger.warning("unreadable %s (%r); treating as zero", BUDGET_KEY, raw)
        return DEFAULT_BUDGET_CENTS


def set_marketing_budget(db_path: str, cents: int, *, changed_by: str = "owner",
                         reason: str | None = None) -> dict:
    """Grant a marketing budget. Owner-only by convention and by call site.

    No agent tool reaches this; it is wired to the owner's own path, so a team that wants
    money has to ask for it on the board and be told yes by a person.
    """
    try:
        target = max(0, int(cents))
    except (TypeError, ValueError):
        raise ValueError(f"{cents!r} is not an amount in cents")
    previous = marketing_budget_cents(db_path)
    core_db.set_setting(db_path, BUDGET_KEY, str(target))
    _record(db_path, {"at": _now(), "field": "marketing_budget_cents", "from": previous,
                      "to": target, "by": changed_by, "reason": reason})
    return {"previous_cents": previous, "budget_cents": target}


def may_spend_on_marketing(db_path: str, cents: int) -> tuple:
    """(allowed, reason). The reason is written to be read by an agent, and to tell it
    what to do instead of spending -- a refusal that does not name the alternative just
    gets worked around."""
    budget = marketing_budget_cents(db_path)
    if budget <= 0:
        return False, (
            "There is no marketing budget: the store runs on free distribution only until "
            "it makes money. Do not spend, and do not ask a platform for paid placement. "
            "If you believe paid promotion is worth it, put it on the owner's board as a "
            "`purchase` request stating the amount, what it buys, and the return you "
            "project with the reasoning behind that number -- he funds it from profit, and "
            "only against a plan that shows an ROI worth having.")
    if cents > budget:
        return False, (f"${cents / 100:,.2f} is more than the ${budget / 100:,.2f} "
                       f"approved marketing budget; ask for the increase on the board first.")
    return True, f"within the ${budget / 100:,.2f} approved budget"
