"""The per-camera detection/identity pipeline: motion gates YOLO person/pet detection,
which gates InsightFace face matching, which gates whether anything needs writing to
vision_events or asking the owner about on the Review page.

This is the piece that actually turns vision.py's dormant schema on. It deliberately
owns no database writes of its own beyond what vision.py and business_db.py already
expose -- every fact this pipeline learns goes through record_event/upsert_unknown_face/
create_review_item, the same functions any other caller would use, so there is exactly
one way an event or a review item gets created and it's easy to audit.

Runs in the core process only (see assistant/core/setup.py's build_vision_context and
assistant/main.py) -- it needs local CUDA for the RTX 3060 both detector.py and
face_id.py target, and the web process may be different, non-GPU hardware entirely.
"""
import json
import logging
import pathlib
import threading
import time
import uuid

from . import business_db, vision

logger = logging.getLogger(__name__)


class VisionRuntime:
    def __init__(
        self, db_path: str, owner_user_id: int, detector, face_identifier,
        generated_media_path: str, face_match_threshold: float = vision.FACE_MATCH_THRESHOLD,
        unknown_face_ask_after: int = vision.UNKNOWN_FACE_ASK_AFTER,
        identified_event_cooldown_seconds: float = 30.0,
    ):
        self.db_path = db_path
        self.owner_user_id = owner_user_id
        self.detector = detector
        self.face_identifier = face_identifier
        self.media_root = pathlib.Path(generated_media_path) / "vision_faces"
        self.face_match_threshold = face_match_threshold
        self.unknown_face_ask_after = unknown_face_ask_after
        self.identified_event_cooldown = identified_event_cooldown_seconds
        self._gates: dict[str, vision.MotionGate] = {}
        self._last_identified_event: dict[tuple, float] = {}
        self._face_id_unavailable = False
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    def _gate_for(self, camera: dict) -> vision.MotionGate:
        gate = self._gates.get(camera["key"])
        if gate is None:
            gate = vision.MotionGate(threshold=camera["motion_threshold"])
            self._gates[camera["key"]] = gate
        return gate

    def _save_thumbnail(self, crop) -> str:
        """Best-effort JPEG under generated_media_path/vision_faces, so the Review page's
        existing /api/review/media/{option_id} (which only ever serves files inside
        generated_media_path) can show it without any change to that endpoint."""
        self.media_root.mkdir(parents=True, exist_ok=True)
        path = self.media_root / f"{uuid.uuid4().hex}.jpg"
        try:
            import cv2
            cv2.imwrite(str(path), crop[:, :, ::-1])  # RGB -> BGR for cv2
            return str(path)
        except Exception:
            pass
        try:
            from PIL import Image
            Image.fromarray(crop).save(path, format="JPEG")
            return str(path)
        except Exception:
            logger.warning("could not save a face thumbnail (no cv2 or PIL available)")
            return ""

    @staticmethod
    def _crop(frame, box: tuple, pad: float = 0.25):
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = box
        px, py = (x2 - x1) * pad, (y2 - y1) * pad
        x1 = max(0, int(x1 - px)); y1 = max(0, int(y1 - py))
        x2 = min(w, int(x2 + px)); y2 = min(h, int(y2 + py))
        if x2 <= x1 or y2 <= y1:
            return None
        return frame[y1:y2, x1:x2]

    def process_camera(self, camera: dict) -> None:
        source = vision.FrameSource(camera)
        frame = source.snapshot()
        if frame is None:
            return
        moved, _score = self._gate_for(camera).update(frame)
        if not moved:
            return

        detections = self.detector.detect(frame)
        summary = self.detector.summarise(detections)
        camera_key = camera["key"]

        if summary["has_pet"]:
            vision.record_event(self.db_path, camera_key, "pet", label=",".join(summary["pets"]))

        if not summary["has_person"]:
            return

        faces = []
        if self.face_identifier is not None and not self._face_id_unavailable:
            try:
                faces = self.face_identifier.embed_faces(frame)
            except ImportError as e:
                self._face_id_unavailable = True
                logger.error(
                    "InsightFace is not installed in this environment (%s) -- person/pet "
                    "detection will continue but nobody will be identified until it is "
                    "(pip install into .venv-vision; see requirements-vision.txt)", e)

        if not faces:
            # A person is there but no face is visible (back turned, too far away,
            # obscured) -- still a presence fact, just not an identity one.
            vision.record_event(self.db_path, camera_key, "person",
                                confidence=summary["top_person_confidence"])
            return

        for face in faces:
            match = vision.match_embedding(self.db_path, face.embedding, self.face_match_threshold)
            if match:
                person_key, name, score = match
                cooldown_key = (camera_key, person_key)
                last = self._last_identified_event.get(cooldown_key, 0.0)
                if time.time() - last >= self.identified_event_cooldown:
                    vision.record_event(self.db_path, camera_key, "identified", label=name,
                                        confidence=score, person_key=person_key)
                    self._last_identified_event[cooldown_key] = time.time()
                continue

            crop = self._crop(frame, face.box)
            thumb = self._save_thumbnail(crop) if crop is not None else ""
            unknown = vision.upsert_unknown_face(self.db_path, camera_key, face.embedding, thumb)
            vision.record_event(self.db_path, camera_key, "unknown_person",
                                confidence=face.det_score, thumbnail_path=thumb)
            if not unknown["asked"] and unknown["seen_count"] >= self.unknown_face_ask_after:
                self._create_face_review(unknown, camera)

    def _create_face_review(self, face: dict, camera: dict) -> None:
        """The enrollment flow, surfaced through the same Review page as everything else
        the team makes: a person/art brief/listing gets an owner decision before it
        moves forward, and so does a new identity. Nothing in this pipeline calls
        vision.enroll_person directly -- see apply_face_review_decision, which review.py
        calls once the owner actually decides."""
        known = vision.get_known_people(self.db_path)
        thumb = face.get("thumbnail_path") or None
        options = [
            {"label": f"This is {p['name']}", "description": p.get("relationship") or "",
             "media_path": thumb, "body": json.dumps({"person_key": p["key"]})}
            for p in known
        ] + [{
            "label": "New person",
            "description": "Type their name in the note field, then approve.",
            "media_path": thumb, "body": json.dumps({"new_person": True}),
        }]
        title = f"Unfamiliar face on {camera.get('name') or camera['key']}"
        summary = f"Seen {face['seen_count']} time(s) so far."
        detail = (
            "Approve an existing person to add this as another look of theirs, or "
            "approve 'New person' with their name typed in the note field to enrol "
            "them. Reject to dismiss -- you won't be asked about this exact face again."
        )
        business_db.create_review_item(
            self.db_path, self.owner_user_id, title, kind="other", summary=summary,
            detail=detail, source_agent="vision", ref_table="unknown_faces",
            ref_id=face["id"], options=options,
        )
        vision.mark_unknown_face_asked(self.db_path, face["id"])

    def run_once(self) -> int:
        """One pass over every enabled camera. Each camera's failure is caught and
        logged on its own -- one bad snapshot URL must not stop the rest of the house
        from being watched."""
        processed = 0
        for camera in vision.list_cameras(self.db_path, enabled_only=True):
            try:
                self.process_camera(camera)
                processed += 1
            except Exception:
                logger.exception("vision: camera %s failed this pass", camera["key"])
        return processed

    def start_worker(self, poll_seconds: float = 2.0) -> None:
        if self._worker and self._worker.is_alive():
            return

        def _loop():
            while not self._stop.wait(poll_seconds):
                try:
                    self.run_once()
                except Exception:
                    logger.exception("vision worker tick failed")

        self._stop.clear()
        self._worker = threading.Thread(target=_loop, daemon=True, name="vision-worker")
        self._worker.start()
        logger.info("Vision worker started (poll every %.1fs)", poll_seconds)

    def stop_worker(self) -> None:
        self._stop.set()
