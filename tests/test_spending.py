"""Sorting a checking account into required and extra.

The design bet is merchant-first: 845 transactions across 96 merchants, so a decision is
recorded against the merchant and applied to everything it ever sold him. These tests
defend the parts that make that bet safe -- a rule must not undo a decision he made by
hand, a re-sync must not undo an afternoon of sorting, and a half-sorted account must not
report itself as a cheap one.
"""
import pytest

from assistant.core import spending


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "spend.db")
    spending.init_spending_db(path)
    return path


def txn(era_id, merchant, amount, date="2026-09-01", outflow=True, category="Shopping"):
    return {"transaction_id": era_id, "account_group_key": "uagr_1",
            "account_name": "Chime account", "amount": amount,
            "description": merchant, "merchant_name": merchant,
            "original_description": merchant.upper(), "category": category,
            "transaction_date": date, "posted_date": date,
            "is_cash_outflow": outflow, "is_pending": False}


def feed(*batches):
    """A fake Era. Each call returns the next page, then empties."""
    pages = list(batches)

    def call_tool(name, args):
        page = args.get("page", 1)
        return {"transactions": pages[page - 1] if page <= len(pages) else []}
    return call_tool


class TestMerchantKey:
    @pytest.mark.parametrize("a,b", [
        ("DOORDASH*ORDER 8837", "DoorDash"),
        ("AMAZON.COM*2H4LK9", "Amazon.com"),
        ("MCDONALD'S #4412", "McDonalds"),
    ])
    def test_one_merchant_however_the_bank_spelled_it(self, a, b):
        """Card networks bolt order ids and store numbers onto the merchant string. Keyed
        on the raw text, a rule per merchant becomes a rule per purchase."""
        assert spending.merchant_key(a) == spending.merchant_key(b)

    def test_different_merchants_stay_different(self):
        assert spending.merchant_key("DoorDash") != spending.merchant_key("Instacart")

    def test_an_empty_name_still_has_a_key(self):
        assert spending.merchant_key(None) == "UNKNOWN"


class TestSync:
    def test_transactions_land(self, db):
        result = spending.sync_from_era(db, feed([txn("t1", "DoorDash", -24.50)]), "uagr_1")
        assert result["added"] == 1
        assert spending.transactions(db)[0]["merchant"] == "DoorDash"

    def test_re_syncing_updates_rather_than_duplicates(self, db):
        spending.sync_from_era(db, feed([txn("t1", "DoorDash", -24.50)]), "uagr_1")
        result = spending.sync_from_era(db, feed([txn("t1", "DoorDash", -26.00)]), "uagr_1")
        assert result["added"] == 0 and result["updated"] == 1
        rows = spending.transactions(db)
        assert len(rows) == 1 and rows[0]["amount"] == -26.00

    def test_a_re_sync_never_undoes_a_classification(self, db):
        """Otherwise an afternoon of sorting is lost to the next refresh."""
        spending.sync_from_era(db, feed([txn("t1", "Rent Co", -1200)]), "uagr_1")
        spending.classify_transaction(db, "t1", "required")
        spending.sync_from_era(db, feed([txn("t1", "Rent Co", -1200)]), "uagr_1")
        assert spending.transactions(db)[0]["necessity"] == "required"

    def test_pages_are_followed(self, db):
        result = spending.sync_from_era(
            db, feed([txn(f"a{i}", "Amazon", -10) for i in range(100)],
                     [txn("b1", "DoorDash", -20)]), "uagr_1", page_size=100)
        assert result["added"] == 101

    def test_money_coming_in_is_not_spending(self, db):
        """Deposits left 'unreviewed' bury the real work under rows there is nothing to
        decide about."""
        spending.sync_from_era(db, feed([txn("t1", "Employer", 2400, outflow=False)]), "uagr_1")
        row = spending.transactions(db)[0]
        assert row["necessity"] == "income" and row["necessity_source"] == "auto"

    def test_an_auto_income_call_is_still_his_to_change(self, db):
        spending.sync_from_era(db, feed([txn("t1", "Refund Co", 30, outflow=False)]), "uagr_1")
        spending.classify_transaction(db, "t1", "extra")
        assert spending.transactions(db)[0]["necessity"] == "extra"


