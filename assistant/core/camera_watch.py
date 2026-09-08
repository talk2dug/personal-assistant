"""The camera-watch loop: turns configured cameras into vision_events, tying together
motion (vision.MotionGate), object detection (detector.Detector) and, on kiosk cameras
only, face identity (face_recognizer.FaceRecognizer).

One thread per enabled camera. Each tick is: snapshot -> motion gate -> (if motion)
detect -> (if a person, and this is a kiosk camera) recognize -> record events. A camera
with nothing happening in it costs one HTTP snapshot and one grayscale diff per poll --
the expensive stages only run when there's something to look at, same discipline
detector.py and vision.py already establish for the pipeline this builds on.

Locked decision: kiosk-only cameras. Face recognition is a strictly narrower thing than
person detection, and it only ever runs on a camera explicitly linked to a voice
terminal (vision.camera_for_device / the cameras.kiosk_device_id column). A hallway or
yard camera still reports "a person is there" for presence, but it never runs a face
through ArcFace and never writes an embedding anywhere -- the household's general
cameras are not a face-recognition system; only the kiosks people are already choosing
to talk to are.
"""
import logging
import threading
import time

from . import vision
from .detector import Detector
from .face_recognizer import FaceRecognizer, cosine_similarity, embed_to_json, embedding_from_json

log = logging.getLogger(__name__)

# How often to check for motion when nothing is happening.
POLL_SECONDS_IDLE = 2.0
# Once motion is confirmed, poll faster so identity/detection tracks someone actually
# standing at the kiosk mid-conversation rather than sampling once and going quiet.
POLL_SECONDS_ACTIVE = 0.5
# How long without a person before a 'cleared' event fires, closing out presence.
PRESENCE_CLEAR_SECONDS = 20
# How many recent unresolved unknown_faces rows on the same camera to check before
# logging a new one -- keeps one lingering stranger from generating a new row every tick.
UNKNOWN_FACE_DEDUPE_WINDOW = 25


class CameraLoop:
    """One camera's watch thread."""

    def __init__(self, db_path: str, camera: dict, detector: Detector,
                 recognizer: FaceRecognizer | None, is_kiosk: bool):
        self.db_path = db_path
        self.camera = camera
        self.detector = detector
        self.recognizer = recognizer
        self.is_kiosk = bool(is_kiosk and recognizer is not None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._had_person = False
        self._last_person_at = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"camera-{self.camera['key']}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        source = vision.FrameSource(self.camera)
        gate = vision.MotionGate(threshold=self.camera.get("motion_threshold") or 0.012)
        log.info("camera %s: watch loop started (kiosk=%s)", self.camera["key"], self.is_kiosk)
        while not self._stop.is_set():
            try:
                self._tick(source, gate)
            except Exception:
                log.exception("camera %s: tick failed", self.camera["key"])
            interval = POLL_SECONDS_ACTIVE if self._had_person else POLL_SECONDS_IDLE
            self._stop.wait(interval)

    def _tick(self, source, gate) -> None:
        frame = source.snapshot()
        if frame is None:
            return
        moved, _score = gate.update(frame)
        if not moved:
            if self._had_person and time.time() - self._last_person_at > PRESENCE_CLEAR_SECONDS:
                vision.record_event(self.db_path, self.camera["key"], "cleared")
                self._had_person = False
            return

        detections = self.detector.detect(frame)
        summary = self.detector.summarise(detections)

        if not summary["has_person"]:
            for d in detections:
                if d.kind == "pet":
                    vision.record_event(self.db_path, self.camera["key"], "pet",
                                        label=d.label, confidence=d.confidence)
            return

        self._had_person = True
        self._last_person_at = time.time()
        vision.record_event(self.db_path, self.camera["key"], "person",
                            confidence=summary["top_person_confidence"])

        if not self.is_kiosk:
            return  # identity only ever runs on a camera linked to a voice terminal

        people = [d for d in detections if d.kind == "person"]
        best = max(people, key=lambda d: d.confidence)
        face = self.recognizer.best_face(best.crop(frame))
        if face is None:
            return  # a person was detected but no face was resolvable -- not an event

        known = vision.list_known_people(self.db_path)
        match = self.recognizer.match(face.embedding, known)
        if match is not None:
            vision.record_event(self.db_path, self.camera["key"], "identified",
                                label=match.name, confidence=match.score,
                                person_key=match.person_key)
        else:
            self._record_unknown(face)

    def _record_unknown(self, face) -> None:
        """Logs an unrecognized face for the owner to review later, without guessing.

        Locked decision: the schema is left open for a later enroll-on-recognition-
        failure feature, but that feature isn't built yet -- this only ever logs. The
        Review page's unknown-faces list (assistant/web/routes/identity.py) is what a
        future chunk would turn into a one-tap "that's X"; today it's read-only.
        """
        recent = vision.list_unknown_faces(self.db_path, resolved=False,
                                           limit=UNKNOWN_FACE_DEDUPE_WINDOW)
        for row in recent:
            if row["camera_key"] != self.camera["key"]:
                continue
            try:
                existing = embedding_from_json(row["embedding"])
            except Exception:
                continue
            if cosine_similarity(face.embedding, existing) >= self.recognizer.match_threshold:
                vision.bump_unknown_face(self.db_path, row["id"])
                return
        unknown_id = vision.record_unknown_face(self.db_path, self.camera["key"],
                                                embed_to_json(face.embedding))
        vision.record_event(self.db_path, self.camera["key"], "unknown_person",
                            detail=f"unknown_face#{unknown_id}")


class CameraWatcher:
    """Owns one CameraLoop per enabled camera, built from the cameras table at startup.

    A single Detector and FaceRecognizer are shared across every loop -- both hold model
    weights and, for the recognizer, GPU/ONNXRuntime state, and several cameras each
    loading their own would be the same mistake detector.py's docstring already warns
    about for the detector alone.

    Deliberately built once at startup rather than hot-reloaded: linking a new camera to
    a kiosk from the Review page takes effect on the next restart of the web process, not
    live. That's a real limitation (see docs/identity-gating.md) rather than a silent one.
    """

    def __init__(self, db_path: str, detector: Detector, recognizer: FaceRecognizer | None):
        self.db_path = db_path
        self.detector = detector
        self.recognizer = recognizer
        self._loops: dict[str, CameraLoop] = {}

    def start(self) -> None:
        vision.init_vision_db(self.db_path)
        for camera in vision.list_cameras(self.db_path, enabled_only=True):
            is_kiosk = bool(camera.get("kiosk_device_id"))
            loop = CameraLoop(self.db_path, camera, self.detector, self.recognizer, is_kiosk)
            self._loops[camera["key"]] = loop
            loop.start()
        log.info("camera watcher: %d camera(s) running", len(self._loops))

    def stop(self) -> None:
        for loop in self._loops.values():
            loop.stop()
