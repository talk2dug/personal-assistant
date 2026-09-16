"""Clears the business team's creative work so the pipeline can start over.

Run by hand, never by an agent. This deletes real rows and there is no undo beyond the
backup it takes first -- which is why it is a script you have to type rather than a tool
something could call. It prints what it is about to do and refuses to proceed without
--yes.

What it removes: product concepts, art briefs, store listings, social posts, and the
PENDING approval cards that point at them (with their rendered options).

What it keeps, deliberately:

  Trend and market leads. The trend feed is what the Product Creator works from and the
  market leads are real dated events with booth fees that someone researched. Clearing
  the chain without clearing the feed is the usual intent: same raw material, fresh
  attempt. Pass --leads to clear the trend feed too.

  DECIDED approval cards. These are the only record of what the owner thinks -- 22
  approved and 11 rejected concepts, and rejection notes like "I already have this
  created. No need to make it again", which is a rule about his workshop that no amount
  of prompting would recover. review_examples feeds them back into every run, and they
  are addressed by source_agent rather than by the row they referenced, so they keep
  working after the row is gone. All four tables use AUTOINCREMENT, so a fresh concept
  can never inherit a deleted one's id and pick up a stale verdict.

Usage:
    python -m scripts.reset_pipelines            # dry run, shows the counts
    python -m scripts.reset_pipelines --yes      # do it
    python -m scripts.reset_pipelines --yes --leads --verdicts   # everything
"""
import argparse
import shutil
import sqlite3
import sys
import time
from contextlib import closing

from assistant.config import load_config

CHAIN_TABLES = ("social_posts", "store_listings", "art_briefs", "product_concepts")


def _counts(conn, owner_user_id: int) -> dict:
    out = {}
    for table in CHAIN_TABLES:
        out[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE owner_user_id = ?", (owner_user_id,)).fetchone()[0]
    marks = ",".join("?" * len(CHAIN_TABLES))
    for label, clause in (("review_items (pending)", "status = 'pending'"),
                          ("review_items (decided)", "status != 'pending'")):
        out[label] = conn.execute(
            f"SELECT COUNT(*) FROM review_items WHERE owner_user_id = ? AND {clause} "
            f"AND ref_table IN ({marks})", (owner_user_id, *CHAIN_TABLES)).fetchone()[0]
    out["trend_leads"] = conn.execute(
        "SELECT COUNT(*) FROM trend_leads WHERE owner_user_id = ?", (owner_user_id,)).fetchone()[0]
    return out


def reset(db_path: str, owner_user_id: int, clear_leads: bool = False,
          clear_verdicts: bool = False) -> dict:
    marks = ",".join("?" * len(CHAIN_TABLES))
    removed = {}
    with closing(sqlite3.connect(db_path)) as conn:
        # Options first: they are children of review_items, and this schema does not have
        # foreign keys switched on, so ON DELETE CASCADE would not fire.
        # Keeping the decided cards means keeping their options too -- which option he
        # picked is part of the verdict, not decoration.
        status_clause = "" if clear_verdicts else " AND i.status = 'pending'"
        removed["review_options"] = conn.execute(
            f"DELETE FROM review_options WHERE item_id IN ("
            f"  SELECT i.id FROM review_items i WHERE i.owner_user_id = ?"
            f"  AND i.ref_table IN ({marks}){status_clause})",
            (owner_user_id, *CHAIN_TABLES)).rowcount
        removed["review_items"] = conn.execute(
            f"DELETE FROM review_items WHERE owner_user_id = ? "
            f"AND ref_table IN ({marks})"
            + ("" if clear_verdicts else " AND status = 'pending'"),
            (owner_user_id, *CHAIN_TABLES)).rowcount

        for table in CHAIN_TABLES:
            removed[table] = conn.execute(
                f"DELETE FROM {table} WHERE owner_user_id = ?", (owner_user_id,)).rowcount

        if clear_leads:
            removed["trend_leads"] = conn.execute(
                "DELETE FROM trend_leads WHERE owner_user_id = ?", (owner_user_id,)).rowcount
        conn.commit()
    return removed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually delete (otherwise dry run)")
    parser.add_argument("--leads", action="store_true", help="also clear the trend feed")
    parser.add_argument("--verdicts", action="store_true",
                        help="also clear DECIDED cards (the team's taste memory)")
    parser.add_argument("--owner", type=int, default=1, help="owner user id (default 1)")
    args = parser.parse_args(argv)

    cfg = load_config()
    with closing(sqlite3.connect(cfg.db_path)) as conn:
        before = _counts(conn, args.owner)

    print("Currently in the pipelines:")
    for name, n in before.items():
        keep = ""
        if name == "trend_leads" and not args.leads:
            keep = "   (KEPT — pass --leads to clear)"
        if name == "review_items (decided)" and not args.verdicts:
            keep = "   (KEPT — the team's taste memory; --verdicts to clear)"
        print(f"  {name:24} {n:5}{keep}")

    if not args.yes:
        print("\nDry run. Re-run with --yes to delete.")
        return 0

    backup = cfg.db_path.replace(".db", f".backup-{time.strftime('%Y%m%d-%H%M%S')}.db")
    shutil.copy2(cfg.db_path, backup)
    print(f"\nBacked up to {backup}")

    removed = reset(cfg.db_path, args.owner, clear_leads=args.leads,
                    clear_verdicts=args.verdicts)
    print("Removed:")
    for name, n in removed.items():
        print(f"  {name:24} {n:5}")
    print("\nDone. The team starts over on its next pipeline tick.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
