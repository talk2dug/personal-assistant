"""Room presence / identity API — camera registration, current presence, known people,
and the enrollment flow. Owner-only throughout: this is who's-in-the-house data, and
unlike the review queue or chat there's no meaningful "partner" view of it yet.

Enrollment is deliberately two calls, not one: this process (jarvis-web) never has
InsightFace loaded (see core/face_recognizer.py's docstring on why that stays isolated
in .venv-vision), so POST /people/enroll only creates a pending request row; the
camera-watch process (jarvis-vision) is what actually looks at frames and calls
vision.record_enrollment_sample as it collects them. GET .../enroll/{id} is how the
owner watches that progress from the web UI.
"""
from fastapi import APIRouter, HTTPException, Request

from ...core import vision
from ..auth import require_user

router = APIRouter(prefix="/api/vision", tags=["vision"])


def _require_owner(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "owner":
        raise HTTPException(403, "room presence/identity is owner-only")
    return user


# --- cameras ------------------------------------------------------------------

@router.get("/cameras")
async def list_cameras(request: Request):
    _require_owner(request)
    cfg = request.app.state.cfg
    return {"cameras": vision.list_cameras(cfg.db_path)}


@router.post("/cameras")
async def add_camera(request: Request):
    """Registers or updates a camera. role='kiosk' + device_id is what makes a
    camera eligible for face recognition and identity gating at all — see
    core/identity.py. Everything else defaults to role='house' (detection only,
    exactly what existed before this chunk)."""
    _require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    for field in ("key", "name", "url"):
        if not body.get(field):
            raise HTTPException(400, f"{field} is required")
    role = body.get("role", "house")
    if role not in vision.CAMERA_ROLES:
        raise HTTPException(400, f"role must be one of {vision.CAMERA_ROLES}")
    if role == "kiosk" and not body.get("device_id"):
        raise HTTPException(400, "a kiosk camera must be paired with a device_id")
    try:
        camera = vision.add_camera(
            cfg.db_path, key=body["key"], name=body["name"], url=body["url"],
            kind=body.get("kind", "mjpeg"), location=body.get("location", ""),
            motion_threshold=body.get("motion_threshold", 0.012),
            recordable=body.get("recordable", True), role=role,
            device_id=body.get("device_id"),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"camera": camera}


# --- presence -------------------------------------------------------------------

@router.get("/presence")
async def presence(request: Request, within_seconds: int = 120):
    _require_owner(request)
    cfg = request.app.state.cfg
    return {"presence": vision.presence_now(cfg.db_path, within_seconds=within_seconds)}


@router.get("/events")
async def events(request: Request, camera_key: str = "", kind: str = "", limit: int = 50):
    _require_owner(request)
    cfg = request.app.state.cfg
    return {"events": vision.recent_events(cfg.db_path, camera_key=camera_key, kind=kind, limit=limit)}


# --- known people ---------------------------------------------------------------

@router.get("/people")
async def list_people(request: Request):
    _require_owner(request)
    cfg = request.app.state.cfg
    # Embeddings are the whole point of this table but are meaningless (and large) to
    # a browser -- strip them from the list view rather than ship 512 floats per sample
    # per person to a page that only ever shows a name and a sample count.
    people = vision.list_known_people(cfg.db_path)
    return {"people": [{k: v for k, v in p.items() if k != "embeddings"} for p in people]}


@router.delete("/people/{key}")
async def delete_person(key: str, request: Request):
    _require_owner(request)
    cfg = request.app.state.cfg
    ok = vision.delete_known_person(cfg.db_path, key)
    if not ok:
        raise HTTPException(404, "no known person with that key")
    return {"ok": True}


@router.get("/unknown-faces")
async def unknown_faces(request: Request, unresolved_only: bool = True):
    _require_owner(request)
    cfg = request.app.state.cfg
    faces = vision.list_unknown_faces(cfg.db_path, unresolved_only=unresolved_only)
    return {"unknown_faces": [{k: v for k, v in f.items() if k != "embedding"} for f in faces]}


# --- enrollment -------------------------------------------------------------------

@router.post("/people/enroll")
async def enroll(request: Request):
    """Starts enrollment: stand in front of the named kiosk camera, then poll the
    returned request id. Single-user today (see core/vision.py's module docstring) --
    nothing stops a second name being enrolled, but nothing in the identity-gating
    path treats a second person any differently from the first; that's the intended
    'no rework needed' seam, not a claim that multi-user is finished."""
    user = _require_owner(request)
    cfg = request.app.state.cfg
    body = await request.json()
    camera_key = body.get("camera_key")
    name = body.get("name")
    if not camera_key or not name:
        raise HTTPException(400, "camera_key and name are required")
    camera = vision.get_camera(cfg.db_path, camera_key)
    if camera is None:
        raise HTTPException(404, "no camera with that key")
    if camera.get("role") != "kiosk":
        raise HTTPException(400, "enrollment only runs against a kiosk camera")
    req = vision.create_enrollment_request(
        cfg.db_path, camera_key=camera_key, person_name=name,
        samples_wanted=body.get("samples_wanted", 5),
        relationship=body.get("relationship", "household"), requested_by=user["id"],
    )
    return {"request": req}


@router.get("/people/enroll/{request_id}")
async def enroll_status(request_id: int, request: Request):
    _require_owner(request)
    cfg = request.app.state.cfg
    req = vision.get_enrollment_request(cfg.db_path, request_id)
    if req is None:
        raise HTTPException(404, "no enrollment request with that id")
    return {"request": req}


@router.post("/people/enroll/{request_id}/cancel")
async def enroll_cancel(request_id: int, request: Request):
    _require_owner(request)
    cfg = request.app.state.cfg
    ok = vision.cancel_enrollment_request(cfg.db_path, request_id)
    if not ok:
        raise HTTPException(404, "no pending enrollment request with that id")
    return {"ok": True}
