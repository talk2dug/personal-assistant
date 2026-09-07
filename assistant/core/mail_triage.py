"""Pure classification logic for autonomous email triage: junk-likelihood scoring,
category classification, folder suggestion, and template-based draft replies.

Deliberately rule-based rather than LLM-based: deterministic and fully unit-testable
without a live model or network call, cheap to run over a whole inbox, and good enough
for the common cases (obvious spam, order/support/billing/scheduling mail) this feature
targets. Nothing here touches IMAP, a database, or the network -- mail_tools.py wires it
to the real mailbox.
"""
import re

# --- junk scoring --------------------------------------------------------------------

JUNK_PHRASES = [
    "act now", "click here", "limited time", "risk free", "risk-free", "no obligation",
    "congratulations", "you've won", "you have won", "claim your prize", "claim your reward",
    "verify your account", "verify your identity", "suspended account", "unusual activity",
    "wire transfer", "gift card", "crypto giveaway", "double your", "work from home",
    "make money fast", "lowest price guaranteed", "one time offer", "special promotion",
    "dear customer", "dear valued customer", "dear friend", "urgent response required",
    "this is not spam", "100% free", "free trial", "no credit card required", "act immediately",
]

# Free/cheap TLDs heavily favored by mass-spam and phishing campaigns. Not proof of
# anything on their own (plenty of legitimate small sites use them), which is why each
# hit is a modest score bump rather than an automatic verdict.
SUSPICIOUS_TLDS = (".zip", ".top", ".work", ".click", ".xyz", ".gq", ".cf", ".tk", ".loan", ".men", ".rest")

MONEY_SYMBOL_RE = re.compile(r"[$€£]\s?\d")
EXCESSIVE_PUNCT_RE = re.compile(r"[!?]{2,}")
URL_RE = re.compile(r"https?://[^\s)>\]]+", re.I)
SENDER_DOMAIN_RE = re.compile(r"@([\w.-]+)")

JUNK_THRESHOLD = 0.5


def score_junk(sender: str, subject: str, body: str = "") -> tuple[float, list[str]]:
    """Returns (score in [0, 1], reasons). Higher is more likely junk/spam.

    Heuristic and additive on purpose: several weak signals (shouty subject, spammy
    phrasing, money symbols, link-stuffed body, suspicious sending domain) are more
    reliable together than any single one, and additive scoring keeps every contributing
    reason visible to whoever reads it rather than collapsing into an opaque yes/no.
    """
    sender = sender or ""
    subject = subject or ""
    body = body or ""
    text = f"{subject}\n{body}".lower()
    score = 0.0
    reasons: list[str] = []

    letters = [c for c in subject if c.isalpha()]
    if len(letters) >= 6:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio > 0.6:
            score += 0.2
            reasons.append("subject is mostly capital letters")

    phrase_hits = [p for p in JUNK_PHRASES if p in text]
    if phrase_hits:
        score += min(0.45, 0.15 * len(phrase_hits))
        reasons.append(f"contains spam-typical phrasing ({', '.join(phrase_hits[:3])})")

    if MONEY_SYMBOL_RE.search(text):
        score += 0.1
        reasons.append("mentions money amounts prominently")

    if EXCESSIVE_PUNCT_RE.search(subject):
        score += 0.1
        reasons.append("excessive punctuation in subject")

    urls = URL_RE.findall(body)
    if len(urls) >= 4:
        score += 0.15
        reasons.append(f"body contains {len(urls)} links")

    if any(tld in text for tld in SUSPICIOUS_TLDS):
        score += 0.15
        reasons.append("references a suspicious top-level domain")

    domain_match = SENDER_DOMAIN_RE.search(sender)
    if domain_match:
        domain = domain_match.group(1).lower()
        if any(domain.endswith(tld) for tld in SUSPICIOUS_TLDS):
            score += 0.2
            reasons.append(f"sender domain uses a suspicious TLD ({domain})")
        if re.search(r"\d{4,}", domain):
            score += 0.1
            reasons.append("sender domain contains an unusual numeric string")

    return min(score, 1.0), reasons


def is_junk(score: float, threshold: float = JUNK_THRESHOLD) -> bool:
    return score >= threshold


# --- category classification ----------------------------------------------------------

CATEGORY_ORDER_INQUIRY = "order_inquiry"
CATEGORY_SUPPORT = "support_request"
CATEGORY_BILLING = "billing_invoice"
CATEGORY_SCHEDULING = "meeting_scheduling"
CATEGORY_NEWSLETTER = "newsletter_marketing"
CATEGORY_SPAM = "spam_junk"
CATEGORY_OTHER = "other"

ALL_CATEGORIES = [
    CATEGORY_ORDER_INQUIRY, CATEGORY_SUPPORT, CATEGORY_BILLING, CATEGORY_SCHEDULING,
    CATEGORY_NEWSLETTER, CATEGORY_SPAM, CATEGORY_OTHER,
]

