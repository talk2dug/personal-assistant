"""Authenticated live camera access for the web UI."""
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...core import vision
from ..auth import require_user

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.get("/{camera_key}/stream")
def stream_camera(camera_key: str, request: Request):
    require_user(request)
    camera = vision.get_camera(request.app.state.cfg.db_path, camera_key)
    if camera is None:
        raise HTTPException(404, "camera not found")
    if camera["kind"] != "mjpeg":
        raise HTTPException(501, "this camera type does not support live web streaming yet")

    def chunks():
        try:
            with urllib.request.urlopen(camera["url"], timeout=10) as response:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        except Exception:
            return

    return StreamingResponse(
        chunks(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )