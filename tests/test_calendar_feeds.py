"""A subscribed work calendar.

The failures that matter here are all about a morning: a recurring standup that only shows
once, a meeting in another timezone landing on the wrong day, and — the worst one — a flaky
corporate endpoint at 6am wiping the day it failed to fetch.
"""
from datetime import date, timedelta

import pytest

from assistant.core import calendar_feeds, db as core_db


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "feeds.db")
    core_db.init_db(path)
    core_db.upsert_user(path, "111", "Dug", "owner")
    calendar_feeds.init_calendar_feeds(path)
    return path


@pytest.fixture
def owner(db):
    return core_db.get_user_by_chat_id(db, "111")["id"]


def ics(*events: str) -> str:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
            + "".join(events) + "END:VCALENDAR\r\n")


def vevent(uid, start, end=None, summary="Meeting", extra=""):
    body = f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:{start}\r\nSUMMARY:{summary}\r\n"
    if end:
        body += f"DTEND:{end}\r\n"
    return body + extra + "END:VEVENT\r\n"


def stamp(d: date, hhmm="140000"):
    return f"{d.strftime('%Y%m%d')}T{hhmm}Z"


class TestParsing:
    def test_a_recurring_meeting_appears_on_every_occurrence(self):
        """His standup exists once in the file and weekly on his calendar. Reading DTSTART
        alone would show it on one Tuesday and silently lose the rest."""
        start = date.today() - timedelta(days=1)
        text = ics(vevent("standup", stamp(start, "120000"), summary="Standup",
                          extra="RRULE:FREQ=WEEKLY;COUNT=4\r\n"))
        events = calendar_feeds.parse_events(
            text, "America/New_York", start - timedelta(days=1), start + timedelta(days=30))
        assert len(events) == 4
        assert {e["summary"] for e in events} == {"Standup"}

    def test_a_meeting_is_placed_on_the_day_it_falls_on_for_him(self):
        """A 12:30 Central call is a 13:30 Eastern call. Storing the source timezone's
        clock time would put a late-evening meeting on the wrong day entirely."""
        day = date.today()
        text = ics(vevent("tz", f"{day.strftime('%Y%m%d')}T233000Z"))
        events = calendar_feeds.parse_events(
            text, "America/New_York", day - timedelta(days=1), day + timedelta(days=1))
        # 23:30 UTC is 19:30 the same day in New York.
        assert events[0]["local_time"] == "19:30"
        assert events[0]["local_date"] == day.isoformat()

    def test_an_all_day_event_keeps_its_date(self):
        """An all-day DTSTART is a bare date with no tzinfo. Treating it as midnight UTC
        would drag it back a day for anyone west of London."""
        day = date.today() + timedelta(days=2)
        text = ics(vevent("allday", f"VALUE=DATE:{day.strftime('%Y%m%d')}".split(":")[-1]))
        text = text.replace(f"DTSTART:{day.strftime('%Y%m%d')}",
                            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}")
        events = calendar_feeds.parse_events(
            text, "America/New_York", day - timedelta(days=1), day + timedelta(days=1))
        assert events[0]["local_date"] == day.isoformat()
        assert events[0]["all_day"] == 1 and events[0]["local_time"] is None

    def test_a_cancelled_meeting_is_not_on_his_day(self):
        day = date.today()
        text = ics(vevent("off", stamp(day), extra="STATUS:CANCELLED\r\n"))
        assert calendar_feeds.parse_events(
            text, "America/New_York", day - timedelta(days=1), day + timedelta(days=1)) == []


class TestLocation:
    @pytest.mark.parametrize("raw,expected", [
        ("https://tigerdata.zoom.us/j/95969375404?pwd=KbCFF4r0", "Zoom"),
        ("Microsoft Teams Meeting", "Teams"),
        ("https://teams.microsoft.com/l/meetup-join/19%3ameeting", "Teams"),
        ("2805 E Parham Road, Richmond, VA", "2805 E Parham Road, Richmond, VA"),
        ("", ""),
    ])
    def test_a_join_link_collapses_but_a_real_address_survives(self, raw, expected):
        """A 120-character join URL pushes the meeting title off a phone row to tell him
        nothing he can use -- he joins from the invite, not from this screen."""
        assert calendar_feeds.tidy_location(raw) == expected


