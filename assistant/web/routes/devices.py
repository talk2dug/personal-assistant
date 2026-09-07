"""Standalone voice devices — the Pi terminals that replace the Alexas.

Each device is deliberately thin: it listens for the wake word, records, and plays audio
back. Everything else happens here. That keeps every unit identical, so building the
fourth one is flashing an SD card rather than another integration, and upgrading the
assistant upgrades all of them at once.

`/turn` does a whole exchange in one request — audio in, transcript + reply + speech out.
A device on the far side of the house shouldn't need four round trips to answer one
question, and doing it server-side means the device's displayed state can be advanced as
each stage completes rather than the client guessing.

Auth is a static device key, not the session cookie: these clients are headless, and the
kiosk browser showing the orb has nobody to log it in.
"""
import asyncio
import base64
import functools
import logging
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import Response

from ...core import db, kitchen_db, vision
from ...core.engine import handle_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/devices", tags=["devices"])

# Live per-device state, in memory on purpose: it describes what a device is doing right
# now, which is meaningless after a restart. Devices re-report within seconds.
DEVICE_STATE: dict[str, dict] = {}

# A device that hasn't reported in this long is treated as gone rather than stuck in
# whatever it was last doing.
STALE_AFTER_SEC = 90


def _require_device_key(request: Request) -> None:
    cfg = request.app.state.cfg
    key = getattr(cfg, "device_api_key", None)
    if not key:
        raise HTTPException(503, "device endpoints are not configured")
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not supplied:
        supplied = request.query_params.get("key", "")
    if not supplied or not secrets.compare_digest(supplied, key):
        raise HTTPException(401, "invalid device key")


def _owner_user_id(request: Request) -> int:
    cfg = request.app.state.cfg
    owner_cfg = next((u for u in cfg.users if u.role == "owner"), None)
    if owner_cfg is None:
        raise HTTPException(500, "no owner configured")
    owner = db.get_user_by_chat_id(cfg.db_path, owner_cfg.telegram_chat_id)
    if owner is None:
        raise HTTPException(500, "owner not found in db")
    return owner["id"]


def _set_state(device_id: str, **fields) -> dict:
    entry = DEVICE_STATE.setdefault(device_id, {"device_id": device_id, "state": "idle", "caption": ""})
    entry.update(fields)
    entry["updated_at"] = time.time()
    return entry


def _apply_pending_recipe_view(device_id: str, db_path: str) -> None:
    """display_recipe (if called from anywhere in the house) leaves its result here for
    whichever device_id it targeted -- unlike show_camera's pending_camera_views (keyed
    by user_id, always the device mid-interaction with whoever's talking), a recipe's
    target is an explicit argument that can be a *different* device than the one issuing
    the command, so this has to be checked from both /turn's own-device fast path (the
    kiosk's own mic, for an immediate same-turn display) and the top of GET /{device_id}
    (the regular ~700ms poll, for a command issued elsewhere in the house)."""
    recipe = kitchen_db.pop_pending_recipe_view(db_path, device_id)
    if recipe is not None:
        _set_state(device_id, recipe=recipe, recipe_seq=DEVICE_STATE.get(device_id, {}).get("recipe_seq", 0) + 1)


@router.post("/{device_id}/state")
async def report_state(device_id: str, request: Request):
    """A device telling us what it's doing, so the screen can show it."""
    _require_device_key(request)
    body = await request.json()
    entry = _set_state(
        device_id,
        state=body.get("state", "idle"),
        caption=body.get("caption", ""),
        name=body.get("name") or DEVICE_STATE.get(device_id, {}).get("name") or device_id,
    )
    return {"ok": True, "device": entry}


@router.get("/{device_id}")
async def get_state(device_id: str, request: Request):
    """What the kiosk page polls. Falls back to a sane idle rather than 404ing, so a
    freshly-booted screen shows the orb instead of an error while its client starts."""
    _require_device_key(request)
    cfg = request.app.state.cfg
    _apply_pending_recipe_view(device_id, cfg.db_path)
    entry = DEVICE_STATE.get(device_id)
    if entry is None:
        return {"device_id": device_id, "state": "offline", "caption": "", "online": False}
    online = (time.time() - entry.get("updated_at", 0)) < STALE_AFTER_SEC
    return {**entry, "online": online, "state": entry["state"] if online else "offline"}


