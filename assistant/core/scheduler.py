"""Background poller that fires due reminders, and (optionally) pulls Apple Calendar
changes into the reminders table and refreshes the Era finance cache. Transport-agnostic:
takes a notify(chat_id, text) callback so it doesn't need to know about Telegram.
"""
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from . import agents, business_db, db, location, market_data, personal_agents, staff
from .engine import handle_message
from .finance import CADENCE_DAYS

logger = logging.getLogger(__name__)


def start(
    db_path: str, notify, poll_interval_seconds: int = 30,
    calendar=None, caldav_sync_interval_seconds: int = 300,
    era=None, era_cache_interval_seconds: int = 1200,
    business=None, llm=None, business_intervals=None, tz_name: str = "America/New_York",
    business_agents_enabled: bool = False, home_assistant=None, phone=None, mail=None, obsidian=None,
    location_poll_seconds: int = 120, location_force_seconds: int = 600,
    market_api_key: str | None = None, market_poll_seconds: int = 60,
    market_track_limit: int = 250,
    airbnb=None, ticketmaster=None, kroger=None, ccxt=None, letterstream=None,
    personal=None, personal_research_minutes: int = 30, git_ops=None,
) -> BackgroundScheduler:
    """calendar is an engine.CalendarContext (skip Apple Calendar sync if None).
    era is an engine.EraContext (skip the finance cache refresh if None).
    business is an engine.BusinessContext; together with a web-searching llm AND
    business_agents_enabled it schedules the market/trend/research/pipeline agents and the
    daily briefing. With business_agents_enabled False (the default) nothing agent-related
    is scheduled at all, though every agent still runs on demand from chat."""
    business_intervals = business_intervals or {
        "market_hours": 72, "trend_hours": 24, "research_minutes": 120,
        "pipeline_hours": 12, "digest_hour": 8,
    }
    scheduler = BackgroundScheduler()

    def _guarded_simple(name, fn):
        """A scheduled job must never raise into APScheduler: one unhandled exception
        and the job is dropped for the lifetime of the process, silently."""
        def _tick():
            try:
                fn()
            except Exception:
                logger.exception("%s job failed", name)
        return _tick

    def _recipients_for(reminder):
        users = db.all_users(db_path)
        if reminder["scope"] == "shared":
            return [u["telegram_chat_id"] for u in users]
        owner = next((u for u in users if u["id"] == reminder["owner_user_id"]), None)
        return [owner["telegram_chat_id"]] if owner else []

    def _tick():
        for reminder in db.due_reminders(db_path):
            for chat_id in _recipients_for(reminder):
                try:
                    notify(chat_id, f"⏰ Reminder: {reminder['text']}")
                except Exception:
                    logger.exception("failed to send reminder %s to %s", reminder["id"], chat_id)
            db.mark_reminder_sent(db_path, reminder["id"])

    scheduler.add_job(_tick, "interval", seconds=poll_interval_seconds, id="reminder_poll")

    if calendar is not None:
        owner = next((u for u in db.all_users(db_path) if u["role"] == "owner"), None)

        def _caldav_tick():
            if owner is None:
                return
            for scope, calendar_url in (
                ("private", calendar.personal_calendar), ("shared", calendar.shared_calendar),
            ):
                try:
                    sync_calendar(calendar.client, db_path, calendar_url, scope, owner["id"])
                except Exception:
                    logger.exception("caldav sync failed for %s", calendar_url)

        scheduler.add_job(_caldav_tick, "interval", seconds=caldav_sync_interval_seconds, id="caldav_sync")

    if era is not None:
        def _era_cache_tick():
            try:
                refresh_era_cache(era.mcp_client, db_path)
            except Exception:
                logger.exception("era cache refresh failed")

        scheduler.add_job(_era_cache_tick, "interval", seconds=era_cache_interval_seconds, id="era_cache_refresh")

    if home_assistant is not None:
        owner = next((u for u in db.all_users(db_path) if u["role"] == "owner"), None)
        _last_forced_refresh = 0.0

        def _location_tick():
            """Watches where the owner is and fires anything waiting on him moving.

            Polling rather than subscribing: HA can push state changes over websocket,
            but that's a persistent connection to keep alive and reconnect, and arriving
            somewhere is not a sub-minute-latency event. A poll is far less to go wrong.
            """
            if owner is None:
                return

            # iOS suspends the companion app, so a passive read can be hours stale. Ask
            # the phone for a fresh fix — but only periodically, and only when something
            # is actually waiting on his position, because each request is a push and
            # waking the phone every couple of minutes for nothing is a battery tax.
            nonlocal _last_forced_refresh
            now = time.time()
            if (db.has_armed_location_triggers(db_path, owner["id"])
                    and now - _last_forced_refresh >= location_force_seconds):
                _last_forced_refresh = now
                home_assistant.mcp_client.request_location_update()
                # Give the phone a moment to answer; the next tick reads it regardless.
                time.sleep(6)

            fix = home_assistant.mcp_client.location()
            if fix.get("latitude") is None:
                return
            transition = location.detect_transition(
                db_path, owner["id"], fix["latitude"], fix["longitude"], fix.get("accuracy_m"))
            if transition is None:
                return

            event, place = transition["event"], transition["place"]
            logger.info("location: %s %s", event, place["name"])

            for reminder in db.due_location_reminders(db_path, place["id"], event):
                verb = "arriving at" if event == "arrive" else "leaving"
                notify(owner["telegram_chat_id"], f"Reminder ({verb} {place['name']}): {reminder['text']}")
                db.mark_location_reminder_fired(db_path, reminder["id"])

            for routine in db.due_routines(db_path, place["id"], event):
                # Mark fired before running: a routine whose prompt is slow or throws must
                # not be retried on the next tick and end up firing repeatedly.
                db.mark_routine_fired(db_path, routine["id"])
                try:
                    reply = handle_message(
                        db_path, llm, owner["id"], routine["prompt"], tz_name=tz_name,
                        era=era, calendar=calendar, phone=phone, mail=mail, obsidian=obsidian,
                        home_assistant=home_assistant, business=business, personal=personal,
                        airbnb=airbnb, ticketmaster=ticketmaster, kroger=kroger, ccxt=ccxt,
                        letterstream=letterstream, git_ops=git_ops,
                    )
                    if reply:
                        notify(owner["telegram_chat_id"], f"{routine['name']}: {reply}")
                except Exception:
                    logger.exception("routine %s failed", routine["name"])

        scheduler.add_job(_location_tick, "interval", seconds=location_poll_seconds, id="location_poll")

    if business is not None and llm is not None and hasattr(llm, "research") and business_agents_enabled:
        # Two independent reasons nothing may be scheduled here. The agents need a
        # web-searching backend — on Ollama they'd have no way to find anything and would
        # produce hallucinated markets rather than none. And business_agents_enabled is
        # the owner's master switch, currently off while he and Jarvis work out cadence;
        # on-demand runs from chat are unaffected either way.
        owner = next((u for u in db.all_users(db_path) if u["role"] == "owner"), None)
        profile = business.profile

        def _guarded(name, fn):
            def _tick():
                try:
                    fn()
                except Exception:
                    logger.exception("%s agent failed", name)
            return _tick

        def _first_run_at(agent_name: str, interval_hours: int, delay_minutes: int):
            """When to fire an agent for the first time after startup.

            Not "immediately" — each scan is a long run of web searches billed against a
            subscription, and a service restart is not a reason to redo one. Fire soon
            only if the last successful run is already older than the interval (or there
            has never been one); otherwise wait out the remainder.
            """
            now = datetime.now(timezone.utc)
            runs = [r for r in business_db.recent_agent_runs(db_path, limit=50)
                    if r["agent"] == agent_name and r["status"] == "ok"]
            if not runs:
                return now + timedelta(minutes=delay_minutes)
            last = datetime.fromisoformat(runs[0]["started_at"])
            due = last + timedelta(hours=interval_hours)
            return max(due, now + timedelta(minutes=delay_minutes))

        if owner is not None:
            market_hours = business_intervals["market_hours"]
            trend_hours = business_intervals["trend_hours"]
            scheduler.add_job(
                _guarded("market", lambda: agents.run_market_agent(db_path, llm, owner["id"], profile)),
                "interval", hours=market_hours, id="market_agent",
                next_run_time=_first_run_at("market_finder", market_hours, 2),
            )
            scheduler.add_job(
                _guarded("trend", lambda: agents.run_trend_agent(db_path, llm, owner["id"], profile)),
                "interval", hours=trend_hours, id="trend_agent",
                next_run_time=_first_run_at("trend_scout", trend_hours, 5),
            )

            # The creative pipeline runs on one shared tick rather than four schedules.
            # Each stage is a no-op unless the stage above it produced something the owner
            # approved, so they're cheap when idle and there's no value in them firing at
            # different times — Product Creator finding nothing new is one skipped run.
            pipeline_hours = business_intervals.get("pipeline_hours", 12)

            def _pipeline_tick():
                agents.run_product_creator(db_path, llm, owner["id"], profile)
                agents.run_art_director(db_path, llm, owner["id"], profile)
                agents.run_store_manager(db_path, llm, owner["id"], profile)
                agents.run_social_director(db_path, llm, owner["id"], profile)

            scheduler.add_job(
                _guarded("pipeline", _pipeline_tick), "interval", hours=pipeline_hours,
                id="creative_pipeline", next_run_time=_first_run_at("product_creator", pipeline_hours, 8),
            )

            def _digest_tick():
                text = agents.build_digest(db_path, owner["id"], hours=24)
                if text:
                    notify(owner["telegram_chat_id"], text)

            scheduler.add_job(
                _guarded("digest", _digest_tick), "cron", hour=business_intervals["digest_hour"], minute=0,
                timezone=tz_name, id="business_digest",
            )

        # The research queue runs regardless of owner lookup — it's keyed on queued rows.
        # It starts a minute after boot rather than waiting out a full interval: an
        # interval trigger's first fire is one interval away, so each service restart
        # would push queued research back again, and a few restarts in an afternoon
        # could starve it indefinitely (observed exactly that during development). Cheap
        # to run early — with an empty queue it returns without touching the LLM.
        scheduler.add_job(
            _guarded("research", lambda: agents.run_research_queue(db_path, llm, profile)),
            "interval", minutes=business_intervals["research_minutes"], id="research_agent",
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
        )

        # Hired staff on a schedule. One data-driven job rather than a scheduler entry
        # per employee: hiring and releasing happen at runtime from chat, and keeping a
        # job registry in sync with the staff table would be a second source of truth to
        # get wrong. This ticks every 5 minutes and asks the table who is due and on
        # shift, which is the resolution a monitoring role actually needs -- an hourly
        # tick cannot honour "every 15 minutes".
        def _staff_alert(headline: str, body: str, urgency: str, person: dict) -> None:
            """Deliver an employee's escalation to the owner.

            The employee produced a judgement; this decides it is worth sending and how
            loudly. Nothing an employee writes reaches the phone without passing through
            here, so an unattended job cannot notify on its own authority.
            """
            if owner is None:
                return
            prefix = {"high": "URGENT", "normal": "", "low": "FYI"}.get(urgency, "")
            title = f"{person['title']}: {headline}".strip()
            text = f"{prefix + ' - ' if prefix else ''}{title}\n\n{(body or '').strip()[:1200]}"
            notify(owner["telegram_chat_id"], text)

        def _staff_tick():
            """One pass over the roster.

            Logs on every tick, including the empty ones: "nobody was due" and "the job
            stopped firing" are indistinguishable from the outside, and telling them
            apart after the fact is exactly what was needed here.
            """
            due = staff.due_for_cadence(db_path, tz_name)
            if not due:
                logger.debug("staff tick: nobody due")
                return
            logger.info("staff tick: running %s", [p["key"] for p in due])
            results = staff.run_due(db_path, llm, tz_name=tz_name, notify=_staff_alert)
            for r in results:
                logger.info("staff run %s ok=%s alert=%s alerted=%s",
                            r["employee"], r["ok"], r["alert"], r["alerted"])

        scheduler.add_job(
            _guarded("staff_cadence", _staff_tick),
            "interval", minutes=5, id="staff_cadence",
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
            # An employee whose run overruns its cadence must not silently disable the
            # whole roster: with max_instances=1 every later tick is dropped, and one slow
            # LLM call would stop everyone else from ever being checked.
            max_instances=3, coalesce=True, misfire_grace_time=300,
        )

    if personal is not None and llm is not None and hasattr(llm, "research"):
        # Deliberately a top-level job, NOT nested inside the business_agents_enabled
        # block above: that flag governs the print business's unattended market/trend/
        # pipeline agents and is off by default. "Find me a dentist" is core owner
        # functionality and must run regardless of whether the owner has opted the
        # business into scheduled scans.
        owner = next((u for u in db.all_users(db_path) if u["role"] == "owner"), None)
        if owner is not None:
            scheduler.add_job(
                _guarded_simple(
                    "personal_research",
                    lambda: personal_agents.run_personal_research_queue(db_path, llm, owner["id"]),
                ),
                "interval", minutes=personal_research_minutes, id="personal_research_agent",
                # Starts a minute after boot, same reasoning as the business research
                # queue: an interval trigger's first fire is a full interval away, and a
                # queued "find me a doctor" waiting through several restarts is a bad look.
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
            )

    if market_api_key:
        # The crypto feed. Deliberately outside the business_agents_enabled gate: the
        # cache is cheap (1 credit a poll, 14% of the daily budget at 60s) and an
        # employee that wakes to an empty table is worse than useless -- it would report
        # "no data" as though that were a market condition.
        def _market_tick():
            result = market_data.refresh(db_path, market_api_key, limit=market_track_limit)
            if not result.get("ok"):
                logger.warning("market poll failed: %s", result.get("error"))
            elif result.get("appeared") or result.get("disappeared"):
                logger.info("market listings changed: +%s -%s",
                            result["appeared"], result["disappeared"])

        scheduler.add_job(
            _guarded_simple("market", _market_tick), "interval",
            seconds=market_poll_seconds, id="market_poll",
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=10),
        )
        logger.info("market feed: polling top %d every %ds", market_track_limit, market_poll_seconds)

    scheduler.start()
    return scheduler


