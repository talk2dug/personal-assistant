"""The crypto desk's memory: what gets written to the vault, what gets read back, and
what deliberately does not.

Every test here points at a temp vault. The real one is the owner's own notes, and a test
suite that writes into it is a test suite that corrupts his second brain.

The failure this whole module exists to prevent is amnesia -- the trade log showed the
same tickers bought, stopped out, and re-bought days later on "fresh momentum", because
nothing carried the lesson forward. So the tests that matter most are the ones proving a
lesson survives a run boundary, and that nothing fabricated gets filed as one.
"""
from datetime import datetime, timezone

import pytest

from assistant.core import crypto_journal
from assistant.core.obsidian_client import ObsidianClient

TRADER = "Crypto Day Trader (Paper Trading)"
ANALYST = "Crypto Research Analyst"
NOW = datetime(2026, 9, 13, 14, 2, tzinfo=timezone.utc)


@pytest.fixture
def obsidian(tmp_path):
    """A throwaway vault. Never the real one -- see this module's docstring."""
    return ObsidianClient(str(tmp_path / "vault"))


def entry(**kw):
    base = {"kind": "analysis", "summary": "BTC range-bound 74-80k", "direction_change": False,
            "tickers": ["BTC"], "detail": "Volume is thin above 80k.", "lesson": None}
    base.update(kw)
    return base


class TestParseJournalEntry:
    def test_a_well_formed_block_is_extracted(self):
        out = crypto_journal.parse_journal_entry(
            'Here is my read.\n\n```journal\n'
            '{"kind": "lesson", "summary": "RAY keeps whipsawing", "direction_change": true,'
            ' "tickers": ["ray"], "detail": "Three stop-outs.", "lesson": "Stop chasing RAY."}\n'
            '```\n')
        assert out["kind"] == "lesson"
        assert out["direction_change"] is True
        assert out["tickers"] == ["RAY"]            # normalised, so grouping actually groups
        assert out["lesson"] == "Stop chasing RAY."

    def test_the_last_block_wins(self):
        """Same rule as parse_orders: a model that restates puts the final answer last."""
        out = crypto_journal.parse_journal_entry(
            '```journal\n{"summary": "first"}\n```\nOn reflection:\n'
            '```journal\n{"summary": "second"}\n```')
        assert out["summary"] == "second"

    def test_malformed_json_yields_no_entry_rather_than_a_guess(self):
        """A fabricated journal entry is worse than a missing one -- it becomes a
        'lesson' the desk then trusts forever."""
        assert crypto_journal.parse_journal_entry('```journal\n{"summary": "oops",,}\n```') is None

    def test_prose_with_no_block_is_a_legitimate_answer(self):
        assert crypto_journal.parse_journal_entry("Nothing worth noting today.") is None
        assert crypto_journal.parse_journal_entry(None) is None
        assert crypto_journal.parse_journal_entry("") is None

    def test_an_orders_block_is_never_filed_as_a_journal_entry(self):
        """The trader emits both blocks in one reply. A bare ```json fence around its
        orders must not be mistaken for narrative."""
        assert crypto_journal.parse_journal_entry(
            '```json\n{"orders": [{"side": "buy", "code": "SOL", "usd": 100}]}\n```') is None

    def test_the_journal_block_is_found_even_when_orders_follow_it(self):
        out = crypto_journal.parse_journal_entry(
            '```journal\n{"summary": "buying the SOL breakout", "detail": "volume confirms"}\n```\n'
            '```orders\n{"orders": [{"side": "buy", "code": "SOL", "usd": 100}]}\n```')
        assert out["summary"] == "buying the SOL breakout"

    def test_an_orders_block_above_the_journal_does_not_eat_it(self):
        """The mirror of the parse_orders regression: the trader emits both blocks, and
        whichever came second used to be swallowed by the first one's closing fence."""
        out = crypto_journal.parse_journal_entry(
            '```orders\n{"orders": [{"side": "buy", "code": "SOL", "usd": 100}]}\n```\n'
            '```journal\n{"summary": "bought the SOL breakout"}\n```')
        assert out["summary"] == "bought the SOL breakout"

    def test_an_unrelated_code_fence_is_skipped_not_resynchronised_on(self):
        out = crypto_journal.parse_journal_entry(
            "```python\nprint('hi')\n```\n```journal\n{\"summary\": \"still here\"}\n```")
        assert out["summary"] == "still here"

    def test_a_block_carrying_no_narrative_at_all_is_ignored(self):
        assert crypto_journal.parse_journal_entry(
            '```journal\n{"kind": "analysis", "tickers": ["BTC"]}\n```') is None

    def test_a_nonsense_kind_falls_back_rather_than_being_stored(self):
        out = crypto_journal.parse_journal_entry(
            '```journal\n{"kind": "vibes", "summary": "up"}\n```')
        assert out["kind"] == "analysis"

    def test_tickers_that_are_not_a_list_do_not_raise(self):
        out = crypto_journal.parse_journal_entry(
            '```journal\n{"summary": "up", "tickers": "BTC"}\n```')
        assert out["tickers"] == []


