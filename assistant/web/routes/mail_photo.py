"""Photograph a piece of post, send it here, and have it read and filed.

Device-key auth rather than a session cookie, because the caller is an iOS Shortcut on
the Share Sheet and a Shortcut cannot hold a login. Same Bearer key the voice terminals
use -- see devices.py's own note on why that key is the boundary here.

The upload half deliberately mirrors kitchen.py's receipt endpoint: read the bytes, keep
the original on disk, run the model off the event loop, hand back a draft. The one
difference is that this also files the result, because the point is that he photographs
the post as it arrives and does not have to come back to a screen afterwards.
"""
import asyncio
import functools
import logging
import pathlib
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, UploadFile

from ...core import mail_photo, personal_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mail-photo", tags=["mail-photo"])

# A phone photo of a letter is a couple of MB; 25 gives room for a burst-mode shot
# without letting the endpoint be used to fill the disk.
MAX_BYTES = 25 * 1024 * 1024
ALLOWED_SUFFIXES = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp")


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
    from ...core import db

    owner = next((u for u in db.all_users(request.app.state.cfg.db_path)
                  if u["role"] == "owner"), None)
    if owner is None:
        raise HTTPException(503, "no owner account")
    return int(owner["id"])


@router.post("")
async def post_mail_photo(request: Request, photo: UploadFile):
    """Read one photographed letter, file it, and raise a task if it needs one."""
    _require_device_key(request)
    cfg = request.app.state.cfg
    owner_id = _owner_user_id(request)

    suffix = pathlib.Path(photo.filename or "").suffix.lower()
    if suffix and suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, f"{suffix} is not an image this can read")

    image_bytes = await photo.read()
    if not image_bytes:
        raise HTTPException(400, "empty photo")
    if len(image_bytes) > MAX_BYTES:
        raise HTTPException(413, f"{len(image_bytes):,} bytes is over the limit")

    # Saved BEFORE the model runs. If the read fails or comes back nonsense, the
    # photograph is still the record, and he does not have to go back to the envelope.
    out_dir = pathlib.Path(cfg.generated_media_path) / "mail_photos"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = out_dir / f"mail_{owner_id}_{int(time.time() * 1000)}{suffix or '.jpg'}"
    saved.write_bytes(image_bytes)

    loop = asyncio.get_running_loop()
    call = functools.partial(mail_photo.read_photo, request.app.state.bridge, image_bytes)
    reading = await loop.run_in_executor(None, call)

    piece_id = mail_photo.record(cfg.db_path, owner_id, reading, photo_path=str(saved))

    # An unreadable photo is still filed -- with the photo attached and a task saying so.
    # Silently dropping it is how a letter gets lost between the doormat and the desk.
    if not reading.get("parsed"):
        task_id = personal_db.create_task(
            cfg.db_path, owner_id,
            "Unreadable mail photo - open the envelope and check it yourself",
            priority="normal", track="personal")
        personal_db.add_task_detail(cfg.db_path, task_id, "note",
                                    reading.get("error") or "the model returned nothing",
                                    label="Why it could not be read")
        personal_db.add_task_detail(cfg.db_path, task_id, "note", str(saved),
                                    label="The photo")
        mail_photo.attach_task(cfg.db_path, piece_id, task_id)
        return {"id": piece_id, "parsed": False, "task_id": task_id,
                "error": reading.get("error"), "photo_path": str(saved)}

    task_id = None
    if mail_photo.should_raise_task(reading):
        task_id = personal_db.create_task(
            cfg.db_path, owner_id, mail_photo.task_text(reading),
            priority="high" if reading.get("deadline_risk") else "normal",
            due_at=reading.get("due_date"), track="personal")
        if reading.get("summary"):
            personal_db.add_task_detail(cfg.db_path, task_id, "note",
                                        reading["summary"], label="What the letter says")
        personal_db.add_task_detail(cfg.db_path, task_id, "note", str(saved),
                                    label="The photo")
        if reading.get("unreadable"):
            personal_db.add_task_detail(
                cfg.db_path, task_id, "note",
                "Could not read: " + ", ".join(reading["unreadable"]),
                label="Check these against the letter")
        mail_photo.attach_task(cfg.db_path, piece_id, task_id)

    return {"id": piece_id, "parsed": True, "task_id": task_id,
            "sender": reading.get("sender"), "kind": reading.get("kind"),
            "summary": reading.get("summary"), "amount": reading.get("amount"),
            "due_date": reading.get("due_date"), "action": reading.get("action"),
            "confidence": reading.get("confidence"),
            "unreadable": reading.get("unreadable"), "photo_path": str(saved)}


@router.get("")
async def list_mail(request: Request, limit: int = 50, status: str | None = None):
    """What has come through the door. Owner session OR device key, so the Shortcut can
    show a confirmation and the web UI can list the pile."""
    from ..auth import require_owner

    try:
        owner = require_owner(request)
        owner_id = int(owner["id"])
    except HTTPException:
        _require_device_key(request)
        owner_id = _owner_user_id(request)
    return {"mail": mail_photo.recent(request.app.state.cfg.db_path, owner_id,
                                      limit=limit, status=status)}


@router.post("/{piece_id}/status")
async def update_status(piece_id: int, request: Request):
    from ..auth import require_owner

    owner = require_owner(request)
    body = await request.json()
    status = (body.get("status") or "").strip()
    try:
        ok = mail_photo.set_status(request.app.state.cfg.db_path, int(owner["id"]),
                                   piece_id, status)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, "no such mail piece")
    return {"ok": True}
