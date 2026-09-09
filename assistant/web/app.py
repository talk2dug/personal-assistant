"""FastAPI backend for the Jarvis web UI — serves the built React app and exposes
/api/*. Runs as its own process (jarvis-web.service on pi5nas002), sharing jarvis.db
with the Telegram bot process (safe via WAL mode) but nothing else — see the Phase 4
plan for why this is a second process rather than threaded into the existing one.

Adding a new section (future agents): add a router module under routes/, include it
below. That convention is the whole point of splitting things this way.
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware


class SPAStaticFiles(StaticFiles):
    """Serves the built React app, falling back to index.html for unknown paths.

    Client-side routes (/finance, /review, /device) don't exist as files, so a *direct*
    load of one — a hard refresh, a bookmark, or a kiosk browser opening a deep link —
    404s under a plain static mount. That went unnoticed for a long time because the UI
    is always entered at / and navigates client-side; the Pi terminal, which boots
    straight to /device, hit it immediately.

    Anything under /api keeps its real 404: a missing endpoint must look like a missing
    endpoint, not silently return an HTML page to something expecting JSON.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # Check the real request path, not the mount-relative one: what StaticFiles
            # is handed here isn't reliably prefixed the way you'd expect.
            request_path = scope.get("path", "")
            if exc.status_code == 404 and not request_path.startswith("/api/"):
                return await super().get_response("index.html", scope)
            raise

from .auth import router as auth_router
from .routes.agents import router as agents_router
from .routes.chat import router as chat_router
from .routes.crypto import router as crypto_router
from .routes.devices import router as devices_router
from .routes.email_drafts import router as email_drafts_router
from .routes.notifications import router as notifications_router
from .routes.finance import router as finance_router
from .routes.grocery import router as grocery_router
from .routes.media import router as media_router
from .routes.openai_compat import router as openai_compat_router
from .routes.personal_tasks import router as personal_tasks_router
from .routes.review import router as review_router
from .routes.schedule import router as schedule_router
from .routes.tools import router as tools_router
from .routes.vision import router as vision_router
from .routes.weather import router as weather_router


def create_app(
    cfg, llm, era, calendar, phone=None, stt=None, mail=None, obsidian=None, home_assistant=None,
    business=None, personal=None, bridge=None, speaker=None, static_dir: str | None = None,
    airbnb=None, ticketmaster=None, kroger=None, ccxt=None, letterstream=None,
) -> FastAPI:
    app = FastAPI(title="Jarvis")
    app.add_middleware(SessionMiddleware, secret_key=cfg.web_session_secret or "dev-insecure-secret-change-me")

    app.state.cfg = cfg
    app.state.llm = llm
    app.state.era = era
    app.state.calendar = calendar
    app.state.phone = phone
    app.state.stt = stt
    app.state.mail = mail
    app.state.obsidian = obsidian
    app.state.home_assistant = home_assistant
    app.state.business = business
    app.state.personal = personal
    app.state.bridge = bridge
    app.state.speaker = speaker
    app.state.airbnb = airbnb
    app.state.ticketmaster = ticketmaster
    app.state.kroger = kroger
    app.state.ccxt = ccxt
    app.state.letterstream = letterstream

    app.include_router(auth_router)
    app.include_router(chat_router)
    app.include_router(finance_router)
    app.include_router(openai_compat_router)
    app.include_router(tools_router)
    app.include_router(agents_router)
    app.include_router(crypto_router)
    app.include_router(review_router)
    app.include_router(devices_router)
    app.include_router(notifications_router)
    app.include_router(media_router)
    app.include_router(schedule_router)
    app.include_router(weather_router)
    app.include_router(personal_tasks_router)
    app.include_router(grocery_router)
    app.include_router(vision_router)

    if static_dir and Path(static_dir).is_dir():
        app.mount("/", SPAStaticFiles(directory=static_dir, html=True), name="static")

    return app



