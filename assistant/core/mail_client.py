"""Read/write access to the user's iCloud Mail account via IMAP/SMTP, using the same
Apple ID + app-specific password already configured for CalDAV (Phase 3) — Apple
app-specific passwords authenticate the whole Apple ID, not a single protocol, so no
new credential is needed. Exposes call_tool(name, arguments) so engine.py can treat
this exactly like Era/phone's MCP-style tool contexts, even though nothing here
actually goes over MCP.

Write-capable tools (mark-read, archive, delete) never guess a folder name: the account's
iCloud-assigned name for its Archive/Trash folders isn't guaranteed to literally be
"Archive"/"Trash" (iCloud commonly calls the trash folder "Deleted Messages"), so folder
targeting is resolved through IMAP's SPECIAL-USE attributes (\\Archive, \\Trash) via
list_folders/_resolve_folder, with a same-named fallback only if the server doesn't
advertise SPECIAL-USE. "Delete" is always a move into Trash, never IMAP's destructive
EXPUNGE-without-a-copy — nothing here can permanently wipe a message.
"""
import email
import imaplib
import re
import smtplib
from email.header import decode_header
from email.message import EmailMessage

IMAP_HOST = "imap.mail.me.com"
SMTP_HOST = "smtp.mail.me.com"

_LIST_RESPONSE_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$')


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        out.append(text.decode(enc or "utf-8", errors="replace") if isinstance(text, bytes) else text)
    return "".join(out)


