"""Answering from SQLite instead of from a model.

"What is the current value of the paper trading?" measured 8.9 seconds against his real
database -- one SQL query wrapped in a CLI spawn and an agentic tool loop. This layer
answers it in ~25ms.

What has to be right is mostly the REFUSALS. A wrong instant answer is worse than a slow
right one: it is confidently wrong and it steals the turn from the model that would have
got it right. So most of this file is about the questions it must decline.
"""
from datetime import datetime, timedelta, timezone

import pytest

from assistant.core import (attention, business_db, db as core_db, direct_answers,
                            executive, missions)

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "direct.db")
    core_db.init_db(p)
    business_db.init_business_db(p)
    missions.init_missions_db(p)
    attention.init_attention_db(p)
    executive.seed_missions(p)
    return p


@pytest.fixture
def stalled(path):
    """The store sat at zero for well over a day and a remedy was already tried.

    Times are offsets from the real clock, not from a frozen NOW: snapshot() reads the
    wall clock, so a fixture on a fake one produced "-595 minutes ago".
    """
    import sqlite3
    real_now = datetime.now(timezone.utc)
    # The real 2026-09-21 shape: the top of the chain produced, art_director skipped.
    with sqlite3.connect(path) as conn:
        for agent, status in (("trend_scout", "ok"), ("product_creator", "ok"),
                              ("art_director", "skipped"), ("store_manager", "skipped"),
                              ("social_director", "skipped")):
            at = (real_now - timedelta(hours=3)).isoformat()
            conn.execute(
                """INSERT INTO agent_runs (agent, started_at, finished_at, status, summary)
                   VALUES (?,?,?,?,?)""",
                (agent, at, at, status,
                 "Produced." if status == "ok" else "No approved concepts waiting on artwork."))
    m = missions.get_mission(path, executive.STORE_MISSION)
    missions.record_reading(path, m["id"], 0, "0 live, 0 waiting to go live",
                            now=real_now - timedelta(hours=40))
    missions.record_intervention(path, m["id"], "stuck at 0", "ran art_director",
                                 now=real_now - timedelta(hours=2))
    crypto = missions.get_mission(path, executive.CRYPTO_MISSION)
    missions.record_reading(path, crypto["id"], 53.06, "391 trades in 30 days",
                            now=real_now - timedelta(minutes=5))
    return path


class TestItAnswers:
    def test_his_actual_question_that_took_nine_seconds(self, stalled):
        out = direct_answers.try_direct_answer(
            stalled, "What is the current value of the paper trading?")
        assert out is not None and "$53.06" in out

    @pytest.mark.parametrize("question", [
        "how is the store doing?", "whats the status of the store",
        "hows the shop", "where is the store at", "store status",
    ])
    def test_the_same_question_however_he_phrases_it(self, stalled, question):
        assert "0 of 10" in (direct_answers.try_direct_answer(stalled, question) or "")

    def test_a_stalled_mission_says_why_and_what_was_tried(self, stalled):
        """The two things he said he never gets told."""
        out = direct_answers.try_direct_answer(stalled, "how is the store doing?")
        assert "Hasn't moved in 40 hours" in out
        assert "ran art_director" in out

    def test_the_measure_note_is_included_when_it_adds_something(self, stalled):
        """Live example that prompted this: the store read "0 of 10", which sounds like
        a dead shop, while the note read "0 live, 3 waiting to go live" -- the factory
        works and the door is shut. Different problem, different next action."""
        m = missions.get_mission(stalled, executive.STORE_MISSION)
        missions.record_reading(stalled, m["id"], 0, "0 live, 3 waiting to go live",
                                now=datetime.now(timezone.utc))
        out = direct_answers.try_direct_answer(stalled, "how is the store doing?")
        assert "3 waiting to go live" in out

    def test_a_note_that_only_repeats_the_number_is_not_echoed(self, stalled):
        m = missions.get_mission(stalled, executive.STORE_MISSION)
        missions.record_reading(stalled, m["id"], 7, "7", now=datetime.now(timezone.utc))
        out = direct_answers.try_direct_answer(stalled, "how is the store doing?")
        assert "(7)" not in out

    def test_longest_subject_wins_so_paper_trading_is_not_the_store(self, stalled):
        assert "$53.06" in direct_answers.try_direct_answer(stalled, "hows paper trading")

    def test_what_is_blocked_lists_only_the_stalled_ones(self, stalled):
        out = direct_answers.try_direct_answer(stalled, "whats blocked on me?")
        assert "Jarvis Store" in out and "Crypto desk" not in out

    def test_nothing_blocked_says_so_plainly(self, path):
        for key in (executive.STORE_MISSION, executive.CRYPTO_MISSION):
            m = missions.get_mission(path, key)
            missions.record_reading(path, m["id"], 1, now=datetime.now(timezone.utc))
        assert "Nothing's blocked" in direct_answers.try_direct_answer(path, "whats blocked")

    def test_a_bare_status_gives_the_whole_board(self, stalled):
        out = direct_answers.try_direct_answer(stalled, "status")
        assert "Jarvis Store" in out and "Crypto desk" in out


class TestItRefuses:
    """Every one of these must reach the real model untouched."""

    @pytest.mark.parametrize("question", [
        "make three more products for the store",
        "how is the store doing? also publish the next listing",
        "can you fix the store pipeline",
        "start the crypto desk again",
        "set the store target to 20",
    ])
    def test_anything_asking_for_work_is_never_answered_from_a_table(self, stalled, question):
        assert direct_answers.try_direct_answer(stalled, question) is None

    @pytest.mark.parametrize("question", [
        "what do you think we should do about the store this month",
        "why is the store not selling anything, what would you change",
    ])
    def test_a_question_wanting_judgement_goes_to_the_model(self, stalled, question):
        assert direct_answers.try_direct_answer(stalled, question) is None

    def test_an_unknown_subject_is_not_guessed_at(self, stalled):
        assert direct_answers.try_direct_answer(stalled, "how is the greenhouse doing") is None

    def test_a_subject_with_no_question_is_left_alone(self, stalled):
        assert direct_answers.try_direct_answer(stalled, "the store") is None

    def test_a_long_message_is_a_conversation_not_a_lookup(self, stalled):
        long = ("how is the store doing " + "and what about everything else " * 6).strip()
        assert len(long) > 160
        assert direct_answers.try_direct_answer(stalled, long) is None

    def test_an_empty_message_is_declined(self, stalled):
        assert direct_answers.try_direct_answer(stalled, "   ") is None

    def test_a_database_with_no_missions_declines_rather_than_inventing(self, tmp_path):
        p = str(tmp_path / "bare.db")
        core_db.init_db(p)
        assert direct_answers.try_direct_answer(p, "how is the store doing") is None

    def test_a_broken_lookup_never_swallows_the_turn(self, stalled, monkeypatch):
        monkeypatch.setattr(missions, "snapshot",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
        assert direct_answers.try_direct_answer(stalled, "how is the store doing") is None
