#!/usr/bin/env python3
"""Jarvis phone-line audio bridge (jarvisaudio1).

Connects an inbound VoIP.ms call (via baresip) to Jarvis's conversational /turn
endpoint on JarvisWeb, turn by turn:

    inbound call -> baresip auto-answers -> play greeting -> record caller until a
    short silence -> POST that audio to /api/devices/<id>/turn -> play the reply
    speech back to the caller -> loop until the caller hangs up.

Audio path (NEVER the physical sound card -- jarvis_device.py owns that):
    baresip <--ALSA snd-aloop loopback--> this orchestrator (arecord/aplay).
    baresip audio_player -> hw:Loopback,0,0   ==> we arecord hw:Loopback,1,0 (caller RX)
    baresip audio_source <- hw:Loopback,0,1   <== we aplay   hw:Loopback,1,1 (our reply TX)
Call media is 8 kHz mono s16 (PCMU/PCMA telephone). /turn wants 16 kHz mono WAV;
Piper replies come back as WAV at the voice's own rate. We resample with audioop.

Control: baresip ctrl_tcp (netstring-framed JSON events + commands) on 127.0.0.1:4444.

baresip is launched as a child of this process from a DEDICATED config dir
(~/voip-bridge/bare) so the stock ~/.baresip is left untouched; killing this
process kills baresip, so nothing is left auto-answering after the test.
"""
import atexit
import audioop
import base64
import io
import json
import logging
import os
import queue
import random
import signal
import socket
import subprocess
import sys
import threading
import time
import wave

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

# ---------------------------------------------------------------------------
# Config (env-overridable so the main session can tune without editing code)
# ---------------------------------------------------------------------------
SERVER      = os.environ.get("JARVIS_SERVER", "http://192.168.0.148:8080")
DEVICE_ID   = os.environ.get("JARVIS_DEVICE_ID", "phone1")
DEVICE_KEY  = os.environ.get("JARVIS_DEVICE_KEY", "")  # provide via env / start.sh -- never commit the key
# Orpheus natural-voice TTS service (on the Jarvis box). The bridge voices replies with
# this and falls back to the server's Piper audio if it errors or is down.
ORPHEUS_URL     = os.environ.get("ORPHEUS_URL", "http://192.168.0.148:8130/tts")
ORPHEUS_VOICE   = os.environ.get("ORPHEUS_VOICE", "dan")
ORPHEUS_TIMEOUT = float(os.environ.get("ORPHEUS_TIMEOUT", "30"))

HOME        = os.path.expanduser("~")
BARE_CFGDIR = os.environ.get("BARE_CFGDIR", os.path.join(HOME, "voip-bridge", "bare"))
CTRL_HOST   = "127.0.0.1"
CTRL_PORT   = int(os.environ.get("CTRL_PORT", "4444"))

# ALSA loopback endpoints owned by THIS process (baresip uses device 0; we use device 1)
REC_DEV     = os.environ.get("REC_DEV", "hw:Loopback,1,0")   # caller RX in
PLAY_DEV    = os.environ.get("PLAY_DEV", "hw:Loopback,1,1")  # our reply out

RATE        = 8000          # telephone rate on the loopback
FRAME_MS    = 20
FRAME_BYTES = int(RATE * (FRAME_MS / 1000.0)) * 2   # mono s16 -> 320 bytes @20ms

# Silence / end-of-utterance detection (RMS on 16-bit samples). Heavily logged so
# the live call can be used to retune these two numbers.
SPEECH_RMS      = int(os.environ.get("SPEECH_RMS", "350"))   # onset threshold
SILENCE_RMS     = int(os.environ.get("SILENCE_RMS", "250"))  # trailing-silence threshold
SILENCE_HANG_S  = float(os.environ.get("SILENCE_HANG_S", "1.5"))
ONSET_FRAMES    = int(os.environ.get("ONSET_FRAMES", "2"))
MAX_UTTER_S     = float(os.environ.get("MAX_UTTER_S", "15"))
NO_SPEECH_S     = float(os.environ.get("NO_SPEECH_S", "12"))  # give up a listen with no onset
PREROLL_FRAMES  = 4

