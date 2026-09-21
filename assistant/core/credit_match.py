"""Matching the same account across three bureaus that each spell it differently.

The three reports describe one financial life, but nothing joins them. TransUnion truncates
names to a fixed width ("BRIDGECRES"), Equifax writes the legal entity ("Bridgecrest Credit
Company"), and Experian writes the consumer brand ("DRIVETIME/BRIDGECREST"). Comparing them
by name said 23 accounts appeared on only one bureau, which was nonsense -- most were one
account wearing three names.

That number mattered, which is why this exists: "reported by Experian but not TransUnion"
is a *dispute*, and a dispute built on a naming artefact wastes a real letter and the 30-day
clock that comes with it.

Matching is deliberately graded rather than boolean. A confident match and a guess are both
useful, but only if the caller can tell them apart:

    exact   -- the canonical names are identical
    prefix  -- one is a truncation of the other, which is TransUnion's whole habit
    token   -- they share a distinctive word, which is the weakest and can over-merge
               (CHIME/STRIDE BANK and CHIME/THE BANCORP BANK are different accounts that
               share "CHIME"), so last-four digits break the tie wherever a bureau prints
               them and the confidence is reported honestly when they do not.

Nothing here decides anything. It groups, labels how sure it is, and leaves the judgement
to whoever reads it.
"""
import re
from collections import defaultdict

# Words that appear in everybody's name and so distinguish nobody. Dropping them is what
# makes "BRIDGECREST CREDIT COMPANY" and "DRIVETIME/BRIDGECREST" comparable at all.
GENERIC_TOKENS = {
    "BANK", "BANKS", "NA", "NATIONAL", "INC", "LLC", "CO", "CORP", "CORPORATION",
    "COMPANY", "SERVICES", "SERVICE", "SYSTEMS", "SYSTEM", "CREDIT", "FINANCIAL",
    "FINANCE", "CARD", "CARDS", "USA", "US", "THE", "OF", "AND", "GROUP", "MANAGEMENT",
    "RECOVERY", "COLLECTION", "COLLECTIONS", "ACCEPTANCE", "FUNDING", "LENDING",
    "LOAN", "LOANS", "ASSOC", "ASSOCIATES", "LP", "LTD", "TRUST", "FED", "FEDERAL",
    "SELFREPORTED", "SELF", "REPORTED",
}

# Below this a shared or truncated token is too common to mean anything -- "US" and "CARD"
# would otherwise merge half the report.
MIN_TOKEN = 5
MIN_PREFIX = 6


# Bureau shorthand for the same issuer. Each bureau abbreviates differently and none of
# them is derivable from the others, so this is a lookup rather than a rule: WFBNA is
# Wells Fargo Bank NA, and no amount of tokenising gets you there.
ALIASES = {
    "WFBNA": "WELLSFARGO",
    "WF": "WELLSFARGO",
    "WELLSFARGO": "WELLSFARGO",
    "SYNCB": "SYNCHRONY",
    "SYNCHRONYBANK": "SYNCHRONY",
    "JPMCB": "CHASE",
    "CHASECARD": "CHASE",
    "CBNA": "CITI",
    "CITIBANK": "CITI",
    "COMENITYBA": "COMENITY",
    "COMENITYBANK": "COMENITY",
    "CHIMEFIN": "CHIME",
    "DRIVETIME": "BRIDGECREST",
    "CAPONE": "CAPITALONE",
}

# Brands that are two words. Tokenising splits them, and then a single common word does
# the matching: "CAPITAL ONE" and "JEFFERSON CAPITAL SYSTEMS" share "CAPITAL" and were
# merged into one account. Fusing the phrase first means the brand matches as a brand.
PHRASE_ALIASES = {
    "CAPITAL ONE": "CAPITALONE",
    "WELLS FARGO": "WELLSFARGO",
    "JEFFERSON CAPITAL": "JEFFERSONCAPITAL",
    "I C SYSTEMS": "ICSYSTEM",
    "I C SYSTEM": "ICSYSTEM",
    "ONLINE INFORMATION": "ONLINEINFORMATION",
    "CREDIT COLLECTION": "CREDITCOLLECTION",
    "EXPRESS RECOVERY": "EXPRESSRECOVERY",
    "MISSION LANE": "MISSIONLANE",
}

