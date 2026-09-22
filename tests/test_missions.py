"""Missions, the attention bus, and the executive tick.

Written from the 2026-09-21 failure, which was two failures with one shape. The weather
watcher sent nineteen texts in three hours about one flood watch; the store produced
nothing for a week and sent none. Both because every component owns a step and nothing
owns an outcome. What has to be right here:

* a number that stops moving is noticed, and a number that never existed counts as the
  most stalled thing there is rather than the newest;
* the blocker found is the most UPSTREAM broken stage, never the loudest symptom;
* Jarvis fixes what it can before it speaks, and speaks when it cannot;
* it never repeats itself -- not the same remedy, and not the same sentence.
"""
from datetime import datetime, timedelta, timezone

import pytest

from assistant.core import (attention, business_db, db as core_db, executive, missions,
                            personal_db)

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "missions.db")
    core_db.init_db(p)
    business_db.init_business_db(p)
    missions.init_missions_db(p)
    attention.init_attention_db(p)
    # A broken worker now lands on his board as well as on his phone, so the tick needs
    # somewhere to put a task.
    personal_db.init_personal_db(p)
    return p


def _mission(path, **kw):
    kw.setdefault("key", "m")
    kw.setdefault("title", "A mission")
    kw.setdefault("metric", "things")
    kw.setdefault("target", 10)
    missions.upsert_mission(path, **kw)
    return missions.get_mission(path, kw["key"])


class TestTheNumber:
    def test_only_movement_is_recorded(self, path):
        """The table holds movements, so the last row IS the last progress."""
        m = _mission(path)
        assert missions.record_reading(path, m["id"], 0, now=NOW) is True
        assert missions.record_reading(path, m["id"], 0, now=NOW + timedelta(hours=1)) is False
        assert missions.record_reading(path, m["id"], 1, now=NOW + timedelta(hours=2)) is True
        assert missions.last_movement(path, m["id"]) == NOW + timedelta(hours=2)

    def test_a_changed_note_refreshes_without_faking_movement(self, path):
        """The store sitting at 0 live while the queue behind it goes from 3 waiting to
        10 is the same number and a different situation. The note must follow, but the
        last-movement time must not: nothing progressed."""
        m = _mission(path)
        missions.record_reading(path, m["id"], 0, "0 live, 3 waiting", now=NOW)
        moved = missions.record_reading(path, m["id"], 0, "0 live, 10 waiting",
                                        now=NOW + timedelta(hours=5))
        assert moved is False, "the number did not move"
        reading = missions.latest_reading(path, m["id"])
        assert reading["note"] == "0 live, 10 waiting"
        assert missions.last_movement(path, m["id"]) == NOW, "movement time must not shift"

    def test_an_unchanged_note_writes_nothing(self, path):
        m = _mission(path)
        missions.record_reading(path, m["id"], 0, "same", now=NOW)
        assert missions.record_reading(path, m["id"], 0, "same",
                                       now=NOW + timedelta(hours=1)) is False
        assert missions.last_movement(path, m["id"]) == NOW

    def test_a_mission_that_has_never_been_read_is_stalled(self, path):
        """None is not zero. A mission with no reading is the most stalled thing in the
        system, not the freshest -- treating it as fresh is how a pipeline that never
        started stays invisible."""
        m = _mission(path)
        assert missions.hours_since_movement(path, m["id"], now=NOW) is None
        assert missions.is_stalled(path, m, now=NOW) is True

    def test_a_number_sitting_still_past_its_limit_is_stalled(self, path):
        m = _mission(path, stall_hours=24)
        missions.record_reading(path, m["id"], 3, now=NOW)
        assert missions.is_stalled(path, m, now=NOW + timedelta(hours=10)) is False
        assert missions.is_stalled(path, m, now=NOW + timedelta(hours=30)) is True

    def test_a_mission_at_its_target_is_never_stalled(self, path):
        """A won mission that stops moving stopped for the right reason."""
        m = _mission(path, target=5, stall_hours=1)
        missions.record_reading(path, m["id"], 5, now=NOW)
        assert missions.is_stalled(path, m, now=NOW + timedelta(days=30)) is False

    def test_a_paused_mission_stops_escalating_without_losing_its_history(self, path):
        m = _mission(path, stall_hours=1)
        missions.record_reading(path, m["id"], 1, now=NOW)
        missions.set_status(path, "m", "paused")
        paused = missions.get_mission(path, "m")
        assert missions.is_stalled(path, paused, now=NOW + timedelta(days=5)) is False
        assert missions.latest_reading(path, m["id"])["value"] == 1


