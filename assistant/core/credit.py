"""Where his credit actually stands, and what is owed to him by when.

The goal is a mortgage in a few years, which sets the whole shape of this: a score near
800 is not reached by one clever letter, it is reached by utilisation coming down, derogatory
marks ageing off or being removed, and a thin file thickening — over quarters, not weeks.
So everything here is dated, and nothing here reports a number without saying when it was
true.

Two rules the rest of the module exists to enforce.

**A dispute deadline runs from RECEIPT, not from posting.** The FCRA gives a bureau 30
days from when it receives a dispute (45 if the consumer sends additional information
during that window). That is why the letters go certified: with a delivery date the
deadline is provable, and without one it is a guess. A deadline you cannot defend is one
you cannot enforce, so when delivery is unknown this estimates conservatively and says
that it is estimating.

**What a bureau says and what he owes are different facts.** `debts` is money; a tradeline
is a claim about him that may be wrong, stale, or not his. Paying a collection does not
remove it — it can sit there for seven years from the first delinquency either way — so
the two are never merged, and a paid debt is never reported as a fixed report.

Nothing here disputes anything on its own. Every letter is drafted, quoted and only mailed
after he confirms it, because a dispute is a legal assertion made in his name.
"""
import logging
import sqlite3
from contextlib import closing
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# FCRA §611(a)(1)(A): 30 days from receipt, extended to 45 when the consumer supplies
# further information inside the original window.
INVESTIGATION_DAYS = 30
EXTENDED_INVESTIGATION_DAYS = 45

# Used only when USPS has not confirmed delivery yet. Deliberately generous -- guessing
# short produces a deadline that has not really arrived, and chasing a bureau early is how
# a well-founded dispute gets dismissed as premature.
ASSUMED_TRANSIT_DAYS = 5

# Utilisation is about 30% of a FICO score and the fastest thing he can actually move.
# Under 30% stops the bleeding; under 10% is where the points are.
UTILISATION_GOOD = 0.30
UTILISATION_EXCELLENT = 0.10

# A derogatory mark generally falls off seven years after the first delinquency that led
# to it. Knowing the date is the difference between disputing something and simply
# outlasting it -- and outlasting costs nothing.
DEROGATORY_YEARS = 7


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def response_deadline(mailed_at: str | None, delivered_at: str | None = None,
                      extended: bool = False) -> dict | None:
    """When the bureau's answer is due, and how confident that date is.

    Returns the basis alongside the date because they are not equally defensible. A
    deadline computed from a delivery confirmation can be asserted to a bureau; one
    computed from a posting date plus a transit guess cannot, and saying so is the
    difference between a follow-up that lands and one that is brushed off.
    """
    window = EXTENDED_INVESTIGATION_DAYS if extended else INVESTIGATION_DAYS
    delivered = _as_date(delivered_at)
    if delivered is not None:
        return {"due_on": (delivered + timedelta(days=window)).isoformat(),
                "basis": "delivery confirmed", "certain": True, "window_days": window}
    mailed = _as_date(mailed_at)
    if mailed is None:
        return None
    assumed = mailed + timedelta(days=ASSUMED_TRANSIT_DAYS)
    return {"due_on": (assumed + timedelta(days=window)).isoformat(),
            "basis": f"estimated — posted {mailed.isoformat()}, delivery not confirmed",
            "certain": False, "window_days": window}


def falls_off_on(first_delinquency: str | None) -> str | None:
    """When a derogatory mark ages off by law. Free, and often sooner than a dispute."""
    start = _as_date(first_delinquency)
    if start is None:
        return None
    return start.replace(year=start.year + DEROGATORY_YEARS).isoformat()


