"""Jarvis VoIP worker -- Jarvis's real phone line (the JarvisVoip service).

WHY THIS EXISTS
    The jarvisaudio2 LTE SIM does SMS but its carrier BLOCKS voice (see core/cellular.py
    and device/jarvis_cellular.py). A SIP account (VoIP.ms) gives Jarvis an actual number
    it can receive voice calls on, over IP, with nothing riding the cellular voice path.
    SIP is IP-based, so this does NOT run on the modem Pi: cfg.voip_host = None means this
    Windows box, where faster-whisper (STT) and Piper (TTS) and the engine already live,
    so call audio bridges to them IN-PROCESS instead of making a second LAN hop.

CONFIRMED USE-CASE: CONVERSATIONAL INBOUND. The owner calls the DID (571-832-2742) and
    talks to Jarvis. The turn shape mirrors the voice terminals' /api/devices/{id}/turn
    (assistant/web/routes/devices.py): caller speech -> Whisper -> handle_message (with the
    SAME context dict the device/SMS paths build) -> Piper -> played back to the caller.
    It is half-duplex by turn, like the kiosks: Jarvis listens, endpoints on silence,
    thinks, then speaks; capture is muted while Jarvis is speaking (no echo of its own TTS).

THREADING MODEL -- this is the load-bearing design decision, learned the hard way.
    Native ML (Piper/onnxruntime, faster-whisper) CANNOT run on a thread that PJSUA2 owns
    or that has called ep.libRegisterThread -- doing so faults the process with a Windows
    access violation (observed live, one native library deeper each attempt: espeak's
    phonemizer, then onnxruntime.run). So the work is split across three thread kinds and
    each one's rule is absolute:
      1. The MAIN thread runs ep.libHandleEvents in a loop and owns EVERY pjsua2 call --
         account/register, answering, creating the capture port, and ALL playback
         (AudioMediaPlayer create/start/stop). SIP/timer/EOF callbacks dispatch here
         (uaConfig.threadCnt=0).
      2. The pjmedia clock thread delivers AudioMediaPort.onFrameReceived. That handler
         does ONE trivial thing -- put the PCM on a queue. No ML, no new pjsua2 objects.
      3. A PLAIN Python worker thread (NEVER libRegisterThread'd, NEVER touches pjsua2)
         does endpointing + STT + engine + TTS and pushes reply WAV bytes onto a queue.
    The main loop drains that reply queue and plays it. This mirrors JarvisWeb/JarvisRadio,
    where the identical Piper+Whisper run fine on plain daemon threads -- the ONLY thing
    that ever made them crash here was being on a pjsua2 thread, which this design forbids.

SAFETY / STATUS
    * The whole live path is gated behind cfg.voip_enabled. With it False (the default and
      the current setting) this process is a benign no-op that NEVER creates a SIP endpoint,
      NEVER registers, and NEVER answers a call. Do not flip voip_enabled or start this
      service to "see if it works" -- the live call test is a coordinated, attended step.
    * OUTBOUND is not built here and voip_calling_enabled is not consulted for dialing:
      this pass is inbound-only. Placing calls to humans stays a separate, gated feature.
    * Inbound authorization is cfg.voip_allowed_callers, enforced HERE and FAIL-CLOSED via
      core.cellular.is_allowed -- the exact same allow-list logic the SMS channel uses.
      An unlisted caller is rejected before any audio or model context is created.
    * PJSUA2 is imported LAZILY inside the live path only, so this module loads and the
      service installs even where PJSUA2 isn't built.
"""
from __future__ import annotations

import io
import logging
import queue
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from .config import Config, load_config
from .core import cellular
from .core.logging_setup import setup_logging

logger = logging.getLogger(__name__)

# ---- audio format the bridge works in -------------------------------------------------
# Whisper's native rate. We ask PJSIP's conference bridge for 16 kHz mono/16-bit frames;
# PJSIP resamples between this and whatever codec the call negotiated (PCMU is 8 kHz), so
# the STT side always sees the rate faster-whisper wants regardless of the codec on air.
SAMPLE_RATE = 16000
FRAME_MS = 20
BYTES_PER_SAMPLE = 2

