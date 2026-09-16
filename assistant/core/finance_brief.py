"""The owner's whole money picture, pre-digested for an employee that cannot fetch it.

Staff have no tools by design -- they gather and propose, they never act -- so a financial
planner with no feed does not plan. It web-searches, invents a budget from national
averages, and reports numbers that have nothing to do with this person. This is the
channel that makes the role possible at all, and it is the same pattern the crypto desk
already uses (see staff.build_feed_briefing).

Everything here is READ-ONLY and assembled from what the system already knows: the Era
account/spending caches, the recurring charges he entered by hand, the debts the mail
sweep found, and the budgets and savings goals he has set. Nothing in this module writes.

Two things it does that a plain data dump would not:

**It groups debts that are one obligation.** The mail sweep files a row per creditor name
it sees, so a single Synchrony PayPal card that went to collections shows up seven times
-- as Synchrony, as Synchrony PayPal Credit, as Unifin collecting for Jefferson Capital,
and as Jefferson Capital four ways. Summed naively that is one debt counted seven times.
`account_last4` is the join that makes this reliable rather than a guess about names: the
account number does not change when the debt is sold.

**It ages every number.** A balance last seen in March 2023 is not a balance, it is a
memory, and a payoff plan built on it is fiction. Every figure carries how old it is, and
the totals are explicitly floors over what is *recent*, separate from what is merely
*known*.
"""
import logging
from datetime import date

from . import db, finance, personal_db

logger = logging.getLogger(__name__)

# Older than this and a balance is reported as historical rather than current. A year is
# generous for a credit card and far too generous for a collection account, but the point
# is not precision -- it is that the planner must never state a 2023 figure as today's.
STALE_AFTER_DAYS = 365

# How far ahead to expand recurring bills. Long enough to cover the next two paycheques
# and the bills between them, which is the window a plan is actually made in.
HORIZON_DAYS = 45


def _age_days(observed_on: str | None, today: date) -> int | None:
    if not observed_on:
        return None
    try:
        return (today - date.fromisoformat(str(observed_on)[:10])).days
    except ValueError:
        return None


def group_debts(debts: list[dict], today: date | None = None) -> dict:
    """Debts collapsed to the obligations they actually represent.

    Grouped on `account_last4`, never on the creditor name. Names change every time a debt
    is sold or handed to a new agency -- that is exactly how one card became seven rows --
    while the account number survives the handoff. Rows with no account number cannot be
    grouped safely and are returned on their own rather than matched on a guess.

    Nothing is merged or deleted here. A group is a claim that these rows LOOK like one
    debt, with the evidence attached so the owner can confirm or reject it; deciding is
    his, and acting on it is done through the debt tools he already has.
    """
    today = today or date.today()
    grouped: dict[str, list[dict]] = {}
    singles: list[dict] = []
    for debt in debts:
        last4 = (debt.get("account_last4") or "").strip()
        if last4:
            grouped.setdefault(last4, []).append(debt)
        else:
            singles.append(debt)

    groups, ungrouped = [], list(singles)
    for last4, rows in sorted(grouped.items()):
        if len(rows) == 1:
            ungrouped.append(rows[0])
            continue
        balances = sorted({r["current_balance"] for r in rows if r["current_balance"] is not None})
        ages = [a for a in (_age_days(r.get("last_observed_on"), today) for r in rows) if a is not None]
        groups.append({
            "account_last4": last4,
            "rows": rows,
            "names": [r["creditor"] for r in rows],
            # The balances the rows disagree about. One value means they agree and the
            # group is almost certainly one debt; several means somebody's figure is out
            # of date, and which one is current is the question to put to him.
            "distinct_balances": balances,
            "newest_age_days": min(ages) if ages else None,
            # What summing the rows naively would have added, versus counting the group
            # once. The difference is the size of the mistake being avoided.
            "naive_sum": round(sum(r["current_balance"] or 0 for r in rows), 2),
            "likely_balance": balances[-1] if balances else None,
        })

    return {"groups": groups, "ungrouped": ungrouped}


def debt_picture(db_path: str, owner_user_id: int, today: date | None = None) -> dict:
    """Debts, grouped and aged, with totals that say what they are totals OF."""
    today = today or date.today()
    debts = personal_db.list_debts(db_path, owner_user_id, tracking_state="tracked")
    active = [d for d in debts if d.get("status") == "active"]
    grouping = group_debts(active, today)

    def _obligation(balance, age):
        return {"balance": balance, "age_days": age}

    obligations = []
    for group in grouping["groups"]:
        obligations.append(_obligation(group["likely_balance"], group["newest_age_days"]))
    for row in grouping["ungrouped"]:
        obligations.append(_obligation(row["current_balance"],
                                       _age_days(row.get("last_observed_on"), today)))

    def _total(predicate):
        return round(sum(o["balance"] for o in obligations
                         if o["balance"] is not None and predicate(o)), 2)

    recent = _total(lambda o: o["age_days"] is not None and o["age_days"] <= STALE_AFTER_DAYS)
    everything = _total(lambda o: True)

    return {
        "obligation_count": len(obligations),
        "row_count": len(active),
        "groups": grouping["groups"],
        "ungrouped": grouping["ungrouped"],
        # Deliberately three numbers, not one. The gap between them IS the finding.
        "recent_balance_floor": recent,
        "all_known_balance_floor": everything,
        "naive_row_sum": round(sum(d["current_balance"] or 0 for d in active), 2),
        "unknown_balance_count": sum(1 for o in obligations if o["balance"] is None),
        "proposed_count": len(personal_db.list_debts(db_path, owner_user_id,
                                                     tracking_state="proposed")),
    }