def _strip_html(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_body(msg: "email.message.Message", max_chars: int) -> str:
    if msg.is_multipart():
        plain = html = None
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                plain = part.get_payload(decode=True)
            elif ctype == "text/html" and html is None:
                html = part.get_payload(decode=True)
        raw, is_html = (plain, False) if plain is not None else (html, True)
    else:
        raw = msg.get_payload(decode=True)
        is_html = msg.get_content_type() == "text/html"
    if raw is None:
        return ""
    text = raw.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return (_strip_html(text) if is_html else text)[:max_chars]


def _parse_list_entry(raw) -> dict | None:
    """One line of an IMAP LIST response, e.g. b'(\\Archive \\HasNoChildren) "/" Archive',
    into {"name": ..., "flags": [...]}. Returns None for a line the server sent that this
    regex doesn't recognise, rather than raising — a folder listing should degrade to
    fewer entries, not fail outright, on an oddly formatted line."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    match = _LIST_RESPONSE_RE.match(raw)
    if not match:
        return None
    name = match.group("name").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return {"name": name, "flags": match.group("flags").split()}


class MailClient:
    def __init__(self, apple_id: str, app_password: str):
        self.apple_id = apple_id
        self.app_password = app_password

    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(IMAP_HOST)
        conn.login(self.apple_id, self.app_password)
        return conn

    def _list_entries(self, conn: imaplib.IMAP4_SSL) -> list[dict]:
        _, data = conn.list()
        return [e for e in (_parse_list_entry(raw) for raw in (data or [])) if e]

    def _resolve_folder(self, conn: imaplib.IMAP4_SSL, special_use: str, fallback_names: list[str]) -> str | None:
        """The account's real folder name for a role like \\Archive or \\Trash. Prefers
        whatever the server itself flags as that role (SPECIAL-USE, RFC 6154); only falls
        back to a literal name from fallback_names if the server doesn't advertise one,
        since an iCloud account's trash folder is commonly named "Deleted Messages" rather
        than "Trash" and guessing wrong would silently target the wrong mailbox."""
        entries = self._list_entries(conn)
        for entry in entries:
            if special_use in entry["flags"]:
                return entry["name"]
        names = {entry["name"] for entry in entries}
        for candidate in fallback_names:
            if candidate in names:
                return candidate
        return None

    def _headers(self, conn: imaplib.IMAP4_SSL, uid: bytes) -> dict:
        _, data = conn.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] FLAGS)")
        msg = email.message_from_bytes(data[0][1])
        flags_raw = data[0][0].decode(errors="replace")
        return {
            "uid": uid.decode(),
            "from": _decode(msg.get("From")),
            "subject": _decode(msg.get("Subject")),
            "date": msg.get("Date", ""),
            "unread": "\\Seen" not in flags_raw,
        }

    def list_recent(self, folder: str = "INBOX", limit: int = 10) -> dict:
        conn = self._imap()
        try:
            conn.select(folder, readonly=True)
            _, data = conn.uid("search", None, "ALL")
            uids = data[0].split()[-limit:][::-1] if data and data[0] else []
            return {"emails": [self._headers(conn, uid) for uid in uids]}
        finally:
            conn.logout()

    def search(self, query: str, folder: str = "INBOX", limit: int = 10) -> dict:
        conn = self._imap()
        try:
            conn.select(folder, readonly=True)
            # A multi-word query is quoted as one IMAP TEXT phrase, which only matches that
            # exact phrase verbatim — a genuinely empty/failed search (bad syntax, no hits)
            # comes back as data == [None] rather than [b''], which .split() can't handle.
            _, data = conn.uid("search", None, "TEXT", f'"{query}"')
            uids = data[0].split()[-limit:][::-1] if data and data[0] else []
            return {"emails": [self._headers(conn, uid) for uid in uids]}
        finally:
            conn.logout()

    def read_message(self, uid: str, folder: str = "INBOX", max_chars: int = 4000) -> dict:
        conn = self._imap()
        try:
            conn.select(folder, readonly=True)
            _, data = conn.uid("fetch", uid, "(BODY.PEEK[])")
            if not data or data[0] is None:
                return {"error": f"no message with uid {uid}"}
            msg = email.message_from_bytes(data[0][1])
            return {
                "uid": uid,
                "from": _decode(msg.get("From")),
                "to": _decode(msg.get("To")),
                "subject": _decode(msg.get("Subject")),
                "date": msg.get("Date", ""),
                "body": _extract_body(msg, max_chars),
            }
        finally:
            conn.logout()

    def send(self, to: str, subject: str, body: str) -> dict:
        message = EmailMessage()
        message["From"] = self.apple_id
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP(SMTP_HOST, 587) as smtp:
            smtp.starttls()
            smtp.login(self.apple_id, self.app_password)
            smtp.send_message(message)
        return {"ok": True, "to": to, "subject": subject}

    def mark_read(self, uid: str, folder: str = "INBOX") -> dict:
        """Sets \\Seen on a message. The mailbox is selected writable (not readonly=True
        like the read-only tools above) because this call's whole point is the flag write."""
        conn = self._imap()
        try:
            conn.select(folder)
            typ, data = conn.uid("store", uid, "+FLAGS", "(\\Seen)")
            if typ != "OK" or not data or data[0] is None:
                return {"error": f"no message with uid {uid} in {folder}"}
            return {"ok": True, "uid": uid, "folder": folder}
        finally:
            conn.logout()

    def archive_message(self, uid: str, folder: str = "INBOX") -> dict:
        """Copies the message into the account's real Archive folder, then removes the
        original from its source folder. A copy-then-expunge, not IMAP MOVE, so this
        works against servers (iCloud included, historically) that don't support the
        MOVE extension (RFC 6851)."""
        conn = self._imap()
        try:
            archive_folder = self._resolve_folder(conn, "\\Archive", ["Archive"])
            if archive_folder is None:
                return {"error": "could not find an Archive folder on this account — "
                                 "check list_mail_folders for the real name"}
            conn.select(folder)
            typ, _ = conn.uid("copy", uid, archive_folder)
            if typ != "OK":
                return {"error": f"no message with uid {uid} in {folder}"}
            conn.uid("store", uid, "+FLAGS", "(\\Deleted)")
            conn.expunge()
            return {"ok": True, "uid": uid, "from_folder": folder, "to_folder": archive_folder}
        finally:
            conn.logout()

    def delete_message(self, uid: str, folder: str = "INBOX") -> dict:
        """Moves the message to the account's real Trash folder (copy, then expunge the
        original) — never a permanent, unrecoverable wipe. iCloud's own name for this
        folder is commonly "Deleted Messages" rather than "Trash", which is exactly why
        this resolves it via SPECIAL-USE instead of assuming a literal name."""
        conn = self._imap()
        try:
            trash_folder = self._resolve_folder(conn, "\\Trash", ["Deleted Messages", "Trash"])
            if trash_folder is None:
                return {"error": "could not find a Trash folder on this account — "
                                 "check list_mail_folders for the real name"}
            conn.select(folder)
            typ, _ = conn.uid("copy", uid, trash_folder)
            if typ != "OK":
                return {"error": f"no message with uid {uid} in {folder}"}
            conn.uid("store", uid, "+FLAGS", "(\\Deleted)")
            conn.expunge()
            return {"ok": True, "uid": uid, "from_folder": folder, "to_folder": trash_folder}
        finally:
            conn.logout()

    def list_folders(self) -> dict:
        """Every real IMAP folder on this account, with whatever SPECIAL-USE flags the
        server advertises for it (e.g. \\Archive, \\Trash, \\Sent) — so a folder is looked
        up here rather than assumed, since iCloud's own naming ("Deleted Messages" for
        trash, for instance) isn't the same on every account."""
        conn = self._imap()
        try:
            return {"folders": self._list_entries(conn)}
        finally:
            conn.logout()

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name == "list_emails":
            return self.list_recent(folder=arguments.get("folder", "INBOX"), limit=arguments.get("limit", 10))
        if name == "search_emails":
            return self.search(
                arguments["query"], folder=arguments.get("folder", "INBOX"), limit=arguments.get("limit", 10)
            )
        if name == "read_email":
            return self.read_message(arguments["uid"], folder=arguments.get("folder", "INBOX"))
        if name == "send_email":
            return self.send(arguments["to"], arguments["subject"], arguments["body"])
        if name == "mark_email_read":
            return self.mark_read(arguments["uid"], folder=arguments.get("folder", "INBOX"))
        if name == "archive_email":
            return self.archive_message(arguments["uid"], folder=arguments.get("folder", "INBOX"))
        if name == "delete_email":
            return self.delete_message(arguments["uid"], folder=arguments.get("folder", "INBOX"))
        if name == "list_mail_folders":
            return self.list_folders()
        return {"error": f"unknown mail tool {name}"}
