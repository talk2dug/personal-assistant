"""Verifies the autonomous junk-scan job wiring: run_mail_junk_scan calls into the mail
client's own scan_inbox_for_junk tool -- the scoring/moving logic itself belongs to
mail_client.py and junk_filter.py (see their own tests); this only checks the wiring,
the same way test_scheduler_era_cache.py only checks refresh_era_cache's wiring.
"""
from assistant.core.scheduler import run_mail_junk_scan


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
