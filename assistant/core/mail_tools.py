"""Chat-facing tools for autonomous email triage, layered on the existing iCloud Mail
integration (mail_client.py / MailClient): junk-flagging, draft-reply generation, and
auto-filing into folders.

MailTriageClient exposes call_tool(name, arguments) with the same shape as every other
integration in this codebase (personal_tools.py's PersonalClient, mail_client.py's
MailClient itself, etc.), so it plugs into engine.py's existing tool-dispatch and
confirmation-gate machinery unchanged: mutating actions (flag_junk_email, file_email,
save_draft_email) belong in a sensitive_tools set the same way send_email does, since
they change the real mailbox; triage_inbox/draft_reply_email/list_mail_triage_log are
read-only or advisory and should run directly, same reasoning as list_emails/read_email.
"""
from . import mail_triage as triage
from . import mail_triage_db as triage_db

MAIL_TRIAGE_TOOLS = [
    {"type": "function", "function": {
        "name": "triage_inbox",
        "description": (
            "Scan recent emails in a folder and classify each one: a junk/spam likelihood "
            "score, a category (order_inquiry, support_request, billing_invoice, "
            "meeting_scheduling, newsletter_marketing, spam_junk, other), the folder it "
            "should probably be filed to, and a draft reply for the categories where a "
            "reply is expected. This only reads and classifies -- it never moves, flags, "
            "or sends anything. Use it to give the owner a triage summary, or before "
            "calling flag_junk_email/file_email/draft_reply_email on a specific message."
        ),
        "parameters": {"type": "object", "properties": {
            "folder": {"type": "string", "description": "Default INBOX."},
            "limit": {"type": "integer", "description": "Default 20, max 25 when deep=true."},
            "unread_only": {"type": "boolean", "description": "Default false."},
            "deep": {"type": "boolean", "description": (
                "Default false. When true, fetches each message's body for more accurate "
                "junk/category detection -- slower, one IMAP fetch per message."
            )},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "flag_junk_email",
        "description": (
            "Move a message that's been identified as junk/spam into the Junk folder. A "
            "real mailbox change -- confirm with the owner first unless he's already asked "
            "you to clean up junk mail."
        ),
        "parameters": {"type": "object", "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string", "description": "Source folder, default INBOX."},
        }, "required": ["uid"]},
    }},
    {"type": "function", "function": {
        "name": "file_email",
        "description": (
            "Move a message into a category-appropriate folder (or one you specify). Real "
            "mailbox change. The target folder must already exist in the mail account -- "
            "this never creates folders."
        ),
        "parameters": {"type": "object", "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string", "description": "Source folder, default INBOX."},
            "target_folder": {"type": "string", "description": (
                "Where to file it. Omit to use the category-based suggestion from the most "
                "recent triage of this message (classifies it fresh if none is on record yet)."
            )},
        }, "required": ["uid"]},
    }},
    {"type": "function", "function": {
        "name": "draft_reply_email",
        "description": (
            "Generate a draft reply for one message, based on its category (order update, "
            "support request, billing question, scheduling request). Returns text only -- "
            "nothing is saved or sent. Use save_draft_email to actually save it into Drafts."
        ),
        "parameters": {"type": "object", "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string", "description": "Default INBOX."},
        }, "required": ["uid"]},
    }},
    {"type": "function", "function": {
        "name": "save_draft_email",
        "description": (
            "Save a reply into the account's Drafts folder for the owner to review and send "
            "himself -- this never sends anything. If body is omitted, generates one the same "
            "way draft_reply_email does. Real mailbox change (creates a draft message)."
        ),
        "parameters": {"type": "object", "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string", "description": "Source folder, default INBOX."},
            "to": {"type": "string", "description": "Defaults to the original sender's address."},
            "subject": {"type": "string", "description": "Defaults to 'Re: <original subject>'."},
            "body": {"type": "string", "description": "Defaults to an auto-generated draft."},
        }, "required": ["uid"]},
    }},
    {"type": "function", "function": {
        "name": "list_mail_triage_log",
        "description": "Recent triage decisions Jarvis has made about emails -- junk calls, categories, filing, drafts.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Default 20."},
            "only_junk": {"type": "boolean"},
            "category": {"type": "string"},
        }, "required": []},
    }},
]