# ---- turn endpointing -----------------------------------------------------------------
# Mirrors radio_main.py's proven burst segmentation, but a phone line is not squelched
# digital silence like the scanner -- there is always some room/line noise -- so the RMS
# gate is higher and MUST be tuned against a real call. Marked here as the first dial to
# adjust during the attended call test.
SPEECH_RMS = 500.0            # above this = the caller is talking (TUNE on a real call)
HANG_SECONDS = 1.0            # trailing silence that ends the caller's turn
MIN_SPEECH_SECONDS = 0.4      # shorter than this is a cough/click, not a turn
MAX_TURN_SECONDS = 30.0       # hard cap so a noisy line can't buffer forever
PLAYBACK_TIMEOUT_SECONDS = 60.0   # safety net if an EOF callback never arrives

IDLE_SLEEP_SECONDS = 300      # nap cadence while the service is a disabled no-op

GREETING = "Hi, this is Jarvis. How can I help?"


def _wav_bytes(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw mono 16-bit PCM as a WAV, the container both Whisper and the player want."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _rms(frame: bytes) -> float:
    if not frame:
        return 0.0
    a = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


# ======================================================================================
#  Conversational turn engine -- runs ENTIRELY on a plain worker thread
# ======================================================================================
class CallTurnEngine:
    """The STT -> engine -> TTS loop for one call. Runs on a plain Python thread that must
    NEVER call pjsua2 (see the module THREADING MODEL note -- native ML on a pjsua2 thread
    faults the process).

    Wiring:
      * feed(pcm)          -- called from the pjmedia capture callback; just enqueues.
      * emit_reply(wav)    -- a plain callback (a queue.put) the worker uses to hand a
                              finished reply WAV to the MAIN thread, which does the pjsua2
                              playback. emit_reply MUST NOT touch pjsua2.
      * set_speaking(bool) -- called by the MAIN thread around playback so the worker mutes
                              capture while Jarvis is talking (no echo of its own TTS).
    """

    def __init__(self, cfg: Config, llm, user_id: int, contexts: dict, stt, speaker,
                 emit_reply):
        self.cfg = cfg
        self.llm = llm
        self.user_id = user_id
        self.contexts = contexts
        self.stt = stt
        self.speaker = speaker
        self.emit_reply = emit_reply            # plain callback(wav_bytes); NO pjsua2
        self._in: queue.Queue = queue.Queue(maxsize=256)
        self._speaking = threading.Event()      # set == Jarvis talking, drop caller audio
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._loop, name="voip-turn", daemon=True)

    # -- called from the pjmedia capture callback (trivial, no ML/no pjsua2) --------------
    def feed(self, pcm: bytes) -> None:
        if self._stop.is_set() or self._speaking.is_set():
            return
        try:
            self._in.put_nowait(pcm)
        except queue.Full:
            pass                                # a dropped 20 ms frame is harmless

    # -- called from the MAIN thread's playback pump -------------------------------------
    def set_speaking(self, on: bool) -> None:
        if on:
            self._speaking.set()
        else:
            self._drain()                       # discard audio captured while we spoke
            self._speaking.clear()

    def _drain(self) -> None:
        while True:
            try:
                self._in.get_nowait()
            except queue.Empty:
                return

    def start(self) -> None:
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    # -- the worker (plain thread) -------------------------------------------------------
    def _loop(self) -> None:
        self._reply(GREETING)                   # greet as soon as media is up

        buf = bytearray()
        speech_seen = False
        silence_run = 0.0
        frame_secs = FRAME_MS / 1000.0

        while not self._stop.is_set():
            if self._speaking.is_set():
                # Jarvis is talking; abandon any half-built segment and wait it out.
                buf.clear()
                speech_seen = False
                silence_run = 0.0
                time.sleep(0.05)
                continue
            try:
                pcm = self._in.get(timeout=0.2)
            except queue.Empty:
                continue
            if _rms(pcm) >= SPEECH_RMS:
                speech_seen = True
                silence_run = 0.0
                buf.extend(pcm)
            elif speech_seen:
                buf.extend(pcm)                 # keep trailing silence inside the turn
                silence_run += frame_secs
            long_enough = len(buf) >= MIN_SPEECH_SECONDS * SAMPLE_RATE * BYTES_PER_SAMPLE
            too_long = len(buf) >= MAX_TURN_SECONDS * SAMPLE_RATE * BYTES_PER_SAMPLE
            if (speech_seen and silence_run >= HANG_SECONDS and long_enough) or too_long:
                segment = bytes(buf)
                buf.clear()
                speech_seen = False
                silence_run = 0.0
                self._handle_turn(segment)

    def _handle_turn(self, pcm: bytes) -> None:
        from .core.engine import handle_message
        try:
            transcript = self.stt.transcribe(_wav_bytes(pcm))
        except Exception:
            logger.exception("voip: transcription failed")
            return
        if not transcript.strip():
            return
        logger.info("voip caller said: %s", transcript[:160])
        try:
            # No source= : a phone call is a real interactive conversation (the owner is
            # actually there), so its turns belong in Jarvis's memory window for multi-turn
            # context -- exactly like the voice terminals in devices.py. A non-None source
            # would tag it as a background caller and keep it OUT of that window.
            reply = handle_message(
                self.cfg.db_path, self.llm, self.user_id, transcript,
                tz_name=self.cfg.timezone, **self.contexts)
        except Exception:
            logger.exception("voip: handle_message failed")
            reply = "Sorry, I hit an error handling that."
        if reply and reply.strip():
            logger.info("voip Jarvis reply: %s", reply[:160])
            self._reply(reply)

    def _reply(self, text: str) -> None:
        """Synthesise (Piper, on THIS plain thread) and hand the WAV to the main thread.

        Sets speaking BEFORE emitting so capture is muted the instant a reply exists, even
        before the main loop has picked it up to play. The main loop clears speaking again
        when playback finishes (on EOF)."""
        if not (self.speaker and self.speaker.available()):
            return
        try:
            wav = self.speaker.synthesize(text)
        except Exception:
            logger.exception("voip: TTS synthesis failed")
            return
        self._speaking.set()
        self.emit_reply(wav)


