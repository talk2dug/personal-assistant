"""Photograph a piece of post, send it here, and have it read and filed.

Device-key auth rather than a session cookie, because the caller is an iOS Shortcut on
the Share Sheet and a Shortcut cannot hold a login. Same Bearer key the voice terminals
use -- see devices.py's own note on why that key is the boundary here.

The upload half mirrors kitchen.py's receipt endpoint -- read the bytes, keep the
original on disk -- but the response does NOT wait for the model. Reading a letter takes
about twenty seconds on simrig, and a cellular upload takes its own time before that; the
first real photo Jack sent timed out on his phone while the server was still working
happily. So the request ends as soon as the bytes are safe on disk, and the reading
happens afterwards. He photographs the post as it arrives and never waits on a screen.
"""
import logging
import pathlib
import secrets
import time

from fastapi import (APIRouter, BackgroundTasks, Depends, HTTPException, Request,
                     UploadFile)

from ...core import mail_photo, personal_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mail-photo", tags=["mail-photo"])

# A phone photo of a letter is a couple of MB; 25 gives room for a burst-mode shot
# without letting the endpoint be used to fill the disk.
MAX_BYTES = 25 * 1024 * 1024
ALLOWED_SUFFIXES = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp")


def _require_device_key(request: Request) -> None:
    """The door, and it says out loud when someone is turned away.

    Setting a Shortcut up is done blind: iOS shows nothing unless a Show Result action
    was added, so a first attempt that fails looks identical to one that worked. These
    lines are what makes the difference visible from this end. They record the SHAPE of
    what arrived -- whether a header was present, whether it was spelled Bearer -- and
    never the key itself, which would put the credential in a log that rotates to disk.
    """
    cfg = request.app.state.cfg
    key = getattr(cfg, "device_api_key", None)
    if not key:
        raise HTTPException(503, "device endpoints are not configured")

    header = request.headers.get("authorization", "")
    supplied = header.removeprefix("Bearer ").strip()
    via = "header"
    if not supplied:
        supplied = request.query_params.get("key", "")
        via = "query"
    if not supplied:
        logger.warning(
            "mail-photo: refused, no key at all (authorization header %s, from %s)",
            "present but unparsed: %r" % header[:12] if header else "absent",
            request.client.host if request.client else "?")
        raise HTTPException(401, "invalid device key")
    if not secrets.compare_digest(supplied, key):
        logger.warning(
            "mail-photo: refused, wrong key via %s (%d chars, expected %d, from %s)",
            via, len(supplied), len(key),
            request.client.host if request.client else "?")
        raise HTTPException(401, "invalid device key")


def _owner_user_id(request: Request) -> int:
    from ...core import db

    owner = next((u for u in db.all_users(request.app.state.cfg.db_path)
                  if u["role"] == "owner"), None)
    if owner is None:
        raise HTTPException(503, "no owner account")
    return int(owner["id"])


# Declared as a dependency rather than called in the body, so it runs BEFORE FastAPI
# validates the upload field. Called inside the function it answered an unauthenticated
# POST with 422 ("photo field required") instead of 401 -- which leaks the endpoint's
# shape to anyone probing, and, more practically, means a Shortcut with the wrong field
# name and a Shortcut with the wrong key look identical while he is setting it up.
@router.post("", dependencies=[Depends(_require_device_key)])
async def post_mail_photo(request: Request, background: BackgroundTasks,
                          photo: UploadFile):
    """Read one photographed letter, file it, and raise a task if it needs one."""
    cfg = request.app.state.cfg
    owner_id = _owner_user_id(request)

    # Logged before anything can reject it, so a rejected upload is still visible as an
    # arrival rather than as silence.
    logger.info("mail-photo: received %r (%s) from %s", photo.filename,
                photo.content_type, request.client.host if request.client else "?")

    suffix = pathlib.Path(photo.filename or "").suffix.lower()
    if suffix and suffix not in ALLOWED_SUFFIXES:
        logger.warning("mail-photo: refused suffix %r from filename %r",
                       suffix, photo.filename)
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

    piece_id = mail_photo.record_pending(cfg.db_path, owner_id, str(saved))
    logger.info("mail-photo: piece %s saved (%d bytes), reading it in the background",
                piece_id, len(image_bytes))

    # The response ends HERE, as soon as the bytes are safe on disk. Reading a letter
    # takes about twenty seconds on simrig and a cellular upload takes its own time on
    # top; making the phone hold the connection for both is what timed out the first
    # real photo Jack sent. The reading happens after the response has gone.
    background.add_task(_read_and_file, cfg.db_path, owner_id, piece_id,
                        request.app.state.bridge, image_bytes, str(saved))
    return {"id": piece_id, "status": "received", "photo_path": str(saved),
            "note": "reading it now - it will appear in your mail list shortly"}


def _read_and_file(db_path: str, owner_id: int, piece_id: int, bridge,
                   image_bytes: bytes, saved: str) -> None:
    """Runs after the response. Must never raise: nothing is listening any more, and an
    exception here would lose the letter silently, which is the failure this whole
    feature exists to prevent."""
    try:
        reading = mail_photo.read_photo(bridge, image_bytes)
        mail_photo.apply_reading(db_path, piece_id, reading)

        if not reading.get("parsed"):
            task_id = personal_db.create_task(
                db_path, owner_id,
                "Unreadable mail photo - open the envelope and check it yourself",
                priority="normal", track="personal")
            personal_db.add_task_detail(
                db_path, task_id, "note",
                reading.get("error") or "the model returned nothing",
                label="Why it could not be read")
            personal_db.add_task_detail(db_path, task_id, "note", saved,
                                        label="The photo")
            mail_photo.attach_task(db_path, piece_id, task_id)
            logger.warning("mail-photo: piece %s could not be read: %s",
                           piece_id, reading.get("error"))
            return

        logger.info("mail-photo: piece %s read as %s from %r (confidence %s)",
                    piece_id, reading.get("kind"), reading.get("sender"),
                    reading.get("confidence"))

        if mail_photo.should_raise_task(reading):
            task_id = personal_db.create_task(
                db_path, owner_id, mail_photo.task_text(reading),
                priority="high" if reading.get("deadline_risk") else "normal",
                due_at=reading.get("due_date"), track="personal")
            if reading.get("summary"):
                personal_db.add_task_detail(db_path, task_id, "note",
                                            reading["summary"],
                                            label="What the letter says")
            personal_db.add_task_detail(db_path, task_id, "note", saved,
                                        label="The photo")
            if reading.get("unreadable"):
                personal_db.add_task_detail(
                    db_path, task_id, "note",
                    "Could not read: " + ", ".join(reading["unreadable"]),
                    label="Check these against the letter")
            mail_photo.attach_task(db_path, piece_id, task_id)
    except Exception:                                               # noqa: BLE001
        logger.exception("mail-photo: reading piece %s failed after the response",
                         piece_id)


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
