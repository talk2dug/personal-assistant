#!/usr/bin/env python3
"""Prove the engine works before any real audio exists.

Synthesises a small sound library, then exercises the mixer directly rather than through
the FIFO. Testing render() is both more precise and more portable than reading a pipe:
it can assert on exact sample values, and it runs on a machine without os.mkfifo, which
matters because this gets developed on Windows and deployed on Linux.

    python3 selftest.py            # run the checks
    python3 selftest.py --keep     # also leave the generated WAVs in sounds/ocean/

With --keep you can start the daemon immediately and hear the engine before sourcing a
single real recording.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soundscaped as ss  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "  ok  " if ok else " FAIL "
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""))


def write_wav(path: Path, audio: np.ndarray, rate: int = ss.RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def synth_library(root: Path) -> None:
    """A fake ocean: a wash of filtered noise as a bed, and short chirps as one-shots."""
    rng = np.random.default_rng(7)

    # Bed: 12s of pink-ish noise, deliberately non-identical at head and tail so the
    # crossfade has something real to smooth.
    n = ss.RATE * 12
    noise = rng.standard_normal((n, 2)) * 0.15
    # Cheap smoothing to take the fizz off white noise.
    kernel = np.ones(64) / 64
    bed = np.stack([np.convolve(noise[:, c], kernel, mode="same") for c in range(2)], axis=1)
    bed /= max(float(np.abs(bed).max()), 1e-9)      # normalise; the box filter costs ~8x
    bed *= np.linspace(0.8, 1.2, n)[:, None]      # slow drift, so head != tail
    write_wav(root / "ocean" / "waves-bed.wav", bed * 0.6)

    # One-shots: 0.4s tones at different pitches, tight to the event.
    for i, freq in enumerate((900, 1400, 2100), start=1):
        t = np.linspace(0, 0.4, int(ss.RATE * 0.4), endpoint=False)
        env = np.sin(np.pi * np.linspace(0, 1, len(t))) ** 2
        tone = np.sin(2 * np.pi * freq * t) * env * 0.6
        write_wav(root / "ocean" / f"gull-{i}.wav", np.stack([tone, tone], axis=1))

    t = np.linspace(0, 1.2, int(ss.RATE * 1.2), endpoint=False)
    rumble = np.sin(2 * np.pi * 70 * t) * np.exp(-t * 2) * 0.7
    write_wav(root / "ocean" / "thunder-1.wav", np.stack([rumble, rumble], axis=1))


def write_presets(presets: Path) -> None:
    presets.mkdir(parents=True, exist_ok=True)
    (presets / "test-ocean.json").write_text(json.dumps({
        "name": "Test Ocean",
        "bed": {"files": ["ocean/waves-bed.wav"], "gain_db": -6.0, "crossfade_sec": 2.0},
        "layers": [
            {"name": "gulls", "files": ["ocean/gull-*.wav"],
             "interval_sec": [0.2, 0.35], "gain_db": [-20, -20],
             "pan": [-1.0, -1.0], "rate_jitter": 0.0, "play_at_night": False},
            {"name": "thunder", "files": ["ocean/thunder-1.wav"],
             "interval_sec": [0.2, 0.35], "gain_db": [-20, -20],
             "pan": [1.0, 1.0], "rate_jitter": 0.0, "play_at_night": True},
        ],
    }, indent=2))
    (presets / "test-silent.json").write_text(json.dumps({
        "name": "Test Silent",
        "bed": {"files": ["ocean/waves-bed.wav"], "gain_db": -6.0, "crossfade_sec": 0.0},
        "layers": [],
    }, indent=2))


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))


def render_seconds(scape: ss.Soundscape, seconds: float) -> np.ndarray:
    blocks = int(seconds * ss.RATE / ss.BLOCK)
    return np.concatenate([scape.render() for _ in range(max(1, blocks))])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="leave the synthesised WAVs in ./sounds so you can hear it now")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    tmp = Path(tempfile.mkdtemp(prefix="soundscape-test-"))
    sounds, presets = tmp / "sounds", tmp / "presets"
    synth_library(sounds)
    write_presets(presets)

    print(f"library: {sounds}\n")

    # 1-3: loading
    bed = ss.load_wav(sounds / "ocean" / "waves-bed.wav")
    check("bed loads as float32 stereo", bed.dtype == np.float32 and bed.shape[1] == 2,
          f"{bed.shape} {bed.dtype}")
    check("bed is the expected length", abs(len(bed) - ss.RATE * 12) <= 1, f"{len(bed)} samples")
    check("bed is not silent", rms(bed) > 0.01, f"rms={rms(bed):.4f}")

    # 4-6: the loop seam
    looped = ss.bake_loop(bed, 2.0)
    xf = int(2.0 * ss.RATE)
    check("baked loop is shorter by one crossfade", len(looped) == len(bed) - xf,
          f"{len(looped)} vs {len(bed)}")
    seam_gap = abs(float(looped[0].mean()) - float(looped[-1].mean()))
    check("loop seam is continuous", seam_gap < 0.25, f"discontinuity {seam_gap:.4f}")
    mid = rms(looped[xf:xf + ss.RATE])
    seam = rms(looped[:ss.RATE])
    check("equal-power crossfade holds level at the seam",
          0.5 < (seam / max(mid, 1e-9)) < 1.6, f"seam/mid = {seam / max(mid, 1e-9):.2f}")

    # 7-9: playback and fades
    scape = ss.Soundscape(presets, sounds, tmp / "fifo")
    check("presets are discovered", set(scape.available_presets()) >= {"test-ocean", "test-silent"},
          str(scape.available_presets()))
    scape.play("test-silent", level=1.0, fade_sec=0.0)
    block = render_seconds(scape, 0.5)
    check("bed produces audio", rms(block) > 0.005, f"rms={rms(block):.4f}")

    scape.set_volume(0.0, fade_sec=0.0)
    scape.render()
    scape.play("test-silent", level=1.0, fade_sec=2.0)
    first = rms(scape.render())
    later = rms(render_seconds(scape, 1.8)[-ss.BLOCK:])
    check("fade in ramps up", later > first * 3, f"{first:.5f} -> {later:.5f}")

    # 10-11: volume and ducking as independent stages
    scape.set_volume(1.0, fade_sec=0.0)
    render_seconds(scape, 0.2)
    loud = rms(render_seconds(scape, 0.3))
    scape.set_duck(0.1, fade_sec=0.0)
    render_seconds(scape, 0.2)
    ducked = rms(render_seconds(scape, 0.3))
    check("duck lowers the level", ducked < loud * 0.4, f"{loud:.4f} -> {ducked:.4f}")
    scape.unduck(fade_sec=0.0)
    render_seconds(scape, 0.2)
    restored = rms(render_seconds(scape, 0.3))
    check("unduck restores it", restored > ducked * 3, f"{ducked:.4f} -> {restored:.4f}")

    # 12: duck does not disturb the volume target underneath it
    scape.set_volume(0.5, fade_sec=0.0)
    scape.set_duck(0.2, fade_sec=0.0)
    render_seconds(scape, 0.1)
    scape.unduck(fade_sec=0.0)
    render_seconds(scape, 0.1)
    check("volume survives a duck/unduck cycle", abs(scape.status()["level"] - 0.5) < 0.01,
          f"level={scape.status()['level']}")

    # 13-15: layers, panning, night suppression
    scape.play("test-ocean", level=1.0, fade_sec=0.0)
    audio = render_seconds(scape, 3.0)
    check("one-shot layers fire", rms(audio) > 0.02, f"rms={rms(audio):.4f}")
    left, right = rms(audio[:, 0]), rms(audio[:, 1])
    check("panning is audible across channels", abs(left - right) > 0.002,
          f"L={left:.4f} R={right:.4f}")

    day_balance = rms(audio[:, 0]) / max(rms(audio[:, 1]), 1e-9)
    scape.play("test-ocean", level=1.0, fade_sec=0.0, night=True)
    night_audio = render_seconds(scape, 4.0)
    night_balance = rms(night_audio[:, 0]) / max(rms(night_audio[:, 1]), 1e-9)
    check("night suppresses day-only layers", night_balance < day_balance * 0.9,
          f"L/R balance day {day_balance:.2f} -> night {night_balance:.2f}")

    # 16-18: the sleep fade
    scape.play("test-silent", level=1.0, fade_sec=0.0, night=True)
    render_seconds(scape, 0.1)
    scape.sleep(None, duration_min=0.05, level=1.0)      # 3 second fade
    start = rms(render_seconds(scape, 0.3))
    render_seconds(scape, 1.3)                 # advance the stream, not the wall clock
    middle = rms(render_seconds(scape, 0.3))
    render_seconds(scape, 1.5)
    end_block = render_seconds(scape, 0.3)
    check("sleep fade reduces level over time", middle < start,
          f"{start:.4f} -> {middle:.4f}")
    check("sleep fade reaches silence", rms(end_block) < start * 0.1,
          f"final rms={rms(end_block):.6f}")
    check("status reports the sleep timer", "sleep_remaining_sec" in scape.status())

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed:", ", ".join(FAIL))

    if args.keep:
        dest = here / "sounds"
        for src in (sounds).rglob("*.wav"):
            target = dest / src.relative_to(sounds)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        shutil.copy2(presets / "test-ocean.json", here / "presets" / "test-ocean.json")
        print(f"\nkept the synthesised library in {dest}")
        print("  try:  python3 soundscaped.py --preset test-ocean --fifo /tmp/x")
    else:
        shutil.rmtree(tmp, ignore_errors=True)

    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