def utilisation(tradelines: list[dict]) -> dict:
    """Revolving utilisation, overall and per card.

    Per card as well as overall because FICO looks at both, and one maxed card drags the
    score even when the total looks healthy -- which is a fixable thing that an overall
    figure alone would hide.
    """
    cards = [t for t in tradelines
             if t.get("kind") == "credit_card"
             and (t.get("credit_limit") or 0) > 0
             and (t.get("status") or "").lower() not in ("closed",)]
    if not cards:
        return {"overall": None, "cards": [], "total_balance": 0.0, "total_limit": 0.0}

    total_balance = sum(t.get("balance") or 0 for t in cards)
    total_limit = sum(t["credit_limit"] for t in cards)
    per_card = sorted(
        ({"creditor": t["creditor"], "balance": t.get("balance") or 0,
          "limit": t["credit_limit"],
          "ratio": round((t.get("balance") or 0) / t["credit_limit"], 4)}
         for t in cards),
        key=lambda c: c["ratio"], reverse=True)
    return {
        "overall": round(total_balance / total_limit, 4) if total_limit else None,
        "total_balance": round(total_balance, 2),
        "total_limit": round(total_limit, 2),
        "cards": per_card,
        # What paying down to each threshold would cost, which turns "reduce utilisation"
        # into a number he can decide about.
        "to_reach_30": max(0.0, round(total_balance - total_limit * UTILISATION_GOOD, 2)),
        "to_reach_10": max(0.0, round(total_balance - total_limit * UTILISATION_EXCELLENT, 2)),
    }


def latest_report(db_path: str, owner_user_id: int, bureau: str | None = None) -> dict | None:
    query = "SELECT * FROM credit_reports WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if bureau:
        query += " AND bureau = ?"
        params.append(bureau)
    query += " ORDER BY pulled_on DESC, id DESC LIMIT 1"
    with closing(_connect(db_path)) as conn:
        row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        report = dict(row)
        report["tradelines"] = [dict(t) for t in conn.execute(
            "SELECT * FROM credit_tradelines WHERE report_id = ? ORDER BY "
            "  CASE WHEN derogatory IS NOT NULL THEN 0 ELSE 1 END, balance DESC",
            (report["id"],))]
    return report


def open_disputes(db_path: str, owner_user_id: int, today: date | None = None) -> list[dict]:
    """Every dispute that has not been resolved, with its clock attached.

    Ordered by how overdue it is, because an answer that never came is the one piece of
    this he can actually act on today -- a bureau that misses the window has to delete the
    item, and that only happens if somebody notices.
    """
    today = today or date.today()
    with closing(_connect(db_path)) as conn:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM dispute_items WHERE owner_user_id = ? AND status != 'resolved'"
            " ORDER BY created_at", (owner_user_id,))]
        if not items:
            return []
        marks = ",".join("?" * len(items))
        letters: dict[int, list[dict]] = {}
        for row in conn.execute(
                f"SELECT * FROM dispute_letters WHERE dispute_item_id IN ({marks})"
                f" ORDER BY quoted_at", [i["id"] for i in items]):
            letters.setdefault(row["dispute_item_id"], []).append(dict(row))

    out = []
    for item in items:
        sent = [l for l in letters.get(item["id"], []) if l["status"] == "mailed"]
        latest = sent[-1] if sent else None
        deadline = None
        overdue_by = None
        if latest is not None:
            deadline = (
                {"due_on": latest["response_due_at"], "basis": "recorded", "certain": True,
                 "window_days": INVESTIGATION_DAYS}
                if latest.get("response_due_at")
                else response_deadline(latest.get("mailed_at"), latest.get("delivered_at")))
            if deadline and not latest.get("response_received_at"):
                due = _as_date(deadline["due_on"])
                if due is not None:
                    overdue_by = (today - due).days
        out.append({
            **item,
            "letters": letters.get(item["id"], []),
            "letters_sent": len(sent),
            "last_mailed_at": latest["mailed_at"] if latest else None,
            "answered": bool(latest and latest.get("response_received_at")),
            "deadline": deadline,
            # Positive means the window has closed with no answer.
            "overdue_by_days": overdue_by,
        })
    out.sort(key=lambda d: (d["overdue_by_days"] is None, -(d["overdue_by_days"] or 0)))
    return out


