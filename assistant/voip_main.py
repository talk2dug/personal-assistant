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
    * PJSUA2 (PJSIP's Python binding) is the SIP engine. It is imported LAZILY inside the
      live path only, so this module loads and the service installs even on a box where
      PJSUA2 isn't built yet. As of writing PJSUA2 has no pip wheel on any platform (it is
      a SWIG/PJSIP source build); if that install is not completed, main() logs it and idles.

DESIGN NOTE -- the turn engine is deliberately SIP-library-agnostic. CallTurnEngine knows
    only "here are PCM frames from the caller" (feed) and "play this WAV to the caller"
    (a speak callback). All the PJSUA2-specific glue lives in the _pj_* section below, so
    if the SIP endpoint ever moves to baresip-on-a-Pi (the documented fallback), only that
    thin glue changes, not the STT/endpoint/engine/TTS core.
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
#  SIP-agnostic conversational turn engine
# ======================================================================================
class CallTurnEngine:
    """Runs the STT -> engine -> TTS loop for one call, decoupled from the SIP library.

    The SIP glue calls feed() with each inbound PCM frame (16 kHz mono 16-bit). A worker
    thread endpoints the caller's speech on silence, transcribes it, runs a full Jarvis
    turn, synthesises the reply, and hands the reply WAV to `speak`. While Jarvis is
    speaking, capture is muted (set_speaking) so its own voice is never transcribed.
    """

    def __init__(self, cfg: Config, llm, user_id: int, contexts: dict, stt, speaker,
                 speak, register_thread=None):
        self.cfg = cfg
        self.llm = llm
        self.user_id = user_id
        self.contexts = contexts
        self.stt = stt
        self.speaker = speaker
        self.speak = speak                      # callback: speak(wav_bytes) -> None
        self.register_thread = register_thread  # SIP libs need foreign threads registered
        self._frames: queue.Queue = queue.Queue(maxsize=256)
        self._speaking = threading.Event()      # set == Jarvis is talking, mute capture
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._loop, name="voip-turn", daemon=True)

    # -- called from the SIP media thread ------------------------------------------------
    def feed(self, pcm: bytes) -> None:
        if self._speaking.is_set() or self._stop.is_set():
            return                              # ignore our own playback / after hangup
        try:
            self._frames.put_nowait(pcm)
        except queue.Full:
            pass                                # a dropped 20 ms frame is harmless

    def set_speaking(self, on: bool) -> None:
        (self._speaking.set if on else self._speaking.clear)()

    def start(self) -> None:
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    # -- the worker ----------------------------------------------------------------------
    def _loop(self) -> None:
        if self.register_thread:
            try:
                self.register_thread("voip-turn")   # ep.libRegisterThread for pjsua2 calls
            except Exception:
                logger.exception("voip: could not register turn thread with the SIP lib")

        # Open with a greeting so the caller knows they're through.
        self._say(GREETING)

        buf = bytearray()
        speech_seen = False
        silence_run = 0.0
        hang = HANG_SECONDS
        frame_secs = FRAME_MS / 1000.0

        while not self._stop.is_set():
            try:
                pcm = self._frames.get(timeout=0.2)
            except queue.Empty:
                continue
            loud = _rms(pcm) >= SPEECH_RMS
            if loud:
                speech_seen = True
                silence_run = 0.0
                buf.extend(pcm)
            elif speech_seen:
                buf.extend(pcm)                 # keep trailing silence inside the turn
                silence_run += frame_secs
            spoke_long_enough = len(buf) >= MIN_SPEECH_SECONDS * SAMPLE_RATE * BYTES_PER_SAMPLE
            too_long = len(buf) >= MAX_TURN_SECONDS * SAMPLE_RATE * BYTES_PER_SAMPLE
            if (speech_seen and silence_run >= hang and spoke_long_enough) or too_long:
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
            reply = handle_message(
                self.cfg.db_path, self.llm, self.user_id, transcript,
                tz_name=self.cfg.timezone, source="voip", **self.contexts)
        except Exception:
            logger.exception("voip: handle_message failed")
            reply = "Sorry, I hit an error handling that."
        if reply and reply.strip():
            logger.info("voip Jarvis reply: %s", reply[:160])
            self._say(reply)

    def _say(self, text: str) -> None:
        if not (self.speaker and self.speaker.available()):
            return
        try:
            wav = self.speaker.synthesize(text)
        except Exception:
            logger.exception("voip: TTS synthesis failed")
            return
        self.set_speaking(True)
        try:
            self.speak(wav)                     # blocks until playback finishes
        except Exception:
            logger.exception("voip: playback failed")
        finally:
            # Drop anything captured during our own speech, then re-open the mic.
            while not self._frames.empty():
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    break
            self.set_speaking(False)


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
    return llm, owner_id, contexts, stt, speaker


# ======================================================================================
#  PJSUA2 glue -- the only SIP-library-specific code
# ======================================================================================
def _run_pjsua2(cfg: Config, llm, owner_id: int, contexts: dict, stt, speaker) -> None:
    """Bring up a PJSUA2 endpoint, register to VoIP.ms, and answer allowed inbound calls.

    Only ever reached with cfg.voip_enabled True AND pjsua2 importable. Everything the
    SWIG binding touches from a background thread is done after ep.libRegisterThread.
    """
    import pjsua2 as pj

    ep = pj.Endpoint()
    ep.libCreate()
    ep_cfg = pj.EpConfig()
    ep_cfg.logConfig.level = 3
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
             "contexts": contexts, "stt": stt, "speaker": speaker,
             "register_thread": ep.libRegisterThread, "calls": []}

    acc = _make_account(pj, state)
    acc.create(acfg)
    logger.warning("voip: registering to %s as %s (DID %s) -- LIVE",
                   cfg.sip_registrar, cfg.sip_user, cfg.sip_did)

    try:
        while True:
            ep.libHandleEvents(100)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            ep.libDestroy()
        except Exception:
            pass


