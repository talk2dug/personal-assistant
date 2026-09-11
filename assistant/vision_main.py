"""Entrypoint for the vision worker (jarvis-vision.service) -- camera watching, YOLO11n
detection, and InsightFace identity resolution.

A separate process, and a separate virtualenv (.venv-vision), from jarvis-core (Telegram)
and jarvis-web: torch, ultralytics and insightface are real GPU dependencies that must
never become something the chat/web processes need installed just to answer a question.
See detector.py's own module docstring for why. Runs on the machine with the GPU
(laptop1's RTX 3060) and shares jarvis.db (WAL) with the other two processes exactly the
way the GPU bridge already does -- writes go through assistant.core.vision's functions
only, same discipline as every other module that touches this database.
"""
import logging

from .config import load_config
from .core import vision
from .core.logging_setup import setup_logging
from .core.vision_worker import run_forever

setup_logging("jarvis-vision")
logger = logging.getLogger(__name__)


def main() -> None:
    cfg = load_config()
    vision.init_vision_db(cfg.db_path)

    # Cameras and which terminal each belongs to are config-driven, the same way cfg.users
    # seeds the users table at startup -- so day-one coverage (Touch1, laptop1) is a
    # config entry, not a migration or a manual DB write, and camera #3 later is one more
    # line in each of these two blocks. Same cfg.cameras/device_camera_map fields
    # main.py's/web_main.py's own _seed_cameras() read -- one source of truth, not a
    # second config surface this process alone would understand.
    for cam in cfg.cameras or []:
        vision.add_camera(
            cfg.db_path, cam["key"], cam["name"], cam["url"],
            kind=cam.get("kind", "mjpeg"), location=cam.get("location", ""),
            motion_threshold=cam.get("motion_threshold", 0.012),
            recordable=cam.get("recordable", True),
        )
    for device_id, camera_key in (cfg.device_camera_map or {}).items():
        vision.set_terminal_camera(cfg.db_path, device_id, camera_key)

    cameras = vision.list_cameras(cfg.db_path)
    logger.info("Jarvis vision worker starting: %d camera(s) configured (%d enabled)",
               len(cameras), sum(1 for c in cameras if c["enabled"]))
    run_forever(cfg.db_path, device=cfg.vision_device)


if __name__ == "__main__":
    main()
