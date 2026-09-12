"""Verifies MailClient's write-capable tools — mark-read, archive, delete-to-trash and
list-folders — with imaplib mocked out — no real iCloud account needed for these.

The property that matters most for archive/delete: neither ever assumes a literal
folder name. Both resolve the real folder through IMAP SPECIAL-USE flags first, and
only fall back to a matching literal name if the server doesn't advertise one, because
iCloud's own trash folder is commonly named "Deleted Messages", not "Trash".
"""
import imaplib

import pytest

from assistant.core.mail_client import MailClient


class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL. Records every call so tests can assert on the
    exact IMAP verbs/arguments used, and lets a test control what LIST/uid COPY report.
"""

    def __init__(self, list_response=None, uid_responses=None):
        self.calls = []
        self._list_response = list_response if list_response is not None else []
        # uid_responses maps a command name ("store", "copy", ...) to the (typ, data)
        # tuple that uid() should return for it; anything not listed defaults to a plain OK.
        self._uid_responses = uid_responses or {}
        self.logged_out = False
        self.selected = None
        self.expunged = False

    def login(self, user, password):
        self.calls.append(("login", user, password))
        return "OK", [b"success"]

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        self.selected = folder
        return "OK", [b"1"]

    def list(self):
        self.calls.append(("list",))
        return "OK", self._list_response

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command in self._uid_responses:
            return self._uid_responses[command]
        return "OK", [b"1"]

    def expunge(self):
        self.calls.append(("expunge",))
        self.expunged = True
        return "OK", [b"1"]

    def logout(self):
        self.calls.append(("logout",))
        self.logged_out = True


SPECIAL_USE_LIST = [
    b'(\\HasNoChildren) "/" INBOX',
    b'(\\Archive \\HasNoChildren) "/" Archive',
    b'(\\Trash \\HasNoChildren) "/" "Deleted Messages"',
    b'(\\Sent \\HasNoChildren) "/" "Sent Messages"',
]

NO_SPECIAL_USE_LIST = [
    b'(\\HasNoChildren) "/" INBOX',
    b'(\\HasNoChildren) "/" Archive',
    b'(\\HasNoChildren) "/" Trash',
]


@pytest.fixture
def client():
    return MailClient("user@icloud.com", "app-password")


def _patch_imap(monkeypatch, fake: FakeIMAP):
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host: fake)


def test_mark_read_stores_seen_flag_on_the_selected_folder(client, monkeypatch):
    fake = FakeIMAP()
    _patch_imap(monkeypatch, fake)

    result = client.mark_read("42", folder="INBOX")

    assert result == {"ok": True, "uid": "42", "folder": "INBOX"}
    assert ("select", "INBOX", False) in fake.calls
    assert ("uid", "store", "42", "+FLAGS", "(\\Seen)") in fake.calls
    assert fake.logged_out


def test_mark_read_missing_uid_is_an_error_not_a_crash(client, monkeypatch):
    fake = FakeIMAP(uid_responses={"store": ("OK", [None])})
    _patch_imap(monkeypatch, fake)

    result = client.mark_read("999")

    assert "error" in result


def test_archive_resolves_the_real_archive_folder_via_special_use(client, monkeypatch):
    fake = FakeIMAP(list_response=SPECIAL_USE_LIST)
    _patch_imap(monkeypatch, fake)

    result = client.archive_message("7", folder="INBOX")

    assert result == {"ok": True, "uid": "7", "from_folder": "INBOX", "to_folder": "Archive"}
    assert ("uid", "copy", "7", "Archive") in fake.calls
    assert ("uid", "store", "7", "+FLAGS", "(\\Deleted)") in fake.calls
    assert fake.expunged


def test_archive_falls_back_to_literal_name_without_special_use(client, monkeypatch):
    fake = FakeIMAP(list_response=NO_SPECIAL_USE_LIST)
    _patch_imap(monkeypatch, fake)

    result = client.archive_message("7")

    assert result["to_folder"] == "Archive"


def test_archive_with_no_archive_folder_anywhere_is_an_error(client, monkeypatch):
    fake = FakeIMAP(list_response=[b'(\\HasNoChildren) "/" INBOX'])
    _patch_imap(monkeypatch, fake)

    result = client.archive_message("7")

    assert "error" in result
    assert "copy" not in [c[1] for c in fake.calls if c[0] == "uid"]


def test_delete_moves_to_the_real_trash_folder_not_deleted_messages_literal(client, monkeypatch):
    """iCloud's trash folder is commonly named "Deleted Messages" — this must be found
    via the \\Trash SPECIAL-USE flag, not assumed to be called "Trash"."""
    fake = FakeIMAP(list_response=SPECIAL_USE_LIST)
    _patch_imap(monkeypatch, fake)

    result = client.delete_message("9", folder="INBOX")

    assert result == {"ok": True, "uid": "9", "from_folder": "INBOX", "to_folder": "Deleted Messages"}
    assert ("uid", "copy", "9", "Deleted Messages") in fake.calls
    assert fake.expunged


def test_delete_never_expunges_without_copying_first(client, monkeypatch):
    """The safety property the task requires: delete is a move, never a bare wipe. If the
    copy into Trash fails, nothing should be flagged \\Deleted or expunged."""
    fake = FakeIMAP(list_response=SPECIAL_USE_LIST, uid_responses={"copy": ("NO", [b"failed"])})
    _patch_imap(monkeypatch, fake)

    result = client.delete_message("9")

    assert "error" in result
    assert not fake.expunged
    assert ("uid", "store", "9", "+FLAGS", "(\\Deleted)") not in fake.calls


def test_delete_falls_back_to_literal_trash_without_special_use(client, monkeypatch):
    fake = FakeIMAP(list_response=NO_SPECIAL_USE_LIST)
    _patch_imap(monkeypatch, fake)

    result = client.delete_message("9")

    assert result["to_folder"] == "Trash"


def test_list_folders_returns_names_and_special_use_flags(client, monkeypatch):
    fake = FakeIMAP(list_response=SPECIAL_USE_LIST)
    _patch_imap(monkeypatch, fake)

    result = client.list_folders()

    assert {"name": "INBOX", "flags": ["\\HasNoChildren"]} in result["folders"]
    assert {"name": "Archive", "flags": ["\\Archive", "\\HasNoChildren"]} in result["folders"]
    assert {"name": "Deleted Messages", "flags": ["\\Trash", "\\HasNoChildren"]} in result["folders"]


def test_call_tool_dispatches_the_new_tools_by_name(client, monkeypatch):
    fake = FakeIMAP(list_response=SPECIAL_USE_LIST)
    _patch_imap(monkeypatch, fake)

    assert client.call_tool("mark_email_read", {"uid": "1"})["ok"] is True
    assert client.call_tool("archive_email", {"uid": "2"})["to_folder"] == "Archive"
    assert client.call_tool("delete_email", {"uid": "3"})["to_folder"] == "Deleted Messages"
    assert client.call_tool("list_mail_folders", {})["folders"]
    assert "error" in client.call_tool("bogus_tool", {})
