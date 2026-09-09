"""Cameras, presence and identity — the read surface over the detection pipeline in
core/vision.py, plus the one thing a device client needs to decide on its own: whether
to listen continuously right now.

Deliberately read-mostly. The pipeline that actually writes vision_events/known_people/
unknown_faces rows runs in scripts/vision_worker.py, in its own .venv-vision process (see
that module's docstring) -- this app process never imports detector.py,
face_recognition.py or camera_watch.py, only vision.py's plain-sqlite functions. Camera
CRUD is the one write here, and it's just metadata (a URL and a name), not a model.
"""
import secrets

from fastapi import APIRouter, HTTPException, Request

from ...core import vision
from ..auth import require_user

router = APIRouter(prefix="/api/vision", tags=["vision"])


def _owner_id(request: Request) -> int:
    user = require_user(request)
    if user["role"] != "owner":
        raise HTTPException(403, "vision/cameras are owner-only")
    return user["id"]


def _require_device_key(request: Request) -> None:
    """Same auth as every other /api/devices/* route: a static device key, not the
    session cookie -- the client polling this has nobody to log it in."""
    cfg = request.app.state.cfg
    key = getattr(cfg, "device_api_key", None)
    if not key:
        raise HTTPException(503, "device endpoints are not configured")
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not supplied or not secrets.compare_digest(supplied, key):
        raise HTTPException(401, "invalid device key")


@router.get("/cameras")
async def cameras(request: Request):
    _owner_id(request)
    cfg = request.app.state.cfg
    return {"cameras": vision.list_cameras(cfg.db_path)}


@router.post("/cameras")
async def add_camera(request: Request):
    """Adds or updates one camera. Day one this is called at startup from config.json's
    'cameras' list (Touch1, laptop1); exposed here too so the owner can add the next one
    from the web UI without a config edit and a restart."""
    _owner_id(request)
    cfg = request.app.state.cfg
    body = await request.json()
    for field in ("key", "name", "url"):
        if not body.get(field):
            raise HTTPException(400, f"{field} is required")
    try:
        cam = vision.add_camera(
            cfg.db_path, body["key"], body["name"], body["url"],
            kind=body.get("kind", "mjpeg"), location=body.get("location", ""),
            motion_threshold=body.get("motion_threshold", 0.012),
            recordable=body.get("recordable", True),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"camera": cam}


@router.get("/presence")
async def presence(request: Request, within_seconds: int = 120):
    _owner_id(request)
    cfg = request.app.state.cfg
    return {
        "cameras": vision.presence_now(cfg.db_path, within_seconds=within_seconds),
        "known_people": vision.known_people_present(cfg.db_path, within_seconds=within_seconds),
    }


@router.get("/events")
async def events(request: Request, camera_key: str = "", kind: str = "", limit: int = 50):
    _owner_id(request)
    cfg = request.app.state.cfg
    return {"events": vision.recent_events(cfg.db_path, camera_key=camera_key, kind=kind, limit=limit)}


@router.get("/people")
async def people(request: Request):
    _owner_id(request)
    cfg = request.app.state.cfg
    return {"people": vision.list_known_people(cfg.db_path)}


@router.get("/conversation-mode/{device_id}")
async def conversation_mode(device_id: str, request: Request):
    """Whether device_id should listen continuously without a wake word right now --
    polled by the device client every few seconds (see jarvis_device.py's open-mic
    poller). True only when someone the house actually recognises is currently in view
    of that device's camera; an unrecognised person in frame does NOT enable it, same
    reasoning as detector.py's confidence gating -- open-mic triggered by a stranger or a
    false positive is the expensive mistake here, not a missed continuous-listening
    window.

    Which camera watches which device's room is config (device_camera_map), not code --
    day one it's the identity map (Touch1 the terminal is Touch1 the camera; laptop1's
    webcam watches laptop1), and a device/camera split later is a config change.
    """
    _require_device_key(request)
    cfg = request.app.state.cfg
    camera_key = (getattr(cfg, "device_camera_map", None) or {}).get(device_id)
    if not camera_key:
        return {"open_mic": False, "person": None}

    cams = vision.presence_now(cfg.db_path, within_seconds=45)
    present_here = set(cams.get(camera_key, {}).get("people", []))
    if not present_here:
        return {"open_mic": False, "person": None}

    known = vision.known_people_present(cfg.db_path, within_seconds=45)
    match = next((p for p in known if p["key"] in present_here), None)
    return {"open_mic": match is not None, "person": match["name"] if match else None}
