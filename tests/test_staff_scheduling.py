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


class TestFeedBriefingContent:
    """A real, confirmed incident: employees were told to "only trade coins in the
    tracked set" and to set a numeric stop-loss/take-profit, with no actual way to check
    the former and nothing that ever showed the latter back to them. These pin that the
    briefing now hands over the real tracked list and each position's committed plan.
    """

    @pytest.fixture
    def market_db(self, db):
        import sqlite3
        from datetime import datetime, timezone
        from assistant.core import market_data
        market_data.init_market_db(db)
        conn = sqlite3.connect(db)
        now = datetime.now(timezone.utc).isoformat()
        for code, rank, rate in (("BTC", 1, 80_000.0), ("SOL", 7, 200.0)):
            conn.execute(
                """INSERT INTO market_coins (code, name, rank, rate, present,
                                             first_seen, last_seen, updated_at)
                   VALUES (?,?,?,?,1,?,?,?)""", (code, code, rank, rate, now, now, now))
        conn.execute(
            "INSERT INTO market_polls (ok, coins, at) VALUES (1, 2, ?)", (now,))
        conn.commit()
        conn.close()
        return db

    def test_market_briefing_lists_every_tradeable_code(self, market_db):
        out = staff.build_feed_briefing(market_db, "market")
        assert "TRADEABLE ON THIS FEED" in out
        assert "BTC" in out and "SOL" in out

    def test_market_briefing_includes_kraken_sourced_gap_coins_as_tradeable(self, market_db):
        """TAO/WLD/AERO/etc. used to be called out as a permanent "KNOWN GAPS" watch-only
        carve-out in this briefing. Now that a supplemental Kraken poll prices them
        (source='kraken'), they must appear in the ordinary tradeable list like any other
        coin -- no separate gap sentence, and no longer watch-only."""
        import sqlite3
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(market_db)
        conn.execute(
            """INSERT INTO market_coins (code, name, rate, present, source,
                                         first_seen, last_seen, updated_at)
               VALUES ('TAO','TAO',237.5,1,'kraken',?,?,?)""", (now, now, now))
        conn.commit()
        conn.close()
        out = staff.build_feed_briefing(market_db, "market")
        assert "TAO" in out
        assert "KNOWN GAPS" not in out
        assert "watch-only" not in out

    def test_paper_briefing_shows_the_committed_stop_and_target(self, market_db):
        from assistant.core import paper_trading
        paper_trading.execute_orders(
            market_db, [{"side": "buy", "code": "SOL", "usd": 100, "stop_loss": 180.0, "take_profit": 240.0}])
        out = staff.build_feed_briefing(market_db, "paper")
        assert "stop $180" in out or "stop $180.00" in out
        assert "target $240" in out or "target $240.00" in out

    def test_paper_briefing_flags_a_position_with_no_exit_plan(self, market_db):
        from assistant.core import paper_trading
        paper_trading.execute_orders(market_db, [{"side": "buy", "code": "SOL", "usd": 100}])
        out = staff.build_feed_briefing(market_db, "paper")
        assert "none set" in out

    def test_paper_briefing_shows_the_reason_and_exit_kind_on_a_fill(self, market_db):
        from assistant.core import paper_trading
        paper_trading.execute_orders(
            market_db, [{"side": "buy", "code": "SOL", "usd": 100, "reason": "momentum breakout"}])
        out = staff.build_feed_briefing(market_db, "paper")
        assert "momentum breakout" in out


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
            def research(self, prompt, system_prompt=None, timeout=None, **kwargs):
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
            def research(self, prompt, system_prompt=None, timeout=None, **kwargs):
                raise RuntimeError("backend down")

        staff.run_due(db, DeadLLM(), tz_name="UTC")
        work = staff.recent_work(db, key, limit=1)
        assert work and work[0]["status"] == "failed"
        assert "backend down" in work[0]["error"]


