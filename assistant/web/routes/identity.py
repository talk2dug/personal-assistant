"""Owner-only identity administration for the Review page: registering a camera,
linking it to a kiosk terminal, enrolling the owner's face, and seeing what the
camera-watch loop couldn't recognize.

Single-user enrollment (locked decision): every enroll call here writes to the one
'owner' row in known_people (identity_gate.OWNER_PERSON_KEY). known_people's schema
supports any number of people (see vision.py) and this stays deliberately narrow --
enrolling anyone else, or turning an unknown_faces row into a one-tap "that's X", is the
enroll-on-recognition-failure feature a later chunk builds; this only ever confirms the
owner.
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from ...core import identity_gate, vision
from ...core.face_recognizer import embed_to_json
from ..auth import require_owner

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/identity", tags=["identity"])

OWNER_NAME_FALLBACK = "Owner"


def _stack(request: Request):
    detector = request.app.state.detector
    recognizer = request.app.state.face_recognizer
    if detector is None or recognizer is None:
        raise HTTPException(503, "the vision/identity stack is not enabled on this server "
                                 "(set vision_enabled and identity_gating_enabled in config.json)")
    return detector, recognizer


@router.get("/status")
async def status(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    person = vision.get_known_person(cfg.db_path, identity_gate.OWNER_PERSON_KEY)
    return {
        "vision_enabled": request.app.state.detector is not None,
        "identity_gating_enabled": bool(getattr(cfg, "identity_gating_enabled", False)),
        "owner_enrolled": bool(person and person["sample_count"] > 0),
        "owner_sample_count": person["sample_count"] if person else 0,
        "match_threshold": getattr(cfg, "identity_match_threshold", None),
    }


@router.get("/cameras")
async def cameras(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    return {"cameras": vision.list_cameras(cfg.db_path)}


@router.post("/cameras")
async def add_camera(request: Request):
    """Registers a camera. There's no auto-discovery here -- the owner supplies the
    uStreamer/RTSP URL, same as any other piece of house infrastructure in this project."""
    require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    key = (body.get("key") or "").strip()
    name = (body.get("name") or "").strip()
    url = (body.get("url") or "").strip()
    if not key or not name or not url:
        raise HTTPException(400, "key, name and url are required")
    try:
        camera = vision.add_camera(
            cfg.db_path, key, name, url, kind=body.get("kind", "mjpeg"),
            location=body.get("location", ""),
            motion_threshold=float(body.get("motion_threshold", 0.012)),
            recordable=bool(body.get("recordable", True)),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"camera": camera}


@router.post("/cameras/{camera_key}/link")
async def link_camera(camera_key: str, request: Request):
    """Marks a camera as the one watching a given kiosk terminal -- what makes it
    eligible for face recognition at all. See the kiosk-only-cameras locked decision.
    Takes effect for the camera-watch loop on the next restart (CameraWatcher builds its
    threads once at startup); identity_gate.resolve() itself reads the link live."""
    require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    device_id = (body.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(400, "device_id is required")
    camera = vision.link_camera_to_device(cfg.db_path, camera_key, device_id)
    if camera is None:
        raise HTTPException(404, "no camera with that key")
    return {"camera": camera}


@router.post("/cameras/{camera_key}/unlink")
async def unlink_camera(camera_key: str, request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    ok = vision.unlink_camera_device(cfg.db_path, camera_key)
    if not ok:
        raise HTTPException(404, "no camera with that key")
    return {"ok": True}


@router.get("/known-people")
async def known_people(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    people = vision.list_known_people(cfg.db_path)
    for p in people:
        p.pop("embeddings", None)  # never ship raw biometric vectors to the browser
    return {"people": people}


@router.get("/unknown-faces")
async def unknown_faces(request: Request):
    require_owner(request)
    cfg = request.app.state.cfg
    faces = vision.list_unknown_faces(cfg.db_path, resolved=False, limit=50)
    for f in faces:
        f.pop("embedding", None)
    return {"unknown_faces": faces}


@router.post("/enroll/owner")
async def enroll_owner(request: Request):
    """Captures one fresh sample from a kiosk camera and adds it to the owner's
    enrollment. Call it a handful of times (different lighting/angle/day) -- matching
    uses the best of every stored sample, so a few real ones beat one perfect one.
    """
    owner = require_owner(request)
    cfg = request.app.state.cfg
    detector, recognizer = _stack(request)
    body = await request.json()
    camera_key = (body.get("camera_key") or "").strip()
    if not camera_key:
        raise HTTPException(400, "camera_key is required")

    camera = vision.get_camera(cfg.db_path, camera_key)
    if camera is None:
        raise HTTPException(404, "no camera with that key")

    frame = vision.FrameSource(camera).snapshot()
    if frame is None:
        raise HTTPException(502, f"could not reach camera {camera_key}")

    detections = detector.detect(frame)
    people = [d for d in detections if d.kind == "person"]
    if not people:
        raise HTTPException(422, "no person visible in the camera right now")
    best = max(people, key=lambda d: d.confidence)

    face = recognizer.best_face(best.crop(frame))
    if face is None:
        raise HTTPException(422, "a person is visible but no face could be resolved — "
                                 "move closer to the camera or check the lighting")

    vision.upsert_known_person(cfg.db_path, identity_gate.OWNER_PERSON_KEY,
                               owner.get("display_name") or OWNER_NAME_FALLBACK,
                               relationship="household")
    person = vision.add_face_sample(cfg.db_path, identity_gate.OWNER_PERSON_KEY,
                                    embed_to_json(face.embedding))
    logger.info("enrolled a new owner face sample from %s (total samples: %d)",
               camera_key, person["sample_count"])
    return {"sample_count": person["sample_count"], "det_score": face.det_score}


@router.delete("/enroll/owner")
async def reset_owner_enrollment(request: Request):
    """Clears every stored sample -- the honest way to start over rather than let a bad
    enrollment (wrong lighting rig, someone else briefly in frame) linger and quietly
    lower match quality forever."""
    require_owner(request)
    cfg = request.app.state.cfg
    ok = vision.delete_known_person(cfg.db_path, identity_gate.OWNER_PERSON_KEY)
    return {"ok": ok}