class TestJournalInstructions:
    def test_the_template_survives_being_built_with_json_inside_it(self):
        """The literal JSON example is exactly what broke the verdict template via
        str.format and stopped every scheduled employee for over an hour."""
        for role in ("trader", "analyst"):
            out = crypto_journal.journal_instructions(role)
            assert '"direction_change"' in out and "```journal" in out

    def test_the_trader_is_told_not_to_journal_routine_runs(self):
        """The owner's answer to the design's open question 1."""
        assert "only on a run where something actually happened" in \
            crypto_journal.journal_instructions("trader")
        assert "every run" in crypto_journal.journal_instructions("analyst")


class TestPriorContext:
    def test_a_first_run_is_told_it_has_no_notes_rather_than_going_silent(self, obsidian):
        """Silence and a broken vault look identical to the model. One of them is worth
        mentioning in its report."""
        out = crypto_journal.build_prior_context(obsidian, ANALYST)
        assert "No journal entries on file yet" in out

    def test_the_running_summary_and_recent_dailies_come_back(self, obsidian):
        obsidian.write_note(crypto_journal.SUMMARY_FOLDER, crypto_journal.summary_title(ANALYST),
                            "- LESSON: thin books above 80k")
        obsidian.write_note(crypto_journal.JOURNAL_FOLDER,
                            crypto_journal.daily_title(ANALYST, "2026-09-13"),
                            "### 09:00 UTC\nETH looks heavy.")
        out = crypto_journal.build_prior_context(obsidian, ANALYST)
        assert "thin books above 80k" in out
        assert "ETH looks heavy" in out

    def test_another_employee_s_notes_are_not_read(self, obsidian):
        """The analyst and the trader keep separate records on purpose -- one shared
        summary would launder one's reasoning into the other's."""
        obsidian.write_note(crypto_journal.JOURNAL_FOLDER,
                            crypto_journal.daily_title(TRADER, "2026-09-13"),
                            "### 09:00 UTC\nbought SOL")
        out = crypto_journal.build_prior_context(obsidian, ANALYST)
        assert "bought SOL" not in out

    def test_the_injected_block_is_size_capped(self, obsidian):
        obsidian.write_note(crypto_journal.SUMMARY_FOLDER, crypto_journal.summary_title(ANALYST),
                            "x" * 50_000)
        for day in ("2026-09-12", "2026-09-13"):
            obsidian.write_note(crypto_journal.JOURNAL_FOLDER,
                                crypto_journal.daily_title(ANALYST, day), "y" * 50_000)
        out = crypto_journal.build_prior_context(obsidian, ANALYST)
        # This lands in every single run's prompt; unbounded here is unbounded forever.
        assert len(out) < crypto_journal.PRIOR_CONTEXT_CHARS + 1500

    def test_the_newest_entries_survive_truncation_not_the_oldest(self, obsidian):
        obsidian.write_note(crypto_journal.SUMMARY_FOLDER, crypto_journal.summary_title(ANALYST),
                            "ANCIENT-MARKER " + "x" * 5_000 + " NEWEST-MARKER")
        out = crypto_journal.build_prior_context(obsidian, ANALYST)
        assert "NEWEST-MARKER" in out
        assert "ANCIENT-MARKER" not in out

    def test_no_client_means_no_block(self):
        assert crypto_journal.build_prior_context(None, ANALYST) == ""

    def test_an_unreadable_vault_costs_memory_not_the_run(self, obsidian):
        class BrokenVault:
            def read_note(self, *a, **k):
                raise OSError("drive not ready")

            def list_notes(self, *a, **k):
                raise OSError("drive not ready")

        out = crypto_journal.build_prior_context(BrokenVault(), ANALYST)
        assert "No journal entries on file yet" in out      # degraded, not raised