class TestFailureNotification:
    """A failed/timed-out scheduled run used to notify nobody: the whole notify block in
    run_due was gated on outcome.get("ok"), so alert_policy never even got consulted for
    a run that crashed. That's the exact "Jarvis wasn't told the job failed" gap -- a
    coding job dying mid-task (e.g. hitting the subprocess timeout) was only ever visible
    in staff_work/the logs, never to the owner."""

    def test_a_failed_run_notifies_even_with_alert_policy_never(self, db):
        key = staff.hire(db, "Delta", "Delta is a senior specialist with fifteen years of relevant experience.",
                         cadence="interval", interval_minutes=5, standing_assignment="do the thing",
                         alert_policy="never")["key"]
        staff.set_status(db, key, "active")

        class DeadLLM:
            def research(self, prompt, system_prompt=None, timeout=None, **kwargs):
                raise RuntimeError("backend down")

        notified = []
        staff.run_due(db, DeadLLM(), tz_name="UTC",
                      notify=lambda headline, body, urgency, person: notified.append(headline))
        assert notified, "a failed run must reach the owner regardless of alert_policy"
        assert "failed" in notified[0].lower()

    def test_a_second_failure_within_cooldown_is_not_double_notified(self, db):
        key = staff.hire(db, "Epsilon", "Epsilon is a senior specialist with fifteen years of relevant experience.",
                         cadence="interval", interval_minutes=5, standing_assignment="do the thing",
                         alert_policy="never", alert_cooldown_min=30)["key"]
        staff.set_status(db, key, "active")

        class DeadLLM:
            def research(self, prompt, system_prompt=None, timeout=None, **kwargs):
                raise RuntimeError("backend down")

        notified = []
        notify = lambda headline, body, urgency, person: notified.append(headline)
        staff.run_due(db, DeadLLM(), tz_name="UTC", notify=notify)
        staff.run_due(db, DeadLLM(), tz_name="UTC", notify=notify)
        assert len(notified) == 1, "the cooldown must still apply, or a persistently broken employee spams every tick"

    def test_a_successful_quiet_run_still_does_not_notify(self, db):
        """Preserves the pre-existing behaviour this fix must not disturb: alert_policy
        'never' with nothing alert-worthy stays silent on a run that actually delivered."""
        key = staff.hire(db, "Zeta", "Zeta is a senior specialist with fifteen years of relevant experience.",
                         cadence="interval", interval_minutes=5, standing_assignment="do the thing",
                         alert_policy="never")["key"]
        staff.set_status(db, key, "active")

        class QuietLLM:
            def research(self, prompt, system_prompt=None, timeout=None, **kwargs):
                return 'all good\n{"alert": false, "urgency": "low", "headline": "fine"}'

        notified = []
        staff.run_due(db, QuietLLM(), tz_name="UTC",
                      notify=lambda headline, body, urgency, person: notified.append(headline))
        assert notified == []

    def test_run_due_forwards_its_timeout_to_the_assignment(self, db):
        """Regression for the 900s default that killed real in-progress coding jobs:
        run_due must pass its own timeout through to assign()/llm.research() rather than
        silently falling back to some other default."""
        key = staff.hire(db, "Eta", "Eta is a senior specialist with fifteen years of relevant experience.",
                         cadence="interval", interval_minutes=5, standing_assignment="do the thing")["key"]
        staff.set_status(db, key, "active")

        seen = []

        class RecordingLLM:
            def research(self, prompt, system_prompt=None, timeout=None, **kwargs):
                seen.append(timeout)
                return "done"

        staff.run_due(db, RecordingLLM(), tz_name="UTC", timeout=10800)
        assert seen == [10800]


class FakeWorkQueue:
    """Records submit()/has_pending() calls without touching any real storage --
    run_due only ever needs this duck-typed shape, not the real WorkQueue/SQLite table."""

    def __init__(self, pending: set[str] = frozenset()):
        self.pending = set(pending)
        self.submitted: list[tuple] = []
        self._next_id = 1

    def has_pending(self, employee_key: str) -> bool:
        return employee_key in self.pending

    def submit(self, employee_key, assignment, kind="on_demand", owner_user_id=None):
        self.submitted.append((employee_key, assignment, kind))
        queue_id = self._next_id
        self._next_id += 1
        return queue_id


