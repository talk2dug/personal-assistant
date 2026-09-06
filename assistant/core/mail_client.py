"""Read/send access to the user's iCloud Mail account via IMAP/SMTP, using the same
Apple ID + app-specific password already configured for CalDAV (Phase 3) — Apple
app-specific passwords authenticate the whole Apple ID, not a single protocol, so no
new credential is needed. Exposes call_tool(name, arguments) so engine.py can treat
this exactly like Era/phone's MCP-style tool contexts, even though nothing here
actually goes over MCP.
"""
import email
import imaplib
import re
import smtplib
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
        return {"error": f"unknown mail tool {name}"}