class TestRules:
    def test_one_decision_covers_every_past_purchase(self, db):
        """96 merchants against 845 transactions. This is the whole design."""
        spending.sync_from_era(db, feed([txn(f"t{i}", "DoorDash", -20) for i in range(12)]),
                               "uagr_1")
        spending.set_merchant_rule(db, "DoorDash", "extra")
        assert all(t["necessity"] == "extra" for t in spending.transactions(db))

    def test_a_rule_reaches_transactions_that_arrive_later(self, db):
        spending.set_merchant_rule(db, "DoorDash", "extra")
        spending.sync_from_era(db, feed([txn("t9", "DOORDASH*ORDER 4412", -18)]), "uagr_1")
        assert spending.transactions(db)[0]["necessity"] == "extra"

    def test_a_rule_never_overwrites_a_hand_made_decision(self, db):
        """Amazon is genuinely both things. A rule that undid his per-order corrections on
        every sync would make correcting anything pointless."""
        spending.sync_from_era(db, feed([txn("t1", "Amazon", -15), txn("t2", "Amazon", -300)]),
                               "uagr_1")
        spending.classify_transaction(db, "t2", "required", note="replacement fridge part")
        spending.set_merchant_rule(db, "Amazon", "extra")
        by_id = {t["era_id"]: t for t in spending.transactions(db)}
        assert by_id["t1"]["necessity"] == "extra"
        assert by_id["t2"]["necessity"] == "required", "the manual call stands"
        assert by_id["t2"]["note"] == "replacement fridge part"

    def test_changing_a_rule_re_applies_it(self, db):
        spending.sync_from_era(db, feed([txn("t1", "Gym", -40)]), "uagr_1")
        spending.set_merchant_rule(db, "Gym", "extra")
        spending.set_merchant_rule(db, "Gym", "required")
        assert spending.transactions(db)[0]["necessity"] == "required"

    def test_an_unknown_necessity_is_refused(self, db):
        with pytest.raises(ValueError):
            spending.set_merchant_rule(db, "Gym", "maybe")

    def test_unreviewed_cannot_be_a_rule(self, db):
        """A rule exists to make a decision. "Undecided" is the absence of one."""
        with pytest.raises(ValueError):
            spending.set_merchant_rule(db, "Gym", "unreviewed")


class TestTheWorkList:
    def test_merchants_are_ordered_by_what_they_cost(self, db):
        """The first ten minutes should go to the merchants that decide the budget."""
        spending.sync_from_era(db, feed([
            txn("t1", "Coffee", -4), txn("t2", "Rent Co", -1200), txn("t3", "DoorDash", -60),
        ]), "uagr_1")
        assert [m["merchant"] for m in spending.merchants(db)] == ["Rent Co", "DoorDash", "Coffee"]

    def test_a_merchant_row_carries_its_rule(self, db):
        spending.sync_from_era(db, feed([txn("t1", "Rent Co", -1200)]), "uagr_1")
        spending.set_merchant_rule(db, "Rent Co", "required")
        assert spending.merchants(db)[0]["necessity"] == "required"

    def test_the_unreviewed_filter_shows_only_what_is_left(self, db):
        spending.sync_from_era(db, feed([txn("t1", "Rent Co", -1200), txn("t2", "Gym", -40)]),
                               "uagr_1")
        spending.set_merchant_rule(db, "Rent Co", "required")
        assert [m["merchant"] for m in spending.merchants(db, only_unreviewed=True)] == ["Gym"]


