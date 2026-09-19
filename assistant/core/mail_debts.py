"""The historical debt sweep: assembling the owner's debt picture out of his mail history.

WHY THIS IS NOT mail_bills.py
-----------------------------
mail_bills.py looks FORWARD. It reads the most recent messages in the inbox and asks "is
this a bill". That is the right shape for a bill, which is a thing that arrives and then
becomes due. It is the wrong shape for a debt, for two independent reasons, and this
module inverts both.

1. DIRECTION. Asked to list his debts so payoff priorities could be assigned, the owner
   said he wasn't going to, because he can't:

       "we will add the actual debt once the email scanner is woring most of the debt is
        in there and i dont have it written down, also when text messages come in ill let
        jarvis kow and he can add them as well. In assition once i get the scanner to
        email configured, there will be physical main [mail] with debt that needs tobe
        tracked"

   The debt is in his HISTORY, not in his recent mail. A forward-looking pass will never
   surface it. So this one runs backwards through every folder, bounded and resumable,
   making progress through a backlog over many runs rather than needing one heroic pass.

2. WHAT IT IS LOOKING FOR. A bill is an amount due by a date. A debt is a balance owed,
   usually carrying an interest rate. The existing bill detector skims straight past a
   credit-card statement (nothing is "due" in the sense it means) or mis-files it as a
   one-off bill, losing the balance and the APR -- which are the only two numbers a payoff
   effort actually needs. So this asks its own question: creditor, balance, APR, minimum
   payment. See DEBT_SYSTEM, where telling the two apart is most of the prompt.

Scanned physical mail needs no special handling here and deliberately gets none: his
Epson scans to email, which lands in the same mailbox, so it flows through this same path
the moment he configures it.

NARROWING BEFORE CLASSIFYING
----------------------------
His mailbox is large -- tens of thousands of messages. LLM-classifying all of it is not
affordable and never will be, so every run shortlists first with IMAP SEARCH (server-side,
no message bodies transferred) and only classifies the shortlist. Two kinds of term, which
catch different things: phrases that appear in the body of a statement ("minimum payment",
"account ending"), and the sending domains of real creditors and loan servicers, which
catch a statement whose wording nothing anticipated. See SEARCH_TERMS.

BOUNDED AND RESUMABLE
---------------------
Each run: shortlist per folder, subtract everything already in the email_debt_scans
ledger, classify up to a per-run cap, stop. Every message is judged exactly once ever, so
running it again continues the backlog instead of redoing the front of it, and stopping it
mid-run loses nothing but the current message. A message that couldn't be read or that
blew up mid-classification is deliberately left OUT of the ledger so a later run retries
it -- in a one-time pass over his history, "burnt in the ledger" means "invisible forever".

PROPOSE, DON'T CREATE
---------------------
Nothing here puts a debt into his debt picture. A detection becomes a debts row with
tracking_state='proposed', which every read that totals or ranks his money filters out,
plus a review card asking him to confirm. He was explicit that assigning payoff priorities
is a joint, ongoing effort, so this also never writes debts.priority -- it surfaces and
suggests, he decides.

DEDUPLICATION
-------------
Twelve monthly statements from one card must converge on ONE debt with twelve balance
observations. That works because a proposal is a real row that the NEXT statement matches
against (personal_db.match_debt searches proposed rows too), so statement #2 attaches an
observation to statement #1's proposal instead of proposing a second debt. Three further
guards sit behind it: the scan ledger (one judgement per message), the observation
source-ref index (one observation per source message), and has_equivalent_observation (the
same statement filed in two folders is two uids but one fact). Where matching is genuinely
ambiguous -- two cards from the same creditor and no account number in the message -- it
refuses to guess and asks him instead, because a wrong merge silently folds two real debts
into one.

This module READS and RECORDS only. No code path here can archive, delete, move, mark
read, or send: it calls list_folders, search_uids and read_message, all read-only. It
creates no reminder and moves no money, and like mail_bills.py it never writes
manual_recurring_charges, which the owner's budget projections key off.
"""
import json
import logging
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from . import business_db, mail_db, personal_db

logger = logging.getLogger(__name__)

