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

    LIVE MEANS A REAL CHANNEL AGREES. The count requires an external_id -- an id handed
    back by Etsy, Shopify or Printify -- and not merely status = 'published'. As Jarvis
    put it to Jack on 2026-09-22: *"published just records your decision internally; it
    doesn't push to Etsy, Shopify, or anywhere real."* A metric that a status flip can
    satisfy would have reported this mission WON, and closed it, with not one product
    for sale anywhere on earth. The number has to be answerable by someone other than us.
    """
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(db_path)) as conn:
        n = conn.execute(
            """SELECT count(*) FROM store_listings
                WHERE owner_user_id = ? AND status IN ('published', 'on_sale')
                  AND external_id IS NOT NULL AND trim(external_id) <> ''""",
            (owner_user_id,)).fetchone()[0]
        # Counted apart, because the two mean very different things. Waiting is work
        # done; claimed-but-unconfirmed is a listing this system believes it published
        # and no storefront has ever heard of.
        claimed = conn.execute(
            """SELECT count(*) FROM store_listings
                WHERE owner_user_id = ? AND status IN ('published', 'on_sale')
                  AND (external_id IS NULL OR trim(external_id) = '')""",
            (owner_user_id,)).fetchone()[0]
        drafts = conn.execute(
            """SELECT count(*) FROM store_listings
                WHERE owner_user_id = ? AND status IN ('draft', 'approved')""",
            (owner_user_id,)).fetchone()[0]
    note = f"{n} live, {drafts} waiting to go live"
    if claimed:
        note += (f", {claimed} marked published but on no real storefront")
    return float(n), note


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


# --- is the machinery actually working --------------------------------------------

# A worker that fails three times in a day is broken, however many times it also
# succeeded in between.
#
# Counting CONSECUTIVE failures is the obvious rule and it would have missed the bug
# that prompted this one. The crypto day trader only failed on the runs where it raised
# a stop, so it never failed twice in a row -- and it still died 83 times over two days
# while its success rate looked like 84%. Rate inside a window catches that; a streak
# does not.
BROKEN_FAILURES = 3
# Judged over its last runs rather than over a fixed window, which fails at both ends: a
# worker fixed an hour ago would go on being reported broken all day, and one that runs
# daily could never reach three failures inside 24h however reliably it failed. Twenty
# runs is also self-clearing -- once it works again it drops off on its own.
BROKEN_SAMPLE = 20
# A worker that failed, was switched off and has sat quiet for a week is not news.
BROKEN_STALE_DAYS = 7

# Both tables mean the same thing and spell it differently: staff_work says 'failed',
# agent_runs says 'error'. Kept as data rather than two near-identical functions.
WORKER_SOURCES = (
    ("employee", """SELECT s.key AS key, s.title AS title, w.status AS status,
                           w.error AS error, w.started_at AS at
                      FROM staff_work w JOIN staff s ON s.id = w.staff_id
                     ORDER BY w.started_at DESC"""),
    ("agent", """SELECT r.agent AS key, r.agent AS title, r.status AS status,
                        r.summary AS error, r.started_at AS at
                   FROM agent_runs r
                  ORDER BY r.started_at DESC"""),
)

FAILED_STATUSES = ("failed", "error")


def broken_workers(db_path: str, now: datetime | None = None,
                   sample: int = BROKEN_SAMPLE,
                   threshold: int = BROKEN_FAILURES) -> list[dict]:
    """Every scheduled worker that is failing repeatedly, worst first.

    This is the half of "keep the missions moving" that was missing. The mission loop
    below watches NUMBERS, and a number can keep moving while the machinery producing it
    is broken: crypto P&L drifted from $51 to $55 across the same two days the day trader
    was crashing on every stop raise, because the positions it had already opened went on
    closing themselves. Nothing was stalled, so nothing was diagnosed, so the only thing
    that ever mentioned it was a single text that nobody owned.

    Reports the most common error rather than the most recent: 83 KeyErrors and one
    unrelated timeout is one problem, and naming the timeout would send him after the
    wrong thing.
    """
    from collections import Counter
    from contextlib import closing
    import sqlite3

    now = now or _now()
    stale_before = (now - timedelta(days=BROKEN_STALE_DAYS)).isoformat()
    out = []
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for kind, sql in WORKER_SOURCES:
            try:
                rows = [dict(r) for r in conn.execute(sql)]
            except sqlite3.OperationalError:
                # A deployment that has never run one of the two kinds has no table.
                continue
            by_key: dict = {}
            for row in rows:                      # already newest-first from the query
                by_key.setdefault(row["key"], []).append(row)
            for key, runs in by_key.items():
                recent = runs[:sample]
                bad = [r for r in recent
                       if (r["status"] or "").lower() in FAILED_STATUSES]
                if len(bad) < threshold:
                    continue
                if max(r["at"] for r in bad) < stale_before:
                    continue
                errors = Counter((r["error"] or "unrecorded").strip()[:120] for r in bad)
                error, hits = errors.most_common(1)[0]
                out.append({
                    "kind": kind, "key": key,
                    "title": recent[0]["title"] or key,
                    "failures": len(bad), "runs": len(recent),
                    "error": error, "same_error": hits,
                    "first": min(r["at"] for r in bad),
                    "last": max(r["at"] for r in bad),
                })
    out.sort(key=lambda w: w["failures"], reverse=True)
    return out


def health_report(worker: dict) -> str:
    """One sentence he can act on: what broke, how often, and since when.

    The failure COUNT next to the run count is the part that matters. "Failing" invites
    the reasonable assumption that it is down; "83 of 529 runs" says it is limping, which
    is a different and much easier thing to ignore for two days.
    """
    share = f"{worker['failures']} of its last {worker['runs']} runs"
    span = worker["first"][:16].replace("T", " ")
    return (f"{worker['title']} is failing: {share}, {worker['same_error']} of them "
            f"with {worker['error']}, starting {span}Z.")


def _open_health_task(db_path: str, owner_user_id: int, key: str):
    """The still-open task already raised for this worker, if any."""
    from . import personal_db

    marker = _HEALTH_MARKER % key
    try:
        tasks = personal_db.list_tasks(db_path, owner_user_id, status="open")
    except Exception:
        logger.exception("could not read tasks while checking worker health")
        return None
    for task in tasks:
        if marker in (task.get("text") or ""):
            return task
    return None


# Carried in the task text so the next tick can find its own task again. Ugly, and the
# alternative is a table whose only column is this string.
_HEALTH_MARKER = "[worker:%s]"


def watch_workers(db_path: str, owner_user_id: int, say=None,
                  now: datetime | None = None) -> list[dict]:
    """Notice broken workers, put each on his board once, and tell him once.

    Two channels on purpose, because one of them was the whole problem. A text is how he
    finds out; a task is how it survives being read on a phone and forgotten. The day
    trader sent exactly one text in two days -- correctly, since the cooldown is what
    stops 83 failures becoming 83 texts -- and because nothing else recorded it, that
    text was the entire institutional memory of the bug.
    """
    from . import personal_db

    now = now or _now()
    handled = []
    for worker in broken_workers(db_path, now=now):
        body = health_report(worker)
        existing = _open_health_task(db_path, owner_user_id, worker["key"])
        if existing is None:
            text = (f"Fix {worker['title']}: {worker['same_error']}x {worker['error']} "
                    f"{_HEALTH_MARKER % worker['key']}")
            try:
                personal_db.init_personal_db(db_path)
                personal_db.create_task(db_path, owner_user_id, text,
                                        priority="high", track="project")
            except Exception:
                logger.exception("could not raise a task for %s", worker["key"])
        verdict = attention.should_say(db_path, f"worker:{worker['key']}", body,
                                       priority="high", now=now)
        if verdict["say"] and say is not None:
            say(f"worker:{worker['key']}", verdict["body"], "high")
        handled.append({"worker": worker["key"], "failures": worker["failures"],
                        "task_existed": existing is not None, "told": verdict["say"]})
    return handled


def stage_store_products(db_path: str, owner_user_id: int, printify=None,
                         limit: int = 5) -> dict:
    """Turn approved listings into real products, ready to go on sale.

    The last mile the pipeline never had. Staging is reversible -- a Printify product
    that has not been published is a draft nobody can buy, and deleting it is one call --
    so this runs unattended like every other stage. Whether any of it actually goes ON
    SALE is a separate switch (store_policy.go_live), because that step is the one that
    cannot be taken back.
    """
    from . import store_policy, store_publish

    if printify is None:
        return {"ok": False, "why": "Printify is not configured", "made": [], "blocked": []}
    try:
        return store_publish.publish_approved(
            db_path, owner_user_id, printify,
            live=store_policy.go_live(db_path), limit=limit)
    except Exception as exc:                                        # noqa: BLE001
        logger.exception("staging store products failed")
        return {"ok": False, "why": f"{type(exc).__name__}: {exc}",
                "made": [], "blocked": []}


# --- the tick ---------------------------------------------------------------------

def run_once(db_path: str, owner_user_id: int, run_agent=None, say=None,
             now: datetime | None = None, printify=None) -> dict:
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
        if missions.has_never_moved(db_path, fresh["id"]):
            # The clock starts when the mission is created, not when the thing actually
            # broke, so a mission seeded on a metric that was already dead reports a
            # tidy "stuck for 19h". The store had been at zero for a week before anyone
            # was measuring it; saying 19h understates it by six days.
            stuck_for = "its whole life -- it has never once moved"
        elif hours is not None:
            stuck_for = f"{hours:.0f}h"
        else:
            stuck_for = "as long as it has existed"
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

    # Missions above measure whether the numbers are moving. This asks the other
    # question, the one nothing was asking: is the machinery still working at all.
    workers = watch_workers(db_path, owner_user_id, say=say, now=now)

    # And the last mile: approved listings become real products. Nothing here goes on
    # sale unless store_policy.go_live says so.
    staged = stage_store_products(db_path, owner_user_id, printify) if printify else None

    return {"at": now.isoformat(), "missions": results, "workers": workers,
            "staged": staged}