# ======================================================================================
#  Context assembly (mirrors web_main.main so a phone call has the full toolset)
# ======================================================================================
def build_runtime(cfg: Config):
    """Assemble llm + owner user id + the context dict + stt/speaker, exactly as the web
    process does, so a call reaches the same Jarvis the web UI and voice terminals do."""
    from .core import db
    from .core.stt import Transcriber
    from .core.tts import Speaker
    from .core.setup import (
        build_airbnb_context, build_business_context, build_calendar_context,
        build_ccxt_context, build_cellular_context, build_era_context,
        build_git_ops_context, build_gpu_bridge, build_home_assistant_context,
        build_kroger_context, build_letterstream_context, build_llm,
        build_local_llm_context, build_mail_context, build_obsidian_context,
        build_personal_context, build_phone_context, build_recipe_context,
        build_ticketmaster_context,
    )

    db.init_db(cfg.db_path)
    owner = next((u for u in cfg.users if u.role == "owner"), None)
    owner_row = db.get_user_by_chat_id(cfg.db_path, owner.telegram_chat_id) if owner else None
    owner_id = owner_row["id"] if owner_row else None

    llm = build_llm(cfg, owner_user_id=owner_id)
    bridge = build_gpu_bridge(cfg)
    letterstream = build_letterstream_context(cfg)
    kroger = build_kroger_context(cfg)
    contexts = {
        "era": build_era_context(cfg),
        "calendar": build_calendar_context(cfg),
        "phone": build_phone_context(cfg),
        "mail": build_mail_context(cfg),
        "obsidian": build_obsidian_context(cfg),
        "home_assistant": build_home_assistant_context(cfg),
        "business": build_business_context(cfg, owner_id, llm=llm, bridge=bridge),
        "personal": build_personal_context(cfg, owner_id, letterstream=letterstream, kroger=kroger),
        "airbnb": build_airbnb_context(cfg),
        "ticketmaster": build_ticketmaster_context(cfg),
        "kroger": kroger,
        "ccxt": build_ccxt_context(cfg),
        "letterstream": letterstream,
        "git_ops": build_git_ops_context(cfg),
        "recipe": build_recipe_context(cfg),
        "cellular_ctx": build_cellular_context(cfg),
        "local_llm": build_local_llm_context(cfg),
    }
    stt = Transcriber(model_size=cfg.stt_model_size)
    speaker = Speaker(voice_path=cfg.piper_voice_path)
    # Pre-warm both models with a REAL op on THIS (main) thread, before pjsua2 starts. Piper
    # lazily imports the NATIVE espeak phonemizer on first synthesize, and onnxruntime builds
    # its session on first run; forcing both here means the worker only ever REUSES modules
    # already imported and initialised (the sys.modules cache makes native create_module a
    # no-op on the worker). Combined with the worker being a plain, non-pjsua2 thread, this
    # is belt-and-braces against the access-violation crashes seen when ML first touched a
    # pjsua2 thread.
    try:
        speaker.synthesize("Voice interface ready.")
    except Exception:
        logger.exception("voip: TTS pre-warm failed")
    try:
        stt.transcribe(_wav_bytes(b"\x00\x00" * 1600))   # 0.1 s of silence
    except Exception:
        logger.exception("voip: STT pre-warm failed")
    return llm, owner_id, contexts, stt, speaker