def build(db_path: str, owner_user_id: int, today: date | None = None) -> dict:
    """The whole picture, as data. Rendered separately so it can be tested as values."""
    today = today or date.today()

    accounts = db.list_era_accounts(db_path)
    charges = (db.list_manual_recurring_charges(db_path, owner_user_id)
               + [c for c in db.list_era_recurring_charges(db_path, include_excluded=False)])

    expenses = [c for c in charges if (c.get("direction") or "expense") == "expense"]
    income = [c for c in charges if c.get("direction") == "income"]

    spendable = finance.spendable_balance(accounts) if accounts else 0.0
    pay_periods = finance.find_pay_periods(income, today, horizon_days=HORIZON_DAYS)
    # Same setting the safe-to-spend tool and the meal planner read, so all three
    # agree on what counts as 'do not go below this'.
    buffer_amount = float(db.get_setting(db_path, db.FINANCE_SAFETY_BUFFER_SETTING, "0") or 0)

    return {
        "as_of": today.isoformat(),
        "accounts": accounts,
        "spendable": spendable,
        "net_worth": finance.net_worth(accounts) if accounts else None,
        "safety_buffer": buffer_amount,
        "fixed_expenses": expenses,
        "income_sources": income,
        "monthly_fixed_total": _monthly_total(expenses),
        "monthly_income_total": _monthly_total(income),
        "upcoming": finance.expand_occurrences(charges, today,
                                               date.fromordinal(today.toordinal() + HORIZON_DAYS)),
        "pay_periods": pay_periods,
        "safe_to_spend": finance.safe_to_spend(spendable, charges, pay_periods, buffer_amount, today),
        "spending_by_category": db.list_era_category_spending(db_path, "last_30_days"),
        "budgets": db.list_budgets(db_path, owner_user_id),
        "savings_goals": db.list_savings_goals(db_path, owner_user_id),
        "debts": debt_picture(db_path, owner_user_id, today),
    }


# Cadences expressed as times per month, so a quarterly subscription and a monthly bill
# can be added together honestly. Anything unrecognised is treated as monthly, which
# over- rather than under-states the commitment -- the safer direction for a plan.
_PER_MONTH = {"daily": 30.0, "weekly": 4.333, "biweekly": 2.167, "semimonthly": 2.0,
              "monthly": 1.0, "monthly_on_day": 1.0, "monthly_on_last_day": 1.0,
              "quarterly": 1 / 3, "semiannual": 1 / 6, "yearly": 1 / 12, "annual": 1 / 12}


def _monthly_total(charges: list[dict]) -> float:
    total = 0.0
    for charge in charges:
        rate = _PER_MONTH.get((charge.get("cadence") or "").strip().lower(), 1.0)
        total += (charge.get("amount") or 0.0) * rate
    return round(total, 2)


def _money(value) -> str:
    return f"${value:,.2f}" if isinstance(value, (int, float)) else "unknown"


def _aged(value, age_days) -> str:
    """A number is only as good as its date, so they are never printed apart."""
    if value is None:
        return "balance never stated as a number"
    if age_days is None:
        return f"{_money(value)} (date unknown)"
    if age_days > STALE_AFTER_DAYS:
        years = age_days / 365.0
        return f"{_money(value)} but last seen {years:.1f} YEARS ago — historical, not current"
    return f"{_money(value)} as of {age_days}d ago"