class TestRunDueWithWorkQueue:
    """With a work_queue given, run_due must enqueue rather than call assign() at all --
    that call, and the alert-policy decision that used to follow it inline, now happen
    later in work_queue.py's worker (see staff.handle_cadence_outcome)."""

    def test_a_due_employee_is_enqueued_as_cadence_kind_not_run_inline(self, db):
        key = staff.hire(db, "Theta", "Theta is a senior specialist with fifteen years of relevant experience.",
                         cadence="interval", interval_minutes=5, standing_assignment="do the thing")["key"]
        staff.set_status(db, key, "active")
        queue = FakeWorkQueue()

        class ExplodingLLM:
            def research(self, *a, **k):
                raise AssertionError("assign() must not be called when a work_queue is given")

        results = staff.run_due(db, ExplodingLLM(), tz_name="UTC", work_queue=queue)

        assert queue.submitted == [(key, "do the thing", "cadence")]
        assert results == [{"employee": key, "ok": None, "alert": None, "alerted": False,
                            "headline": None, "queued": True, "queue_id": 1}]

    def test_an_employee_already_pending_in_the_queue_is_not_enqueued_again(self, db):
        """The real gap this guards: due_for_cadence has no idea a previous cadence run
        is still sitting in the queue, so without this check a job that outlives one
        5-minute tick would be piled on again every tick until the first copy drains."""
        key = staff.hire(db, "Iota", "Iota is a senior specialist with fifteen years of relevant experience.",
                         cadence="interval", interval_minutes=5, standing_assignment="do the thing")["key"]
        staff.set_status(db, key, "active")
        queue = FakeWorkQueue(pending={key})

        results = staff.run_due(db, object(), tz_name="UTC", work_queue=queue)

        assert queue.submitted == []
        assert results == [{"employee": key, "ok": None, "alert": None, "alerted": False,
                            "headline": None, "queued": False, "skipped": "already queued or running"}]

    def test_a_verdict_condition_is_still_appended_before_enqueueing(self, db):
        key = staff.hire(db, "Kappa", "Kappa is a senior specialist with fifteen years of relevant experience.",
                         cadence="interval", interval_minutes=5, standing_assignment="check things",
                         alert_condition="something breaks", alert_policy="on_alert")["key"]
        staff.set_status(db, key, "active")
        queue = FakeWorkQueue()

        staff.run_due(db, object(), tz_name="UTC", work_queue=queue)

        assignment = queue.submitted[0][1]
        assert "check things" in assignment
        assert "something breaks" in assignment


class TestHandleCadenceOutcome:
    """The alert decision itself, extracted out of run_due so work_queue.py's worker can
    make the identical decision once an async cadence job finishes."""

    def test_a_failure_notifies_regardless_of_alert_policy(self, db):
        person = staff.hire(db, "Lambda", "Lambda is a senior specialist with fifteen years of relevant experience.",
                            alert_policy="never")
        notified = []
        result = staff.handle_cadence_outcome(
            db, person, {"ok": False, "error": "backend down"},
            notify=lambda h, b, u, p: notified.append((h, b)))
        assert result["ok"] is False
        assert notified and "failed" in notified[0][0].lower()

    def test_a_quiet_success_with_policy_never_does_not_notify(self, db):
        person = staff.hire(db, "Mu", "Mu is a senior specialist with fifteen years of relevant experience.",
                            alert_policy="never")
        notified = []
        result = staff.handle_cadence_outcome(
            db, person, {"ok": True, "output": 'fine\n{"alert": false, "urgency": "low", "headline": "fine"}'},
            notify=lambda h, b, u, p: notified.append(h))
        assert result["alert"] is False
        assert notified == []

    def test_an_alerting_verdict_with_policy_on_alert_notifies(self, db):
        person = staff.hire(db, "Nu", "Nu is a senior specialist with fifteen years of relevant experience.",
                            alert_policy="on_alert", alert_condition="BTC moves more than 5%")
        notified = []
        result = staff.handle_cadence_outcome(
            db, person,
            {"ok": True, "output": 'BTC dumped\n{"alert": true, "urgency": "high", "headline": "BTC -10%"}'},
            notify=lambda h, b, u, p: notified.append(h))
        assert result["alerted"] is True
        assert notified == ["BTC -10%"]