# ======================================================================================
#  PJSUA2 glue -- ALL of this runs on the main / pjmedia threads, never the ML worker
# ======================================================================================
def _run_pjsua2(cfg: Config, llm, owner_id: int, contexts: dict, stt, speaker) -> None:
    """Bring up a PJSUA2 endpoint, register to VoIP.ms, answer allowed inbound calls, and
    pump playback -- all on this (main) thread. Reached only with voip_enabled True and
    pjsua2 importable."""
    import pjsua2 as pj

    ep = pj.Endpoint()
    ep.libCreate()
    ep_cfg = pj.EpConfig()
    ep_cfg.logConfig.level = 3
    # No PJSUA worker threads: with the default thread count, PJSIP's own threads fire SWIG
    # director callbacks into Python from threads the interpreter doesn't know about -> an
    # access violation the instant the first event dispatches. threadCnt=0 makes SIP/timer/
    # EOF callbacks run on THIS thread (the one pumping libHandleEvents). The ML worker is a
    # separate plain thread that never calls pjsua2 at all, so it needs no registration.
    ep_cfg.uaConfig.threadCnt = 0
    ep.libInit(ep_cfg)

    transport_map = {
        "udp": pj.PJSIP_TRANSPORT_UDP,
        "tcp": pj.PJSIP_TRANSPORT_TCP,
        "tls": pj.PJSIP_TRANSPORT_TLS,
    }
    tp_type = transport_map.get((cfg.sip_transport or "udp").lower(), pj.PJSIP_TRANSPORT_UDP)
    tcfg = pj.TransportConfig()
    tcfg.port = 0                       # ephemeral local port -- registration handles NAT
    ep.transportCreate(tp_type, tcfg)
    ep.libStart()

    # Headless server: drive the conference-bridge clock from a null device rather than a
    # physical sound card. This box is also a gaming/racing rig with several virtual audio
    # devices (Steam/Oculus) that a SIP bridge should never open or contend for; the null
    # device gives a steady clock and keeps all audio on the RTP<->port path.
    try:
        ep.audDevManager().setNullDev()
    except Exception:
        logger.exception("voip: setNullDev failed; falling back to the default sound device")

    # Prefer the codecs config asks for (PCMU/G722 on VoIP.ms), best-first.
    _prioritise_codecs(ep, cfg.sip_codecs)

    domain = cfg.sip_domain or cfg.sip_registrar
    tp_suffix = "" if tp_type == pj.PJSIP_TRANSPORT_UDP else f";transport={cfg.sip_transport.lower()}"

    acfg = pj.AccountConfig()
    acfg.idUri = f"sip:{cfg.sip_user}@{domain}"
    acfg.regConfig.registrarUri = f"sip:{cfg.sip_registrar}{tp_suffix}"
    if cfg.sip_proxy:
        acfg.sipConfig.proxies.append(f"sip:{cfg.sip_proxy}{tp_suffix}")
    # realm "*" matches whatever realm VoIP.ms challenges with (its POP domain).
    acfg.sipConfig.authCreds.append(
        pj.AuthCredInfo("digest", "*", cfg.sip_user, 0, cfg.sip_password))

    state = {"ep": ep, "cfg": cfg, "llm": llm, "owner_id": owner_id,
             "contexts": contexts, "stt": stt, "speaker": speaker, "calls": []}

    acc = _make_account(pj, state)
    acc.create(acfg)
    logger.warning("voip: registering to %s as %s (DID %s) -- LIVE",
                   cfg.sip_registrar, cfg.sip_user, cfg.sip_did)

    try:
        while True:
            ep.libHandleEvents(20)
            # Drain each call's reply queue and manage its player -- ALL pjsua2 playback
            # happens here on the main thread, never on the ML worker.
            for call in list(state["calls"]):
                try:
                    _service_playback(pj, call)
                except Exception:
                    logger.exception("voip: playback servicing error")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            ep.libDestroy()
        except Exception:
            pass