# Account types that genuinely exclude one another. Everything else is permissive: a
# charged-off credit card is reported as a "collection" by one bureau and a "credit card"
# by another, and that disagreement is the very thing worth disputing -- refusing to match
# them would hide it.
_EXCLUSIVE_KINDS = {"credit_card", "auto", "mortgage", "student_loan"}

# Kinds that can never be the same account. An auto loan and a credit card share a lender
# constantly -- Capital One issues both -- and merging them produced one group of ten rows
# that claimed to be a single account.
_KIND_UNKNOWN = {None, "", "other"}


def _kinds_compatible(a: str | None, b: str | None) -> bool:
    if a in _KIND_UNKNOWN or b in _KIND_UNKNOWN:
        return True                      # unknown type cannot contradict anything
    if a in _EXCLUSIVE_KINDS and b in _EXCLUSIVE_KINDS:
        return a == b                    # an auto loan is never a credit card
    return True


def _apply_aliases(words: list) -> list:
    return [ALIASES.get(w, w) for w in words]


def tokens(name: str) -> list:
    """The meaningful words in a creditor name, in order.

    Splits on punctuation as well as spaces because the bureaus use "/" and "-" to join
    a brand to its issuing bank ("CHIME/STRIDE BANK", "DRIVETIME/BRIDGECREST"), and maps
    each bureau's shorthand onto one spelling.
    """
    flat = re.sub(r"[^A-Za-z0-9]+", " ", (name or "").upper()).strip()
    # Space-padded replacement rather than a word-boundary regex: the phrases are literal
    # and this cannot be tripped by escaping, which it was.
    padded = " " + flat + " "
    for phrase, fused in PHRASE_ALIASES.items():
        padded = padded.replace(" " + phrase + " ", " " + fused + " ")
    flat = padded.strip()
    words = [w for w in flat.split() if w and not w.isdigit()]
    words = _apply_aliases(words)
    meaningful = [w for w in words if w not in GENERIC_TOKENS]
    # If every word was generic, the generic words ARE the name -- "CREDIT COLLECTION
    # SERVICES" is a real company, and returning nothing for it would group it with every
    # other all-generic name in the file.
    return meaningful or words


def canonical(name: str) -> str:
    return "".join(tokens(name))


def _tokens_match(a: list, b: list) -> str | None:
    """How two token lists relate, or None if they do not. Strongest answer wins."""
    if not a or not b:
        return None
    set_a, set_b = set(a), set(b)
    if a == b or set_a == set_b:
        return "exact"

    # TransUnion truncates names to a fixed width, so a prefix is its signature rather
    # than a coincidence.
    for x in a:
        for y in b:
            if x != y and len(x) >= MIN_PREFIX and len(y) >= MIN_PREFIX:
                if x.startswith(y) or y.startswith(x):
                    return "prefix"

    shared = {w for w in set_a & set_b if len(w) >= MIN_TOKEN}
    if not shared:
        return None
    # A shared word is only evidence if it is most of the shorter name. "CAPITAL ONE" and
    # "JEFFERSON CAPITAL SYSTEMS" share exactly one common corporate word and are not
    # remotely the same company; requiring it to carry the name stops that.
    shorter = min(len(set_a), len(set_b))
    if len(shared) * 2 >= shorter:
        return "token"
    return None


