"""Regressions around the scheduled-employee loop.

These cover a live outage: every scheduled employee silently stopped running for over an
hour. Two independent defects combined -- a prompt template that raised on format(), and
a loop with no per-employee isolation, so the first failure took the whole roster down.
Neither surfaced as an error anywhere the owner could see.
"""
import pytest

from assistant.core import staff


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "staff.db")
    staff.init_staff_db(path)
    return path


class TestVerdictInstructions:
    def test_json_example_does_not_break_substitution(self):
        """The template contains a literal {"alert": ...} example.

        str.format reads that as a field name and raises KeyError: '"alert"'. That is the
        exact failure that stopped every scheduled employee, so it is pinned here.
        """
        out = staff.build_verdict_instructions("BTC moves more than 5%")
        assert "BTC moves more than 5%" in out
        assert '{"alert": true or false' in out
        assert "__CONDITION__" not in out

    def test_a_condition_containing_braces_is_still_safe(self):
        # The owner writes these in his own words; they are not required to be format-safe.
        out = staff.build_verdict_instructions('anything matching {"urgent": true}')
        assert '{"urgent": true}' in out


class TestFeedGranting:
    def test_market_context_is_inferred_from_the_job(self, db):
        assert staff.infer_data_feeds("Crypto Analyst", "Watches token prices") == "market"

    def test_a_tradable_ledger_is_never_inferred_from_wording(self, db):
        """The premise of this module is that a job description cannot grant its own
        powers. `paper` hands out an account that can be traded, so no phrasing may
        produce it -- an analyst describing the paper-trading desk it reports to briefly
        did exactly that."""
        for text in ("Runs paper trading simulations all day",
                     "Reports to the paper-trading desk",
                     "Senior day trader, 20 years experience"):
            assert "paper" not in staff.infer_data_feeds("Analyst", text)

    def test_feeds_can_be_granted_explicitly(self, db):
        key = staff.hire(db, "Trader", "Fifteen years trading experience across spot and derivatives markets.")["key"]
        assert staff.set_data_feeds(db, key, "market,paper")
        assert staff.get_staff(db, key)["data_feeds"] == "market,paper"

    def test_unknown_feeds_are_refused(self, db):
        key = staff.hire(db, "Trader", "Fifteen years trading experience across spot and derivatives markets.")["key"]
        with pytest.raises(ValueError, match="unknown feed"):
            staff.set_data_feeds(db, key, "market,filesystem")

    def test_an_employee_with_no_feeds_gets_no_briefing(self, db):
        assert staff.build_feed_briefing(db, None) == ""
        assert staff.build_feed_briefing(db, "") == ""


class TestColleagueBriefing:
    def test_an_employee_cannot_be_briefed_from_itself(self, db):
        key = staff.hire(db, "Trader", "Fifteen years trading experience across spot and derivatives markets.")["key"]
        with pytest.raises(ValueError, match="itself"):
            staff.set_briefing_from(db, key, key)

    def test_unknown_colleagues_are_refused(self, db):
        key = staff.hire(db, "Trader", "Fifteen years trading experience across spot and derivatives markets.")["key"]
        with pytest.raises(ValueError, match="no employee with key"):
            staff.set_briefing_from(db, key, "imaginary_analyst")

    def test_no_colleagues_means_no_handoff_text(self, db):
        assert staff.build_colleague_briefing(db, None) == ""

    def test_a_silent_colleague_is_reported_rather_than_imagined(self, db):
        analyst = staff.hire(db, "Analyst", "Twenty years of markets research, macro and on-chain analysis.")["key"]
        trader = staff.hire(db, "Trader", "Fifteen years trading experience across spot and derivatives markets.")["key"]
        staff.set_briefing_from(db, trader, analyst)
        out = staff.build_colleague_briefing(db, analyst)
        # The downstream employee must be told the upstream one has said nothing, not
        # left to fill the silence in.
        assert "nothing delivered" in out
        assert "Do not invent their view" in out


class TestRunDueIsolation:
    def test_one_employee_failing_does_not_stop_the_others(self, db, monkeypatch):
        """A single uncaught error used to abort the entire pass, so one broken employee
        took every other employee offline and looked like 'nothing was due'."""
        for title in ("Alpha", "Beta"):
            key = staff.hire(db, title, f"{title} is a senior specialist with fifteen years of relevant experience.", cadence="interval",
                             interval_minutes=5, standing_assignment="do the thing")["key"]
            staff.set_status(db, key, "active")

        calls = []

        class ExplodingLLM:
            def research(self, prompt, system_prompt=None, timeout=None):
                calls.append(prompt)
                if len(calls) == 1:
                    raise RuntimeError("first employee blows up")
                return 'all good\n{"alert": false, "urgency": "low", "headline": "fine"}'

        results = staff.run_due(db, ExplodingLLM(), tz_name="UTC")
        assert len(results) == 2, "both employees must be attempted"
        assert len(calls) == 2, "the second employee must still be reached"
        assert [r["ok"] for r in results] == [False, True]

    def test_a_failure_is_recorded_not_swallowed(self, db):
        key = staff.hire(db, "Gamma", "Gamma is a senior specialist with fifteen years of relevant experience.", cadence="interval",
                         interval_minutes=5, standing_assignment="do the thing")["key"]

        class DeadLLM:
            def research(self, prompt, system_prompt=None, timeout=None):
                raise RuntimeError("backend down")

        staff.run_due(db, DeadLLM(), tz_name="UTC")
        work = staff.recent_work(db, key, limit=1)
        assert work and work[0]["status"] == "failed"
        assert "backend down" in work[0]["error"]
