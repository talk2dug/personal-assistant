#!/usr/bin/env python3
"""Jarvis voice terminal â€” the client that runs on each Raspberry Pi.

Deliberately thin. It listens for a wake word, records what you say, hands the audio to
Jarvis, and plays the answer. No speech recognition, no language model, no text-to-speech
of its own. That is the whole point: every terminal is identical and disposable, building
the fourth one is flashing an SD card, and improving the assistant improves all of them
at once without touching a single Pi.

Flow, once per exchange:
    openWakeWord hears "hey jarvis"  ->  chime  ->  record until you stop talking
    ->  POST the audio to /api/devices/<id>/turn  ->  play the WAV that comes back

One exception to "waits for the wake word": open-mic mode. A background poller
(JarvisClient.conversation_mode) asks the server every few seconds whether a camera-
recognised known person is currently in view of this device (assistant/web/routes/
vision.py's /api/vision/conversation-mode/{device_id}, driven by core/camera_watch.py).
While that's true, the wake-word branch of the state machine below is simply skipped --
any frame arriving in state "wake" starts recording immediately, no "hey Jarvis" needed --
and control returns to that same bypass after each utterance rather than back to
wake-word gating. The instant presence/identity is lost, so does open-mic; there is no
latch that keeps it on past whoever triggered it.

Audio is owned entirely by this process. A browser on the same Pi cannot also hold the
microphone â€” two clients fighting over one capture device is the kind of thing that works
on the bench and fails at 2am â€” so the screen is a display only, driven by the state this
client reports to the server.

Install and run:
    pip install -r device/requirements.txt
    python3 device/jarvis_device.py --config device/device.json
"""
from __future__ import annotations

import argparse
import base64
import collections
import io
import json
import logging
import pathlib
import subprocess
import sys
import threading
import time
import wave

import httpx
import numpy as np
import sounddevice as sd

log = logging.getLogger("jarvis-device")

SAMPLE_RATE = 16000          # what both openWakeWord and Whisper expect
FRAME_MS = 80                # openWakeWord works on 80ms chunks
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000

# How long of a gap counts as "you've finished speaking". Long enough to survive a pause
# for breath mid-sentence, short enough not to feel like it's ignoring you.
SILENCE_HANGOVER_SEC = 1.1
MIN_UTTERANCE_SEC = 0.4      # anything shorter is a cough or a door
MAX_UTTERANCE_SEC = 20.0     # hard stop, so a noisy room can't record forever
# How long to wait for you to actually start talking after the wake word. Without this,
# the natural pause between "hey Jarvis" and the question counts as silence and the
# recording ends before you've said anything â€” observed exactly that on the first unit,
# which sent 1.2s of audio containing only the tail of the wake word. Reused as-is for
# open-mic mode: the "wait for you to actually start talking" problem is identical, just
# without a wake word to mark when the waiting began.
START_TIMEOUT_SEC = 6.0
# The wake word fires while you're still finishing the word "Jarvis", so the frames
# immediately after it are loud â€” counting those as "you've started speaking" arms the
# silence timer straight away and the recording ends during your breath. Ignore them.
WAKE_TAIL_SEC = 0.45
# And require a real amount of speech, not one loud frame, so a cough or a door doesn't
# arm it either.
MIN_SPEECH_SEC = 0.25
# Audio kept from *before* the wake word fires, so a request that runs straight into the
# wake word ("hey Jarvis what's the weather") doesn't lose its first syllable. Not used
# in open-mic mode -- there is no trigger moment to keep audio "before", recording just
# starts on the current frame.
PREROLL_SEC = 0.5
# How often to ask the server whether open-mic mode should be on. A camera-driven signal
# that's a few seconds stale is fine -- someone walking into or out of frame isn't a
# sub-second event -- and this keeps it to a cheap periodic GET rather than a socket.
OPEN_MIC_POLL_SEC = 4.0


