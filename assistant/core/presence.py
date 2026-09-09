"""The gating logic for Room Presence & Identity: decides, for a given voice terminal,
whether the person actually standing near it is confirmed by camera to be someone
allowed to hear personal or financial information -- and if not, strips those
integrations out of the turn entirely rather than trusting the model to decline
gracefully.

Two design choices worth being explicit about, because they cut against the grain of how
the rest of this codebase handles optional integrations:

**Fails closed.** Every other optional context here (phone, Home Assistant, mail...) is
designed so an outage only disables a feature, never blocks the assistant -- see
setup.py. Identity is the deliberate exception: a camera that's unreachable, a face that
doesn't match, or simply nobody visible in frame all collapse to the same answer, "not
confirmed". The cost of a false negative here (Jarvis is more cautious than it strictly
needed to be) is inconvenience; the cost of a false positive (an unconfirmed voice gets
the bank balance) is not one you get to take back.

**Confirmation is per-turn, not per-session.** There is no "logged in until you leave"
state. Every device turn re-checks the camera at call time -- someone confirmed thirty
seconds ago who has since stepped out of frame is, correctly, unconfirmed again on the
very next question.
"""
from dataclasses import dataclass

from . import db, vision

# Which known_people.access_level values unlock personal/financial context on an
# unauthenticated voice terminal. A config list, not a hardcoded 'owner' check, because
# "household" (a partner who should see the shared calendar but maybe not the crypto
# account) is a real distinction the owner may want later -- see UserConfig/
# BusinessProfile for the same reasoning about not baking in assumptions that are
# actually policy.
DEFAULT_AUTHORIZED_ACCESS_LEVELS = frozenset({"owner"})

# The context kwargs handle_message accepts that actually carry personal or financial
# information. A deliberately explicit, named list rather than "gate everything not
# excluded": a new integration added later without updating this list would default to
# *visible* on an unconfirmed terminal, which is the wrong default for anything touching
# money or the owner's own data. That absence should be caught in review, not discovered
# in production -- so a new sensitive integration must be added here on purpose.
#
# Deliberately left OUT for day one: phone, calendar, home_assistant, obsidian, airbnb,
# ticketmaster. Calendar and HA in particular are judgment calls the owner should
# confirm -- see the PR description -- but they're already one layer defended today
# (ha_sensitive_domains/pending_actions requires an explicit "yes" in the same
# conversation for lock/alarm/cover) whereas era/mail/ccxt/etc. hand over information
# just by being asked, with nothing else in the way.
SENSITIVE_CONTEXT_KEYS = (
    "era",            # bank/financial accounts, budgets, transactions
    "personal",       # personal projects/tasks/pantry -- his own life
    "mail",           # email, read and send
    "business",       # the business's financial/ops data
    "ccxt",           # real-money crypto trading
    "kroger",         # grocery cart tied to his real account
    "letterstream",   # physical mail sent under his real name/address
)


@dataclass
class PresenceResult:
    device_id: str
    camera_key: str | None
    identity: dict | None       # known_people row, or None
    authorized: bool
    reason: str                 # for logs / a review page, never spoken to the room


def confirmed_identity(db_path: str, device_id: str, within_seconds: int = 45) -> dict | None:
    """The known_people row for whoever the terminal's assigned camera most recently and
    currently sees, or None if nobody is confirmed (including: no camera assigned)."""
    camera_key = vision.get_terminal_camera(db_path, device_id)
    if not camera_key:
        return None
    return vision.identity_on_camera(db_path, camera_key, within_seconds=within_seconds)


def evaluate(
    db_path: str, device_id: str, within_seconds: int = 45,
    authorized_access_levels: frozenset[str] | set[str] | None = None,
) -> PresenceResult:
    """The single decision point: can this terminal's current turn see
    personal/financial context."""
    levels = authorized_access_levels or DEFAULT_AUTHORIZED_ACCESS_LEVELS
    camera_key = vision.get_terminal_camera(db_path, device_id)
    if not camera_key:
        return PresenceResult(device_id, None, None, False, "no camera assigned to this terminal")

    identity = vision.identity_on_camera(db_path, camera_key, within_seconds=within_seconds)
    if identity is None:
        return PresenceResult(device_id, camera_key, None, False, "no confirmed identity on camera")

    if identity.get("access_level") not in levels:
        return PresenceResult(
            device_id, camera_key, identity, False,
            f"{identity['name']} is recognised but not authorized for sensitive info",
        )
    return PresenceResult(device_id, camera_key, identity, True, f"confirmed as {identity['name']}")


def gate_contexts(contexts: dict, result: PresenceResult) -> dict:
    """`contexts` unchanged if authorized; otherwise a copy with every sensitive key
    forced to None, so an unconfirmed speaker's turn is built with those integrations
    genuinely absent -- not merely instructed not to use them.
    """
    if result.authorized:
        return contexts
    gated = dict(contexts)
    for key in SENSITIVE_CONTEXT_KEYS:
        if key in gated:
            gated[key] = None
    return gated


GUEST_KEY_PREFIX = "__device_guest__:"


def guest_user_id(db_path: str, device_id: str) -> int:
    """A per-device, identity-less user row so an unconfirmed turn's conversation history
    and any privately-scoped writes stay isolated from the owner's real account -- belt
    and suspenders alongside gate_contexts: even if some future tool call didn't route
    through a sensitive context key, it would still be running as a user with no private
    data of its own to expose.

    Per-device rather than one shared guest, so a stranger at touch1 never inherits
    whatever an unconfirmed conversation at laptop1 said five minutes earlier.
    """
    return db.get_or_create_guest_user(
        db_path, f"{GUEST_KEY_PREFIX}{device_id}", f"Guest ({device_id})")