def _service_playback(pj, call) -> None:
    """Main-thread playback pump for one call: tear down a finished player, then start the
    next queued reply. No ML here; only pjsua2 calls, which is exactly what this thread is
    allowed to do."""
    if call.turn is None or call.play_q is None:
        return

    # 1) finished player -> stop, clean up, re-open the mic.
    if call.player is not None:
        done = call.player_done.is_set()
        overtime = (time.time() - call.player_started) > PLAYBACK_TIMEOUT_SECONDS
        if done or overtime:
            try:
                call.player.stopTransmit(call.call_audio)
            except Exception:
                pass
            call.player = None
            call.player_done = None
            if call.tmp is not None:
                try:
                    call.tmp.unlink()
                except OSError:
                    pass
                call.tmp = None
            call.turn.set_speaking(False)       # resume listening

    # 2) nothing playing -> start the next reply if one is waiting.
    if call.player is None:
        try:
            wav = call.play_q.get_nowait()
        except queue.Empty:
            return
        call.turn.set_speaking(True)            # mute capture for the duration
        tmp = Path(tempfile.gettempdir()) / f"jarvis_voip_{id(call)}_{int(time.time()*1000)}.wav"
        try:
            tmp.write_bytes(wav)
            done = threading.Event()
            player = _make_player(pj, done)
            player.createPlayer(str(tmp), pj.PJMEDIA_FILE_NO_LOOP)
            player.startTransmit(call.call_audio)   # player -> caller
            call.player = player
            call.player_done = done
            call.tmp = tmp
            call.player_started = time.time()
        except Exception:
            logger.exception("voip: failed to start playback")
            call.turn.set_speaking(False)
            try:
                tmp.unlink()
            except OSError:
                pass


def _make_player(pj, done_event):
    class _Player(pj.AudioMediaPlayer):
        def onEof2(self):
            # Fires on the main/libHandleEvents thread (threadCnt=0). Only signal; the
            # playback pump does the teardown so player lifecycle stays single-threaded.
            done_event.set()
    return _Player()


def _prioritise_codecs(ep, codecs: list[str]) -> None:
    # PJSIP codec ids look like "PCMU/8000/1", "G722/16000/1". Match by the name prefix
    # and assign descending priority; unknown names are skipped rather than fatal.
    try:
        available = [c.codecId for c in ep.codecEnum2()]
    except Exception:
        logger.exception("voip: could not enumerate codecs; leaving defaults")
        return
    pri = 255
    for want in (codecs or []):
        for cid in available:
            if cid.upper().startswith(want.upper() + "/"):
                try:
                    ep.codecSetPriority(cid, pri)
                    pri = max(1, pri - 20)
                except Exception:
                    pass


