"""The shape of a day, and the rules that decide what he is asked to do.

These exist because the failure mode is not a crash, it is a list he stops trusting. Three
rules carry that trust, and each is pinned here:

  - Work he CANNOT do is never offered. "I can't book the trip until Ghost is situated
    with a vet and a boarding facility" -- offering the flight anyway teaches him the list
    is guessing, and a list he distrusts is one he closes.
  - A task that has sat for weeks surfaces on its own. It is not low priority, it is
    avoided, and if age did not count it would stay invisible forever.
  - A nudge that arrives after the moment has passed is a reproach, not a reminder, so a
    missed window is dropped rather than sent late.
"""
from datetime import date, datetime, timedelta

import pytest

from assistant.core import db as core_db, personal_db, routine

TODAY = date(2026, 9, 16)          # a Wednesday
WEEKDAY_OF_TODAY = TODAY.weekday()  # 2


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "day.db")
    core_db.init_db(path)
    personal_db.init_personal_db(path)
    core_db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner(db_path):
    return core_db.get_user_by_chat_id(db_path, "111")["id"]


def _task(db_path, owner, text, **kw):
    return personal_db.create_task(db_path, owner, text, **kw)


def _age(days):
    return (TODAY - timedelta(days=days)).isoformat()


class TestWeighing:
    """The score always ships with its reasons: a number alone is something to argue
    with, 'sat for 10 days, unblocks 2 others' is something to act on."""

    def test_an_old_task_outranks_a_fresh_one(self):
        old = routine.weigh_task({"created_at": _age(20), "priority": "normal"}, TODAY)
        new = routine.weigh_task({"created_at": _age(0), "priority": "normal"}, TODAY)
        assert old["score"] > new["score"]
        assert any("sat for 20 days" in r for r in old["reasons"])

    def test_ageing_stops_climbing_so_it_cannot_drown_a_deadline(self):
        """A task from March must not outrank one due today that unblocks two others."""
        ancient = routine.weigh_task({"created_at": _age(400), "priority": "normal"}, TODAY)
        urgent = routine.weigh_task(
            {"created_at": _age(1), "priority": "normal", "due_at": TODAY.isoformat()},
            TODAY, blocks_count=2)
        assert ancient["score"] <= routine.AGE_WEIGHT_CAP
        assert urgent["score"] > ancient["score"]

    def test_overdue_beats_due_today_beats_due_soon(self):
        def at(days):
            return routine.weigh_task(
                {"created_at": _age(1), "priority": "normal",
                 "due_at": (TODAY + timedelta(days=days)).isoformat()}, TODAY)["score"]
        assert at(-2) > at(0) > at(2) > at(30)

    def test_unblocking_others_raises_the_score(self):
        """Getting Ghost a vet is worth more than it looks: the trip is waiting on it."""
        alone = routine.weigh_task({"created_at": _age(5), "priority": "normal"}, TODAY)
        unblocks = routine.weigh_task({"created_at": _age(5), "priority": "normal"},
                                      TODAY, blocks_count=2)
        assert unblocks["score"] > alone["score"]
        assert "unblocks 2 others" in unblocks["reasons"]

    def test_something_already_started_beats_starting_something_new(self):
        started = routine.weigh_task(
            {"created_at": _age(3), "priority": "normal", "status": "doing"}, TODAY)
        fresh = routine.weigh_task(
            {"created_at": _age(3), "priority": "normal", "status": "open"}, TODAY)
        assert started["score"] > fresh["score"]
        assert "already started" in started["reasons"]

    def test_a_low_priority_task_is_pushed_down_not_hidden(self):
        low = routine.weigh_task({"created_at": _age(2), "priority": "low"}, TODAY)
        assert low["score"] < routine.weigh_task(
            {"created_at": _age(2), "priority": "normal"}, TODAY)["score"]

    def test_a_task_with_no_dates_at_all_still_scores(self):
        assert routine.weigh_task({"priority": "normal"}, TODAY)["score"] is not None

    def test_an_unparseable_date_does_not_crash_the_plan(self):
        weighed = routine.weigh_task(
            {"created_at": "not a date", "due_at": "also not", "priority": "normal"}, TODAY)
        assert weighed["score"] == 0.0