class Config:
    def __init__(self, path: pathlib.Path):
        data = json.loads(path.read_text())
        self.server: str = data["server"].rstrip("/")
        self.device_id: str = data.get("device_id", "pi")
        self.device_name: str = data.get("device_name", self.device_id)
        self.api_key: str = data["api_key"]
        self.wake_model: str = data.get("wake_model", "hey_jarvis")
        self.wake_threshold: float = data.get("wake_threshold", 0.5)
        # "tflite" or "onnx". tflite is ~1.4x faster on ARM and is the difference
        # between fitting and not fitting the real-time budget on a Pi 3.
        self.inference_framework: str = data.get("inference_framework", "tflite")
        self.input_device = data.get("input_device")     # None = system default
        self.output_device = data.get("output_device")
        # Floor only. The real threshold is calibrated against the room â€” a fixed value
        # is wrong the moment the device moves. Measured ambient noise on the first unit
        # was already above the naive 0.012 default, which would have meant it never
        # decided you'd stopped speaking.
        self.vad_floor: float = data.get("vad_floor", 0.006)
        self.vad_margin: float = data.get("vad_margin", 2.5)
        self.chime: bool = data.get("chime", True)
        # Cross-terminal wake-word arbitration (server-side: assistant/core/
        # wake_arbitration.py) -- defaults on since the problem it prevents (two
        # terminals within earshot both chiming and answering the same "hey Jarvis") is
        # real, not hypothetical, so a device with no opinion in its config still gets
        # the fix.
        self.wake_arbitration_enabled: bool = data.get("wake_arbitration", True)
        # Filled in by resolve_audio_devices() once the hardware has been inspected.
        self.capture_rate: int = SAMPLE_RATE
        # Whether the system has any capture-capable device at all, named or default.
        # Distinct from input_device being None (which just means "use the default") --
        # a Pi with no microphone plugged in yet has no default to fall back to either,
        # and opening a stream against device=None still raises in that case.
        self.has_microphone: bool = True


def pick_capture_rate(device) -> int:
    """The highest-quality rate this microphone will actually open at.

    openWakeWord and Whisper both want 16kHz, and most USB microphones provide it. Some
    do not: the Dell AC511 soundbar's mic refuses it outright, and PortAudio surfaces
    that as "Invalid sample rate" when the stream opens, which previously killed the
    process on every restart. Where 16k is unavailable we capture at whatever the device
    does support and downsample in the callback -- the models never see the difference.
    """
    candidates = [SAMPLE_RATE, 48000, 44100, 32000, 22050]
    try:
        info = sd.query_devices(device, "input")
        default = int(info["default_samplerate"])
        if default not in candidates:
            candidates.insert(1, default)
    except Exception:
        pass
    for rate in candidates:
        try:
            sd.check_input_settings(device=device, samplerate=rate,
                                    channels=1, dtype="int16")
            return rate
        except Exception:
            continue
    return SAMPLE_RATE          # nothing worked; let the real error surface at open


