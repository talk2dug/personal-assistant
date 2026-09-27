"""One calendar over everything with a date on it.

Six sources feed this, and the failures worth defending against are all about trust: a
duplicate reads as two payments, a silently dropped source reads as a lighter week than he
has, and a missed bill that scrolls out of view stays missed.
"""
from datetime import date, timedelta

import pytest

from assistant.core import agenda, db as core_db, personal_db


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "agenda.db")
    core_db.init_db(path)
    personal_db.init_personal_db(path)
    core_db.upsert_user(path, "111", "Dug", "owner")
    return path


@pytest.fixture
def owner(db):
    return core_db.get_user_by_chat_id(db, "111")["id"]


def day(n):
    return (date.today() + timedelta(days=n)).isoformat()


class TestDateParsing:
    @pytest.mark.parametrize("value,expected", [
        ("2026-09-15", date(2026, 9, 15)),
        ("2026-09-15T14:30:00+00:00", date(2026, 9, 15)),
        ("2026-09-15T14:30:00Z", date(2026, 9, 15)),
    ])
    def test_every_shape_the_tables_store(self, value, expected):
        """Each source stores its date differently -- a bare date, a timestamp, a Z suffix
        -- and one unparsed format means a whole source silently contributes nothing."""
        assert agenda._as_date(value) == expected

    @pytest.mark.parametrize("value", [None, "", "not a date", "2026-13-45"])
    def test_nonsense_is_none_rather_than_an_exception(self, value):
        assert agenda._as_date(value) is None


class TestGathering:
    def test_a_dated_task_appears(self, db, owner):
        personal_db.create_task(db, owner, "Pay the Capital One cards", due_at=day(3),
                                priority="high", track="personal")
        data = agenda.upcoming(db, owner, days=30)
        entry = next(e for d in data["days"] for e in d["entries"] if e["kind"] == "task")
        assert entry["title"].startswith("Pay the Capital One")
        assert entry["urgent"] is True

    def test_an_undated_task_is_not_invented_onto_a_day(self, db, owner):
        """67 of his tasks have no due date. Putting them on "today" would bury the ones
        that genuinely are due."""
        personal_db.create_task(db, owner, "someday maybe", track="personal")
        assert agenda.upcoming(db, owner, days=30)["counts"]["task"] == 0

    def test_a_finished_task_drops_off(self, db, owner):
        tid = personal_db.create_task(db, owner, "done thing", due_at=day(2), track="personal")
        personal_db.update_task(db, owner, tid, status="done")
        assert agenda.upcoming(db, owner, days=30)["counts"]["task"] == 0

    def test_entries_are_ordered_by_day_then_by_what_matters(self, db, owner):
        """Money leaving the account outranks a reminder to buy milk on the same day."""
        ranks = [agenda._KIND_RANK[k] for k in ("bill", "income", "deadline", "task", "reminder")]
        assert ranks == sorted(ranks)