class TestBlockedWorkIsNeverOffered:
    def test_a_blocked_task_is_held_back_and_explained(self, db_path, owner):
        """The real case, in his words."""
        vet = _task(db_path, owner, "Find a vet for Ghost and get his records")
        boarding = _task(db_path, owner, "Find a boarding place for Ghost")
        flight = _task(db_path, owner, "Book the flight")
        personal_db.block_task(db_path, flight, vet)
        personal_db.block_task(db_path, flight, boarding)

        plan = routine.shortlist(db_path, owner, TODAY)
        assert flight not in [t["id"] for t in plan["pick_from"]]
        blocked = next(t for t in plan["blocked"] if t["id"] == flight)
        assert {b["id"] for b in blocked["waiting_on"]} == {vet, boarding}

    def test_finishing_one_of_two_blockers_does_not_release_the_task(self, db_path, owner):
        """Booking the flight needs BOTH. Collapsing that to one blocker would clear it
        the moment either finished, which is the bug the join table exists to prevent."""
        vet = _task(db_path, owner, "Vet")
        boarding = _task(db_path, owner, "Boarding")
        flight = _task(db_path, owner, "Flight")
        personal_db.block_task(db_path, flight, vet)
        personal_db.block_task(db_path, flight, boarding)
        personal_db.update_task(db_path, owner, vet, status="done")

        plan = routine.shortlist(db_path, owner, TODAY)
        assert flight in [t["id"] for t in plan["blocked"]]
        assert [b["id"] for b in plan["blocked"][0]["waiting_on"]] == [boarding]

    def test_finishing_the_last_blocker_releases_it_with_no_edit(self, db_path, owner):
        vet = _task(db_path, owner, "Vet")
        flight = _task(db_path, owner, "Flight")
        personal_db.block_task(db_path, flight, vet)
        personal_db.update_task(db_path, owner, vet, status="done")
        plan = routine.shortlist(db_path, owner, TODAY)
        assert flight in [t["id"] for t in plan["pick_from"]]
        assert plan["blocked"] == []

    def test_a_dropped_blocker_also_releases_the_task(self, db_path, owner):
        """Abandoning the boarding search should not strand the flight forever."""
        boarding = _task(db_path, owner, "Boarding")
        flight = _task(db_path, owner, "Flight")
        personal_db.block_task(db_path, flight, boarding)
        personal_db.update_task(db_path, owner, boarding, status="dropped")
        assert flight in [t["id"] for t in routine.shortlist(db_path, owner, TODAY)["pick_from"]]

    def test_a_task_cannot_block_itself(self, db_path, owner):
        task = _task(db_path, owner, "Thing")
        with pytest.raises(ValueError):
            personal_db.block_task(db_path, task, task)

    def test_a_cycle_is_refused_rather_than_silently_losing_both(self, db_path, owner):
        a = _task(db_path, owner, "A")
        b = _task(db_path, owner, "B")
        personal_db.block_task(db_path, a, b)
        with pytest.raises(ValueError):
            personal_db.block_task(db_path, b, a)

    def test_a_longer_cycle_is_refused_too(self, db_path, owner):
        a, b, c = (_task(db_path, owner, n) for n in ("A", "B", "C"))
        personal_db.block_task(db_path, a, b)
        personal_db.block_task(db_path, b, c)
        with pytest.raises(ValueError):
            personal_db.block_task(db_path, c, a)

    def test_the_shortlist_stays_short(self, db_path, owner):
        """Thirty-three tasks sorted by date is a wall, and a wall gets closed."""
        for i in range(20):
            _task(db_path, owner, f"Task {i}")
        plan = routine.shortlist(db_path, owner, TODAY, limit=5)
        assert len(plan["pick_from"]) == 5
        assert plan["open_count"] == 20


