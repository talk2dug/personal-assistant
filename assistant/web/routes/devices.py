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
kiosk browser showing the orb has nobody to log it in. That's exactly why `/turn` is also
where Room Presence & Identity gating lives (see assistant/core/presence.py) — unlike
chat.py, which sits behind a real login, anyone standing near an open-mic terminal can
trigger a turn, so *who's actually there* has to be established some other way before
personal or financial context is handed to the model at all.

`/wake_claim` is the other half of the open-mic story: arbitration across terminals so
only the one the owner is actually speaking near answers a given utterance (see
assistant/core/wake_arbitration.py).

A kiosk terminal has no persistent connection to hand a result to outside of /turn's own
response, so anything a tool call stages for it (show_camera's target camera; the kitchen
screen's display_recipe) has to ride along in the same DEVICE_STATE dict the terminal
already polls for its on-screen state — see the camera/camera_seq handling below and
vision.py's pending_camera_views docstring for why a plain function-local variable can't
carry it across the gap between one /turn call and the next poll.
"""
import asyncio
import base64
import functools
import logging
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import Response

from ...core import db, kitchen_db, presence, vision, wake_arbitration
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
    freshly-booted screen shows the orb instead of an error while its client starts.

    Whatever's in DEVICE_STATE for this device rides along verbatim -- including
    camera/camera_seq and recipe/recipe_seq when a tool call staged one for it -- so a
    field that was never set is simply absent from the response rather than present as
    null, letting the client tell "nothing to show" apart from "explicitly cleared".
    """
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


@router.post("/{device_id}/wake_claim")
async def wake_claim(device_id: str, request: Request):
    """Arbitration for a single wake-word moment across every terminal on the LAN --
    see wake_arbitration.py. Called by the device client the instant it hears the wake
    word, before it chimes or starts recording, so at most one terminal answers a given
    utterance even when several were close enough to hear it.
    """
    _require_device_key(request)
    cfg = request.app.state.cfg
    body = await request.json()
    try:
        score = float(body.get("score", 0.0))
    except (TypeError, ValueError):
        raise HTTPException(400, "score must be a number")

    loop = asyncio.get_running_loop()
    proceed = await loop.run_in_executor(
        None, wake_arbitration.claim, device_id, score,
        cfg.wake_arbitration_window_ms, cfg.wake_arbitration_margin,
    )
    return {"proceed": proceed}


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

    # Room Presence & Identity: who is actually standing near this terminal right now,
    # by camera -- not who the device claims to be, since there's no login here at all.
    presence_result = presence.evaluate(
        cfg.db_path, device_id,
        within_seconds=cfg.presence_confirm_window_seconds,
        authorized_access_levels=frozenset(cfg.presence_authorized_access_levels),
    )
    logger.info("device %s presence: %s", device_id, presence_result.reason)

    if presence_result.authorized:
        user_id = _owner_user_id(request)
    else:
        # No confirmed, authorized identity: run this turn as a fresh per-device guest so
        # conversation history and any privately-scoped data stay isolated from the
        # owner's real account, on top of (not instead of) stripping the sensitive
        # contexts below.
        user_id = presence.guest_user_id(cfg.db_path, device_id)

    all_contexts = {
        "era": request.app.state.era, "calendar": request.app.state.calendar,
        "phone": request.app.state.phone, "mail": request.app.state.mail,
        "obsidian": request.app.state.obsidian, "home_assistant": request.app.state.home_assistant,
        "business": request.app.state.business, "personal": request.app.state.personal,
        "airbnb": request.app.state.airbnb, "ticketmaster": request.app.state.ticketmaster,
        "kroger": request.app.state.kroger, "ccxt": request.app.state.ccxt,
        "letterstream": request.app.state.letterstream,
        # Not personal/financial, so not in SENSITIVE_CONTEXT_KEYS -- these stay
        # available from a voice terminal regardless of who presence identifies as
        # speaking, same as before presence gating existed.
        "git_ops": request.app.state.git_ops, "recipe": request.app.state.recipe,
        "local_llm": request.app.state.local_llm,
    }
    gated_contexts = presence.gate_contexts(all_contexts, presence_result)

    call = functools.partial(
        handle_message, cfg.db_path, request.app.state.llm, user_id, transcript,
        tz_name=cfg.timezone, **gated_contexts,
    )
    try:
        reply = await loop.run_in_executor(None, call)
    except Exception as e:
        _set_state(device_id, state="idle", caption="")
        raise HTTPException(500, f"assistant failed: {e}")

    # show_camera (engine.py) stages its result keyed by the user this turn ran as,
    # since that's the only identity a tool call inside handle_message has to hand --
    # relay it into this device's polled state (with an incrementing seq so the kiosk
    # can tell a fresh request from a stale one) and hand it back in this same response
    # too, since the terminal that asked shouldn't have to wait for its own next poll.
    camera_view = vision.pop_pending_camera_view(cfg.db_path, user_id)
    if camera_view is not None:
        next_seq = DEVICE_STATE.get(device_id, {}).get("camera_seq", 0) + 1
        _set_state(device_id, camera=camera_view, camera_seq=next_seq)

    # display_recipe's target is this same device_id when asked at the kiosk's own mic --
    # apply it now so it's in this same response, not just the target's next poll.
    _apply_pending_recipe_view(device_id, cfg.db_path)

    spoken_audio = None
    if speaker is not None and speaker.available() and reply:
        try:
            wav = await loop.run_in_executor(None, speaker.synthesize, reply)
            spoken_audio = base64.b64encode(wav).decode()
        except Exception:
            # A voice failure must still deliver the answer on screen.
            logger.exception("tts failed for device %s", device_id)

    _set_state(device_id, state="speaking", caption=reply)
    return {"transcript": transcript, "reply": reply, "audio": spoken_audio, "camera": camera_view}


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
