"""The executive tick: the component that wants the missions to win.

missions.py measures and diagnoses; attention.py decides what is worth saying. This is
the part that does something about it, and it is deliberately the only part that may act
on its own initiative.

Jack, 2026-09-21, on the store sitting dead for a week: *"i have not been told why, and i
have not been told its being fixed."* Both halves are this module's job, in that order --
work out why, fix what it can, and only then say something.

WHAT IT MAY DO WITHOUT ASKING (his decision, 2026-09-21): anything reversible. Running an
agent, drafting a concept, rendering art, writing a listing or a post -- all of it
produces something that can be deleted, and none of it spends money or reaches another
person. Money out the door, anything irreversible, and anything sent in his name still
wait for him. The reasoning he accepted: proactive plus ask-permission-for-everything is
just more texts, which is the disease.

THE MOVE THAT MATTERS is the boring one. The pipeline runs every twelve hours, so a stage
that has work waiting sits on it for up to twelve hours before touching it, and a stage
that skips costs another twelve. Five stages of that is days per product in the good case.
When this finds work sitting, it runs that stage NOW. Most of "keeping the ball rolling"
turns out to be refusing to wait for the next tick.
"""
import logging
from datetime import datetime, timedelta, timezone

from . import attention, missions

logger = logging.getLogger(__name__)

STORE_MISSION = "store_products_live"
CRYPTO_MISSION = "crypto_pnl_30d"

# Most upstream first. The order is the diagnosis: the first stage in this list that is
# not producing is the cause, and everything after it is a symptom.
STORE_CHAIN = "trend_scout,product_creator,art_director,store_manager,social_director"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- measuring --------------------------------------------------------------------

def measure_store(db_path: str, owner_user_id: int) -> tuple[float, str]:
    """Products actually live for sale.

    Not revenue, and the difference is deliberate. Revenue has no source until something
    is listed, so tracking it today would report a flat zero caused by a missing
    integration and read as a market verdict. This counts the thing that must move first
    and cannot be faked by activity: four agents can run all week and leave it at zero.
    """
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(db_path)) as conn:
        n = conn.execute(
            """SELECT count(*) FROM store_listings
                WHERE owner_user_id = ? AND status IN ('published', 'on_sale')""",
            (owner_user_id,)).fetchone()[0]
        drafts = conn.execute(
            """SELECT count(*) FROM store_listings
                WHERE owner_user_id = ? AND status IN ('draft', 'approved')""",
            (owner_user_id,)).fetchone()[0]
    return float(n), f"{n} live, {drafts} waiting to go live"


def measure_crypto(db_path: str, owner_user_id: int) -> tuple[float, str]:
    """Realised P&L over the trailing 30 days.

    Realised, not mark-to-market: an open position's paper gain is a forecast, and the
    desk's documented failure was exit discipline -- exactly the thing an unrealised
    number hides.
    """
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """SELECT count(*) AS n, coalesce(sum(realized), 0) AS pnl FROM paper_trades
                WHERE at >= ?""",
            ((_now() - timedelta(days=30)).isoformat(),)).fetchone()
    return round(float(row[1]), 2), f"{row[0]} trades in 30 days"


MEASURES = {STORE_MISSION: measure_store, CRYPTO_MISSION: measure_crypto}


def seed_missions(db_path: str) -> None:
    """Define the two money missions. Idempotent; their history survives a redefinition."""
    missions.init_missions_db(db_path)
    missions.upsert_mission(
        db_path, STORE_MISSION,
        title="Jarvis Store: products live and selling",
        metric="products live for sale", unit="listings", target=10,
        chain=STORE_CHAIN,
        # Twelve hours is one full pipeline tick. A day of no movement means a whole tick
        # produced nothing, which is the thing that went unnoticed for a week.
        stall_hours=24)
    missions.upsert_mission(
        db_path, CRYPTO_MISSION,
        title="Crypto desk: realised P&L, trailing 30 days",
        metric="realised P&L, 30 days", unit="usd", target=500,
        chain="",  # no agent chain -- the desk trades continuously rather than in stages
        stall_hours=48)


# --- what is actually sitting there -----------------------------------------------

def store_work_waiting(db_path: str, owner_user_id: int) -> dict:
    """Per stage, how much work is queued for it right now.

    This is what separates "nothing to do" from "nobody has got to it". Both look like a
    skip in agent_runs, and only one of them is a problem worth waking up for.
    """
    from . import business_db
    def count(fn, *a, **kw):
        try:
            return len(fn(*a, **kw))
        except Exception:
            logger.exception("work probe failed")
            return 0
    return {
        "product_creator": count(business_db.list_trend_leads, db_path, owner_user_id,
                                 status="new", limit=50),
        "art_director": count(business_db.concepts_without, db_path, owner_user_id,
                              "art_briefs", limit=50),
        "store_manager": count(business_db.concepts_without, db_path, owner_user_id,
                               "store_listings", limit=50),
        "social_director": count(business_db.listings_without_posts, db_path,
                                 owner_user_id, limit=50),
    }