@router.get("")
async def list_devices(request: Request):
    _require_device_key(request)
    now = time.time()
    return {"devices": [
        {**d, "online": (now - d.get("updated_at", 0)) < STALE_AFTER_SEC}
        for d in DEVICE_STATE.values()
    ]}


@router.post("/{device_id}/turn")
async def turn(device_id: str, request: Request, audio: UploadFile):
    """One full exchange: recorded audio in, transcript + reply + spoken audio out."""
    _require_device_key(request)
    cfg = request.app.state.cfg
    stt = request.app.state.stt
    speaker = request.app.state.speaker
    if stt is None:
        raise HTTPException(503, "speech-to-text is not configured")

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(400, "empty audio")

    loop = asyncio.get_running_loop()
    _set_state(device_id, state="thinking", caption="")

    try:
        transcript = await loop.run_in_executor(None, stt.transcribe, audio_bytes)
    except Exception as e:
        _set_state(device_id, state="idle", caption="")
        raise HTTPException(500, f"transcription failed: {e}")

    if not transcript.strip():
        # Wake-word false trigger, or the room was quiet. Say nothing rather than
        # answering a question nobody asked.
        _set_state(device_id, state="idle", caption="")
        return {"transcript": "", "reply": "", "audio": None, "heard_nothing": True}

    _set_state(device_id, state="thinking", caption=transcript)

    owner_id = _owner_user_id(request)
    call = functools.partial(
        handle_message, cfg.db_path, request.app.state.llm, owner_id, transcript,
        tz_name=cfg.timezone, era=request.app.state.era, calendar=request.app.state.calendar,
        phone=request.app.state.phone, mail=request.app.state.mail,
        obsidian=request.app.state.obsidian, home_assistant=request.app.state.home_assistant,
        business=request.app.state.business, personal=request.app.state.personal,
        airbnb=request.app.state.airbnb, ticketmaster=request.app.state.ticketmaster,
        kroger=request.app.state.kroger, ccxt=request.app.state.ccxt,
        letterstream=request.app.state.letterstream, git_ops=request.app.state.git_ops,
        recipe=request.app.state.recipe,
    )
    try:
        reply = await loop.run_in_executor(None, call)
    except Exception as e:
        _set_state(device_id, state="idle", caption="")
        raise HTTPException(500, f"assistant failed: {e}")

    spoken_audio = None
    if speaker is not None and speaker.available() and reply:
        try:
            wav = await loop.run_in_executor(None, speaker.synthesize, reply)
            spoken_audio = base64.b64encode(wav).decode()
        except Exception:
            # A voice failure must still deliver the answer on screen.
            logger.exception("tts failed for device %s", device_id)

    # show_camera (if this turn called it) leaves its result here rather than
    # returning it directly through handle_message -- see pending_camera_views in
    # vision.py. The kiosk screen only ever polls /{device_id} for its state, so the
    # camera has to ride along on that same polled object, not just this response;
    # camera_seq lets Device.jsx notice a *new* one without the server needing to
    # "clear" it afterward (a GET poll shouldn't have side effects).
    camera = vision.pop_pending_camera_view(cfg.db_path, owner_id)
    state_fields = {"state": "speaking", "caption": reply}
    if camera is not None:
        state_fields["camera"] = camera
        state_fields["camera_seq"] = DEVICE_STATE.get(device_id, {}).get("camera_seq", 0) + 1
    _set_state(device_id, **state_fields)
    # display_recipe's target is this same device_id when asked at the kiosk's own mic --
    # check it here too (on top of get_state's poll-based check) so that case shows up
    # within this same turn rather than waiting for the next ~700ms poll.
    _apply_pending_recipe_view(device_id, cfg.db_path)
    return {"transcript": transcript, "reply": reply, "audio": spoken_audio, "camera": camera}


@router.post("/say")
async def say(request: Request):
    """Synthesise arbitrary text — used for chimes/announcements and for testing a voice
    without going through a whole turn."""
    _require_device_key(request)
    speaker = request.app.state.speaker
    if speaker is None or not speaker.available():
        raise HTTPException(503, "text-to-speech is not configured")
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    loop = asyncio.get_running_loop()
    try:
        wav = await loop.run_in_executor(None, speaker.synthesize, text)
    except Exception as e:
        raise HTTPException(500, f"speech synthesis failed: {e}")
    return Response(content=wav, media_type="audio/wav")