# How many messages one run will actually classify. Small on purpose: this is a backlog
# pass, not a race, and the cost of a bigger number is real LLM spend against a mailbox
# with tens of thousands of messages in it. Progress is made by running it often.
DEFAULT_PER_RUN_LIMIT = 15
# How many candidate UIDs to pull back per folder per run. Bounds the IMAP side
# independently of the LLM side -- a folder whose shortlist runs to thousands still
# returns a bounded list, newest-first, and the ledger carries the rest to the next run.
DEFAULT_SHORTLIST_LIMIT = 400
# Body characters handed to the model. Statements bury the balance in a long HTML table,
# so this is deliberately more generous than mail_bills' 3000.
BODY_CHARS = 4000

# Phrases that appear in a real statement, collections notice or loan servicer letter.
# Multi-word on purpose: IMAP TEXT matches substrings, so "minimum payment" is enormously
# more precise than "balance" on its own, which every marketing email in the world says.
DEBT_PHRASE_TERMS = [
    ("TEXT", "minimum payment"),
    ("TEXT", "minimum payment due"),
    ("TEXT", "amount due"),
    ("TEXT", "past due"),
    ("TEXT", "current balance"),
    ("TEXT", "outstanding balance"),
    ("TEXT", "statement balance"),
    ("TEXT", "principal balance"),
    ("TEXT", "remaining balance"),
    ("TEXT", "account ending"),
    ("TEXT", "annual percentage rate"),
    ("TEXT", "interest charged"),
    ("TEXT", "collection agency"),
    ("TEXT", "debt collector"),
    ("TEXT", "charged off"),
    ("TEXT", "payoff amount"),
    ("SUBJECT", "statement"),
    ("SUBJECT", "your statement is ready"),
    ("SUBJECT", "autopay"),
    ("SUBJECT", "past due"),
]

# Real card issuers, banks, student-loan servicers, BNPL lenders and collection agencies.
# These catch the statement whose wording nothing above anticipated -- which is most of
# the value, since every issuer phrases its emails differently and a sender domain doesn't
# change. Bounded deliberately: each entry is one IMAP SEARCH per folder per run.
CREDITOR_SENDER_DOMAINS = [
    "capitalone.com", "chase.com", "discover.com", "americanexpress.com", "aexp.com",
    "citi.com", "citibank.com", "synchronybank.com", "synchrony.com", "bankofamerica.com",
    "wellsfargo.com", "usbank.com", "barclaycardus.com", "comenity.net", "bread.com",
    "creditonebank.com", "firstpremier.com", "milestonecard.com", "mercury.com",
    "navient.com", "nelnet.com", "mohela.com", "aidvantage.com", "studentaid.gov",
    "greatlakes.org", "edfinancial.com", "salliemae.com",
    "ally.com", "sofi.com", "upstart.com", "lendingclub.com", "onemainfinancial.com",
    "upgrade.com", "marcus.com", "prosper.com", "avant.com", "oportun.com",
    "affirm.com", "klarna.com", "afterpay.com",
    "portfoliorecovery.com", "midlandcredit.com", "ncogroup.com", "enhancedrecovery.com",
]

SEARCH_TERMS = DEBT_PHRASE_TERMS + [("FROM", domain) for domain in CREDITOR_SENDER_DOMAINS]

# Folders a debt statement is never usefully found in. Junk and Trash are excluded for the
# same reason mail_bills skips junk-logged mail -- a scam invoice reads exactly like a
# debt. Sent and Drafts are his own outgoing words, not a creditor's. \Noselect entries are
# containers, not mailboxes, and selecting one is an IMAP error.
SKIP_FOLDER_FLAGS = ("\\Junk", "\\Trash", "\\Drafts", "\\Sent", "\\Noselect", "\\All")
SKIP_FOLDER_NAMES = frozenset({
    "junk", "spam", "bulk mail", "trash", "deleted messages", "deleted items",
    "drafts", "sent", "sent messages", "sent items", "outbox", "notes",
})

DEBT_KIND_LABELS = {
    "credit_card": "credit card", "loan": "loan", "student_loan": "student loan",
    "auto": "auto loan", "mortgage": "mortgage", "medical": "medical debt",
    "collections": "collections account", "other": "debt",
}

