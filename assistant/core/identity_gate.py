"""Fail-closed identity gating for the voice terminals.

Locked decisions this module exists to enforce:

- kiosk-only cameras. Identity is only ever attempted through a camera explicitly
  linked to the terminal that's asking (vision.camera_for_device) -- never a general
  house camera, and never whichever camera is nearest.
- fail-closed to shared data. Anything short of a confident, positive match against
  the owner's own enrolled face -- no camera, no face, low confidence, an unrecognized
  face, a match against someone else, a disabled/unreachable kiosk camera, any exception
  at all -- resolves to NOT the owner. There is no ambiguous middle state that defaults
  to full access; resolve() has exactly one path that returns confirmed_owner=True.
- single-user enrollment. The only identity this module is able to confirm right now
  is the owner (OWNER_PERSON_KEY). known_people's schema supports other people (see
  vision.py) and FaceRecognizer.match() already returns whoever the best match actually
  is, but resolve() only ever treats a match against OWNER_PERSON_KEY as confirmed --
  a match against some other, future-enrolled person is deliberately still not-owner
  until a later chunk decides what a confirmed non-owner identity should be allowed to
  do (the enroll-on-recognition-failure feature the schema is left open for).

What "shared" means in practice is decided here too: SHARED_CONTEXT_KEYS is the subset
of the integration kwargs devices.py normally passes to engine.handle_message that stay
available when the speaker isn't confirmed as the owner. Everything else -- finance,
mail, the owner's own phone, Obsidian notes, the business side, grocery ordering,
real-money trading, physical mail, and (for now) the calendar -- is cut rather than
guessed at. Cutting the calendar wholesale, instead of keeping just its shared half, is
a known simplification: that split lives inside engine.CalendarContext's own dispatch,
which this module (and this review session -- engine.py is far too large to have been
safely read and verified end to end here) has no verified visibility into. See
docs/identity-gating.md for the follow-up this leaves.
"""
import logging
from dataclasses import dataclass

from . import vision

log = logging.getLogger(__name__)

OWNER_PERSON_KEY = "owner"

# Kept when identity isn't confirmed. home_assistant is judged "shared house" rather
# than "the owner's own", and its own sensitive_tools gate (locks/covers/alarm) already
# requires a separate confirmation step independent of this one. Everything else in the
# normal turn() kwargs (era, phone, mail, obsidian, business, airbnb, ticketmaster,
# kroger, ccxt, letterstream, calendar) is owner-private or a real-world/financial action
# and is cut -- see the module docstring for why calendar in particular is cut wholesale
# rather than split.
SHARED_CONTEXT_KEYS = frozenset({"home_assistant"})


@dataclass
class IdentityResult:
    confirmed_owner: bool
    reason: str
    person_key: str | None = None
    name: str | None = None
    score: float | None = None


def _fail(reason: str) -> IdentityResult:
    log.info("identity gate: not confirmed (%s)", reason)
    return IdentityResult(confirmed_owner=False, reason=reason)


def resolve(db_path: str, device_id: str, detector, recognizer) -> IdentityResult:
    """Best-effort attempt to confirm the owner is the one speaking to `device_id`.

    Every branch that isn't an unambiguous, positive owner match returns via _fail() --
    that's the fail-closed contract, and it's deliberately impossible to fall out of
    this function with confirmed_owner=True except through the one explicit path at the
    bottom. `detector` and `recognizer` are passed in (rather than imported/constructed
    here) so callers -- and tests -- control exactly what runs; None for either fails
    closed immediately rather than raising.
    """
    if detector is None or recognizer is None:
        return _fail("recognition_not_configured")

    try:
        camera = vision.camera_for_device(db_path, device_id)
    except Exception:
        log.exception("identity gate: camera lookup failed for device %s", device_id)
        return _fail("lookup_error")
    if camera is None:
        return _fail("no_kiosk_camera")
    if not camera.get("enabled", 1):
        return _fail("camera_disabled")

    try:
        frame = vision.FrameSource(camera).snapshot()
    except Exception:
        log.exception("identity gate: snapshot failed for camera %s", camera["key"])
        return _fail("camera_error")
    if frame is None:
        return _fail("camera_unreachable")

    try:
        detections = detector.detect(frame)
    except Exception:
        log.exception("identity gate: detection failed for camera %s", camera["key"])
        return _fail("detection_error")
    people = [d for d in detections if d.kind == "person"]
    if not people:
        return _fail("no_person_detected")
    best = max(people, key=lambda d: d.confidence)

    try:
        face = recognizer.best_face(best.crop(frame))
    except Exception:
        log.exception("identity gate: face recognition failed for camera %s", camera["key"])
        return _fail("recognition_error")
    if face is None:
        return _fail("no_face")

    try:
        known = vision.list_known_people(db_path)
        match = recognizer.match(face.embedding, known)
    except Exception:
        log.exception("identity gate: matching failed for camera %s", camera["key"])
        return _fail("match_error")
    if match is None:
        return _fail("unrecognized")
    if match.person_key != OWNER_PERSON_KEY:
        # Correctly identified as *someone*, just not the owner. Still fail-closed --
        # single-user enrollment means only the owner's presence unlocks private data.
        return _fail(f"recognized_non_owner:{match.person_key}")

    log.info("identity gate: confirmed owner at %s (score %.3f)", device_id, match.score)
    return IdentityResult(confirmed_owner=True, reason="owner_confirmed",
                          person_key=match.person_key, name=match.name, score=match.score)


def restricted_kwargs(full_kwargs: dict) -> dict:
    """Given the full set of integration kwargs devices.py would normally pass to
    engine.handle_message, returns the fail-closed subset for an unconfirmed speaker.
    Anything not in SHARED_CONTEXT_KEYS is set to None rather than omitted, so callers
    can always pass every key through and let this function decide which survive."""
    return {k: (v if k in SHARED_CONTEXT_KEYS else None) for k, v in full_kwargs.items()}
