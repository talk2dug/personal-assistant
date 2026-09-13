"""Pure computation for the finance dashboard: projecting a running balance forward
from recurring income/expenses, and figuring out when a savings goal becomes reachable.

No I/O here — callers pass in the current balance and recurring-charge data (from the
Era cache tables and/or manually-entered bills) and get back a plain series. Keeping
this pure makes it trivial to unit test with synthetic data instead of needing a live
Era connection.
"""
import calendar
from datetime import date, datetime, timedelta

# Cadence is approximated in days for most patterns (e.g. "monthly" = 30 days, not "same
# day next month") — fine for a spending-awareness forecast, not an accounting system.
# The exception is pay/rent-on-specific-calendar-days patterns (see MONTHLY_ON_DAY /
# MONTHLY_ON_LAST_DAY below) — a flat 30-day step drifts away from a real anchored date
# by about a day every two months, which is exactly the bug that prompted adding these.
CADENCE_DAYS = {
    "daily": 1,
    "weekly": 7,
    "biweekly": 14,
    "semimonthly": 15,
    "monthly": 30,
    "quarterly": 91,
    "semiannual": 182,
    "yearly": 365,
    "annual": 365,
}

MONTHLY_ON_DAY = "monthly_on_day"  # same day-of-month every month (clamped to short months)
MONTHLY_ON_LAST_DAY = "monthly_on_last_day"  # the actual last calendar day every month
_MAX_MONTHLY_ITERATIONS = 600  # ~50 years — a safety cap, not a real limit on how far this reaches

# The full set of cadence values project_balance/expand_occurrences understand as real
# recurring patterns (anything else falls back to being treated as a one-off — see
# _occurrence_dates). Defined once here so the REST layer (routes/finance.py) and the
# chat-tool layer (personal_tools.py) both validate manual recurring charges against the
# exact same list instead of each maintaining their own copy that could drift apart.
VALID_CADENCES = frozenset(CADENCE_DAYS) | {MONTHLY_ON_DAY, MONTHLY_ON_LAST_DAY}


def _occurrence_dates(charge: dict, range_start: date, range_end: date) -> list[date]:
    """The dates a recurring charge falls due within [range_start, range_end], regardless
    of which cadence style it uses."""
    next_date = _parse_date(charge.get("next_expected_date"))
    if next_date is None:
        return []
    cadence = (charge.get("cadence") or "").strip().lower()

    if cadence == MONTHLY_ON_DAY:
        return _monthly_on_day_dates(next_date.day, range_start, range_end)
    if cadence == MONTHLY_ON_LAST_DAY:
        return _monthly_on_last_day_dates(range_start, range_end)

    step_days = CADENCE_DAYS.get(cadence)
    if step_days is None:
        return [next_date] if range_start <= next_date <= range_end else []

    dates = []
    occurrence = next_date
    while occurrence < range_start:
        occurrence += timedelta(days=step_days)
    while occurrence <= range_end:
        dates.append(occurrence)
        occurrence += timedelta(days=step_days)
    return dates


def _monthly_on_day_dates(day_of_month: int, range_start: date, range_end: date) -> list[date]:
    """One occurrence per month on day_of_month, clamped to each month's actual length
    (e.g. day_of_month=31 lands on Feb 28/29 in February)."""
    dates = []
    year, month = range_start.year, range_start.month
    for _ in range(_MAX_MONTHLY_ITERATIONS):
        last_day = calendar.monthrange(year, month)[1]
        occurrence = date(year, month, min(day_of_month, last_day))
        if occurrence > range_end:
            break
        if occurrence >= range_start:
            dates.append(occurrence)
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return dates


def _monthly_on_last_day_dates(range_start: date, range_end: date) -> list[date]:
    """One occurrence per month on that month's actual last calendar day."""
    dates = []
    year, month = range_start.year, range_start.month
    for _ in range(_MAX_MONTHLY_ITERATIONS):
        last_day = calendar.monthrange(year, month)[1]
        occurrence = date(year, month, last_day)
        if occurrence > range_end:
            break
        dates.append(occurrence)  # last day of range_start's month is always >= range_start
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return dates


