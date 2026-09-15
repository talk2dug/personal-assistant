"""Letting Jarvis handle a conversation by text on the owner's behalf.

The shape of the thing this is for: "ask Nadia if she's free Thursday, and if she is,
tell her I'll pick her up at eight." That is two sends separated by someone else's reply,
which means Jarvis needs to send, read what came back, and send again.

SENDING IS GATED AND ALWAYS WILL BE. `send_text` goes through the same
pending_actions/review confirmation every other outward-facing action here uses -- the
Kroger cart, a CCXT trade, a mailed dispute letter. The reason is not that the model is
untrustworthy in general; it is that a text to another human cannot be recalled, is read
as having come from Jack, and lands on someone who did not opt into being messaged by
software. A wrong one is not a bug you fix, it is a thing you have to explain to a
person. So the owner sees the exact recipient and the exact words before anything leaves.

Reading is not gated. The messages are already the owner's own, sitting in his own
database, and a Jarvis that can send but cannot see the reply is useless for the only
job this exists to do.

Texts go out from the house LTE line, not from Jack's personal number. That is honest by
construction -- the recipient sees the number that has been talking to them, and nobody
is impersonating anyone.
"""
import json
import logging

from . import cellular

logger = logging.getLogger(__name__)

SMS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_text_contacts",
            "description": (
                "Everyone Jarvis can send a text message to, with their relationship to "
                "the owner. Check this before sending if you are unsure who is meant."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_text_thread",
            "description": (
                "The recent text conversation with one person, newest first. Use this to "
                "see whether someone has replied yet, and what they said, before deciding "
                "what to send next."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "who": {"type": "string",
                            "description": "A contact name, or a phone number."},
                    "limit": {"type": "integer",
                              "description": "How many messages to return (default 10)."},
                },
                "required": ["who"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_text",
            "description": (
                "Send a text message on the owner's behalf, from the house's cellular "
                "line. This REQUIRES the owner's explicit confirmation before anything "
                "is sent -- calling it does not send. Write the message exactly as it "
                "should be read by the recipient: it is a message from the owner, not a "
                "note about him, so write 'I'll pick you up at 8' rather than 'Jack says "
                "he will pick you up at 8' unless he asked you to speak as yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string",
                           "description": "A contact name, or a full phone number."},
                    "message": {"type": "string",
                                "description": "The exact text to send."},
                },
                "required": ["to", "message"],
            },
        },
    },
]

SMS_TOOL_NAMES = {t["function"]["name"] for t in SMS_TOOLS}
# The one that reaches another human. Everything else only reads the owner's own data.
SMS_SENSITIVE_TOOLS = {"send_text"}

SMS_SYSTEM_NOTE = (
    "\n\nYou can send text messages for the owner from the house cellular line "
    "(send_text), see who you may text (list_text_contacts), and read what someone has "
    "replied (read_text_thread). Sending always requires his explicit confirmation -- "
    "calling send_text only proposes it. When he asks you to handle an exchange "
    "('ask her if she's free Thursday, then tell her I'll pick her up at 8'), send the "
    "first message, and check read_text_thread on a later turn to see whether they have "
    "answered before sending the second. Do not invent a reply that has not arrived."
)


def resolve_recipient(db_path: str, to: str) -> tuple[str | None, str | None, str | None]:
    """(number, display name, error). A raw number is allowed but flagged as unknown.

    An unrecognised NAME is an error rather than a guess: "text Sarah" with two Sarahs on
    file must stop and ask, not pick one. A raw number is let through because the
    confirmation step shows it in full, and that is where a typo gets caught by the one
    person who can recognise it.
    """
    contact = cellular.find_contact(db_path, to)
    if contact:
        return contact["number"], contact["name"], None
    digits = cellular.normalize_number(to)
    if len(digits) >= 10:
        return digits, None, None
    return None, None, (
        f"No contact matches {to!r}, and it is not a full phone number. "
        "Use list_text_contacts to see who is on file, or ask the owner for the number."
    )


def handle(db_path: str, name: str, arguments: dict) -> str:
    """Dispatch a non-sensitive SMS tool. send_text never reaches here -- engine.py
    intercepts it into the confirmation flow first."""
    if name == "list_text_contacts":
        contacts = cellular.list_contacts(db_path)
        if not contacts:
            return json.dumps({"contacts": [],
                               "note": "Nobody on file yet. Ask the owner for a name and number."})
        return json.dumps({"contacts": [
            {"name": c["name"], "relationship": c["relationship"], "number": c["number"]}
            for c in contacts]})

    if name == "read_text_thread":
        who = str(arguments.get("who") or "")
        number, display, error = resolve_recipient(db_path, who)
        if error:
            return json.dumps({"error": error})
        limit = int(arguments.get("limit") or 10)
        thread = cellular.thread_with(db_path, number, limit=limit)
        return json.dumps({
            "who": display or number,
            "messages": [
                # 'them'/'you' rather than inbound/outbound: the model is reading a
                # conversation, not a transport log.
                {"from": "them" if m["direction"] == "inbound" else "you",
                 "text": m["text"], "at": m["created_at"], "status": m["status"]}
                for m in thread],
            "note": "Newest first. Nothing here means they have not replied yet."
        })

    return json.dumps({"error": f"unknown sms tool {name}"})


def execute_send(db_path: str, arguments: dict) -> str:
    """Actually queue a confirmed text. Reached only after the owner says yes."""
    to = str(arguments.get("to") or "")
    message = str(arguments.get("message") or "").strip()
    number, display, error = resolve_recipient(db_path, to)
    if error:
        return json.dumps({"error": error})
    if not message:
        return json.dumps({"error": "refusing to send an empty message"})
    message_id = cellular.queue_outbound(db_path, number, message)
    logger.warning("sms: queued a text to %s (%s) on the owner's behalf",
                   display or number, number)
    return json.dumps({"queued": True, "id": message_id,
                       "to": display or number, "text": cellular.trim_for_sms(message)})
