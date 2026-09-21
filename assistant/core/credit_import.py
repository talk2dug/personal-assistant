"""Getting credit reports into the system, and measuring progress between them.

The Credit Specialist has had an empty table to work from since the day it was hired:
credit.py's own briefing says "NO CREDIT REPORT UPLOADED YET -- there are no tradelines to
work with". Every utilisation figure, dispute and payoff order it could produce has
therefore been guesswork. This is the way in.

Two rules shape all of it.

**Nothing sensitive leaves this machine.** A credit report carries a Social Security
number, full account numbers, employers and address history. Parsing is local, regex-based
and deterministic -- no model sees the file, not even to "help read it". Only the fields a
dispute actually needs are stored: creditor, the last four digits, balance, limit, status
and dates. `redact()` strips SSNs and full account numbers from any text before it is
written anywhere, because the one place this could leak is an error message quoting the
line it failed on.

**A best-effort parse must say it is best-effort.** Bureau layouts differ, exports differ,
and a wrong balance silently entered is worse than no balance at all -- it would be
disputed as fact. So parsing reports what it found AND what it could not read, and the
caller is expected to show him both rather than quietly accepting the result.

Progress monitoring falls out of the shape: each upload is its own dated report with its
own tradelines, so `compare()` diffs two of them and answers the only question that
matters month to month -- what actually changed.
"""
import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# --- redaction ------------------------------------------------------------------------
# Applied before anything is logged or stored. Ordered most-specific first so a full
# account number is not partially eaten by the SSN pattern.
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_LONG_ACCOUNT = re.compile(r"\b\d{9,19}\b")
_MASKED_TAIL = re.compile(r"(?:[xX*#]{2,}[\s-]*)(\d{4})\b")


def redact(text: str) -> str:
    """Remove the things that must never be stored, logged or shown.

    Kept deliberately blunt: it is better to redact a harmless long number than to let one
    real account number through because the pattern was clever.
    """
    if not text:
        return ""
    text = _SSN.sub("[SSN REDACTED]", text)
    return _LONG_ACCOUNT.sub(lambda m: "[ACCT ****" + m.group(0)[-4:] + "]", text)


# --- text extraction --------------------------------------------------------------------

def extract_text(path: str) -> str:
    """Pull readable text out of a report file.

    PDFs are read with pypdf. A scanned/image-only PDF yields nothing, which is a real and
    common case -- the caller must treat empty text as "could not read this", not as "this
    report has no accounts".
    """
    lowered = path.lower()
    if lowered.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError:                                  # pragma: no cover
            raise RuntimeError("pypdf is required to read PDF credit reports")
        reader = PdfReader(path)
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if lowered.endswith((".txt", ".csv", ".html", ".htm", ".json")):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        if lowered.endswith((".html", ".htm")):
            text = re.sub(r"<[^>]+>", " ", text)
        return text
    raise ValueError(f"unsupported report format: {os.path.basename(path)}")


# --- parsing -----------------------------------------------------------------------------

BUREAUS = {"experian": "experian", "equifax": "equifax", "transunion": "transunion",
           "trans union": "transunion"}

_MONEY = r"\$?\s?([0-9][0-9,]*(?:\.\d{2})?)"

_ACCOUNT_KINDS = (
    ("collection", "collections"), ("charge-off", "collections"), ("charge off", "collections"),
    ("student loan", "student_loan"), ("auto", "auto"), ("mortgage", "mortgage"),
    ("medical", "medical"), ("credit card", "credit_card"), ("revolving", "credit_card"),
    ("installment", "loan"), ("personal loan", "loan"),
)

_STATUS_WORDS = ("open", "closed", "paid", "current", "late", "delinquent", "charge-off",
                 "charged off", "collection", "in collections", "derogatory",
                 "pays as agreed", "never late", "settled", "transferred")