class TestSync:
    def test_events_land_and_can_be_read_back(self, db, owner):
        day = date.today() + timedelta(days=1)
        feed = calendar_feeds.add_feed(db, owner, "Work", "https://example.test/cal.ics")
        result = calendar_feeds.sync_feed(
            db, feed, "America/New_York",
            fetch=lambda url, timeout=60: ics(vevent("a", stamp(day), summary="Standup")))
        assert result["ok"] and result["events"] == 1
        found = calendar_feeds.events_between(db, owner, day, day)
        assert [e["summary"] for e in found] == ["Standup"]
        assert found[0]["feed_name"] == "Work"

    def test_a_cancelled_meeting_disappears_on_the_next_sync(self, db, owner):
        """The window is replaced on success, so an upstream cancellation is reflected."""
        day = date.today() + timedelta(days=1)
        feed = calendar_feeds.add_feed(db, owner, "Work", "https://example.test/cal.ics")
        calendar_feeds.sync_feed(db, feed, "America/New_York",
                                 fetch=lambda url, timeout=60: ics(vevent("a", stamp(day))))
        calendar_feeds.sync_feed(db, feed, "America/New_York",
                                 fetch=lambda url, timeout=60: ics())
        assert calendar_feeds.events_between(db, owner, day, day) == []

    def test_a_failed_fetch_keeps_the_last_good_copy(self, db, owner):
        """The one that matters. A corporate endpoint refusing at 6am must not be the
        reason he walks into the office with an empty calendar."""
        day = date.today() + timedelta(days=1)
        feed = calendar_feeds.add_feed(db, owner, "Work", "https://example.test/cal.ics")
        calendar_feeds.sync_feed(db, feed, "America/New_York",
                                 fetch=lambda url, timeout=60: ics(vevent("a", stamp(day))))

        def boom(url, timeout=60):
            raise OSError("connection reset")

        result = calendar_feeds.sync_feed(db, feed, "America/New_York", fetch=boom)
        assert result["ok"] is False
        assert len(calendar_feeds.events_between(db, owner, day, day)) == 1, \
            "his meetings survived a failed refresh"

    def test_a_sign_in_page_is_a_failure_not_an_empty_calendar(self, db, owner):
        """A revoked share link answers 200 with HTML. Parsing that as zero events would
        quietly empty his week and report success."""
        feed = calendar_feeds.add_feed(db, owner, "Work", "https://example.test/cal.ics")
        result = calendar_feeds.sync_feed(
            db, feed, "America/New_York",
            fetch=lambda url, timeout=60: "<html><body>Sign in</body></html>")
        assert result["ok"] is False and "iCalendar" in result["error"]

    def test_subscribing_twice_to_one_url_does_not_double_it(self, db, owner):
        url = "https://example.test/cal.ics"
        first = calendar_feeds.add_feed(db, owner, "Work", url)
        again = calendar_feeds.add_feed(db, owner, "Work calendar", url)
        assert first == again
        assert len(calendar_feeds.list_feeds(db, owner)) == 1


class TestTheUrlIsACredential:
    def test_it_is_never_handed_back_out(self, db, owner):
        """Anyone holding this link can read his whole work calendar. Nothing in the UI
        needs it, and a private feed link in a JSON response is one in a browser cache."""
        url = ("https://outlook.office365.com/owa/calendar/8f4b6e8874ab4addb6bfa8cb1a7562b7"
               "@example.com/398d9b4a729d41e5bf5ee481746502a018116667427730748243/calendar.ics")
        calendar_feeds.add_feed(db, owner, "Work", url)
        listed = calendar_feeds.list_feeds(db, owner)[0]
        assert "398d9b4a729d41e5bf" not in listed["url"]
        assert "8f4b6e8874ab4addb6bfa8cb1a7562b7" not in listed["url"]

    def test_an_error_message_cannot_leak_it(self, db, owner):
        """requests and httpx both put the full URL in their exception text."""
        url = "https://outlook.office365.com/owa/calendar/deadbeefdeadbeefdeadbeef/calendar.ics"
        feed = calendar_feeds.add_feed(db, owner, "Work", url)

        def boom(u, timeout=60):
            raise OSError(f"failed to fetch {u}")

        result = calendar_feeds.sync_feed(db, feed, "America/New_York", fetch=boom)
        assert "deadbeefdeadbeef" not in result["error"]