def project_balance(
    current_balance: float, recurring_charges: list[dict], horizon_days: int = 180, start_date: date | None = None,
) -> list[dict]:
    """Returns a daily series [{"date": ISO date, "balance": float}, ...] from start_date
    (default today) through start_date + horizon_days, applying each recurring charge's
    occurrences as they fall due. Positive for income, negative for expenses.

    Each charge dict needs: amount, direction ("income"|"expense"), cadence (a key in
    CADENCE_DAYS, MONTHLY_ON_DAY, or MONTHLY_ON_LAST_DAY — else treated as a one-off),
    next_expected_date (ISO date; for MONTHLY_ON_DAY this also supplies the target day-of-month).
    """
    start = start_date or date.today()
    end = start + timedelta(days=horizon_days)

    occurrences_by_date: dict[date, float] = {}
    for charge in recurring_charges:
        signed_amount = charge["amount"] if charge["direction"] == "income" else -charge["amount"]
        for occurrence in _occurrence_dates(charge, start, end):
            occurrences_by_date[occurrence] = occurrences_by_date.get(occurrence, 0.0) + signed_amount

    series = []
    running = current_balance
    for offset in range(horizon_days + 1):
        day = start + timedelta(days=offset)
        running += occurrences_by_date.get(day, 0.0)
        series.append({"date": day.isoformat(), "balance": round(running, 2)})
    return series


def expand_occurrences(recurring_charges: list[dict], start_date: date, end_date: date) -> list[dict]:
    """Returns individual occurrences of each recurring charge within [start_date, end_date]
    — [{"date", "description", "amount", "direction"}, ...] — for the calendar view, as
    opposed to project_balance's cumulative running total."""
    occurrences = []
    for charge in recurring_charges:
        for when in _occurrence_dates(charge, start_date, end_date):
            occurrences.append(_occurrence(charge, when))

    occurrences.sort(key=lambda o: o["date"])
    return occurrences


def _occurrence(charge: dict, when: date) -> dict:
    return {
        "date": when.isoformat(),
        "description": charge.get("description", ""),
        "amount": charge["amount"],
        "direction": charge["direction"],
    }


def find_pay_periods(income_charges: list[dict], today: date, horizon_days: int = 45) -> list[dict]:
    """Pairs consecutive payday occurrences into periods for meal planning ("plan meals for
    the days between paydays"). Reuses expand_occurrences rather than re-deriving occurrence
    dates -- a period boundary is just two adjacent paydays. Filters to direction == "income"
    itself rather than trusting the caller to pre-filter -- a mixed list (e.g. the raw output
    of list_manual_recurring_charges, which includes bills alongside paychecks) must not let
    an expense's date sneak in as a false payday boundary.

    Looks both backward and forward from today (a horizon_days window each way) so the period
    today actually falls inside has a real start date even when that payday was in the past,
    not just the next upcoming one.

    Needs at least 2 distinct payday dates in the window to produce even one period -- with
    only one, there's no second boundary to pair it with. Multiple charges landing on the same
    date (e.g. a paycheck and a same-day transfer) collapse into one boundary, not two periods
    of length zero; their descriptions join with ", ".

    Returns [{"start_date", "end_date", "start_description", "end_description", "is_current"}, ...]
    sorted by start_date, all dates ISO strings. is_current is True for the one period today
    falls inside (start_date <= today < end_date) -- callers decide what "today" means when
    it's ambiguous (e.g. a planning conversation happening exactly on a payday), this function
    just reports what it finds.
    """
    income_only = [c for c in income_charges if c.get("direction") == "income"]
    start_range = today - timedelta(days=horizon_days)
    end_range = today + timedelta(days=horizon_days)
    occurrences = expand_occurrences(income_only, start_range, end_range)

    by_date: dict[str, list[str]] = {}
    for occ in occurrences:
        by_date.setdefault(occ["date"], []).append(occ["description"])
    paydays = sorted(by_date)

    periods = []
    for i in range(len(paydays) - 1):
        start_str, end_str = paydays[i], paydays[i + 1]
        start_d, end_d = _parse_date(start_str), _parse_date(end_str)
        periods.append({
            "start_date": start_str,
            "end_date": end_str,
            "start_description": ", ".join(by_date[start_str]),
            "end_description": ", ".join(by_date[end_str]),
            "is_current": start_d <= today < end_d,
        })
    return periods


# --- account-type classification (cash vs. investment vs. liability) -------------
#
# Era's own account_type taxonomy isn't documented anywhere reachable from this codebase,
# and the only real values seen in this deployment so far (captured via live testing in
# test_scheduler_era_cache.py) are plain "Checking"/"Savings" -- both cash. So rather than
# hardcode an enum this classifies by keyword match on whatever string Era actually sends,
# and defaults anything unrecognized to "cash" (the safer assumption for a spendable-cash
# total: undercounting a real liability is worse than slightly overcounting an oddly-typed
# account). Revisit this classification if/when a real 401k or credit card account is
# actually connected and its real type string can be seen.
LIABILITY_TYPE_KEYWORDS = ("credit card", "credit", "loan", "mortgage", "line of credit", "debt")
INVESTMENT_TYPE_KEYWORDS = (
    "invest", "401k", "401(k)", "403b", "403(b)", "ira", "retirement", "brokerage", "pension", "hsa",
)