MAIL_TRIAGE_SYSTEM_NOTE = (
    " You can also triage the owner's inbox: use triage_inbox to scan and classify recent "
    "email (junk likelihood, category, suggested folder, and a draft reply where one makes "
    "sense) without changing anything. Moving or flagging real mail (flag_junk_email, "
    "file_email, save_draft_email) changes his actual mailbox, so confirm with him before "
    "doing it unless he's clearly already asked you to clean up or file things. Never invent "
    "a target folder that triage_inbox didn't return -- filing to a folder that doesn't exist "
    "in his account will fail. draft_reply_email and save_draft_email produce a draft for him "
    "to review, never an email that actually sends."
)


class MailTriageClient:
    """Wraps an existing MailClient (mail_client.py) to add classification, filing, and
    drafting on top of its list/search/read/move/append primitives, recording what it
    decided in mail_triage_db.py so the same message isn't reclassified from scratch on
    every look and so there's an audit trail of what Jarvis did to real mail.
    """

    def __init__(
        self, mail_client, db_path: str, owner_user_id: int,
        category_folders: dict | None = None, junk_threshold: float | None = None,
    ):
        self.mail = mail_client
        self.db_path = db_path
        self.owner_user_id = owner_user_id
        self.category_folders = {**triage.DEFAULT_CATEGORY_FOLDERS, **(category_folders or {})}
        self.junk_threshold = triage.JUNK_THRESHOLD if junk_threshold is None else junk_threshold
        triage_db.init_mail_triage_db(db_path)

    # -- internal ------------------------------------------------------------------

    def _classify(self, uid: str, folder: str, deep: bool, headers: dict | None = None) -> dict:
        body = ""
        if headers is None:
            if deep:
                full = self.mail.read_message(uid, folder=folder)
                if "error" in full:
                    return full
                sender, subject, body = full.get("from", ""), full.get("subject", ""), full.get("body", "")
            else:
                listed = self.mail.list_recent(folder=folder, limit=200).get("emails", [])
                match = next((e for e in listed if e["uid"] == uid), None)
                if match is None:
                    return {"error": f"no message with uid {uid} in {folder}"}
                sender, subject = match.get("from", ""), match.get("subject", "")
        else:
            sender, subject = headers.get("from", ""), headers.get("subject", "")
            if deep:
                full = self.mail.read_message(uid, folder=folder)
                body = full.get("body", "") if "error" not in full else ""

        junk_score, junk_reasons = triage.score_junk(sender, subject, body)
        junk = triage.is_junk(junk_score, self.junk_threshold)
        category, confidence, cat_reasons = triage.classify_category(
            sender, subject, body, junk_score=junk_score if junk else None,
        )
        suggested_folder = triage.suggest_folder(category, self.category_folders)
        drafted_reply = triage.draft_reply(category, sender, subject, body)

        triage_db.upsert_triage(
            self.db_path, self.owner_user_id, folder, uid,
            sender=sender, subject=subject, is_junk=junk, junk_score=junk_score,
            junk_reasons=junk_reasons, category=category, category_confidence=confidence,
            category_reasons=cat_reasons, suggested_folder=suggested_folder, drafted_reply=drafted_reply,
        )
        return {
            "uid": uid, "folder": folder, "from": sender, "subject": subject,
            "junk": {"is_junk": junk, "score": round(junk_score, 2), "reasons": junk_reasons},
            "category": category, "category_confidence": round(confidence, 2), "category_reasons": cat_reasons,
            "suggested_folder": suggested_folder, "suggested_reply": drafted_reply,
        }

    # -- tools ---------------------------------------------------------------------

    def triage_inbox(
        self, folder: str = "INBOX", limit: int = 20, unread_only: bool = False, deep: bool = False,
    ) -> dict:
        if deep:
            limit = min(limit, 25)
        listed = self.mail.list_recent(folder=folder, limit=limit).get("emails", [])
        if unread_only:
            listed = [e for e in listed if e.get("unread")]
        results = [self._classify(e["uid"], folder, deep, headers=e) for e in listed]
        results = [r for r in results if "error" not in r]
        junk_count = sum(1 for r in results if r["junk"]["is_junk"])
        counts_by_category: dict = {}
        for r in results:
            counts_by_category[r["category"]] = counts_by_category.get(r["category"], 0) + 1
        return {"triaged": results, "junk_count": junk_count, "counts_by_category": counts_by_category}

    def flag_junk_email(self, uid: str, folder: str = "INBOX") -> dict:
        junk_folder = self.category_folders.get(triage.CATEGORY_SPAM, "Junk")
        result = self.mail.move_message(uid, junk_folder, source_folder=folder)
        if result.get("ok"):
            triage_db.upsert_triage(
                self.db_path, self.owner_user_id, folder, uid, is_junk=True, filed_folder=junk_folder,
            )
        return result

    def file_email(self, uid: str, folder: str = "INBOX", target_folder: str | None = None) -> dict:
        if not target_folder:
            record = triage_db.get_triage(self.db_path, self.owner_user_id, folder, uid)
            if record and record.get("suggested_folder"):
                target_folder = record["suggested_folder"]
            else:
                classified = self._classify(uid, folder, deep=True)
                if "error" in classified:
                    return classified
                target_folder = classified["suggested_folder"]
        result = self.mail.move_message(uid, target_folder, source_folder=folder)
        if result.get("ok"):
            triage_db.mark_filed(self.db_path, self.owner_user_id, folder, uid, target_folder)
        return result

    def draft_reply_email(self, uid: str, folder: str = "INBOX") -> dict:
        full = self.mail.read_message(uid, folder=folder)
        if "error" in full:
            return full
        sender, subject, body = full.get("from", ""), full.get("subject", ""), full.get("body", "")
        junk_score, _ = triage.score_junk(sender, subject, body)
        junk = triage.is_junk(junk_score, self.junk_threshold)
        category, _, _ = triage.classify_category(sender, subject, body, junk_score=junk_score if junk else None)
        draft = triage.draft_reply(category, sender, subject, body)
        triage_db.upsert_triage(self.db_path, self.owner_user_id, folder, uid, category=category, drafted_reply=draft)
        if draft is None:
            return {"category": category, "draft": None,
                    "note": f"no standard reply template applies to category '{category}'"}
        return {"category": category, "draft": draft}

    def save_draft_email(
        self, uid: str, folder: str = "INBOX", to: str | None = None,
        subject: str | None = None, body: str | None = None,
    ) -> dict:
        full = self.mail.read_message(uid, folder=folder)
        if "error" in full:
            return full
        to = to or full.get("from", "")
        original_subject = full.get("subject", "")
        subject = subject or (
            original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
        )
        if not body:
            generated = self.draft_reply_email(uid, folder=folder)
            if generated.get("draft") is None:
                return {"error": "no draft body was supplied and no template applies to this message's category"}
            body = generated["draft"]
        result = self.mail.append_draft(to, subject, body, in_reply_to=full.get("message_id") or None)
        if result.get("ok"):
            triage_db.upsert_triage(self.db_path, self.owner_user_id, folder, uid, drafted_reply=body)
        return result

    def list_mail_triage_log(
        self, limit: int = 20, only_junk: bool | None = None, category: str | None = None,
    ) -> dict:
        return {"log": triage_db.list_triage(self.db_path, self.owner_user_id, limit, only_junk, category)}

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name == "triage_inbox":
            return self.triage_inbox(
                arguments.get("folder", "INBOX"), arguments.get("limit", 20),
                arguments.get("unread_only", False), arguments.get("deep", False),
            )
        if name == "flag_junk_email":
            return self.flag_junk_email(arguments["uid"], arguments.get("folder", "INBOX"))
        if name == "file_email":
            return self.file_email(
                arguments["uid"], arguments.get("folder", "INBOX"), arguments.get("target_folder"),
            )
        if name == "draft_reply_email":
            return self.draft_reply_email(arguments["uid"], arguments.get("folder", "INBOX"))
        if name == "save_draft_email":
            return self.save_draft_email(
                arguments["uid"], arguments.get("folder", "INBOX"), arguments.get("to"),
                arguments.get("subject"), arguments.get("body"),
            )
        if name == "list_mail_triage_log":
            return self.list_mail_triage_log(
                arguments.get("limit", 20), arguments.get("only_junk"), arguments.get("category"),
            )
        return {"error": f"unknown mail triage tool {name}"}
