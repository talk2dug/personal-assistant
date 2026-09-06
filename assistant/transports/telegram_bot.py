"""Telegram transport: long-polling bot restricted to the configured chat_id allowlist.

Long-polling (rather than webhooks) means no inbound firewall/port-forwarding changes
are needed — the bot just reaches out to Telegram's servers.
"""
import asyncio
import functools
import logging

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..core import db
from ..core.engine import handle_message

logger = logging.getLogger(__name__)


def build_application(
    token: str, db_path: str, llm, tz_name: str = "America/New_York", era=None, calendar=None, phone=None, mail=None,
    obsidian=None, home_assistant=None, business=None, personal=None, airbnb=None, ticketmaster=None, kroger=None,
    ccxt=None, letterstream=None, git_ops=None,
) -> Application:
    """era, phone, mail, obsidian, and home_assistant (engine.EraContext / PhoneContext /
    MailContext / ObsidianContext / HomeAssistantContext) are exposed only to users with
    role == 'owner' — the partner's chat never sees Era's finance tools, control of the owner's
    phone, the owner's inbox, the owner's vault, or smart-home control. calendar
    (engine.CalendarContext) applies to both roles, same as the reminder tools it hooks into.
    airbnb/ticketmaster/kroger follow the owner-only rule too: kroger holds the owner's real
    cart, and there's no reason for airbnb/ticketmaster to be a one-off exception to it."""
    application = Application.builder().token(token).build()

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = str(update.effective_chat.id)
        user = db.get_user_by_chat_id(db_path, chat_id)
        if user is None:
            logger.warning("rejected message from unrecognized chat_id %s", chat_id)
            return
        is_owner = user["role"] == "owner"
        user_era = era if is_owner else None
        user_phone = phone if is_owner else None
        user_mail = mail if is_owner else None
        user_obsidian = obsidian if is_owner else None
        user_home_assistant = home_assistant if is_owner else None
        user_business = business if is_owner else None
        user_personal = personal if is_owner else None
        user_airbnb = airbnb if is_owner else None
        user_ticketmaster = ticketmaster if is_owner else None
        user_kroger = kroger if is_owner else None
        user_ccxt = ccxt if is_owner else None
        user_letterstream = letterstream if is_owner else None
        user_git_ops = git_ops if is_owner else None
        # handle_message does a blocking HTTP call to simrig (and sometimes Era/Apple/the phone); run
        # it off the event loop thread so one user's request can't stall the other's.
        loop = asyncio.get_running_loop()
        call = functools.partial(
            handle_message, db_path, llm, user["id"], update.message.text,
            tz_name=tz_name, era=user_era, calendar=calendar, phone=user_phone, mail=user_mail,
            obsidian=user_obsidian, home_assistant=user_home_assistant, business=user_business,
            personal=user_personal, airbnb=user_airbnb, ticketmaster=user_ticketmaster,
            kroger=user_kroger, ccxt=user_ccxt, letterstream=user_letterstream, git_ops=user_git_ops,
        )
        reply = await loop.run_in_executor(None, call)
        await update.message.reply_text(reply)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return application


def make_notifier(application: Application, loop: asyncio.AbstractEventLoop):
    """Returns a sync notify(chat_id, text) callback safe to call from the
    scheduler's background thread — it hands the send off to the bot's event loop."""

    def notify(chat_id: str, text: str) -> None:
        future = asyncio.run_coroutine_threadsafe(
            application.bot.send_message(chat_id=chat_id, text=text), loop
        )
        future.result(timeout=30)

    return notify
