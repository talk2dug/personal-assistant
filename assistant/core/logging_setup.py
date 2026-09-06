"""File logging for the two long-running services.

Both entrypoints run as Windows scheduled tasks, which discard stdout. Everything the
code carefully logged -- including the `_guarded` wrappers in scheduler.py, whose whole
purpose is to record why a background job failed instead of taking the process down --
went nowhere, so a job that silently stopped running left no evidence at all.

Rotating rather than a single file: these run for weeks and a scheduler logging every
tick will otherwise fill a disk quietly, which is the same class of problem.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(service: str, level: int = logging.INFO) -> str:
    """Log to logs/<service>.log as well as stdout. Returns the path."""
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"{service}.log")

    root = logging.getLogger()
    root.setLevel(level)
    # Idempotent: a second call (or a reload) must not double every line.
    if not any(getattr(h, "_jarvis_service", None) == service for h in root.handlers):
        handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(FORMAT))
        handler._jarvis_service = service
        root.addHandler(handler)
    if not any(isinstance(h, logging.StreamHandler) and
               not isinstance(h, RotatingFileHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(stream)

    # APScheduler logs skipped runs at WARNING ("maximum number of running instances
    # reached") -- the one message that explains a job that stops firing without error.
    logging.getLogger("apscheduler").setLevel(logging.INFO)
    return path