class TestFindingTheBlocker:
    def _run(self, path, agent, status, summary, at):
        import sqlite3
        with sqlite3.connect(path) as conn:
            conn.execute(
                """INSERT INTO agent_runs (agent, started_at, finished_at, status, summary)
                   VALUES (?,?,?,?,?)""",
                (agent, at.isoformat(), at.isoformat(), status, summary))

    def test_an_agent_that_runs_on_time_and_never_produces_is_not_healthy(self, path):
        """The exact signature of the dead store: green everywhere, producing nothing."""
        for i in range(8):
            self._run(path, "store_manager", "skipped", "No approved concepts waiting.",
                      NOW - timedelta(hours=12 * i))
        report = missions.chain_report(path, "store_manager", hours=200, now=NOW)
        assert report[0]["runs"] == 8
        assert report[0]["barren_streak"] == 8
        assert report[0]["producing"] is False

    def test_the_blocker_is_the_most_upstream_broken_stage(self, path):
        """Read from the downstream end and you get the symptom every time. The store's
        loudest complaint was the social director having nothing to post; the cause was
        four stages above it."""
        self._run(path, "trend_scout", "ok", "5 leads.", NOW - timedelta(hours=2))
        for agent in ("product_creator", "art_director", "store_manager", "social_director"):
            self._run(path, agent, "skipped", "Nothing to do.", NOW - timedelta(hours=1))
        report = missions.chain_report(path, executive.STORE_CHAIN, hours=48, now=NOW)
        assert missions.first_blocked_stage(report)["agent"] == "product_creator"

    def test_a_stage_that_never_ran_at_all_is_a_blocker(self, path):
        report = missions.chain_report(path, "trend_scout,product_creator", now=NOW)
        blocked = missions.first_blocked_stage(report)
        assert blocked["agent"] == "trend_scout" and blocked["never_ran"] is True


class TestDecidingWhatToRun:
    def test_a_stage_with_work_queued_is_run_now_not_in_twelve_hours(self):
        """Work sitting in a queue for half a day is most of why nothing moves."""
        blocker = {"agent": "art_director", "last_reason": "No approved concepts."}
        agent, why = executive.decide_remedy(blocker, {"art_director": 2})
        assert agent == "art_director"
        assert "12h" in why

    def test_a_starved_stage_sends_the_work_to_the_stage_above_it(self):
        blocker = {"agent": "art_director", "last_reason": "No approved concepts."}
        agent, _ = executive.decide_remedy(blocker, {"art_director": 0})
        assert agent == "product_creator"

    def test_the_first_stage_producing_nothing_is_escalated_not_worked_around(self):
        """Nothing upstream to lean on: a dry spell and a broken source look identical
        from here, and only he can tell which."""
        blocker = {"agent": "trend_scout", "last_reason": "No leads found."}
        agent, why = executive.decide_remedy(blocker, {})
        assert agent is None and "first stage" in why

    def test_a_healthy_chain_is_left_alone(self):
        assert executive.decide_remedy(None, {})[0] is None


class TestTheAttentionBus:
    def test_the_same_subject_inside_its_cooldown_is_said_once(self, path):
        first = attention.should_say(path, "weather:flood", "Coastal flood watch.", now=NOW)
        again = attention.should_say(path, "weather:flood", "Coastal flood watch.",
                                     now=NOW + timedelta(minutes=10))
        assert first["say"] is True and again["say"] is False

    def test_dedupe_is_by_subject_so_rewording_cannot_defeat_it(self, path):
        """The nineteen texts were nineteen different strings. A dedupe keyed on the
        wording is the dedupe that already failed."""
        attention.should_say(path, "weather:flood", "Coastal Flood watches issued.", now=NOW)
        again = attention.should_say(path, "weather:flood", "coastal flood watches, issued!",
                                     now=NOW + timedelta(minutes=10))
        assert again["say"] is False

    def test_critical_is_never_held(self, path):
        for i in range(30):
            attention.should_say(path, f"noise:{i}", "chatter", now=NOW)
        out = attention.should_say(path, "eas:tornado", "Tornado warning.",
                                   priority="critical", now=NOW)
        assert out["say"] is True

    def test_the_daily_budget_stops_a_flood_of_routine_notices(self, path):
        sent = sum(attention.should_say(path, f"t{i}", f"thing {i}", now=NOW)["say"]
                   for i in range(attention.DAILY_BUDGET + 8))
        assert sent == attention.DAILY_BUDGET

    def test_repetition_is_reported_as_duration_not_repeated(self, path):
        """Once something has been held several times, the useful fact is how long it
        has been true -- not the same sentence for the fourth time."""
        attention.should_say(path, "mission:store", "Store is stuck.", priority="high", now=NOW)
        for i in range(4):
            attention.should_say(path, "mission:store", "Store is stuck.", priority="high",
                                 now=NOW + timedelta(hours=1 + i))
        out = attention.should_say(path, "mission:store", "Store is stuck.",
                                   priority="high", now=NOW + timedelta(hours=10))
        assert out["say"] is True
        assert "still true" in out["body"]

    def test_what_was_held_back_is_kept_and_answerable(self, path):
        """A bus that silently drops things is the failure it was built to prevent."""
        attention.should_say(path, "weather:flood", "Flood watch.", now=NOW)
        for i in range(5):
            attention.should_say(path, "weather:flood", "Flood watch.",
                                 now=NOW + timedelta(minutes=10 * (i + 1)))
        held = attention.held_back(path, now=NOW + timedelta(hours=2))
        assert held[0]["topic"] == "weather:flood" and held[0]["times"] == 5


