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
        results[device_id] = wake_arbitration.claim(device_id, score, window_ms=300, margin=0.05)
        durations[device_id] = time.time() - t0

    # jarvisaudio2 reports a much louder score almost immediately after jarvisaudio1;
    # jarvisaudio1 should lose well before its own 300ms window would otherwise close,
    # even though jarvisaudio2 (the eventual winner) legitimately waits out its own
    # window before declaring itself the winner.
    t1 = threading.Thread(target=run, args=("jarvisaudio1", 0.4, 0.0))
    t2 = threading.Thread(target=run, args=("jarvisaudio2", 0.95, 0.02))
    t1.start(); t2.start()
    t1.join()
    t2.join()

    assert results == {"jarvisaudio1": False, "jarvisaudio2": True}
    assert durations["jarvisaudio1"] < 0.2, \
        "the quieter terminal should lose fast once a clearly better rival appears, not wait out its whole window"


def test_close_scores_within_margin_go_to_the_higher_one_without_flapping(monkeypatch):
    monkeypatch.setattr(wake_arbitration, "_claims", {})
    results = {}

    def run(device_id, score):
        results[device_id] = wake_arbitration.claim(device_id, score, window_ms=100, margin=0.05)

    # Within the margin of each other -- neither should "clearly" beat the other during
    # the wait, so the window closes and the strictly higher score wins.
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