class TestTheThingsThatKeepLifeInBalance:
    def _habit(self, db_path, owner, name, target, category="health"):
        return personal_db.add_rhythm(db_path, owner, name, "habit",
                                      category=category, target_per_week=target)

    def test_a_habit_never_done_is_slipping(self, db_path, owner):
        self._habit(db_path, owner, "Stationary bike", 4)
        status = routine.rhythm_status(db_path, owner, TODAY)
        assert status["habits"][0]["slipping"] is True

    def test_doing_it_today_stops_it_slipping(self, db_path, owner):
        bike = self._habit(db_path, owner, "Stationary bike", 4)
        personal_db.log_rhythm(db_path, bike, TODAY.isoformat(), "done")
        assert routine.rhythm_status(db_path, owner, TODAY)["habits"][0]["slipping"] is False

    def test_slipping_is_measured_against_that_habit_s_own_rate(self, db_path, owner):
        """Three quiet days is nothing for a twice-a-week thing and a real gap for a
        four-times-a-week one. A fixed threshold would nag about one and miss the other."""
        often = self._habit(db_path, owner, "Bike", 4)
        rarely = self._habit(db_path, owner, "See Nadia", 1, category="relationship")
        three_days_ago = (TODAY - timedelta(days=3)).isoformat()
        personal_db.log_rhythm(db_path, often, three_days_ago, "done")
        personal_db.log_rhythm(db_path, rarely, three_days_ago, "done")

        by_name = {h["name"]: h for h in routine.rhythm_status(db_path, owner, TODAY)["habits"]}
        assert by_name["Bike"]["slipping"] is True
        assert by_name["See Nadia"]["slipping"] is False

    def test_it_counts_progress_against_the_weekly_target(self, db_path, owner):
        bike = self._habit(db_path, owner, "Bike", 4)
        monday = TODAY - timedelta(days=TODAY.weekday())
        for offset in (0, 1):
            personal_db.log_rhythm(db_path, bike, (monday + timedelta(days=offset)).isoformat(), "done")
        habit = routine.rhythm_status(db_path, owner, TODAY)["habits"][0]
        assert habit["this_week"] == 2
        assert habit["remaining_this_week"] == 2

    def test_a_skip_is_recorded_as_an_answer_not_an_absence(self, db_path, owner):
        bike = self._habit(db_path, owner, "Bike", 4)
        personal_db.log_rhythm(db_path, bike, TODAY.isoformat(), "skipped", note="away that night")
        habit = routine.rhythm_status(db_path, owner, TODAY)["habits"][0]
        assert habit["today_state"] == "skipped"
        assert habit["this_week"] == 0, "a skip is not progress"

    def test_logging_twice_in_a_day_cannot_inflate_a_streak(self, db_path, owner):
        bike = self._habit(db_path, owner, "Bike", 4)
        personal_db.log_rhythm(db_path, bike, TODAY.isoformat(), "done")
        personal_db.log_rhythm(db_path, bike, TODAY.isoformat(), "done")
        assert routine.rhythm_status(db_path, owner, TODAY)["habits"][0]["this_week"] == 1

    def test_changing_your_mind_overwrites_rather_than_adds(self, db_path, owner):
        bike = self._habit(db_path, owner, "Bike", 4)
        personal_db.log_rhythm(db_path, bike, TODAY.isoformat(), "skipped")
        personal_db.log_rhythm(db_path, bike, TODAY.isoformat(), "done")
        habit = routine.rhythm_status(db_path, owner, TODAY)["habits"][0]
        assert habit["today_state"] == "done" and habit["this_week"] == 1


class TestAnchors:
    def test_a_weekday_anchor_is_not_due_at_the_weekend(self, db_path, owner):
        personal_db.add_rhythm(db_path, owner, "Leave for work", "anchor", category="work",
                               at_time="07:30", days="0,1,2,3,4", lead_minutes=15)
        saturday = date(2026, 9, 19)
        assert routine.rhythm_status(db_path, owner, TODAY)["anchors"][0]["due_today"] is True
        assert routine.rhythm_status(db_path, owner, saturday)["anchors"][0]["due_today"] is False

    def test_an_anchor_with_no_days_runs_every_day(self, db_path, owner):
        personal_db.add_rhythm(db_path, owner, "Feed Ghost", "anchor", category="care",
                               at_time="06:45", lead_minutes=0)
        for day in (TODAY, date(2026, 9, 19), date(2026, 9, 20)):
            assert routine.rhythm_status(db_path, owner, day)["anchors"][0]["due_today"] is True

    def test_an_anchor_needs_a_time_and_a_habit_needs_a_target(self, db_path, owner):
        with pytest.raises(ValueError):
            personal_db.add_rhythm(db_path, owner, "No time", "anchor")
        with pytest.raises(ValueError):
            personal_db.add_rhythm(db_path, owner, "No target", "habit")