def _make_account(pj, state):
    class JarvisAccount(pj.Account):
        def onRegState(self, prm):
            logger.info("voip: registration state code=%s reason=%s",
                        getattr(prm, "code", "?"), getattr(prm, "reason", ""))

        def onIncomingCall(self, prm):
            call = _make_call(pj, self, prm.callId, state)
            ci = call.getInfo()
            caller = _digits_from_uri(ci.remoteUri)
            op = pj.CallOpParam()
            if not cellular.is_allowed(caller, state["cfg"].voip_allowed_callers):
                # Fail closed, exactly like the SMS allow-list: an unlisted caller never
                # reaches Jarvis. 603 Decline.
                logger.warning("voip: REJECTED inbound call from %r (not allow-listed)", caller)
                op.statusCode = pj.PJSIP_SC_DECLINE
                call.hangup(op)
                return
            logger.warning("voip: ANSWERING inbound call from %r", caller)
            op.statusCode = pj.PJSIP_SC_OK
            call.answer(op)
            state["calls"].append(call)

    return JarvisAccount()


def _make_call(pj, account, call_id, state):
    class JarvisCall(pj.Call):
        def __init__(self, acc, cid):
            super().__init__(acc, cid)
            self.turn: CallTurnEngine | None = None
            self.port = None                    # capture AudioMediaPort (kept alive here)
            self.call_audio = None
            self.play_q: queue.Queue | None = None
            self.player = None                  # active AudioMediaPlayer, or None
            self.player_done = None
            self.player_started = 0.0
            self.tmp = None

        def onCallState(self, prm):
            ci = self.getInfo()
            logger.info("voip: call state %s", ci.stateText)
            if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                if self.turn:
                    self.turn.stop()
                if self.player is not None:
                    try:
                        self.player.stopTransmit(self.call_audio)
                    except Exception:
                        pass
                    self.player = None
                if self.tmp is not None:
                    try:
                        self.tmp.unlink()
                    except OSError:
                        pass
                    self.tmp = None
                try:
                    state["calls"].remove(self)
                except ValueError:
                    pass

        def onCallMediaState(self, prm):
            ci = self.getInfo()
            for i, mi in enumerate(ci.media):
                if mi.type == pj.PJMEDIA_TYPE_AUDIO and \
                        mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    self._bridge(self.getAudioMedia(i))
                    break

        def _bridge(self, call_audio):
            # Runs on the main/pjmedia thread. Wire capture (caller -> queue) and stand up
            # the plain-thread turn engine; playback is handled by the main-loop pump, so
            # NOTHING here or downstream calls pjsua2 from the ML worker.
            self.call_audio = call_audio
            self.play_q = queue.Queue()

            # emit_reply is a bare queue.put -- a plain callable with no pjsua2 in it.
            self.turn = CallTurnEngine(
                state["cfg"], state["llm"], state["owner_id"], state["contexts"],
                state["stt"], state["speaker"], emit_reply=self.play_q.put)

            self.port = _make_capture_port(pj, self.turn.feed)
            fmt = pj.MediaFormatAudio()
            # L16 = linear 16-bit PCM, 16 kHz mono, 20 ms frames. init() sets format id +
            # media type correctly (verified against 2.15.1: init(formatId, clockRate,
            # channelCount, frameTimeUsec, bitsPerSample, ...)). PJSIP resamples between this
            # and the negotiated codec (PCMU is 8 kHz).
            fmt.init(pj.PJMEDIA_FORMAT_L16, SAMPLE_RATE, 1, FRAME_MS * 1000, 16)
            self.port.createPort("jarvis-capture", fmt)

            call_audio.startTransmit(self.port)   # caller -> our port (onFrameReceived)
            self.turn.start()                     # greets, then runs the STT/engine/TTS loop

    return JarvisCall(account, call_id)


