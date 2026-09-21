"""The store's rate dial.

Jack's terms: one product a day for now, changed by telling Jarvis, and no approval gate
on what the team makes. So the rate has to be a setting he can move by talking, every move
has to carry a reason the dashboard can show, and an absurd number has to be refused out
loud rather than accepted and acted on.
"""
import pytest

from assistant.core import db as core_db, store_policy as sp


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "policy.db")
    core_db.init_db(path)
    return path


def test_the_default_is_one_a_day(db):
    assert sp.products_per_day(db) == 1
    assert sp.current(db)["paused"] is False


def test_he_can_turn_it_up(db):
    result = sp.set_products_per_day(db, 3, reason="wants more volume")
    assert result == {"previous": 1, "products_per_day": 3, "paused": False}
    assert sp.products_per_day(db) == 3


def test_zero_pauses_rather_than_breaking(db):
    """'Pause the store' has to be expressible. Deleting the setting or using a negative
    number would both read as 'unset' somewhere downstream."""
    sp.set_products_per_day(db, 0, reason="pausing over the holiday")
    assert sp.products_per_day(db) == 0
    assert sp.current(db)["paused"] is True


def test_a_negative_rate_is_refused(db):
    with pytest.raises(ValueError, match="negative"):
        sp.set_products_per_day(db, -2)
    assert sp.products_per_day(db) == 1, "the refusal must not have changed anything"


def test_an_absurd_rate_is_refused_out_loud(db):
    """These are real listings against a real fulfiller. Refusing costs one sentence; a
    silent typo costs a catalogue full of junk."""
    with pytest.raises(ValueError, match="typo"):
        sp.set_products_per_day(db, 500)
    assert sp.products_per_day(db) == 1


def test_nonsense_is_refused_rather_than_coerced(db):
    with pytest.raises(ValueError):
        sp.set_products_per_day(db, "lots")


def test_a_corrupt_setting_falls_back_instead_of_stopping_or_running_away(db):
    """The only reading that is safe in both directions: a store that silently stops is
    as wrong as one that silently floods."""
    core_db.set_setting(db, sp.RATE_KEY, "banana")
    assert sp.products_per_day(db) == sp.DEFAULT_PER_DAY


def test_a_stored_value_past_the_ceiling_is_clamped_on_read(db):
    core_db.set_setting(db, sp.RATE_KEY, "9999")
    assert sp.products_per_day(db) == sp.MAX_PER_DAY


class TestTheReasonTrail:
    def test_every_change_records_what_he_said(self, db):
        """A rate that changed without explanation is indistinguishable from a bug, and
        the dashboard has to answer 'why did we ship four things on Tuesday'."""
        sp.set_products_per_day(db, 4, reason="pushing for the holiday run")
        entry = sp.current(db)["last_change"]
        assert entry["field"] == "products_per_day"
        assert entry["from"] == 1 and entry["to"] == 4
        assert entry["reason"] == "pushing for the holiday run"
        assert entry["by"] == "owner"

    def test_history_is_newest_first(self, db):
        sp.set_products_per_day(db, 2, reason="first")
        sp.set_products_per_day(db, 5, reason="second")
        assert [h["reason"] for h in sp.policy_history(db)][:2] == ["second", "first"]

    def test_history_does_not_grow_without_bound(self, db):
        for i in range(1, sp.HISTORY_LIMIT + 6):
            sp.set_products_per_day(db, (i % sp.MAX_PER_DAY) + 1, reason=f"change {i}")
        assert len(sp.policy_history(db)) == sp.HISTORY_LIMIT

    def test_a_refused_change_leaves_no_trace(self, db):
        with pytest.raises(ValueError):
            sp.set_products_per_day(db, 999)
        assert sp.policy_history(db) == []

    def test_corrupt_history_does_not_break_the_dashboard(self, db):
        core_db.set_setting(db, sp.HISTORY_KEY, "{not json")
        assert sp.policy_history(db) == []
        assert sp.current(db)["last_change"] is None


class TestAutopublish:
    def test_it_is_on_by_default_because_he_said_so(self, db):
        """'I dont need to approve what they make or sell.'"""
        assert sp.autopublish(db) is True

    def test_he_can_put_the_gate_back(self, db):
        assert sp.set_autopublish(db, False, reason="want to eyeball the first few")["autopublish"] is False
        assert sp.autopublish(db) is False

    def test_turning_the_gate_on_and_off_is_recorded_too(self, db):
        sp.set_autopublish(db, False, reason="checking quality")
        entry = sp.policy_history(db)[0]
        assert entry["field"] == "autopublish" and entry["to"] is False
