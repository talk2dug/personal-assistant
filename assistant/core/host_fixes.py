"""The whitelist: a small, explicit, human-curated set of remedies that may run against a
live host without asking first.

This is deliberately NOT a general command-execution path. ssh_ops.py's own docstring
explains why run_command is never exposed as a raw chat tool -- the owner's requirement
for server changes is a *plan* he approves once (ops_plans.py), not a yes/no per command,
and exposing it directly would let a model route around that by just calling it
repeatedly. Nothing here weakens that gate. Anything NOT on this whitelist still goes
through the normal propose_ops_plan review flow, same as it does today.

What makes a whitelisted fix different is that it is pre-approved BY JACK, in this file,
ahead of time, for one specific, narrow, known-safe situation -- not decided by a model at
the moment something looks broken. Starts empty. Which fixes belong here is a real policy
call about what's safe to run unattended against a live box, and that's his call, not
something to guess broadly.
"""
import logging

from . import host_health

logger = logging.getLogger(__name__)

# host_name -> fix. Add entries here once a specific, recurring failure has a specific,
# known-safe remedy -- e.g.:
#
# WHITELISTED_FIXES = {
#     "jarvisbox": {
#         "id": "restart_jarvisweb",
#         "when": "JarvisWeb port unreachable",
#         "command": "powershell -Command \"Restart-Service -Name JarvisWeb -Force\"",
#         "description": "Restarts the JarvisWeb Windows service.",
#     },
# }
WHITELISTED_FIXES: dict[str, dict] = {}


def attempt_fix(db_path: str, ssh_ops, host_name: str, now: str | None = None) -> dict | None:
    """Runs the whitelisted fix for this host, if one exists. Returns None (no attempt
    made) when there's nothing whitelisted for this host, or ssh_ops isn't available.

    Never touches ops_plans or the review queue -- a whitelisted fix is pre-approved by
    definition, so routing it through the one-at-a-time human review flow would just be
    friction for something Jack already signed off on when he added the entry.
    """
    fix = WHITELISTED_FIXES.get(host_name)
    if fix is None or ssh_ops is None:
        return None
    try:
        result = ssh_ops.run_command(host_name, fix["command"])
        outcome = "ok" if result.get("ok") else f"failed (exit {result.get('exit_code')})"
    except Exception as e:                                           # noqa: BLE001
        logger.exception("whitelisted fix %s for %s failed to run", fix["id"], host_name)
        result = {"ok": False, "output": f"{type(e).__name__}: {e}"}
        outcome = "errored"
    host_health.record_fix_attempt(db_path, host_name, fix["id"], outcome, now=now)
    logger.info("host_fixes: ran %s on %s -> %s", fix["id"], host_name, outcome)
    return {"id": fix["id"], "description": fix.get("description", ""),
            "outcome": outcome, "result": result}
