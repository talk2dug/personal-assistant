"""Chat-facing tools for the owner's own life — separate from business_tools.py the same way
personal_db.py is separate from business_db.py: personal to-dos, personal projects, errands
he's delegated ("find me a doctor"), and credit score tracking and credit-report dispute
letters have nothing to do with the print business. (Kitchen recipes/inventory are their
own sibling module, kitchen_tools.py, dispatched through this same PersonalClient.)

Everything except the two tools that reach LetterStream is ungated for the same reason as
the rest of this module: writes only to Jarvis's own database, spends no money, touches
nothing physical. draft_dispute_letter/track_dispute_letter reach out to LetterStream, but
only ever via letterstream_send_mail (preauth-only, never spends or mails anything on its
own -- see letterstream_client.py) and letterstream_track_mail (read-only status lookup).
Neither of those two tools is gated for the same reason the raw letterstream_send_mail
tool isn't: the one call that actually costs money and puts real mail in the system is
letterstream_authorize_mail, and this module never calls it. That stays exactly where it
already is -- the sensitive_tools/pending_actions gate on LetterStreamContext, proven in
test_engine_letterstream.py -- so a dispute letter can only ever actually be mailed after
the owner explicitly confirms the recipient, the letter text, and the quoted cost.
"""
from . import db, finance, kitchen_tools, meal_plan_db, personal_db

# The three national bureaus' published dispute-processing addresses, so the model isn't
# asked to know or guess them and the owner isn't asked to type them every time. These
# addresses are correct as of when this was written but bureaus do change them
# occasionally -- draft_dispute_letter accepts explicit recipient_* arguments any time to
# override this, which is also the only option for bureau='other' (a furnisher/creditor
# rather than a bureau, which has no fixed lookup).
BUREAU_ADDRESSES = {
    "experian": {
        "recipient_name": "Experian",
        "recipient_address": "P.O. Box 4500",
        "recipient_city": "Allen", "recipient_state": "TX", "recipient_zip": "75013",
    },
    "equifax": {
        "recipient_name": "Equifax Information Services LLC",
        "recipient_address": "P.O. Box 740256",
        "recipient_city": "Atlanta", "recipient_state": "GA", "recipient_zip": "30374",
    },
    "transunion": {
        "recipient_name": "TransUnion Consumer Dispute Center",
        "recipient_address": "P.O. Box 2000",
        "recipient_city": "Chester", "recipient_state": "PA", "recipient_zip": "19016",
    },
}

_RECIPIENT_FIELDS = (
    "recipient_name", "recipient_address", "recipient_city", "recipient_state", "recipient_zip",
)

_KITCHEN_TOOL_NAMES = {t["function"]["name"] for t in kitchen_tools.KITCHEN_TOOLS}