class TestRecordRun:
    def _notes(self, obsidian, folder):
        return {n["title"] for n in obsidian.list_notes(folder)["notes"]}

    def test_the_analyst_journals_every_run(self, obsidian):
        out = crypto_journal.record_run(obsidian, ANALYST, entry(), role="analyst", now=NOW)
        assert out["journaled"]
        assert f"2026-09-13 -- {ANALYST}" in self._notes(obsidian, crypto_journal.JOURNAL_FOLDER)

    def test_a_routine_trader_run_is_not_journaled(self, obsidian):
        """The owner's decision: no trade, no direction change, no lesson -- no entry. A
        trader at a frequent cadence journaling every tick writes a log nobody reads."""
        out = crypto_journal.record_run(obsidian, TRADER, entry(), role="trader", now=NOW)
        assert not out["journaled"]
        assert "routine run" in out["reason"]
        assert self._notes(obsidian, crypto_journal.JOURNAL_FOLDER) == set()

    def test_a_trader_run_with_a_fill_is_journaled(self, obsidian):
        out = crypto_journal.record_run(
            obsidian, TRADER, entry(), role="trader", now=NOW,
            fills=[{"side": "buy", "code": "SOL", "qty": 2.5, "price": 200.0, "usd": 500.0,
                    "fee": 0.5, "reason": "breakout"}])
        assert out["journaled"]
        body = obsidian.read_note(crypto_journal.JOURNAL_FOLDER,
                                  crypto_journal.daily_title(TRADER, "2026-09-13"))["content"]
        assert "BUY 2.5 SOL" in body and "breakout" in body

    def test_a_trader_run_with_only_a_lesson_is_journaled(self, obsidian):
        out = crypto_journal.record_run(obsidian, TRADER, entry(lesson="Stop chasing RAY."),
                                        role="trader", now=NOW)
        assert out["journaled"]

    def test_a_trader_run_with_only_a_direction_change_is_journaled(self, obsidian):
        out = crypto_journal.record_run(obsidian, TRADER, entry(direction_change=True),
                                        role="trader", now=NOW)
        assert out["journaled"]

    def test_fills_are_recorded_even_when_the_model_emitted_no_block(self, obsidian):
        """The fills are the ledger's own fact. Losing them because the model forgot its
        formatting would put a hole in exactly the record this exists to build."""
        out = crypto_journal.record_run(
            obsidian, TRADER, None, role="trader", now=NOW,
            fills=[{"side": "sell", "code": "EOS", "qty": 10, "price": 0.5, "usd": 5.0,
                    "fee": 0.005, "realized": -1.25}])
        assert out["journaled"]
        body = obsidian.read_note(crypto_journal.JOURNAL_FOLDER,
                                  crypto_journal.daily_title(TRADER, "2026-09-13"))["content"]
        assert "No journal block" in body
        assert "realised $-1.25" in body

    def test_nothing_is_written_when_there_is_neither_a_block_nor_a_fill(self, obsidian):
        out = crypto_journal.record_run(obsidian, ANALYST, None, role="analyst", now=NOW)
        assert not out["journaled"]
        assert self._notes(obsidian, crypto_journal.JOURNAL_FOLDER) == set()

    def test_a_routine_analysis_does_not_touch_the_running_summary(self, obsidian):
        """Section 2.2: the summary is re-read into every prompt, so only things that
        should never need re-deriving are folded into it."""
        out = crypto_journal.record_run(obsidian, ANALYST, entry(), role="analyst", now=NOW)
        assert out["folded"] is False
        assert self._notes(obsidian, crypto_journal.SUMMARY_FOLDER) == set()

    def test_a_lesson_is_folded_into_the_running_summary(self, obsidian):
        crypto_journal.record_run(obsidian, ANALYST, entry(lesson="Thin books above 80k."),
                                  role="analyst", now=NOW)
        body = obsidian.read_note(crypto_journal.SUMMARY_FOLDER,
                                  crypto_journal.summary_title(ANALYST))["content"]
        # The exact "- LESSON: " form matters: compaction greps for it, and a lesson that
        # does not match is a lesson the housekeeping pass silently throws away.
        assert "- LESSON: Thin books above 80k." in body

    def test_a_direction_change_folds_the_thesis_per_ticker(self, obsidian):
        crypto_journal.record_run(obsidian, ANALYST,
                                  entry(direction_change=True, tickers=["BTC", "ETH"]),
                                  role="analyst", now=NOW)
        body = obsidian.read_note(crypto_journal.SUMMARY_FOLDER,
                                  crypto_journal.summary_title(ANALYST))["content"]
        assert "- THESIS BTC: BTC range-bound 74-80k" in body
        assert "- THESIS ETH:" in body

    def test_a_close_folds_the_realised_result_and_the_machine_computed_record(self, obsidian):
        crypto_journal.record_run(
            obsidian, TRADER, entry(), role="trader", now=NOW,
            fills=[{"side": "sell", "code": "SOL", "qty": 2.5, "price": 100.0, "usd": 250.0,
                    "fee": 0.25, "realized": -251.0}],
            stats={"closed_trades": 4, "wins": 1, "win_rate_pct": 25.0, "realized_pnl": -300.0,
                   "fees_paid": 2.0, "equity": 200.0})
        body = obsidian.read_note(crypto_journal.SUMMARY_FOLDER,
                                  crypto_journal.summary_title(TRADER))["content"]
        assert "- CLOSED SOL: realised $-251.00" in body
        # The numbers come from the ledger at write time, never from the model's recall.
        assert "25.0%" in body and "4 closed round-trips" in body

    def test_a_close_links_back_to_the_day_the_position_was_opened(self, obsidian, tmp_path):
        import sqlite3
        from assistant.core import market_data, paper_trading
        db = str(tmp_path / "paper.db")
        market_data.init_market_db(db)
        paper_trading.init_paper_db(db)
        conn = sqlite3.connect(db)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""INSERT INTO market_coins (code, name, rank, rate, present,
                                                  first_seen, last_seen, updated_at)
                        VALUES ('SOL','SOL',1,200.0,1,?,?,?)""", (now, now, now))
        conn.commit(); conn.close()
        paper_trading.ensure_account(db, starting_cash=1000.0)
        paper_trading.execute_orders(db, [{"side": "buy", "code": "SOL", "usd": 100}])

        crypto_journal.record_run(
            obsidian, TRADER, None, role="trader", now=NOW, db_path=db,
            fills=[{"side": "sell", "code": "SOL", "qty": 0.5, "price": 100.0, "usd": 50.0,
                    "fee": 0.05, "realized": -50.0}])
        body = obsidian.read_note(crypto_journal.JOURNAL_FOLDER,
                                  crypto_journal.daily_title(TRADER, "2026-09-13"))["content"]
        assert f"see [[{now[:10]} -- {TRADER}]]" in body

    def test_two_runs_on_one_day_share_a_note_and_stay_individually_timestamped(self, obsidian):
        crypto_journal.record_run(obsidian, ANALYST, entry(summary="morning read"),
                                  role="analyst", now=NOW)
        crypto_journal.record_run(obsidian, ANALYST, entry(summary="afternoon read"),
                                  role="analyst",
                                  now=datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc))
        notes = self._notes(obsidian, crypto_journal.JOURNAL_FOLDER)
        assert len(notes) == 1, "one note per employee per day, regardless of run cadence"
        body = obsidian.read_note(crypto_journal.JOURNAL_FOLDER,
                                  crypto_journal.daily_title(ANALYST, "2026-09-13"))["content"]
        assert "### 14:02 UTC" in body and "### 18:30 UTC" in body
        assert "morning read" in body and "afternoon read" in body


class TestCompaction:
    """write_note is append-only by design. Left alone, the one note meant to prevent
    prompt bloat becomes the prompt bloat."""

    def _grow(self, obsidian, title, lessons=15):
        for i in range(lessons):
            obsidian.write_note(
                crypto_journal.SUMMARY_FOLDER, crypto_journal.summary_title(title),
                f"### entry {i}\n- THESIS BTC: call number {i}\n- LESSON: lesson {i}\n"
                + "padding " * 80)

    def test_an_under_threshold_summary_is_left_alone(self, obsidian):
        obsidian.write_note(crypto_journal.SUMMARY_FOLDER, crypto_journal.summary_title(ANALYST),
                            "- LESSON: short and fine")
        out = crypto_journal.compact_summary(obsidian, ANALYST, now=NOW)
        assert not out["compacted"]

    def test_a_missing_summary_is_not_an_error(self, obsidian):
        out = crypto_journal.compact_summary(obsidian, ANALYST, now=NOW)
        assert not out["compacted"] and "no running summary" in out["reason"]

    def test_an_oversized_summary_is_rewritten_smaller(self, obsidian):
        self._grow(obsidian, ANALYST)
        out = crypto_journal.compact_summary(obsidian, ANALYST, now=NOW)
        assert out["compacted"]
        assert out["now"] < out["was"]

    def test_nothing_is_deleted_only_relocated(self, obsidian):
        self._grow(obsidian, ANALYST)
        crypto_journal.compact_summary(obsidian, ANALYST, now=NOW)
        archived = obsidian.read_note(crypto_journal.ARCHIVE_FOLDER,
                                      f"{ANALYST} Summary -- 2026-09-13")["content"]
        # The oldest lesson is gone from the live summary but must still exist somewhere.
        assert "lesson 0" in archived

    def test_the_most_recent_lessons_and_the_current_thesis_survive(self, obsidian):
        self._grow(obsidian, ANALYST, lessons=15)
        crypto_journal.compact_summary(obsidian, ANALYST, keep_lessons=10, now=NOW)
        body = obsidian.read_note(crypto_journal.SUMMARY_FOLDER,
                                  crypto_journal.summary_title(ANALYST))["content"]
        assert "lesson 14" in body and "lesson 5" in body
        assert "lesson 0" not in body
        # Last mention of a ticker wins -- it is the current call, not the first one.
        assert "- THESIS BTC: call number 14" in body
        assert "call number 0" not in body

    def test_the_rewritten_note_is_still_readable_by_the_next_pass(self, obsidian):
        """Compaction has to produce the same line format it consumes, or the second pass
        silently discards everything the first one kept."""
        self._grow(obsidian, ANALYST)
        crypto_journal.compact_summary(obsidian, ANALYST, now=NOW)
        crypto_journal.record_run(obsidian, ANALYST, entry(lesson="a fresh one"),
                                  role="analyst", now=NOW)
        out = crypto_journal.compact_summary(obsidian, ANALYST, force=True, now=NOW)
        assert out["compacted"]
        body = obsidian.read_note(crypto_journal.SUMMARY_FOLDER,
                                  crypto_journal.summary_title(ANALYST))["content"]
        assert "a fresh one" in body and "lesson 14" in body
        # The thesis written after the first compaction supersedes the one that survived
        # it -- "last mention wins" has to hold across a compaction boundary too, or the
        # desk keeps re-reading a call it has already changed its mind about.
        assert "- THESIS BTC: BTC range-bound 74-80k" in body
        assert "call number 14" not in body

    def test_the_compacted_note_stays_readable_as_prior_context(self, obsidian):
        self._grow(obsidian, ANALYST)
        crypto_journal.compact_summary(obsidian, ANALYST, now=NOW)
        out = crypto_journal.build_prior_context(obsidian, ANALYST)
        assert "RUNNING SUMMARY" in out and "lesson 14" in out


class TestHousekeepingSelection:
    """Journalling is an explicitly granted feed, never inferred -- half this roster's job
    descriptions mention crypto, and inferring it would fill the owner's real vault with
    dev-team chatter."""

    @pytest.fixture
    def db(self, tmp_path):
        from assistant.core import staff
        path = str(tmp_path / "staff.db")
        staff.init_staff_db(path)
        return path

    def test_only_employees_granted_the_feed_are_journalled(self, db):
        from assistant.core import staff
        keeper = staff.hire(db, "Desk Trader",
                            "Fifteen years trading crypto markets across spot and derivatives.")["key"]
        other = staff.hire(db, "Backend Dev",
                           "Ten years of Python backend work, including crypto price feeds.")["key"]
        staff.set_data_feeds(db, keeper, "market,journal")
        keys = [e["key"] for e in crypto_journal.journal_employees(db)]
        assert keeper in keys and other not in keys

    def test_housekeeping_with_no_vault_is_a_no_op(self, db):
        assert crypto_journal.run_housekeeping(db, None) == []

    def test_housekeeping_compacts_a_grown_summary(self, db, obsidian):
        from assistant.core import staff
        key = staff.hire(db, "Desk Trader",
                         "Fifteen years trading crypto markets across spot and derivatives.")["key"]
        staff.set_data_feeds(db, key, "market,journal")
        title = staff.get_staff(db, key)["title"]
        for i in range(15):
            obsidian.write_note(crypto_journal.SUMMARY_FOLDER, crypto_journal.summary_title(title),
                                f"- LESSON: lesson {i}\n" + "padding " * 80)
        results = crypto_journal.run_housekeeping(db, obsidian)
        assert results and results[0]["compacted"]

    def test_one_employee_s_vault_failure_does_not_stop_the_rest(self, db, obsidian):
        from assistant.core import staff
        for title in ("Desk Trader", "Desk Analyst"):
            key = staff.hire(db, title,
                             f"{title}: fifteen years across crypto spot and derivatives markets.")["key"]
            staff.set_data_feeds(db, key, "market,journal")

        class HalfBrokenVault:
            def __init__(self, real):
                self.real = real
                self.calls = 0

            def note_path(self, folder, title):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("drive not ready")
                return self.real.note_path(folder, title)

        results = crypto_journal.run_housekeeping(db, HalfBrokenVault(obsidian))
        assert len(results) == 2, "both employees must be attempted"
        assert "OSError" in results[0]["reason"]