def sync_calendar(
    client, db_path: str, calendar_url: str, scope: str, owner_user_id: int,
    lookback_days: int = 1, lookahead_days: int = 180,
) -> None:
    """Reconciles one Apple Calendar against the reminders linked to it: new remote
    events become new reminders, changed ones update the linked reminder (Apple's side
    wins on pull), and remote deletions cancel the linked reminder. No merge logic for
    genuine conflicts — last side to sync wins, which is fine at personal scale."""
    now = datetime.now(timezone.utc)
    start_range = now - timedelta(days=lookback_days)
    end_range = now + timedelta(days=lookahead_days)

    remote_events = client.list_events(calendar_url, start_range, end_range)
    remote_by_uid = {e["uid"]: e for e in remote_events}

    for reminder in db.reminders_linked_to_calendar(db_path, calendar_url):
        if reminder["caldav_uid"] not in remote_by_uid:
            db.cancel_reminder(db_path, reminder["owner_user_id"], reminder["id"])

    for uid, event in remote_by_uid.items():
        remote_start = event["start"]
        if remote_start is None:
            continue
        if remote_start.tzinfo is None:
            remote_start = remote_start.replace(tzinfo=timezone.utc)
        remote_due_at = remote_start.astimezone(timezone.utc).isoformat()

        existing = db.find_reminder_by_caldav_uid(db_path, uid)
        if existing is None:
            reminder_id = db.add_reminder(db_path, owner_user_id, text=event["summary"], due_at=remote_due_at, scope=scope)
            db.set_caldav_link(db_path, reminder_id, uid, calendar_url)
        elif existing["text"] != event["summary"] or existing["due_at"] != remote_due_at:
            db.update_reminder_from_remote(db_path, existing["id"], event["summary"], remote_due_at)


