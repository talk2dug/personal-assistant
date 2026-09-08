"""Camera-watch loop: pulls frames from each configured camera, runs them through the
YOLO detector for motion/person/pet detection, and -- for cameras whose `role` is
'kiosk' -- through the face recognizer, writing everything to vision.py's tables as
events.

One thread per camera. Cameras are I/O-bound snapshot fetches punctuated by cheap
motion diffing; a detector/recognizer call is tens of milliseconds and each already
holds its own internal lock (see detector.Detector, face_recognizer.FaceRecognizer), so
several camera threads sharing one Detector/FaceRecognizer instance queue safely rather
than contend or double-load the models.

Kiosk-only face recognition (Phase 1 decision #1, core/identity.py): a camera whose
role isn't 'kiosk' never has a frame handed to the recognizer at all, regardless of
what's in view -- not filtered after the fact, not run and discarded. The house/general
cameras that existed before this module (pets, motion, an unnamed person walking
through frame) are completely untouched by anything face-related.
"""
import logging
import threading
import time

from . import vision

log = logging.getLogger(__name__)

# How often a camera is re-checked for motion. Cheap (a downscaled frame diff), so this
# can run faster than the detector/recognizer ever need to.
POLL_SEC = 0.5

# A person box must be at least this fraction of the frame's shorter side before a face
# is worth looking for in it -- a person crossing the far end of a hallway produces a
# crop too small for ArcFace to do anything useful with, and trying anyway just spends
# CPU manufacturing a confident-looking non-match.
MIN_PERSON_BOX_FRACTION = 0.12

# Identity events are only worth writing when something changed -- same reasoning as
# vision.py's own docstring ("a camera watching an empty room all night should produce
# nothing"). This is the per-camera debounce against repeating the same identification
# every tick while someone just stands there talking to the kiosk.
REIDENTIFY_EVERY_SEC = 20.0


def _box_fraction(frame, box) -> float:
    h, w = frame.shape[:2]
    bw, bh = box[2] - box[0], box[3] - box[1]
    return min(bw, bh) / max(1, min(h, w))


class CameraWatcher:
    def __init__(self, db_path: str, camera: dict, detector, recognizer=None):
        self.db_path = db_path
        self.camera = camera
        self.detector = detector
        self.recognizer = recognizer      # None means: detect people/pets, never faces
        self._motion = vision.MotionGate(threshold=camera.get("motion_threshold", 0.012))
        self._last_identity_write = 0.0
        self._stop = threading.Event()

    def _is_kiosk(self) -> bool:
        return self.camera.get("role") == "kiosk" and self.recognizer is not None

    def _handle_enrollment(self, frame, people) -> bool:
        """If the owner asked to enroll someone at this camera, try to collect one
        sample from this frame. Returns True if this frame was consumed by enrollment
        (so it isn't also run through identification in the same pass)."""
        request = vision.pending_enrollment_request(self.db_path, self.camera["key"])
        if request is None:
            return False
        if len(people) != 1:
            # Ambiguous -- nobody there, or more than one person in frame. Enrollment
            # only ever consumes a sample when there's exactly one candidate, so a
            # housemate walking through mid-enrollment can't get attributed to it.
            return True
        face = self.recognizer.best_face(people[0].crop(frame))
        if face is None:
            return True
        vision.record_enrollment_sample(self.db_path, request["id"], face.embedding)
        return True

    def _identify(self, frame, people) -> None:
        if not people:
            return
        # Largest box = closest/most centred person -- the one actually addressing the
        # kiosk, not someone passing behind them.
        person = max(people, key=lambda d: (d.box[2] - d.box[0]) * (d.box[3] - d.box[1]))
        if _box_fraction(frame, person.box) < MIN_PERSON_BOX_FRACTION:
            return
        face = self.recognizer.best_face(person.crop(frame))
        if face is None:
            return
        now = time.time()
        if now - self._last_identity_write < REIDENTIFY_EVERY_SEC:
            return
        self._last_identity_write = now
        match = vision.match_face(self.db_path, face.embedding)
        if match:
            vision.record_event(
                self.db_path, self.camera["key"], "identified", label=match["name"],
                confidence=match["match_score"], person_key=match["key"],
            )
        else:
            unknown = vision.record_unknown_face(self.db_path, self.camera["key"], face.embedding)
            vision.record_event(
                self.db_path, self.camera["key"], "unknown_person",
                confidence=face.det_score, detail=f"unknown_face_id={unknown['id']}",
            )

    def tick(self) -> None:
        frame = vision.FrameSource(self.camera).snapshot()
        if frame is None:
            return
        moved, _score = self._motion.update(frame)
        if not moved:
            return

        detections = self.detector.detect(frame)
        summary = self.detector.summarise(detections)
        if summary["has_person"]:
            vision.record_event(self.db_path, self.camera["key"], "person",
                                confidence=summary["top_person_confidence"])
        for pet_label in summary["pets"]:
            vision.record_event(self.db_path, self.camera["key"], "pet", label=pet_label)

        if not self._is_kiosk():
            return
        people = [d for d in detections if d.kind == "person"]
        if self._handle_enrollment(frame, people):
            return
        self._identify(frame, people)

    def run(self) -> None:
        log.info("watching camera %s (role=%s)", self.camera["key"], self.camera.get("role"))
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("camera %s: tick failed", self.camera["key"])
            time.sleep(POLL_SEC)

    def stop(self) -> None:
        self._stop.set()


def start_watchers(db_path: str, detector, recognizer=None) -> list[CameraWatcher]:
    """One thread per enabled camera. recognizer=None runs every camera in
    detection-only mode (no face recognition at all, identity.py fails closed
    everywhere) -- the safe state if InsightFace couldn't be loaded."""
    watchers = []
    for camera in vision.list_cameras(db_path, enabled_only=True):
        watcher = CameraWatcher(db_path, camera, detector, recognizer)
        threading.Thread(target=watcher.run, name=f"camera-{camera['key']}", daemon=True).start()
        watchers.append(watcher)
    return watchers
