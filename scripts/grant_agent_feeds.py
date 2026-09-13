"""Grant the vault-backed data feeds to the employees that should hold them.

    python scripts/grant_agent_feeds.py --dry-run     # show what would change
    python scripts/grant_agent_feeds.py              # apply

Idempotent and additive: an employee that already holds a feed is left alone, and no feed
is ever removed here. Safe to re-run after any deploy.

Why a script rather than inference in `hire()`: `staff.set_data_feeds` is explicit by
design, and this codebase has already been bitten once by inferring a feed from job-
description wording (the research analyst picked up `paper` -- a tradeable ledger -- from
a line describing who it reported to). Keeping the grant as a deliberate, reviewable action
preserves that rule. It also means the roster stays inspectable: what an employee can see
is a row in a table, not a keyword match nobody can reproduce.

Two feeds are granted here, and the difference in who gets them is the whole point:

  * `policy` (agent_policy.py) -- READ-ONLY. Reads the owner's four hand-written
    engineering policy notes into the prompt and writes nothing anywhere. Granted broadly,
    to every active engineering employee, because the cost of a wrong grant is prompt
    budget and the cost of a missing one is an employee breaking a rule it was never
    shown. The policy notes themselves name specific roles (backend work routes to
    heavy_backend_systems_engineer; a PR goes to senior_software_tester), so an engineer
    that cannot see them cannot follow them.

  * `journal` (crypto_journal.py) -- WRITES NOTES the owner reads. Granted narrowly, to
    exactly the four engineering employees with a real run history:
    senior_software_developer (39 runs), senior_software_architect (11),
    senior_react_engineer (3), systems_engineer (3). Every other engineering employee has
    run 0-2 times ever; a journal for those is a folder of near-empty notes nobody reads,
    which devalues the ones that matter. This is the owner's own call and the live run
    counts bear it out.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from assistant.config import load_config  # noqa: E402
from assistant.core import staff  # noqa: E402

# Read-only policy retrieval: every active engineering employee.
POLICY_DEPARTMENTS = ("engineering",)

# Writes notes into the vault: only employees that actually run. See the module docstring.
JOURNAL_KEYS = (
    "senior_software_developer",
    "senior_software_architect",
    "senior_react_engineer",
    "systems_engineer",
)


def plan(db_path: str, only: str = "all") -> list[dict]:
    """What would change. Pure read -- nothing here touches the database.

    `only` narrows to one feed, which matters during a staged rollout: the journal grant
    must not land before the code that knows how to write an engineering journal entry,
    or four employees start emitting blocks nothing files.
    """
    changes = []
    for emp in staff.list_staff(db_path):
        if emp["status"] != "active":
            continue
        held = {f.strip() for f in (emp.get("data_feeds") or "").split(",") if f.strip()}
        wanted = set(held)
        if only in ("all", "policy") and emp.get("department") in POLICY_DEPARTMENTS:
            wanted.add("policy")
        if only in ("all", "journal") and emp["key"] in JOURNAL_KEYS:
            wanted.add("journal")
        if wanted != held:
            changes.append({
                "key": emp["key"], "title": emp["title"],
                "from": ",".join(sorted(held)) or "(none)",
                "to": ",".join(sorted(wanted)),
                "adding": ",".join(sorted(wanted - held)),
            })
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show changes without applying")
    parser.add_argument("--db", default=None, help="database path (defaults to config.json)")
    parser.add_argument("--only", choices=("all", "policy", "journal"), default="all",
                        help="grant just one feed, for a staged rollout")
    args = parser.parse_args()

    db_path = args.db or load_config().db_path
    changes = plan(db_path, only=args.only)
    if not changes:
        print("Nothing to do — every employee already holds the feeds it should.")
        return 0

    for change in changes:
        print(f"  {change['key']:<40s} {change['from']:<22s} -> {change['to']}"
              f"   (+{change['adding']})")
    if args.dry_run:
        print(f"\n{len(changes)} employee(s) would change. Re-run without --dry-run to apply.")
        return 0

    for change in changes:
        staff.set_data_feeds(db_path, change["key"], change["to"])
    print(f"\nApplied to {len(changes)} employee(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