def _era_payload(tool_result: dict) -> dict:
    """Era's MCP tool results carry their actual payload as a JSON-encoded string in
    content[0] (confirmed by direct testing against the live server)."""
    return json.loads(tool_result["content"][0])


def refresh_era_cache(mcp_client, db_path: str) -> None:
    """Pulls current balances and recurring-charge detection from Era and writes them
    into the local cache tables the finance dashboard reads from — avoids a live Era
    round trip (which can be slow, see mcp_client.py's timeout notes) on every page load."""
    accounts = _era_payload(mcp_client.call_tool("accounts__list_financial_accounts", {}))
    for account in accounts.get("accounts", []):
        balance = account.get("balance") or {}
        db.upsert_era_account(
            db_path, account["account_group_key"], account.get("name", ""), account.get("type"),
            balance.get("current"), balance.get("available"),
        )

    recurring = _era_payload(mcp_client.call_tool("transactions__list_recurring_charges", {}))
    for item in recurring.get("recurring", []):
        merchant = item.get("merchant", "Unknown")
        frequency = (item.get("frequency") or "").strip().lower()
        item_type = item.get("type")
        charge_key = f"{merchant}|{frequency}|{item_type}"
        direction = "income" if item_type == "income" else "expense"
        amount = (item.get("estimated_amount") or {}).get("amount", 0.0)

        # Era gives last_seen, not a next-due date — derive one using the same cadence
        # approximation finance.py uses, so the two stay consistent.
        next_expected = None
        last_seen = item.get("last_seen")
        if last_seen:
            step_days = CADENCE_DAYS.get(frequency)
            if step_days:
                next_expected = (date.fromisoformat(last_seen) + timedelta(days=step_days)).isoformat()
            else:
                next_expected = last_seen

        db.upsert_era_recurring_charge(db_path, charge_key, merchant, amount, direction, frequency, next_expected)

    for period in ("this_month", "last_30_days"):
        spending = _era_payload(
            mcp_client.call_tool("insights__analyze_spending", {"period": period, "group_by": "category"})
        )
        for group in spending.get("groups", []):
            db.upsert_era_category_spending(
                db_path, group["category_key"], period, group.get("label", ""),
                group["amount"], group.get("percent_of_total"), group.get("transaction_count"),
            )


