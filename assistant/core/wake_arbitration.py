"""In-memory arbitration across voice terminals for a single wake-word moment.

Every terminal (touch1, laptop1, jarvisaudio1, jarvisaudio2, jarvisbox, ...) runs its own
openWakeWord model against its own microphone at all times -- see device/jarvis_device.py.
That's a deliberate privacy choice (raw audio never leaves a room unless something in
that room actually said the wake word), and it means more than one terminal can hear the
same real utterance if rooms are close together or a door is open. Without arbitration,
every terminal that heard it chimes, records, and answers -- overlapping replies from
two directions at once.

claim() is the fix: the instant a terminal's local model crosses its wake threshold, it
reports its confidence score here and waits briefly to see whether a rival, louder claim
comes in from elsewhere before committing to chime and record. Confidence score is used
as the proximity signal rather than something more elaborate (RSSI, camera-confirmed
position) because it's the one signal every terminal already produces uniformly, with or
without camera coverage -- and camera coverage is day-one only two terminals deep, while
wake-word terminals are more than that already.

Deliberately in-memory, not a table: like devices.py's own DEVICE_STATE, this describes a
few hundred milliseconds of "who's currently arbitrating", meaningless a moment later and
across a restart. Persisting it would only add a table to prune.
"""
import threading
import time

# How long a terminal waits, having reported its own score, to see if a louder claim
# comes in before deciding the window has closed. Long enough for every terminal on a
# home LAN to have reported (well under 400ms round trip even on a loaded Pi), short
# enough that the pause between the wake word and starting to record doesn't feel like a
# hang.
DEFAULT_WINDOW_MS = 400
# A rival claim has to beat the current best by more than this to matter -- otherwise two
# terminals a similar distance from someone (open-plan rooms, a hallway) could flap
# between which one wins turn to turn for no perceptible reason.
DEFAULT_MARGIN = 0.05
# Poll interval while waiting out the arbitration window. Small relative to the window so
# the decision isn't meaningfully delayed by the polling granularity itself.
_POLL_SECONDS = 0.02

_lock = threading.Lock()
# device_id -> (score, claimed_at). Module-level and process-wide on purpose: every
# terminal's /wake_claim request lands on the same jarvis-web process, so a plain dict
# behind a lock is the whole mechanism -- no cross-process coordination is needed.
_claims: dict[str, tuple[float, float]] = {}


def _prune_stale(now: float, window_s: float) -> None:
    """Drops claims old enough that they can't be relevant to a *new* arbitration --
    otherwise a terminal that crashed mid-window (or simply never called back) would
    permanently occupy a slot and could keep winning on a stale score forever."""
    stale = [d for d, (_, t) in _claims.items() if now - t > window_s * 4]
    for d in stale:
        _claims.pop(d, None)


def claim(device_id: str, score: float, window_ms: int = DEFAULT_WINDOW_MS,
         margin: float = DEFAULT_MARGIN) -> bool:
    """Registers this terminal's wake-word confidence and blocks for up to `window_ms`.

    Returns True if this terminal should proceed (chime, record, answer); False if
    another terminal reported a clearly better score for the same moment and should
    answer instead. Safe to call concurrently from every terminal's own request.
    """
    window_s = window_ms / 1000.0
    now = time.time()
    with _lock:
        _prune_stale(now, window_s)
        _claims[device_id] = (score, now)

    deadline = now + window_s
    while time.time() < deadline:
        time.sleep(_POLL_SECONDS)
        with _lock:
            rival_best = max(
                (s for d, (s, t) in _claims.items() if d != device_id and t >= now - window_s),
                default=None,
            )
        if rival_best is not None and rival_best >= score + margin:
            # Someone clearly closer has already claimed it -- no reason to keep waiting
            # out the rest of the window; that's a faster "go back to sleep" for the loser.
            with _lock:
                _claims.pop(device_id, None)
            return False

    with _lock:
        candidates = [(s, d) for d, (s, t) in _claims.items() if t >= now - window_s]
        _claims.pop(device_id, None)
    if not candidates:
        return True
    # Ties (a genuinely simultaneous, equal-confidence hear) go to whichever device_id
    # sorts first -- deterministic, rather than depending on dict iteration order, which
    # isn't a contract worth relying on here.
    best_score, best_device = max(candidates, key=lambda pair: (pair[0], pair[1]))
    return best_device == device_id