class TestPaperRecordInTheBriefing:
    """The win rate was computed by paper_trading.performance() since the day that module
    was written, and never once shown to the employee trading against it. The per-coin
    split did not exist at all, so nothing here could answer "I have lost on this ticker
    three times running" -- which the trade log shows happening repeatedly.
    """

    @pytest.fixture
    def market_db(self, db):
        import sqlite3
        from datetime import datetime, timezone
        from assistant.core import market_data
        market_data.init_market_db(db)
        conn = sqlite3.connect(db)
        now = datetime.now(timezone.utc).isoformat()
        for code, rate in (("BTC", 80_000.0), ("SOL", 200.0)):
            conn.execute(
                """INSERT INTO market_coins (code, name, rank, rate, present,
                                             first_seen, last_seen, updated_at)
                   VALUES (?,?,1,?,1,?,?,?)""", (code, code, rate, now, now, now))
        conn.execute("INSERT INTO market_polls (ok, coins, at) VALUES (1, 2, ?)", (now,))
        conn.commit()
        conn.close()
        return db

    def _lose_on(self, db, code, crashed_to, back_to=None):
        import sqlite3
        from assistant.core import paper_trading
        paper_trading.execute_orders(db, [{"side": "buy", "code": code, "usd": 100}])
        conn = sqlite3.connect(db)
        conn.execute("UPDATE market_coins SET rate = ? WHERE code = ?", (crashed_to, code))
        conn.commit()
        conn.close()
        paper_trading.execute_orders(db, [{"side": "sell", "code": code, "qty": "all"}])
        if back_to is not None:
            conn = sqlite3.connect(db)
            conn.execute("UPDATE market_coins SET rate = ? WHERE code = ?", (back_to, code))
            conn.commit()
            conn.close()

    def test_the_win_rate_finally_reaches_the_employee(self, market_db):
        self._lose_on(market_db, "SOL", 100.0)
        out = staff.build_feed_briefing(market_db, "paper")
        assert "YOUR RECORD SO FAR" in out
        assert "1 closed round-trips" in out
        assert "0.0% win rate" in out

    def test_coins_it_has_repeatedly_lost_on_are_named(self, market_db):
        for _ in range(3):
            self._lose_on(market_db, "SOL", 100.0, back_to=200.0)
        out = staff.build_feed_briefing(market_db, "paper")
        assert "coins you have LOST money on" in out
        assert "SOL" in out and "(0W/3L)" in out

    def test_with_no_closed_trades_it_says_so_rather_than_implying_a_record(self, market_db):
        out = staff.build_feed_briefing(market_db, "paper")
        assert "no per-coin record to learn from" in out

    def test_the_record_leads_the_briefing_and_the_movers_follow(self, market_db):
        """Framing, not strategy: the first thing a trader read on every run used to be a
        list of coins that had just jumped, before it had seen a single fact about its own
        results. Both blocks are still present; the order changed."""
        self._lose_on(market_db, "SOL", 100.0)
        out = staff.build_feed_briefing(market_db, "market,paper")
        assert out.index("YOUR RECORD SO FAR") < out.index("CRYPTO FEED")

    def test_movers_are_no_longer_labelled_as_a_trade_shortlist(self, market_db):
        out = staff.build_feed_briefing(market_db, "market")
        assert "movers, last hour:" not in out


