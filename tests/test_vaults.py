"""Money in a pot no aggregator can see into.

Jack opened SoFi Vaults and found they were missing from Era. They always will be: SoFi's
terms say a Vault is an internal allocation of the Savings account, not an account, so the
feed reports one combined balance.

So the split is hand-kept, and hand-kept figures drift. The tests that matter are about
noticing the drift, and about never letting a debt fund be counted as savings.
"""
import pytest

from assistant.core import db as core_db, vaults


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "v.db")
    core_db.init_db(path)
    core_db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner(db):
    return core_db.get_user_by_chat_id(db, "111")["id"]


def make(db, owner, name, current, target=1000.0, kind="saving", where="SoFi Vault"):
    return core_db.create_savings_goal(db, owner, name, target, current_amount=current,
                                       kind=kind, funded_from=where)


class TestAGoalNowKnowsWhatIsInIt:
    def test_a_balance_survives_the_round_trip(self, db, owner):
        make(db, owner, "Emergency Fund", 500.0, target=11130.0)
        goal = core_db.list_savings_goals(db, owner)[0]
        assert goal["current_amount"] == 500.0
        assert goal["remaining"] == 10630.0
        assert goal["progress"] == pytest.approx(0.0449, abs=0.001)

    def test_progress_on_a_zero_target_is_none_not_a_crash(self, db, owner):
        core_db.create_savings_goal(db, owner, "someday", 0.0)
        assert core_db.list_savings_goals(db, owner)[0]["progress"] is None

    def test_updating_sets_the_balance_rather_than_adding_to_it(self, db, owner):
        """He says "there's 600 in the emergency vault", not "add 600" -- taking it as a
        delta would silently double every correction."""
        gid = make(db, owner, "Emergency Fund", 500.0)
        core_db.update_savings_goal(db, gid, current_amount=600.0)
        assert core_db.list_savings_goals(db, owner)[0]["current_amount"] == 600.0

    def test_an_invalid_kind_is_refused(self, db, owner):
        with pytest.raises(ValueError):
            core_db.create_savings_goal(db, owner, "x", 10.0, kind="whatever")


class TestADebtFundIsNotSavings:
    def test_the_two_totals_are_reported_apart(self, db, owner):
        """Adding a settlement fund to an emergency fund and calling it savings would
        flatter the one number the payoff plan turns on."""
        make(db, owner, "Emergency Fund", 800.0, kind="saving")
        make(db, owner, "Wells Fargo settlement", 1200.0, kind="earmarked")

        summary = vaults.summary(db, owner)
        assert summary["saving_total"] == 800.0
        assert summary["earmarked_total"] == 1200.0
        assert "combined" not in summary and "total" not in summary

    def test_a_pre_existing_goal_defaults_to_saving(self, db, owner):
        core_db.create_savings_goal(db, owner, "old goal", 100.0)
        assert vaults.summary(db, owner)["saving_total"] == 0.0
        assert core_db.list_savings_goals(db, owner)[0]["kind"] == "saving"


class TestReconciliation:
    def test_matching_vaults_reconcile(self, db, owner):
        make(db, owner, "Emergency", 300.0)
        make(db, owner, "Debt", 200.0, kind="earmarked")
        result = vaults.reconcile(db, owner, 500.0, "SoFi")
        assert result["ok"] is True and result["difference"] == 0.0

    def test_unallocated_money_is_stated_not_alarmed_about(self, db, owner):
        """Money in the account that no vault claims is the normal remainder."""
        make(db, owner, "Emergency", 300.0)
        result = vaults.reconcile(db, owner, 500.0, "SoFi")
        assert result["ok"] is False and result["difference"] == 200.0
        assert "not in any vault" in result["message"]

    def test_vaults_claiming_more_than_exists_is_called_out(self, db, owner):
        """The one that matters: he would be planning against money that is not there."""
        make(db, owner, "Emergency", 900.0)
        result = vaults.reconcile(db, owner, 500.0, "SoFi")
        assert result["ok"] is False and result["difference"] == -400.0
        assert "more than exists" in result["message"]
        assert "out of date" in result["message"]

    def test_a_small_difference_is_tolerated(self, db, owner):
        """A transfer in flight is a normal state, not an error worth crying about."""
        make(db, owner, "Emergency", 500.0)
        assert vaults.reconcile(db, owner, 500.40, "SoFi")["ok"] is True

    def test_an_unreadable_balance_is_unverified_not_zero(self, db, owner):
        """Treating a missing balance as zero would report every vault as overdrawn by
        its own value."""
        make(db, owner, "Emergency", 500.0)
        result = vaults.reconcile(db, owner, None, "SoFi")
        assert result["ok"] is None
        assert result["tracked"] == 500.0
        assert "unverified" in result["message"]

    def test_only_vaults_in_that_account_are_counted(self, db, owner):
        make(db, owner, "SoFi pot", 300.0, where="SoFi Vault — Emergency")
        make(db, owner, "Chime pot", 999.0, where="Chime Savings")
        assert vaults.reconcile(db, owner, 300.0, "SoFi")["ok"] is True

    def test_a_goal_with_no_host_account_is_not_a_vault(self, db, owner):
        """A plain goal he has not attached to anywhere must not count against a real
        account's balance."""
        core_db.create_savings_goal(db, owner, "someday maybe", 5000.0, current_amount=50.0)
        assert vaults.vaults(db, owner) == []
        assert vaults.reconcile(db, owner, 0.0, "SoFi")["ok"] is True
