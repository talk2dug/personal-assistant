"""Read/send access to the user's iCloud Mail account via IMAP/SMTP, using the same
Apple ID + app-specific password already configured for CalDAV (Phase 3) — Apple
app-specific passwords authenticate the whole Apple ID, not a single protocol, so no
new credential is needed. Exposes call_tool(name, arguments) so engine.py can treat
this exactly like Era/phone's MCP-style tool contexts, even though nothing here
actually goes over MCP.

Also exposes the raw IMAP primitives (list_folders, move_message, append_draft) that
mail_tools.py's MailTriageClient builds on for junk-flagging, auto-filing, and
draft-reply -- kept here rather than duplicated because they're just more IMAP
operations against the same connection this file already knows how to open.
"""
import email
import imaplib
import re
import smtplib
import time
from email.header import decode_header
from email.message import EmailMessage

IMAP_HOST = "imap.mail.me.com"
SMTP_HOST = "smtp.mail.me.com"


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


class MailClient:
    def __init__(self, apple_id: str, app_password: str):
        self.apple_id = apple_id
        self.app_password = app_password

    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(IMAP_HOST)
        conn.login(self.apple_id, self.app_password)
        return conn

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
                "message_id": msg.get("Message-ID", ""),
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

    def list_folders(self) -> dict:
        """IMAP folder names available in the account, so filing logic never has to guess
        or hardcode which folders actually exist."""
        conn = self._imap()
        try:
            typ, data = conn.list()
            folders = []
            if typ == "OK":
                for raw in data or []:
                    if raw is None:
                        continue
                    decoded = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
                    match = re.search(r'"([^"]*)"\s*$', decoded) or re.search(r"(\S+)\s*$", decoded)
                    if match:
                        folders.append(match.group(1))
            return {"folders": folders}
        finally:
            conn.logout()

    def move_message(self, uid: str, target_folder: str, source_folder: str = "INBOX") -> dict:
        """Moves a message between folders -- the mechanics behind both flag_junk_email
        (moving to Junk) and file_email (moving to a category folder).

        Tries UID MOVE (RFC 6851), which iCloud supports; falls back to the older
        COPY + mark \\Deleted + EXPUNGE dance for any IMAP server that doesn't, so this
        isn't silently broken against a mail provider that predates that extension.
        """
        conn = self._imap()
        try:
            conn.select(source_folder)
            try:
                typ, data = conn.uid("MOVE", uid, target_folder)
            except imaplib.IMAP4.error as e:
                typ, data = "NO", [str(e).encode()]
            if typ == "OK":
                return {"ok": True, "uid": uid, "from": source_folder, "to": target_folder}

            typ, data = conn.uid("COPY", uid, target_folder)
            if typ != "OK":
                return {"error": f"could not move uid {uid} to '{target_folder}': {data}"}
            conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            conn.expunge()
            return {"ok": True, "uid": uid, "from": source_folder, "to": target_folder, "method": "copy+expunge"}
        finally:
            conn.logout()

    def append_draft(
        self, to: str, subject: str, body: str, folder: str = "Drafts",
        in_reply_to: str | None = None, references: str | None = None,
    ) -> dict:
        """Saves a reply into the Drafts folder without sending it -- what save_draft_email
        uses so a drafted reply is something the owner reviews and sends himself, never
        something Jarvis sends on his behalf."""
        message = EmailMessage()
        message["From"] = self.apple_id
        message["To"] = to
        message["Subject"] = subject
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = references
        message.set_content(body)
        conn = self._imap()
        try:
            typ, data = conn.append(
                folder, "\\Draft", imaplib.Time2Internaldate(time.time()), message.as_bytes(),
            )
            if typ != "OK":
                return {"error": f"could not save draft to '{folder}': {data}"}
            return {"ok": True, "folder": folder, "to": to, "subject": subject}
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
        if name == "list_folders":
            return self.list_folders()
        return {"error": f"unknown mail tool {name}"}
