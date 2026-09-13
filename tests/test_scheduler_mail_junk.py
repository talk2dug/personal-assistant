"""Verifies the autonomous junk-scan job wiring: run_mail_junk_scan calls into the mail
client's own scan_inbox_for_junk tool -- the scoring/moving logic itself belongs to
mail_client.py and junk_filter.py (see their own tests); this only checks the wiring,
the same way test_scheduler_era_cache.py only checks refresh_era_cache's wiring.

Also covers record_junk_scan_results (Phase 3: the audit trail for that already-
autonomous pass) -- persisting mail_junk_log rows is its own concern from the scan
itself, so it gets its own tests here rather than being folded into mail_client's.
"""
import pytest

from assistant.core import mail_db
from assistant.core.scheduler import record_junk_scan_results, run_mail_junk_scan


class FakeMailClient:
    def __init__(self, response):
        self.calls = []
        self._response = response

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self._response


def test_run_mail_junk_scan_calls_scan_tool_with_limit_and_unread_only():
    client = FakeMailClient({"scanned": 10, "flagged": 2, "moved": 2, "results": []})

    result = run_mail_junk_scan(client, limit=15)

    assert client.calls == [("scan_inbox_for_junk", {"limit": 15, "only_unread": True})]
    assert result == {"scanned": 10, "flagged": 2, "moved": 2, "results": []}


def test_run_mail_junk_scan_default_limit():
    client = FakeMailClient({"scanned": 0, "flagged": 0, "moved": 0, "results": []})

    run_mail_junk_scan(client)

    assert client.calls == [("scan_inbox_for_junk", {"limit": 25, "only_unread": True})]


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    mail_db.init_mail_db(path)
    return path


def test_record_junk_scan_results_only_logs_flagged_messages(db_path):
    results = [
        {"uid": "1", "from": "a@x.com", "subject": "hi", "score": 1.0, "reasons": [], "flagged": False},
        {"uid": "2", "from": "spam@x.com", "subject": "buy now", "score": 8.0, "reasons": ["urgent"],
         "flagged": True, "moved": True},
    ]

    logged = record_junk_scan_results(db_path, "INBOX", results)

    assert logged == 1
    entries = mail_db.list_junk_log(db_path)
    assert len(entries) == 1
    assert entries[0]["uid"] == "2"
    assert entries[0]["subject"] == "buy now"
    assert entries[0]["moved"] is True
    assert entries[0]["moved_to"] == "Junk"
    assert entries[0]["reasons"] == ["urgent"]


def test_record_junk_scan_results_records_failed_moves_without_a_destination(db_path):
    """A message can be flagged as junk but still fail to actually move (a transient IMAP
    error) -- that's still worth a visible row, just without a moved_to folder."""
    results = [
        {"uid": "3", "from": "spam@x.com", "subject": "buy now", "score": 9.0, "reasons": ["urgent"],
         "flagged": True, "moved": False},
    ]

    record_junk_scan_results(db_path, "INBOX", results)

    entries = mail_db.list_junk_log(db_path)
    assert entries[0]["moved"] is False
    assert entries[0]["moved_to"] is None


def test_record_junk_scan_results_handles_dry_run_missing_moved_key(db_path):
    """dry_run scans never add a "moved" key at all -- must not be treated as a truthy move."""
    results = [
        {"uid": "4", "from": "spam@x.com", "subject": "buy now", "score": 9.0, "reasons": [], "flagged": True},
    ]

    record_junk_scan_results(db_path, "INBOX", results)

    entries = mail_db.list_junk_log(db_path)
    assert entries[0]["moved"] is False


def test_record_junk_scan_results_returns_zero_for_no_flagged_messages(db_path):
    assert record_junk_scan_results(db_path, "INBOX", []) == 0
