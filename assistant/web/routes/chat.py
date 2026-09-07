"""The web equivalent of transports/telegram_bot.py — same engine.handle_message,
same conversation history table, just a different transport. A section, per the
project's 'new section = new router, registered once' convention."""
import asyncio
import base64
import functools

from fastapi import APIRouter, HTTPException, Request, UploadFile

from ...core import db, vision
from ...core.engine import handle_message
from ..auth import require_user

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.get("/history")
async def history(request: Request):
    user = require_user(request)
    cfg = request.app.state.cfg
    return db.recent_messages(cfg.db_path, user["id"], limit=50)


@router.post("/message")
async def send_message(request: Request):
    user = require_user(request)
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return {"reply": ""}

    # Optional camera snapshot from the web UI's on-demand capture toggle — a data:
    # URL (e.g. "data:image/jpeg;base64,...") so we strip the prefix before decoding.
    image_bytes = None
    image_data_url = body.get("image")
    if image_data_url:
        try:
            image_bytes = base64.b64decode(image_data_url.split(",", 1)[-1])
        except Exception:
            raise HTTPException(status_code=400, detail="invalid image data")

    cfg = request.app.state.cfg
    is_owner = user["role"] == "owner"
    era = request.app.state.era if is_owner else None
    phone = request.app.state.phone if is_owner else None
    mail = request.app.state.mail if is_owner else None
    obsidian = request.app.state.obsidian if is_owner else None
    home_assistant = request.app.state.home_assistant if is_owner else None
    business = request.app.state.business if is_owner else None
    personal = request.app.state.personal if is_owner else None
    airbnb = request.app.state.airbnb if is_owner else None
    ticketmaster = request.app.state.ticketmaster if is_owner else None
    kroger = request.app.state.kroger if is_owner else None
    ccxt = request.app.state.ccxt if is_owner else None
    letterstream = request.app.state.letterstream if is_owner else None
    git_ops = request.app.state.git_ops if is_owner else None
    recipe = request.app.state.recipe if is_owner else None
    # handle_message blocks (LLM call to simrig, and sometimes Era/CalDAV/the phone) and Era/phone
    # tool calls internally use asyncio.run(), which raises if called from a thread that already
    # has a running event loop — this route runs on uvicorn's event loop, so handle_message must
    # go through a thread executor. Same pattern as transports/telegram_bot.py's on_message.
    loop = asyncio.get_running_loop()
    call = functools.partial(
        handle_message, cfg.db_path, request.app.state.llm, user["id"], text,
        tz_name=cfg.timezone, era=era, calendar=request.app.state.calendar, phone=phone, mail=mail,
        obsidian=obsidian, home_assistant=home_assistant, business=business, personal=personal,
        image_bytes=image_bytes,
        airbnb=airbnb, ticketmaster=ticketmaster, kroger=kroger, ccxt=ccxt, letterstream=letterstream,
        git_ops=git_ops, recipe=recipe,
    )
    reply = await loop.run_in_executor(None, call)
    # show_camera (if the model called it this turn) leaves its result here rather than
    # returning it directly -- see pending_camera_views in vision.py for why: the agentic
    # backend dispatches tool calls from a subprocess via routes/tools.py, not from this
    # handler, so a plain Python variable couldn't carry it back to this response.
    camera = vision.pop_pending_camera_view(cfg.db_path, user["id"])
    return {"reply": reply, **({"camera": camera} if camera else {})}


@router.post("/transcribe")
async def transcribe(request: Request, audio: UploadFile):
    require_user(request)
    stt = request.app.state.stt
    if stt is None:
        raise HTTPException(status_code=503, detail="speech-to-text is not configured")

    audio_bytes = await audio.read()
    # Whisper inference is blocking CPU work — same executor pattern as handle_message
    # above, so it doesn't stall uvicorn's event loop for every other request.
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(None, stt.transcribe, audio_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"transcription failed: {e}")
    return {"text": text}