class TestItNeverQuietlyShrinks:
    def test_one_broken_source_does_not_blank_the_calendar(self, db, owner, monkeypatch):
        """The failure that matters: a CalDAV server on a bad connection taking down a view
        that would otherwise have shown rent and a payoff deadline."""
        personal_db.create_task(db, owner, "still here", due_at=day(1), track="personal")

        def explode(*a, **k):
            raise RuntimeError("calendar server unreachable")
        monkeypatch.setattr(agenda, "_reminders", explode)

        data = agenda.upcoming(db, owner, days=30)
        assert data["counts"]["task"] == 1, "the working sources still render"
        assert [u["source"] for u in data["unavailable"]] == ["reminders"]

    def test_a_missing_source_is_named_rather_than_hidden(self, db, owner, monkeypatch):
        monkeypatch.setattr(agenda, "_recurring", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        data = agenda.upcoming(db, owner, days=30)
        assert data["unavailable"] and "recurring" in data["unavailable"][0]["source"]

    def test_what_was_just_missed_is_still_shown(self, db, owner):
        """A calendar that hides what was missed is a calendar that lets it stay missed."""
        personal_db.create_task(db, owner, "overdue thing", due_at=day(-2), track="personal")
        data = agenda.upcoming(db, owner, days=30, back_days=7)
        assert [e["title"] for e in data["overdue"]] == ["overdue thing"]

    def test_the_window_is_bounded_at_both_ends(self, db, owner):
        personal_db.create_task(db, owner, "ancient", due_at=day(-90), track="personal")
        personal_db.create_task(db, owner, "far future", due_at=day(400), track="personal")
        assert agenda.upcoming(db, owner, days=30, back_days=7)["counts"]["task"] == 0


class TestNoDoubleCounting:
    def test_a_bill_found_in_email_does_not_also_appear_as_its_reminder(self, db, owner):
        """Jarvis files a reminder for every bill it finds in his inbox, so the same
        Shopify invoice arrived on the calendar twice -- once carrying the amount and once
        not. A duplicate here is worse than useless: it reads as two payments."""
        import sqlite3

        rid = core_db.add_reminder(db, owner, "Bill due: Shopify - $39.00", day(3), "private")
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE IF NOT EXISTS email_bills
                        (id INTEGER PRIMARY KEY, owner_user_id INTEGER, payee TEXT,
                         amount REAL, due_date TEXT, status TEXT, reminder_id INTEGER)""")
        conn.execute("INSERT INTO email_bills (owner_user_id, payee, amount, due_date, status,"
                     " reminder_id) VALUES (?,?,?,?,?,?)",
                     (owner, "Shopify", 39.0, day(3), "confirmed", rid))
        conn.commit(); conn.close()

        data = agenda.upcoming(db, owner, days=30)
        titles = [e["title"] for d in data["days"] for e in d["entries"]]
        assert sum(1 for t in titles if "Shopify" in t) == 1
        assert data["counts"]["bill"] == 1 and data["counts"]["reminder"] == 0

    def test_an_unrelated_reminder_is_untouched(self, db, owner):
        core_db.add_reminder(db, owner, "Doctor at 1pm", day(4), "private")
        assert agenda.upcoming(db, owner, days=30)["counts"]["reminder"] == 1


def test_an_empty_calendar_is_not_an_error(db, owner):
    data = agenda.upcoming(db, owner, days=30)
    assert data["days"] == [] and data["unavailable"] == []
    assert data["money_in"] == 0 and data["money_out"] == 0


class TestTheCalendarGridsQueries:
    """What the month/week/day views and the Schedule panel actually ask for."""

    def test_an_absolute_range_overrides_the_relative_window(self, db, owner):
        """Browsing to next March is a fixed range, not "n days from now"."""
        personal_db.create_task(db, owner, "far off", due_at="2027-03-15", track="personal")
        data = agenda.upcoming(db, owner, start=date(2027, 3, 1), end=date(2027, 3, 31))
        assert data["counts"]["task"] == 1
        assert data["from"] == "2027-03-01" and data["to"] == "2027-03-31"

    def test_a_backwards_range_is_corrected_rather_than_returning_nothing(self, db, owner):
        personal_db.create_task(db, owner, "thing", due_at=day(2), track="personal")
        data = agenda.upcoming(db, owner, start=date.today() + timedelta(days=5),
                               end=date.today())
        assert data["counts"]["task"] == 1

    def test_reminders_can_be_left_out(self, db, owner):
        """Jack: "I dont need to see reminders in my schedule, those are to just remind
        me." A nudge is not a commitment with a place in the day."""
        core_db.add_reminder(db, owner, "buy milk", day(1), "private")
        personal_db.create_task(db, owner, "real task", due_at=day(1), track="personal")

        with_them = agenda.upcoming(db, owner, days=5)
        assert with_them["counts"]["reminder"] == 1

        without = agenda.upcoming(db, owner, days=5, exclude_kinds=("reminder",))
        assert without["counts"]["reminder"] == 0
        assert without["counts"]["task"] == 1, "excluding one kind keeps the rest"
        titles = [e["title"] for d in without["days"] for e in d["entries"]]
        assert "buy milk" not in titles

    def test_excluding_a_kind_also_drops_it_from_overdue(self, db, owner):
        """Overdue is assembled separately, so it needs the filter applied too -- a
        reminder he asked not to see must not reappear pinned to the top."""
        core_db.add_reminder(db, owner, "old nudge", day(-2), "private")
        data = agenda.upcoming(db, owner, days=5, back_days=7, exclude_kinds=("reminder",))
        assert [e["title"] for e in data["overdue"]] == []

    def test_an_excluded_source_cannot_report_itself_unavailable(self, db, owner, monkeypatch):
        """If he is not looking at reminders, a broken reminders table is not a warning
        worth showing him."""
        monkeypatch.setattr(agenda, "_reminders",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
        data = agenda.upcoming(db, owner, days=5, exclude_kinds=("reminder",))
        assert data["unavailable"] == []

    def test_a_recurring_bill_survives_excluding_reminders(self, db, owner):
        """'recurring' emits both bill and income, so it cannot be skipped wholesale by
        source -- the filter has to run on the entries as well."""
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE IF NOT EXISTS manual_recurring_charges
                        (id INTEGER PRIMARY KEY, owner_user_id INTEGER, description TEXT,
                         amount REAL, direction TEXT, cadence TEXT,
                         next_expected_date TEXT, created_at TEXT)""")
        conn.execute("""INSERT INTO manual_recurring_charges
                        (owner_user_id, description, amount, direction, cadence,
                         next_expected_date, created_at)
                        VALUES (?,?,?,?,?,?,?)""",
                     (owner, "Rent", 925.0, "expense", "monthly_on_day", day(6), "2026-01-01"))
        conn.commit(); conn.close()
        data = agenda.upcoming(db, owner, days=45, exclude_kinds=("reminder",))
        assert data["counts"]["bill"] >= 1