def downsample_to_16k(block: np.ndarray, src_rate: int) -> np.ndarray:
    """Decimate a capture block to 16kHz.

    Linear interpolation, no anti-alias filter. For speech captured at 44.1/48k that is
    audibly fine and costs almost nothing, which matters in a callback that runs every
    80ms on a Pi.
    """
    if src_rate == SAMPLE_RATE:
        return block
    n_out = int(round(len(block) * SAMPLE_RATE / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.int16)
    x_old = np.arange(len(block), dtype=np.float64)
    x_new = np.linspace(0, len(block) - 1, n_out)
    return np.interp(x_new, x_old, block.astype(np.float64)).astype(np.int16)


def resolve_audio_devices(cfg) -> None:
    """Check the configured audio devices exist, and fall back if they do not.

    Terminals get their hardware moved, and a card cloned from another unit names that
    unit's devices. Previously a missing device raised out of the audio callback, systemd
    restarted the process, and it crash-looped forever -- with the screen frozen on
    whatever state it had last reported. Falling back to the system default keeps the
    terminal usable and puts the real problem in the log where it can be read.

    An output-only fallback is genuinely fine (the reply still appears as a caption). A
    missing *input* is fatal to the whole point, so that is logged as an error even
    though we continue -- the default may still be a working microphone.

    A system with NO input device at all (arecord -l shows nothing -- common on a
    freshly imaged Pi before its USB microphone is plugged in) is a different case from
    "the named device is missing": there is no default to fall back to either, and
    PortAudio raises opening a stream against device=None just the same as a bad name.
    That previously crashed on startup before the state report or heartbeat ever ran,
    so systemd tight-restart-looped a process that was never going to succeed on its
    own -- cfg.has_microphone lets main() skip straight to a clearly-reported waiting
    state instead.
    """
    try:
        devices = sd.query_devices()
    except Exception as e:
        log.error("cannot enumerate audio devices (%s) -- using system defaults", e)
        cfg.input_device = cfg.output_device = None
        return

    cfg.has_microphone = any(d["max_input_channels"] > 0 for d in devices)
    if not cfg.has_microphone:
        log.error("no capture-capable audio device found at all (arecord -l shows none) "
                  "-- plug in a USB microphone; skipping audio setup for now")
        cfg.input_device = cfg.output_device = None
        return

    def find(name, kind):
        if not name:
            return None, True                      # unset means "system default"
        key = "max_input_channels" if kind == "input" else "max_output_channels"
        for d in devices:
            if d[key] > 0 and str(name).lower() in d["name"].lower():
                return name, True
        return None, False

    ins = [d["name"] for d in devices if d["max_input_channels"] > 0]
    outs = [d["name"] for d in devices if d["max_output_channels"] > 0]

    value, ok = find(cfg.input_device, "input")
    if not ok:
        log.error("configured input_device %r is not present. Available inputs: %s. "
                  "Falling back to the system default.", cfg.input_device, ins or "none")
    cfg.input_device = value if ok else None

    value, ok = find(cfg.output_device, "output")
    if not ok:
        log.warning("configured output_device %r is not present. Available outputs: %s. "
                    "Falling back to the system default; replies will still be captioned.",
                    cfg.output_device, outs or "none")
    cfg.output_device = value if ok else None

    cfg.capture_rate = pick_capture_rate(cfg.input_device)
    if cfg.capture_rate != SAMPLE_RATE:
        log.info("microphone will not open at %dHz; capturing at %dHz and downsampling",
                 SAMPLE_RATE, cfg.capture_rate)
    log.info("audio: input=%r output=%r capture_rate=%d",
             cfg.input_device, cfg.output_device, cfg.capture_rate)


class Speaker:
    """Plays WAV bytes out of the configured output device.

    Not every speaker accepts every sample rate. The Dell AC511 soundbar on Touch1
    refuses 22050Hz outright — which is precisely what Piper produces — and PortAudio
    surfaces that as a bare "Invalid sample rate" at play time. Selecting a device by
    name opens the ALSA `hw:` node directly, so nothing resamples on our behalf.
    So: try the native rate, and on refusal resample to whatever the device does accept.
    """

    def __init__(self, output_device=None):
        self.output_device = output_device
        self._resample_to: float | None = None    # learned on first refusal, then reused

    def _device_rate(self) -> float:
        try:
            info = sd.query_devices(self.output_device, "output")
            return float(info["default_samplerate"])
        except Exception:
            return 48000.0

    @staticmethod
    def _resample(audio: np.ndarray, src: float, dst: float) -> np.ndarray:
        """Linear resample. Speech at these rates doesn't justify a filtered resampler,
        and this keeps the terminal free of a scipy dependency."""
        if src == dst:
            return audio
        n_out = int(round(len(audio) * dst / src))
        x_old = np.arange(len(audio), dtype=np.float64)
        x_new = np.linspace(0, len(audio) - 1, n_out)
        if audio.ndim == 1:
            return np.interp(x_new, x_old, audio).astype(np.int16)
        cols = [np.interp(x_new, x_old, audio[:, c]) for c in range(audio.shape[1])]
        return np.stack(cols, axis=1).astype(np.int16)

    def _play(self, audio: np.ndarray, rate: float) -> None:
        if self._resample_to and self._resample_to != rate:
            audio = self._resample(audio, rate, self._resample_to)
            rate = self._resample_to
        try:
            sd.play(audio, samplerate=rate, device=self.output_device, blocking=True)
        except sd.PortAudioError as e:
            if "sample rate" not in str(e).lower() or self._resample_to:
                raise
            target = self._device_rate()
            log.warning("%r rejected %.0fHz — resampling to %.0fHz from now on",
                        self.output_device, rate, target)
            self._resample_to = target
            sd.play(self._resample(audio, rate, target), samplerate=target,
                    device=self.output_device, blocking=True)

    def play_wav_bytes(self, data: bytes) -> None:
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
            channels = wav.getnchannels()
        audio = np.frombuffer(frames, dtype=np.int16)
        if channels > 1:
            audio = audio.reshape(-1, channels)
        self._play(audio, rate)

    def safe_chime(self, up: bool = True) -> None:
        """chime(), but never fatal.

        The chime fires immediately after reporting 'listening'. When the configured
        output device was missing, that raised, killed the process, and systemd restarted
        it -- leaving the screen showing 'listening' forever while the unit crash-looped.
        A terminal with no working speaker should still hear you and caption its answer.
        """
        try:
            self.chime(up)
        except Exception as e:
            log.warning("chime failed (%s: %s) -- continuing without it",
                        type(e).__name__, e)

    def chime(self, up: bool = True) -> None:
        """A short tone so you know it's listening without waiting for the screen."""
        # Synthesised at the device's own rate rather than a fixed 22050, since the
        # chime is the very first sound a terminal makes and must not be the thing
        # that fails on a speaker with a narrow set of supported rates.
        rate = int(self._resample_to or self._device_rate())
        duration = 0.11
        t = np.linspace(0, duration, int(rate * duration), endpoint=False)
        freqs = (660, 990) if up else (990, 660)
        tone = np.concatenate([
            np.sin(2 * np.pi * freqs[0] * t[: len(t) // 2]),
            np.sin(2 * np.pi * freqs[1] * t[len(t) // 2:]),
        ])
        # Fade the edges or it clicks on cheap speakers.
        fade = int(rate * 0.01)
        tone[:fade] *= np.linspace(0, 1, fade)
        tone[-fade:] *= np.linspace(1, 0, fade)
        self._play((tone * 0.25 * 32767).astype(np.int16), rate)


# The server treats a device as offline once its last report is 90s old. Re-sending the
# current state well inside that window is what keeps a terminal that is simply waiting
# for the wake word — its normal condition — from displaying itself as offline.
HEARTBEAT_SEC = 30.0

# Anything scoring at least this is worth reporting even though it didn't fire — it's
# the difference between "heard nothing" and "nearly heard you".
NEAR_MISS_FLOOR = 0.08
NEAR_MISS_EVERY = 20.0


class JarvisClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = httpx.Client(timeout=180.0, headers={"Authorization": f"Bearer {cfg.api_key}"})
        self._last: tuple[str, str] = ("idle", "")
        self._last_sent = 0.0
        self._lock = threading.Lock()

    def report(self, state: str, caption: str = "") -> None:
        """Tell the server what this device is doing, so the screen can show it.

        Never fatal: a terminal that can't update its display should still answer you.
        """
        with self._lock:
            self._last = (state, caption)
            self._last_sent = time.time()
        self._post(state, caption)

    def _post(self, state: str, caption: str) -> None:
        try:
            self.http.post(
                f"{self.cfg.server}/api/devices/{self.cfg.device_id}/state",
                json={"state": state, "caption": caption, "name": self.cfg.device_name},
                timeout=8.0,
            )
        except Exception as e:
            log.debug("state report failed: %s", e)

    def start_heartbeat(self) -> None:
        """Re-send the last reported state periodically, in the background.

        A daemon thread rather than work inside the audio callback: the callback runs
        every 80ms and must never block on the network, and an exchange can occupy the
        main loop for many seconds while the model thinks.
        """
        def beat():
            while True:
                time.sleep(5)
                with self._lock:
                    state, caption = self._last
                    due = time.time() - self._last_sent >= HEARTBEAT_SEC
                if due:
                    with self._lock:
                        self._last_sent = time.time()
                    self._post(state, caption)

        threading.Thread(target=beat, name="heartbeat", daemon=True).start()

    def turn(self, wav_bytes: bytes) -> dict:
        files = {"audio": ("utterance.wav", wav_bytes, "audio/wav")}
        resp = self.http.post(f"{self.cfg.server}/api/devices/{self.cfg.device_id}/turn", files=files)
        resp.raise_for_status()
        return resp.json()

    def wake_claim(self, score: float) -> dict:
        """Reports this terminal's wake-word confidence for the current moment and
        returns {"proceed": bool} -- False means a rival terminal clearly heard it better
        and this one should stand down (assistant/core/wake_arbitration.py). Raises on
        failure, same pattern as conversation_mode() above -- the caller decides how to
        treat that (see the wake-word branch in handle(), which fails open rather than
        silently muting the terminal over a network blip)."""
        resp = self.http.post(
            f"{self.cfg.server}/api/devices/{self.cfg.device_id}/wake_claim",
            json={"score": score}, timeout=8.0)
        resp.raise_for_status()
        return resp.json()

    def conversation_mode(self) -> dict:
        """Whether this device should be in open-mic mode right now, per the server's
        camera-driven presence/identity check (assistant/web/routes/vision.py). Raises on
        failure -- the poller loop below is what makes this non-fatal, same pattern as
        every other network call here having its own try/except at the call site rather
        than swallowing errors in the client itself."""
        resp = self.http.get(
            f"{self.cfg.server}/api/vision/conversation-mode/{self.cfg.device_id}", timeout=8.0)
        resp.raise_for_status()
        return resp.json()

    def wait_for_server(self) -> None:
        """A Pi boots faster than the laptop wakes; don't die because the server isn't up
        yet, just wait for it."""
        delay = 2
        while True:
            try:
                self.http.get(f"{self.cfg.server}/api/devices/{self.cfg.device_id}", timeout=8.0)
                log.info("server reachable at %s", self.cfg.server)
                return
            except Exception:
                log.warning("server not reachable at %s â€” retrying in %ss", self.cfg.server, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)


def to_wav(frames: list[np.ndarray]) -> bytes:
    audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(audio.astype(np.int16).tobytes())
    return buf.getvalue()


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean((frame.astype(np.float32) / 32768.0) ** 2)))


def calibrate_noise_floor(cfg: Config, seconds: float = 1.5) -> float:
    """Measures the room, and derives the silence threshold from it.

    A hardcoded threshold is wrong as soon as the device moves to a different room â€” or
    the fridge kicks in. Uses a low percentile rather than the mean so someone talking
    during calibration raises the floor far less than it otherwise would.
    """
    log.info("calibrating noise floor (%.1fs) ...", seconds)
    frames = int(seconds * 1000 / FRAME_MS)
    levels = []
    capture_block = int(FRAME_SAMPLES * cfg.capture_rate / SAMPLE_RATE)
    with sd.RawInputStream(samplerate=cfg.capture_rate, blocksize=capture_block,
                           dtype="int16", channels=1, device=cfg.input_device) as stream:
        for _ in range(frames):
            data, _overflow = stream.read(capture_block)
            block = downsample_to_16k(np.frombuffer(bytes(data), dtype=np.int16),
                                      cfg.capture_rate)
            levels.append(rms(block))
    ambient = float(np.percentile(levels, 25)) if levels else 0.0
    threshold = max(cfg.vad_floor, ambient * cfg.vad_margin)
    log.info("ambient %.4f -> silence threshold %.4f", ambient, threshold)
    return threshold


def start_open_mic_poller(client: JarvisClient, open_mic: threading.Event, open_mic_person: dict) -> None:
    """Background thread: keeps open_mic (and who triggered it) in sync with the server's
    camera-driven presence/identity check. Runs independently of the audio callback --
    the callback only ever reads open_mic.is_set(), never blocks on the network itself.
    """
    def poll():
        while True:
            try:
                result = client.conversation_mode()
                now_on = bool(result.get("open_mic"))
                if now_on and not open_mic.is_set():
                    log.info("open-mic mode ON — %s is in view, listening without a wake word",
                             result.get("person") or "a recognised person")
                elif not now_on and open_mic.is_set():
                    log.info("open-mic mode OFF — back to wake-word listening")
                open_mic_person["name"] = result.get("person") if now_on else None
                if now_on:
                    open_mic.set()
                else:
                    open_mic.clear()
            except Exception as e:
                # Never fatal, and never flips the mode on a transient failure -- a
                # dropped poll should leave the terminal exactly as it was, not silently
                # start listening without a wake word because a request timed out.
                log.debug("open-mic poll failed (%s); leaving mode unchanged", e)
            time.sleep(OPEN_MIC_POLL_SEC)

    threading.Thread(target=poll, name="open-mic-poll", daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis voice terminal")
    parser.add_argument("--config", default="device.json")
    parser.add_argument("--list-devices", action="store_true", help="show audio devices and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # httpx logs every request at INFO and its whole transport dance at DEBUG. On a device
    # that polls state several times per exchange that buries the lines that matter and
    # fills the journal for no benefit.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.list_devices:
        print(sd.query_devices())
        return 0

    cfg = Config(pathlib.Path(args.config))
    resolve_audio_devices(cfg)
    client = JarvisClient(cfg)
    speaker = Speaker(cfg.output_device)

    client.wait_for_server()
    client.start_heartbeat()

    # Open-mic state, shared between this poller and the audio callback below. A
    # threading.Event rather than a plain bool: .is_set()/.set()/.clear() are already
    # atomic, so the audio callback (running on PortAudio's own thread) never needs a
    # lock just to check it.
    open_mic = threading.Event()
    open_mic_person: dict = {"name": None}
    start_open_mic_poller(client, open_mic, open_mic_person)

    # No point loading a wake-word model or opening a capture stream against hardware
    # that doesn't exist yet. Report clearly and wait for a microphone to appear rather
    # than crash-loop under systemd forever.
    #
    # Waiting means exiting once one shows up, not looping resolve_audio_devices() in
    # place -- confirmed directly against a live process: PortAudio enumerates devices
    # once when it initialises (effectively on the first sounddevice call) and does not
    # notice a USB mic plugged in afterward, so a long-running process can poll forever
    # and never see it, while a *fresh* process picks it up immediately. `arecord -l` has
    # no such cache (it's a new process every call), so that's what's actually polled;
    # once it reports a card, this process exits and systemd's Restart=always hands the
    # job to a brand new process, which initialises PortAudio fresh and sees it correctly.
    if not cfg.has_microphone:
        client.report("no_microphone", "No microphone detected — plug one in.")
        log.warning("no microphone present; checking every 15s for one to appear")
        while True:
            time.sleep(15)
            try:
                found = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
                if "card " in found.stdout:
                    break
            except Exception as e:
                log.debug("arecord -l check failed (%s); still waiting", e)
        log.info("microphone detected — restarting to pick it up cleanly")
        client.report("idle", "Microphone detected, restarting...")
        return 0
        log.info("microphone detected — continuing startup")

    from openwakeword.model import Model as WakeModel

    # Framework choice is a real performance decision, not a detail. Measured on the
    # Pi 3 terminal, per 80ms frame (which must complete in under 80ms or frames drop
    # and wake words are missed):
    #     onnx   p95  91ms  -> 114% of budget, too slow
    #     tflite p95  64ms  ->  80% of budget, fits
    # A Pi 4 has headroom either way; a Pi 3 does not. Note tflite-runtime is built
    # against NumPy 1.x, so a terminal using it must pin numpy<2.
    log.info("loading wake word model %r (%s) ...", cfg.wake_model, cfg.inference_framework)
    try:
        wake = WakeModel(wakeword_models=[cfg.wake_model],
                         inference_framework=cfg.inference_framework)
    except Exception as e:
        # tflite-runtime is built against NumPy 1.x and raises obscurely on a NumPy 2
        # box. Falling back keeps a terminal talking instead of dead — a slower wake
        # word is a far better failure than none, and the log says which one is in use.
        fallback = "onnx" if cfg.inference_framework == "tflite" else "tflite"
        log.warning("%s backend unavailable (%s: %s) — falling back to %s",
                    cfg.inference_framework, type(e).__name__, str(e)[:120], fallback)
        wake = WakeModel(wakeword_models=[cfg.wake_model], inference_framework=fallback)
        log.info("wake word running on %s", fallback)

    silence_threshold = calibrate_noise_floor(cfg)

    client.report("idle")
    log.info("listening for the wake word on device %s", cfg.device_id)

    preroll = collections.deque(maxlen=int(PREROLL_SEC * 1000 / FRAME_MS))
    state = "wake"
    utterance: list[np.ndarray] = []
    silence_started: float | None = None
    started_at = 0.0
    heard_speech = False
    speech_frames = 0
    near_best = 0.0
    near_at = time.time()
    # Set when the current "record" state was entered via open-mic rather than a real
    # wake-word trigger -- read by the two finish() call sites below so open-mic doesn't
    # chime on every utterance of a continuous conversation.
    current_is_open_mic = False
    # Cross-terminal wake-word arbitration's outcome for the wake event currently being
    # recorded -- written by _on_wake's background thread (never the audio callback
    # thread itself, see the chime comment below for why), read by finish() once the
    # utterance is complete. True (the default) fails open: a network hiccup on the
    # arbitration call must never silently mute this terminal.
    wake_claim_result = {"proceed": True}

    def handle(frame: np.ndarray) -> None:
        nonlocal state, utterance, silence_started, started_at, heard_speech, speech_frames
        nonlocal near_best, near_at, current_is_open_mic

        if state == "wake":
            if open_mic.is_set():
                # Someone the house recognises is in view of this device's camera --
                # skip the wake word entirely and start listening now. No preroll: there
                # is no trigger moment to keep audio "before", the current frame is where
                # listening starts.
                log.info("open-mic: %s is here, listening without a wake word",
                         open_mic_person.get("name") or "someone recognised")
                wake.reset()
                preroll.clear()
                utterance = [frame]
                silence_started = None
                heard_speech = False
                speech_frames = 0
                started_at = time.time()
                current_is_open_mic = True
                state = "record"

                def _on_open_mic():
                    name = open_mic_person.get("name")
                    client.report("listening", f"Listening ({name})" if name else "Listening")
                threading.Thread(target=_on_open_mic, daemon=True).start()
                return

            preroll.append(frame)
            scores = wake.predict(frame)
            top = max(scores.values())
            # Near-miss logging. Without it, "the wake word doesn't work" is
            # indistinguishable from "the wake word is heard at 0.4 and the threshold is
            # 0.5" — one is a broken microphone, the other is one config line, and the
            # log looked identical in both cases: silent.
            if top >= NEAR_MISS_FLOOR:
                near_best = max(near_best, top)
            now = time.time()
            if near_best and now - near_at >= NEAR_MISS_EVERY:
                log.info("best wake score in the last %.0fs: %.3f (threshold %.2f)",
                         NEAR_MISS_EVERY, near_best, cfg.wake_threshold)
                near_best, near_at = 0.0, now
            elif not near_best and now - near_at >= NEAR_MISS_EVERY:
                near_at = now

            if top < cfg.wake_threshold:
                return
            log.info("wake word detected (%.2f)", top)
            # Clear the model's buffers or it re-triggers on the same audio next time.
            wake.reset()
            utterance = list(preroll)
            preroll.clear()
            silence_started = None
            heard_speech = False
            speech_frames = 0
            started_at = time.time()
            current_is_open_mic = False
            wake_claim_result["proceed"] = True
            state = "record"
            # client.report is a blocking HTTP call and safe_chime opens a second audio
            # stream -- both must run off PortAudio's own callback thread, which is what
            # is calling handle() right now. Confirmed directly: leaving the chime here
            # crashed the whole process on a laptop whose single shared audio codec
            # aborts with an ALSA assertion (PaAlsaStreamComponent_BeginPolling:
            # `ret == self->nfds`) the instant a second stream opens while this one's
            # callback is still executing. finish() below already gets this right for
            # the reply audio; only the wake chime hadn't been moved off-thread too.
            # Recording itself always starts immediately, unconditionally -- arbitration
            # (below, in this same background thread) only ever gates whether the
            # eventual utterance is actually sent once finish() runs, never whether
            # capture starts, so losing arbitration costs nothing but a discarded local
            # buffer, not a missed word of real audio.
            def _on_wake():
                score = top
                if cfg.wake_arbitration_enabled:
                    try:
                        result = client.wake_claim(score)
                        wake_claim_result["proceed"] = bool(result.get("proceed", True))
                    except Exception as e:
                        # Fails open -- a network blip on the arbitration call must never
                        # silently mute this terminal (same principle as every other
                        # optional check in this codebase).
                        log.debug("wake_claim failed, proceeding anyway: %s", e)
                        wake_claim_result["proceed"] = True
                if not wake_claim_result["proceed"]:
                    log.info("lost wake-word arbitration to another terminal -- staying quiet")
                    return
                client.report("listening")
                if cfg.chime:
                    speaker.safe_chime(up=True)
            threading.Thread(target=_on_wake, daemon=True).start()
            return

        # state == "record"
        utterance.append(frame)
        elapsed = time.time() - started_at
        loud = rms(frame) >= silence_threshold

        if loud:
            # Everything inside the wake-word tail is still the wake word itself. Not
            # applicable to an open-mic-triggered utterance (there's no wake word to tail
            # off), but harmless there too -- WAKE_TAIL_SEC just becomes a no-op window.
            if elapsed >= WAKE_TAIL_SEC:
                speech_frames += 1
                if speech_frames * FRAME_MS / 1000 >= MIN_SPEECH_SEC:
                    heard_speech = True
            silence_started = None
        else:
            speech_frames = 0
            if silence_started is None:
                silence_started = time.time()

        if not heard_speech:
            # Still waiting for you to begin. The gap between the wake word (or, in
            # open-mic mode, the moment listening started) and the question must not be
            # mistaken for the end of the utterance.
            if elapsed < START_TIMEOUT_SEC:
                return
            log.info("nobody spoke within %.0fs â€” going back to sleep", START_TIMEOUT_SEC)
            state = "busy"
            threading.Thread(target=finish, args=([], 0.0, current_is_open_mic), daemon=True).start()
            return

        done = (silence_started is not None and time.time() - silence_started >= SILENCE_HANGOVER_SEC) \
            or elapsed >= MAX_UTTERANCE_SEC
        if not done:
            return

        state = "busy"
        threading.Thread(target=finish, args=(list(utterance), elapsed, current_is_open_mic), daemon=True).start()

    def finish(frames: list[np.ndarray], elapsed: float, was_open_mic: bool = False) -> None:
        nonlocal state
        try:
            if elapsed < MIN_UTTERANCE_SEC:
                log.info("utterance too short (%.2fs) â€” ignoring", elapsed)
                client.report("idle")
                return
            # Lost wake-word arbitration to another terminal (open-mic never arbitrates --
            # there's no wake word to have won or lost). By now the arbitration call has
            # long since resolved: it started the moment the wake word fired, bounded by
            # the server's own wake_arbitration_window_ms (hundreds of ms), and finish()
            # only ever runs after a full utterance was spoken.
            if not was_open_mic and cfg.wake_arbitration_enabled and not wake_claim_result["proceed"]:
                client.report("idle")
                return
            # No chime for open-mic: a ding on every turn of a continuous conversation is
            # worse than the thing it's signalling. The wake-word path still gets one --
            # that's the deliberate "I heard you" cue for an explicit trigger.
            if cfg.chime and not was_open_mic:
                speaker.safe_chime(up=False)
            client.report("thinking")
            log.info("sending %.1fs of audio%s", elapsed, " (open-mic)" if was_open_mic else "")
            result = client.turn(to_wav(frames))

            if result.get("heard_nothing"):
                log.info("nothing intelligible â€” going back to sleep")
                client.report("idle")
                return
            log.info("heard: %s", result.get("transcript"))
            log.info("reply: %s", (result.get("reply") or "")[:160])

            audio_b64 = result.get("audio")
            if audio_b64:
                client.report("speaking", result.get("reply", ""))
                speaker.play_wav_bytes(base64.b64decode(audio_b64))
            client.report("idle")
        except Exception:
            log.exception("turn failed")
            client.report("idle")
        finally:
            # Drop anything captured while we were busy, so the reply's own audio and any
            # chatter during it can't be mistaken for the next wake word. state goes back
            # to "wake" either way -- if open_mic is still set, the very next frame there
            # re-enters listening immediately (see the top of handle()); if it's been
            # cleared (the recognised person left, or presence dropped), the terminal
            # falls straight back to plain wake-word gating with no extra step.
            wake.reset()
            preroll.clear()
            state = "wake"

    def callback(indata, _frames, _time, status):
        if status:
            log.debug("audio status: %s", status)
        if state == "busy":
            return
        handle(downsample_to_16k(np.frombuffer(bytes(indata), dtype=np.int16),
                                 cfg.capture_rate))

    with sd.RawInputStream(
        samplerate=cfg.capture_rate,
        blocksize=int(FRAME_SAMPLES * cfg.capture_rate / SAMPLE_RATE),
        dtype="int16", channels=1, device=cfg.input_device, callback=callback,
    ):
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("shutting down")
            client.report("offline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