GREETING_TEXT   = os.environ.get(
    "GREETING_TEXT",
    "Hi, this is Jarvis. How can I help you?")

# Spoken while /turn is thinking (real tool calls can take 10-25s) so the caller hears an
# acknowledgement instead of dead air. A POOL of varied, human phrasings, all pre-synthesised
# in Jarvis's voice at startup; one is picked at random each turn so it's never the same line
# twice running. JARVIS_FILLERS env ("|"-separated) overrides the pool.
FILLER_TEXTS    = [s.strip() for s in os.environ.get("JARVIS_FILLERS", "|".join([
    "One moment, sir.",
    "Hang tight a sec.",
    "Be right back.",
    "Looking into that now.",
    "Let me jump over there and take a look.",
    "Give me just a second.",
    "On it, one sec.",
    "Let me check that for you.",
    "Pulling that up now.",
    "Right, let me have a look.",
    "Bear with me a moment, sir.",
    "Let me dig into that.",
    "Checking now.",
    "Just a tick, sir.",
])).split("|") if s.strip()]

SILENCE_FRAME = b"\x00" * FRAME_BYTES


def _make_thinking_frames():
    """A soft, looping 'thinking' pulse the tx thread plays while /turn is working -- a
    gentle ~660 Hz blip every 0.5s at low volume, so after the 'one moment' filler the
    caller hears Jarvis quietly 'processing' rather than dead air. Pre-sliced into tx frames."""
    import math
    cycle_n = int(RATE * 0.5)      # one blip per half second
    tone_n = int(RATE * 0.06)      # 60ms blip
    amp = 2200                     # low volume (~0.07 of full scale)
    pcm = bytearray()
    for i in range(cycle_n):
        if i < tone_n:
            env = math.sin(math.pi * i / tone_n)          # smooth 0->1->0, no click
            s = int(amp * env * math.sin(2 * math.pi * 660 * i / RATE))
        else:
            s = 0
        pcm += int(s).to_bytes(2, "little", signed=True)
    if len(pcm) % FRAME_BYTES:
        pcm += b"\x00" * (FRAME_BYTES - len(pcm) % FRAME_BYTES)
    return [bytes(pcm[i:i + FRAME_BYTES]) for i in range(0, len(pcm), FRAME_BYTES)]


THINKING_FRAMES = _make_thinking_frames()

log = logging.getLogger("bridge")


