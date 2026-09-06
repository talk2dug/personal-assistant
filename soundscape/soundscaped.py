#!/usr/bin/env python3
"""Generative ambient soundscape daemon.

Writes raw PCM to a FIFO that snapserver reads as a pipe source, so every Snapcast
client in the house plays the same sample-synced stream.

The design point is that it never repeats. A bed loops underneath, but the character
comes from one-shot layers fired at randomised intervals, gains, stereo positions and
playback rates. Three gull files with rate jitter read as a dozen different gulls; the
same three on a fixed loop read as one gull, forever, and that fatigue is the entire
failure mode this exists to avoid.

Timing is the pipe's job. Writing to a FIFO blocks until snapserver consumes, so the
mixer is paced at exactly real time without a clock, a sleep loop, or any drift to
correct. That is also why the daemon appears to hang before snapserver attaches: the
open() blocks on the reader. The HTTP API is up and answering throughout.

Audio is float32 internally and int16 on the wire. Only the standard library plus numpy
is needed here; soundfile is used by prepare_bed.py, not by the engine, so a satellite
running this needs almost nothing installed.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import math
import os
import random
import struct
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

log = logging.getLogger("soundscaped")

RATE = 48000
CHANNELS = 2
BLOCK = 1024          # frames per mixer block; ~21ms, small enough that a duck feels instant
DTYPE = np.float32


# --- loading ------------------------------------------------------------------

def load_wav(path: str | Path) -> np.ndarray:
    """Read a WAV into float32 stereo at RATE.

    Deliberately stdlib-only. The engine should run on a satellite with numpy and
    nothing else, and every file it loads has already been through prepare_bed.py.
    Mono is duplicated rather than panned centre so later panning has something to work
    with on both channels.
    """
    with wave.open(str(path), "rb") as w:
        n_channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())

    if width == 2:
        audio = np.frombuffer(frames, dtype="<i2").astype(DTYPE) / 32768.0
    elif width == 4:
        audio = np.frombuffer(frames, dtype="<i4").astype(DTYPE) / 2147483648.0
    elif width == 1:
        audio = (np.frombuffer(frames, dtype=np.uint8).astype(DTYPE) - 128.0) / 128.0
    else:
        raise ValueError(f"{path}: unsupported sample width {width}")

    if n_channels > 1:
        audio = audio.reshape(-1, n_channels)[:, :2]
        if audio.shape[1] == 1:
            audio = np.repeat(audio, 2, axis=1)
    else:
        audio = np.stack([audio, audio], axis=1)

    if rate != RATE:
        # Linear resample. The library is normalised to 48k by prepare_bed.py, so this
        # is a safety net for a file that slipped through rather than a hot path.
        n_out = int(round(len(audio) * RATE / rate))
        x_old = np.arange(len(audio), dtype=np.float64)
        x_new = np.linspace(0, len(audio) - 1, n_out)
        audio = np.stack([np.interp(x_new, x_old, audio[:, c]) for c in range(2)],
                         axis=1).astype(DTYPE)

    return np.ascontiguousarray(audio, dtype=DTYPE)


def bake_loop(bed: np.ndarray, crossfade_sec: float) -> np.ndarray:
    """Return a bed that loops seamlessly, with the crossfade baked into the seam.

    Equal power (sqrt curves) rather than linear: a linear crossfade of two uncorrelated
    ambiences dips ~3dB in the middle, which is audible as a breath every time round --
    exactly the periodic artefact the whole design is trying not to have.
    """
    xf = int(crossfade_sec * RATE)
    if xf <= 0 or len(bed) <= 2 * xf:
        return bed
    head, tail = bed[:xf], bed[-xf:]
    t = np.linspace(0.0, 1.0, xf, dtype=DTYPE)[:, None]
    seam = head * np.sqrt(t) + tail * np.sqrt(1.0 - t)
    return np.concatenate([seam, bed[xf:-xf]]).astype(DTYPE)


def expand_files(root: Path, patterns: list[str]) -> list[Path]:
    """Resolve a preset's file list, allowing globs so 'gull-*.wav' picks up however
    many variants exist without editing the preset."""
    out: list[Path] = []
    for pattern in patterns:
        matches = sorted(glob.glob(str(root / pattern)))
        out.extend(Path(m) for m in matches)
    return out


# --- layers -------------------------------------------------------------------

class Layer:
    """One family of one-shots -- gulls, owls, thunder -- and the rules for firing them."""

    def __init__(self, spec: dict, root: Path):
        self.name: str = spec.get("name", "layer")
        self.files = expand_files(root, spec.get("files", []))
        self.samples = [load_wav(p) for p in self.files]
        lo, hi = spec.get("interval_sec", [60, 180])
        self.interval = (float(lo), float(hi))
        g_lo, g_hi = spec.get("gain_db", [-24, -14])
        self.gain_db = (float(g_lo), float(g_hi))
        p_lo, p_hi = spec.get("pan", [-0.7, 0.7])
        self.pan = (float(p_lo), float(p_hi))
        self.rate_jitter = float(spec.get("rate_jitter", 0.0))
        self.play_at_night = bool(spec.get("play_at_night", False))
        self.next_at = 0.0

    def stagger(self, now: float) -> None:
        """Pick the first fire time, in audio seconds. Called on every /play so that
        resuming doesn't fire every layer at once -- all their timers would otherwise be
        long overdue and the soundscape would open with a burst instead of a scene."""
        lo, hi = self.interval
        self.next_at = now + random.uniform(0.0, hi)

    def due(self, now: float) -> bool:
        return bool(self.samples) and now >= self.next_at

    def reschedule(self, now: float) -> None:
        self.next_at = now + random.uniform(*self.interval)

    def make_voice(self) -> "Voice":
        sample = random.choice(self.samples)
        if self.rate_jitter > 0:
            # Resampling shifts pitch and duration together, which is what makes the
            # same file read as a different animal rather than the same one slowed down.
            rate = random.uniform(1.0 - self.rate_jitter, 1.0 + self.rate_jitter)
            n_out = max(1, int(len(sample) / rate))
            x_old = np.arange(len(sample), dtype=np.float64)
            x_new = np.linspace(0, len(sample) - 1, n_out)
            sample = np.stack([np.interp(x_new, x_old, sample[:, c]) for c in range(2)],
                              axis=1).astype(DTYPE)
        gain = 10.0 ** (random.uniform(*self.gain_db) / 20.0)
        pan = random.uniform(*self.pan)
        return Voice(sample, gain, pan)


class Voice:
    """A one-shot currently sounding."""

    __slots__ = ("audio", "pos")

    def __init__(self, sample: np.ndarray, gain: float, pan: float):
        # Constant-power pan: a hard-panned sound keeps its apparent loudness, where a
        # linear pan law makes anything off-centre quieter as well as sideways.
        theta = (pan + 1.0) * 0.25 * math.pi
        gains = np.array([math.cos(theta), math.sin(theta)], dtype=DTYPE) * gain * math.sqrt(2.0)
        self.audio = (sample * gains).astype(DTYPE)
        self.pos = 0

    def mix_into(self, out: np.ndarray) -> bool:
        """Add this voice to the block. Returns False when it has finished."""
        remaining = len(self.audio) - self.pos
        if remaining <= 0:
            return False
        n = min(len(out), remaining)
        out[:n] += self.audio[self.pos:self.pos + n]
        self.pos += n
        return self.pos < len(self.audio)


class Preset:
    def __init__(self, path: Path, sounds_root: Path):
        spec = json.loads(Path(path).read_text())
        self.name: str = spec.get("name", Path(path).stem)
        self.key: str = Path(path).stem
        bed_spec = spec.get("bed", {})
        bed_files = expand_files(sounds_root, bed_spec.get("files", []))
        if not bed_files:
            raise ValueError(f"{path}: preset has no bed files that exist")
        # One bed picked at random per start, so restarting the same preset on different
        # days does not begin identically.
        bed = load_wav(random.choice(bed_files))
        self.bed = bake_loop(bed, float(bed_spec.get("crossfade_sec", 6.0)))
        self.bed_gain = 10.0 ** (float(bed_spec.get("gain_db", -6.0)) / 20.0)
        self.layers = [Layer(l, sounds_root) for l in spec.get("layers", [])]


# --- the mixer ----------------------------------------------------------------

class Soundscape:
    """Holds all mutable state. Every field the HTTP thread touches is guarded by
    `lock`; the mixer takes it once per block and works from a snapshot."""

    def __init__(self, presets_dir: Path, sounds_dir: Path, fifo: Path):
        self.presets_dir = presets_dir
        self.sounds_dir = sounds_dir
        self.fifo = fifo
        self.lock = threading.Lock()

        self.preset: Preset | None = None
        self.bed_pos = 0
        self.voices: list[Voice] = []

        # Three independent gain stages. Keeping duck separate from volume is what lets
        # Jarvis duck mid-sleep-fade without disturbing the fade, and un-duck afterwards
        # without undoing it.
        self.level = 0.0
        self.level_target = 0.0
        self.level_step = 0.0
        self.duck = 1.0
        self.duck_target = 1.0
        self.duck_step = 0.0

        self.night = False
        self.sleep_until = 0.0
        self.sleep_total = 0.0

        # One-pole low-pass state, used only during a sleep fade. Losing the high end as
        # the level falls is most of what drifting off actually feels like; volume alone
        # just gets quiet.
        self.lp_state = np.zeros(CHANNELS, dtype=np.float64)
        self.lp_coeff = 1.0

        self.running = True
        self.started_at = time.time()
        self.blocks = 0
        # Audio time: seconds of stream actually produced. Everything scheduled -- layer
        # intervals, the sleep fade -- is measured against this rather than the wall
        # clock, so timing is a property of the stream instead of of how promptly this
        # process happened to be scheduled.
        self.frames = 0

    @property
    def audio_now(self) -> float:
        """Seconds of audio produced so far. Read under the lock from other threads."""
        return self.frames / RATE

    # -- control (called from the HTTP thread) -------------------------------

    def available_presets(self) -> list[str]:
        return sorted(p.stem for p in self.presets_dir.glob("*.json"))

    def play(self, key: str, level: float = 1.0, fade_sec: float = 5.0,
             night: bool = False) -> None:
        path = self.presets_dir / f"{key}.json"
        if not path.exists():
            raise FileNotFoundError(f"no preset {key!r}")
        preset = Preset(path, self.sounds_dir)
        with self.lock:
            now = self.audio_now
            for layer in preset.layers:
                layer.stagger(now)
            self.preset = preset
            self.bed_pos = 0
            self.voices = []
            self.night = night
            self.sleep_until = 0.0
            self.sleep_total = 0.0
            self.lp_coeff = 1.0
            self._fade_to(level, fade_sec)

    def stop(self, fade_sec: float = 5.0) -> None:
        with self.lock:
            self._fade_to(0.0, fade_sec)

    def set_volume(self, level: float, fade_sec: float = 2.0) -> None:
        with self.lock:
            self._fade_to(level, fade_sec)

    def set_duck(self, level: float, fade_sec: float = 0.4) -> None:
        with self.lock:
            self.duck_target = max(0.0, min(1.0, level))
            self.duck_step = self._step(self.duck, self.duck_target, fade_sec)

    def unduck(self, fade_sec: float = 1.5) -> None:
        self.set_duck(1.0, fade_sec)

    def sleep(self, key: str | None, duration_min: float, level: float = 0.6) -> None:
        if key:
            self.play(key, level=level, fade_sec=5.0, night=True)
        with self.lock:
            self.night = True
            self.sleep_total = max(1.0, duration_min * 60.0)
            self.sleep_until = self.audio_now + self.sleep_total

    def status(self) -> dict:
        with self.lock:
            remaining = max(0.0, self.sleep_until - self.audio_now) if self.sleep_until else 0.0
            return {
                "preset": self.preset.key if self.preset else None,
                "preset_name": self.preset.name if self.preset else None,
                "playing": bool(self.preset) and self.level > 0.001,
                "level": round(self.level, 3),
                "level_target": round(self.level_target, 3),
                "duck": round(self.duck, 3),
                "ducked": self.duck < 0.99,
                "night": self.night,
                "sleep_remaining_sec": int(remaining),
                "layers": [l.name for l in self.preset.layers] if self.preset else [],
                "voices": len(self.voices),
                "uptime_sec": int(time.time() - self.started_at),
                "presets": self.available_presets(),
            }

    # -- internals ------------------------------------------------------------

    def _fade_to(self, target: float, fade_sec: float) -> None:
        self.level_target = max(0.0, min(1.0, target))
        self.level_step = self._step(self.level, self.level_target, fade_sec)

    @staticmethod
    def _step(current: float, target: float, fade_sec: float) -> float:
        blocks = max(1.0, (fade_sec * RATE) / BLOCK)
        return (target - current) / blocks

    def _advance_gains(self) -> None:
        if self.level_step:
            self.level += self.level_step
            if ((self.level_step > 0 and self.level >= self.level_target) or
                    (self.level_step < 0 and self.level <= self.level_target)):
                self.level, self.level_step = self.level_target, 0.0
        if self.duck_step:
            self.duck += self.duck_step
            if ((self.duck_step > 0 and self.duck >= self.duck_target) or
                    (self.duck_step < 0 and self.duck <= self.duck_target)):
                self.duck, self.duck_step = self.duck_target, 0.0

    def _sleep_envelope(self, now: float) -> float:
        """Cosine taper for the sleep fade, and the matching low-pass cutoff.

        Barely moves for the first two thirds and then commits, which is far less
        noticeable than a linear ramp that is audibly descending the whole way down.
        """
        if not self.sleep_until or self.sleep_total <= 0:
            self.lp_coeff = 1.0
            return 1.0
        remaining = self.sleep_until - now
        if remaining <= 0:
            self.lp_coeff = 1.0
            return 0.0
        progress = 1.0 - (remaining / self.sleep_total)      # 0 -> 1 over the fade
        env = 0.5 * (1.0 + math.cos(math.pi * progress))     # 1 -> 0, cosine
        # Cutoff falls from ~18kHz to ~350Hz as the envelope closes.
        cutoff = 350.0 + (18000.0 - 350.0) * (env ** 2)
        x = math.exp(-2.0 * math.pi * cutoff / RATE)
        self.lp_coeff = 1.0 - x
        return env

    def render(self, frames: int = BLOCK) -> np.ndarray:
        """Produce one block of stereo float32. The only place audio is created."""
        out = np.zeros((frames, CHANNELS), dtype=DTYPE)

        with self.lock:
            now = self.audio_now
            preset = self.preset
            if preset is not None:
                bed = preset.bed
                # Wrap the bed read; the seam is already crossfaded so this is seamless.
                end = self.bed_pos + frames
                if end <= len(bed):
                    out += bed[self.bed_pos:end] * preset.bed_gain
                    self.bed_pos = end % len(bed)
                else:
                    first = len(bed) - self.bed_pos
                    out[:first] += bed[self.bed_pos:] * preset.bed_gain
                    rest = frames - first
                    out[first:] += bed[:rest] * preset.bed_gain
                    self.bed_pos = rest

                for layer in preset.layers:
                    if layer.due(now):
                        # Night suppression happens at fire time, not load time, so
                        # entering sleep mode silences layers already scheduled.
                        if not self.night or layer.play_at_night:
                            self.voices.append(layer.make_voice())
                        layer.reschedule(now)

                self.voices = [v for v in self.voices if v.mix_into(out)]

            self.frames += frames
            self._advance_gains()
            envelope = self._sleep_envelope(now)
            gain = self.level * self.duck * envelope
            lp = self.lp_coeff
            self.blocks += 1

        out *= gain

        if lp < 0.999:
            # One-pole per channel, carried across blocks so there is no discontinuity
            # at block boundaries. A per-sample Python loop, which sounds alarming but
            # costs ~1% of a core at 48kHz and only runs during a sleep fade -- worth it
            # to keep the engine's dependencies at numpy alone.
            state = self.lp_state
            for c in range(CHANNELS):
                col = out[:, c].astype(np.float64)
                y = np.empty_like(col)
                acc = state[c]
                for i in range(len(col)):
                    acc += lp * (col[i] - acc)
                    y[i] = acc
                state[c] = acc
                out[:, c] = y.astype(DTYPE)

        # Hard clip as a backstop. If this is doing anything audible, bed.gain_db is set
        # too high -- the bed is normalised to -20 dBFS precisely so the layers fit above.
        np.clip(out, -1.0, 1.0, out=out)
        return out

    def run(self) -> None:
        """Open the FIFO and write forever. The open blocks until snapserver attaches,
        and each write blocks until it consumes -- which is what paces this at real time
        with no clock of its own."""
        log.info("opening %s (waits until snapserver attaches)", self.fifo)
        fd = os.open(str(self.fifo), os.O_WRONLY)
        log.info("snapserver attached, streaming")
        try:
            while self.running:
                block = self.render(BLOCK)
                pcm = (block * 32767.0).astype("<i2").tobytes()
                os.write(fd, pcm)
        finally:
            os.close(fd)


# --- HTTP control -------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    scape: Soundscape = None       # type: ignore[assignment]

    def log_message(self, fmt, *args):        # noqa: A003 - quieten the default logger
        log.debug("http %s", fmt % args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                          # noqa: N802
        if self.path.rstrip("/") in ("/status", ""):
            self._send(200, self.scape.status())
        elif self.path.rstrip("/") == "/presets":
            self._send(200, {"presets": self.scape.available_presets()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):                         # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._send(400, {"error": "body must be JSON"})
            return

        route = self.path.rstrip("/")
        s = self.scape
        try:
            if route == "/play":
                s.play(body.get("preset") or "", float(body.get("level", 1.0)),
                       float(body.get("fade_sec", 5.0)), bool(body.get("night", False)))
            elif route == "/stop":
                s.stop(float(body.get("fade_sec", 5.0)))
            elif route == "/volume":
                s.set_volume(float(body.get("level", 1.0)), float(body.get("fade_sec", 2.0)))
            elif route == "/duck":
                s.set_duck(float(body.get("level", 0.12)), float(body.get("fade_sec", 0.4)))
            elif route == "/unduck":
                s.unduck(float(body.get("fade_sec", 1.5)))
            elif route == "/sleep":
                s.sleep(body.get("preset"), float(body.get("duration_min", 45)),
                        float(body.get("level", 0.6)))
            else:
                self._send(404, {"error": f"unknown route {route}"})
                return
        except FileNotFoundError as e:
            self._send(404, {"error": str(e)})
            return
        except Exception as e:
            self._send(400, {"error": f"{type(e).__name__}: {e}"})
            return
        # Every call answers with current status, so a caller never needs a second
        # request to find out what happened.
        self._send(200, s.status())


def serve_http(scape: Soundscape, port: int) -> ThreadingHTTPServer:
    Handler.scape = scape
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    log.info("control API on :%d", port)
    return server


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent
    ap.add_argument("--fifo", default="/run/soundscape/ambient")
    ap.add_argument("--presets", default=str(here / "presets"))
    ap.add_argument("--sounds", default=str(here / "sounds"))
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--preset", help="preset to start playing immediately")
    ap.add_argument("--level", type=float, default=1.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    fifo = Path(args.fifo)
    fifo.parent.mkdir(parents=True, exist_ok=True)
    if not fifo.exists():
        os.mkfifo(str(fifo), 0o660)
        log.info("created FIFO %s", fifo)

    scape = Soundscape(Path(args.presets), Path(args.sounds), fifo)
    serve_http(scape, args.port)

    if args.preset:
        try:
            scape.play(args.preset, level=args.level)
            log.info("playing %s", args.preset)
        except Exception as e:
            log.error("could not start %s: %s", args.preset, e)

    try:
        scape.run()
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