def _prioritise_codecs(ep, codecs: list[str]) -> None:
    import pjsua2 as pj
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
    engine_holder = state

    class JarvisAccount(pj.Account):
        def onRegState(self, prm):
            logger.info("voip: registration state code=%s reason=%s",
                        getattr(prm, "code", "?"), getattr(prm, "reason", ""))

        def onIncomingCall(self, prm):
            call = _make_call(pj, self, prm.callId, engine_holder)
            ci = call.getInfo()
            caller = _digits_from_uri(ci.remoteUri)
            allowed = cellular.is_allowed(caller, engine_holder["cfg"].voip_allowed_callers)
            op = pj.CallOpParam()
            if not allowed:
                # Fail closed, exactly like the SMS allow-list: an unlisted caller never
                # reaches Jarvis. 603 Decline.
                logger.warning("voip: REJECTED inbound call from %s (not allow-listed)", caller)
                op.statusCode = pj.PJSIP_SC_DECLINE
                call.hangup(op)
                return
            logger.warning("voip: ANSWERING inbound call from %s", caller)
            op.statusCode = pj.PJSIP_SC_OK
            call.answer(op)
            engine_holder["calls"].append(call)

    return JarvisAccount()


def _make_call(pj, account, call_id, state):
    class JarvisCall(pj.Call):
        def __init__(self, acc, cid):
            super().__init__(acc, cid)
            self.turn: CallTurnEngine | None = None
            self.port = None
            self._players: list = []

        def onCallState(self, prm):
            ci = self.getInfo()
            logger.info("voip: call state %s", ci.stateText)
            if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                if self.turn:
                    self.turn.stop()
                try:
                    state["calls"].remove(self)
                except ValueError:
                    pass

        def onCallMediaState(self, prm):
            ci = self.getInfo()
            for i, mi in enumerate(ci.media):
                if mi.type == pj.PJMEDIA_TYPE_AUDIO and \
                        mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    call_audio = self.getAudioMedia(i)
                    self._bridge(call_audio)
                    break

        def _bridge(self, call_audio):
            # Capture caller audio into a custom port -> CallTurnEngine.feed.
            self.port = _make_capture_port(pj, lambda pcm: self.turn.feed(pcm) if self.turn else None)
            fmt = pj.MediaFormatAudio()
            fmt.type = pj.PJMEDIA_TYPE_AUDIO
            fmt.clockRate = SAMPLE_RATE
            fmt.channelCount = 1
            fmt.bitsPerSample = 16
            fmt.frameTimeUsec = FRAME_MS * 1000
            self.port.createPort("jarvis-capture", fmt)
            call_audio.startTransmit(self.port)      # caller -> our port (onFrameReceived)

            def speak(wav_bytes: bytes) -> None:
                # Playback via a file player -- robust and it lets PJSIP resample Piper's
                # rate to the call's. One player per utterance, torn down on EOF.
                self._play_wav(call_audio, wav_bytes)

            self.turn = CallTurnEngine(
                state["cfg"], state["llm"], state["owner_id"], state["contexts"],
                state["stt"], state["speaker"], speak,
                register_thread=state["register_thread"])
            self.turn.start()

        def _play_wav(self, call_audio, wav_bytes: bytes) -> None:
            tmp = Path(tempfile.gettempdir()) / f"jarvis_voip_{int(time.time()*1000)}.wav"
            tmp.write_bytes(wav_bytes)
            done = threading.Event()

            class _Player(pj.AudioMediaPlayer):
                def onEof2(self):
                    done.set()

            player = _Player()
            try:
                player.createPlayer(str(tmp), pj.PJMEDIA_FILE_NO_LOOP)
                player.startTransmit(call_audio)     # player -> caller
                # Wait out playback (onEof2 fires when the file ends); cap as a safety net.
                done.wait(timeout=MAX_TURN_SECONDS)
            except Exception:
                logger.exception("voip: player error")
            finally:
                try:
                    player.stopTransmit(call_audio)
                except Exception:
                    pass
                try:
                    tmp.unlink()
                except OSError:
                    pass

    return JarvisCall(account, call_id)


def _make_capture_port(pj, on_pcm):
    class _CapturePort(pj.AudioMediaPort):
        def onFrameReceived(self, frame):
            try:
                on_pcm(_frame_to_bytes(frame))
            except Exception:
                logger.exception("voip: capture frame error")

        def onFrameRequested(self, frame):
            # We never transmit FROM this port (playback is a separate player), but the
            # binding may still poll it -- hand back a typed empty frame rather than crash.
            frame.type = pj.PJMEDIA_FRAME_TYPE_NONE

    return _CapturePort()


def _frame_to_bytes(frame) -> bytes:
    """PJSUA2 exposes frame.buf as a SWIG ByteVector. Its exact Python surface varies by
    build, so convert defensively -- this is the one spot to verify on the first real call."""
    buf = frame.buf
    try:
        return bytes(buf)
    except TypeError:
        return bytes(bytearray(buf))


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

    gaps = missing_prereqs(cfg)
    # voip_enabled being in the list is expected-away here since we passed the gate.
    gaps = [g for g in gaps if not g.startswith("voip_enabled")]
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