def setup_logging():
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG)
    log.addHandler(sh)
    try:
        fh = logging.FileHandler(os.path.join(HOME, "voip-bridge", "bridge.log"))
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        log.addHandler(fh)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Audio format helpers
# ---------------------------------------------------------------------------
def wav_to_pcm8k(wav_bytes):
    """Any WAV (Piper's native rate, /say output) -> 8 kHz mono s16 raw PCM."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        ch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        data = w.readframes(w.getnframes())
    if sw != 2:
        data = audioop.lin2lin(data, sw, 2)
    if ch == 2:
        data = audioop.tomono(data, 2, 0.5, 0.5)
    if sr != RATE:
        data, _ = audioop.ratecv(data, 2, 1, sr, RATE, None)
    return data


def pcm8k_to_wav16k(pcm8k):
    """8 kHz mono s16 raw PCM (from arecord) -> 16 kHz mono WAV bytes for /turn."""
    up, _ = audioop.ratecv(pcm8k, 2, 1, RATE, 16000, None)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(up)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# JarvisWeb HTTP client
# ---------------------------------------------------------------------------
def _headers():
    return {"Authorization": f"Bearer {DEVICE_KEY}"}


def http_say(text):
    """POST /api/devices/say -> WAV bytes (Jarvis's Piper voice)."""
    r = requests.post(f"{SERVER}/api/devices/say", json={"text": text},
                      headers=_headers(), timeout=120)
    r.raise_for_status()
    return r.content


def http_turn(wav16_bytes):
    """POST /api/devices/<id>/turn -> dict(transcript, reply, audio(b64 WAV|None), ...)."""
    files = {"audio": ("utterance.wav", wav16_bytes, "audio/wav")}
    r = requests.post(f"{SERVER}/api/devices/{DEVICE_ID}/turn",
                      files=files, headers=_headers(), timeout=60)
    r.raise_for_status()
    return r.json()


def http_orpheus(text):
    """POST the reply text to the Orpheus TTS service -> natural-voice WAV bytes (24 kHz).
    Raises on any failure so the caller falls back to the server's Piper audio."""
    r = requests.post(ORPHEUS_URL, json={"text": text, "voice": ORPHEUS_VOICE},
                      timeout=ORPHEUS_TIMEOUT)
    r.raise_for_status()
    return r.content


# ---------------------------------------------------------------------------
# baresip ctrl_tcp (netstring-framed JSON)
# ---------------------------------------------------------------------------
class CtrlTcp:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sock = None
        self.buf = bytearray()
        self.on_event = None          # callback(event_dict)
        self._resp = queue.Queue()
        self._stop = False

    def connect(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.sock = socket.create_connection((self.host, self.port), timeout=3)
                self.sock.settimeout(1.0)
                log.info("ctrl_tcp connected %s:%s", self.host, self.port)
                threading.Thread(target=self._reader, daemon=True).start()
                return True
            except OSError as e:
                log.debug("ctrl_tcp not up yet (%s); retrying", e)
                time.sleep(0.5)
        return False

    def _read_netstring(self):
        while not self._stop:
            idx = self.buf.find(b":")
            if idx != -1:
                try:
                    length = int(self.buf[:idx])
                except ValueError:
                    self.buf.clear()
                    return None
                need = idx + 1 + length + 1
                if len(self.buf) >= need:
                    payload = bytes(self.buf[idx + 1: idx + 1 + length])
                    del self.buf[:need]
                    return payload
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                return None
            self.buf += data

    def _reader(self):
        while not self._stop:
            payload = self._read_netstring()
            if payload is None:
                if self._stop:
                    return
                time.sleep(0.2)
                continue
            try:
                msg = json.loads(payload.decode(errors="replace"))
            except Exception:
                continue
            if msg.get("event"):
                etype = msg.get("type", "")
                log.info("baresip event: %s %s", etype, msg.get("param", ""))
                if self.on_event:
                    try:
                        self.on_event(msg)
                    except Exception:
                        log.exception("on_event handler failed")
            elif "response" in msg:
                self._resp.put(msg)

    def command(self, cmd, params=""):
        obj = {"command": cmd, "params": params, "token": str(time.time())}
        s = json.dumps(obj)
        self.sock.sendall(f"{len(s)}:{s},".encode())
        try:
            return self._resp.get(timeout=5)
        except queue.Empty:
            return None

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Full-duplex loopback audio engine (arecord + aplay against snd-aloop)
# ---------------------------------------------------------------------------
class AudioEngine:
    """Owns the two long-lived ALSA processes for the duration of one call.

    - arecord streams the caller's decoded RX; a reader thread frames it and pushes
      (frame, rms) so listen() can run its VAD state machine in real time.
    - aplay is fed continuously by a writer thread: silence when idle (so baresip's
      capture on the other end of the loopback never x-runs), queued reply frames when
      speaking. play() blocks until the queued audio has drained.
    """
    def __init__(self):
        self.rec = None
        self.play = None
        self.cap_q = queue.Queue(maxsize=2000)
        self.tx_q = queue.Queue()
        self._stop = threading.Event()
        self._threads = []
        self.thinking = False   # when True, idle tx plays THINKING_FRAMES instead of silence
        self._think_idx = 0

    def start(self):
        self._stop.clear()
        self.rec = subprocess.Popen(
            ["arecord", "-D", REC_DEV, "-f", "S16_LE", "-r", str(RATE),
             "-c", "1", "-t", "raw", "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.play = subprocess.Popen(
            ["aplay", "-D", PLAY_DEV, "-f", "S16_LE", "-r", str(RATE),
             "-c", "1", "-t", "raw", "-q"],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self._threads = [
            threading.Thread(target=self._rx_loop, daemon=True),
            threading.Thread(target=self._tx_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()
        log.info("audio engine up (rec=%s play=%s)", REC_DEV, PLAY_DEV)

    def _rx_loop(self):
        while not self._stop.is_set():
            chunk = self.rec.stdout.read(FRAME_BYTES)
            if not chunk:
                break
            if len(chunk) < FRAME_BYTES:
                chunk = chunk + b"\x00" * (FRAME_BYTES - len(chunk))
            rms = audioop.rms(chunk, 2)
            try:
                self.cap_q.put_nowait((chunk, rms))
            except queue.Full:
                try:
                    self.cap_q.get_nowait()
                except queue.Empty:
                    pass

    def _tx_loop(self):
        while not self._stop.is_set():
            try:
                frame = self.tx_q.get(timeout=0.005)
            except queue.Empty:
                if self.thinking and THINKING_FRAMES:
                    frame = THINKING_FRAMES[self._think_idx % len(THINKING_FRAMES)]
                    self._think_idx += 1
                else:
                    frame = SILENCE_FRAME
            try:
                self.play.stdin.write(frame)
                self.play.stdin.flush()
            except Exception:
                break

    def drain_capture(self):
        while True:
            try:
                self.cap_q.get_nowait()
            except queue.Empty:
                return

    def enqueue_pcm(self, pcm8k):
        """Queue PCM for playback WITHOUT blocking, so the tx thread plays it while we do
        other work -- used for a filler that overlaps the /turn think time."""
        for i in range(0, len(pcm8k), FRAME_BYTES):
            self.tx_q.put(pcm8k[i:i + FRAME_BYTES])

    def play_pcm(self, pcm8k):
        """Enqueue 8k mono s16 PCM and block until it has been handed to aplay."""
        secs = len(pcm8k) / (RATE * 2.0)
        log.info("playing %d bytes (%.1fs) to caller", len(pcm8k), secs)
        for i in range(0, len(pcm8k), FRAME_BYTES):
            self.tx_q.put(pcm8k[i:i + FRAME_BYTES])
        while not self.tx_q.empty() and not self._stop.is_set():
            time.sleep(0.02)
        time.sleep(0.35)   # let aplay's own buffer drain
        log.info("playback done")

    def listen(self):
        """Return caller PCM (8k mono s16) from onset to trailing silence, or None."""
        self.drain_capture()
        preroll = []
        frames = []
        started = False
        speech_run = 0
        silent_run = 0
        hang_frames = int(SILENCE_HANG_S / (FRAME_MS / 1000.0))
        max_frames = int(MAX_UTTER_S / (FRAME_MS / 1000.0))
        t0 = time.time()
        last_log = 0
        log.info("listening (speech_rms>=%d, silence_rms<%d, hang=%.1fs)",
                 SPEECH_RMS, SILENCE_RMS, SILENCE_HANG_S)
        while not self._stop.is_set():
            try:
                frame, rms = self.cap_q.get(timeout=1.0)
            except queue.Empty:
                if not started and (time.time() - t0) > NO_SPEECH_S:
                    log.info("no speech within %.0fs -> giving up this listen", NO_SPEECH_S)
                    return None
                continue
            now = time.time()
            if now - last_log > 0.5:
                log.debug("rms=%d started=%s frames=%d", rms, started, len(frames))
                last_log = now
            if not started:
                preroll.append(frame)
                if len(preroll) > PREROLL_FRAMES:
                    preroll.pop(0)
                if rms >= SPEECH_RMS:
                    speech_run += 1
                    if speech_run >= ONSET_FRAMES:
                        started = True
                        frames.extend(preroll)
                        log.info("speech onset (rms=%d)", rms)
                else:
                    speech_run = 0
                    if (now - t0) > NO_SPEECH_S:
                        log.info("no speech within %.0fs -> giving up this listen", NO_SPEECH_S)
                        return None
            else:
                frames.append(frame)
                if rms < SILENCE_RMS:
                    silent_run += 1
                else:
                    silent_run = 0
                if silent_run >= hang_frames:
                    log.info("end of speech: %d frames (%.1fs) then %.1fs silence",
                             len(frames), len(frames) * FRAME_MS / 1000.0, SILENCE_HANG_S)
                    break
                if len(frames) >= max_frames:
                    log.info("max utterance %.0fs reached", MAX_UTTER_S)
                    break
        if not frames:
            return None
        return b"".join(frames)

    def stop(self):
        self._stop.set()
        for proc, name in ((self.rec, "arecord"), (self.play, "aplay")):
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self.rec = self.play = None
        log.info("audio engine stopped")


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------
class Bridge:
    def __init__(self, launch_baresip=True):
        self.launch_baresip = launch_baresip
        self.baresip = None
        self.ctrl = CtrlTcp(CTRL_HOST, CTRL_PORT)
        self.ctrl.on_event = self._on_event
        self.engine = None
        self.call_active = threading.Event()
        self.call_id = None
        self.greeting_pcm = None
        self.filler_pcms = []
        self._shutdown = False

    # -- baresip lifecycle --
    def start_baresip(self):
        if not self.launch_baresip:
            log.info("not launching baresip (assuming it is already running)")
            return
        log.info("launching baresip -f %s", BARE_CFGDIR)
        self.baresip = subprocess.Popen(
            ["baresip", "-f", BARE_CFGDIR],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=self._baresip_log, daemon=True).start()

    def _baresip_log(self):
        for line in iter(self.baresip.stdout.readline, b""):
            log.info("baresip| %s", line.decode(errors="replace").rstrip())

    def stop_baresip(self):
        if self.baresip:
            log.info("stopping baresip")
            try:
                self.baresip.terminate()
                self.baresip.wait(timeout=3)
            except Exception:
                try:
                    self.baresip.kill()
                except Exception:
                    pass
            self.baresip = None

    # -- ctrl events --
    def _on_event(self, ev):
        etype = ev.get("type", "")
        if etype == "CALL_ESTABLISHED":
            self.call_id = ev.get("id")
            self.call_active.set()
            log.info(">>> CALL_ESTABLISHED id=%s peer=%s", self.call_id, ev.get("peeruri"))
        elif etype in ("CALL_CLOSED", "CALL_TERMINATED"):
            log.info("<<< %s id=%s", etype, ev.get("id"))
            self.call_active.clear()
            # Stop the thinking pulse the instant a call ends, even if converse() is still
            # blocked in an in-flight /turn -- otherwise a call-back inherits the beeping.
            eng = self.engine
            if eng is not None:
                eng.thinking = False
        elif etype == "CALL_INCOMING":
            log.info("CALL_INCOMING from %s (answermode=auto should accept)", ev.get("peeruri"))

    # -- greeting --
    def prepare_greeting(self):
        # Pre-synthesise the greeting + "one moment" filler ONCE at startup so they play
        # instantly on a call. Prefer Orpheus (the same natural voice as the replies); fall
        # back to Piper /say per line if Orpheus is down.
        def _synth(text):
            try:
                return wav_to_pcm8k(http_orpheus(text)), "orpheus"
            except Exception as e:
                log.warning("orpheus greeting synth failed (%s); using piper /say", e)
                return wav_to_pcm8k(http_say(text)), "piper"
        try:
            self.greeting_pcm, how = _synth(GREETING_TEXT)
            log.info("greeting ready (%d bytes pcm8k) via %s", len(self.greeting_pcm), how)
            self.filler_pcms = []
            for phrase in FILLER_TEXTS:
                try:
                    pcm, _ = _synth(phrase)
                    self.filler_pcms.append(pcm)
                except Exception as e:
                    log.warning("filler synth failed for %r (%s); skipping", phrase, e)
            log.info("fillers ready: %d phrases", len(self.filler_pcms))
        except Exception as e:
            log.warning("could not synthesise greeting (%s); using a short tone", e)
            # 0.4s 440Hz beep as an audible fallback so the caller hears *something*
            import math
            self.greeting_pcm = b"".join(
                int(8000 * math.sin(2 * math.pi * 440 * i / RATE)).to_bytes(2, "little", signed=True)
                for i in range(int(RATE * 0.4)))

    # -- one conversation --
    def converse(self):
        self.engine = AudioEngine()
        time.sleep(0.4)   # let baresip open its side of the loopback first
        self.engine.start()
        my_call_id = self.call_id   # so we can tell if THIS call ends or a new one replaces it
        if self.greeting_pcm:
            self.engine.play_pcm(self.greeting_pcm)
        idle_listens = 0
        while self.call_active.is_set() and not self._shutdown:
            audio = self.engine.listen()
            if not self.call_active.is_set():
                break
            if audio is None:
                idle_listens += 1
                if idle_listens >= 3:
                    log.info("3 empty listens; still waiting for the caller")
                    idle_listens = 0
                continue
            idle_listens = 0
            try:
                wav16 = pcm8k_to_wav16k(audio)
                # Cover the /turn think time so the caller isn't in dead silence: queue a
                # short "one moment" that plays via the tx thread WHILE we POST, not before.
                if self.filler_pcms:
                    self.engine.enqueue_pcm(random.choice(self.filler_pcms))
                # After the filler drains, keep the line alive with a soft thinking pulse
                # until the reply is ready (real tool calls can run 10-25s).
                self.engine._think_idx = 0
                self.engine.thinking = True
                log.info("POSTing %d bytes (16k WAV) to /turn", len(wav16))
                t0 = time.time()
                resp = http_turn(wav16)
                log.info("/turn %.1fs transcript=%r reply=%r audio=%s",
                         time.time() - t0, (resp.get("transcript") or "")[:120],
                         (resp.get("reply") or "")[:200],
                         "yes" if resp.get("audio") else "none")
            except Exception:
                self.engine.thinking = False
                log.exception("/turn failed; continuing")
                continue
            if (not self.call_active.is_set()) or (self.call_id != my_call_id):
                self.engine.thinking = False
                log.info("call ended or changed during the think; abandoning this turn")
                break
            if resp.get("heard_nothing"):
                self.engine.thinking = False
                log.info("server heard nothing; re-listening")
                continue
            # Voice the reply. Prefer Orpheus (natural voice); the thinking pulse keeps
            # playing through the synth so there's no dead air, then we play the result.
            # Fall back to the server's Piper audio, then /say, if Orpheus errors or is down.
            reply_text = resp.get("reply") or ""
            pcm = None
            if reply_text:
                try:
                    pcm = wav_to_pcm8k(http_orpheus(reply_text))
                    log.info("orpheus voiced reply (%d chars -> %d pcm bytes)", len(reply_text), len(pcm))
                except Exception as e:
                    log.warning("orpheus TTS failed (%s); falling back to piper", e)
            if pcm is None and resp.get("audio"):
                try:
                    pcm = wav_to_pcm8k(base64.b64decode(resp["audio"]))
                except Exception:
                    log.exception("piper fallback decode failed")
            if pcm is None and reply_text:
                try:
                    pcm = wav_to_pcm8k(http_say(reply_text))
                except Exception:
                    log.exception("/say fallback failed")
            self.engine.thinking = False
            if pcm:
                self.engine.play_pcm(pcm)
        self.engine.stop()
        self.engine = None
        log.info("conversation ended")

    # -- main loop --
    def run(self):
        self.prepare_greeting()
        self.start_baresip()
        if not self.ctrl.connect():
            log.error("could not connect to ctrl_tcp; aborting")
            return
        reg = self.ctrl.command("reginfo")
        if reg:
            log.info("reginfo: %s", (reg.get("data") or "").replace("\n", " ").strip())
        log.info("bridge ready; waiting for inbound calls (DID 571-832-2742). Ctrl-C to stop.")
        while not self._shutdown:
            if self.call_active.wait(timeout=1.0):
                try:
                    self.converse()
                except Exception:
                    log.exception("converse crashed")
                    if self.engine:
                        self.engine.stop()
                        self.engine = None

    def shutdown(self, *_):
        log.info("shutting down")
        self._shutdown = True
        self.call_active.clear()
        if self.engine:
            self.engine.stop()
        self.ctrl.close()
        self.stop_baresip()


def main():
    setup_logging()
    if requests is None:
        log.error("python3-requests not available")
        sys.exit(2)
    launch = "--no-baresip" not in sys.argv
    b = Bridge(launch_baresip=launch)
    atexit.register(b.stop_baresip)
    signal.signal(signal.SIGINT, b.shutdown)
    signal.signal(signal.SIGTERM, b.shutdown)
    try:
        b.run()
    finally:
        b.shutdown()


if __name__ == "__main__":
    main()
