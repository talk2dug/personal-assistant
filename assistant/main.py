"""Entrypoint: wires together the db, LLM client, scheduler, and Telegram transport."""
import asyncio
import logging

from .config import load_config
from .core import db
from .core import scheduler
from .core import vision
from .core.setup import (
    build_airbnb_context, build_business_context, build_calendar_context, build_ccxt_context,
    build_era_context, build_gpu_bridge, build_notifier, build_home_assistant_context, build_kroger_context,
    build_letterstream_context, build_llm, build_mail_context, build_obsidian_context,
    build_personal_context, build_phone_context, build_ticketmaster_context,
)
from .transports import telegram_bot

from .core.logging_setup import setup_logging

setup_logging("jarvis-core")
logger = logging.getLogger(__name__)


def _seed_cameras(cfg) -> None:
    """Cameras are metadata (a key/name/url), so config.json is the source of truth for
    day-one coverage -- same pattern as seeding users below. The actual detection loop
    (scripts/vision_worker.py) never touches config; it just reads whatever rows are
    here, so adding a camera through the web UI later works without editing this list.
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
    vision.init_vision_db(cfg.db_path)
    _seed_cameras(cfg)
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
    personal = build_personal_context(cfg, owner_row["id"] if owner_row else None)
    airbnb = build_airbnb_context(cfg)
    ticketmaster = build_ticketmaster_context(cfg)
    kroger = build_kroger_context(cfg)
    ccxt = build_ccxt_context(cfg)
    letterstream = build_letterstream_context(cfg)
    # The bridge worker lives in this process alongside the scheduler — one place owns
    # all background work, so there's exactly one queue draining the GPU.
    if bridge is not None:
        bridge.start_worker()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = telegram_bot.build_application(
        cfg.telegram_bot_token, cfg.db_path, llm, cfg.timezone, era=era, calendar=calendar, phone=phone, mail=mail,
        obsidian=obsidian, home_assistant=home_assistant, business=business, personal=personal,
        airbnb=airbnb, ticketmaster=ticketmaster, kroger=kroger, ccxt=ccxt, letterstream=letterstream,
    )
    # Routed through the user's notification policy: reminders follow the same
    # 'phone when I'm out' preference as anything else Jarvis sends unprompted.
    notify = build_notifier(cfg, telegram_bot.make_notifier(application, loop), home_assistant)
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
    )

    logger.info("Jarvis core starting, polling Telegram...")
    application.run_polling()


if __name__ == "__main__":
    main()



