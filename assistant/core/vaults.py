"""Pots of money inside an account no aggregator can see into.

Jack opened a SoFi account with two Vaults — Emergency Fund and Debt — and found they were
not in Era. They never will be: SoFi's own terms say Vaults "are not separate 'accounts,'
but rather they represent internal allocations of the cash which is already in your SoFi
Savings Account." MX is shown one Savings balance and the split is invisible to it.

So the split is tracked here, as savings goals carrying a `current_amount` and a
`funded_from`, and the one thing that makes that trustworthy is the reconciliation below:
the vaults must add up to the balance the bank actually reports. Hand-kept figures drift —
that is their nature — and a drift he cannot see is worse than no figure at all, because he
would plan against it.

Two deliberate rules:

  * **Never create a manual Era account per vault.** The money is already counted inside
    the Savings balance, so mirroring it would inflate net worth by the vault total. With a
    plan built on an emergency-fund target, that is a number he would act on wrongly.
  * **A debt fund is not savings.** Money accumulating for a Wells Fargo settlement is
    already owed to somebody. It is `kind='earmarked'` and is reported apart from the
    savings figure, never added into it.
"""
from __future__ import annotations

import logging

from . import db as core_db

logger = logging.getLogger(__name__)

# Accepted slack between the vaults and the bank's own figure. Not zero: the reported
# balance and his last vault update are snapshots taken at different moments, and a
# transfer in flight is a normal state, not an error worth crying about.
TOLERANCE = 1.00


def vaults(db_path: str, owner_user_id: int, account: str | None = None) -> list[dict]:
    """The goals held inside one account, or every goal that names a host account."""
    out = []
    for goal in core_db.list_savings_goals(db_path, owner_user_id):
        where = goal.get("funded_from") or ""
        if not where:
            continue
        if account and account.lower() not in where.lower():
            continue
        out.append(goal)
    return out


def reconcile(db_path: str, owner_user_id: int, account_balance: float | None,
              account: str = "SoFi") -> dict:
    """Do the tracked vaults add up to what the bank says?

    `account_balance` is the figure the aggregator reports for the host account. None
    means we could not read it, which is reported as such rather than treated as zero — a
    missing balance must never read as "your vaults are over by their whole value".
    """
    held = vaults(db_path, owner_user_id, account)
    tracked = round(sum(float(g.get("current_amount") or 0) for g in held), 2)

    if account_balance is None:
        return {"ok": None, "tracked": tracked, "reported": None, "difference": None,
                "vaults": held,
                "message": (f"{len(held)} vault(s) tracking ${tracked:,.2f}, but the "
                            f"{account} balance could not be read, so this is unverified.")}

    reported = round(float(account_balance), 2)
    difference = round(reported - tracked, 2)
    ok = abs(difference) <= TOLERANCE

    if ok:
        message = f"Vaults reconcile: ${tracked:,.2f} tracked against ${reported:,.2f} at {account}."
    elif difference > 0:
        # Money in the account that no vault claims. Usually fine -- it is the unallocated
        # remainder -- so it is stated, not alarmed about.
        message = (f"${difference:,.2f} in {account} is not in any vault "
                   f"(${tracked:,.2f} tracked of ${reported:,.2f}).")
    else:
        # This is the one that matters: the vaults claim more than the account holds, so
        # at least one figure is stale and he is planning against money that is not there.
        message = (f"The vaults claim ${tracked:,.2f} but {account} only holds "
                   f"${reported:,.2f} — ${abs(difference):,.2f} more than exists. "
                   f"One of the vault figures is out of date.")

    return {"ok": ok, "tracked": tracked, "reported": reported,
            "difference": difference, "vaults": held, "message": message}


def summary(db_path: str, owner_user_id: int) -> dict:
    """Savings and earmarked money, kept apart.

    Adding a debt settlement fund to an emergency fund and calling the total "savings" is
    the specific mistake this exists to prevent: one is a buffer he can use, the other is
    already spoken for.
    """
    goals = core_db.list_savings_goals(db_path, owner_user_id)
    saving = [g for g in goals if (g.get("kind") or "saving") == "saving"]
    earmarked = [g for g in goals if (g.get("kind") or "saving") == "earmarked"]
    total = lambda rows: round(sum(float(g.get("current_amount") or 0) for g in rows), 2)  # noqa: E731
    return {
        "saving_total": total(saving),
        "earmarked_total": total(earmarked),
        "saving": saving,
        "earmarked": earmarked,
        # Deliberately not a combined figure. See the docstring.
        "note": "Earmarked money is already owed and is not part of savings.",
    }
