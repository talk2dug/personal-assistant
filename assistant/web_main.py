"""Entrypoint for the web UI (jarvis-web.service) â€” separate process from main.py's
Telegram bot, sharing config/db but running independently."""
import logging

import uvicorn

from .config import load_config
from .core import db, media_scan
from .core.setup import (
    build_airbnb_context, build_business_context, build_calendar_context, build_ccxt_context,
    build_era_context, build_git_ops_context, build_gpu_bridge, build_home_assistant_context,
    build_kroger_context, build_letterstream_context, build_llm, build_mail_context,
    build_obsidian_context, build_personal_context, build_phone_context, build_ticketmaster_context,
)
from .core.stt import Transcriber
from .core.tts import Speaker
from .web.app import create_app

from .core.logging_setup import setup_logging

setup_logging("jarvis-web")
logger = logging.getLogger(__name__)


def main() -> None:
    cfg = load_config()
    db.init_db(cfg.db_path)
    # The catalogue is written by scripts/scan_network_media.py, which may never have
    # run on a fresh install — create the tables here so the Media page returns empty
    # results rather than a 500.
    media_scan.init_media_db(cfg.db_path)
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
    # The bridge is built here so the chat tools work from the web UI too ("I'm racing"),
    # but no worker is started: JarvisCore owns the single queue drainer. Claiming is
    # cross-process safe, so a synchronous job from here still can't collide with it.
    bridge = build_gpu_bridge(cfg)
    business = build_business_context(cfg, owner_row["id"] if owner_row else None, llm=llm, bridge=bridge)
    personal = build_personal_context(cfg, owner_row["id"] if owner_row else None)
    airbnb = build_airbnb_context(cfg)
    ticketmaster = build_ticketmaster_context(cfg)
    kroger = build_kroger_context(cfg)
    ccxt = build_ccxt_context(cfg)
    letterstream = build_letterstream_context(cfg)
    git_ops = build_git_ops_context(cfg)
    stt = Transcriber(model_size=cfg.stt_model_size)
    speaker = Speaker(voice_path=cfg.piper_voice_path)

    app = create_app(
        cfg, llm, era, calendar, phone, stt, mail=mail, obsidian=obsidian, home_assistant=home_assistant,
        business=business, personal=personal, bridge=bridge, speaker=speaker, static_dir="web/dist",
        airbnb=airbnb, ticketmaster=ticketmaster, kroger=kroger, ccxt=ccxt, letterstream=letterstream,
        git_ops=git_ops,
    )

    logger.info("Jarvis web UI starting on port %d", cfg.web_port)
    uvicorn.run(app, host="0.0.0.0", port=cfg.web_port)


if __name__ == "__main__":
    main()

