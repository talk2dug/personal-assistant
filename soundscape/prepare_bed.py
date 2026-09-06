#!/usr/bin/env python3
"""Normalise a downloaded recording into a usable bed or one-shot.

Downloads are inconsistent in every way that matters: 44.1k or 22k, mono or stereo, a DC
offset from a cheap preamp, wind rumble under everything, and levels varying by 20dB
between two files from the same archive. Dropping those into the mixer straight gives a
bed that is either inaudible or swamps the layers.

    ./prepare_bed.py raw/ocean-pt-reyes.mp3 sounds/ocean/waves-bed.wav --seconds 240 --start 30
    ./prepare_bed.py raw/gull.wav sounds/ocean/gull-1.wav --seconds 4 --target-db -18

Beds default to -20 dBFS RMS, which leaves headroom for the one-shot layers to sit above
without the sum clipping. One-shots are usually run a little hotter, hence --target-db.

Needs soundfile (which needs libsndfile) for anything that is not a WAV. The engine
itself does not: this is the only place a non-WAV is ever read.
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

RATE = 48000


def read_any(path: Path) -> tuple[np.ndarray, int]:
    """WAV via the stdlib, anything else via soundfile if it is installed."""
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as w:
            ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
        if width == 2:
            audio = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
        elif width == 4:
            audio = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
        elif width == 1:
            audio = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
        else:
            raise SystemExit(f"{path}: unsupported sample width {width}")
        audio = audio.reshape(-1, ch) if ch > 1 else audio[:, None]
        return audio, rate
    try:
        import soundfile as sf
    except ImportError:
        raise SystemExit(
            f"{path.suffix} needs soundfile: sudo apt install python3-soundfile libsndfile1")
    audio, rate = sf.read(str(path), always_2d=True, dtype="float64")
    return audio, rate


def to_stereo(audio: np.ndarray) -> np.ndarray:
    if audio.shape[1] == 1:
        return np.repeat(audio, 2, axis=1)
    return audio[:, :2]


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return audio
    n_out = int(round(len(audio) * dst_rate / src_rate))
    x_old = np.arange(len(audio), dtype=np.float64)
    x_new = np.linspace(0, len(audio) - 1, n_out)
    return np.stack([np.interp(x_new, x_old, audio[:, c]) for c in range(audio.shape[1])],
                    axis=1)


def highpass(audio: np.ndarray, cutoff: float, rate: int) -> np.ndarray:
    """One-pole high-pass, to take out wind rumble and any DC the recorder left behind.

    Below about 40Hz a field recording carries handling noise and wind that no speaker
    reproduces usefully, but which does eat headroom -- so removing it makes the file
    louder at the same peak.
    """
    x = np.exp(-2.0 * np.pi * cutoff / rate)
    a = (1.0 + x) / 2.0
    out = np.empty_like(audio)
    for c in range(audio.shape[1]):
        col = audio[:, c]
        y = np.empty_like(col)
        prev_x = prev_y = 0.0
        for i in range(len(col)):
            prev_y = a * (col[i] - prev_x) + x * prev_y
            prev_x = col[i]
            y[i] = prev_y
        out[:, c] = y
    return out


def fade_ends(audio: np.ndarray, seconds: float, rate: int) -> np.ndarray:
    n = min(int(seconds * rate), len(audio) // 2)
    if n <= 0:
        return audio
    ramp = np.linspace(0.0, 1.0, n)[:, None]
    audio[:n] *= ramp
    audio[-n:] *= ramp[::-1]
    return audio


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("dest")
    ap.add_argument("--seconds", type=float, default=240.0, help="length to take")
    ap.add_argument("--start", type=float, default=0.0, help="offset into the source")
    ap.add_argument("--target-db", type=float, default=-20.0, help="RMS target in dBFS")
    ap.add_argument("--highpass", type=float, default=40.0, help="0 to disable")
    ap.add_argument("--fade", type=float, default=0.05, help="edge fade, seconds")
    args = ap.parse_args()

    src, dst = Path(args.source), Path(args.dest)
    if not src.exists():
        raise SystemExit(f"no such file: {src}")

    audio, rate = read_any(src)
    print(f"read   {src.name}: {len(audio)/rate:.1f}s, {audio.shape[1]}ch, {rate}Hz")

    begin = int(args.start * rate)
    end = begin + int(args.seconds * rate)
    audio = audio[begin:end]
    if len(audio) == 0:
        raise SystemExit("--start is past the end of the file")

    audio = to_stereo(audio)
    audio = resample(audio, rate, RATE)
    audio -= audio.mean(axis=0)                       # strip DC
    if args.highpass > 0:
        audio = highpass(audio, args.highpass, RATE)

    current = float(np.sqrt(np.mean(audio ** 2)))
    if current > 0:
        target = 10.0 ** (args.target_db / 20.0)
        audio *= target / current
        print(f"level  {20*np.log10(max(current,1e-12)):.1f} dBFS -> {args.target_db:.1f} dBFS")

    audio = fade_ends(audio, args.fade, RATE)

    peak = float(np.abs(audio).max())
    if peak > 1.0:
        # Normalising RMS can push transients past full scale; pull the whole thing down
        # rather than clipping them, since a clipped one-shot sounds broken not loud.
        audio /= peak
        print(f"peak   {peak:.2f} exceeded full scale, scaled down")

    dst.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())

    print(f"wrote  {dst}: {len(audio)/RATE:.1f}s stereo {RATE}Hz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
