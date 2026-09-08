"""GitHub PR/CI watchdog cache: one poller (refresh) diffs every open PR's real status
against what was last seen and records genuine transitions -- a check flipping to
failure, a PR becoming mergeable/unmergeable, CI finishing, a PR getting merged or
closed -- for the scheduler to hand to Jarvis. See docs/watchdog-system-design.md
section 2's "Option C": this polls GitHub's REST API (via the same GitOpsClient every
dev-team git tool already uses, so no second GitHub credential) rather than standing up
public webhook ingress, which the doc rules out as disproportionate cost for a
Tailscale-private deployment with no public ingress today.

Same shape as market_data.py: a local cache table a poller fills, diffed against
what was there before, so "what changed" is a stored fact rather than something
re-derived from a single snapshot every time.
"""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

SCHEMA = """
-- Latest known state per PR, plus when it last actually changed. One row per PR
-- number, overwritten each poll -- github_pr_state.state = 'closed' with merged = 1
-- is itself the "this PR is done" signal, not a separate table.
CREATE TABLE IF NOT EXISTS github_pr_state (
    pr_number INTEGER PRIMARY KEY,
    title TEXT,
    url TEXT,
    state TEXT NOT NULL,
    merged INTEGER NOT NULL DEFAULT 0,
    -- Tri-state: GitHub itself returns null while it's still computing mergeability.
    mergeable INTEGER,
    -- One word for the PR's overall CI state (see summarize_checks) rather than storing
    -- every individual check run -- what the watchdog needs is "did this change", and a
    -- one-word summary is enough to diff against next poll.
    checks_conclusion TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_changed_at TEXT
);
"""


def init_github_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def summarize_checks(checks: list[dict]) -> str:
    """One word for a PR's overall CI state. Failure beats pending beats success, since
    a still-running check next to an already-failed one is still worth flagging as a
    failure, not "pending"."""
    if not checks:
        return "none"
    if any(c.get("conclusion") in ("failure", "timed_out", "cancelled") for c in checks):
        return "failure"
    if any(c.get("status") != "completed" for c in checks):
        return "pending"
    if all(c.get("conclusion") == "success" for c in checks):
        return "success"
    return "mixed"


def _signature(status: dict) -> dict:
    return {
        "state": status["state"],
        "merged": bool(status["merged"]),
        "mergeable": status.get("mergeable"),
        "checks_conclusion": summarize_checks(status.get("checks") or []),
    }


def _get_state(conn, pr_number: int):
    row = conn.execute(
        "SELECT * FROM github_pr_state WHERE pr_number = ?", (pr_number,)).fetchone()
    return dict(row) if row else None


def refresh(db_path: str, git_ops_client) -> dict:
    """One poll: fetch every open PR's real status, plus the final status of any PR
    that WAS tracked as open and has since dropped out of the open list (that drop is
    itself the merged/closed transition, and list_open_prs alone would silently miss
    it -- it only ever returns what's currently open).

    Returns a summary rather than raising, so a scheduled job that hits a network blip
    records nothing and tries again next tick instead of taking down the scheduler
    (same reasoning as market_data.refresh).
    """
    open_prs = git_ops_client.list_open_prs()
    if not isinstance(open_prs, list):
        return {"ok": False, "error": f"unexpected list_open_prs response: {str(open_prs)[:200]}"}

    now = _now()
    titles = {pr["number"]: pr.get("title") for pr in open_prs}
    open_numbers = set(titles)
    changes = []
    with closing(_connect(db_path)) as conn:
        previously_open = {
            r["pr_number"] for r in
            conn.execute("SELECT pr_number FROM github_pr_state WHERE state = 'open'")
        }

        for pr_number in open_numbers | (previously_open - open_numbers):
            status = git_ops_client.get_pr_status(pr_number)
            if not status.get("ok"):
                continue
            new_sig = _signature(status)
            previous = _get_state(conn, pr_number)
            title = titles.get(pr_number) or (previous or {}).get("title")
            first_seen = (previous or {}).get("first_seen") or now

            # First time ever seeing this PR: nothing to compare against, so recording
            # it is not itself a "change" -- same guard market_data.refresh's `had_any`
            # uses to avoid reporting an entire first poll as all-new listings.
            changed = previous is not None and (
                previous["state"] != new_sig["state"]
                or bool(previous["merged"]) != new_sig["merged"]
                or previous["mergeable"] != new_sig["mergeable"]
                or previous["checks_conclusion"] != new_sig["checks_conclusion"]
            )
            last_changed_at = now if changed else (previous or {}).get("last_changed_at")

            conn.execute(
                """INSERT INTO github_pr_state
                       (pr_number, title, url, state, merged, mergeable, checks_conclusion,
                        first_seen, last_seen, last_changed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pr_number) DO UPDATE SET
                       title=excluded.title, url=excluded.url, state=excluded.state,
                       merged=excluded.merged, mergeable=excluded.mergeable,
                       checks_conclusion=excluded.checks_conclusion,
                       last_seen=excluded.last_seen, last_changed_at=excluded.last_changed_at""",
                (pr_number, title, status["url"], new_sig["state"], int(new_sig["merged"]),
                 new_sig["mergeable"], new_sig["checks_conclusion"], first_seen, now, last_changed_at),
            )
            if changed:
                changes.append({
                    "pr_number": pr_number, "title": title, "url": status["url"],
                    "previous": {
                        "state": previous["state"], "merged": bool(previous["merged"]),
                        "mergeable": previous["mergeable"], "checks_conclusion": previous["checks_conclusion"],
                    },
                    "now": new_sig,
                })
        conn.commit()
    return {"ok": True, "changes": changes}
