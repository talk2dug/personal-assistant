"""Unit tests for MailClient's junk-flagging: heuristic annotation on
list_recent results, and the flag_junk tool that tags a message on the
server. All IMAP is faked -- these must never open a real connection, same
convention as test_letterstream_client.py / test_home_assistant_client.py.
"""
from email.message import EmailMessage

from assistant.core.mail_client import MailClient


def _raw_message(from_addr: str, subject: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg["Date"] = "Mon, 1 Jan 2024 00:00:00 -0000"
    msg.set_content("body")
    return msg.as_bytes()


def _make_fake_imap(raw_bytes: bytes):
    class FakeIMAP:
        def __init__(self, host):
            self.host = host
            self.stored = []

        def login(self, user, password):
            return ("OK", [b"logged in"])

        def select(self, folder, readonly=False):
            return ("OK", [b"1"])

        def uid(self, command, *args):
            if command == "search":
                return ("OK", [b"1"])
            if command == "fetch":
                return ("OK", [(b"1 (FLAGS (\\Seen))", raw_bytes)])
            if command == "store":
                self.stored.append(args)
                return ("OK", [b"done"])
            raise AssertionError(f"unexpected uid command: {command}")

        def logout(self):
            pass

    return FakeIMAP


def test_list_recent_flags_obvious_junk(monkeypatch):
    raw = _raw_message("deals@offers.xyz", "WINNER!!! Click here now!!!")
    monkeypatch.setattr("assistant.core.mail_client.imaplib.IMAP4_SSL", _make_fake_imap(raw))

    client = MailClient("me@icloud.com", "app-password")
    result = client.list_recent()

    assert result["emails"][0]["junk"] is True


def test_list_recent_does_not_flag_an_ordinary_message(monkeypatch):
    raw = _raw_message("mom@family.com", "Dinner Sunday?")
    monkeypatch.setattr("assistant.core.mail_client.imaplib.IMAP4_SSL", _make_fake_imap(raw))

    client = MailClient("me@icloud.com", "app-password")
    result = client.list_recent()

    assert result["emails"][0]["junk"] is False


def test_flag_junk_stores_the_junk_keyword(monkeypatch):
    raw = _raw_message("deals@offers.xyz", "WINNER!!! Click here now!!!")
    monkeypatch.setattr("assistant.core.mail_client.imaplib.IMAP4_SSL", _make_fake_imap(raw))

    client = MailClient("me@icloud.com", "app-password")
    result = client.flag_junk("1")

    assert result == {"ok": True, "uid": "1", "flagged": "Junk"}


def test_call_tool_dispatches_flag_junk(monkeypatch):
    raw = _raw_message("deals@offers.xyz", "WINNER!!! Click here now!!!")
    monkeypatch.setattr("assistant.core.mail_client.imaplib.IMAP4_SSL", _make_fake_imap(raw))

    client = MailClient("me@icloud.com", "app-password")
    result = client.call_tool("flag_junk", {"uid": "1"})

    assert result["ok"] is True


def test_call_tool_unknown_name_returns_error():
    client = MailClient("me@icloud.com", "app-password")
    assert "error" in client.call_tool("bogus_tool", {})
