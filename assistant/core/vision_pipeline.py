"""The camera loop: motion -> detection -> identity, tied to one camera.

Deliberately a plain function rather than a thread-per-camera class: whatever schedules
this (a background poller) already owns its own timing, and gains nothing from a second
thread per camera. Each tick does at most one detector pass and, only when a person was
actually seen, one embedder pass -- gated by the same motion check vision.py already
uses, so an idle house costs one frame fetch and one frame-diff per camera per tick and
nothing else.

This module is intentionally NOT wired into scheduler.py yet. Doing that safely means
reading the real call site that constructs the scheduler (main.py / web_main.py) so the
Detector and FaceEmbedder get built once at startup rather than per tick, and that pass
wants its own review rather than a guess bundled into this one.
"""
import logging

from . import vision

log = logging.getLogger(__name__)

# An unknown face has to be seen this many times before it's worth interrupting the
# owner with "who is this?" -- a single frame at a bad angle produces an embedding that
# won't even match the same person five minutes later, and asking about every one of
# those would make the review queue useless within a day.
ASK_AFTER_SIGHTINGS = 3


class CameraRuntime:
    """Per-camera state a tick needs across calls: the frame source and its own motion
    gate. Kept as plain process memory, not written to the DB -- this is runtime cache,
    not a fact about the house, and doesn't need to survive a restart."""

    def __init__(self, camera: dict):
        self.camera = camera
        self.source = vision.FrameSource(camera)
        self.gate = vision.MotionGate(threshold=camera.get("motion_threshold", 0.012))


def tick(db_path: str, runtime: CameraRuntime, detector, embedder,
         owner_user_id: int, create_review_item) -> dict:
    """One pass for one camera.

    create_review_item is business_db.create_review_item (or a stand-in in tests) --
    passed in rather than imported, so this module doesn't force every caller (including
    ones that only care about motion/pet events) to pull in the business schema.

    Returns a small dict describing what happened, mainly so a scheduler job can log
    something more useful than "ran".
    """
    camera_key = runtime.camera["key"]
    frame = runtime.source.snapshot()
    if frame is None:
        return {"ok": False, "camera": camera_key, "reason": "no frame"}

    moving, score = runtime.gate.update(frame)
    if not moving:
        return {"ok": True, "camera": camera_key, "motion": False}

    detections = detector.detect(frame)
    summary = detector.summarise(detections)

    if not summary["has_person"] and not summary["has_pet"]:
        # Motion with nothing the detector cares about -- a light changing, a curtain --
        # is deliberately not logged; vision_events would otherwise fill with noise
        # indistinguishable from "something happened".
        return {"ok": True, "camera": camera_key, "motion": True, "person": False}

    for pet_label in summary["pets"]:
        vision.record_event(db_path, camera_key, "pet", label=pet_label)

    identified, unknown = 0, 0
    if summary["has_person"]:
        known_people = vision.list_known_people(db_path)
        for d in [d for d in detections if d.kind == "person"]:
            crop = d.crop(frame)
            embedding = embedder.embed(crop) if embedder is not None else None
            if embedding is None:
                # Detected a person but couldn't get a face out of the crop (back turned,
                # too far, bad angle) -- log the presence honestly rather than guessing.
                vision.record_event(db_path, camera_key, "person", confidence=d.confidence)
                continue

            person, match_score = vision.identify_embedding(embedding, known_people)
            if person is not None:
                identified += 1
                vision.record_event(
                    db_path, camera_key, "identified", label=person["name"],
                    confidence=match_score, person_key=person["key"],
                )
                continue

            unknown += 1
            face = vision.find_or_create_unknown_face(db_path, camera_key, embedding)
            vision.record_event(
                db_path, camera_key, "unknown_person", confidence=match_score,
                detail=f"unknown_face#{face['id']}",
            )

            if not face["asked"] and face["seen_count"] >= ASK_AFTER_SIGHTINGS:
                where = runtime.camera.get("location") or camera_key
                create_review_item(
                    db_path, owner_user_id,
                    title=f"Who's this, seen on the {where} camera?",
                    kind="other", source_agent="vision",
                    summary=f"Seen {face['seen_count']} times, not yet identified.",
                    detail=(
                        "Reply with a name (and relationship -- household, family, "
                        "friend, service, or other) to enroll this person, or say to "
                        "dismiss it if this is a one-off visitor not worth remembering."
                    ),
                    priority="low",
                    options=[{"label": "Enroll", "media_path": face.get("thumbnail_path"),
                              "description": f"unknown_face#{face['id']}"}],
                )
                vision.mark_unknown_face_asked(db_path, face["id"])

    return {
        "ok": True, "camera": camera_key, "motion": True, "person": summary["has_person"],
        "identified": identified, "unknown": unknown,
    }
