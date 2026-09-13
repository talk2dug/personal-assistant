"""Uses the exact real Era response shapes captured via live testing against
context.era.app, so a schema drift on Era's side would be caught here too."""
import json

import pytest

from assistant.core import db
from assistant.core.scheduler import refresh_era_cache


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


ACCOUNTS_RESPONSE = {
    "is_error": False,
    "content": [json.dumps({
        "accounts": [
            {
                "account_group_key": "uagr_4e5yVacCNZ0", "name": "Chime Savings Account",
                "type": "Savings", "balance": {"current": 343.14, "available": 343.14, "currency": "USD"},
            },
            {
                "account_group_key": "uagr_2HGCKax1mbS", "name": "Chime account",
                "type": "Checking", "balance": {"current": 263.77, "available": 263.77, "currency": "USD"},
            },
        ],
        "total_count": 2,
    })],
}

RECURRING_RESPONSE = {
    "is_error": False,
    "content": [json.dumps({
        "recurring": [
            {
                "merchant": "Hyper Sol Payroll", "estimated_amount": {"amount": 4759.86, "currency_code": "USD"},
                "frequency": "monthly", "category": "Paychecks", "last_seen": "2026-08-29", "type": "income",
            },
            {
                "merchant": "Netflix", "estimated_amount": {"amount": 27.94, "currency_code": "USD"},
                "frequency": "quarterly", "category": "Utilities", "last_seen": "2026-08-15", "type": "subscription",
            },
        ],
        "monthly_totals": {},
    })],
}


SPENDING_RESPONSE = {
    "is_error": False,
    "content": [json.dumps({
        "period": {"start": "2026-08-03", "end": "2026-09-02", "label": "August 3 - September 2, 2026"},
        "group_by": "category",
        "total_spending": 1327.71,
        "groups": [
            {
                "category_key": "fcat_42pmXjDBL44", "label": "Dining out",
                "amount": 1327.71, "percent_of_total": 12.8, "transaction_count": 63,
            },
        ],
    })],
}

# Exact response shapes for these three haven't been captured via live testing the way the
# ones above were (see db.upsert_era_insight's docstring) -- these fakes are plausible
# shapes only, good enough to confirm refresh_era_cache caches whatever comes back opaquely.
FORECAST_SPENDING_RESPONSE = {
    "is_error": False,
    "content": [json.dumps({"projected_total": 2100.50, "daily_rate": 68.5})],
}

CASH_FLOW_RESPONSE = {
    "is_error": False,
    "content": [json.dumps({"periods": [{"label": "August 2026", "income": 4759.86, "spending": 3200.11}]})],
}

COMPARE_SPENDING_RESPONSE = {
    "is_error": False,
    "content": [json.dumps({"period_a_total": 1327.71, "period_b_total": 1500.0, "delta_pct": -11.5})],
}


class FakeMCPClient:
    def call_tool(self, name, arguments):
        if name == "accounts__list_financial_accounts":
            return ACCOUNTS_RESPONSE
        if name == "transactions__list_recurring_charges":
            return RECURRING_RESPONSE
        if name == "insights__analyze_spending":
            return SPENDING_RESPONSE
        if name == "insights__forecast_spending":
            return FORECAST_SPENDING_RESPONSE
        if name == "insights__get_cash_flow":
            return CASH_FLOW_RESPONSE
        if name == "insights__compare_spending_periods":
            return COMPARE_SPENDING_RESPONSE
        raise ValueError(f"unexpected tool: {name}")


def test_refresh_populates_account_cache(db_path):
    refresh_era_cache(FakeMCPClient(), db_path)

    accounts = db.list_era_accounts(db_path)
    assert len(accounts) == 2
    checking = next(a for a in accounts if a["account_type"] == "Checking")
    assert checking["balance"] == 263.77
    assert checking["available_balance"] == 263.77


def test_refresh_populates_recurring_charge_cache(db_path):
    refresh_era_cache(FakeMCPClient(), db_path)

    charges = db.list_era_recurring_charges(db_path)
    assert len(charges) == 2

    payroll = next(c for c in charges if c["description"] == "Hyper Sol Payroll")
    assert payroll["direction"] == "income"
    assert payroll["amount"] == 4759.86
    assert payroll["next_expected_date"] == "2026-09-28"  # last_seen (08-29) + 30 days (monthly approximation)

    netflix = next(c for c in charges if c["description"] == "Netflix")
    assert netflix["direction"] == "expense"
    assert netflix["next_expected_date"] == "2026-11-14"  # last_seen (08-15) + 91 days (quarterly approximation)


def test_refresh_populates_category_spending_cache_for_both_periods(db_path):
    refresh_era_cache(FakeMCPClient(), db_path)

    this_month = db.list_era_category_spending(db_path, "this_month")
    last_30 = db.list_era_category_spending(db_path, "last_30_days")
    assert len(this_month) == 1
    assert len(last_30) == 1
    assert this_month[0]["label"] == "Dining out"
    assert this_month[0]["amount"] == 1327.71
    assert this_month[0]["transaction_count"] == 63


def test_refresh_is_idempotent_on_rerun(db_path):
    refresh_era_cache(FakeMCPClient(), db_path)
    refresh_era_cache(FakeMCPClient(), db_path)

    assert len(db.list_era_accounts(db_path)) == 2
    assert len(db.list_era_recurring_charges(db_path)) == 2
    assert len(db.list_era_category_spending(db_path, "this_month")) == 1
    # one net-worth snapshot per day, not one per refresh
    assert len(db.list_net_worth_snapshots(db_path)) == 1


def test_refresh_caches_era_insights_opaquely(db_path):
    refresh_era_cache(FakeMCPClient(), db_path)

    insights = db.list_era_insights(db_path)
    assert insights["forecast_spending"]["payload"] == {"projected_total": 2100.50, "daily_rate": 68.5}
    assert insights["cash_flow"]["payload"]["periods"][0]["label"] == "August 2026"
    assert insights["compare_spending_periods"]["payload"]["delta_pct"] == -11.5


def test_refresh_records_a_net_worth_snapshot(db_path):
    refresh_era_cache(FakeMCPClient(), db_path)

    snapshots = db.list_net_worth_snapshots(db_path)
    assert len(snapshots) == 1
    # Both fake accounts are Savings/Checking -- both cash, no investment or liability.
    assert snapshots[0]["cash"] == 343.14 + 263.77
    assert snapshots[0]["investment"] == 0.0
    assert snapshots[0]["liability"] == 0.0
    assert snapshots[0]["net_worth"] == 343.14 + 263.77
