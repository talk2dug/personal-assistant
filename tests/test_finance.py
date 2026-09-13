from datetime import date

from assistant.core.finance import (
    classify_account_type,
    expand_occurrences,
    find_pay_periods,
    goal_progress,
    group_account_balances,
    net_worth,
    project_balance,
    safe_to_spend,
    spendable_balance,
)


def test_no_charges_keeps_balance_flat():
    series = project_balance(1000.0, [], horizon_days=10)
    assert len(series) == 11  # inclusive of day 0
    assert all(p["balance"] == 1000.0 for p in series)


def test_single_one_off_expense_applies_once_on_its_date():
    today = date(2026, 9, 1)
    charges = [{"amount": 100.0, "direction": "expense", "cadence": None, "next_expected_date": "2026-09-05"}]
    series = project_balance(1000.0, charges, horizon_days=10, start_date=today)

    by_date = {p["date"]: p["balance"] for p in series}
    assert by_date["2026-09-04"] == 1000.0
    assert by_date["2026-09-05"] == 900.0
    assert by_date["2026-09-10"] == 900.0  # doesn't repeat


def test_monthly_expense_recurs_within_horizon():
    today = date(2026, 9, 1)
    charges = [{"amount": 50.0, "direction": "expense", "cadence": "monthly", "next_expected_date": "2026-09-01"}]
    series = project_balance(1000.0, charges, horizon_days=65, start_date=today)

    final_balance = series[-1]["balance"]
    # day 0, +30, +60 all fall within a 65-day horizon -> 3 occurrences
    assert final_balance == 1000.0 - 150.0


def test_income_increases_balance():
    today = date(2026, 9, 1)
    charges = [{"amount": 2000.0, "direction": "income", "cadence": "biweekly", "next_expected_date": "2026-09-01"}]
    series = project_balance(0.0, charges, horizon_days=14, start_date=today)

    assert series[0]["balance"] == 2000.0  # day 0 occurrence
    assert series[-1]["balance"] == 4000.0  # day 14 occurrence


def test_unrecognized_cadence_treated_as_one_off():
    today = date(2026, 9, 1)
    charges = [{"amount": 20.0, "direction": "expense", "cadence": "fortnightly-ish", "next_expected_date": "2026-09-03"}]
    series = project_balance(500.0, charges, horizon_days=30, start_date=today)

    assert series[-1]["balance"] == 480.0  # applied exactly once, not repeated


def test_charge_before_start_date_still_projects_forward_occurrences():
    """A charge whose next_expected_date is in the past relative to start_date should
    still generate future occurrences from its cadence, not be skipped entirely."""
    today = date(2026, 9, 15)
    charges = [{"amount": 10.0, "direction": "expense", "cadence": "weekly", "next_expected_date": "2026-09-01"}]
    series = project_balance(100.0, charges, horizon_days=14, start_date=today)

    # occurrences should land on 2026-09-15, 2026-09-22, 2026-09-29 (walked forward from 09-01)
    assert series[-1]["balance"] < 100.0  # at least one occurrence applied


def test_charge_missing_next_expected_date_is_skipped():
    charges = [{"amount": 10.0, "direction": "expense", "cadence": "monthly", "next_expected_date": None}]
    series = project_balance(100.0, charges, horizon_days=10)
    assert all(p["balance"] == 100.0 for p in series)


def test_goal_progress_finds_crossing_date():
    series = [
        {"date": "2026-09-01", "balance": 100.0},
        {"date": "2026-09-02", "balance": 300.0},
        {"date": "2026-09-03", "balance": 500.0},
    ]
    assert goal_progress(series, 250.0) == "2026-09-02"


def test_goal_progress_returns_none_when_never_reached():
    series = [{"date": "2026-09-01", "balance": 100.0}, {"date": "2026-09-02", "balance": 150.0}]
    assert goal_progress(series, 1000.0) is None


def test_expand_occurrences_within_range():
    # "monthly" is a flat 30-day step (not calendar months), so Sept 1 -> Oct 1 -> Oct 31
    charges = [
        {"description": "Netflix", "amount": 15.99, "direction": "expense", "cadence": "monthly", "next_expected_date": "2026-09-01"},
    ]
    occs = expand_occurrences(charges, date(2026, 9, 1), date(2026, 10, 31))
    dates = [o["date"] for o in occs]
    assert dates == ["2026-09-01", "2026-10-01", "2026-10-31"]
    assert occs[0]["description"] == "Netflix"
    assert occs[0]["amount"] == 15.99
    assert occs[0]["direction"] == "expense"


def test_expand_occurrences_excludes_outside_range():
    charges = [
        {"description": "Rent", "amount": 1200.0, "direction": "expense", "cadence": "monthly", "next_expected_date": "2026-01-01"},
    ]
    occs = expand_occurrences(charges, date(2026, 9, 1), date(2026, 9, 30))
    assert len(occs) == 1
    assert date(2026, 9, 1) <= date.fromisoformat(occs[0]["date"]) <= date(2026, 9, 30)


