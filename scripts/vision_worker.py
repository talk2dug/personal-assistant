#!/usr/bin/env python3
"""Jarvis vision worker — watches the house cameras (Touch1 and laptop1, day one),
turning YOLO detections and insightface face matches into the presence/identity rows in
core/vision.py, and a persistently-unrecognised face into a question on the Review page.

Deliberately its own process, under its own .venv-vision (see requirements-vision.txt at
the repo root). jarvis-core.service and jarvis-web.service run in the shared .venv and
must never depend on torch/ultralytics/insightface — see core/detector.py and
core/face_recognition.py's docstrings for why. This script is the only place
core/camera_watch.py is imported.

It shares jarvis.db with the other two processes over SQLite's WAL mode, exactly the way
jarvis-core.service and jarvis-web.service already share it — no new coordination
mechanism, just another local writer.

Install and run (on the host with the RTX 3060):
    python -m venv .venv-vision
    .venv-vision\\Scripts\\pip install -r requirements-vision.txt
    .venv-vision\\Scripts\\pip install torch --index-url https://download.pytorch.org/whl/cu121
    .venv-vision\\Scripts\\python.exe scripts\\vision_worker.py --config config.json
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Run as `python scripts/vision_worker.py`, not `python -m` — this makes `assistant`
# importable either way without requiring the caller to set PYTHONPATH themselves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.config import load_config          # noqa: E402
from assistant.core import business_db, db, vision  # noqa: E402
from assistant.core.camera_watch import VisionRunner  # noqa: E402
from assistant.core.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis vision worker")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    setup_logging("jarvis-vision")
    log = logging.getLogger(__name__)

    cfg = load_config(args.config)
    db.init_db(cfg.db_path)
    vision.init_vision_db(cfg.db_path)
    business_db.init_business_db(cfg.db_path)   # review_items lives here

    for u in cfg.users:
        db.upsert_user(cfg.db_path, u.telegram_chat_id, u.display_name, u.role)
    owner_cfg = next((u for u in cfg.users if u.role == "owner"), None)
    if owner_cfg is None:
        log.error("no owner configured -- nothing to watch on behalf of")
        return 1
    owner = db.get_user_by_chat_id(cfg.db_path, owner_cfg.telegram_chat_id)

    for cam in cfg.cameras or []:
        vision.add_camera(
            cfg.db_path, cam["key"], cam["name"], cam["url"],
            kind=cam.get("kind", "mjpeg"), location=cam.get("location", ""),
            motion_threshold=cam.get("motion_threshold", 0.012),
            recordable=cam.get("recordable", True),
        )
    if not cfg.cameras:
        log.warning("no cameras configured (config.json has no 'cameras' list) — nothing to watch")

    runner = VisionRunner(
        cfg.db_path, owner["id"], poll_seconds=cfg.camera_poll_seconds,
        device=cfg.vision_device, media_dir=cfg.generated_media_path,
        match_threshold=cfg.face_match_threshold,
        enroll_after_sightings=cfg.enroll_after_sightings,
    )
    log.info("Jarvis vision worker starting")
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
