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
import logging

from . import business_db, db, finance, kitchen_tools, meal_plan_db, personal_db

logger = logging.getLogger(__name__)

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
            "track": {"type": "string", "enum": ["personal", "project"], "description": (
                "'personal' is his own life — errands, appointments, Ghost, the car — and is "
                "the default. 'project' is work on building Jarvis itself. Only personal "
                "tasks appear when planning his day, because ranking 'wake-word arbitration' "
                "against 'find a vet' makes both lists useless.")},
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
        "name": "get_credit_picture",
        "description": (
            "Where his credit stands right now: score history, the latest report and its "
            "tradelines, revolving utilisation and what it would cost to get under 30% and "
            "10%, derogatory marks with the date each ages off on its own, every dispute in "
            "flight with its deadline and whether the bureau has blown it, and the credit "
            "lines suggested so far. Call this before answering anything about his credit — "
            "it is exact, and a score quoted from memory is worse than none."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "add_credit_report",
        "description": (
            "Record a credit report he has pulled or uploaded. Create this first, then add "
            "each account on it with add_tradeline. Score models differ by 50+ points, so "
            "record which one it is when the report says."
        ),
        "parameters": {"type": "object", "properties": {
            "bureau": {"type": "string", "enum": ["experian", "equifax", "transunion", "other"]},
            "pulled_on": {"type": "string", "description": "Local ISO date the report is dated."},
            "score": {"type": "integer"},
            "score_model": {"type": "string", "description": "e.g. 'FICO 8', 'VantageScore 3.0'."},
            "source": {"type": "string", "description": "e.g. annualcreditreport.com."},
            "notes": {"type": "string"},
        }, "required": ["bureau", "pulled_on"]},
    }},
    {"type": "function", "function": {
        "name": "add_tradeline",
        "description": (
            "Add one account from a credit report. A tradeline is what the BUREAU says — a "
            "claim about him that may be wrong, stale or not his — which is not the same as "
            "what he owes, so never merge these with his debts. credit_limit matters more "
            "than it looks: utilisation is about 30% of the score."
        ),
        "parameters": {"type": "object", "properties": {
            "report_id": {"type": "integer"},
            "creditor": {"type": "string"},
            "account_last4": {"type": "string"},
            "kind": {"type": "string", "enum": ["credit_card", "loan", "student_loan",
                                                "auto", "mortgage", "medical",
                                                "collections", "other"]},
            "status": {"type": "string", "description": "As the report words it."},
            "balance": {"type": "number"},
            "credit_limit": {"type": "number"},
            "opened_on": {"type": "string"},
            "past_due": {"type": "number"},
            "derogatory": {"type": "string", "description": (
                "e.g. 'collection', 'charge-off', 'late_30'. Leave empty for a clean account.")},
            "derogatory_on": {"type": "string", "description": (
                "Date of first delinquency. This is what decides when it ages off, so "
                "capture it whenever the report shows it.")},
            "notes": {"type": "string"},
        }, "required": ["report_id", "creditor"]},
    }},
    {"type": "function", "function": {
        "name": "suggest_credit_line",
        "description": (
            "Record a card or loan worth considering for HIS situation, with why. This is a "
            "suggestion he reviews, never an application — a hard inquiry and a new account "
            "move his score in both directions, so he decides. Say plainly in `why` which "
            "input it improves and what it costs him."
        ),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": ["card", "loan", "secured_card",
                                                "credit_builder", "other"]},
            "issuer": {"type": "string"},
            "why": {"type": "string", "description": "What it does for his file, specifically."},
            "reward": {"type": "string", "description": "Cashback, points or miles."},
            "annual_fee": {"type": "number"},
            "est_approval": {"type": "string", "enum": ["likely", "borderline", "unlikely"]},
            "priority": {"type": "integer"},
        }, "required": ["name", "why"]},
    }},
    {"type": "function", "function": {
        "name": "update_credit_recommendation",
        "description": (
            "Move a suggested credit line along — he planned it, applied, was approved or "
            "declined, or wants it dismissed. Also how you attach the task once he turns one "
            "into something to do."
        ),
        "parameters": {"type": "object", "properties": {
            "rec_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["suggested", "planned", "applied",
                                                  "approved", "declined", "dismissed"]},
            "task_id": {"type": "integer"},
            "notes": {"type": "string"},
            "priority": {"type": "integer"},
        }, "required": ["rec_id"]},
    }},
    {"type": "function", "function": {
        "name": "record_letter_delivered",
        "description": (
            "Record that USPS confirmed a dispute letter was delivered. This is what starts "
            "the statutory clock for real — the bureau's 30 days run from RECEIPT, not from "
            "posting — so the deadline only becomes defensible once this is set. Call it "
            "whenever tracking shows delivery."
        ),
        "parameters": {"type": "object", "properties": {
            "letter_id": {"type": "integer"},
            "delivered_on": {"type": "string", "description": "Local ISO date."},
        }, "required": ["letter_id", "delivered_on"]},
    }},
    {"type": "function", "function": {
        "name": "record_dispute_response",
        "description": (
            "Record that a bureau answered a dispute, and what it said. Until this is set "
            "the letter reads as unanswered, which is deliberate: a dispute past its window "
            "with no response is leverage, and one that was answered but never recorded "
            "looks exactly the same until somebody checks."
        ),
        "parameters": {"type": "object", "properties": {
            "letter_id": {"type": "integer"},
            "summary": {"type": "string", "description": (
                "What they said — verified, deleted, updated, or rejected as frivolous.")},
            "received_on": {"type": "string", "description": "Local ISO date. Omit for today."},
        }, "required": ["letter_id", "summary"]},
    }},
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
        "name": "import_leonardo_art",
        "description": (
            "Pull the images he has already made in Leonardo.Ai into his design "
            "catalogue, so he does not have to download hundreds of them by hand. Run "
            "with check_only first: that asks his account whether the API key can even "
            "see his web-app work, which Leonardo does not document, and costs nothing. "
            "Importing spends no Leonardo credits either -- credits go on generating, "
            "not on listing or downloading. Safe to run again; anything already in the "
            "catalogue is skipped by its Leonardo id."
        ),
        "parameters": {"type": "object", "properties": {
            "check_only": {"type": "boolean",
                           "description": "Just report what the key can see. Do this "
                                          "first, and tell him the numbers."},
            "max_images": {"type": "integer",
                           "description": "Cap for one run. Default 500."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_mail",
        "description": (
            "The physical post he has photographed and emailed in: who sent it, what it "
            "says, what it wants, how much and by when. Use this whenever he asks about "
            "a letter, a bill, a collection notice, 'what came in the post', or wants to "
            "talk through something he has been sent. Each piece keeps the model's own "
            "confidence and a list of anything it could not read, so say so rather than "
            "presenting a shaky reading as fact. The photo path is included -- the "
            "picture is the record, the reading is a guess."
        ),
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "How many, newest first. Default 25."},
            "kind": {"type": "string",
                     "description": "Narrow to one sort: bill, tax, legal, collection, "
                                    "government, medical, insurance, bank, statement."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_agenda",
        "description": (
            "Everything with a DATE on it, merged into days: his work calendar (the "
            "subscribed Outlook feed), bills and paydays, task due dates, deadlines and "
            "reminders. This is the only tool that can see his meetings — plan_my_day "
            "covers routine and tasks and has no calendar in it at all, and list_reminders "
            "sees only reminders. Call this whenever he asks what is on his agenda, what "
            "he has today or tomorrow, what his week looks like, or whether he is free."
        ),
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer",
                     "description": "How many days forward, including today. 1 = today "
                                    "only, 2 = today and tomorrow. Defaults to 7."},
            "back_days": {"type": "integer",
                          "description": "Days of history to include. Defaults to 0 — "
                                         "only pass this if he asks what he missed."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "plan_my_day",
        "description": (
            "The whole shape of his day: the routine anchors due today and whether each is "
            "done, the personal tasks he has already chosen for today, a short weighted "
            "shortlist to choose from, what is blocked and on what, and which of his "
            "regular habits are slipping. Call this whenever he asks to plan his day, what "
            "he should do, or what is going on today — it is one call and it is exact, so "
            "never assemble this from list_personal_tasks and guesswork."
        ),
        "parameters": {"type": "object", "properties": {
            "track": {"type": "string", "enum": ["personal", "project"],
                      "description": (
                          "Defaults to personal — his own life. Pass 'project' only if he "
                          "explicitly asks about work on building Jarvis itself."
                      )},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "list_day_rhythm",
        "description": (
            "His daily routine: the time-anchored things (wake, feed Ghost, leave for work, "
            "bed) and the things he wants to do a certain number of times a week (bike, sim "
            "racing, seeing Nadia, personal projects). Read this before changing anything, "
            "so you edit the right row rather than adding a duplicate."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "add_day_rhythm",
        "description": (
            "Add something to his routine. Use kind='anchor' for anything with a clock time "
            "('feed Ghost at 7'), and kind='habit' for anything with a rate ('ride the bike "
            "four times a week'). Do this whenever he describes part of his routine rather "
            "than just agreeing with him."
        ),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "What it is, e.g. 'Leave for work'."},
            "kind": {"type": "string", "enum": ["anchor", "habit"]},
            "category": {"type": "string",
                         "enum": ["wake", "work", "care", "health", "relationship",
                                  "project", "fun", "wind_down", "other"]},
            "at_time": {"type": "string", "description": "Local 24h HH:MM. Required for an anchor."},
            "days": {"type": "string", "description": (
                "Comma-separated weekday numbers, Monday=0, e.g. '0,1,2,3,4' for weekdays. "
                "Omit for every day.")},
            "target_per_week": {"type": "integer", "description": "Required for a habit."},
            "lead_minutes": {"type": "integer", "description": (
                "How many minutes before at_time to text him. 0 texts him at the time "
                "itself. Omit entirely to track it without ever chasing him.")},
            "hard": {"type": "boolean", "description": (
                "True for something that cannot slip without real consequence — leaving for "
                "work, medication. These still reach him on a quiet day.")},
            "notes": {"type": "string"},
        }, "required": ["name", "kind"]},
    }},
    {"type": "function", "function": {
        "name": "update_day_rhythm",
        "description": (
            "Change one item of his routine — the time, which days, how much warning, the "
            "weekly target, or turn it off. Use this when he corrects the schedule; the "
            "seeded times were inferred, not given by him, so expect corrections."
        ),
        "parameters": {"type": "object", "properties": {
            "rhythm_id": {"type": "integer"},
            "name": {"type": "string"},
            "category": {"type": "string",
                         "enum": ["wake", "work", "care", "health", "relationship",
                                  "project", "fun", "wind_down", "other"]},
            "at_time": {"type": "string", "description": "Local 24h HH:MM."},
            "days": {"type": "string", "description": "Weekday numbers, Monday=0."},
            "target_per_week": {"type": "integer"},
            "lead_minutes": {"type": "integer"},
            "hard": {"type": "boolean"},
            "notes": {"type": "string"},
            "enabled": {"type": "boolean", "description": "False stops it without deleting the history."},
        }, "required": ["rhythm_id"]},
    }},
    {"type": "function", "function": {
        "name": "log_day_rhythm",
        "description": (
            "Record that something in his routine happened, or that he deliberately skipped "
            "it. Call this when he mentions doing one of them — 'fed Ghost', 'got my ride "
            "in', 'skipped the bike tonight'. A skip is a real answer worth recording, not "
            "the same as silence, and it stops him being nudged again today."
        ),
        "parameters": {"type": "object", "properties": {
            "rhythm_id": {"type": "integer"},
            "state": {"type": "string", "enum": ["done", "skipped"]},
            "on_date": {"type": "string", "description": "Local ISO date. Omit for today."},
            "note": {"type": "string", "description": "Why, if he said — 'was at Nadia's'."},
        }, "required": ["rhythm_id"]},
    }},
    {"type": "function", "function": {
        "name": "pick_task_for_today",
        "description": (
            "Commit a personal task to today, which is what 'I'll do X today' means. "
            "Different from marking it started: this is the choosing step, and it is per "
            "day, so not getting to it shows up honestly rather than carrying forward."
        ),
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"},
            "on_date": {"type": "string", "description": "Local ISO date. Omit for today."},
        }, "required": ["task_id"]},
    }},
    {"type": "function", "function": {
        "name": "unpick_task_for_today",
        "description": "Take a task back off today's plan — he changed his mind or ran out of day.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"},
            "on_date": {"type": "string"},
        }, "required": ["task_id"]},
    }},
    {"type": "function", "function": {
        "name": "add_task_detail",
        "description": (
            "Attach the information needed to actually DO a task — a phone number, an "
            "address, whose name to ask for, a link, or a note. Add these whenever he "
            "mentions one in passing, because the alternative is him hunting for the vet's "
            "number when he is already standing outside. These also ride along into the "
            "calendar event on his phone when a reminder references the task."
        ),
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"},
            "kind": {"type": "string", "enum": ["phone", "address", "person", "link", "note"]},
            "value": {"type": "string", "description": (
                "Write a phone number exactly as he says it — iOS makes it tappable itself, "
                "and reformatting is how that stops working.")},
            "label": {"type": "string", "description": "Optional, e.g. 'Old vet in Asheville'."},
        }, "required": ["task_id", "kind", "value"]},
    }},
    {"type": "function", "function": {
        "name": "block_task",
        "description": (
            "Record that one task cannot be started until another is finished — 'I can't "
            "book the trip until Ghost has a vet and a boarding place'. A blocked task is "
            "held out of his daily shortlist and shown as waiting, and it frees itself "
            "automatically when the blocker is done. Use it whenever he describes an order "
            "things have to happen in."
        ),
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer", "description": "The task that has to wait."},
            "blocked_by_id": {"type": "integer", "description": "The task it is waiting on."},
        }, "required": ["task_id", "blocked_by_id"]},
    }},
    {"type": "function", "function": {
        "name": "unblock_task",
        "description": "Remove a dependency between two tasks — it turned out not to matter.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"},
            "blocked_by_id": {"type": "integer"},
        }, "required": ["task_id", "blocked_by_id"]},
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

    # --- debts ------------------------------------------------------------------------
    # He has no written list of his debts and isn't going to make one: "most of the debt is
    # in there [his email] and i dont have it written down, also when text messages come in
    # ill let jarvis kow and he can add them as well". These tools ARE that second channel.
    # A balance that arrived by text, a figure he read off a statement, a collections call
    # he took -- he says it, and it lands against the right creditor.
    {"type": "function", "function": {
        "name": "list_debts",
        "description": (
            "List the debts he's tracking — creditor, current balance and when it was last "
            "observed, rate, minimum payment, his payoff priority, and where each number "
            "came from. Balances are dated observations, so 'current' always means 'as of' "
            "a date. Use this before recording a balance so you know which accounts exist."
        ),
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["active", "paid_off", "in_dispute", "closed"]},
            "include_proposed": {
                "type": "boolean",
                "description": (
                    "Also include debts found in his email that he hasn't confirmed yet. "
                    "Default false — unconfirmed finds are guesses and are not part of his "
                    "debt picture. Never present a proposed debt as one he owes."
                ),
            },
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "record_debt_balance",
        "description": (
            "Record a debt balance he just told you — the main way debt gets into the "
            "tracker apart from the email sweep. Use it whenever he states a balance, "
            "however casually ('got a text from Capital One, balance is $4,200', 'the "
            "Navient loan is down to 18k'). It matches an existing debt where one "
            "plausibly matches, creates one where it doesn't, and — this matters — returns "
            "needs_disambiguation instead of guessing when he has more than one account "
            "with that creditor and hasn't said which. When that happens, ASK him which "
            "account he means and call this again with debt_id; do not pick one. Record "
            "only figures he actually states: never estimate a balance, and never carry a "
            "number over from an older statement as if he'd just said it."
        ),
        "parameters": {"type": "object", "properties": {
            "creditor": {"type": "string", "description": "Who the money is owed to, e.g. 'Capital One'."},
            "balance": {"type": "number", "description": "The outstanding balance owed, as a plain number."},
            "balance_text": {
                "type": "string",
                "description": (
                    "Use INSTEAD of balance when what he said isn't one clear number "
                    "('somewhere between 800 and 1200'). Stored as his words, with no "
                    "figure invented from it."
                ),
            },
            "debt_id": {"type": "integer", "description": "Target a specific tracked debt — use after a needs_disambiguation answer."},
            "account_last4": {"type": "string", "description": "Last 4 of the account only, if he says it. Never a full account number."},
            "apr": {"type": "number", "description": "Interest rate as a percentage, e.g. 24.99 — only if he states it."},
            "minimum_payment": {"type": "number"},
            "kind": {
                "type": "string",
                "enum": ["credit_card", "loan", "student_loan", "auto", "mortgage", "medical", "collections", "other"],
                "description": "Only used when this creates a new debt.",
            },
            "observed_on": {"type": "string", "description": "Local ISO date the figure was true; defaults to today."},
            "notes": {"type": "string", "description": "Anything he said about it that the numbers don't capture."},
        }, "required": ["creditor"]},
    }},
    {"type": "function", "function": {
        "name": "update_debt",
        "description": (
            "Correct or update a tracked debt's details — the creditor's name, the account "
            "ending, what kind of debt it is, its due day, notes, or its status (mark it "
            "paid_off when he says it's cleared, in_dispute when he's disputing it, closed "
            "when the account is gone). This never changes a balance: a balance is a dated "
            "observation, so record a new one with record_debt_balance instead — that's "
            "what makes the trend real."
        ),
        "parameters": {"type": "object", "properties": {
            "debt_id": {"type": "integer"},
            "creditor": {"type": "string"},
            "account_last4": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": ["credit_card", "loan", "student_loan", "auto", "mortgage", "medical", "collections", "other"],
            },
            "status": {"type": "string", "enum": ["active", "paid_off", "in_dispute", "closed"]},
            "due_day": {"type": "integer", "description": "Day of the month the payment is due, 1-31."},
            "notes": {"type": "string"},
        }, "required": ["debt_id"]},
    }},
    {"type": "function", "function": {
        "name": "set_debt_priority",
        "description": (
            "Set HIS payoff priority for one debt (1 = pay this off first), or clear it "
            "with priority 0. Only ever call this for an order he actually states. He was "
            "explicit that assigning payoff priorities is a joint, ongoing effort, so "
            "suggest and discuss freely — get_debt_summary gives you both standard "
            "strategies ranked — but never assign an order yourself and never present a "
            "suggestion as a decided plan."
        ),
        "parameters": {"type": "object", "properties": {
            "debt_id": {"type": "integer"},
            "priority": {"type": "integer", "description": "1 = first. 0 clears the priority."},
        }, "required": ["debt_id", "priority"]},
    }},
    {"type": "function", "function": {
        "name": "get_debt_summary",
        "description": (
            "The whole debt picture: total owed, total minimum payments, estimated monthly "
            "interest, which debt is costing the most, how many balances aren't known, and "
            "BOTH standard payoff orderings (avalanche = highest rate first, cheapest "
            "overall; snowball = smallest balance first, easiest to sustain) as "
            "suggestions. Present them as options for him to pick between, never as an "
            "assigned plan. Say plainly that the total is a floor when unknown_balance_count "
            "is above zero — he's assembling this picture precisely because he doesn't know it."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "list_debt_proposals",
        "description": (
            "Debts found in his email history by the mail sweep that he hasn't confirmed "
            "yet. These are guesses, not his debts — they're excluded from every total "
            "until he confirms one. Walk him through them when he asks what the scanner "
            "found."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "resolve_debt_proposal",
        "description": (
            "Record his verdict on a debt found in his email: confirm it (it becomes a "
            "tracked debt, keeping every balance observation the sweep found for it) or "
            "dismiss it (it's wrong, or not his). Only ever call this on an explicit answer "
            "from him — confirming a guess on his behalf puts money in his debt picture "
            "that he never agreed was there."
        ),
        "parameters": {"type": "object", "properties": {
            "debt_id": {"type": "integer"},
            "verdict": {"type": "string", "enum": ["confirm", "dismiss"]},
            "note": {"type": "string", "description": "Anything he said about why."},
        }, "required": ["debt_id", "verdict"]},
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


# The dispute tools a credit employee may hold. Record-keeping only, and the exclusions
# are the point: draft_dispute_letter, record_dispute_letter_mailed and track_dispute_letter
# all reach LetterStream, which prices and queues real postage. Opening a tracking row is
# bookkeeping; putting a letter in the mail is an outward act that spends the owner's money
# and starts a legal clock, and that stays his to trigger.
#
# Without these the specialist can only ever describe a dispute queue it has no way to
# fill, so "disputes in flight" would read as "none" forever while it kept recommending
# the same items every run.
CREDIT_TRACKING_TOOL_NAMES = (
    "list_dispute_items", "create_dispute_item", "update_dispute_item",
    "record_dispute_response", "get_credit_picture", "list_credit_scores",
)

CREDIT_TRACKING_TOOLS = [t for t in PERSONAL_TOOLS
                         if t["function"]["name"] in CREDIT_TRACKING_TOOL_NAMES]


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
    " You also run his day, and this is the part he leans on most -- he has ADHD, and the "
    "whole point is that he should not have to hold the shape of a day in his head."
    " A TASK YOU RAISED FROM A PHOTOGRAPHED LETTER IS A STARTING POINT, NOT AN "
    "INSTRUCTION. It was written from the letter alone, and a letter only ever tells "
    "you the sender's side. When he adds something the paper cannot know -- he moved, "
    "it is already paid, that is not his account, the amount is wrong, the car was sold "
    "-- the right action can invert completely, from 'pay this' to 'do not pay this "
    "yet'. That happened on a Fauquier County trailer tax: the bill said pay $2,662.12, "
    "he said he and the trailers left the county in 2021, and paying became the wrong "
    "move. When it happens, FIX THE TASK with update_personal_task rather than leaving "
    "a wrong instruction on his list and explaining the truth only in chat -- he will "
    "read the list later and not the conversation. "
    " DO NOT SIMPLY AGREE WITH HIM. Check whether his reasoning actually holds before "
    "telling him he is right; you have web search, so use it on anything official. His "
    "Fauquier premise was 'I moved away, so how can they charge me' -- and that alone "
    "does not win, because Virginia taxes a vehicle by where it is GARAGED and keeps "
    "assessing until someone tells the Commissioner of the Revenue it left. The bill "
    "was procedurally right and substantively wrong, which is a different argument and "
    "the one that works. Agreeing with him would have cost him the case; explaining the "
    "mechanism gave him a winnable one. Find the actual rule, name it, and say what it "
    "means for him. "
    " Then finish the job: if there is a written process, say who to write to and by "
    "when, offer to draft the letter, and put the deadline on the task. He would rather "
    "not use the phone, so treat a phone number as the fallback and never the plan. "
    "When he asks about a letter, a bill, a collection notice or what came in the "
    "post, call get_mail -- he photographs his post and emails it in, and that tool is "
    "the only thing that can see it. Respect the confidence on each piece: a reading "
    "off a phone photo is a guess, and a wrong figure in his finances is worse than "
    "saying you are unsure. "
    "When he asks what is ON his agenda -- today, tomorrow, this week, or whether he is free -- "
    "call get_agenda. It is the only tool that can see his work calendar; plan_my_day has no "
    "calendar in it and list_reminders sees only reminders, so answering from either of those "
    "tells him he has nothing on a day with two meetings in it. When he "
    "asks what to do, or to plan his day, call plan_my_day: it returns his "
    "routine anchors, what he already chose, a short weighted shortlist, what is blocked and "
    "on what, and which habits are slipping -- one exact call, so never assemble that from "
    "list_personal_tasks and guesswork. When he settles on something, pick_task_for_today, "
    "so the plan is a commitment rather than a conversation. Offer at most two or three; a "
    "longer list is one he abandons."
    " His routine is yours to maintain by voice. list_day_rhythm first, then add_day_rhythm "
    "or update_day_rhythm -- anchors for anything with a clock time, habits for anything with "
    "a weekly rate. The times currently in there were INFERRED from a wake reminder and the "
    "distance to his office, not given by him, so treat every correction as expected and just "
    "make it. When he mentions doing one of these -- fed Ghost, got his ride in, skipped the "
    "bike -- log_day_rhythm it, including skips: a skip is a real answer and it stops him "
    "being nudged again that day."
    " Whenever he mentions a phone number, address or person attached to something he has to "
    "do, attach it with add_task_detail instead of only repeating it back. Those details reach "
    "his phone through the calendar event, and the alternative is him hunting for the vet's "
    "number while standing outside. When he describes an order things must happen in -- he "
    "cannot book the trip until Ghost has a vet and a boarding place -- record it with "
    "block_task, which keeps the blocked task out of his shortlist until it is genuinely "
    "actionable and frees it automatically."
    " Keep his own life and the work of building Jarvis apart: personal tasks are errands, "
    "appointments, Ghost, the car; project tasks are what is needed to make the system. Pass "
    "track='project' only for the latter. Planning a day only ever considers personal ones, "
    "because ranking wake-word arbitration against finding a vet makes both lists useless."
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
    " You also keep his DEBT tracker, and you should understand why it works the way it "
    "does: he has no written list of his debts and isn't going to make one — most of it "
    "sits in his email, and the rest arrives as texts and physical mail. So the picture "
    "gets assembled. The email sweep proposes debts it finds in his mail history and you "
    "walk him through them (list_debt_proposals, resolve_debt_proposal); anything he tells "
    "you directly goes straight in with record_debt_balance. Use that tool any time he "
    "states a balance, however offhand — that is the whole second channel. If it comes "
    "back needs_disambiguation he has more than one account with that creditor: ask him "
    "which, never guess, because attaching one card's balance to another card's history "
    "corrupts both and can't be spotted afterwards. Balances are dated observations, never "
    "overwritten, so always say what a number is 'as of' and never present an old balance "
    "as current. Never estimate a balance, a rate or a payoff figure he hasn't given you. "
    "Payoff priority is HIS: get_debt_summary ranks both standard strategies for you to "
    "put to him, but only set_debt_priority — on an order he actually states — decides "
    "anything. He confirmed assigning priorities is a joint, ongoing effort, so keep "
    "raising it, and keep it a conversation rather than a plan you hand him."
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

    def __init__(self, db_path: str, owner_user_id: int, letterstream=None, kroger=None,
                 tz_name: str = "America/New_York", leonardo_api_key: str | None = None,
                 generated_media_path: str = "generated"):
        self.db_path = db_path
        self.owner_user_id = owner_user_id
        # A routine is lived in local time. Without this, "log that I fed Ghost" at 8pm
        # would file against tomorrow's date for most of the evening.
        self.tz_name = tz_name
        self.letterstream = letterstream
        # Raw kroger.mcp_client, same loose-coupling convention as letterstream above --
        # only kitchen_tools.sync_kroger_purchases actually calls it (see dispatch below).
        self.kroger = kroger
        # For importing the images he has already made in Leonardo rather than
        # downloading hundreds of them by hand.
        self.leonardo_api_key = leonardo_api_key
        self.generated_media_path = generated_media_path

    def _today(self) -> str:
        from . import routine
        return routine._now_local(self.tz_name).date().isoformat()

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
                arguments.get("priority", "normal"), arguments.get("due_at"),
                track=arguments.get("track"))
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

        if name == "get_credit_picture":
            from . import credit
            return credit.picture(db_path, owner)
        if name == "add_credit_report":
            try:
                report_id = personal_db.add_credit_report(
                    db_path, owner, arguments["bureau"], arguments["pulled_on"],
                    score=arguments.get("score"), score_model=arguments.get("score_model"),
                    source=arguments.get("source"), notes=arguments.get("notes"))
            except ValueError as e:
                return {"error": str(e)}
            return {"ok": True, "report_id": report_id}
        if name == "add_tradeline":
            fields = {k: v for k, v in arguments.items()
                      if k not in ("report_id", "creditor")}
            try:
                tradeline_id = personal_db.add_tradeline(
                    db_path, arguments["report_id"], arguments["creditor"], **fields)
            except ValueError as e:
                return {"error": str(e)}
            return {"ok": True, "tradeline_id": tradeline_id}
        if name == "suggest_credit_line":
            rec_id = personal_db.add_credit_recommendation(
                db_path, owner, arguments["name"], arguments["why"],
                kind=arguments.get("kind") or "card",
                issuer=arguments.get("issuer"), reward=arguments.get("reward"),
                annual_fee=arguments.get("annual_fee"),
                est_approval=arguments.get("est_approval"),
                priority=arguments.get("priority"))
            return {"ok": True, "recommendation_id": rec_id}
        if name == "update_credit_recommendation":
            fields = {k: v for k, v in arguments.items() if k != "rec_id"}
            ok = personal_db.update_credit_recommendation(
                db_path, owner, arguments["rec_id"], **fields)
            return {"ok": ok} if ok else {"error": "no such recommendation, or nothing to change"}
        if name == "record_letter_delivered":
            from . import credit
            deadline = credit.response_deadline(None, arguments["delivered_on"])
            ok = personal_db.record_letter_delivered(
                db_path, owner, arguments["letter_id"], arguments["delivered_on"],
                response_due_at=deadline["due_on"] if deadline else None)
            if not ok:
                return {"error": "no such letter"}
            return {"ok": True, "response_due_at": deadline["due_on"] if deadline else None,
                    "note": "The bureau's 30 days now run from this date."}
        if name == "record_dispute_response":
            ok = personal_db.record_dispute_response(
                db_path, owner, arguments["letter_id"], arguments["summary"],
                received_on=arguments.get("received_on"))
            return {"ok": ok} if ok else {"error": "no such letter"}

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

        if name == "import_leonardo_art":
            from . import leonardo

            key = getattr(self, "leonardo_api_key", None)
            if not key:
                return {"error": "No Leonardo API key is configured. He needs to make "
                                 "one on Leonardo's API Access page and put it in "
                                 "config.json as leonardo_api_key. Note the API is "
                                 "billed separately from his web subscription."}
            client = leonardo.LeonardoClient(key)
            if arguments.get("check_only"):
                return leonardo.probe(client)
            return leonardo.import_generations(
                db_path, owner, client, self.generated_media_path,
                max_images=int(arguments.get("max_images") or 500))

        if name == "get_mail":
            # The picture is the record and the reading is a guess -- both go to the
            # model, so it can hedge where the reading hedged instead of repeating a
            # number off a phone photo as though it came from a bank.
            from . import mail_photo

            pieces = mail_photo.recent(db_path, owner, limit=int(arguments.get("limit") or 25))
            wanted = (arguments.get("kind") or "").strip().lower()
            if wanted:
                pieces = [p for p in pieces if (p.get("kind") or "") == wanted]
            return {"mail": [{
                "id": p["id"], "received": p["at"], "sender": p["sender"],
                "kind": p["kind"], "summary": p["summary"], "amount": p["amount"],
                "due_date": p["due_date"], "account_ref": p["account_ref"],
                "action": p["action"], "confidence": p["confidence"],
                "could_not_read": p["unreadable"], "photo": p["photo_path"],
                "task_id": p["task_id"], "status": p["status"],
            } for p in pieces]}
        if name == "get_agenda":
            # The same call the Agenda screen makes, so chat and the screen can never
            # disagree about what day he is having. It was already merging the work
            # calendar correctly; there was simply no tool to reach it from a
            # conversation, so "what's on my agenda today" could only ever find the
            # reminder about a concert.
            from . import agenda
            return agenda.upcoming(
                db_path, owner,
                days=max(1, int(arguments.get("days") or 7)),
                back_days=max(0, int(arguments.get("back_days") or 0)))
        if name == "plan_my_day":
            from . import routine
            return routine.plan_day(db_path, owner, tz_name=self.tz_name,
                                    track=arguments.get("track") or "personal")
        if name == "list_day_rhythm":
            return {"rhythm": personal_db.list_rhythm(db_path, owner, include_disabled=True)}
        if name == "add_day_rhythm":
            try:
                rhythm_id = personal_db.add_rhythm(
                    db_path, owner, arguments["name"], arguments["kind"],
                    category=arguments.get("category") or "other",
                    at_time=arguments.get("at_time"), days=arguments.get("days"),
                    target_per_week=arguments.get("target_per_week"),
                    lead_minutes=arguments.get("lead_minutes"),
                    hard=bool(arguments.get("hard")), notes=arguments.get("notes"))
            except ValueError as e:
                return {"error": str(e)}
            return {"ok": True, "rhythm_id": rhythm_id}
        if name == "update_day_rhythm":
            fields = {k: v for k, v in arguments.items() if k != "rhythm_id"}
            try:
                ok = personal_db.update_rhythm(db_path, owner, arguments["rhythm_id"], **fields)
            except ValueError as e:
                return {"error": str(e)}
            return {"ok": ok} if ok else {"error": "no such routine item, or nothing to change"}
        if name == "log_day_rhythm":
            on_date = arguments.get("on_date") or self._today()
            mine = {r["id"] for r in personal_db.list_rhythm(db_path, owner, include_disabled=True)}
            if arguments["rhythm_id"] not in mine:
                return {"error": "no such routine item"}
            personal_db.log_rhythm(db_path, arguments["rhythm_id"], on_date,
                                   arguments.get("state") or "done",
                                   note=arguments.get("note"), source="chat")
            return {"ok": True, "on_date": on_date}

        if name == "pick_task_for_today":
            on_date = arguments.get("on_date") or self._today()
            added = personal_db.pick_for_day(db_path, owner, arguments["task_id"], on_date)
            return {"ok": True, "added": added, "on_date": on_date}
        if name == "unpick_task_for_today":
            on_date = arguments.get("on_date") or self._today()
            return {"ok": personal_db.unpick_for_day(db_path, owner, arguments["task_id"], on_date)}

        if name == "add_task_detail":
            try:
                detail_id = personal_db.add_task_detail(
                    db_path, arguments["task_id"], arguments["kind"], arguments["value"],
                    label=arguments.get("label"))
            except ValueError as e:
                return {"error": str(e)}
            return {"ok": True, "detail_id": detail_id}
        if name == "block_task":
            try:
                added = personal_db.block_task(db_path, arguments["task_id"],
                                               arguments["blocked_by_id"])
            except ValueError as e:
                return {"error": str(e)}
            return {"ok": True, "added": added}
        if name == "unblock_task":
            return {"ok": personal_db.unblock_task(db_path, arguments["task_id"],
                                                   arguments["blocked_by_id"])}

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

        if name == "list_debts":
            include_proposed = bool(arguments.get("include_proposed"))
            return {"debts": personal_db.list_debts(
                db_path, owner, tracking_state=None if include_proposed else "tracked",
                status=arguments.get("status"))}
        if name == "record_debt_balance":
            return self._record_debt_balance(arguments)
        if name == "update_debt":
            return self._update_debt(arguments)
        if name == "set_debt_priority":
            # 0 clears rather than ranking something first -- there is no "priority zero",
            # and a tool schema can't express "an integer or null".
            priority = arguments.get("priority")
            ok = personal_db.set_debt_priority(
                db_path, owner, arguments["debt_id"], None if not priority else int(priority))
            return {"ok": ok} if ok else {"error": "debt not found"}
        if name == "get_debt_summary":
            return personal_db.debt_summary(db_path, owner)
        if name == "list_debt_proposals":
            proposals = personal_db.list_debts(db_path, owner, tracking_state="proposed")
            return {
                "proposals": proposals,
                "note": (
                    "These were read out of his email and he has NOT confirmed them. They "
                    "are excluded from every debt total until he does. Do not present them "
                    "as debts he owes."
                ),
            }
        if name == "resolve_debt_proposal":
            return self._resolve_debt_proposal(arguments)

        if name in _KITCHEN_TOOL_NAMES:
            return kitchen_tools.dispatch(db_path, owner, name, arguments, kroger_mcp_client=self.kroger)

        return {"error": f"unknown personal tool {name}"}

    # --- debts ---------------------------------------------------------------------

    @staticmethod
    def _stated_amount(arguments: dict, number_key: str, text_key: str | None = None) -> tuple:
        """(text, value) for a figure he stated. A plain number keeps a rendered text form
        alongside it; free text he gave instead ("between 800 and 1200") is stored as his
        words with the numeric column left NULL rather than having a figure invented from
        it -- the same paired text/typed discipline mail_bills.py established."""
        raw_text = str(arguments.get(text_key) or "").strip() if text_key else ""
        value = arguments.get(number_key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = None
        if value is not None:
            return (raw_text or f"{float(value):,.2f}"), float(value)
        return raw_text, None

    def _record_debt_balance(self, arguments: dict) -> dict:
        db_path, owner = self.db_path, self.owner_user_id
        creditor = str(arguments.get("creditor") or "").strip()
        account_last4 = personal_db.sanitize_account_last4(arguments.get("account_last4"))
        balance_text, balance = self._stated_amount(arguments, "balance", "balance_text")
        apr_text, apr = self._stated_amount(arguments, "apr")
        if apr is not None:
            apr_text = f"{apr}%"
        minimum_text, minimum = self._stated_amount(arguments, "minimum_payment")

        if not creditor and arguments.get("debt_id") is None:
            return {"error": "creditor is required unless a debt_id is given"}
        if not balance_text and minimum is None:
            return {"error": (
                "nothing to record — give the balance he actually stated, or use "
                "balance_text for wording that isn't a single number"
            )}

        kind = arguments.get("kind") or "credit_card"
        if kind not in personal_db.DEBT_KINDS:
            return {"error": f"kind must be one of {sorted(personal_db.DEBT_KINDS)}"}

        debt_id = arguments.get("debt_id")
        created = False
        if debt_id is not None:
            if personal_db.get_debt(db_path, owner, debt_id) is None:
                return {"error": "debt not found"}
        else:
            match = personal_db.find_matching_debt(db_path, owner, creditor, account_last4)
            if match["ambiguous"]:
                # The one case where guessing does damage that can't be spotted later, so
                # nothing is written and the question goes back to him.
                candidates = [
                    {"debt_id": d["id"], "creditor": d["creditor"],
                     "account_last4": d["account_last4"],
                     "current_balance": d["current_balance"],
                     "current_balance_observed_on": d["current_balance_observed_on"]}
                    for d in personal_db.list_debts(db_path, owner, tracking_state=None)
                    if d["creditor_key"] == personal_db.normalize_creditor(creditor)
                ]
                return {
                    "needs_disambiguation": True, "reason": match["reason"],
                    "candidates": candidates,
                    "message": (
                        "Nothing recorded. Ask him which of these accounts he means and "
                        "call record_debt_balance again with that debt_id — do not pick one."
                    ),
                }
            debt_id = match["debt_id"]
            if debt_id is None:
                # He told Jarvis about it directly, so it is tracked from the start: a
                # thing he said is not a guess awaiting his confirmation.
                debt_id = personal_db.create_debt(
                    db_path, owner, creditor, account_last4=account_last4, kind=kind,
                    tracking_state="tracked", origin="chat",
                    origin_detail="he told Jarvis directly", notes=arguments.get("notes"))
                created = True
            elif match.get("learned_account_last4"):
                personal_db.update_debt(
                    db_path, owner, debt_id, account_last4=match["learned_account_last4"])

        observation_id = personal_db.add_debt_observation(
            db_path, debt_id, observed_on=arguments.get("observed_on"),
            balance_text=balance_text, balance=balance, apr_text=apr_text, apr=apr,
            minimum_payment_text=minimum_text, minimum_payment=minimum,
            source="chat", source_detail="he told Jarvis directly",
            confirmed=True, notes=arguments.get("notes"),
        )
        return {
            "ok": True, "debt_id": debt_id, "created_debt": created,
            "observation_id": observation_id, "match_reason": None if created else "matched an existing debt",
            "debt": personal_db.get_debt(db_path, owner, debt_id),
        }

    def _update_debt(self, arguments: dict) -> dict:
        fields = {k: arguments.get(k) for k in
                  ("creditor", "account_last4", "kind", "status", "due_day", "notes")}
        try:
            ok = personal_db.update_debt(self.db_path, self.owner_user_id, arguments["debt_id"], **fields)
        except ValueError as exc:
            return {"error": str(exc)}
        if not ok:
            return {"error": "debt not found, or nothing to change"}
        return {"ok": True, "debt": personal_db.get_debt(self.db_path, self.owner_user_id, arguments["debt_id"])}

    def _resolve_debt_proposal(self, arguments: dict) -> dict:
        db_path, owner = self.db_path, self.owner_user_id
        debt_id = arguments["debt_id"]
        verdict = arguments.get("verdict")
        if verdict not in ("confirm", "dismiss"):
            return {"error": "verdict must be 'confirm' or 'dismiss'"}
        debt = personal_db.get_debt(db_path, owner, debt_id)
        if debt is None:
            return {"error": "debt not found"}
        if debt["tracking_state"] != "proposed":
            return {"error": f"that debt is already {debt['tracking_state']}, not awaiting a verdict"}

        state = "tracked" if verdict == "confirm" else "dismissed"
        personal_db.update_debt(db_path, owner, debt_id, tracking_state=state)
        # Clear the card too, or the same question sits on the Review page forever -- the
        # exact dangling-card problem get_review_item_by_ref exists to solve.
        try:
            card = business_db.get_review_item_by_ref(db_path, owner, "debts", debt_id)
            if card is not None and card["status"] == "pending":
                business_db.decide_review_item(
                    db_path, owner, card["id"],
                    "approved" if verdict == "confirm" else "rejected",
                    note=arguments.get("note"))
        except Exception:
            logger.exception("failed to close the review card for debt %s", debt_id)
        return {"ok": True, "debt_id": debt_id, "tracking_state": state,
                "debt": personal_db.get_debt(db_path, owner, debt_id)}

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
