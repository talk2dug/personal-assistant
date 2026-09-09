"""Ties the pieces together into the actual house-watching loop: motion gate -> YOLO ->
face recognition -> the presence/identity schema in vision.py, plus turning a
persistently-unrecognised face into a question on the Review page.

Deliberately the one place that imports detector.py and face_recognition.py alongside
vision.py and business_db.py -- see scripts/vision_worker.py, the only process that
imports this module, and requirements-vision.txt for why. main.py and web_main.py must
never import this module: doing so would pull torch/ultralytics/insightface into
jarvis-core.service and jarvis-web.service, which run on hosts that may not even have a
GPU (see detector.py's docstring).
"""
import logging
import time

from . import business_db
from . import detector as detector_mod
from . import face_recognition, vision

log = logging.getLogger(__name__)

# The fields whose change means "this is a different moment worth a row", per vision.py's
# own "one row per meaningful change, not per frame" rule.
_STATE_KEYS = ("has_person", "has_pet", "pets")


class CameraWatcher:
    """One camera's state machine: motion -> detect -> (for people) recognise -> record.

    Keeps its own last-seen summary and last-seen identity sets so record_event only
    fires on a real transition (empty -> occupied, stranger -> named, person leaves) --
    not on every tick a person happens to still be standing there.
    """

    def __init__(self, camera: dict, db_path: str, owner_user_id: int,
                 detector: "detector_mod.Detector", recognizer: "face_recognition.FaceRecognizer | None",
                 media_dir: str = "generated", match_threshold: float = 0.42,
                 enroll_after_sightings: int = vision.ENROLL_AFTER_SIGHTINGS):
        self.camera = camera
        self.key = camera["key"]
        self.db_path = db_path
        self.owner_user_id = owner_user_id
        self.detector = detector
        self.recognizer = recognizer
        self.media_dir = media_dir
        self.match_threshold = match_threshold
        self.enroll_after_sightings = enroll_after_sightings
        self.source = vision.FrameSource(camera)
        self.motion = vision.MotionGate(threshold=camera.get("motion_threshold", 0.012))
        self._last_summary: dict | None = None
        self._last_people: set[str] = set()
        self._last_unknown_ids: set[int] = set()

    def tick(self) -> None:
        frame = self.source.snapshot()
        if frame is None:
            return
        moved, _score = self.motion.update(frame)
        if not moved:
            if self._last_summary and (self._last_summary["has_person"] or self._last_summary["has_pet"]):
                # The motion gate has settled with nobody left in frame -- the room going
                # quiet is itself the meaningful event, distinct from never having had
                # anyone in it at all.
                vision.record_event(self.db_path, self.key, "cleared")
                self._last_summary = None
                self._last_people = set()
                self._last_unknown_ids = set()
            return

        detections = self.detector.detect(frame)
        summary = self.detector.summarise(detections)

        people_now: set[str] = set()
        unknown_now: set[int] = set()
        for det in (d for d in detections if d.kind == "person"):
            person_key, unknown_id = self._identify(frame, det)
            if person_key:
                people_now.add(person_key)
            if unknown_id:
                unknown_now.add(unknown_id)

        transitioned = self._last_summary is None or any(
            summary[k] != self._last_summary.get(k) for k in _STATE_KEYS
        )
        if transitioned:
            if summary["has_person"] and not people_now and not unknown_now:
                # Someone is in frame but no face was readable (facing away, too far,
                # too dark) -- still worth a generic presence row.
                vision.record_event(self.db_path, self.key, "person",
                                    confidence=summary["top_person_confidence"])
            if summary["has_pet"]:
                vision.record_event(self.db_path, self.key, "pet",
                                    label=",".join(summary["pets"]))

        self._last_summary = summary
        self._last_people = people_now
        self._last_unknown_ids = unknown_now

    def _identify(self, frame, det: "detector_mod.Detection") -> tuple[str | None, int | None]:
        """Runs face recognition on one person detection, records the outcome, and
        returns (matched person_key, unknown_face id) -- exactly one of which is set,
        or both None when no usable face was found at all."""
        if self.recognizer is None:
            return None, None
        crop = det.crop(frame)
        face = self.recognizer.best_face(crop)
        if face is None:
            return None, None

        match = vision.find_known_match(self.db_path, face.embedding, threshold=self.match_threshold)
        if match:
            if match["key"] not in self._last_people:
                vision.record_event(
                    self.db_path, self.key, "identified", label=match["name"],
                    confidence=match["score"], person_key=match["key"],
                )
            return match["key"], None

        thumb = self._save_thumbnail(crop)
        row = vision.upsert_unknown_face(self.db_path, self.key, face.embedding, thumb)
        if row["id"] not in self._last_unknown_ids:
            vision.record_event(self.db_path, self.key, "unknown_person",
                                confidence=face.det_score, detail=f"unknown_face:{row['id']}")
        self._maybe_ask_to_enroll(row)
        return None, row["id"]

    def _save_thumbnail(self, crop) -> str:
        """Best-effort; a failed save must not stop the identification pipeline -- the
        review item still works without a picture, it's just a plain approve/reject."""
        try:
            import pathlib
            import uuid

            import cv2

            out_dir = pathlib.Path(self.media_dir) / "faces"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{uuid.uuid4().hex}.jpg"
            cv2.imwrite(str(path), crop[:, :, ::-1])   # RGB -> BGR for cv2
            return str(path)
        except Exception as e:
            log.debug("thumbnail save failed: %s", e)
            return ""

    def _maybe_ask_to_enroll(self, unknown_face: dict) -> None:
        if unknown_face["asked"] or unknown_face["seen_count"] < self.enroll_after_sightings:
            return
        business_db.create_review_item(
            self.db_path, self.owner_user_id,
            title=f"Unrecognised face at {self.camera.get('name') or self.key}",
            kind="other",
            summary=(f"Seen {unknown_face['seen_count']} times, most recently just now. "
                     f"Type their name in the note and approve to add them as a known "
                     f"person, or reject to leave them unidentified."),
            source_agent="camera_watch",
            ref_table="unknown_faces", ref_id=unknown_face["id"],
            options=([{"label": "Unknown person", "media_path": unknown_face["thumbnail_path"]}]
                    if unknown_face["thumbnail_path"] else None),
        )
        vision.mark_unknown_face_asked(self.db_path, unknown_face["id"])
        log.info("asked the owner to identify unknown face %s (seen %dx at %s)",
                 unknown_face["id"], unknown_face["seen_count"], self.key)