def recommendations(db_path: str, owner_user_id: int, status: str | None = None) -> list[dict]:
    query = "SELECT * FROM credit_recommendations WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY priority IS NULL, priority, created_at"
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(query, params)]


def score_history(db_path: str, owner_user_id: int, limit: int = 24) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM credit_score_entries WHERE owner_user_id = ?"
            " ORDER BY recorded_on DESC, id DESC LIMIT ?", (owner_user_id, limit))]


def picture(db_path: str, owner_user_id: int, today: date | None = None) -> dict:
    """Everything about his credit in one object: where he stands, what is in flight,
    what is owed to him by when, and what is worth doing next."""
    today = today or date.today()
    scores = score_history(db_path, owner_user_id)
    report = latest_report(db_path, owner_user_id)
    tradelines = report["tradelines"] if report else []
    disputes = open_disputes(db_path, owner_user_id, today)

    derogatories = [t for t in tradelines if t.get("derogatory")]
    for mark in derogatories:
        mark["falls_off_on"] = mark.get("falls_off_on") or falls_off_on(mark.get("derogatory_on"))

    return {
        "as_of": today.isoformat(),
        "scores": scores,
        "latest_score": scores[0] if scores else None,
        "report": {k: v for k, v in (report or {}).items() if k != "tradelines"} or None,
        "tradelines": tradelines,
        "utilisation": utilisation(tradelines),
        "derogatories": derogatories,
        "disputes": disputes,
        "overdue_disputes": [d for d in disputes
                             if (d["overdue_by_days"] or 0) > 0 and not d["answered"]],
        "recommendations": recommendations(db_path, owner_user_id),
    }


# --- the feed a credit specialist is handed on every run ---------------------------

def _money(value) -> str:
    return f"${value:,.2f}" if isinstance(value, (int, float)) else "unknown"


def _pct(ratio) -> str:
    return f"{ratio * 100:.1f}%" if isinstance(ratio, (int, float)) else "unknown"


def render(pic: dict) -> str:
    """The credit picture as text for an employee that cannot fetch anything itself."""
    lines = [f"CREDIT (exact, as of {pic['as_of']}):"]

    latest = pic.get("latest_score")
    if latest:
        lines.append(f"  score {latest.get('score')} ({latest.get('bureau')}, "
                     f"recorded {str(latest.get('recorded_on'))[:10]})")
        if len(pic["scores"]) > 1:
            previous = pic["scores"][1]
            delta = (latest.get("score") or 0) - (previous.get("score") or 0)
            lines.append(f"    previous {previous.get('score')} on "
                         f"{str(previous.get('recorded_on'))[:10]} ({delta:+d})")
    else:
        lines.append("  NO SCORE ON RECORD. Do not estimate one — say it is unknown and "
                     "ask him to add the next one he sees.")

    report = pic.get("report")
    if report:
        lines.append(f"  latest report: {report.get('bureau')} pulled "
                     f"{str(report.get('pulled_on'))[:10]}"
                     + (f", {report.get('score_model')}" if report.get("score_model") else ""))
    else:
        lines.append("  NO CREDIT REPORT UPLOADED YET — there are no tradelines to work "
                     "from, so nothing below covers utilisation or derogatory marks.")

    util = pic["utilisation"]
    if util.get("overall") is not None:
        lines.append(f"  revolving utilisation {_pct(util['overall'])} "
                     f"({_money(util['total_balance'])} of {_money(util['total_limit'])})")
        lines.append(f"    to reach 30%: pay {_money(util['to_reach_30'])} | "
                     f"to reach 10%: pay {_money(util['to_reach_10'])}")
        for card in util["cards"][:6]:
            lines.append(f"    {card['creditor']}: {_pct(card['ratio'])} "
                         f"({_money(card['balance'])} of {_money(card['limit'])})")

    if pic["derogatories"]:
        lines.append(f"  derogatory marks ({len(pic['derogatories'])}) — each with when it "
                     f"ages off on its own:")
        for mark in pic["derogatories"][:12]:
            falls = mark.get("falls_off_on") or "unknown"
            lines.append(f"    {mark['creditor']} [{mark.get('derogatory')}] "
                         f"{_money(mark.get('balance'))}, first delinquent "
                         f"{mark.get('derogatory_on') or 'unknown'}, falls off {falls}")

    if pic["disputes"]:
        lines.append(f"  disputes in flight ({len(pic['disputes'])}):")
        for dispute in pic["disputes"]:
            when = dispute.get("deadline") or {}
            overdue = dispute.get("overdue_by_days")
            if dispute["answered"]:
                state = "answered"
            elif overdue is not None and overdue > 0:
                state = f"OVERDUE by {overdue}d — the bureau missed its window"
            elif when.get("due_on"):
                state = f"due {when['due_on']}" + ("" if when.get("certain") else " (estimated)")
            else:
                state = "not mailed yet"
            lines.append(f"    [{dispute['bureau']}] {dispute['creditor_name']}: "
                         f"{dispute['item_description'][:60]} — {state}")
    else:
        lines.append("  no disputes in flight.")

    if pic["recommendations"]:
        lines.append("  credit lines suggested so far:")
        for rec in pic["recommendations"][:10]:
            lines.append(f"    [{rec['status']}] {rec['name']} ({rec['kind']}) — "
                         f"{(rec.get('why') or '')[:70]}")

    return "\n".join(lines)