def render(picture: dict) -> str:
    """The feed block. Written to be read by a model that will otherwise guess."""
    lines = [f"PERSONAL FINANCES (exact, as of {picture['as_of']}):"]

    if picture["accounts"]:
        lines.append(f"  spendable cash {_money(picture['spendable'])}"
                     + (f" | net worth {_money(picture['net_worth']['net_worth'])}"
                        if picture["net_worth"] else ""))
        for acct in picture["accounts"]:
            lines.append(f"    {acct.get('name')} ({acct.get('account_type')}): "
                         f"{_money(acct.get('balance'))}")
    else:
        lines.append("  NO ACCOUNTS CONNECTED — balances unknown. Say so rather than "
                     "assuming a balance.")

    lines.append(f"  committed monthly: income {_money(picture['monthly_income_total'])} "
                 f"vs fixed bills {_money(picture['monthly_fixed_total'])}")
    for charge in picture["fixed_expenses"]:
        lines.append(f"    -{_money(charge.get('amount'))} {charge.get('description')} "
                     f"({charge.get('cadence')}, next {charge.get('next_expected_date')})")
    for charge in picture["income_sources"]:
        lines.append(f"    +{_money(charge.get('amount'))} {charge.get('description')} "
                     f"({charge.get('cadence')}, next {charge.get('next_expected_date')})")

    sts = picture.get("safe_to_spend")
    if sts:
        lines.append(f"  safe to spend before {sts['payday']}: {_money(sts['safe_to_spend'])} "
                     f"(lowest projected balance {_money(sts['minimum_projected_balance'])}, "
                     f"buffer {_money(sts['safety_buffer'])})")
    else:
        lines.append("  safe-to-spend: not computable — not enough payday history yet.")

    if picture["spending_by_category"]:
        synced = picture["spending_by_category"][0].get("last_synced_at") or "unknown"
        lines.append(f"  where it actually went (last 30 days, cached {synced[:10]}):")
        for row in picture["spending_by_category"][:15]:
            lines.append(f"    {row.get('label')}: {_money(row.get('amount'))} "
                         f"over {row.get('transaction_count')} transactions")

    budgets, goals = picture["budgets"], picture["savings_goals"]
    lines.append(f"  budgets set: {len(budgets) or 'NONE — nothing is budgeted yet'}")
    for budget in budgets:
        lines.append(f"    {budget.get('category_label')}: {_money(budget.get('monthly_limit'))}/mo")
    lines.append(f"  savings goals: {len(goals) or 'NONE — nothing is being saved toward yet'}")
    for goal in goals:
        lines.append(f"    {goal.get('name')}: target {_money(goal.get('target_amount'))} "
                     f"by {goal.get('target_date') or 'no date'}")

    lines += _debt_lines(picture["debts"], date.fromisoformat(picture["as_of"]))
    return "\n".join(lines)


def _debt_lines(debts: dict, today: date) -> list[str]:
    lines = [f"  DEBTS — {debts['row_count']} tracked rows that look like "
             f"{debts['obligation_count']} real obligations:"]
    if debts["naive_row_sum"] != debts["all_known_balance_floor"]:
        lines.append(f"    Summing the rows gives {_money(debts['naive_row_sum'])}, but that "
                     f"DOUBLE-COUNTS debts filed more than once. Counting each obligation "
                     f"once: {_money(debts['all_known_balance_floor'])}.")
    lines.append(f"    Seen within a year: {_money(debts['recent_balance_floor'])} "
                 f"— this is the only figure safe to plan against.")
    lines.append(f"    Including balances older than a year: "
                 f"{_money(debts['all_known_balance_floor'])} (ages shown below).")
    if debts["unknown_balance_count"]:
        lines.append(f"    {debts['unknown_balance_count']} obligation(s) have NO balance "
                     f"ever stated as a number — they are missing from every total above.")
    if debts["proposed_count"]:
        lines.append(f"    {debts['proposed_count']} more are unconfirmed guesses, "
                     f"deliberately excluded until he says otherwise.")

    if debts["groups"]:
        lines.append("    Rows that appear to be ONE debt (same account number, different "
                     "creditor names as it was sold on) — needs his confirmation:")
        for group in debts["groups"]:
            lines.append(f"      account ...{group['account_last4']}: "
                         f"{len(group['rows'])} rows — {', '.join(group['names'])}")
            if len(group["distinct_balances"]) > 1:
                lines.append("        rows DISAGREE on the balance: "
                             + ", ".join(_money(b) for b in group["distinct_balances"])
                             + " — which is current is unresolved")
            lines.append(f"        counted once: "
                         f"{_aged(group['likely_balance'], group['newest_age_days'])}"
                         f" (summing the rows would say {_money(group['naive_sum'])})")

    if debts["ungrouped"]:
        lines.append("    Standalone obligations:")
        for row in debts["ungrouped"]:
            age = _age_days(row.get("last_observed_on"), today)
            minimum = row.get("current_minimum_payment")
            extra = f", min payment {_money(minimum)}" if minimum else ""
            lines.append(f"      {row['creditor']} ({row.get('kind') or 'unknown kind'}): "
                         f"{_aged(row.get('current_balance'), age)}{extra}")
    return lines


def briefing(db_path: str, owner_user_id: int, today: date | None = None) -> str:
    """The finance feed block, or an honest failure. Never raises into a staff run."""
    try:
        return render(build(db_path, owner_user_id, today))
    except Exception as e:
        logger.exception("finance brief failed")
        return (f"PERSONAL FINANCES: unavailable ({type(e).__name__}). Do not plan from "
                f"remembered or assumed figures this run — say the feed is down.")