class VisionRunner:
    """Polls every enabled camera on an interval. One Detector and one FaceRecognizer are
    shared across all cameras -- both hold CUDA context, not just weights, so several
    cameras on one GPU should queue through a shared lock rather than each grab their own
    (same reasoning as detector.Detector's own docstring).
    """

    def __init__(self, db_path: str, owner_user_id: int, poll_seconds: float = 1.5,
                 device: str | None = None, media_dir: str = "generated",
                 match_threshold: float = 0.42,
                 enroll_after_sightings: int = vision.ENROLL_AFTER_SIGHTINGS):
        self.db_path = db_path
        self.owner_user_id = owner_user_id
        self.poll_seconds = poll_seconds
        self.media_dir = media_dir
        self.match_threshold = match_threshold
        self.enroll_after_sightings = enroll_after_sightings
        self.detector = detector_mod.Detector(device=device)
        self.recognizer = face_recognition.FaceRecognizer(device=device)
        self._watchers: dict[str, CameraWatcher] = {}
        self._stop = False

    def _sync_cameras(self) -> None:
        """Picks up cameras added/removed/disabled since the last pass, without needing a
        restart -- the owner adding a camera through the web UI should not require
        bouncing this process."""
        cameras = vision.list_cameras(self.db_path, enabled_only=True)
        seen = set()
        for cam in cameras:
            seen.add(cam["key"])
            if cam["key"] not in self._watchers:
                self._watchers[cam["key"]] = CameraWatcher(
                    cam, self.db_path, self.owner_user_id, self.detector, self.recognizer,
                    media_dir=self.media_dir, match_threshold=self.match_threshold,
                    enroll_after_sightings=self.enroll_after_sightings,
                )
        for stale in set(self._watchers) - seen:
            del self._watchers[stale]

    def run_forever(self) -> None:
        log.info("vision runner starting (device=%s, poll=%.1fs)",
                 self.detector.device, self.poll_seconds)
        while not self._stop:
            self._sync_cameras()
            if not self._watchers:
                log.debug("no enabled cameras yet")
            for watcher in list(self._watchers.values()):
                try:
                    watcher.tick()
                except Exception:
                    log.exception("camera %s tick failed", watcher.key)
            time.sleep(self.poll_seconds)

    def stop(self) -> None:
        self._stop = True