# Checked in this order: an order-shipping notice that also happens to carry an
# "unsubscribe" footer should still route as an order update, not a newsletter, so the
# more actionable categories are matched before the catch-all bulk-mail one.
_CATEGORY_KEYWORDS = [
    (CATEGORY_BILLING, [
        "invoice", "receipt", "payment due", "billing", "past due", "balance due", "payment received",
    ]),
    (CATEGORY_ORDER_INQUIRY, [
        "order", "tracking number", "shipment", "shipped", "delivery", "delivered",
        "return label", "where is my order",
    ]),
    (CATEGORY_SCHEDULING, [
        "schedule a call", "meeting", "reschedule", "available time", "calendar invite",
        "book a time", "let's meet", "set up a call",
    ]),
    (CATEGORY_SUPPORT, [
        "help", "support", "issue", "problem", "broken", "refund", "complaint", "not working", "trouble",
    ]),
    (CATEGORY_NEWSLETTER, [
        "unsubscribe", "newsletter", "view in browser", "weekly digest", "you are receiving this email",
    ]),
]

DEFAULT_CATEGORY_FOLDERS = {
    CATEGORY_ORDER_INQUIRY: "Orders",
    CATEGORY_SUPPORT: "Support",
    CATEGORY_BILLING: "Billing",
    CATEGORY_SCHEDULING: "INBOX",
    CATEGORY_NEWSLETTER: "Newsletters",
    CATEGORY_SPAM: "Junk",
    CATEGORY_OTHER: "INBOX",
}


def classify_category(
    sender: str, subject: str, body: str = "", junk_score: float | None = None,
) -> tuple[str, float, list[str]]:
    """Returns (category, confidence in [0, 1], reasons).

    junk_score, when passed, is expected to already be above the junk threshold (the
    caller decides that) -- when it is, the message is classified spam_junk outright
    rather than searched for category keywords a spam email may coincidentally contain.
    """
    if junk_score is not None and is_junk(junk_score):
        return CATEGORY_SPAM, junk_score, ["junk-likelihood score was above threshold"]

    text = f"{subject}\n{body}".lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        hits = [kw for kw in keywords if kw in text]
        if hits:
            confidence = 0.9 if len(hits) >= 2 else 0.65
            return category, confidence, [f"matched: {', '.join(hits)}"]

    return CATEGORY_OTHER, 0.3, ["no strong category keywords matched"]


def suggest_folder(category: str, overrides: dict | None = None) -> str:
    folders = {**DEFAULT_CATEGORY_FOLDERS, **(overrides or {})}
    return folders.get(category, "INBOX")


# --- draft replies ---------------------------------------------------------------------

# Categories where an auto-drafted reply is actually useful. Newsletters/marketing and
# spam get no draft (there's nothing to reply to); "other" gets no draft because a
# template written for an unknown situation is more likely to say the wrong thing than
# to help.
REPLIABLE_CATEGORIES = {CATEGORY_ORDER_INQUIRY, CATEGORY_SUPPORT, CATEGORY_BILLING, CATEGORY_SCHEDULING}


def _first_name(from_header: str) -> str:
    if not from_header:
        return "there"
    name_part = from_header.split("<")[0].strip().strip('"')
    if not name_part or "@" in name_part:
        return "there"
    return name_part.split()[0]


def _reply_subject(subject: str) -> str:
    subject = subject or ""
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


_TEMPLATES = {
    CATEGORY_ORDER_INQUIRY: (
        "Hi {name},\n\nThanks for reaching out about your order. I'm looking into the current "
        "status now and will follow up shortly with tracking details.\n\nIf you have an order "
        "number handy, replying with it will help me find it faster.\n\nThanks for your patience,\n"
        "[Your name]"
    ),
    CATEGORY_SUPPORT: (
        "Hi {name},\n\nSorry to hear you're running into trouble. I've noted the issue you "
        "described and I'm looking into it now.\n\nCould you share a bit more detail (what you "
        "expected vs. what happened, and any error message) so I can get this sorted as quickly "
        "as possible?\n\nThanks,\n[Your name]"
    ),
    CATEGORY_BILLING: (
        "Hi {name},\n\nThanks for the note about billing. I'm reviewing the invoice/payment "
        "details now and will get back to you with a clear answer shortly.\n\nBest,\n[Your name]"
    ),
    CATEGORY_SCHEDULING: (
        "Hi {name},\n\nHappy to find a time. Here's my availability -- let me know what works "
        "best for you and I'll send a calendar invite.\n\n[Add specific times here]\n\nBest,\n"
        "[Your name]"
    ),
}


def draft_reply(category: str, sender: str, subject: str, body: str = "") -> str | None:
    """A template-based draft reply for categories where one is likely to be useful, or
    None otherwise. Always marked as a draft that hasn't been sent -- this never
    constitutes an actual reply, only text for a human (or save_draft_email) to use.
    """
    if category not in REPLIABLE_CATEGORIES:
        return None
    body_text = _TEMPLATES[category].format(name=_first_name(sender))
    return f"[DRAFT -- not sent, review before sending]\nSubject: {_reply_subject(subject)}\n\n{body_text}"