def classify_account_type(account_type: str | None) -> str:
    """Buckets an Era account_type string into 'cash' (spendable — checking/savings/money
    market/anything unrecognized), 'investment' (retirement/brokerage — real money, but not
    liquid for day-to-day spending), or 'liability' (credit cards/loans — owed, not owned)."""
    if not account_type:
        return "cash"
    lowered = account_type.lower()
    if any(k in lowered for k in LIABILITY_TYPE_KEYWORDS):
        return "liability"
    if any(k in lowered for k in INVESTMENT_TYPE_KEYWORDS):
        return "investment"
    return "cash"


def group_account_balances(accounts: list[dict]) -> dict:
    """Sums each account's balance into its classify_account_type() bucket instead of
    blending every account into one number. Liability balances are summed exactly as Era
    reports them (whatever sign convention it uses for "amount owed") -- this deployment
    has never seen a real connected liability account, so callers computing net worth
    should treat this figure as an amount to subtract, not assume it's already negative."""
    totals = {"cash": 0.0, "investment": 0.0, "liability": 0.0}
    for account in accounts:
        bucket = classify_account_type(account.get("account_type"))
        totals[bucket] += account.get("balance") or 0.0
    return {k: round(v, 2) for k, v in totals.items()}


def spendable_balance(accounts: list[dict]) -> float:
    """The part of group_account_balances actually usable to cover upcoming bills --
    excludes illiquid investment/retirement money and liability balances (owed, not
    owned). This is what projections and the "safe to spend" number should start from,
    not a blended total that overstates what's actually available to spend."""
    return group_account_balances(accounts)["cash"]


def net_worth(accounts: list[dict]) -> dict:
    """assets (cash + investment) minus liabilities, plus the group breakdown, for the
    net-worth-over-time history (see db.upsert_net_worth_snapshot)."""
    groups = group_account_balances(accounts)
    assets = round(groups["cash"] + groups["investment"], 2)
    liabilities = groups["liability"]
    return {**groups, "assets": assets, "liabilities": liabilities, "net_worth": round(assets - liabilities, 2)}


# --- pay-period-aware "safe to spend" ---------------------------------------------

def safe_to_spend(
    current_balance: float, recurring_charges: list[dict], pay_periods: list[dict],
    safety_buffer: float = 0.0, today: date | None = None,
) -> dict | None:
    """How much of current_balance can actually be spent right now without the projected
    daily balance (after every known upcoming bill/income) ever dropping below
    safety_buffer before the next payday: the lowest point project_balance's series hits
    between now and the current pay period's end_date, minus safety_buffer.

    pay_periods is meal_plan_db.get_pay_periods's output (or find_pay_periods's own, same
    shape) -- this function doesn't resolve pay periods itself so callers reuse the
    owner's real paycheck-date resolution (which prefers his manual corrections over
    Era's noisier auto-detection) rather than duplicating it here.

    Returns None when there's no current period to bound the window (not enough payday
    data yet -- see find_pay_periods's own "needs at least 2 distinct paydays" note).
    """
    today = today or date.today()
    current_period = next((p for p in pay_periods if p.get("is_current")), None)
    if current_period is None:
        return None
    end_date = _parse_date(current_period["end_date"])
    if end_date is None or end_date <= today:
        return None

    horizon_days = (end_date - today).days
    series = project_balance(current_balance, recurring_charges, horizon_days=horizon_days, start_date=today)
    # Exclude the payday itself: the question is "before the next payday", and the
    # incoming paycheck landing on end_date would otherwise mask a real dip that happens
    # right up until it arrives.
    window = [p for p in series if p["date"] < current_period["end_date"]]
    if not window:
        window = series
    minimum_projected_balance = min(p["balance"] for p in window)
    return {
        "safe_to_spend": round(minimum_projected_balance - safety_buffer, 2),
        "minimum_projected_balance": round(minimum_projected_balance, 2),
        "safety_buffer": safety_buffer,
        "payday": current_period["end_date"],
    }


def goal_progress(projection_series: list[dict], target_amount: float) -> str | None:
    """The first date in the series the projected balance reaches target_amount, or
    None if it's not reached within the projected horizon."""
    for point in projection_series:
        if point["balance"] >= target_amount:
            return point["date"]
    return None


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except (ValueError, TypeError):
        return None
