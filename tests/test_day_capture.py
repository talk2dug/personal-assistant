"""The Day planner rebuild's new backend pieces: quick-capture inbox, daily notes, task
steps, task history, the week strip and tomorrow's brief.
"""
from datetime import date, timedelta

import pytest

from assistant.core import db as core_db, personal_db, routine


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "day.db")
    core_db.init_db(path)
    personal_db.init_personal_db(path)
    core_db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner(db):
    return core_db.get_user_by_chat_id(db, "111")["id"]


def day(n=0):
    return (date.today() + timedelta(days=n)).isoformat()


class TestCapture:
    def test_capture_then_sort_into_a_task(self, db, owner):
        capture_id = personal_db.add_capture(db, owner, "Ghost is out of the salmon food", day())
        [item] = personal_db.list_capture(db, owner, day())
        assert item["text"] == "Ghost is out of the salmon food"
        assert item["sorted_into_task_id"] is None

        task_id = personal_db.create_task(db, owner, "Buy salmon food", track="personal")
        assert personal_db.sort_capture(db, owner, capture_id, task_id) is True
        [item] = personal_db.list_capture(db, owner, day())
        assert item["sorted_into_task_id"] == task_id

    def test_unsorted_only_hides_what_has_a_task(self, db, owner):
        c1 = personal_db.add_capture(db, owner, "quote the hotel", day())
        personal_db.add_capture(db, owner, "still loose", day())
        task_id = personal_db.create_task(db, owner, "quote the hotel", track="personal")
        personal_db.sort_capture(db, owner, c1, task_id)
        unsorted = personal_db.list_capture(db, owner, day(), unsorted_only=True)
        assert [c["text"] for c in unsorted] == ["still loose"]

    def test_capture_is_scoped_to_its_date(self, db, owner):
        personal_db.add_capture(db, owner, "today's thing", day())
        personal_db.add_capture(db, owner, "yesterday's thing", day(-1))
        assert [c["text"] for c in personal_db.list_capture(db, owner, day())] == ["today's thing"]


class TestDayNotes:
    def test_add_list_delete(self, db, owner):
        note_id = personal_db.add_day_note(db, owner, day(), "Press ran hot on the black pass")
        assert [n["text"] for n in personal_db.list_day_notes(db, owner, day())] == \
            ["Press ran hot on the black pass"]
        assert personal_db.delete_day_note(db, owner, note_id) is True
        assert personal_db.list_day_notes(db, owner, day()) == []


class TestTaskSteps:
    def test_steps_keep_order_and_toggle(self, db, owner):
        task_id = personal_db.create_task(db, owner, "Ship the order", track="personal")
        s1 = personal_db.add_task_step(db, task_id, "Print label")
        s2 = personal_db.add_task_step(db, task_id, "Box it up")
        steps = personal_db.list_task_steps(db, [task_id])[task_id]
        assert [s["text"] for s in steps] == ["Print label", "Box it up"]
        assert all(s["done"] == 0 for s in steps)

        assert personal_db.toggle_task_step(db, s1, True) is True
        steps = personal_db.list_task_steps(db, [task_id])[task_id]
        assert steps[0]["done"] == 1 and steps[0]["done_at"] is not None
        assert steps[1]["done"] == 0

    def test_deleting_a_step(self, db, owner):
        task_id = personal_db.create_task(db, owner, "Ship the order", track="personal")
        step_id = personal_db.add_task_step(db, task_id, "Print label")
        assert personal_db.delete_task_step(db, step_id) is True
        assert personal_db.list_task_steps(db, [task_id]) == {}


class TestTaskHistory:
    def test_create_pick_unpick_and_status_all_log(self, db, owner):
        task_id = personal_db.create_task(db, owner, "Find a vet for Ghost", track="personal")
        personal_db.pick_for_day(db, owner, task_id, day())
        personal_db.unpick_for_day(db, owner, task_id, day())
        personal_db.update_task(db, owner, task_id, status="doing")

        kinds = [e["kind"] for e in personal_db.list_task_events(db, task_id)]
        # Newest first.
        assert kinds == ["status", "unpicked", "picked", "created"]

    def test_a_no_op_update_does_not_log(self, db, owner):
        task_id = personal_db.create_task(db, owner, "Find a vet for Ghost", track="personal")
        personal_db.update_task(db, owner, task_id, text="Find a vet for Ghost (urgent)")
        kinds = [e["kind"] for e in personal_db.list_task_events(db, task_id)]
        assert kinds == ["created"]


class TestWeekShape:
    def test_seven_days_by_default(self, db, owner):
        week = routine.week_shape(db, owner, date.today())
        assert len(week) == 7
        assert week[0]["date"] == day()

    def test_an_anchor_shows_up_on_its_day(self, db, owner):
        personal_db.add_rhythm(db, owner, "Leave for the shop", "anchor", category="work",
                               at_time="16:45", hard=True)
        week = routine.week_shape(db, owner, date.today())
        assert week[0]["anchor_count"] == 1
        assert week[0]["hard_count"] == 1


class TestTomorrowBrief:
    def test_cannot_slip_surfaces_hard_anchors(self, db, owner):
        personal_db.add_rhythm(db, owner, "Dyno session at Frank's", "anchor", category="work",
                               at_time="10:00", hard=True)
        brief = routine.tomorrow_brief(db, owner, date.today())
        assert brief["date"] == day(1)
        assert [a["name"] for a in brief["cannot_slip"]] == ["Dyno session at Frank's"]

    def test_an_undone_pick_carries_over(self, db, owner):
        task_id = personal_db.create_task(db, owner, "Order 3mm acrylic", track="personal")
        personal_db.pick_for_day(db, owner, task_id, day())
        brief = routine.tomorrow_brief(db, owner, date.today())
        assert [t["text"] for t in brief["carries_over"]] == ["Order 3mm acrylic"]

    def test_a_done_pick_does_not_carry_over(self, db, owner):
        task_id = personal_db.create_task(db, owner, "Order 3mm acrylic", track="personal")
        personal_db.pick_for_day(db, owner, task_id, day())
        personal_db.update_task(db, owner, task_id, status="done")
        brief = routine.tomorrow_brief(db, owner, date.today())
        assert brief["carries_over"] == []

    def test_prep_tonight_reads_the_labelled_detail(self, db, owner):
        task_id = personal_db.create_task(db, owner, "Dyno session at Frank's", track="personal")
        personal_db.pick_for_day(db, owner, task_id, day(1))
        personal_db.add_task_detail(db, task_id, "note", "Load the press blanks",
                                    label="Prep tonight")
        brief = routine.tomorrow_brief(db, owner, date.today())
        assert {"task_id": task_id, "text": "Load the press blanks"} in brief["prep_tonight"]
