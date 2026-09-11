"""Entrypoint: wires together the db, LLM client, scheduler, and Telegram transport."""
import asyncio
import logging

from .config import load_config
from .core import business_db, db, github_client, staff, vision, work_queue
from .core import scheduler
from .core.setup import (
    build_airbnb_context, build_business_context, build_calendar_context, build_ccxt_context,
    build_era_context, build_git_ops_context, build_gpu_bridge, build_notifier,
    build_home_assistant_context, build_kroger_context, build_letterstream_context, build_llm,
    build_local_llm_context, build_mail_context, build_obsidian_context, build_personal_context,
    build_phone_context, build_recipe_context, build_ticketmaster_context,
)
from .transports import telegram_bot

from .core.logging_setup import setup_logging

setup_logging("jarvis-core")
logger = logging.getLogger(__name__)


def _seed_cameras(cfg) -> None:
    """Cameras are metadata (a key/name/url), so config.json is the source of truth for
    day-one coverage -- same pattern as seeding users below. The actual detection loop
    (scripts/vision_worker.py, run from the separate jarvis-vision.service/vision_main.py
    process -- see that module's docstring) never touches config; it just reads whatever
    rows are here, so adding a camera through the web UI later works without editing
    this list.
    """
    for cam in cfg.cameras or []:
        try:
            vision.add_camera(
                cfg.db_path, cam["key"], cam["name"], cam["url"],
                kind=cam.get("kind", "mjpeg"), location=cam.get("location", ""),
                motion_threshold=cam.get("motion_threshold", 0.012),
                recordable=cam.get("recordable", True),
            )
        except Exception:
            logger.exception("failed to seed camera %r from config", cam.get("key"))


def main() -> None:
    cfg = load_config()
    db.init_db(cfg.db_path)
    # review_items is the Review page's single decision queue -- pending_actions (Kroger/
    # CCXT/mail/HA/git confirmations) get a linked row there regardless of whether the
    # business feature itself is configured, so this can't stay gated behind that flag.
    business_db.init_business_db(cfg.db_path)
    # show_camera/list_cameras/add_camera are always-on tools (see engine.py's CAMERA_TOOLS),
    # not behind a build_*_context flag, so the cameras table must exist unconditionally too.
    vision.init_vision_db(cfg.db_path)
    _seed_cameras(cfg)
    # github_pr_state backs the GitHub PR/CI watchdog (scheduler.py's run_github_watchdog)
    # -- unconditional for the same reason business_db/vision are: cheap to create, and
    # the watchdog job itself is what actually checks whether git_ops is configured.
    github_client.init_github_db(cfg.db_path)
    for u in cfg.users:
        db.upsert_user(cfg.db_path, u.telegram_chat_id, u.display_name, u.role)

    owner = next((u for u in cfg.users if u.role == "owner"), None)
    owner_row = db.get_user_by_chat_id(cfg.db_path, owner.telegram_chat_id) if owner else None
    llm = build_llm(cfg, owner_user_id=owner_row["id"] if owner_row else None)
    era = build_era_context(cfg)
    calendar = build_calendar_context(cfg)
    phone = build_phone_context(cfg)
    mail = build_mail_context(cfg)
    obsidian = build_obsidian_context(cfg)
    home_assistant = build_home_assistant_context(cfg)
    bridge = build_gpu_bridge(cfg)
    business = build_business_context(cfg, owner_row["id"] if owner_row else None, llm=llm, bridge=bridge)
    airbnb = build_airbnb_context(cfg)
    ticketmaster = build_ticketmaster_context(cfg)
    kroger = build_kroger_context(cfg)
    ccxt = build_ccxt_context(cfg)
    # Built before Personal so its LetterStreamTools/Kroger clients can be handed into
    # PersonalClient -- LetterStream for the credit dispute tracker's draft_dispute_letter/
    # track_dispute_letter, Kroger for sync_kroger_purchases -- see build_personal_context's
    # docstring.
    letterstream = build_letterstream_context(cfg)
    personal = build_personal_context(
        cfg, owner_row["id"] if owner_row else None, letterstream=letterstream, kroger=kroger)
    git_ops = build_git_ops_context(cfg)
    recipe = build_recipe_context(cfg)
    local_llm = build_local_llm_context(cfg)
    # The bridge worker lives in this process alongside the scheduler — one place owns
    # all background work, so there's exactly one queue draining the GPU.
    if bridge is not None:
        bridge.start_worker()

    # The actual camera-watching/YOLO/InsightFace detection loop runs in its own process
    # (jarvis-vision.service / assistant/vision_main.py) and its own virtualenv
    # (.venv-vision) -- never in this one. See vision_main.py's docstring for why: torch/
    # ultralytics/insightface are real GPU dependencies that must not become something
    # the Telegram/chat process needs installed just to answer a question.

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = telegram_bot.build_application(
        cfg.telegram_bot_token, cfg.db_path, llm, cfg.timezone, era=era, calendar=calendar, phone=phone, mail=mail,
        obsidian=obsidian, home_assistant=home_assistant, business=business, personal=personal,
        airbnb=airbnb, ticketmaster=ticketmaster, kroger=kroger, ccxt=ccxt, letterstream=letterstream,
        git_ops=git_ops, recipe=recipe, local_llm=local_llm,
    )
    # Routed through the user's notification policy: reminders follow the same
    # 'phone when I'm out' preference as anything else Jarvis sends unprompted.
    notify = build_notifier(cfg, telegram_bot.make_notifier(application, loop), home_assistant)
    # assign_work's queue (see work_queue.WorkQueue) is constructed back in
    # build_business_context, before notify exists -- started here, now that it does, the
    # same way the GPU bridge is started separately from where it's built.
    if business is not None and business.work_queue is not None:
        # Any staff_work/work_queue row still 'running' at this point cannot be a real
        # in-progress job -- this process just started, and both drain strictly one job
        # at a time, so a 'running' row is proof the previous process died mid-run and
        # nobody ever followed up. A real, confirmed incident: 10 rows sat 'running' for
        # 3-6 days, invisible to any status check, before this existed. Reconciling here,
        # before the worker (re)starts, means a crash self-heals on the next restart
        # instead of needing a manual DB fix.
        orphaned_runs = staff.reconcile_orphaned_work(cfg.db_path)
        orphaned_jobs = work_queue.reconcile_orphaned(cfg.db_path)
        if orphaned_runs or orphaned_jobs:
            logger.warning("startup: reconciled %d orphaned staff_work + %d orphaned work_queue row(s)",
                           orphaned_runs, orphaned_jobs)

        def _cadence_notify(headline: str, body: str, urgency: str, person: dict) -> None:
            """Delivers a scheduled ('cadence') job's alert once the work queue's worker
            finishes it -- same formatting and owner-resolution as scheduler.py's
            _staff_alert, since staff_cadence's own tick now only enqueues (see
            staff.run_due's docstring) and this is what actually notifies once the real
            outcome is known."""
            if owner_row is None:
                return
            notify(owner_row["telegram_chat_id"], staff.format_alert_text(headline, body, urgency, person))

        business.work_queue.start_worker(
            llm, notify=notify, cadence_notify=_cadence_notify,
            timeout=cfg.staff_assignment_timeout_seconds,
            # So a finished coding task can report a PR's real, live CI status instead
            # of the employee's own unverified claim -- see work_queue._pr_status_line.
            git_ops_client=git_ops.mcp_client if git_ops is not None else None)
    scheduler.start(
        cfg.db_path, notify, cfg.poll_interval_seconds,
        calendar=calendar, caldav_sync_interval_seconds=cfg.caldav_sync_interval_seconds,
        era=era, era_cache_interval_seconds=cfg.era_cache_interval_seconds,
        # The agents live in this process, not the web one — a single scheduler owns all
        # background work, same as reminders and the Era cache.
        business=business, llm=llm, tz_name=cfg.timezone,
        business_agents_enabled=cfg.business_agents_enabled,
        # Location watching needs HA for GPS and the other contexts so a routine's
        # prompt has the same tools a chat turn would.
        home_assistant=home_assistant, phone=phone, mail=mail, obsidian=obsidian,
        location_poll_seconds=cfg.location_poll_seconds,
        location_force_seconds=cfg.location_force_seconds,
        business_intervals={
            "market_hours": cfg.market_scan_interval_hours,
            "trend_hours": cfg.trend_scan_interval_hours,
            "research_minutes": cfg.research_queue_interval_minutes,
            "pipeline_hours": cfg.pipeline_interval_hours,
            "digest_hour": cfg.business_digest_hour,
        },
        # The crypto feed. One poller fills a local cache; the agents read that rather
        # than the API, so their spend is bounded and they get history no single response
        # could give them.
        market_api_key=cfg.livecoinwatch_api_key,
        market_poll_seconds=cfg.market_poll_seconds,
        market_track_limit=cfg.market_track_limit,
        airbnb=airbnb, ticketmaster=ticketmaster, kroger=kroger, ccxt=ccxt, letterstream=letterstream,
        personal=personal, personal_research_minutes=cfg.personal_research_interval_minutes,
        git_ops=git_ops, recipe=recipe,
        # Autonomous inbox triage: scores and moves likely junk out of INBOX on an
        # interval, independent of business_agents_enabled (see scheduler.py's
        # docstring for why this isn't gated behind that switch).
        mail_junk_scan_interval_seconds=cfg.mail_junk_scan_interval_seconds,
        kroger_sync_interval_seconds=cfg.kroger_sync_interval_seconds,
        # Watchdog: notices a due personal task or a Review item nobody came back to,
        # instead of the owner only finding out when he happens to ask or check the page.
        task_watchdog_interval_seconds=cfg.task_watchdog_interval_seconds,
        review_watchdog_interval_seconds=cfg.review_watchdog_interval_seconds,
        review_watchdog_stale_hours=cfg.review_watchdog_stale_hours,
        github_watchdog_interval_seconds=cfg.github_watchdog_interval_seconds,
        local_llm=local_llm, local_llm_keepalive_interval_seconds=cfg.local_llm_keepalive_interval_seconds,
        staff_assignment_timeout_seconds=cfg.staff_assignment_timeout_seconds,
    )

    logger.info("Jarvis core starting, polling Telegram...")
    application.run_polling()


if __name__ == "__main__":
    main()
