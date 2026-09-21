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


def detect_score(text: str):
    """A score if the report states one. Bounded to the real FICO/Vantage range so a
    stray four-digit number or a year is not read as a credit score."""
    # [^\n] rather than \D between the label and the number: a lazy non-digit
    # scan stops dead on the "3" of "VantageScore 3.0: 642" and never reaches the score.
    for pattern in (r"(?:FICO|VantageScore|Vantage|credit)[^\n]{0,30}?\b(\d{3})\b",
                    r"\bscore\b[^\n]{0,20}?\b(\d{3})\b"):
        for match in re.finditer(pattern, text or "", re.I):
            value = int(match.group(1))
            if 300 <= value <= 900:
                return value
    return None


def parse_tradelines(text: str) -> dict:
    """Best-effort extraction of accounts from report text.

    Returns {"tradelines": [...], "unparsed": [...]}. The second list is not decoration:
    a report whose accounts could not be read must look different from a report with no
    accounts, or a parse failure becomes "your credit is clean".
    """
    tradelines, unparsed = [], []
    if not text:
        return {"tradelines": [], "unparsed": ["no readable text in the file"]}

    # Reports put one account per block, separated by blank lines or a creditor heading.
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        chunk = " ".join(block.split())
        if len(chunk) < 25:
            continue
        low = chunk.lower()
        if not any(w in low for w in ("account", "balance", "creditor", "opened",
                                      "credit limit", "high balance", "status")):
            continue

        last4 = None
        masked = _MASKED_TAIL.search(chunk)
        if masked:
            last4 = masked.group(1)
        else:
            tail = re.search(r"account\s*(?:#|number|no\.?)?\s*[:\-]?\s*[\dxX*#-]*?(\d{4})\b",
                             chunk, re.I)
            last4 = tail.group(1) if tail else None

        creditor = None
        name = re.search(r"^([A-Z][A-Za-z0-9 &'./-]{2,40})(?=\s|$)", chunk)
        if name:
            creditor = name.group(1).strip(" .-")
        if not creditor:
            unparsed.append(redact(chunk[:110]))
            continue

        balance = _money_to_float(
            (re.search(rf"balance\D{{0,18}}{_MONEY}", chunk, re.I) or [None, None])[1]
            if re.search(rf"balance\D{{0,18}}{_MONEY}", chunk, re.I) else None)
        limit_match = re.search(rf"(?:credit limit|high balance|limit)\D{{0,18}}{_MONEY}",
                                chunk, re.I)
        credit_limit = _money_to_float(limit_match.group(1)) if limit_match else None
        past_due_match = re.search(rf"past due\D{{0,18}}{_MONEY}", chunk, re.I)
        past_due = _money_to_float(past_due_match.group(1)) if past_due_match else None

        kind = next((k for word, k in _ACCOUNT_KINDS if word in low), "other")
        status = next((w for w in _STATUS_WORDS if w in low), None)
        opened = re.search(r"opened\D{0,16}(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})",
                           chunk, re.I)

        tradelines.append({
            "creditor": creditor[:60],
            "account_last4": last4,
            "kind": kind,
            "status": status,
            "balance": balance,
            "credit_limit": credit_limit,
            "past_due": past_due,
            "opened_on": opened.group(1) if opened else None,
        })

    return {"tradelines": tradelines, "unparsed": unparsed}


def parse_report(path: str) -> dict:
    """Everything readable about one report file, with nothing sensitive retained."""
    text = extract_text(path)
    if not (text or "").strip():
        return {"bureau": None, "score": None, "tradelines": [], "file": os.path.basename(path),
                "unparsed": ["no text could be extracted -- if this is a scanned PDF it "
                             "needs an export with selectable text, or the accounts typed in"]}
    parsed = parse_tradelines(text)
    return {"bureau": detect_bureau(text), "score": detect_score(text),
            "file": os.path.basename(path), "chars": len(text), **parsed}


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
        notes=(f"Imported from {parsed.get('file')}; "
               f"{len(parsed.get('tradelines') or [])} accounts parsed, "
               f"{len(parsed.get('unparsed') or [])} blocks unreadable."))

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
            "rejected": rejected, "unparsed": parsed.get("unparsed") or []}


def import_file(db_path: str, owner_user_id: int, path: str, *, bureau: str | None = None,
                pulled_on: str | None = None) -> dict:
    """Parse one report file and store it. The whole path, for one file."""
    parsed = parse_report(path)
    result = store_report(db_path, owner_user_id, parsed, bureau=bureau,
                          pulled_on=pulled_on, source=os.path.basename(path))
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