PERSONAL_TOOLS = [
    {"type": "function", "function": {
        "name": "list_personal_projects",
        "description": "List the owner's personal projects (things he's building or working toward, not business).",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["active", "paused", "done", "dropped"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "create_personal_project",
        "description": "Start tracking a new personal project. Use when he describes something he's personally taking on.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "goal": {"type": "string", "description": "What finishing this looks like."},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "update_personal_project",
        "description": "Change a personal project's name, goal, or status.",
        "parameters": {"type": "object", "properties": {
            "project_id": {"type": "integer"},
            "name": {"type": "string"},
            "goal": {"type": "string"},
            "status": {"type": "string", "enum": ["active", "paused", "done", "dropped"]},
        }, "required": ["project_id"]},
    }},
    {"type": "function", "function": {
        "name": "list_personal_tasks",
        "description": "List the owner's personal to-dos, optionally filtered by status or project.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["open", "doing", "done", "dropped"]},
            "project_id": {"type": "integer"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "create_personal_task",
        "description": (
            "Add a personal to-do — errands, chores, appointments to book, anything for his "
            "own life rather than the business (that's create_task). Create these proactively "
            "when he mentions something he needs to do."
        ),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "project_id": {"type": "integer", "description": "Optional personal project to file it under."},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            "due_at": {"type": "string", "description": "Optional local ISO 8601 date/time."},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "update_personal_task",
        "description": "Update a personal task — mark it done, change priority, move it, reword it.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"},
            "text": {"type": "string"},
            "status": {"type": "string", "enum": ["open", "doing", "done", "dropped"]},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            "project_id": {"type": "integer"},
        }, "required": ["task_id"]},
    }},
    {"type": "function", "function": {
        "name": "request_personal_research",
        "description": (
            "Queue a personal errand for the background agent, which searches the web and "
            "writes back a practical answer. Use this whenever he asks you to find or look "
            "into something for him personally that deserves real digging — a doctor or "
            "dentist near him, a service provider, comparing options, how to handle "
            "something. Tell him you've put it in hand and will report back; do not answer "
            "from memory when this is the right call."
        ),
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "Short label, e.g. 'Find a dentist'."},
            "question": {"type": "string", "description": "The specific thing to find out."},
            "project_id": {"type": "integer"},
        }, "required": ["topic"]},
    }},
    {"type": "function", "function": {
        "name": "list_personal_research",
        "description": "Recent personal errands and their findings, including anything still queued.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Default 10."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "list_credit_scores",
        "description": (
            "List the owner's manually-recorded credit score history — there is no live "
            "credit-bureau feed, so this is only ever what he's told Jarvis after checking "
            "a score himself. Returned oldest-first, so it plots as a trend over time."
        ),
        "parameters": {"type": "object", "properties": {
            "bureau": {"type": "string", "enum": ["experian", "equifax", "transunion", "other"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "add_credit_score",
        "description": (
            "Record a credit score for one bureau, right after he tells you one he just "
            "checked (a bureau's own site, a lender's soft pull, Credit Karma, etc). Never "
            "estimate, guess, or invent a score yourself."
        ),
        "parameters": {"type": "object", "properties": {
            "bureau": {"type": "string", "enum": ["experian", "equifax", "transunion", "other"]},
            "score": {"type": "integer", "description": "300-850."},
            "recorded_on": {"type": "string", "description": "Local ISO date he actually checked it; defaults to today."},
            "source": {"type": "string", "description": "e.g. 'Credit Karma', 'Chase Credit Journey', 'hard pull'."},
            "notes": {"type": "string"},
        }, "required": ["bureau", "score"]},
    }},
    {"type": "function", "function": {
        "name": "list_dispute_items",
        "description": (
            "List credit report items being disputed, optionally filtered by status or "
            "bureau. Each item is tracked per bureau through drafted -> mailed -> resolved; "
            "the same inaccurate item reported by two bureaus is two separate items."
        ),
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["drafted", "mailed", "resolved"]},
            "bureau": {"type": "string", "enum": ["experian", "equifax", "transunion", "other"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "create_dispute_item",
        "description": (
            "Start tracking a new disputed credit report item for one bureau. Use when he "
            "identifies something inaccurate on a report he wants disputed. If the same "
            "inaccurate item is reported by more than one bureau, create one item per "
            "bureau — each is disputed and mailed separately."
        ),
        "parameters": {"type": "object", "properties": {
            "bureau": {"type": "string", "enum": ["experian", "equifax", "transunion", "other"]},
            "creditor_name": {"type": "string"},
            "item_description": {"type": "string", "description": "What's being disputed."},
            "reason": {"type": "string", "description": "Why it's inaccurate — this goes into the dispute letter."},
            "account_reference": {"type": "string", "description": "Account/reference number on the report, if any."},
        }, "required": ["bureau", "creditor_name", "item_description", "reason"]},
    }},
    {"type": "function", "function": {
        "name": "update_dispute_item",
        "description": (
            "Update a dispute item's status or details. Only move it to 'resolved' once "
            "he's told you the bureau actually responded or fixed it — LetterStream has no "
            "way to know that on its own; mailing a letter only ever moves an item to "
            "'mailed', never 'resolved'."
        ),
        "parameters": {"type": "object", "properties": {
            "dispute_item_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["drafted", "mailed", "resolved"]},
            "resolution": {"type": "string", "description": "What the bureau actually did, once known."},
            "creditor_name": {"type": "string"},
            "item_description": {"type": "string"},
            "reason": {"type": "string"},
            "account_reference": {"type": "string"},
        }, "required": ["dispute_item_id"]},
    }},
    {"type": "function", "function": {
        "name": "draft_dispute_letter",
        "description": (
            "Compose and QUOTE a physical dispute letter for a tracked item via "
            "LetterStream. This only prices and queues the mailing (LetterStream's preauth "
            "step) — it never sends anything and never spends money. Write the actual "
            "letter_text yourself first: a formal dispute letter identifying the item, the "
            "reason it's inaccurate, and a request that the bureau investigate and correct "
            "or remove it. If the item's bureau is experian, equifax, or transunion and no "
            "recipient_* arguments are given, this mails to that bureau's standard "
            "dispute-processing address; for bureau 'other' (a furnisher/creditor rather "
            "than a bureau) recipient_name/address/city/state/zip are required. After this "
            "returns, tell him the exact recipient, the exact letter text, and the quoted "
            "cost, and do not call letterstream_authorize_mail until he explicitly confirms "
            "he wants it actually mailed — never before."
        ),
        "parameters": {"type": "object", "properties": {
            "dispute_item_id": {"type": "integer"},
            "letter_text": {"type": "string"},
            "recipient_name": {"type": "string"},
            "recipient_address": {"type": "string"},
            "recipient_address_2": {"type": "string"},
            "recipient_city": {"type": "string"},
            "recipient_state": {"type": "string"},
            "recipient_zip": {"type": "string"},
            "mail_type": {
                "type": "string", "enum": ["firstclass", "certified", "certnoerr"],
                "description": "Defaults to certified — dispute letters normally want proof of mailing.",
            },
        }, "required": ["dispute_item_id", "letter_text"]},
    }},
    {"type": "function", "function": {
        "name": "list_dispute_letters",
        "description": (
            "List every LetterStream quote/mailing recorded against one dispute item, most "
            "recent first — check this before drafting a follow-up letter so an earlier "
            "authcode or tracking number isn't lost track of."
        ),
        "parameters": {"type": "object", "properties": {
            "dispute_item_id": {"type": "integer"},
        }, "required": ["dispute_item_id"]},
    }},
    {"type": "function", "function": {
        "name": "record_dispute_letter_mailed",
        "description": (
            "Bookkeeping only — never calls LetterStream. Call this immediately after "
            "letterstream_authorize_mail has actually succeeded for a dispute letter's "
            "authcode, so the tracker reflects that it was really mailed. Never call this "
            "before authorize_mail has actually run and succeeded, and never as a substitute "
            "for getting his explicit confirmation first."
        ),
        "parameters": {"type": "object", "properties": {
            "dispute_letter_id": {"type": "integer"},
            "tracking_number": {"type": "string", "description": "If LetterStream's authorize response included one."},
        }, "required": ["dispute_letter_id"]},
    }},
    {"type": "function", "function": {
        "name": "track_dispute_letter",
        "description": "Check the current USPS tracking/delivery status of a dispute letter that has actually been mailed.",
        "parameters": {"type": "object", "properties": {
            "dispute_letter_id": {"type": "integer"},
        }, "required": ["dispute_letter_id"]},
    }},

    # --- budgets / manual recurring charges / savings goals -----------------------
    # Thin chat-tool wrappers over db.py's already-working CRUD (the Finance page's REST
    # layer calls the exact same functions) -- before these existed, the owner could only
    # manage a budget, a manually-entered bill, or a savings goal by clicking through the
    # Finance page, never by just telling Jarvis. None of these guess numbers on his
    # behalf; they only ever record what he explicitly says.
    {"type": "function", "function": {
        "name": "list_budgets",
        "description": "List the owner's monthly spending-category budget limits.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "set_budget",
        "description": (
            "Set (or update, if one already exists for this category) a monthly spending "
            "limit for a category. Use the category_key/category_label he's discussing — "
            "check list_budgets or recent spending-category context first rather than "
            "inventing a key. Never pick a limit yourself; only record what he states."
        ),
        "parameters": {"type": "object", "properties": {
            "category_key": {"type": "string", "description": "e.g. 'fcat_dining' — Era's category key, not a display name."},
            "category_label": {"type": "string", "description": "Human-readable label, e.g. 'Dining out'."},
            "monthly_limit": {"type": "number"},
        }, "required": ["category_key", "category_label", "monthly_limit"]},
    }},
    {"type": "function", "function": {
        "name": "delete_budget",
        "description": "Remove a monthly budget limit for a category.",
        "parameters": {"type": "object", "properties": {
            "budget_id": {"type": "integer"},
        }, "required": ["budget_id"]},
    }},
    {"type": "function", "function": {
        "name": "list_manual_recurring_charges",
        "description": (
            "List bills/income he's manually entered — for a real charge Era's own "
            "detection missed or got wrong (wrong amount, wrong cadence, wrong next date)."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "add_manual_recurring_charge",
        "description": (
            "Record a recurring bill or income source by hand — use when he mentions a "
            "real, regular charge that either isn't showing up in Era's own detection yet "
            "or that Era has wrong. Never invent an amount or date; ask if unsure."
        ),
        "parameters": {"type": "object", "properties": {
            "description": {"type": "string"},
            "amount": {"type": "number"},
            "direction": {"type": "string", "enum": ["income", "expense"]},
            "cadence": {
                "type": "string", "enum": sorted(finance.VALID_CADENCES),
                "description": (
                    "monthly_on_day/monthly_on_last_day anchor to a real calendar date "
                    "(e.g. paid on the 15th, rent due the last day of the month) without "
                    "drifting over time — prefer these over 'monthly' for anything tied to "
                    "a specific day."
                ),
            },
            "next_expected_date": {"type": "string", "description": "Local ISO date of the next (or most recent) occurrence."},
        }, "required": ["description", "amount", "direction", "cadence", "next_expected_date"]},
    }},
    {"type": "function", "function": {
        "name": "delete_manual_recurring_charge",
        "description": "Remove a manually-entered recurring bill/income entry.",
        "parameters": {"type": "object", "properties": {
            "charge_id": {"type": "integer"},
        }, "required": ["charge_id"]},
    }},
    {"type": "function", "function": {
        "name": "list_savings_goals",
        "description": "List the owner's savings goals, including the projected date each becomes reachable.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "create_savings_goal",
        "description": "Start tracking a new savings goal he names, with the amount he wants to save toward.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "What he's saving for, e.g. 'Japan trip'."},
            "target_amount": {"type": "number"},
            "target_date": {"type": "string", "description": "Optional local ISO date he's aiming for."},
        }, "required": ["name", "target_amount"]},
    }},
    {"type": "function", "function": {
        "name": "update_savings_goal",
        "description": "Change a savings goal's name, target amount, or target date.",
        "parameters": {"type": "object", "properties": {
            "goal_id": {"type": "integer"},
            "name": {"type": "string"},
            "target_amount": {"type": "number"},
            "target_date": {"type": "string", "description": "Local ISO date, or empty string to clear it."},
        }, "required": ["goal_id"]},
    }},
    {"type": "function", "function": {
        "name": "delete_savings_goal",
        "description": "Remove a savings goal.",
        "parameters": {"type": "object", "properties": {
            "goal_id": {"type": "integer"},
        }, "required": ["goal_id"]},
    }},

    # --- pay-period-aware "safe to spend" -------------------------------------------
    {"type": "function", "function": {
        "name": "get_safe_to_spend",
        "description": (
            "How much of his current spendable cash balance can actually be spent right "
            "now without the projected balance dropping below his safety buffer before "
            "the next payday — the real answer to 'what can I actually spend right now'. "
            "Returns null if there isn't enough pay-period data yet to bound the window."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_safety_buffer",
        "description": "The current safety-buffer cushion used by get_safe_to_spend (defaults to $0 until he sets one).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "set_safety_buffer",
        "description": (
            "Set the safety-buffer cushion get_safe_to_spend subtracts — how far above $0 "
            "he wants his projected balance to stay before the next payday. Only set this "
            "to a number he actually states; never pick one for him."
        ),
        "parameters": {"type": "object", "properties": {
            "safety_buffer": {"type": "number", "description": "A non-negative dollar amount."},
        }, "required": ["safety_buffer"]},
    }},
]

PERSONAL_SYSTEM_NOTE = (
    " You also keep track of the owner's own personal life, separate from the business: his "
    "personal projects, his to-do list, and errands he's asked you to look into. Treat that as "
    "a standing responsibility. When he mentions something he's personally working on, record "
    "it with create_personal_project; when he mentions something he needs to do, record it with "
    "create_personal_task rather than replying with encouragement and letting it evaporate. When "
    "he asks you to find or look into something personal that deserves real digging — a doctor, "
    "a service, comparing options — queue it with request_personal_research rather than "
    "answering from memory: it runs a real web search in the background and reports back, so "
    "say you'll look into it rather than pretending you already know. Never confuse this with "
    "the business tools (create_project/create_task/request_research) — those are for the "
    "print business, these are for him."
    " You also track his credit: add_credit_score whenever he tells you a score he just "
    "checked (never estimate one yourself), and the credit-report dispute tracker "
    "(create_dispute_item, update_dispute_item, draft_dispute_letter, list_dispute_letters, "
    "track_dispute_letter). draft_dispute_letter only quotes a letter through LetterStream's "
    "preauth step — it never spends money or mails anything by itself. After drafting, always "
    "relay the exact recipient, the exact letter text, and the quoted cost, and wait for his "
    "explicit yes before ever calling letterstream_authorize_mail — that is the one call that "
    "releases real postage and puts a real, unrecallable piece of mail in the system, and it "
    "requires his confirmation every single time, no matter how routine the dispute feels. The "
    "instant an authorization actually succeeds, call record_dispute_letter_mailed so the "
    "tracker reflects reality — but only then, never before, and never as a stand-in for "
    "getting the confirmation itself."
    " You also manage his finances conversationally, not just through the Finance page: "
    "set_budget/delete_budget for monthly category spending limits, "
    "add_manual_recurring_charge/delete_manual_recurring_charge for a bill or income source "
    "Era's own detection missed or got wrong, and create_savings_goal/update_savings_goal/"
    "delete_savings_goal for what he's saving toward. Never invent a budget limit, a bill "
    "amount, or a savings target yourself — record only numbers he actually states. When he "
    "asks what he can actually spend right now, use get_safe_to_spend: it's the lowest his "
    "projected balance will hit before his next payday, minus his safety buffer "
    "(get_safety_buffer/set_safety_buffer) — a real pay-period-aware answer, not a flat "
    "category cap."
)


class PersonalClient:
    """Executes the personal tools. Same call_tool shape as every other integration.

    letterstream, when given, is the raw call_tool(name, arguments) object LetterStream's
    own raw tools use (LetterStreamTools) — not the whole LetterStreamContext dataclass.
    Loose coupling on purpose: this class only needs something it can call
    letterstream_send_mail/letterstream_track_mail on, the same way MailClient or any
    other integration is handed in elsewhere. None of the money-spending logic
    (from_addr, PDF rendering, the HMAC auth, preauth vs. doauth) is duplicated here.
    """

    def __init__(self, db_path: str, owner_user_id: int, letterstream=None, kroger=None):
        self.db_path = db_path
        self.owner_user_id = owner_user_id
        self.letterstream = letterstream
        # Raw kroger.mcp_client, same loose-coupling convention as letterstream above --
        # only kitchen_tools.sync_kroger_purchases actually calls it (see dispatch below).
        self.kroger = kroger

    def call_tool(self, name: str, arguments: dict) -> dict:
        db_path, owner = self.db_path, self.owner_user_id

        if name == "list_personal_projects":
            return {"projects": personal_db.list_projects(db_path, owner, arguments.get("status"))}
        if name == "create_personal_project":
            pid = personal_db.create_project(db_path, owner, arguments["name"], arguments.get("goal"))
            return {"ok": True, "project_id": pid}
        if name == "update_personal_project":
            ok = personal_db.update_project(
                db_path, owner, arguments["project_id"],
                name=arguments.get("name"), goal=arguments.get("goal"), status=arguments.get("status"))
            return {"ok": ok}

        if name == "list_personal_tasks":
            return {"tasks": personal_db.list_tasks(
                db_path, owner, arguments.get("status"), arguments.get("project_id"))}
        if name == "create_personal_task":
            tid = personal_db.create_task(
                db_path, owner, arguments["text"], arguments.get("project_id"),
                arguments.get("priority", "normal"), arguments.get("due_at"))
            return {"ok": True, "task_id": tid}
        if name == "update_personal_task":
            ok = personal_db.update_task(
                db_path, owner, arguments["task_id"], text=arguments.get("text"),
                status=arguments.get("status"), priority=arguments.get("priority"),
                project_id=arguments.get("project_id"))
            return {"ok": ok}

        if name == "request_personal_research":
            rid = personal_db.create_research(
                db_path, owner, arguments["topic"], arguments.get("question"), arguments.get("project_id"))
            return {
                "ok": True, "research_id": rid,
                "message": (
                    "Queued for the background research agent. Tell him you've put it in hand "
                    "and will report back — you do not have findings yet."
                ),
            }
        if name == "list_personal_research":
            return {"research": personal_db.list_research(db_path, owner, arguments.get("limit", 10))}

        if name == "list_credit_scores":
            return {"scores": personal_db.list_credit_score_entries(db_path, owner, arguments.get("bureau"))}
        if name == "add_credit_score":
            entry_id = personal_db.create_credit_score_entry(
                db_path, owner, arguments["bureau"], arguments["score"],
                arguments.get("recorded_on"), arguments.get("source"), arguments.get("notes"))
            return {"ok": True, "entry_id": entry_id}

        if name == "list_dispute_items":
            return {"items": personal_db.list_dispute_items(
                db_path, owner, arguments.get("status"), arguments.get("bureau"))}
        if name == "create_dispute_item":
            item_id = personal_db.create_dispute_item(
                db_path, owner, arguments["bureau"], arguments["creditor_name"],
                arguments["item_description"], arguments["reason"], arguments.get("account_reference"))
            return {"ok": True, "dispute_item_id": item_id}
        if name == "update_dispute_item":
            ok = personal_db.update_dispute_item(
                db_path, owner, arguments["dispute_item_id"],
                status=arguments.get("status"), resolution=arguments.get("resolution"),
                creditor_name=arguments.get("creditor_name"), item_description=arguments.get("item_description"),
                reason=arguments.get("reason"), account_reference=arguments.get("account_reference"))
            return {"ok": ok}

        if name == "draft_dispute_letter":
            return self._draft_dispute_letter(arguments)

        if name == "list_dispute_letters":
            return {"letters": personal_db.list_dispute_letters(db_path, owner, arguments["dispute_item_id"])}

        if name == "record_dispute_letter_mailed":
            ok = personal_db.mark_dispute_letter_mailed(
                db_path, owner, arguments["dispute_letter_id"], arguments.get("tracking_number"))
            return {"ok": ok}

        if name == "track_dispute_letter":
            return self._track_dispute_letter(arguments)

        if name == "list_budgets":
            return {"budgets": db.list_budgets(db_path, owner)}
        if name == "set_budget":
            budget_id = db.create_budget(
                db_path, owner, arguments["category_key"], arguments["category_label"], arguments["monthly_limit"])
            return {"ok": True, "budget_id": budget_id}
        if name == "delete_budget":
            ok = db.delete_budget(db_path, arguments["budget_id"])
            return {"ok": ok}

        if name == "list_manual_recurring_charges":
            return {"charges": db.list_manual_recurring_charges(db_path, owner)}
        if name == "add_manual_recurring_charge":
            if arguments["direction"] not in ("income", "expense"):
                return {"error": "direction must be 'income' or 'expense'"}
            if arguments["cadence"] not in finance.VALID_CADENCES:
                return {"error": f"cadence must be one of {sorted(finance.VALID_CADENCES)}"}
            charge_id = db.create_manual_recurring_charge(
                db_path, owner, arguments["description"], arguments["amount"],
                arguments["direction"], arguments["cadence"], arguments["next_expected_date"])
            return {"ok": True, "charge_id": charge_id}
        if name == "delete_manual_recurring_charge":
            ok = db.delete_manual_recurring_charge(db_path, arguments["charge_id"])
            return {"ok": ok}

        if name == "list_savings_goals":
            return {"goals": db.list_savings_goals(db_path, owner)}
        if name == "create_savings_goal":
            goal_id = db.create_savings_goal(
                db_path, owner, arguments["name"], arguments["target_amount"], arguments.get("target_date"))
            return {"ok": True, "goal_id": goal_id}
        if name == "update_savings_goal":
            kwargs = {"name": arguments.get("name"), "target_amount": arguments.get("target_amount")}
            if "target_date" in arguments:
                kwargs["target_date"] = arguments["target_date"] or None
            ok = db.update_savings_goal(db_path, arguments["goal_id"], **kwargs)
            return {"ok": ok}
        if name == "delete_savings_goal":
            ok = db.delete_savings_goal(db_path, arguments["goal_id"])
            return {"ok": ok}

        if name == "get_safe_to_spend":
            return meal_plan_db.get_safe_to_spend(db_path, owner)
        if name == "get_safety_buffer":
            return {"safety_buffer": float(db.get_setting(db_path, db.FINANCE_SAFETY_BUFFER_SETTING, "0") or 0)}
        if name == "set_safety_buffer":
            value = arguments["safety_buffer"]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                return {"error": "safety_buffer must be a non-negative number"}
            db.set_setting(db_path, db.FINANCE_SAFETY_BUFFER_SETTING, str(float(value)))
            return {"ok": True, "safety_buffer": float(value)}

        if name in _KITCHEN_TOOL_NAMES:
            return kitchen_tools.dispatch(db_path, owner, name, arguments, kroger_mcp_client=self.kroger)

        return {"error": f"unknown personal tool {name}"}

    def _draft_dispute_letter(self, arguments: dict) -> dict:
        db_path, owner = self.db_path, self.owner_user_id
        item = personal_db.get_dispute_item(db_path, owner, arguments["dispute_item_id"])
        if item is None:
            return {"error": "dispute item not found"}
        if self.letterstream is None:
            return {"error": "LetterStream is not configured"}

        recipient = {f: arguments.get(f) for f in _RECIPIENT_FIELDS}
        recipient["recipient_address_2"] = arguments.get("recipient_address_2", "")
        if not recipient["recipient_name"]:
            default = BUREAU_ADDRESSES.get(item["bureau"])
            if default is None:
                return {"error": (
                    "recipient_name/recipient_address/recipient_city/recipient_state/recipient_zip "
                    "are required when the dispute item's bureau is 'other'"
                )}
            recipient = {**default, "recipient_address_2": ""}
        missing = [f for f in _RECIPIENT_FIELDS if not recipient.get(f)]
        if missing:
            return {"error": f"missing recipient fields: {', '.join(missing)}"}

        letter_text = arguments["letter_text"]
        mail_type = arguments.get("mail_type", "certified")
        quote = self.letterstream.call_tool("letterstream_send_mail", {
            "letter_text": letter_text, "mail_type": mail_type, **recipient,
        })
        letter_id = personal_db.create_dispute_letter(
            db_path, item["id"], letter_text=letter_text,
            recipient_name=recipient["recipient_name"], recipient_address=recipient["recipient_address"],
            recipient_address_2=recipient.get("recipient_address_2") or None,
            recipient_city=recipient["recipient_city"], recipient_state=recipient["recipient_state"],
            recipient_zip=recipient["recipient_zip"], mail_type=mail_type,
            quoted_cost=quote.get("cost"), authcode=quote.get("authcode"),
            job_name=quote.get("job"), doc_id=quote.get("doc_id"),
        )
        return {"ok": True, "dispute_letter_id": letter_id, "quote": quote}

    def _track_dispute_letter(self, arguments: dict) -> dict:
        db_path, owner = self.db_path, self.owner_user_id
        letter = personal_db.get_dispute_letter(db_path, owner, arguments["dispute_letter_id"])
        if letter is None:
            return {"error": "dispute letter not found"}
        if self.letterstream is None:
            return {"error": "LetterStream is not configured"}

        result = self.letterstream.call_tool("letterstream_track_mail", {
            "tracking_number": letter.get("tracking_number"), "doc_id": letter.get("doc_id"), "kind": "track",
        })
        cert = result.get("cert") or result.get("tracking_number")
        if cert and cert != letter.get("tracking_number"):
            personal_db.update_dispute_letter_tracking(db_path, owner, letter["id"], cert)
        return {"ok": True, "tracking": result}
