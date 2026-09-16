"""The money picture a financial planner is handed, and the arithmetic it must not get wrong.

The reason this module exists at all: staff have no tools, so an employee that cannot see
these figures invents them. The reason THESE tests exist is narrower. Two numbers in this
brief are load-bearing and both were wrong in the raw data:

  - 29 tracked debt rows are about 20 real obligations. The mail sweep files a row per
    creditor NAME, and a single Synchrony PayPal card that went to collections appears
    seven times under seven names. Summed naively it reads $17,151; counted once it is
    $10,637. A payoff plan built on the first number is planning against $6,514 that does
    not exist.

  - Most of those balances were last seen in 2022-2023. Only $2,594 has been observed
    within a year. A balance from 3.5 years ago is not a balance.

So: group on the account number (which survives a debt being sold) and never on the name,
and never print a figure without its age.
"""
from datetime import date

import pytest

from assistant.core import finance_brief


def _debt(id, creditor, last4=None, balance=None, observed_on=None, *,
          status="active", kind="credit_card", minimum=None):
    return {"id": id, "creditor": creditor, "account_last4": last4, "kind": kind,
            "status": status, "current_balance": balance, "last_observed_on": observed_on,
            "current_minimum_payment": minimum, "priority": None}


TODAY = date(2026, 9, 15)


class TestGroupingByAccountNumber:
    def test_rows_sharing_an_account_number_are_one_obligation(self):
        """The real case: one Synchrony PayPal card, sold to Jefferson Capital, serviced by
        Unifin -- seven rows, seven names, one account."""
        rows = [_debt(1, "Synchrony Bank", "7560", 743.39, "2023-10-27"),
                _debt(2, "Synchrony Bank (PayPal Credit)", "7560", 831.29, "2023-04-26"),
                _debt(3, "Jefferson Capital Systems LLC", "7560", 1087.92, "2026-08-05"),
                _debt(4, "Unifin Inc (for Jefferson Capital)", "7560", 1087.92, "2026-07-25")]
        result = finance_brief.group_debts(rows, TODAY)
        assert len(result["groups"]) == 1
        assert result["ungrouped"] == []
        assert result["groups"][0]["account_last4"] == "7560"

    def test_the_group_reports_what_naive_summing_would_have_cost(self):
        rows = [_debt(1, "A", "7560", 1000.0, "2026-08-05"),
                _debt(2, "B", "7560", 1000.0, "2026-07-25")]
        group = finance_brief.group_debts(rows, TODAY)["groups"][0]
        assert group["naive_sum"] == 2000.0
        assert group["likely_balance"] == 1000.0

    def test_rows_that_disagree_on_the_balance_say_so(self):
        """Which figure is current is a question for the owner, not something to average."""
        rows = [_debt(1, "Original creditor", "7560", 743.39, "2023-10-27"),
                _debt(2, "Collection agency", "7560", 1087.92, "2026-08-05")]
        group = finance_brief.group_debts(rows, TODAY)["groups"][0]
        assert group["distinct_balances"] == [743.39, 1087.92]

    def test_the_group_is_aged_by_its_freshest_row(self):
        """A 2023 row and a 2026 row for the same account: the debt was seen in 2026."""
        rows = [_debt(1, "Old name", "7560", 743.39, "2023-10-27"),
                _debt(2, "New name", "7560", 1087.92, "2026-08-05")]
        group = finance_brief.group_debts(rows, TODAY)["groups"][0]
        assert group["newest_age_days"] == (TODAY - date(2026, 8, 5)).days

    def test_names_are_never_used_to_group(self):
        """Four Capital One cards are four cards. Grouping on the name would merge them
        and hide three real debts -- the opposite and worse failure."""
        rows = [_debt(1, "Capital One", "4338", 640.90, "2026-01-01"),
                _debt(2, "Capital One", "6656", 187.33, "2026-01-01"),
                _debt(3, "Capital One", "2850", 446.37, "2026-01-01")]
        result = finance_brief.group_debts(rows, TODAY)
        assert result["groups"] == []
        assert len(result["ungrouped"]) == 3

    def test_a_row_without_an_account_number_is_never_guessed_into_a_group(self):
        rows = [_debt(1, "Jefferson Capital", "7560", 1087.92, "2026-08-05"),
                _debt(2, "Jefferson Capital", None, 1087.92, "2026-08-05")]
        result = finance_brief.group_debts(rows, TODAY)
        assert result["groups"] == []
        assert len(result["ungrouped"]) == 2

    def test_an_account_number_seen_once_is_not_a_group(self):
        result = finance_brief.group_debts([_debt(1, "Klarna", "0837", 302.37, "2026-09-04")], TODAY)
        assert result["groups"] == []
        assert len(result["ungrouped"]) == 1