def group_accounts(rows: list) -> list:
    """Group tradelines that look like the same account across bureaus.

    `rows` are dicts carrying at least `creditor`, `bureau`, and ideally `account_last4`,
    `kind` and `balance`. Returns one entry per apparent account with the bureaus that
    report it, how confident the grouping is, and the balances each bureau states.

    Three separators keep distinct accounts apart, and all three were needed against the
    real reports: different last-four digits, incompatible account types, and one member
    per bureau -- a bureau listing an account twice is listing two accounts, which is how
    the two I C System collections and the three Capital One cards stay separate.
    """
    groups: list = []
    for row in rows:
        row_tokens = tokens(row.get("creditor"))
        last4 = (row.get("account_last4") or "").strip() or None
        kind = row.get("kind")
        bureau = row.get("bureau")
        placed = False
        for group in groups:
            relation = _tokens_match(row_tokens, group["tokens"])
            if not relation:
                continue
            members = group["members"]
            known = {m.get("account_last4") for m in members
                     if (m.get("account_last4") or "").strip()}
            if last4 and known and last4 not in known:
                continue
            if not all(_kinds_compatible(kind, m.get("kind")) for m in members):
                continue
            same_bureau = [m for m in members if m.get("bureau") == bureau]
            if same_bureau and not any(m.get("balance") == row.get("balance")
                                       for m in same_bureau):
                continue
            members.append(row)
            group["relations"].add(relation)
            if len(row_tokens) > len(group["tokens"]):
                group["tokens"] = row_tokens
            if len(row.get("creditor") or "") > len(group["name"]):
                group["name"] = row.get("creditor") or group["name"]
            if kind not in _KIND_UNKNOWN:
                group["kind"] = kind
            placed = True
            break
        if not placed:
            groups.append({"name": row.get("creditor") or "(unnamed)",
                           "tokens": row_tokens, "members": [row],
                           "kind": kind, "relations": {"exact"}})

    out = []
    for group in groups:
        members = group["members"]
        bureaus = sorted({m.get("bureau") for m in members if m.get("bureau")})
        balances = {m.get("bureau"): m.get("balance") for m in members}
        stated = [b for b in balances.values() if b is not None]
        confidence = ("exact" if group["relations"] == {"exact"}
                      else "token" if "token" in group["relations"] else "prefix")
        out.append({
            "creditor": group["name"],
            "key": "".join(group["tokens"]),
            "kind": group.get("kind") or "other",
            "bureaus": bureaus,
            "confidence": confidence if len(members) > 1 else "single",
            "balances": balances,
            "balances_agree": len(set(stated)) <= 1,
            "members": members,
        })
    return sorted(out, key=lambda g: (-len(g["bureaus"]), g["creditor"]))


def cross_bureau(db_path: str, owner_user_id: int) -> dict:
    """Every account across the newest report from each bureau, grouped and compared.

    The two findings worth a letter come out of this: an account one bureau reports and
    another does not, and an account all three report with different balances. Both are
    ordinary grounds for a dispute, and neither is visible from a single report.
    """
    import sqlite3
    from contextlib import closing

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    with closing(conn):
        latest = {r["bureau"]: r["id"] for r in conn.execute(
            """SELECT bureau, id FROM credit_reports WHERE owner_user_id = ?
                   ORDER BY pulled_on ASC, id ASC""", (owner_user_id,))}
        rows = []
        for bureau, report_id in latest.items():
            for t in conn.execute("SELECT * FROM credit_tradelines WHERE report_id = ?",
                                  (report_id,)):
                row = dict(t)
                row["bureau"] = bureau
                rows.append(row)

    groups = group_accounts(rows)
    covered = sorted(latest)
    missing, disagreeing = [], []
    for group in groups:
        absent = [b for b in covered if b not in group["bureaus"]]
        if absent and len(covered) > 1:
            missing.append({**group, "missing_from": absent})
        if len(group["bureaus"]) > 1 and not group["balances_agree"]:
            disagreeing.append(group)

    return {
        "bureaus": covered,
        "accounts": len(groups),
        "tradelines": len(rows),
        "reported_by_all": [g for g in groups if len(g["bureaus"]) == len(covered)],
        "missing_from_some": missing,
        "balances_disagree": disagreeing,
        "groups": groups,
    }