def test_monthly_on_day_lands_on_exact_calendar_day_every_month():
    # This is the bug that prompted adding this cadence: flat 30-day steps drift off a
    # real calendar date (e.g. "the 15th") after a couple of months. This must not drift.
    charges = [{"amount": 4759.86, "direction": "income", "cadence": "monthly_on_day", "next_expected_date": "2026-09-15"}]
    occs = expand_occurrences(charges, date(2026, 9, 1), date(2027, 2, 28))
    dates = [o["date"] for o in occs]
    assert dates == ["2026-09-15", "2026-10-15", "2026-11-15", "2026-12-15", "2027-01-15", "2027-02-15"]


def test_monthly_on_day_clamps_in_short_months():
    charges = [{"amount": 100.0, "direction": "expense", "cadence": "monthly_on_day", "next_expected_date": "2026-01-31"}]
    occs = expand_occurrences(charges, date(2026, 1, 1), date(2026, 4, 30))
    dates = [o["date"] for o in occs]
    # Feb has 28 days in 2026 (not a leap year); day 31 clamps to the actual last day.
    assert dates == ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]


def test_monthly_on_last_day_lands_on_actual_month_end():
    charges = [{"amount": 925.0, "direction": "expense", "cadence": "monthly_on_last_day", "next_expected_date": "2026-09-01"}]
    occs = expand_occurrences(charges, date(2026, 9, 1), date(2027, 2, 1))
    dates = [o["date"] for o in occs]
    assert dates == ["2026-09-30", "2026-10-31", "2026-11-30", "2026-12-31", "2027-01-31"]


def test_semimonthly_pay_via_two_monthly_anchored_charges():
    """The actual real-world case: paid on the 15th and the last day of the month."""
    charges = [
        {"amount": 2000.0, "direction": "income", "cadence": "monthly_on_day", "next_expected_date": "2026-09-15"},
        {"amount": 2000.0, "direction": "income", "cadence": "monthly_on_last_day", "next_expected_date": "2026-09-15"},
    ]
    series = project_balance(0.0, charges, horizon_days=30, start_date=date(2026, 9, 1))
    by_date = {p["date"]: p["balance"] for p in series}
    assert by_date["2026-09-14"] == 0.0
    assert by_date["2026-09-15"] == 2000.0
    assert by_date["2026-09-29"] == 2000.0
    assert by_date["2026-09-30"] == 4000.0


def test_expand_occurrences_sorted_by_date():
    charges = [
        {"description": "B", "amount": 5.0, "direction": "expense", "cadence": None, "next_expected_date": "2026-09-10"},
        {"description": "A", "amount": 5.0, "direction": "expense", "cadence": None, "next_expected_date": "2026-09-05"},
    ]
    occs = expand_occurrences(charges, date(2026, 9, 1), date(2026, 9, 30))
    assert [o["description"] for o in occs] == ["A", "B"]


def test_find_pay_periods_real_semimonthly_pattern_flags_the_current_period():
    """The actual real-world case again (see test_semimonthly_pay_via_two_monthly_anchored_charges
    above): paid on the 15th and the last day of the month. 'today' falls between them."""
    charges = [
        {"description": "Paycheck (15th)", "amount": 2000.0, "direction": "income",
         "cadence": "monthly_on_day", "next_expected_date": "2026-09-15"},
        {"description": "Paycheck (last day)", "amount": 2000.0, "direction": "income",
         "cadence": "monthly_on_last_day", "next_expected_date": "2026-09-30"},
    ]
    periods = find_pay_periods(charges, today=date(2026, 9, 20), horizon_days=45)

    current = next(p for p in periods if p["is_current"])
    assert current["start_date"] == "2026-09-15"
    assert current["end_date"] == "2026-09-30"
    assert current["start_description"] == "Paycheck (15th)"
    assert current["end_description"] == "Paycheck (last day)"

    # The next period (30th -> the following 15th) is also present, just not current.
    assert any(p["start_date"] == "2026-09-30" and not p["is_current"] for p in periods)


def test_find_pay_periods_on_a_payday_itself_treats_that_day_as_the_new_periods_start():
    charges = [
        {"description": "Paycheck (15th)", "amount": 2000.0, "direction": "income",
         "cadence": "monthly_on_day", "next_expected_date": "2026-09-15"},
        {"description": "Paycheck (last day)", "amount": 2000.0, "direction": "income",
         "cadence": "monthly_on_last_day", "next_expected_date": "2026-09-30"},
    ]
    periods = find_pay_periods(charges, today=date(2026, 9, 15), horizon_days=45)
    current = next(p for p in periods if p["is_current"])
    assert current["start_date"] == "2026-09-15"


