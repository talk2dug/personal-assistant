"""Thin client for Apple iCloud Calendar via CalDAV (auth: Apple ID + app-specific password).

iCloud's CalDAV server doesn't reliably support UID-based REPORT queries — confirmed by
direct testing, `event_by_uid` throws a 412 Precondition Failed against it. So lookups by
UID use a date-range search narrowed around a known/expected time, then filter client-side,
rather than searching by UID directly.
"""
from datetime import datetime, timedelta

import caldav


class CalDAVClient:
    def __init__(self, url: str, username: str, password: str):
        self._client = caldav.DAVClient(url=url, username=username, password=password)

    def _calendar(self, calendar_url: str) -> caldav.Calendar:
        return caldav.Calendar(client=self._client, url=calendar_url)

    def create_event(self, calendar_url: str, summary: str, start: datetime, duration_minutes: int = 30) -> str:
        """Creates an event, returns the CalDAV-assigned UID (the server/library
        generates its own UID regardless of what's requested, confirmed by testing)."""
        cal = self._calendar(calendar_url)
        end = start + timedelta(minutes=duration_minutes)
        event = cal.add_event(dtstart=start, dtend=end, summary=summary)
        return event.id

    def delete_event(self, calendar_url: str, uid: str, near: datetime, window_hours: int = 2) -> bool:
        """Deletes the event with this UID. `near` (our own due_at for it) narrows the
        search, since iCloud can't reliably search by UID directly. Returns False if not found."""
        cal = self._calendar(calendar_url)
        window = timedelta(hours=window_hours)
        matches = [r for r in cal.search(start=near - window, end=near + window, event=True, expand=False) if r.id == uid]
        if not matches:
            return False
        matches[0].delete()
        return True

    def list_events(self, calendar_url: str, start: datetime, end: datetime) -> list[dict]:
        """Returns [{"uid", "summary", "start"}, ...] for events in the given range."""
        cal = self._calendar(calendar_url)
        events = []
        for r in cal.search(start=start, end=end, event=True, expand=False):
            comp = r.icalendar_component
            dtstart = comp.get("dtstart")
            events.append({
                "uid": r.id,
                "summary": str(comp.get("summary", "")),
                "start": dtstart.dt if dtstart else None,
            })
        return events
