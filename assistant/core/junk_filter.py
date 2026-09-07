"""Heuristic junk/spam scoring for incoming email.

Deliberately rule-based rather than a model call: junk-flagging is meant to run on
every new message in the background (see scheduler.py's mail_junk_scan job), so it has
to be fast, free, and dependable with no network dependency of its own. A weighted set
of signals -- known spam vocabulary, sender/brand impersonation, shouting subject
lines, link-shortener and raw-IP links -- catches the overwhelming majority of
real-world spam/phishing without ever calling an LLM or a third-party API.

This is deliberately not a full spam filter: SPF/DKIM/DNSBL-style checks are Apple's
job upstream of this (a message that fails those is normally never delivered to the
inbox at all). This module decides whether a message that DID reach the inbox should
be triaged back out of it.

Every function here is pure (no I/O), so it's trivial to unit test in isolation from
IMAP -- see tests/test_junk_filter.py.
"""
import re

# Any single signal below is rarely definitive on its own; the default threshold
# expects two or three to line up before a message gets moved, so one flattering
# coincidence (a legit newsletter that happens to say "limited time") won't trip it,
# but a phishing email that shouts, impersonates a brand, AND asks you to click
# immediately will clear it easily.
DEFAULT_THRESHOLD = 4.0

# Phrases that show up constantly in spam/phishing but essentially never in ordinary
# correspondence a small maker business would receive. Weighted higher for phrases
# that are almost never legitimate on their own (wiring money, "you've won").
_KEYWORD_WEIGHTS: dict[str, float] = {
    "act now": 1.5,
    "act immediately": 1.5,
    "urgent action required": 2,
    "verify your account": 2,
    "confirm your identity": 1.5,
    "suspended your account": 2,
    "account has been limited": 2,
    "click here immediately": 2,
    "wire transfer": 2,
    "gift card": 2,
    "gift cards": 2,
    "you have won": 2.5,
    "you've won": 2.5,
    "claim your prize": 2.5,
    "risk-free": 1,
    "no obligation": 1,
    "make money fast": 2,
    "lowest price guaranteed": 1,
    "congratulations you": 2,
    "dear customer": 1,
    "dear valued customer": 1.5,
    "nigerian": 2,
    "inheritance": 1.5,
    "crypto investment": 2,
    "double your": 1.5,
    "limited time offer": 1,
    "this is not a scam": 2.5,
    "social security number": 2,
    "tax refund": 1.5,
    "unsubscribe here": 0.5,
}

# Brand names commonly impersonated in phishing display names. If the display name
# claims to be one of these but the sending domain doesn't contain it, that's a strong
# signal -- legitimate PayPal mail comes from a paypal.com address, not a random
# free-mail or throwaway domain.
_IMPERSONATED_BRANDS = [
    "paypal", "apple", "amazon", "netflix", "microsoft", "irs", "bank of america",
    "wells fargo", "chase", "usps", "fedex", "ups", "dhl", "docusign", "google",
    "facebook", "instagram", "linkedin",
]

_SHORTENER_DOMAINS = ("bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd")

# Loosely matches 'Display Name <address@domain>' or a bare address; decode_header'd
# From values are free-form text, not guaranteed RFC 5322, so this is intentionally
# forgiving rather than a strict parser.
_FROM_RE = re.compile(r'^\s*"?([^"<]*)"?\s*<?([\w.+-]+@[\w.-]+)>?\s*$')


def _split_sender(sender: str) -> tuple[str, str]:
    """('display name', 'address@domain') from a decoded From header, both lowercased.
    Falls back to treating the whole string as the address if it doesn't parse cleanly
    -- some senders omit the display name entirely."""
    match = _FROM_RE.match(sender or "")
    if not match:
        return "", (sender or "").strip().lower()
    display, address = match.groups()
    return display.strip().lower(), address.strip().lower()


def score_message(sender: str, subject: str, body: str = "") -> dict:
    """Scores one message for junk/spam likelihood.

    Returns {'score': float, 'reasons': [str, ...]} -- reasons are kept (not just the
    number) so a flagged message can show its own justification rather than a bare
    number, which matters the first time the owner asks "why did it move this one".
    """
    subject = subject or ""
    body = body or ""
    haystack = f"{subject}\n{body}".lower()
    display_name, address = _split_sender(sender)
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""

    score = 0.0
    reasons: list[str] = []

    for phrase, weight in _KEYWORD_WEIGHTS.items():
        if phrase in haystack:
            score += weight
            reasons.append(f"contains spam phrase '{phrase}'")

    if len(subject) >= 8 and subject.upper() == subject and any(c.isalpha() for c in subject):
        score += 2
        reasons.append("subject line is all caps")

    if subject.count("!") >= 2:
        score += 1.5
        reasons.append("excessive exclamation marks in subject")

    for brand in _IMPERSONATED_BRANDS:
        brand_compact = brand.replace(" ", "")
        # The registrable-ish label (second-from-right, e.g. 'paypal' in mail.paypal.com)
        # must actually equal the brand -- a plain substring check (an earlier version of
        # this) misses the classic trick of burying the brand name in a longer fake
        # domain, e.g. 'totally-not-paypal.tk' contains 'paypal' as a substring but its
        # real second-level label is 'totallynotpaypal', not 'paypal'.
        labels = domain.replace("-", "").split(".") if domain else []
        second_level = labels[-2] if len(labels) >= 2 else (labels[0] if labels else "")
        if brand in display_name and domain and second_level != brand_compact:
            score += 4
            reasons.append(f"sender display name claims '{brand}' but domain is '{domain}'")
            break  # one impersonation signal is enough -- multiple brand names in one
                   # display name would be unusual to begin with

    if any(shortener in haystack for shortener in _SHORTENER_DOMAINS):
        score += 2
        reasons.append("contains a link-shortener URL")

    if re.search(r"https?://\d{1,3}(?:\.\d{1,3}){3}", haystack):
        score += 2
        reasons.append("contains a raw IP-address link")

    return {"score": round(score, 2), "reasons": reasons}


def is_junk(sender: str, subject: str, body: str = "", threshold: float = DEFAULT_THRESHOLD) -> bool:
    """Convenience yes/no wrapper around score_message for callers that don't need the
    breakdown."""
    return score_message(sender, subject, body)["score"] >= threshold