def _money_to_float(raw: str | None):
    if not raw:
        return None
    try:
        return float(raw.replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def detect_bureau(text: str) -> str | None:
    lowered = (text or "")[:6000].lower()
    hits = [(lowered.find(word), name) for word, name in BUREAUS.items() if word in lowered]
    return min(hits)[1] if hits else None


# Only labels that genuinely announce a score. The bare word "credit" was here once and
# was catastrophic: in a CREDIT report that word sits beside every limit on every page, so
# "Credit Limit: $750" was read as a credit score of 750 and stored as fact. A score is a
# number the whole repair strategy is steered by -- a wrong one is far worse than none.
_SCORE_LABEL = re.compile(
    r"(?:FICO|VantageScore|Vantage|credit\s+score|score)", re.I)

# If any of these sit between the label and the number, the number is money or a count,
# not a score.
_NOT_A_SCORE = ("limit", "balance", "payment", "due", "amount", "high", "credit line",
                "utilization", "utilisation", "inquiries", "accounts")

# FICO and VantageScore both top out at 850. Anything above it is a dollar figure, a year
# or a reference number wearing a score's clothes.
SCORE_MIN, SCORE_MAX = 300, 850


def detect_score(text: str):
    """The credit score, but only when the report actually says so.

    Deliberately conservative in every direction: an unfound score is a blank field he can
    type in, while an invented one silently misdirects every dispute and payoff decision
    made from it.
    """
    for label in _SCORE_LABEL.finditer(text or ""):
        window = (text or "")[label.end():label.end() + 28]
        if any(word in window.lower() for word in _NOT_A_SCORE):
            continue
        for number in re.finditer(r"(?<![\d.$])(\d{3})(?![\d.])", window):
            # A "$" anywhere before the digits in this window means money, not a score.
            if "$" in window[:number.start()]:
                continue
            value = int(number.group(1))
            if SCORE_MIN <= value <= SCORE_MAX:
                return value
    return None



# Phrases that mean a block is report furniture -- a page header, a section title, a
# summary band -- rather than an account. A real upload produced a tradeline whose creditor
# was "Your TransUnion Credit Report Personal", with a limit and a status invented from
# whatever numbers happened to be nearby. An imaginary account on a credit report is worse
# than a missing one: it can be disputed, reasoned about, and paid.
_NOT_A_CREDITOR = (
    "credit report", "personal information", "account summary", "report summary",
    "prepared for", "page ", "table of contents", "your report", "file number",
    "consumer statement", "score factors", "inquiries", "public records",
    "credit accounts", "account history", "revolving accounts", "installment accounts",
    "dispute", "contact us", "how to read", "glossary",
)


def looks_like_an_account(line: dict) -> bool:
    """Whether a parsed block is really a tradeline.

    Two rules, both learned from one bad import. The creditor must not read as report
    furniture, and there must be at least one money figure -- an account with no balance,
    no limit and nothing past due is not an account, it is a heading that happened to sit
    near a number.
    """
    creditor = (line.get("creditor") or "").lower()
    if not creditor or any(phrase in creditor for phrase in _NOT_A_CREDITOR):
        return False
    return any(line.get(field) is not None
               for field in ("balance", "credit_limit", "past_due"))


# How a report divides itself into accounts. Neither real bureau uses blank lines -- the
# first attempt split on them and got ONE block out of a 59,891-character TransUnion file,
# which is how a page header ended up stored as a tradeline. Both formats instead repeat a
# marker before every account, so that is what to anchor on.
_RECORD_MARKERS = (
    # TransUnion prints this header line above each account.
    re.compile(r"Account Name\s+Account Number", re.I),
    # Equifax repeats a "Confirmation # ..." page banner before each one.
    re.compile(r"Confirmation\s*#\s*\d+", re.I),
)

# Field labels, in both bureaus' wording. Most specific first: "High Credit" must be tried
# before "Credit", and "Amount Past Due" before "Past Due".
_FIELD_PATTERNS = {
    "balance": (r"Balance(?:\s*Amount)?",),
    "credit_limit": (r"Credit\s*Limit",),
    "high_balance": (r"High\s*(?:Balance|Credit)",),
    "past_due": (r"Amount\s*Past\s*Due", r"Past\s*Due"),
}

_TYPE_LABEL = re.compile(
    r"(?:Loan/Account\s*Type|Loan\s*Type|Account\s*Type)\s*:?\s*([A-Za-z /]{3,40})", re.I)
_STATUS_LABEL = re.compile(
    r"(?:Pay\s*Status|Account\s*Status|Status)\s*:?\s*([A-Za-z>,;'\- ]{3,48})", re.I)
_OPENED_LABEL = re.compile(r"Date\s*Opened\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.I)

# Equifax shows the last four (*1234); TransUnion shows the LEADING digits and masks the
# tail (123456789012****). Those leading digits are more sensitive than the last four and
# identify nothing useful, so they are never captured.
_LAST4_TAIL = re.compile(r"\*+\s*(\d{4})\b")
_MASKED_HEAD = re.compile(r"\b\d{6,}\*{2,}")


def split_records(text: str) -> list:
    """Cut the report into one string per account.

    Falls back to blank-line blocks only when neither marker is present, which is the
    hand-typed or CSV case rather than either real bureau export.
    """
    for marker in _RECORD_MARKERS:
        parts = marker.split(text)
        if len(parts) >= 3:                     # a header plus at least two accounts
            return [p for p in parts[1:] if p.strip()]
    return [b for b in re.split(r"\n\s*\n", text) if b.strip()]


def _labelled_money(record: str, patterns) -> float | None:
    """A money figure that belongs to its label.

    The gap between label and number is held to a few characters on purpose. A looser
    search walked past an empty field and collected the next number on the line: a dozen
    Equifax accounts came back with "past due $60" picked up from a neighbouring column
    while their balances were zero. An empty field must read as None, not as whatever
    number happens to be nearby.
    """
    for pattern in patterns:
        match = re.search(pattern + r"\s*:?\s{0,3}\$\s?([\d,]+(?:\.\d{2})?)", record, re.I)
        if match:
            return _money_to_float(match.group(1))
    return None


def _creditor_from(record: str) -> str | None:
    """The lender's name, which both formats put first in the record.

    Every run of digits is removed, not just masked ones. TransUnion prints the LEADING
    digits of the account number on the same line as the name and masks the tail, so a
    name-shaped capture was carrying real account digits into the database -- "CAPITAL ONE
    5178058035". The lender's name never legitimately contains a long number, so stripping
    them all costs nothing and closes that hole completely.
    """
    for raw in record.strip().splitlines():
        line = " ".join(raw.split())
        if not line or len(line) < 3:
            continue
        line = re.sub(r"\*+", " ", line)
        line = re.sub(r"\b\d{3,}\b", " ", line)          # any long number, masked or not
        # Chime prints a hex account id ("484D8DB018") beside the name. A lender name never
        # contains a long mixed alphanumeric token, so it is an identifier, not a name.
        line = re.sub(r"\b(?=[A-Za-z]*\d)[A-Za-z0-9]{6,}\b", " ", line)
        line = re.sub(r"\s+-\s+(Closed|Open|Paid).*$", "", line, flags=re.I)
        line = re.sub(r"\s{2,}.*$", "", line)
        line = " ".join(line.split()).strip(" -:")
        if len(line) < 3 or any(p in line.lower() for p in _NOT_A_CREDITOR):
            continue
        if re.fullmatch(r"[\d\W]+", line):
            continue
        return line[:60]
    return None


def parse_tradelines(text: str) -> dict:
    """Best-effort extraction of accounts from report text.

    Returns {"tradelines": [...], "unparsed": [...]}. The second list is not decoration:
    a report whose accounts could not be read must look different from a report with no
    accounts, or a parse failure becomes "your credit is clean".
    """
    tradelines, unparsed = [], []
    if not (text or "").strip():
        return {"tradelines": [], "unparsed": ["no readable text in the file"]}

    for record in split_records(text):
        flat = " ".join(record.split())
        if len(flat) < 40:
            continue

        creditor = _creditor_from(record)
        if not creditor:
            unparsed.append(redact(flat[:110]))
            continue

        # Only ever the LAST four, and only where the bureau prints them. TransUnion masks
        # the tail and shows leading digits instead; nothing is stored for those.
        tail = _LAST4_TAIL.search(flat)
        last4 = tail.group(1) if tail else None

        # The account type comes from the type LABEL only. Scanning the whole record for
        # keywords read Wells Fargo as a collection, because the dispute boilerplate every
        # Equifax record carries mentions collections.
        type_hit = _TYPE_LABEL.search(flat)
        type_text = (type_hit.group(1) if type_hit else "").lower()
        kind = next((k for word, k in _ACCOUNT_KINDS if word in type_text), None)
        status_hit = _STATUS_LABEL.search(flat)
        status = " ".join(status_hit.group(1).split())[:40] if status_hit else None
        if kind is None and status:
            # A charged-off or collection STATUS is a fact about the account, unlike a
            # stray word in the page furniture.
            kind = next((k for word, k in _ACCOUNT_KINDS if word in status.lower()), None)
        kind = kind or "other"

        limit = _labelled_money(flat, _FIELD_PATTERNS["credit_limit"])
        high = _labelled_money(flat, _FIELD_PATTERNS["high_balance"])
        # High balance is NOT a credit limit. Treating it as one made every collection
        # look like a maxed-out card -- limit equal to balance -- and utilisation is about
        # 30% of the score, so a wrong limit is a wrong strategy. Revolving accounts can
        # fall back to it as an estimate; nothing else can.
        if limit is None and kind == "credit_card":
            limit = high

        candidate = {
            "creditor": creditor,
            "account_last4": last4,
            "kind": kind,
            "status": status,
            "balance": _labelled_money(flat, _FIELD_PATTERNS["balance"]),
            "credit_limit": limit,
            "past_due": _labelled_money(flat, _FIELD_PATTERNS["past_due"]),
            "opened_on": (_OPENED_LABEL.search(flat).group(1)
                          if _OPENED_LABEL.search(flat) else None),
        }
        if looks_like_an_account(candidate):
            tradelines.append(candidate)
        else:
            unparsed.append(redact(flat[:110]))

    return {"tradelines": tradelines, "unparsed": unparsed}


def diagnose(text: str, parsed: dict) -> str:
    """Why an import found nothing, in language that points at the fix.

    "0 accounts parsed" is useless on its own -- a scanned PDF and an unrecognised layout
    produce the identical count and need completely different responses. The first needs a
    different export; the second needs the parser taught. Saying which is the difference
    between a dead end and a next step.
    """
    chars = len((text or "").strip())
    found = len(parsed.get("tradelines") or [])
    if chars == 0:
        return ("No text at all could be extracted. This is almost certainly a SCANNED or "
                "image-only PDF -- the pages are pictures, not text. Re-download it as a "
                "text/HTML export, or print-to-PDF from the bureau's web view, and the "
                "accounts will come through.")
    if found == 0:
        return (f"Read {chars:,} characters of text, but recognised no accounts in it. The "
                f"text came through fine, so this is a LAYOUT this parser has not been "
                f"taught yet -- it needs tuning against this bureau's format rather than a "
                f"different file.")
    return f"Read {chars:,} characters and recognised {found} account(s)."


def parse_report(path: str) -> dict:
    """Everything readable about one report file, with nothing sensitive retained."""
    text = extract_text(path)
    if not (text or "").strip():
        empty = {"tradelines": [], "unparsed": ["no text could be extracted"]}
        return {"bureau": None, "score": None, "file": os.path.basename(path),
                "chars": 0, "diagnosis": diagnose("", empty), **empty}
    parsed = parse_tradelines(text)
    return {"bureau": detect_bureau(text), "score": detect_score(text),
            "file": os.path.basename(path), "chars": len(text),
            "diagnosis": diagnose(text, parsed), **parsed}


# --- storage -----------------------------------------------------------------------------

def store_report(db_path: str, owner_user_id: int, parsed: dict, *, bureau: str | None = None,
                 pulled_on: str | None = None, source: str | None = None) -> dict:
    """Write a parsed report and its tradelines through personal_db.

    An unreadable bureau becomes "other" rather than failing the import: he can correct a
    label, and a stored report with the wrong name on it is worth far more than no report.
    A tradeline that the schema refuses is skipped and counted, never silently dropped --
    the count is what tells him the parse was imperfect.
    """
    from . import personal_db

    chosen = (bureau or parsed.get("bureau") or "other").lower()
    if chosen not in ("experian", "equifax", "transunion", "other"):
        chosen = "other"
    pulled = pulled_on or datetime.now(timezone.utc).date().isoformat()

    report_id = personal_db.add_credit_report(
        db_path, owner_user_id, bureau=chosen, pulled_on=pulled,
        score=parsed.get("score"), source=source or parsed.get("file"),
        notes=(f"Imported from {parsed.get('file')}. "
               f"{parsed.get('diagnosis') or ''}"))

    stored, rejected = 0, []
    for line in parsed.get("tradelines") or []:
        try:
            personal_db.add_tradeline(db_path, report_id, line["creditor"],
                                      **{k: v for k, v in line.items() if k != "creditor"})
            stored += 1
        except Exception as exc:                          # noqa: BLE001
            rejected.append(redact(f"{line.get('creditor')}: {exc}")[:120])

    return {"report_id": report_id, "bureau": chosen, "pulled_on": pulled,
            "score": parsed.get("score"), "tradelines_stored": stored,
            "chars_read": parsed.get("chars", 0),
            "diagnosis": parsed.get("diagnosis"),
            "rejected": rejected, "unparsed": parsed.get("unparsed") or []}


def import_file(db_path: str, owner_user_id: int, path: str, *, bureau: str | None = None,
                pulled_on: str | None = None, store_empty: bool = False) -> dict:
    """Parse one report file and store it. The whole path, for one file.

    A report that yielded no accounts is NOT stored by default, and that default matters
    more than it looks. credit.latest_report picks the newest row, so an unreadable
    upload -- a scanned Experian PDF, say -- silently becomes "the current picture" and
    shadows the twenty-eight accounts already parsed from the other two bureaus. The
    specialist would then be told the file is clean. A failed import is news to report,
    not a record to keep.
    """
    parsed = parse_report(path)
    if not parsed.get("tradelines") and not store_empty:
        return {"stored": False, "report_id": None, "file": os.path.basename(path),
                "bureau": bureau or parsed.get("bureau"), "score": parsed.get("score"),
                "tradelines_stored": 0, "chars_read": parsed.get("chars", 0),
                "diagnosis": parsed.get("diagnosis"), "rejected": [],
                "unparsed": parsed.get("unparsed") or []}
    result = store_report(db_path, owner_user_id, parsed, bureau=bureau,
                          pulled_on=pulled_on, source=os.path.basename(path))
    result["stored"] = True
    result["file"] = os.path.basename(path)
    return result


# --- progress ----------------------------------------------------------------------------

def _key(line: dict) -> tuple:
    """How a tradeline is recognised across reports.

    Creditor plus last four, because that pair survives the things that legitimately
    change month to month -- balance, status, even the reported account type -- while a
    balance-based key would treat every payment as a different account.
    """
    return ((line.get("creditor") or "").strip().lower(),
            (line.get("account_last4") or "").strip())


def _reports_with_lines(db_path: str, owner_user_id: int, bureau: str | None) -> list[dict]:
    """Every stored report, newest first, each with its tradelines.

    Reads directly rather than through credit.latest_report, which returns only the single
    newest -- comparison needs the one before it, which is exactly the row that function
    is designed to discard.
    """
    import sqlite3
    from contextlib import closing as _closing

    query = "SELECT * FROM credit_reports WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if bureau:
        query += " AND bureau = ?"
        params.append(bureau)
    query += " ORDER BY pulled_on DESC, id DESC"
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    with _closing(conn):
        reports = [dict(r) for r in conn.execute(query, params)]
        for report in reports:
            report["tradelines"] = [dict(t) for t in conn.execute(
                "SELECT * FROM credit_tradelines WHERE report_id = ?", (report["id"],))]
    return reports


def compare(db_path: str, owner_user_id: int, bureau: str | None = None) -> dict:
    """What changed between the two most recent reports -- the point of re-uploading.

    Returns the differences, not the state: "Capital One balance fell $412" is the thing
    worth seeing on a monitoring screen, and it is the one thing a single report can never
    tell you. With fewer than two reports it says so rather than inventing a baseline.
    """
    reports = _reports_with_lines(db_path, owner_user_id, bureau)
    if len(reports) < 2:
        return {"comparable": False,
                "reason": ("Only one report so far. Upload another after your next pull and "
                           "this will show exactly what moved between them."),
                "reports": len(reports)}

    newest, previous = reports[0], reports[1]
    now_lines = {_key(t): t for t in newest["tradelines"]}
    was_lines = {_key(t): t for t in previous["tradelines"]}

    appeared = [now_lines[k] for k in now_lines.keys() - was_lines.keys()]
    gone = [was_lines[k] for k in was_lines.keys() - now_lines.keys()]
    changed = []
    for key in now_lines.keys() & was_lines.keys():
        before, after = was_lines[key], now_lines[key]
        delta = (after.get("balance") or 0) - (before.get("balance") or 0)
        if abs(delta) >= 1 or (before.get("status") or "") != (after.get("status") or ""):
            changed.append({"creditor": after.get("creditor"),
                            "account_last4": after.get("account_last4"),
                            "balance_from": before.get("balance"),
                            "balance_to": after.get("balance"),
                            "balance_delta": round(delta, 2),
                            "status_from": before.get("status"),
                            "status_to": after.get("status")})

    total_before = sum(t.get("balance") or 0 for t in was_lines.values())
    total_after = sum(t.get("balance") or 0 for t in now_lines.values())
    return {
        "comparable": True,
        "from": {"id": previous["id"], "pulled_on": previous["pulled_on"],
                 "bureau": previous["bureau"], "score": previous.get("score")},
        "to": {"id": newest["id"], "pulled_on": newest["pulled_on"],
               "bureau": newest["bureau"], "score": newest.get("score")},
        "score_delta": ((newest.get("score") or 0) - (previous.get("score") or 0)
                        if newest.get("score") and previous.get("score") else None),
        "balance_delta": round(total_after - total_before, 2),
        "accounts_appeared": appeared,
        "accounts_gone": gone,
        "accounts_changed": sorted(changed, key=lambda c: abs(c["balance_delta"] or 0),
                                   reverse=True),
    }
