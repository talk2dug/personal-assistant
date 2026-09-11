"""Covers wake_arbitration.claim() -- the cross-terminal decision of who actually
answers a single wake-word moment."""
import threading
import time

from assistant.core import wake_arbitration


def test_solo_claim_always_wins(monkeypatch):
    monkeypatch.setattr(wake_arbitration, "_claims", {})
    assert wake_arbitration.claim("touch1", 0.8, window_ms=50) is True


def test_clearly_louder_rival_wins_and_loser_returns_before_window_closes(monkeypatch):
    monkeypatch.setattr(wake_arbitration, "_claims", {})
    results = {}
    durations = {}

    def run(device_id, score, delay):
        time.sleep(delay)
        t0 = time.time()
        results[device_id] = wake_arbitration.claim(device_id, score, window_ms=500, margin=0.05)
        durations[device_id] = time.time() - t0

    # jarvisaudio2 reports a much louder score shortly after jarvisaudio1; jarvisaudio1
    # should give up well before its own window closes, once it sees a rival it can't
    # beat -- jarvisaudio2 (the eventual winner) legitimately waits out its own window
    # first, since nothing tells it early that it has already won.
    t1 = threading.Thread(target=run, args=("jarvisaudio1", 0.4, 0.0))
    t2 = threading.Thread(target=run, args=("jarvisaudio2", 0.95, 0.05))
    t1.start(); t2.start()
    t1.join()
    t2.join()

    assert results == {"jarvisaudio1": False, "jarvisaudio2": True}
    # A relative comparison rather than an absolute wall-clock bound -- robust to a
    # slower/noisier CI runner, where an absolute millisecond threshold would be
    # measuring the machine rather than the behaviour. The loser detects a rival and
    # returns after a handful of poll intervals; the winner waits out essentially the
    # whole window either way, so the loser should always finish clearly first.
    assert durations["jarvisaudio1"] < durations["jarvisaudio2"] / 2


def test_close_scores_within_margin_go_to_the_higher_one_without_flapping(monkeypatch):
    monkeypatch.setattr(wake_arbitration, "_claims", {})
    results = {}

    def run(device_id, score):
        results[device_id] = wake_arbitration.claim(device_id, score, window_ms=400, margin=0.05)

    # Within the margin of each other -- neither should "clearly" beat the other during
    # the wait, so the window closes and the strictly higher score wins. This genuinely
    # flaked before claim() stopped self-popping a device's own entry right after
    # computing its candidates (see wake_arbitration.py) -- whichever thread's window
    # closed first would remove itself from _claims before the other thread's own
    # candidates read ran, occasionally handing the loser a win. window_ms is left
    # generous, matching DEFAULT_WINDOW_MS, purely to give two real OS threads room
    # rather than to paper over that race.
    t1 = threading.Thread(target=run, args=("touch1", 0.60))
    t2 = threading.Thread(target=run, args=("laptop1", 0.61))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results["laptop1"] is True
    assert results["touch1"] is False


def test_a_stale_claim_does_not_block_a_later_independent_arbitration(monkeypatch):
    monkeypatch.setattr(wake_arbitration, "_claims", {})
    assert wake_arbitration.claim("touch1", 0.9, window_ms=20) is True
    time.sleep(0.1)
    # A fresh, unrelated wake moment: touch1's own earlier claim must not still be
    # "in the window" and treated as a rival to itself.
    assert wake_arbitration.claim("touch1", 0.3, window_ms=20) is True
