"""Identity gating: turns 'is the owner in front of this kiosk right now' into a
decision about which tool contexts a voice turn is allowed to see.

The five locked Phase 1 decisions this module exists to enforce:

1. **Kiosk webcams only, for now.** Identity is only ever read from a camera whose
   `role` is 'kiosk' and whose `device_id` matches the terminal that's asking. A
   simrig or laptop camera, even if one exists in the cameras table, is never
   consulted here -- see face_recognizer.py's docstring for why that boundary isn't
   ready to be widened yet.
2. **Fail-closed.** No recent, confident recognition means "treat this as anyone",
   never "assume the owner". Every branch below that can't confirm identity returns
   `recognized=False`; there is no branch that defaults the other way.
3. **Shared-only data on failure.** gate_contexts() below withholds exactly the
   contexts that touch the owner's personal or financial life (see
   OWNER_ONLY_CONTEXTS) and leaves everything else — shared calendar, Home Assistant,
   weather, grocery, schedule — untouched. The household still gets to ask Jarvis to
   turn on the lights when the owner isn't the one standing there.
4. **Never default to the owner.** There is no "if uncertain, assume it's the owner"
   branch anywhere in this file.
5. **Single enrolled user today.** IdentityResult.is_owner is really just "is anyone
   confidently recognized", because there's only one person who *can* be, but it's
   written as a real identity comparison rather than a hardcoded "a face is present"
   check so a second enrolled person later (a partner, a housemate) is a data change
   to known_people, not a rewrite of this module.

Known gap, flagged rather than silently left: CalendarContext bundles the owner's
personal calendar and the shared household calendar behind one object, and neither
is in OWNER_ONLY_CONTEXTS below because gating it would currently mean withholding the
shared calendar too. Splitting that is an engine.py/CalendarContext change, not an
identity.py one -- tracked as follow-up work, not solved here.
"""
from dataclasses import dataclass

from . import vision

# Contexts that touch the owner's own money, messages, notes, or make real-world
# changes on his behalf. Withheld the moment identity can't be confirmed.
#
# 'business' is included even though a housemate asking about a craft-fair date isn't
# obviously sensitive, because business_expenses/business_inventory carry real dollar
# figures and the pipeline's approve/reject decisions are the owner's calls -- treated
# as financial data, same as era/ccxt, per the locked "no personal or financial tools"
# wording. 'home_assistant' is deliberately NOT here: physically-consequential domains
# (locks, alarm) already require confirmation regardless of who's asking (see
# ha_sensitive_domains), and turning on a light is a household action, not a personal one.
OWNER_ONLY_CONTEXTS = (
    "era", "phone", "mail", "obsidian", "personal", "business", "ccxt", "kroger", "letterstream",
)

DEFAULT_IDENTITY_WINDOW_SECONDS = 45


@dataclass
class IdentityResult:
    recognized: bool
    person_key: str | None = None
    name: str | None = None
    confidence: float | None = None
    reason: str = ""

    @property
    def is_owner(self) -> bool:
        # See decision #5 above: today "recognized" and "is the owner" are the same
        # fact because there's only one enrolled person. Kept as its own property so a
        # second enrolled person changes this one line, not every call site that
        # currently conflates the two.
        return self.recognized


def resolve_identity(db_path: str, device_id: str | None,
                     within_seconds: int = DEFAULT_IDENTITY_WINDOW_SECONDS) -> IdentityResult:
    """Who, if anyone, a kiosk terminal currently sees. Fail-closed at every branch."""
    if not device_id:
        return IdentityResult(False, reason="no device id")
    camera = _kiosk_camera_for_device(db_path, device_id)
    if camera is None:
        # No kiosk webcam paired with this terminal -- not installed yet, misconfigured,
        # or this is a transport identity gating was never scoped to. Silence, not a
        # guess: this is exactly the state every device is in before a camera is ever
        # registered for it, and it must behave identically to "nobody recognized".
        return IdentityResult(False, reason="no kiosk camera paired with this device")
    result = vision.current_identity(db_path, camera["key"], within_seconds=within_seconds)
    if not result.get("recognized"):
        return IdentityResult(False, reason=f"nobody recognized in the last {within_seconds}s")
    return IdentityResult(
        True, person_key=result["person_key"], name=result.get("label"),
        confidence=result.get("confidence"), reason="recognized",
    )


def _kiosk_camera_for_device(db_path: str, device_id: str) -> dict | None:
    for cam in vision.list_cameras(db_path, enabled_only=True):
        if cam.get("role") == "kiosk" and cam.get("device_id") == device_id:
            return cam
    return None


def gate_contexts(identity: IdentityResult, contexts: dict) -> dict:
    """Given the full set of built contexts (era=..., phone=..., personal=..., ...),
    returns the subset a voice turn from this identity is actually allowed to see.

    Contexts not in OWNER_ONLY_CONTEXTS pass through unchanged in both cases -- gating
    is about personal and financial data specifically, never about the household being
    able to use the assistant at all.
    """
    if identity.is_owner:
        return dict(contexts)
    return {k: (None if k in OWNER_ONLY_CONTEXTS else v) for k, v in contexts.items()}