def test_find_pay_periods_needs_at_least_two_paydays_to_form_a_period():
    charges = [{"description": "Once", "amount": 500.0, "direction": "income",
                "cadence": None, "next_expected_date": "2026-09-15"}]
    assert find_pay_periods(charges, today=date(2026, 9, 20)) == []


def test_find_pay_periods_ignores_expense_charges():
    charges = [
        {"description": "Rent", "amount": 1500.0, "direction": "expense",
         "cadence": "monthly_on_day", "next_expected_date": "2026-09-01"},
        {"description": "Paycheck", "amount": 2000.0, "direction": "income",
         "cadence": "monthly_on_day", "next_expected_date": "2026-09-15"},
    ]
    # Only one income date in range -- still not enough to form a period, even though
    # the expense charge would otherwise supply a second date.
    assert find_pay_periods(charges, today=date(2026, 9, 20), horizon_days=10) == []


def test_classify_account_type_recognizes_liability_keywords():
    assert classify_account_type("Credit Card") == "liability"
    assert classify_account_type("Auto Loan") == "liability"
    assert classify_account_type("Mortgage") == "liability"


def test_classify_account_type_recognizes_investment_keywords():
    assert classify_account_type("401k") == "investment"
    assert classify_account_type("Brokerage") == "investment"
    assert classify_account_type("Roth IRA") == "investment"


def test_classify_account_type_defaults_unknown_and_missing_to_cash():
    assert classify_account_type("Checking") == "cash"
    assert classify_account_type("Savings") == "cash"
    assert classify_account_type("Some Weird Type Era Invents") == "cash"
    assert classify_account_type(None) == "cash"


def test_group_account_balances_buckets_by_type():
    accounts = [
        {"account_type": "Checking", "balance": 500.0},
        {"account_type": "Savings", "balance": 1000.0},
        {"account_type": "401k", "balance": 20000.0},
        {"account_type": "Credit Card", "balance": 300.0},
    ]
    assert group_account_balances(accounts) == {"cash": 1500.0, "investment": 20000.0, "liability": 300.0}


def test_spendable_balance_excludes_investment_and_liability():
    accounts = [
        {"account_type": "Checking", "balance": 500.0},
        {"account_type": "401k", "balance": 20000.0},
        {"account_type": "Credit Card", "balance": 300.0},
    ]
    assert spendable_balance(accounts) == 500.0


def test_net_worth_is_assets_minus_liabilities():
    accounts = [
        {"account_type": "Checking", "balance": 500.0},
        {"account_type": "401k", "balance": 20000.0},
        {"account_type": "Credit Card", "balance": 300.0},
    ]
    result = net_worth(accounts)
    assert result["assets"] == 20500.0
    assert result["liabilities"] == 300.0
    assert result["net_worth"] == 20200.0


def test_safe_to_spend_is_minimum_projected_balance_before_payday_minus_buffer():
    today = date(2026, 9, 20)
    charges = [
        {"description": "Rent", "amount": 900.0, "direction": "expense",
         "cadence": None, "next_expected_date": "2026-09-25"},
        {"description": "Paycheck", "amount": 2000.0, "direction": "income",
         "cadence": None, "next_expected_date": "2026-09-30"},
    ]
    pay_periods = [{"start_date": "2026-09-15", "end_date": "2026-09-30", "is_current": True}]

    result = safe_to_spend(1000.0, charges, pay_periods, safety_buffer=50.0, today=today)

    # balance dips to 100.0 on 09-25 (1000 - 900) and stays there until the 30th
    # (excluded from the window since the payday itself isn't "before payday").
    assert result["minimum_projected_balance"] == 100.0
    assert result["safe_to_spend"] == 50.0
    assert result["payday"] == "2026-09-30"


def test_safe_to_spend_returns_none_without_a_current_period():
    result = safe_to_spend(1000.0, [], pay_periods=[], safety_buffer=0.0, today=date(2026, 9, 20))
    assert result is None


def test_find_pay_periods_collapses_same_day_charges_into_one_boundary():
    """Two income charges landing on the same date (a paycheck and a same-day transfer,
    the real shape era_recurring_charge_cache produces) must not create a zero-length
    period between them."""
    charges = [
        {"description": "Payroll", "amount": 2000.0, "direction": "income",
         "cadence": "monthly", "next_expected_date": "2026-09-15"},
        {"description": "Transfer", "amount": 100.0, "direction": "income",
         "cadence": "monthly", "next_expected_date": "2026-09-15"},
    ]
    periods = find_pay_periods(charges, today=date(2026, 9, 20), horizon_days=45)
    assert all(p["start_date"] != p["end_date"] for p in periods)
    boundary = next(p for p in periods if p["start_date"] == "2026-09-15")
    assert boundary["start_description"] == "Payroll, Transfer"