class TestSummary:
    def _sorted_account(self, db):
        spending.sync_from_era(db, feed([
            txn("t1", "Rent Co", -1200), txn("t2", "Utility", -180),
            txn("t3", "DoorDash", -240), txn("t4", "Gym", -40),
            txn("t5", "Employer", 2400, outflow=False),
            txn("t6", "Chime", -500),
        ]), "uagr_1")
        spending.set_merchant_rule(db, "Rent Co", "required")
        spending.set_merchant_rule(db, "Utility", "required")
        spending.set_merchant_rule(db, "DoorDash", "extra")
        spending.set_merchant_rule(db, "Gym", "extra")
        spending.set_merchant_rule(db, "Chime", "transfer")

    def test_required_and_extra_are_separated(self, db):
        self._sorted_account(db)
        s = spending.summary(db)
        assert s["required"] == 1380.0
        assert s["extra"] == 280.0
        assert s["income"] == 2400.0

    def test_a_transfer_is_not_spending(self, db):
        """"Chime" alone appears 432 times on the real account. Counted as spending it
        would dwarf everything real."""
        self._sorted_account(db)
        s = spending.summary(db)
        assert s["transfers"] == 500.0
        assert s["required"] + s["extra"] == 1660.0

    def test_the_unreviewed_remainder_is_reported_not_hidden(self, db):
        """A half-sorted account that reports only what is classified reads as a much
        cheaper life than it is."""
        spending.sync_from_era(db, feed([txn("t1", "Rent Co", -1200), txn("t2", "Mystery", -900)]),
                               "uagr_1")
        spending.set_merchant_rule(db, "Rent Co", "required")
        s = spending.summary(db)
        assert s["unreviewed"] == 900.0
        assert s["reviewed_share"] == pytest.approx(1200 / 2100, rel=1e-3)

    def test_the_extra_share_is_withheld_until_something_is_classified(self, db):
        spending.sync_from_era(db, feed([txn("t1", "Mystery", -900)]), "uagr_1")
        assert spending.summary(db)["extra_share"] is None

    def test_an_empty_account_does_not_divide_by_zero(self, db):
        s = spending.summary(db)
        assert s["required"] == 0 and s["extra_share"] is None


class TestIncomeIsNotSpending:
    """The bug this class exists for: marking a merchant "extra" swept seven payroll
    deposits -- $23,470 of real income -- into extra spending, because the rule engine
    applied a spending judgement to money coming IN. A budget built on that would have
    reported no income at all.
    """

    def test_a_spending_rule_never_touches_money_coming_in(self, db):
        spending.sync_from_era(db, feed([
            txn("t1", "Hyper Sol Payroll", 4182.89, outflow=False),
            txn("t2", "Hyper Sol Payroll", -12.00),
        ]), "uagr_1")
        spending.set_merchant_rule(db, "Hyper Sol Payroll", "extra")
        by_id = {t["era_id"]: t for t in spending.transactions(db)}
        assert by_id["t1"]["necessity"] == "income", "the deposit must stay income"
        assert by_id["t2"]["necessity"] == "extra", "the outflow still follows the rule"

    def test_transfer_and_income_rules_still_apply_both_ways(self, db):
        """Those two describe DIRECTION, not necessity, so they are legitimate on an
        inflow -- a transfer in from savings really is a transfer."""
        spending.sync_from_era(db, feed([
            txn("t1", "Chime", 8755.66, outflow=False), txn("t2", "Chime", -500.00),
        ]), "uagr_1")
        spending.set_merchant_rule(db, "Chime", "transfer")
        assert all(t["necessity"] == "transfer" for t in spending.transactions(db))

    def test_income_survives_a_later_sync(self, db):
        spending.sync_from_era(db, feed([txn("t1", "Payroll", 4182.89, outflow=False)]), "uagr_1")
        spending.set_merchant_rule(db, "Payroll", "required")
        spending.sync_from_era(db, feed([txn("t1", "Payroll", 4182.89, outflow=False)]), "uagr_1")
        assert spending.transactions(db)[0]["necessity"] == "income"

    def test_he_can_still_override_an_inflow_by_hand(self, db):
        """A refund is an inflow he may legitimately want counted differently."""
        spending.sync_from_era(db, feed([txn("t1", "Store", 30, outflow=False)]), "uagr_1")
        spending.classify_transaction(db, "t1", "extra")
        spending.set_merchant_rule(db, "Store", "required")
        assert spending.transactions(db)[0]["necessity"] == "extra"

    def test_an_income_only_merchant_is_not_buried(self, db):
        """Ranked on outflow alone, an employer shows $0.00 spent and sorts to the bottom
        -- which is how the most important row on the screen got mis-clicked."""
        spending.sync_from_era(db, feed([
            txn("t1", "Hyper Sol Payroll", 4182.89, outflow=False),
            txn("t2", "DoorDash", -60.00),
        ]), "uagr_1")
        rows = spending.merchants(db)
        assert rows[0]["merchant"] == "Hyper Sol Payroll"
        assert rows[0]["received"] == 4182.89
