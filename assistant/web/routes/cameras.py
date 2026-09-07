"""Authenticated live camera access for the web UI and voice-terminal kiosks."""
import secrets
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...core import vision
from ..auth import require_user

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


def _authorized(request: Request) -> None:
    """Session cookie (the main web UI) or the shared device key (a kiosk terminal --
    same two-path pattern routes/devices.py uses, since a screen on a shelf has nobody
    to log it in)."""
    try:
        require_user(request)
        return
    except HTTPException:
        pass
    device_key = getattr(request.app.state.cfg, "device_api_key", None)
    supplied = request.query_params.get("key", "")
    if device_key and supplied and secrets.compare_digest(supplied, device_key):
        return
    raise HTTPException(401, "not logged in")


@router.get("/{camera_key}/stream")
def stream_camera(camera_key: str, request: Request):
    _authorized(request)
    camera = vision.get_camera(request.app.state.cfg.db_path, camera_key)
    if camera is None:
        raise HTTPException(404, "camera not found")
    if camera["kind"] != "mjpeg":
        raise HTTPException(501, "this camera type does not support live web streaming yet")

    # A camera's stored url is its base (uStreamer's own root serves an HTML preview
    # page, not video -- the actual multipart stream lives at /stream, same convention
    # vision.py's snapshot fetch already follows for /snapshot).
    base = camera["url"].rstrip("/")
    stream_url = base if base.endswith("/stream") else base + "/stream"

    try:
        upstream = urllib.request.urlopen(stream_url, timeout=10)
    except Exception as e:
        raise HTTPException(502, f"camera unreachable: {e}")

    # uStreamer's boundary token isn't a fixed value we can hardcode -- forward its own
    # Content-Type verbatim so the boundary the browser parses on actually matches the
    # one the bytes use, or every part silently fails to split.
    content_type = upstream.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=frame")

    def chunks():
        try:
            with upstream:
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        except Exception:
            return

    return StreamingResponse(
        chunks(),
        media_type=content_type,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )