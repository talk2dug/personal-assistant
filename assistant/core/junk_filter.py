"""Lightweight heuristic spam/junk classifier for inbound email headers.

Deliberately dependency-free and network-free: this only looks at the From/
Subject (and, when available, a short body snippet) that MailClient already
fetches for list_recent/search, so classifying a message costs nothing extra
over the IMAP round-trip those already make. It is a heuristic, not a
guarantee -- false negatives are expected, and flag_junk (mail_client.py)
only ever tags a message rather than moving or deleting it, so a wrong call
is cheap to undo.
"""
import re

# Common marketing/spam phrasing. Deliberately short and generic rather than an
# exhaustive blocklist: broad wordlists just chase evolving spam-speak and rot
# quickly. This is meant to catch loud, obvious junk, not to be a spam engine.
_SPAM_PHRASES = (
    "unsubscribe", "act now", "limited time", "click here", "risk free",
    "you've won", "you have won", "congratulations you", "free money",
    "viagra", "cialis", "weight loss", "work from home", "earn $",
    "make money fast", "verify your account", "suspended your account",
    "wire transfer", "nigerian prince", "crypto giveaway", "double your",
    "no cost to you", "cash prize",
)

_SUSPICIOUS_TLDS = (".ru", ".tk", ".top", ".xyz", ".click", ".loan", ".work")

_SENDER_ADDR_RE = re.compile(r"<([^>]+)>")
_REPEATED_RE_RE = re.compile(r"\bre:\s*re:\s*re:", re.I)
_EXCESSIVE_PUNCTUATION_RE = re.compile(r"[!$]{2,}")


def _sender_address(from_header: str) -> str:
    """Pulls the bare address out of a 'Display Name <addr>' header, falling
    back to the raw header when there's no angle-bracket form."""
    match = _SENDER_ADDR_RE.search(from_header or "")
    return (match.group(1) if match else (from_header or "")).strip().lower()


def _is_shouty(subject: str) -> bool:
    letters = [c for c in subject if c.isalpha()]
    if len(letters) < 6:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return (upper / len(letters)) > 0.7


def score(headers: dict) -> int:
    """Counts independent junk signals tripped by a message's headers. 0 means
    nothing suspicious was found. `headers` is the same shape MailClient
    builds for list_recent/search (at minimum 'from' and 'subject'; an
    optional 'snippet' of body text is also considered when present).
    """
    subject = headers.get("subject") or ""
    from_header = headers.get("from") or ""
    sender = _sender_address(from_header)
    haystack = f"{subject} {from_header} {headers.get('snippet') or ''}".lower()

    hits = 0
    if any(phrase in haystack for phrase in _SPAM_PHRASES):
        hits += 1
    if sender.endswith(_SUSPICIOUS_TLDS):
        hits += 1
    if _EXCESSIVE_PUNCTUATION_RE.search(subject) or subject.count("!") >= 3:
        hits += 1
    if _is_shouty(subject):
        hits += 1
    if _REPEATED_RE_RE.search(subject):
        hits += 1
    return hits


def is_junk(headers: dict, threshold: int = 1) -> bool:
    """A message is flagged junk once it trips `threshold` or more independent
    signals. The default of 1 favors recall (surfacing candidates) over
    precision, which is the right tradeoff given flag_junk only tags a
    message rather than moving or deleting it.
    """
    return score(headers) >= threshold