def decide_remedy(blocker: dict | None, waiting: dict) -> tuple[str | None, str]:
    """Given the blocked stage, what to run. Returns (agent_to_run, why).

    Two cases, and telling them apart is the whole point. If the blocked stage has work
    queued, it is not blocked at all -- it is merely waiting for a tick, so run it. If it
    has none, the stage above it did not produce, so run that one instead. Chasing the
    symptom downstream is what makes a stalled pipeline look unfixable.
    """
    if blocker is None:
        return None, "every stage is producing"
    name = blocker["agent"]
    if waiting.get(name, 0) > 0:
        return name, (f"{name} has {waiting[name]} item(s) queued and the next scheduled "
                      f"run is up to 12h away")
    chain = STORE_CHAIN.split(",")
    if name not in chain:
        return None, f"{name} is not part of the store chain"
    i = chain.index(name)
    if i == 0:
        # Nothing upstream to lean on. trend_scout finding nothing is either a real dry
        # spell or a broken source, and only he can tell which.
        return None, (f"{name} is the first stage and is not producing "
                      f"({blocker.get('last_reason') or 'no reason given'})")
    upstream = chain[i - 1]
    return upstream, (f"{name} has nothing to work on, so {upstream} above it "
                      f"is what needs to run")


# --- the tick ---------------------------------------------------------------------

def run_once(db_path: str, owner_user_id: int, run_agent=None, say=None,
             now: datetime | None = None) -> dict:
    """Measure every mission, act on the stalled ones, escalate what it cannot fix.

    `run_agent(name) -> dict` runs one business agent; without it nothing is actuated and
    the tick only measures and reports, which is what the tests use and what a paused
    deployment should do. `say(topic, body, priority) -> bool` delivers to the owner;
    the caller supplies it so this module never touches a transport.
    """
    now = now or _now()
    missions.init_missions_db(db_path)
    attention.init_attention_db(db_path)
    results = []

    for mission in missions.list_missions(db_path):
        key = mission["key"]
        measure = MEASURES.get(key)
        if measure is None:
            continue
        try:
            value, note = measure(db_path, owner_user_id)
        except Exception:
            logger.exception("could not measure mission %s", key)
            continue

        moved = missions.record_reading(db_path, mission["id"], value, note, now=now)
        if moved:
            # Something worked. Close out whatever was last tried, so the next stall is
            # diagnosed against a clean slate rather than yesterday's excuse.
            missions.close_out(db_path, mission["id"], f"moved to {value} ({note})")

        fresh = missions.get_mission(db_path, key)
        stalled = missions.is_stalled(db_path, fresh, now=now)
        outcome = {"mission": key, "value": value, "note": note, "moved": moved,
                   "stalled": stalled, "acted": None, "told": False}

        if not stalled:
            results.append(outcome)
            continue

        report = missions.chain_report(db_path, fresh.get("chain"), now=now)
        blocker = missions.first_blocked_stage(report)
        waiting = store_work_waiting(db_path, owner_user_id) if fresh.get("chain") else {}
        agent_to_run, why = decide_remedy(blocker, waiting)

        hours = missions.hours_since_movement(db_path, fresh["id"], now=now)
        stuck_for = f"{hours:.0f}h" if hours is not None else "as long as it has existed"
        diagnosis = f"{mission['metric']} stuck at {value} for {stuck_for}: {why}"

        acted = False
        if agent_to_run and run_agent is not None:
            if missions.already_tried(db_path, mission["id"], f"ran {agent_to_run}",
                                      hours=11, now=now):
                # Tried recently and the number still has not moved. Running it again
                # would be the machine version of the nineteen texts, and each attempt
                # costs a web-searching model call.
                diagnosis += f" - running {agent_to_run} did not help last time"
            else:
                try:
                    run_agent(agent_to_run)
                    acted = True
                    missions.record_intervention(db_path, mission["id"], diagnosis,
                                                 f"ran {agent_to_run}", now=now)
                    outcome["acted"] = agent_to_run
                    logger.info("executive: %s stalled, ran %s (%s)", key, agent_to_run, why)
                except Exception as e:
                    diagnosis += f" - tried to run {agent_to_run} and it failed: {e}"
                    logger.exception("executive: remedy %s failed", agent_to_run)

        if not acted:
            # Nothing this may do on its own. That is exactly when he should hear about
            # it, and it is the case that has been silent until now.
            missions.record_intervention(db_path, mission["id"], diagnosis, "escalated",
                                         told_owner=True, now=now)
            body = f"{mission['title']}: {diagnosis}."
            verdict = attention.should_say(db_path, f"mission:{key}", body,
                                           priority="high", now=now)
            outcome["told"] = verdict["say"]
            if verdict["say"] and say is not None:
                say(f"mission:{key}", verdict["body"], "high")

        results.append(outcome)

    return {"at": now.isoformat(), "missions": results}