def _cross_bureau_block(db_path: str, owner_user_id: int) -> str:
    """The disagreements between bureaus, which no single report can show.

    Included in the feed because it is where the disputes are: an account one bureau
    reports and another does not, or the same account with different balances, are both
    ordinary grounds for a letter. Confidence is passed through unedited -- a "token"
    match is a guess about two similar names, and a dispute filed on a guess spends a real
    30-day clock.
    """
    from . import credit_match

    try:
        data = credit_match.cross_bureau(db_path, owner_user_id)
    except Exception:                                    # noqa: BLE001
        return ""
    if len(data["bureaus"]) < 2:
        return ""

    lines = [f"  ACROSS BUREAUS ({', '.join(data['bureaus'])}): "
             f"{data['tradelines']} tradelines resolve to {data['accounts']} accounts."]
    if data["balances_disagree"]:
        lines.append("    balances disagree (dispute grounds):")
        for group in data["balances_disagree"][:8]:
            stated = "  ".join(
                f"{b[:3]} ${v:,.0f}" if v is not None else f"{b[:3]} -"
                for b, v in sorted(group["balances"].items()))
            lines.append(f"      {group['creditor'][:34]} [{group['confidence']}] {stated}")
    solid = [g for g in data["missing_from_some"] if g["confidence"] != "token"]
    if solid:
        lines.append("    reported by some bureaus and not others:")
        for group in solid[:8]:
            lines.append(f"      {group['creditor'][:34]} on {','.join(b[:3] for b in group['bureaus'])}"
                         f" / absent from {','.join(b[:3] for b in group['missing_from'])}")
    lines.append("    Name matching is graded: 'exact' is safe, 'prefix' is a truncated "
                 "name, 'token' is a guess. Verify a 'token' match against the reports "
                 "before disputing it.")
    return "\n".join(lines)


def briefing(db_path: str, owner_user_id: int, today=None) -> str:
    """The credit feed block, or an honest failure. Never raises into a staff run."""
    try:
        block = render(picture(db_path, owner_user_id, today))
        extra = _cross_bureau_block(db_path, owner_user_id)
        return block + ("\n" + extra if extra else "")
    except Exception as e:
        logger.exception("credit brief failed")
        return (f"CREDIT: unavailable ({type(e).__name__}). Do not work from remembered "
                f"scores or balances this run — say the feed is down.")
