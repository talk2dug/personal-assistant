"""Regression: a restart must not make a new recipe look like one already seen.

Found in the field -- a recipe was staged for the kitchen screen, display_recipe reported
success, and the screen never changed. DEVICE_STATE is in-memory and empties on restart,
so the old counter restarted at 1, while the kiosk page holds its last-seen value for its
whole lifetime (weeks on a wall display). The new 1 compared equal to the stale 1 and the
overlay was silently skipped.
"""
import time

from assistant.web.routes import devices


def test_a_sequence_value_never_repeats_across_a_restart():
    first = devices._next_seq()
    # A restart is exactly this: the dict is emptied and nothing else changes.
    devices.DEVICE_STATE.clear()
    time.sleep(0.005)
    second = devices._next_seq()
    assert second > first, "a restart must not hand out a value a client may already hold"


def test_it_does_not_restart_from_one():
    """The whole failure was a counter resetting into a range clients still remember."""
    devices.DEVICE_STATE.clear()
    assert devices._next_seq() > 1_000_000


def test_successive_values_increase():
    a = devices._next_seq()
    time.sleep(0.005)
    assert devices._next_seq() > a
