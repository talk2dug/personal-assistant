"""Exercises the junk-scan/flag IMAP plumbing against a fake IMAP connection -- a real
mailbox isn't available in CI, so this checks the commands mail_client issues (search,
fetch, copy, store, expunge) and that scoring/threshold decide what gets moved, not a
live iCloud account.
"""
from email.message import EmailMessage
from unittest.mock import patch

from assistant.core.mail_client import MailClient


def _raw_message(from_: str, subject: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_
    msg["To"] = "owner@example.com"
    msg["Subject"] = subject
    msg.set_content(body)
    return msg.as_bytes()


def _key(uid) -> bytes:
    return uid.encode() if isinstance(uid, str) else uid


class FakeIMAPConnection:
    """Just enough of imaplib.IMAP4_SSL's surface for scan_inbox_for_junk/flag_as_junk
    to run against, backed by an in-memory dict of uid -> raw RFC822 bytes."""

    def __init__(self, messages: dict[bytes, bytes]):
        self.messages = messages
        self.deleted: set[bytes] = set()
        self.copied_to: dict[bytes, str] = {}
        self.expunged = False

    def select(self, folder, readonly=False):
        return "OK", [b"1"]

    def uid(self, command, *args):
        command = command.lower()
        if command == "search":
            uids = sorted(self.messages)
            return "OK", [b" ".join(uids)] if uids else [None]
        if command == "fetch":
            uid = _key(args[0])
            raw = self.messages.get(uid)
            if raw is None:
                return "OK", [None]
            return "OK", [(b"1 (BODY[] {%d}" % len(raw), raw)]
        if command == "copy":
            uid, target = args
            self.copied_to[_key(uid)] = target
            return "OK", [b"copied"]
        if command == "store":
            uid = args[0]
            self.deleted.add(_key(uid))
            return "OK", [b"done"]
        raise AssertionError(f"unexpected uid command: {command} {args}")

    def expunge(self):
        self.expunged = True
        return "OK", [b""]

    def logout(self):
        pass


PHISHING = _raw_message(
    '"PayPal Security" <alerts@totally-not-paypal.tk>',
    "URGENT: Verify your account NOW!!!",
    "Click here immediately to verify your account or it will be suspended.",
)
ORDINARY = _raw_message("Jamie <jamie@example.com>", "Order update", "Your order shipped, thanks!")


@patch.object(MailClient, "_imap")
def test_scan_inbox_for_junk_moves_only_high_scoring_messages(mock_imap):
    conn = FakeIMAPConnection({b"101": PHISHING, b"102": ORDINARY})
    mock_imap.return_value = conn

    client = MailClient("me@icloud.com", "app-password")
    result = client.scan_inbox_for_junk(limit=10)

    assert result["scanned"] == 2
    flagged_uids = {r["uid"] for r in result["results"] if r["flagged"]}
    assert flagged_uids == {"101"}
    assert result["flagged"] == 1
    assert result["moved"] == 1
    assert conn.copied_to == {b"101": "Junk"}
    assert conn.deleted == {b"101"}
    assert conn.expunged is True


@patch.object(MailClient, "_imap")
def test_scan_inbox_for_junk_dry_run_scores_without_moving(mock_imap):
    conn = FakeIMAPConnection({b"101": PHISHING})
    mock_imap.return_value = conn

    client = MailClient("me@icloud.com", "app-password")
    result = client.scan_inbox_for_junk(limit=10, dry_run=True)

    assert result["flagged"] == 1
    assert result["moved"] == 0
    assert conn.copied_to == {}
    assert conn.expunged is False


@patch.object(MailClient, "_imap")
def test_scan_inbox_for_junk_respects_custom_threshold(mock_imap):
    conn = FakeIMAPConnection({b"101": ORDINARY})
    mock_imap.return_value = conn

    client = MailClient("me@icloud.com", "app-password")
    result = client.scan_inbox_for_junk(limit=10, threshold=0.0)

    # Even an ordinary message clears a threshold of 0.
    assert result["flagged"] == 1
    assert result["moved"] == 1


@patch.object(MailClient, "_imap")
def test_flag_as_junk_copies_deletes_and_expunges(mock_imap):
    conn = FakeIMAPConnection({b"101": PHISHING})
    mock_imap.return_value = conn

    client = MailClient("me@icloud.com", "app-password")
    outcome = client.flag_as_junk("101")

    assert outcome == {"ok": True, "uid": "101", "moved_to": "Junk"}
    assert conn.copied_to == {b"101": "Junk"}
    assert conn.deleted == {b"101"}
    assert conn.expunged is True


@patch.object(MailClient, "_imap")
def test_call_tool_routes_scan_and_flag(mock_imap):
    conn = FakeIMAPConnection({b"101": PHISHING})
    mock_imap.return_value = conn

    client = MailClient("me@icloud.com", "app-password")
    scan_result = client.call_tool("scan_inbox_for_junk", {"limit": 5})
    assert scan_result["scanned"] == 1

    conn2 = FakeIMAPConnection({b"202": PHISHING})
    mock_imap.return_value = conn2
    flag_result = client.call_tool("flag_email_as_junk", {"uid": "202"})
    assert flag_result["ok"] is True