class TestJournalLoop:
    """The whole point: a lesson written on one run has to be readable on the next one.

    Unit-testing the write alone would have passed while the loop stayed open -- which is
    exactly the state this desk was already in, since paper_trading.performance() computed
    a win rate nobody ever read.
    """

    @pytest.fixture
    def market_db(self, db):
        import sqlite3
        from datetime import datetime, timezone
        from assistant.core import market_data
        market_data.init_market_db(db)
        conn = sqlite3.connect(db)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO market_coins (code, name, rank, rate, present,
                                         first_seen, last_seen, updated_at)
               VALUES ('SOL','SOL',1,200.0,1,?,?,?)""", (now, now, now))
        conn.execute("INSERT INTO market_polls (ok, coins, at) VALUES (1, 1, ?)", (now,))
        conn.commit()
        conn.close()
        return db

    @pytest.fixture
    def vault(self, tmp_path):
        """A temp vault. The real one is the owner's own notes."""
        from assistant.core.obsidian_client import ObsidianClient
        return ObsidianClient(str(tmp_path / "vault"))

    @pytest.fixture
    def trader(self, market_db):
        key = staff.hire(market_db, "Desk Trader",
                         "Fifteen years trading crypto markets across spot and derivatives.")["key"]
        staff.set_data_feeds(market_db, key, "market,paper,journal")
        return key

    class ScriptedLLM:
        def __init__(self, *responses):
            self.responses = list(responses)
            self.prompts = []

        def research(self, prompt, system_prompt=None, timeout=None, **kwargs):
            self.prompts.append(prompt)
            return self.responses.pop(0)

    def test_a_lesson_written_on_one_run_is_read_back_on_the_next(self, market_db, trader, vault):
        llm = self.ScriptedLLM(
            'Standing down today.\n```journal\n'
            '{"kind": "lesson", "summary": "RAY whipsaws on thin volume",'
            ' "direction_change": true, "tickers": ["RAY"], "detail": "Third stop-out.",'
            ' "lesson": "Stop re-entering RAY after a stop-out."}\n```',
            "Nothing today.")

        staff.assign(market_db, llm, trader, "do your rounds", obsidian=vault)
        staff.assign(market_db, llm, trader, "do your rounds", obsidian=vault)

        assert "Stop re-entering RAY after a stop-out." in llm.prompts[1], \
            "the second run must carry the first run's lesson -- this is the whole feature"
        assert "YOUR OWN PRIOR NOTES" in llm.prompts[1]

    def test_the_first_run_is_told_it_has_no_notes_yet(self, market_db, trader, vault):
        llm = self.ScriptedLLM("Nothing today.")
        staff.assign(market_db, llm, trader, "do your rounds", obsidian=vault)
        assert "No journal entries on file yet" in llm.prompts[0]

    def test_the_employee_is_asked_for_a_block_it_does_not_file_itself(self, market_db, trader, vault):
        """Same split as the orders block: the agent proposes, code writes. A scheduled
        run has no tool loop to call the vault with anyway."""
        llm = self.ScriptedLLM("Nothing today.")
        staff.assign(market_db, llm, trader, "do your rounds", obsidian=vault)
        assert "```journal" in llm.prompts[0]
        assert "write_note" not in llm.prompts[0]

    def test_an_employee_without_the_feed_is_never_asked_for_a_block(self, market_db, vault):
        key = staff.hire(market_db, "Plain Analyst",
                         "Twenty years of markets research, macro and on-chain analysis.")["key"]
        llm = self.ScriptedLLM("Nothing today.")
        staff.assign(market_db, llm, key, "do your rounds", obsidian=vault)
        assert "```journal" not in llm.prompts[0]

    def test_the_journal_records_the_ledger_fill_not_the_model_claim(self, market_db, trader, vault):
        """A model that says 'bought 10 BTC' after a $100 SOL order would otherwise write
        a false trade history into the owner's vault, which is worse than none."""
        from assistant.core import crypto_journal
        llm = self.ScriptedLLM(
            'I bought 10 BTC today, a huge position.\n'
            '```journal\n{"summary": "loading up", "detail": "conviction buy"}\n```\n'
            '```orders\n{"orders": [{"side": "buy", "code": "SOL", "usd": 100,'
            ' "stop_loss": 180.0}]}\n```')
        staff.assign(market_db, llm, trader, "do your rounds", obsidian=vault)

        notes = vault.list_notes(crypto_journal.JOURNAL_FOLDER)["notes"]
        body = vault.read_note(crypto_journal.JOURNAL_FOLDER, notes[0]["title"])["content"]
        assert "BUY 0.5 SOL" in body
        assert "10 BTC" not in body

    def test_a_routine_trader_run_writes_nothing(self, market_db, trader, vault):
        from assistant.core import crypto_journal
        llm = self.ScriptedLLM(
            'Quiet.\n```journal\n{"summary": "no change", "detail": "range intact"}\n```')
        staff.assign(market_db, llm, trader, "do your rounds", obsidian=vault)
        assert vault.list_notes(crypto_journal.JOURNAL_FOLDER)["notes"] == []

    def test_a_journalling_employee_with_no_vault_still_delivers(self, market_db, trader):
        """Memory is the feature; the run is the job. Losing the second to protect the
        first would be the wrong trade."""
        llm = self.ScriptedLLM("Nothing today.")
        out = staff.assign(market_db, llm, trader, "do your rounds", obsidian=None)
        assert out["ok"]
        assert "```journal" not in llm.prompts[0]

    def test_a_vault_write_failure_does_not_fail_the_run(self, market_db, trader):
        class BrokenVault:
            def read_note(self, *a, **k):
                return {"error": "drive not ready"}

            def list_notes(self, *a, **k):
                return {"notes": []}

            def write_note(self, *a, **k):
                raise OSError("drive not ready")

        llm = self.ScriptedLLM(
            'Learned something.\n```journal\n{"summary": "x", "detail": "y",'
            ' "lesson": "z"}\n```')
        out = staff.assign(market_db, llm, trader, "do your rounds", obsidian=BrokenVault())
        assert out["ok"], "the deliverable was already produced; a vault fault must not lose it"

    def test_run_due_carries_the_vault_through_to_the_employee(self, market_db, trader, vault):
        from assistant.core import crypto_journal
        staff.set_shift(market_db, trader, cadence="interval", interval_minutes=5)
        staff.set_standing_assignment(market_db, trader, "do your rounds")
        llm = self.ScriptedLLM(
            'Learned something.\n```journal\n{"summary": "RAY is untradeable for me",'
            ' "detail": "third stop-out", "lesson": "Stop re-entering RAY."}\n```')
        staff.run_due(market_db, llm, tz_name="UTC", obsidian=vault)
        assert vault.list_notes(crypto_journal.JOURNAL_FOLDER)["notes"], \
            "the scheduled path, not just a direct assign(), has to reach the vault"