def _make_capture_port(pj, on_pcm):
    class _CapturePort(pj.AudioMediaPort):
        def onFrameReceived(self, frame):
            # pjmedia clock thread. Do the ONE cheap thing: hand the PCM to the worker's
            # queue. No ML, no pjsua2 object creation -- both would be unsafe here.
            try:
                pcm = _frame_to_bytes(frame)
                if pcm:
                    on_pcm(pcm)
            except Exception:
                logger.exception("voip: capture frame error")

        def onFrameRequested(self, frame):
            # This port is receive-only (playback is a separate AudioMediaPlayer), but the
            # binding may still poll it -- return a typed empty frame rather than crash.
            frame.type = pj.PJMEDIA_FRAME_TYPE_NONE

    return _CapturePort()


def _frame_to_bytes(frame) -> bytes:
    """A received MediaFrame -> PCM bytes. On this 2.15.1 build frame.buf is a SWIG
    ByteVector that bytes() converts directly; slice to frame.size and skip empty/NONE
    frames (a silence frame carries size 0)."""
    size = int(getattr(frame, "size", 0) or 0)
    if size <= 0:
        return b""
    try:
        raw = bytes(frame.buf)
    except TypeError:
        raw = bytes(bytearray(frame.buf))
    return raw[:size] if len(raw) >= size else raw


def _digits_from_uri(uri: str) -> str:
    """Pull the number out of a SIP remote URI like '"Jack" <sip:12027408240@host>'."""
    import re
    m = re.search(r"sip:\+?([0-9]+)@", uri or "")
    return m.group(1) if m else ""


# ======================================================================================
#  Readiness helpers + entrypoint
# ======================================================================================
def missing_prereqs(cfg: Config) -> list[str]:
    missing: list[str] = []
    if not cfg.voip_enabled:
        missing.append("voip_enabled is false")
    if not cfg.sip_registrar:
        missing.append("sip_registrar")
    if not cfg.sip_user:
        missing.append("sip_user")
    if not cfg.sip_password:
        missing.append("sip_password")
    if not cfg.sip_did:
        missing.append("sip_did")
    if not cfg.voip_allowed_callers:
        missing.append("voip_allowed_callers (fail-closed: empty = nobody can call in)")
    return missing


def sip_library_available() -> tuple[bool, str]:
    import importlib.util
    try:
        if importlib.util.find_spec("pjsua2") is not None:
            return True, "pjsua2"
    except Exception:
        pass
    return False, "pjsua2 not installed (SWIG/PJSIP source build; no pip wheel exists)"


def _idle(reason: str) -> int:
    logger.warning("JarvisVoip idle no-op: %s. Not registering, not answering.", reason)
    try:
        while True:
            time.sleep(IDLE_SLEEP_SECONDS)
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    setup_logging("jarvis-voip")
    cfg = load_config()

    where = cfg.voip_host or "this box (local; co-located with STT/TTS/engine)"
    logger.info("JarvisVoip starting. SIP UA host target: %s", where)

    # Master gate. Off = never touch the SIP account. This is the switch that keeps the
    # live call test an attended, deliberate step.
    if not cfg.voip_enabled:
        return _idle("voip_enabled is false")

    gaps = [g for g in missing_prereqs(cfg) if not g.startswith("voip_enabled")]
    if gaps:
        return _idle("missing config: " + "; ".join(gaps))

    lib_ok, lib_note = sip_library_available()
    if not lib_ok:
        return _idle(lib_note)

    logger.info("voip: building runtime (llm, contexts, STT, TTS)...")
    llm, owner_id, contexts, stt, speaker = build_runtime(cfg)
    if owner_id is None:
        return _idle("no owner user configured")

    logger.warning("voip: entering LIVE SIP path (register + answer allowed inbound calls)")
    _run_pjsua2(cfg, llm, owner_id, contexts, stt, speaker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