class TestTotalsSayWhatTheyAreTotalsOf:
    """One number would be a lie here. The gap between the three IS the finding."""

    @pytest.fixture
    def picture(self, monkeypatch):
        rows = [
            # One obligation filed three times, seen recently.
            _debt(1, "Original", "7560", 743.39, "2023-10-27"),
            _debt(2, "Agency A", "7560", 1087.92, "2026-08-05"),
            _debt(3, "Agency B", "7560", 1087.92, "2026-07-25"),
            # A genuinely stale standalone debt.
            _debt(4, "Wells Fargo", "3023", 4997.44, "2023-03-11"),
            # A recent standalone debt.
            _debt(5, "Klarna", None, 302.37, "2026-09-04", kind="other"),
            # Never stated as a number.
            _debt(6, "Midland", "7961", None, "2022-08-16"),
        ]
        monkeypatch.setattr(finance_brief.personal_db, "list_debts",
                            lambda *a, **k: rows if k.get("tracking_state") == "tracked" else [])
        return finance_brief.debt_picture("unused.db", 1, TODAY)

    def test_rows_collapse_to_obligations(self, picture):
        assert picture["row_count"] == 6
        assert picture["obligation_count"] == 4

    def test_the_naive_row_sum_is_reported_so_the_error_is_visible(self, picture):
        assert picture["naive_row_sum"] == round(743.39 + 1087.92 + 1087.92 + 4997.44 + 302.37, 2)

    def test_counting_each_obligation_once_is_lower(self, picture):
        assert picture["all_known_balance_floor"] == round(1087.92 + 4997.44 + 302.37, 2)
        assert picture["all_known_balance_floor"] < picture["naive_row_sum"]

    def test_only_recently_seen_balances_are_plannable(self, picture):
        """Wells Fargo's 2023 figure is excluded: it is a memory, not a balance."""
        assert picture["recent_balance_floor"] == round(1087.92 + 302.37, 2)

    def test_balances_that_were_never_numbers_are_counted_as_missing(self, picture):
        assert picture["unknown_balance_count"] == 1


class TestRenderingRefusesToMislead:
    def test_a_stale_balance_is_labelled_as_history_not_a_balance(self):
        assert "YEARS ago" in finance_brief._aged(4997.44, 1284)
        assert "historical" in finance_brief._aged(4997.44, 1284)

    def test_a_recent_balance_carries_its_age(self):
        assert finance_brief._aged(1087.92, 40) == "$1,087.92 as of 40d ago"

    def test_a_balance_that_was_never_a_number_says_exactly_that(self):
        assert "never stated" in finance_brief._aged(None, 30)

    def test_an_undated_balance_does_not_claim_to_be_current(self):
        assert "date unknown" in finance_brief._aged(500.0, None)

    def test_the_brief_names_the_double_counting_when_it_exists(self, monkeypatch):
        rows = [_debt(1, "A", "7560", 1000.0, "2026-08-05"),
                _debt(2, "B", "7560", 1000.0, "2026-07-25")]
        monkeypatch.setattr(finance_brief.personal_db, "list_debts",
                            lambda *a, **k: rows if k.get("tracking_state") == "tracked" else [])
        text = "\n".join(finance_brief._debt_lines(
            finance_brief.debt_picture("unused.db", 1, TODAY), TODAY))
        assert "DOUBLE-COUNTS" in text
        assert "$2,000.00" in text and "$1,000.00" in text

    def test_no_budgets_or_goals_is_stated_as_a_fact_not_left_blank(self):
        picture = {"as_of": "2026-09-15", "accounts": [], "spendable": 0.0, "net_worth": None,
                   "monthly_income_total": 0.0, "monthly_fixed_total": 0.0,
                   "fixed_expenses": [], "income_sources": [], "safe_to_spend": None,
                   "spending_by_category": [], "budgets": [], "savings_goals": [],
                   "debts": {"row_count": 0, "obligation_count": 0, "groups": [], "ungrouped": [],
                             "recent_balance_floor": 0.0, "all_known_balance_floor": 0.0,
                             "naive_row_sum": 0.0, "unknown_balance_count": 0, "proposed_count": 0}}
        text = finance_brief.render(picture)
        assert "nothing is budgeted yet" in text
        assert "nothing is being saved toward yet" in text

    def test_no_connected_accounts_is_stated_rather_than_shown_as_zero(self):
        """A balance of $0.00 and an unknown balance are different facts, and only one of
        them means "you are broke"."""
        picture = {"as_of": "2026-09-15", "accounts": [], "spendable": 0.0, "net_worth": None,
                   "monthly_income_total": 0.0, "monthly_fixed_total": 0.0,
                   "fixed_expenses": [], "income_sources": [], "safe_to_spend": None,
                   "spending_by_category": [], "budgets": [], "savings_goals": [],
                   "debts": {"row_count": 0, "obligation_count": 0, "groups": [], "ungrouped": [],
                             "recent_balance_floor": 0.0, "all_known_balance_floor": 0.0,
                             "naive_row_sum": 0.0, "unknown_balance_count": 0, "proposed_count": 0}}
        assert "NO ACCOUNTS CONNECTED" in finance_brief.render(picture)


class TestMonthlyNormalisation:
    def test_cadences_are_normalised_before_being_added_together(self):
        """A quarterly subscription and a monthly bill cannot be summed as written."""
        charges = [{"amount": 900.0, "cadence": "monthly_on_day"},
                   {"amount": 30.0, "cadence": "quarterly"}]
        assert finance_brief._monthly_total(charges) == pytest.approx(910.0, abs=0.01)

    def test_two_rents_on_different_days_are_both_counted(self):
        """Rent is paid twice a month here, filed as two charges. Counting one is a
        $925 error in the direction that makes the plan look affordable."""
        charges = [{"amount": 925.0, "cadence": "monthly_on_day"},
                   {"amount": 925.0, "cadence": "monthly_on_last_day"}]
        assert finance_brief._monthly_total(charges) == 1850.0

    def test_an_unrecognised_cadence_is_treated_as_monthly(self):
        """Overstating a commitment is the safe direction to be wrong in."""
        assert finance_brief._monthly_total([{"amount": 50.0, "cadence": "whenever"}]) == 50.0


class TestTheFeedNeverBreaksARun:
    def test_a_failure_becomes_an_instruction_not_an_exception(self, monkeypatch):
        """An employee mid-run must be told the feed is down, not handed a traceback --
        and must not fall back on remembered figures."""
        monkeypatch.setattr(finance_brief, "build",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
        text = finance_brief.briefing("unused.db", 1)
        assert "unavailable" in text
        assert "do not plan from" in text.lower()