DEBT_SYSTEM = (
    "You read one email and decide whether it is evidence of a DEBT the recipient owes.\n\n"
    "A DEBT is a BALANCE OWED, usually carrying an interest rate: a credit card, a "
    "personal or auto loan, a student loan, a mortgage, a medical debt, or an account "
    "that has gone to collections. The thing that makes it a debt is a running balance "
    "that persists between statements.\n\n"
    "A BILL is NOT a debt. A utility bill, phone bill, internet bill, insurance premium, "
    "rent, a subscription, or an invoice for a service is an amount due for something "
    "recently supplied, not a balance carried at interest. Say is_debt false for those, "
    "even though they name an amount and a due date. The one exception is when such an "
    "account has itself been sent to COLLECTIONS or is described as charged off — then it "
    "has become a debt and is_debt is true.\n\n"
    "Reply with ONLY a JSON object, no other text and no markdown fences, in this exact "
    "shape:\n"
    '{"is_debt": true or false, '
    '"creditor": the company the balance is owed to (the lender, issuer, servicer or '
    'collection agency — not the recipient), '
    '"account_last4": ONLY the last four characters of the account number if the message '
    'shows them, otherwise "". NEVER output a full or partial account number longer than '
    "four characters, "
    '"kind": one of "credit_card", "loan", "student_loan", "auto", "mortgage", "medical", '
    '"collections", "other", '
    '"balance": the OUTSTANDING BALANCE OWED, exactly as the message writes it including '
    'the currency symbol (e.g. "$4,218.66"), or "" if the message never states one, '
    '"apr": the interest rate exactly as written (e.g. "24.99%"), or "" if not stated, '
    '"minimum_payment": the minimum payment due, exactly as written, or "" if not stated, '
    '"statement_date": the date this balance was true as of, as YYYY-MM-DD, or "" if the '
    "message does not state one, "
    '"due_date": the date a payment is due, as YYYY-MM-DD, or "" if not stated, '
    '"confidence": "high", "medium" or "low", '
    '"reasoning": one sentence saying why}\n\n'
    "Rules:\n"
    "- The BALANCE is what is owed in total, not the minimum payment and not the amount "
    "of a single recent transaction. If the message gives a minimum payment but no "
    "balance, put it in minimum_payment and leave balance \"\".\n"
    "- A marketing or offer email from a bank or lender — a pre-approval, a balance "
    "transfer offer, a 0% APR promotion, a credit-limit increase offer, a new card "
    "advert — is NOT a debt, however much financial vocabulary it uses. is_debt false.\n"
    "- A receipt or confirmation for a payment already made is NOT a debt by itself. But "
    "a message that confirms a payment AND states the remaining balance IS evidence of "
    "one: is_debt true, and put the remaining balance in balance.\n"
    "- A credit score notification, a fraud alert, a login alert, a card-shipped notice "
    "and a statement-available notice with no figures are NOT debts.\n"
    "- NEVER guess, calculate, convert or infer a number or a date that is not written in "
    "the message. Leave the field \"\" instead. These numbers become his real debt "
    "picture, and an invented one is worse than a missing one.\n"
    "- NEVER output more than four characters in account_last4, whatever the message "
    "shows. Four characters, or an empty string.\n"
    "- The creditor must be the institution actually owed. For a collections notice that "
    "is the collection agency, and mention the original creditor in reasoning."
)


def _parse_json_object(text: str) -> dict | None:
    """Same extraction as mail_bills/mail_importance: models wrap JSON in prose or a fence
    even when told not to."""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        logger.warning("mail debts: could not parse LLM response as JSON")
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_number(text: str | None) -> float | None:
    """A real number only when the text contains exactly one.

    The paired text/typed convention mail_bills._parse_amount established, and it matters
    more here: "$800-$1,200", "0% APR for 12 months" and "varies" all return None, keeping
    the wording verbatim and the numeric column NULL rather than picking an end of a range
    and reporting it as his balance. A promotional "0% APR for 12 months" carrying two
    numbers failing to parse is the behaviour working, not a gap.
    """
    if not text:
        return None
    numbers = re.findall(r"\d[\d,]*(?:\.\d+)?", str(text))
    if len(numbers) != 1:
        return None
    try:
        return float(numbers[0].replace(",", ""))
    except ValueError:
        return None


