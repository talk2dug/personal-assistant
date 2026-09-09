"""Cameras, known people, and live presence — the web surface for what vision.py
tracks. Owner-only, like Finance and Personal: this is data about who's in the house,
not something a partner login needs a view onto for themselves.

Deliberately no endpoint to create or edit a known person directly. Enrollment is a
Review-page decision (see routes/review.py's unknown_faces handling), not a form here --
that's the whole point of requirement (3): a new identity is registered by owner
approval, never silently.
"""
from fastapi import APIRouter, HTTPException, Request

from ...core import vision
from ..auth import require_owner

router = APIRouter(prefix="/api/vision", tags=["vision"])


def _strip_embeddings(person: dict) -> dict:
    """Embeddings are a biometric vector, not display data — they never leave the
    server over this API."""
    return {k: v for k, v in person.items() if k != "embeddings"}


@router.get("/cameras")
async def list_cameras(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    return {"cameras": vision.list_cameras(cfg.db_path)}


@router.post("/cameras")
async def add_camera(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    key, name, url = body.get("key"), body.get("name"), body.get("url")
    if not key or not name or not url:
        raise HTTPException(400, "key, name and url are required")
    camera = vision.add_camera(
        cfg.db_path, key, name, url, kind=body.get("kind", "mjpeg"),
        location=body.get("location", ""), motion_threshold=body.get("motion_threshold", 0.012),
        recordable=body.get("recordable", True),
    )
    device_id = body.get("device_id")
    if device_id:
        vision.set_camera_device(cfg.db_path, key, device_id)
        camera["device_id"] = device_id
    return {"ok": True, "camera": camera}


@router.get("/known-people")
async def known_people(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    return {"people": [_strip_embeddings(p) for p in vision.get_known_people(cfg.db_path)]}


@router.get("/presence")
async def presence(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    within = getattr(cfg, "vision_presence_window_seconds", 180)
    raw = vision.presence_now(cfg.db_path, within_seconds=within)
    by_key = {p["key"]: p["name"] for p in vision.get_known_people(cfg.db_path)}
    for cam in raw.values():
        cam["people"] = [by_key.get(k, k) for k in cam["people"]]
    return {"presence": raw}


@router.get("/events")
async def events(request: Request, camera_key: str = "", kind: str = "", limit: int = 50):
    require_owner(request)
    cfg = request.app.state.cfg
    return {"events": vision.recent_events(cfg.db_path, camera_key, kind, limit)}