class TestTheExecutiveTick:
    def _chain_state(self, path, producing=("trend_scout", "product_creator"),
                     barren=("art_director", "store_manager", "social_director")):
        """The real 2026-09-21 shape: the top of the chain works, the middle skips."""
        import sqlite3
        with sqlite3.connect(path) as conn:
            for agent in producing:
                conn.execute(
                    """INSERT INTO agent_runs (agent, started_at, finished_at, status, summary)
                       VALUES (?,?,?,'ok','Produced.')""",
                    (agent, (NOW - timedelta(hours=2)).isoformat(),
                     (NOW - timedelta(hours=2)).isoformat()))
            for agent in barren:
                for i in range(6):
                    at = (NOW - timedelta(hours=2 + 12 * i)).isoformat()
                    conn.execute(
                        """INSERT INTO agent_runs (agent, started_at, finished_at, status, summary)
                           VALUES (?,?,?,'skipped','Nothing waiting.')""", (agent, at, at))

    def _seed_store(self, path, listings_live=0):
        import sqlite3
        with sqlite3.connect(path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
                                id INTEGER PRIMARY KEY, realized REAL, at TEXT)""")
            existing = conn.execute(
                "SELECT count(*) FROM store_listings WHERE status = 'published'").fetchone()[0]
            for i in range(existing, listings_live):
                conn.execute(
                    """INSERT INTO store_listings
                           (owner_user_id, title, status, channel, external_id,
                            created_at, updated_at)
                       VALUES (1,?,?,?,?,?,?)""",
                    (f"thing {i}", "published", "etsy", f"etsy-{i}",
                     NOW.isoformat(), NOW.isoformat()))
        executive.seed_missions(path)

    def test_a_number_that_never_moved_is_not_described_as_recently_stuck(self, path):
        """The stall clock can only start when the mission is created, so a mission
        defined over something that was ALREADY dead reports a tidy "stuck for 30h".
        True, and it understated the store by a week. Say which it is."""
        self._seed_store(path)
        self._chain_state(path)
        said = []
        executive.run_once(path, 1, now=NOW)
        executive.run_once(path, 1, say=lambda topic, body, pr: said.append(body),
                           now=NOW + timedelta(hours=30))
        store = [b for b in said if "Store" in b]
        assert store, said
        assert "never once moved" in store[0]
        assert "for 30h" not in store[0]

    def test_a_listing_no_storefront_has_heard_of_is_not_live(self, path):
        """The metric has to be answerable by someone other than us.

        'published' is this system recording Jack's decision; it pushes to nothing. A
        count that a status flip could satisfy would have reported the store mission WON
        -- ten products live, target met, mission closed -- with not one thing for sale
        anywhere. Jarvis said it plainly on 2026-09-22: "published just records your
        decision internally."
        """
        import sqlite3
        self._seed_store(path)
        with sqlite3.connect(path) as conn:
            conn.execute(
                """INSERT INTO store_listings
                       (owner_user_id, title, status, created_at, updated_at)
                   VALUES (1,?,?,?,?)""",
                ("claimed", "published", NOW.isoformat(), NOW.isoformat()))
        value, note = executive.measure_store(path, 1)
        assert value == 0.0
        assert "marked published but on no real storefront" in note

    def test_it_fixes_what_it_can_before_it_says_anything(self, path, monkeypatch):
        """The half he has never had. A stall that Jarvis can clear itself should cost
        him no attention at all."""
        self._seed_store(path)
        self._chain_state(path)
        monkeypatch.setattr(executive, "store_work_waiting",
                            lambda *a, **k: {"art_director": 2})
        ran, said = [], []
        executive.run_once(path, 1, now=NOW)  # baseline reading
        out = executive.run_once(path, 1, run_agent=ran.append,
                                 say=lambda *a: said.append(a),
                                 now=NOW + timedelta(hours=30))
        store = next(m for m in out["missions"] if m["mission"] == executive.STORE_MISSION)
        assert store["stalled"] is True
        assert ran == ["art_director"], "it should have run the stage sitting on work"
        assert said == [], "and said nothing, because it handled it"

    def test_it_speaks_only_once_its_own_fix_has_failed(self, path, monkeypatch):
        self._seed_store(path)
        self._chain_state(path)
        monkeypatch.setattr(executive, "store_work_waiting",
                            lambda *a, **k: {"art_director": 2})
        ran, said = [], []
        executive.run_once(path, 1, now=NOW)
        executive.run_once(path, 1, run_agent=ran.append, say=lambda *a: said.append(a),
                           now=NOW + timedelta(hours=30))
        executive.run_once(path, 1, run_agent=ran.append, say=lambda *a: said.append(a),
                           now=NOW + timedelta(hours=36))
        assert ran == ["art_director"], "the same failed remedy must not run twice"
        assert len(said) == 1
        assert "did not help last time" in said[0][1]

    def test_a_mission_that_is_moving_is_left_alone_entirely(self, path):
        self._seed_store(path, listings_live=3)
        ran, said = [], []
        executive.run_once(path, 1, now=NOW)
        out = executive.run_once(path, 1, run_agent=ran.append,
                                 say=lambda *a: said.append(a),
                                 now=NOW + timedelta(hours=2))
        store = next(m for m in out["missions"] if m["mission"] == executive.STORE_MISSION)
        assert store["value"] == 3 and store["stalled"] is False
        assert ran == [] and said == []

    def test_progress_closes_out_the_last_intervention(self, path, monkeypatch):
        """So tomorrow's diagnosis is made against a clean slate, not yesterday's excuse."""
        self._seed_store(path)
        self._chain_state(path)
        monkeypatch.setattr(executive, "store_work_waiting",
                            lambda *a, **k: {"art_director": 1})
        executive.run_once(path, 1, now=NOW)
        executive.run_once(path, 1, run_agent=lambda n: None, now=NOW + timedelta(hours=30))
        self._seed_store(path, listings_live=2)
        executive.run_once(path, 1, run_agent=lambda n: None, now=NOW + timedelta(hours=31))
        m = missions.get_mission(path, executive.STORE_MISSION)
        assert missions.recent_interventions(path, m["id"], now=NOW + timedelta(hours=31))[0]["outcome"]

    def test_without_a_runner_it_only_measures(self, path):
        """A paused deployment, and what the tests lean on: nothing is actuated."""
        self._seed_store(path)
        executive.run_once(path, 1, now=NOW)
        out = executive.run_once(path, 1, now=NOW + timedelta(hours=30))
        assert out["missions"], "it should still measure and report"

class TestWatchingTheMachinery:
    """The half that was missing, written from the failure that proved it missing.

    2026-09-20 to 09-22: the crypto day trader died with KeyError: 'qty' on every run
    that raised a stop -- 83 times over two days, more than half its runs. Nothing
    noticed. The mission that owns the desk watched realised P&L, which drifted from $51
    to $55 over the same period because positions opened earlier went on closing
    themselves, so the number never stalled and the diagnosis never ran. Jack found it by
    reading a text.
    """

    def _staff(self, path, key="crypto_day_trader_paper_trading",
               title="Crypto Day Trader (Paper Trading)"):
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE IF NOT EXISTS staff
                        (id INTEGER PRIMARY KEY, key TEXT, title TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS staff_work
                        (id INTEGER PRIMARY KEY, staff_id INTEGER, status TEXT,
                         error TEXT, started_at TEXT)""")
        conn.execute("INSERT INTO staff (key, title) VALUES (?,?)", (key, title))
        sid = conn.execute("SELECT id FROM staff WHERE key = ?", (key,)).fetchone()[0]
        conn.commit()
        return conn, sid

    def _runs(self, conn, sid, pattern, error="KeyError: 'qty'", start=NOW):
        """pattern is a string of o (delivered) and x (failed), oldest first."""
        for i, ch in enumerate(pattern):
            at = (start - timedelta(hours=len(pattern) - i)).isoformat()
            conn.execute(
                "INSERT INTO staff_work (staff_id, status, error, started_at) "
                "VALUES (?,?,?,?)",
                (sid, "failed" if ch == "x" else "delivered",
                 error if ch == "x" else None, at))
        conn.commit()

    def test_a_worker_that_fails_between_successes_is_still_broken(self, path):
        """THE test. A streak counter reads oxoxoxox as healthy -- it never fails twice
        in a row -- and that is exactly the shape the real bug had, because the run only
        died when it happened to raise a stop."""
        conn, sid = self._staff(path)
        self._runs(conn, sid, "oxoxoxoxoxox")
        conn.close()
        broken = executive.broken_workers(path, now=NOW)
        assert len(broken) == 1
        assert broken[0]["failures"] == 6 and broken[0]["runs"] == 12

    def test_a_healthy_worker_is_not_reported(self, path):
        conn, sid = self._staff(path)
        self._runs(conn, sid, "oooooooooooo")
        conn.close()
        assert executive.broken_workers(path, now=NOW) == []

    def test_one_bad_run_is_not_a_broken_worker(self, path):
        """Everything fails occasionally. Waking him for a single timeout is how a
        useful signal becomes noise he filters out."""
        conn, sid = self._staff(path)
        self._runs(conn, sid, "ooooxoooo")
        conn.close()
        assert executive.broken_workers(path, now=NOW) == []

    def test_failures_older_than_the_window_do_not_count(self, path):
        conn, sid = self._staff(path)
        self._runs(conn, sid, "xxxx", start=NOW - timedelta(days=4))
        conn.close()
        assert executive.broken_workers(path, now=NOW) == []

    def test_it_names_the_common_error_not_the_latest(self, path):
        """41 KeyErrors and one unrelated timeout is one problem. Naming the timeout
        because it happened last sends him after the wrong thing."""
        conn, sid = self._staff(path)
        self._runs(conn, sid, "xxxx")
        conn.execute(
            "INSERT INTO staff_work (staff_id, status, error, started_at) VALUES (?,?,?,?)",
            (sid, "failed", "TimeoutError", NOW.isoformat()))
        conn.commit(); conn.close()
        assert "qty" in executive.broken_workers(path, now=NOW)[0]["error"]

    def test_the_report_gives_the_share_of_runs_not_just_a_count(self, path):
        """'Failing' sounds like down. '6 of 12 runs' says limping, which is the thing
        that went unnoticed for two days."""
        conn, sid = self._staff(path)
        self._runs(conn, sid, "oxoxoxoxoxox")
        conn.close()
        line = executive.health_report(executive.broken_workers(path, now=NOW)[0])
        assert "6 of 12 runs" in line and "qty" in line

    def test_he_is_told_and_it_lands_on_his_board(self, path):
        """Two channels deliberately. The text is how he finds out; the task is how it
        survives being read on a phone and forgotten, which is what happened."""
        from assistant.core import personal_db

        conn, sid = self._staff(path)
        self._runs(conn, sid, "oxoxoxoxoxox")
        conn.close()
        said = []
        out = executive.watch_workers(path, 1, say=lambda t, b, p: said.append((t, b)),
                                      now=NOW)
        assert out and out[0]["told"] is True
        assert said and said[0][0] == "worker:crypto_day_trader_paper_trading"
        tasks = [t for t in personal_db.list_tasks(path, 1, status="open")
                 if "crypto_day_trader" in t["text"]]
        assert len(tasks) == 1

    def test_the_same_break_does_not_raise_a_second_task(self, path):
        """Hourly, for a bug that takes days to fix, is 40 identical tasks."""
        from assistant.core import personal_db

        conn, sid = self._staff(path)
        self._runs(conn, sid, "oxoxoxoxoxox")
        conn.close()
        for hour in range(4):
            executive.watch_workers(path, 1, now=NOW + timedelta(hours=hour))
        tasks = [t for t in personal_db.list_tasks(path, 1, status="open")
                 if "crypto_day_trader" in t["text"]]
        assert len(tasks) == 1

    def test_it_stops_saying_it_once_he_has_heard(self, path):
        conn, sid = self._staff(path)
        self._runs(conn, sid, "oxoxoxoxoxox")
        conn.close()
        said = []
        for minute in (0, 5, 10):
            executive.watch_workers(path, 1, say=lambda t, b, p: said.append(b),
                                    now=NOW + timedelta(minutes=minute))
        assert len(said) == 1, "the cooldown is what stopped 83 failures becoming 83 texts"

    def test_the_tick_reports_workers_alongside_missions(self, path):
        conn, sid = self._staff(path)
        self._runs(conn, sid, "oxoxoxoxoxox")
        conn.close()
        executive.seed_missions(path)
        out = executive.run_once(path, 1, now=NOW)
        assert out["workers"] and out["workers"][0]["failures"] == 6