def _parse_iso_date(value: str | None) -> str | None:
    """An ISO date only when the model actually gave one. "last month", "Oct 1" and an
    impossible 2026-13-45 are dropped rather than guessed at."""
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def message_date(message: dict) -> str | None:
    """The ISO date an email was actually sent, from its Date header.

    This is what a statement's observation is dated by when the model couldn't read a
    statement date out of the body, and getting it right is the difference between a real
    balance trend and twelve points stacked on today. A header that won't parse returns
    None so the caller can fall back explicitly rather than silently dating a 2019
    statement as current.
    """
    raw = (message.get("date") or "").strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        # Some senders (and Jarvis's own fakes) write a plain ISO date instead.
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    return parsed.date().isoformat()


def classify_debt(llm, message: dict, today: date | None = None) -> dict | None:
    """Asks the LLM whether `message` is evidence of a debt, and normalizes what comes back.

    Returns None for anything that isn't -- including output that didn't parse, and a
    claimed debt with no creditor, which is evidence that can't be filed against anything.
    Silence is the safe failure mode, same as mail_bills.classify_bill: a missed statement
    is one his next statement will surface again, while an invented debt is a number in his
    payoff plan that was never real.

    A detection with NO balance is still kept in one narrow case: a high-confidence hit
    that names an account. "Your statement is ready, sign in to view" states no figure but
    is real evidence that the account EXISTS, which is worth something to a man assembling
    a debt list he doesn't have -- and it can invent no precision, since every numeric
    field stays NULL. Anything weaker than that is dropped.
    """
    today = today or date.today()
    prompt = (
        f"Today's date is {today.isoformat()}.\n\n"
        f"From: {message.get('from', '')}\n"
        f"Subject: {message.get('subject', '')}\n"
        f"Date: {message.get('date', '')}\n\n"
        f"{(message.get('body') or '')[:BODY_CHARS]}"
    )
    response = llm.chat(
        messages=[
            {"role": "system", "content": DEBT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        tools=None, think=False,
    )
    parsed = _parse_json_object((response or {}).get("content") or "")
    if not parsed or not parsed.get("is_debt"):
        return None

    creditor = str(parsed.get("creditor") or "").strip()
    if not creditor or not personal_db.normalize_creditor(creditor):
        return None

    balance_text = str(parsed.get("balance") or "").strip()
    # sanitize_account_last4 is the structural guard, not the prompt: a model that ignores
    # the instruction and returns a full card number still cannot store one.
    account_last4 = personal_db.sanitize_account_last4(parsed.get("account_last4"))
    confidence = str(parsed.get("confidence") or "").strip().lower()
    minimum_text = str(parsed.get("minimum_payment") or "").strip()
    if not balance_text and not minimum_text and not (account_last4 and confidence == "high"):
        return None

    kind = str(parsed.get("kind") or "").strip().lower().replace(" ", "_")
    if kind not in personal_db.DEBT_KINDS:
        kind = "other"

    apr_text = str(parsed.get("apr") or "").strip()
    return {
        "creditor": creditor,
        "account_last4": account_last4,
        "kind": kind,
        "balance_text": balance_text,
        "balance": _parse_number(balance_text),
        "apr_text": apr_text,
        "apr": _parse_number(apr_text),
        "minimum_payment_text": minimum_text,
        "minimum_payment": _parse_number(minimum_text),
        "statement_date": _parse_iso_date(parsed.get("statement_date")),
        "due_date": _parse_iso_date(parsed.get("due_date")),
        "confidence": confidence,
        "reasoning": str(parsed.get("reasoning") or "").strip(),
    }


def sweep_folders(mail_client, explicit: list | None = None) -> list:
    """Which folders this run will search, INBOX first.

    Archived and filed mail is where old statements live, so a sweep that only looked at
    INBOX would miss most of what it exists to find. Junk, Trash, Sent and Drafts are
    excluded (see SKIP_FOLDER_FLAGS). A server that won't list its folders degrades to
    INBOX rather than failing the run.
    """
    if explicit:
        return list(explicit)
    try:
        listing = mail_client.list_folders()
    except Exception:
        logger.exception("mail debts: could not list folders, falling back to INBOX")
        return ["INBOX"]

    folders = []
    for entry in listing.get("folders", []):
        name = (entry.get("name") or "").strip()
        flags = entry.get("flags") or []
        if not name or any(flag in flags for flag in SKIP_FOLDER_FLAGS):
            continue
        if name.lower() in SKIP_FOLDER_NAMES:
            continue
        folders.append(name)

    # INBOX first and always present: it is the one folder guaranteed to exist, and it is
    # where scanned physical mail will land once his scanner is configured.
    folders = [f for f in folders if f.upper() != "INBOX"]
    return ["INBOX"] + folders


def _observed_on(result: dict, message: dict, today: date) -> str:
    """The date a detection's facts were true. The model's statement date if it read one,
    else the date the message was actually sent, else today.

    The order matters: dating a 2019 statement as today would flatten a balance trend into
    a vertical line and make a long-paid-down card look like it is still at its old
    balance.
    """
    return result["statement_date"] or message_date(message) or today.isoformat()


def _proposal_card_text(debt: dict, kind_label: str) -> tuple:
    """The review card for a proposed debt: (summary, detail).

    Rendered from the debt's CURRENT state rather than from the message that triggered it,
    because a historical sweep keeps attaching statements to the same proposal -- so the
    card he eventually reads has to describe twelve statements if twelve were found, not
    the one that happened to arrive first.
    """
    where = f" ending {debt['account_last4']}" if debt["account_last4"] else ""
    count = debt["observation_count"]
    trend = debt["balance_trend"]

    if debt["current_balance"] is not None:
        headline = f"{_money(debt['current_balance'])} as of {debt['current_balance_observed_on']}"
    elif debt["current_balance_text"]:
        headline = f"balance stated as {debt['current_balance_text']}"
    else:
        headline = "no balance stated in any message yet"
    summary = f"{kind_label}{where} — {headline} — from {count} message{'s' if count != 1 else ''} in your mail"

    lines = [
        f"I found what looks like a {kind_label} with {debt['creditor']}{where} in your "
        f"email history. It is NOT in your debt list — confirm it and I'll start tracking it.",
    ]
    if len(trend) >= 2:
        lines.append(
            f"Balance over {len(trend)} statements: {_money(trend[0]['balance'])} on "
            f"{trend[0]['observed_on']} → {_money(trend[-1]['balance'])} on {trend[-1]['observed_on']}."
        )
    elif debt["current_balance_text"]:
        lines.append(f"Balance as written: {debt['current_balance_text']}"
                     + (" — not a single clear number, so it's stored as-is rather than guessed at."
                        if debt["current_balance"] is None else "."))

    facts = []
    if debt["current_apr_text"]:
        facts.append(f"rate {debt['current_apr_text']}")
    if debt["current_minimum_payment_text"]:
        facts.append(f"minimum payment {debt['current_minimum_payment_text']}")
    if facts:
        lines.append("Also read from the statements: " + ", ".join(facts) + ".")
    if debt["current_apr_text"] and debt["current_apr"] is None:
        lines.append("The rate isn't a single clear number, so it's kept as written and no "
                     "interest cost is calculated from it.")

    lines.append(f"Where this came from: {debt['origin_detail'] or 'a message in your mailbox'}. "
                 "Everything here was read out of your mail, not confirmed by you — that's "
                 "what confirming it does.")
    lines.append(
        "Nothing has been done to the messages: nothing was read, moved, archived or "
        "replied to. Nothing was paid, scheduled, or added to your recurring charges, and "
        "I have not assigned this a payoff priority — that one's yours."
    )
    return summary, "\n\n".join(lines)


def _money(value) -> str:
    return f"${value:,.2f}" if isinstance(value, (int, float)) else str(value)


def _upsert_proposal_card(db_path: str, owner_user_id: int, debt: dict) -> None:
    """Files the confirmation card for a proposed debt, or refreshes the one already filed.

    One card per proposed DEBT, never one per message -- twelve statements from one card
    must ask him one question, not twelve. A failure here must not lose the evidence: the
    debts/debt_observations rows are the source of truth whether or not this card exists.
    """
    kind_label = DEBT_KIND_LABELS.get(debt["kind"], "debt")
    summary, detail = _proposal_card_text(debt, kind_label)
    where = f" ending {debt['account_last4']}" if debt["account_last4"] else ""
    try:
        existing = business_db.get_review_item_by_ref(db_path, owner_user_id, "debts", debt["id"])
        if existing is not None:
            business_db.refresh_review_item_text(
                db_path, owner_user_id, existing["id"], summary=summary, detail=detail)
            return
        business_db.create_review_item(
            db_path, owner_user_id,
            title=f"Debt found in your mail: {debt['creditor']}{where}",
            kind="other", source_agent="mail_debts", summary=summary, detail=detail,
            ref_table="debts", ref_id=debt["id"],
        )
    except Exception:
        logger.exception("mail debts: failed to file review card for debt %s", debt["id"])


def _ambiguous_card(db_path: str, owner_user_id: int, result: dict, message: dict, reason: str) -> None:
    """Asks him which account a statement belongs to, when matching genuinely can't tell.

    This is the case where guessing does real damage: attaching one card's balances to
    another card's trend line silently corrupts both, and there is no way to notice
    afterwards. So the evidence is surfaced as a question instead of being applied.
    """
    try:
        business_db.create_review_item(
            db_path, owner_user_id,
            title=f"Which {result['creditor']} account is this?",
            kind="other", source_agent="mail_debts",
            summary=f"{result['balance_text'] or 'no balance stated'} — {reason}",
            detail=(
                f"A message from {message.get('from', '')} "
                f"(\"{message.get('subject') or '(no subject)'}\") looks like "
                f"{result['balance_text'] or 'a balance'} owed to {result['creditor']}, but "
                f"{reason}.\n\n"
                "I haven't recorded it against anything, because attaching one account's "
                "balance to another account's history would quietly corrupt both and there'd "
                "be no way to spot it later.\n\n"
                "Tell me which account it belongs to and I'll record it — or say it's a new "
                "one and I'll start tracking it separately."
            ),
            ref_table="debts", ref_id=None,
        )
    except Exception:
        logger.exception("mail debts: failed to file ambiguity card")


def record_debt_evidence(
    db_path: str, owner_user_id: int, result: dict, message: dict, folder: str, uid: str,
    today: date,
) -> dict:
    """Applies one classified message to the debt picture: match, propose-or-attach, observe.

    Every branch is idempotent, which is what makes a re-run a no-op rather than a
    duplicate: create_debt is ON CONFLICT DO NOTHING, the observation carries the source
    ref that its unique index dedupes on, and an identical balance already recorded for
    the same date (the same statement filed in a second folder) is skipped outright.
    """
    match = personal_db.find_matching_debt(
        db_path, owner_user_id, result["creditor"], result["account_last4"])
    if match["ambiguous"]:
        _ambiguous_card(db_path, owner_user_id, result, message, match["reason"])
        return {"debt_id": None, "ambiguous": True, "created": False, "observed": False}

    observed_on = _observed_on(result, message, today)
    origin_detail = (
        f"{DEBT_KIND_LABELS.get(result['kind'], 'debt')} read from a message from "
        f"{message.get('from', '')} dated {observed_on}"
    )

    debt_id = match["debt_id"]
    created = False
    if debt_id is None:
        debt_id = personal_db.create_debt(
            db_path, owner_user_id, result["creditor"], account_last4=result["account_last4"],
            kind=result["kind"], tracking_state="proposed", origin="email",
            origin_detail=origin_detail,
        )
        created = True
    elif match.get("learned_account_last4"):
        # A later statement named the account the first one didn't. Filling it in keeps
        # the row matchable and stops the next statement looking like a different card.
        personal_db.update_debt(
            db_path, owner_user_id, debt_id, account_last4=match["learned_account_last4"])

    observed = False
    if not personal_db.has_equivalent_observation(db_path, debt_id, observed_on, result["balance_text"]):
        observed = personal_db.add_debt_observation(
            db_path, debt_id, observed_on=observed_on,
            balance_text=result["balance_text"], balance=result["balance"],
            apr_text=result["apr_text"], apr=result["apr"],
            minimum_payment_text=result["minimum_payment_text"],
            minimum_payment=result["minimum_payment"],
            due_date=result["due_date"], source="email", source_ref=f"{folder}:{uid}",
            source_detail=(
                f"{message.get('subject') or '(no subject)'} — from {message.get('from', '')}"),
            confidence=result["confidence"], confirmed=False, notes=result["reasoning"],
        ) is not None

    debt = personal_db.get_debt(db_path, owner_user_id, debt_id)
    # Only a proposal gets a card. Once he has confirmed a debt, a new statement is just
    # another observation on something he already told me to track -- not a fresh question.
    if debt is not None and debt["tracking_state"] == "proposed":
        _upsert_proposal_card(db_path, owner_user_id, debt)
    return {"debt_id": debt_id, "ambiguous": False, "created": created, "observed": observed}


def run_debt_mail_sweep_once(
    db_path: str, llm, mail_client, owner_user_id: int, folders: list | None = None,
    per_run_limit: int = DEFAULT_PER_RUN_LIMIT, shortlist_limit: int = DEFAULT_SHORTLIST_LIMIT,
    today: date | None = None,
) -> dict:
    """One bounded, resumable pass over his mail history looking for debt.

    Safe to call repeatedly and safe to stop: a scheduled tick and a manual run are the
    same call, every message is judged exactly once ever, and the per-run cap means a run
    ends rather than grinding through a backlog of tens of thousands. Messages past the cap
    are left UNJUDGED, not skipped -- no ledger row means the next run picks them up.

    Nothing here mutates the mailbox, and nothing it finds enters his debt picture without
    him: detections become tracking_state='proposed' rows plus a review card.
    """
    mail_db.init_mail_db(db_path)
    personal_db.init_personal_db(db_path)
    today = today or date.today()
    targets = sweep_folders(mail_client, folders)

    stats = {
        "folders_searched": 0, "shortlisted": 0, "scanned": 0, "detected": 0,
        "debts_proposed": 0, "observations": 0, "ambiguous": 0, "capped": False,
    }
    remaining = per_run_limit

    for folder in targets:
        if remaining <= 0:
            stats["capped"] = True
            break
        # One query for the whole folder's ledger rather than one per candidate: on a
        # resumed run nearly every matching uid is already judged, and asking about them
        # one at a time is thousands of round trips to decide to do nothing. Handed to
        # search_uids so the shortlist cap applies to what is still UNJUDGED -- capping
        # first would pin every run to the same newest slice and strand everything older
        # (see search_uids' docstring).
        already = mail_db.scanned_debt_uids(db_path, owner_user_id, folder)
        try:
            found = mail_client.search_uids(
                SEARCH_TERMS, folder=folder, limit=shortlist_limit, exclude=already)
        except Exception as exc:
            # One unreadable folder must not end the sweep -- the rest of his mail is
            # still worth searching, and this folder is retried on the next run. A folder
            # that simply can't be selected (a \Noselect container or a client-only
            # mailbox) is an expected condition, so log a one-line warning rather than a
            # full stack trace on every run.
            logger.warning("mail debts: could not search folder %s: %s", folder, exc)
            continue
        stats["folders_searched"] += 1

        candidates = found.get("uids", [])
        stats["shortlisted"] += len(candidates)

        for uid in candidates:
            if remaining <= 0:
                stats["capped"] = True
                break
            # A message the junk pass flagged is not evidence of his debt, even if its
            # move into Junk failed -- same guard as mail_bills, and more necessary here:
            # a fake collections notice is a classic scam and reads exactly like a debt.
            if mail_db.is_logged_junk(db_path, folder, uid):
                continue

            message = mail_client.read_message(uid, folder=folder)
            if message.get("error"):
                logger.warning("mail debts: could not read %s uid %s: %s", folder, uid, message["error"])
                continue

            remaining -= 1
            stats["scanned"] += 1
            try:
                result = classify_debt(llm, message, today=today)
            except Exception:
                logger.exception("mail debts: classification failed for %s uid %s", folder, uid)
                continue

            if result is None:
                mail_db.mark_debt_scanned(db_path, owner_user_id, folder, uid, is_debt=False)
                continue

            outcome = record_debt_evidence(
                db_path, owner_user_id, result, message, folder, uid, today)
            mail_db.mark_debt_scanned(
                db_path, owner_user_id, folder, uid, is_debt=True, debt_id=outcome["debt_id"])
            stats["detected"] += 1
            stats["debts_proposed"] += int(outcome["created"])
            stats["observations"] += int(outcome["observed"])
            stats["ambiguous"] += int(outcome["ambiguous"])

    return stats
