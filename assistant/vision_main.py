"""Entrypoint for the camera-watch service (jarvis-vision) -- runs in its own
.venv-vision (torch/ultralytics/insightface/onnxruntime live there, never in the
assistant's shared .venv, see requirements-vision.txt), a separate process from
jarvis-core and jarvis-web, sharing jarvis.db the same WAL-safe way every other Jarvis
process does.

Kiosk-only face recognition, the camera-watch loop, InsightFace/ArcFace -- see
core/vision_watch.py and core/face_recognizer.py for what actually happens; this file
is only the wiring, same role main.py/web_main.py play for their processes.
"""
import logging
import time

from .config import load_config
from .core import vision
from .core.detector import Detector
from .core.logging_setup import setup_logging
from .core.vision_watch import start_watchers

setup_logging("jarvis-vision")
logger = logging.getLogger(__name__)


def main() -> None:
    cfg = load_config()
    vision.init_vision_db(cfg.db_path)

    detector = Detector()

    recognizer = None
    if cfg.face_recognition_enabled:
        try:
            from .core.face_recognizer import FaceRecognizer
            recognizer = FaceRecognizer()
        except Exception:
            # A missing/broken InsightFace install must not take down motion/pet
            # detection on the house cameras -- same defensive posture as every other
            # optional integration in setup.py. Kiosk cameras simply run detection-only
            # until this is fixed, and identity.py's fail-closed default means nothing
            # downstream silently assumes the owner because of it.
            logger.exception(
                "InsightFace unavailable -- kiosk cameras will run detection-only "
                "(no identity; gating stays fail-closed) until this is fixed"
            )

    cameras = vision.list_cameras(cfg.db_path, enabled_only=True)
    if not cameras:
        logger.warning(
            "no cameras registered -- add one via POST /api/vision/cameras before "
            "this process has anything to watch"
        )
    start_watchers(cfg.db_path, detector, recognizer)

    logger.info("jarvis-vision watching %d camera(s), face recognition %s",
               len(cameras), "on" if recognizer else "off")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("shutting down")


if __name__ == "__main__":
    main()