class TestNudges:
    def _anchor(self, db_path, owner, name="Leave for work", at="07:30", lead=15, **kw):
        return personal_db.add_rhythm(db_path, owner, name, "anchor", category="work",
                                      at_time=at, lead_minutes=lead, **kw)

    def _at(self, hour, minute):
        return datetime(2026, 9, 16, hour, minute)

    def test_it_fires_inside_the_lead_window(self, db_path, owner):
        self._anchor(db_path, owner)
        nudges = routine.due_nudges(db_path, owner, now=self._at(7, 20))
        assert len(nudges) == 1
        assert "10 min" in nudges[0]["text"]

    def test_it_stays_quiet_before_the_window_opens(self, db_path, owner):
        self._anchor(db_path, owner)
        assert routine.due_nudges(db_path, owner, now=self._at(6, 0)) == []

    def test_a_missed_window_is_dropped_rather_than_sent_late(self, db_path, owner):
        """"You were supposed to leave 20 minutes ago" is a reproach, not a reminder."""
        self._anchor(db_path, owner)
        assert routine.due_nudges(db_path, owner, now=self._at(7, 50)) == []

    def test_something_already_done_is_not_nudged(self, db_path, owner):
        ghost = self._anchor(db_path, owner, "Feed Ghost", at="06:45", lead=10)
        personal_db.log_rhythm(db_path, ghost, TODAY.isoformat(), "done")
        assert routine.due_nudges(db_path, owner, now=self._at(6, 40)) == []

    def test_a_nudge_is_sent_once_however_often_the_scheduler_ticks(self, db_path, owner):
        rhythm_id = self._anchor(db_path, owner)
        assert routine.record_nudge(db_path, rhythm_id, "lead", TODAY.isoformat()) is True
        assert routine.record_nudge(db_path, rhythm_id, "lead", TODAY.isoformat()) is False

    def test_an_already_sent_nudge_is_not_offered_again(self, db_path, owner):
        rhythm_id = self._anchor(db_path, owner)
        routine.record_nudge(db_path, rhythm_id, "lead", TODAY.isoformat())
        assert routine.due_nudges(db_path, owner, now=self._at(7, 20)) == []

    def test_an_item_with_no_lead_is_tracked_but_never_chased(self, db_path, owner):
        self._anchor(db_path, owner, "Bedtime", at="22:30", lead=None)
        assert routine.due_nudges(db_path, owner, now=self._at(22, 20)) == []

    def test_an_anchor_that_does_not_run_today_is_not_nudged(self, db_path, owner):
        self._anchor(db_path, owner, days="5,6")   # weekends only
        assert routine.due_nudges(db_path, owner, now=self._at(7, 20)) == []

    def test_a_hard_anchor_is_marked_so_it_can_survive_a_quiet_day(self, db_path, owner):
        self._anchor(db_path, owner, hard=True)
        assert routine.due_nudges(db_path, owner, now=self._at(7, 20))[0]["hard"] is True


class TestPlanDay:
    def test_it_answers_all_three_questions_in_one_call(self, db_path, owner):
        personal_db.add_rhythm(db_path, owner, "Feed Ghost", "anchor", category="care",
                               at_time="06:45", lead_minutes=0)
        personal_db.add_rhythm(db_path, owner, "Bike", "habit", category="health",
                               target_per_week=4)
        _task(db_path, owner, "Find a vet for Ghost")
        plan = routine.plan_day(db_path, owner, today=TODAY)
        assert plan["weekday"] == "Wednesday"
        assert [a["name"] for a in plan["anchors"]] == ["Feed Ghost"]
        assert [h["name"] for h in plan["slipping"]] == ["Bike"]
        assert len(plan["pick_from"]) == 1

    def test_an_empty_day_is_not_an_error(self, db_path, owner):
        plan = routine.plan_day(db_path, owner, today=TODAY)
        assert plan["pick_from"] == [] and plan["anchors"] == [] and plan["open_count"] == 0


class TestWhatATaskNeedsToBeDone:
    def test_details_are_typed_so_the_ui_can_make_them_usable(self, db_path, owner):
        """A number buried in a paragraph is one he has to retype into his phone."""
        task = _task(db_path, owner, "Find a vet for Ghost")
        personal_db.add_task_detail(db_path, task, "phone", "804-555-0142", label="Old vet")
        personal_db.add_task_detail(db_path, task, "address", "123 Main St, Richmond VA")
        personal_db.add_task_detail(db_path, task, "person", "Dr. Alvarez")
        details = personal_db.list_task_details(db_path, [task])[task]
        assert [d["kind"] for d in details] == ["phone", "address", "person"]
        assert details[0]["label"] == "Old vet"

    def test_an_unknown_kind_is_refused(self, db_path, owner):
        task = _task(db_path, owner, "Thing")
        with pytest.raises(ValueError):
            personal_db.add_task_detail(db_path, task, "sms", "x")

    def test_an_empty_value_is_refused(self, db_path, owner):
        task = _task(db_path, owner, "Thing")
        with pytest.raises(ValueError):
            personal_db.add_task_detail(db_path, task, "phone", "   ")

    def test_details_keep_the_order_they_were_added_in(self, db_path, owner):
        task = _task(db_path, owner, "Thing")
        for n in ("first", "second", "third"):
            personal_db.add_task_detail(db_path, task, "note", n)
        assert [d["value"] for d in personal_db.list_task_details(db_path, [task])[task]] \
            == ["first", "second", "third"]

    def test_deleting_the_task_takes_its_details_with_it(self, db_path, owner):
        import sqlite3
        task = _task(db_path, owner, "Thing")
        personal_db.add_task_detail(db_path, task, "phone", "804-555-0142")
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM personal_tasks WHERE id = ?", (task,))
        assert personal_db.list_task_details(db_path, [task]) == {}


