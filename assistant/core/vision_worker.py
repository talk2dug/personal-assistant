"""The camera watch loop: pulls frames, gates on motion, detects people/pets, and
resolves identity for anyone detected -- the thing that actually populates
vision_events, which presence.py then reads to decide who is allowed to hear what.

One thread per enabled camera, all sharing one Detector and one FaceEmbedder (both hold
a GPU context and their own lock) -- the same "share the model, queue the work" reasoning
as gpu_bridge, just synchronous and local rather than a polled job queue, because a face
at the door needs an answer in tens of milliseconds, not whenever a shared queue gets to
it.
"""
import logging
import threading
import time

from . import identity as identity_mod
from . import vision
from .detector import Detector

log = logging.getLogger(__name__)

# Cameras poll for a new frame at this cadence when nothing is moving. Cheap (frame
# differencing on a downscaled image), so this is about being a polite neighbour on an
# MJPEG endpoint / not spinning a CPU core, not about detector cost.
IDLE_POLL_SECONDS = 1.0
# Once the motion gate is open, sample faster so identity resolves quickly after someone
# walks in, rather than a second or more after they've already started speaking.
ACTIVE_POLL_SECONDS = 0.3
# How long with zero people detected before a 'cleared' event fires. Not instant on the
# first motion-free frame: someone standing still for a couple of tenths of a second
# (reading a screen, mid-sentence) would otherwise flap identified -> cleared -> identified
# on every idle frame, which is exactly the flapping vision.MotionGate's own cooldown
# exists to prevent one layer down.
CLEAR_AFTER_SECONDS = 8.0


class CameraWatcher:
    def __init__(self, db_path: str, camera: dict, detector: Detector,
                embedder: identity_mod.FaceEmbedder):
        self.db_path = db_path
        self.camera = camera
        self.detector = detector
        self.embedder = embedder
        self.source = vision.FrameSource(camera)
        self.gate = vision.MotionGate(threshold=camera.get("motion_threshold", 0.012))
        self._had_person = False
        self._last_person_at = 0.0
        self._stop = threading.Event()

    def _resolve_identity(self, frame, det) -> None:
        """For one person detection: crop, embed, match against known_people, and log
        the result. Silently does nothing if no face clears FaceEmbedder's own confidence
        floor (someone facing away, motion blur) -- the 'person' event already recorded
        for this frame is enough for presence to know *someone* is there; it's only
        identity that's unavailable this frame, and there will be another one shortly.
        """
        crop = det.crop(frame)
        result = self.embedder.embed(crop)
        if result is None:
            return
        embedding, _bbox, det_score = result

        known = vision.list_known_people(self.db_path)
        match = identity_mod.match_known_person(embedding, known)
        if match is not None:
            key, score = match
            person = vision.get_known_person(self.db_path, key)
            vision.record_event(
                self.db_path, self.camera["key"], "identified",
                label=(person["name"] if person else key), confidence=score, person_key=key,
            )
        else:
            vision.record_event(
                self.db_path, self.camera["key"], "unknown_person", confidence=det_score,
            )
            vision.record_unknown_face(self.db_path, self.camera["key"], embedding)

    def _maybe_clear(self) -> None:
        if self._had_person and time.time() - self._last_person_at >= CLEAR_AFTER_SECONDS:
            vision.record_event(self.db_path, self.camera["key"], "cleared")
            self._had_person = False

    def tick(self) -> None:
        frame = self.source.snapshot()
        if frame is None:
            return
        moved, _score = self.gate.update(frame)
        if not moved:
            self._maybe_clear()
            return

        detections = self.detector.detect(frame)
        summary = self.detector.summarise(detections)

        if summary["has_pet"] and not summary["has_person"]:
            vision.record_event(self.db_path, self.camera["key"], "pet",
                                label=",".join(summary["pets"]))

        if summary["has_person"]:
            self._had_person = True
            self._last_person_at = time.time()
            vision.record_event(self.db_path, self.camera["key"], "person",
                                confidence=summary["top_person_confidence"])
            for det in detections:
                if det.kind == "person":
                    self._resolve_identity(frame, det)
        else:
            self._maybe_clear()

    def run(self) -> None:
        log.info("watching camera %s (%s)", self.camera["key"], self.camera["url"])
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("camera %s: tick failed", self.camera["key"])
            time.sleep(ACTIVE_POLL_SECONDS if self._had_person else IDLE_POLL_SECONDS)

    def stop(self) -> None:
        self._stop.set()


def run_forever(db_path: str, model_name: str = "yolo11n.pt", device: str | None = None) -> None:
    """Entry point for the vision worker process (assistant/vision_main.py).

    One Detector and one FaceEmbedder for every camera, each internally locked -- two
    cameras firing at once queue behind the same GPU context rather than each loading
    their own copy of the models, which is what keeps this affordable on a single RTX
    3060 the owner also uses for other things.
    """
    vision.init_vision_db(db_path)
    shared_detector = Detector(model_name=model_name, device=device)
    shared_embedder = identity_mod.FaceEmbedder(device=device)

    cameras = vision.list_cameras(db_path, enabled_only=True)
    if not cameras:
        log.warning("no enabled cameras configured -- nothing to watch")
        return

    threads = []
    for camera in cameras:
        watcher = CameraWatcher(db_path, camera, shared_detector, shared_embedder)
        t = threading.Thread(target=watcher.run, name=f"camera-{camera['key']}", daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()