class TestDelivery:
    """He asked to be reminded by text. A push notification behind a lock screen he is
    not looking at is not the same thing, so SMS leads and Telegram is the safety net."""

    def _anchor(self, db_path, owner):
        return personal_db.add_rhythm(db_path, owner, "Leave for work", "anchor",
                                      category="work", at_time="07:30", lead_minutes=15)

    def _at(self, hour, minute):
        return datetime(2026, 9, 16, hour, minute)

    def test_it_texts_him(self, db_path, owner, monkeypatch):
        self._anchor(db_path, owner)
        sent = []
        from assistant.core import cellular
        monkeypatch.setattr(cellular, "queue_outbound",
                            lambda db, number, text, **kw: sent.append((number, text)) or 1)
        out = routine.send_due_nudges(db_path, owner, sms_number="+15551234567",
                                      now=self._at(7, 20))
        assert len(out) == 1 and out[0]["delivered"] is True
        assert sent[0][0] == "+15551234567"
        assert "Leave for work" in sent[0][1]

    def test_a_dead_modem_falls_back_rather_than_losing_the_reminder(self, db_path, owner, monkeypatch):
        """A missed 'leave for work' is worse than one delivered the ordinary way."""
        self._anchor(db_path, owner)
        from assistant.core import cellular
        monkeypatch.setattr(cellular, "queue_outbound",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no modem")))
        fallback = []
        out = routine.send_due_nudges(db_path, owner, sms_number="+15551234567",
                                      notify=fallback.append, now=self._at(7, 20))
        assert out[0]["delivered"] is True
        assert "Leave for work" in fallback[0]

    def test_with_no_number_configured_it_still_reaches_him(self, db_path, owner):
        self._anchor(db_path, owner)
        fallback = []
        out = routine.send_due_nudges(db_path, owner, sms_number=None,
                                      notify=fallback.append, now=self._at(7, 20))
        assert out[0]["delivered"] is True and len(fallback) == 1

    def test_ticking_every_minute_texts_him_once(self, db_path, owner, monkeypatch):
        """The scheduler runs this every 60s across a 15-minute window. Sending on each
        tick is how a person mutes an assistant."""
        self._anchor(db_path, owner)
        sent = []
        from assistant.core import cellular
        monkeypatch.setattr(cellular, "queue_outbound",
                            lambda db, number, text, **kw: sent.append(text) or 1)
        for minute in range(16, 30):
            routine.send_due_nudges(db_path, owner, sms_number="+1555",
                                    now=self._at(7, minute))
        assert len(sent) == 1

    def test_a_failed_send_is_reported_rather_than_silently_swallowed(self, db_path, owner, monkeypatch):
        self._anchor(db_path, owner)
        from assistant.core import cellular
        monkeypatch.setattr(cellular, "queue_outbound",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no modem")))
        out = routine.send_due_nudges(db_path, owner, sms_number="+1555",
                                      notify=None, now=self._at(7, 20))
        assert out[0]["delivered"] is False

    def test_nothing_due_sends_nothing(self, db_path, owner):
        self._anchor(db_path, owner)
        assert routine.send_due_nudges(db_path, owner, sms_number="+1555",
                                       now=self._at(3, 0)) == []


class TestWhatLandsOnHisPhone:
    """The calendar is already synced to his phone, and his phone is where he looks when
    he is out. An event that is only a title sends him back to a laptop to find the number,
    which defeats the point of putting it in the calendar."""

    def test_every_detail_reaches_the_note(self):
        note = routine.task_note(
            {"text": "Find a vet"},
            [{"kind": "phone", "label": "Old vet", "value": "512-555-0142"},
             {"kind": "address", "label": None, "value": "123 W Broad St"},
             {"kind": "person", "label": None, "value": "Dr. Alvarez"}])
        assert "512-555-0142" in note and "123 W Broad St" in note and "Dr. Alvarez" in note

    def test_each_kind_says_what_to_do_with_it(self):
        note = routine.task_note({"text": "t"}, [
            {"kind": "phone", "label": None, "value": "512-555-0142"},
            {"kind": "address", "label": None, "value": "123 Main"}])
        assert "Call" in note and "Go to" in note

    def test_a_phone_number_is_left_exactly_as_written(self):
        """iOS linkifies numbers itself; reformatting is how that stops working."""
        note = routine.task_note({"text": "t"}, [
            {"kind": "phone", "label": None, "value": "(512) 555-0142"}])
        assert "(512) 555-0142" in note

    def test_a_plain_note_carries_no_prefix(self):
        note = routine.task_note({"text": "t"}, [
            {"kind": "note", "label": None, "value": "Ask about the rabies cert."}])
        assert note == "Ask about the rabies cert."

    def test_blockers_are_named_so_he_does_not_start_the_wrong_thing(self):
        note = routine.task_note({"text": "Book flight"}, [],
                                 [{"id": 1, "text": "Vet records"},
                                  {"id": 2, "text": "Boarding"}])
        assert "Waiting on: Vet records; Boarding" in note

    def test_the_due_date_is_included_when_there_is_one(self):
        note = routine.task_note({"text": "t", "due_at": "2026-09-30T12:00:00"}, [])
        assert "Due 2026-09-30" in note

    def test_a_task_with_nothing_attached_produces_an_empty_note(self):
        """An empty description must not become a stray blank line in his calendar."""
        assert routine.task_note({"text": "t"}, []) == ""

    def test_it_is_plain_text_not_markdown(self):
        """iOS renders calendar notes as-is, so asterisks would show up as asterisks."""
        note = routine.task_note({"text": "t"}, [
            {"kind": "phone", "label": "Vet", "value": "512-555-0142"}])
        assert "*" not in note and "#" not in note

    def test_a_detail_with_an_empty_value_is_skipped(self):
        note = routine.task_note({"text": "t"}, [
            {"kind": "phone", "label": "Vet", "value": "   "},
            {"kind": "note", "label": None, "value": "real"}])
        assert note == "real"

    def test_it_loads_details_and_blockers_from_the_database(self, db_path, owner):
        vet = _task(db_path, owner, "Find a vet")
        boarding = _task(db_path, owner, "Find boarding")
        personal_db.block_task(db_path, boarding, vet)
        personal_db.add_task_detail(db_path, boarding, "phone", "804-555-0199", label="Kennel")
        note = routine.task_note_for(db_path, boarding, owner)
        assert "804-555-0199" in note
        assert "Waiting on: Find a vet" in note

    def test_a_missing_task_returns_nothing_rather_than_raising(self, db_path, owner):
        assert routine.task_note_for(db_path, 999999, owner) == ""
