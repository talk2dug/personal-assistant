"""Jarvis radio worker -- the process that listens.

Pulls the two Icecast mounts off jarvishackrf2, transcribes them with the same
faster-whisper model the voice pipeline uses, and fills core/radio.py's tables:

  weather  -> continuous 30 s windows -> radio_transcripts; every few minutes the local
              LLM (simrig) extracts the spoken current conditions -> weather_conditions,
              and any hazard it mentions becomes a radio_items notice.
  scanner  -> the stream is real-time silence when nothing is keyed (fm_node/pace.py),
              so a transmission is simply a run of non-silent frames; each one is
              transcribed and the local LLM decides whether the owner would care.

It also polls the two Pis over SSH, with fixed commands only: the EAS decoder's event
file on jarvishackrf2 (deterministic SAME warnings -- the real severe-weather signal) and
the RF baseline report on jarvishackrf (which vehicle keeps coming back).

Its own process, deliberately (deploy/windows_service.py --variant radio, JarvisRadio):
faster-whisper is loaded here and in JarvisWeb, never in JarvisCore, and a stuck ffmpeg
or a slow model must not stall the chat process or the scheduler. It NEVER notifies:
JarvisCore's scheduler delivers radio_items through the single notify funnel.

Shares jarvis.db over WAL exactly like the vision worker.
"""
from __future__ import annotations

import io
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import wave
from datetime import datetime, timedelta, timezone
from hashlib import sha1
from pathlib import Path

import numpy as np

from .config import load_config
from .core import db, radio
from .core.logging_setup import setup_logging

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _service_profile_fixups() -> None:
    r"""Make the worker independent of which account the service runs as.

    pywin32 installs a service as LocalSystem, whose profile is
    C:\WINDOWS\system32\config\systemprofile: `~/.ssh/simrig_ed25519` in
    ssh_hosts then expands to a path that does not exist (found live -- every SSH poll
    failed), and huggingface re-downloads the whisper model into that profile. The repo
    lives inside the real user's profile, so derive the home from there and point
    HF_HOME at that user's cache. Must run before faster_whisper is imported: the hub
    reads HF_HOME at import. Reading those files is fine for LocalSystem; the cleaner
    fix is still `sc config JarvisRadio obj= .\swayze` the way Core/Web were done.
    """
    profile = os.environ.get("USERPROFILE") or ""
    if "systemprofile" not in profile.lower() and Path(profile).exists():
        return
    home = REPO_ROOT.parents[1]          # <profile>/Documents/PersonalAssistant -> <profile>
    if not (home / ".ssh").exists():
        return
    os.environ["USERPROFILE"] = str(home)
    os.environ.setdefault("HF_HOME", str(home / ".cache" / "huggingface"))
    logger.info("service profile fix: HOME -> %s (was %r)", home, profile)


def _resolve_key_paths(hosts: dict) -> dict:
    """Expand ~ in ssh_hosts key paths against the corrected profile."""
    out = {}
    for name, h in (hosts or {}).items():
        h = dict(h)
        kp = h.get("key_path")
        if kp:
            cand = Path(os.path.expanduser(kp))
            if not cand.exists():
                alt = REPO_ROOT.parents[1] / kp.replace("~/", "").replace("~\\", "")
                if alt.exists():
                    cand = alt
            h["key_path"] = str(cand)
        out[name] = h
    return out

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2

# Scanner segmentation. The pacer emits exact digital zeros when squelched, and the RTL's
# open-squelch floor is far above this, so the threshold only has to separate "nothing"
# from "anything". ~ -46 dBFS.
SPEECH_RMS = 150.0
HANG_SECONDS = 1.5          # gap that ends a transmission
MIN_SEGMENT_SECONDS = 0.8   # shorter than this is a squelch tail, not speech
MAX_SEGMENT_SECONDS = 45.0  # flush a long exchange in pieces

WEATHER_WINDOW_SECONDS = 30
HEARTBEAT_SECONDS = 10
RESPAWN_SECONDS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"


def _wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class StreamListener(threading.Thread):
    """ffmpeg -> 16 kHz mono PCM -> segments handed to `on_segment(stream, pcm, start, end)`."""

    def __init__(self, stream: str, url: str, db_path: str, on_segment, mode: str):
        super().__init__(name=f"listen-{stream}", daemon=True)
        self.stream, self.url, self.db_path, self.on_segment, self.mode = stream, url, db_path, on_segment, mode
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self._listen_once()
            except Exception:
                logger.exception("%s listener crashed", self.stream)
            if not self.stop.is_set():
                time.sleep(RESPAWN_SECONDS)

    def _listen_once(self) -> None:
        cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
               "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
               "-i", self.url, "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-"]
        logger.info("%s: opening %s", self.stream, self.url)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        last_beat = 0.0
        buf = bytearray()
        seg_start: datetime | None = None
        quiet_frames = 0
        hang_frames = int(HANG_SECONDS * 1000 / FRAME_MS)
        try:
            while not self.stop.is_set():
                frame = proc.stdout.read(FRAME_BYTES)
                if not frame or len(frame) < FRAME_BYTES:
                    break
                now = time.time()
                if now - last_beat >= HEARTBEAT_SECONDS:
                    radio.set_state(self.db_path, f"audio:{self.stream}", "ok")
                    last_beat = now

                if self.mode == "continuous":
                    if seg_start is None:
                        seg_start = _now()
                    buf.extend(frame)
                    if len(buf) >= WEATHER_WINDOW_SECONDS * SAMPLE_RATE * 2:
                        self._emit(bytes(buf), seg_start)
                        buf.clear()
                        seg_start = None
                    continue

                # bursts (scanner)
                rms = float(np.sqrt(np.mean(np.frombuffer(frame, dtype=np.int16).astype(np.float32) ** 2)))
                if rms >= SPEECH_RMS:
                    if seg_start is None:
                        seg_start = _now()
                    buf.extend(frame)
                    quiet_frames = 0
                    if len(buf) >= MAX_SEGMENT_SECONDS * SAMPLE_RATE * 2:
                        self._emit(bytes(buf), seg_start)
                        buf.clear()
                        seg_start = _now()
                elif seg_start is not None:
                    buf.extend(frame)
                    quiet_frames += 1
                    if quiet_frames >= hang_frames:
                        self._emit(bytes(buf), seg_start)
                        buf.clear()
                        seg_start = None
                        quiet_frames = 0
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            logger.warning("%s: stream ended (ffmpeg exit %s)", self.stream, proc.poll())

    def _emit(self, pcm: bytes, started: datetime) -> None:
        seconds = len(pcm) / (SAMPLE_RATE * 2)
        if self.mode == "bursts" and seconds < MIN_SEGMENT_SECONDS + HANG_SECONDS:
            return
        self.on_segment(self.stream, pcm, started, started + timedelta(seconds=seconds))


class Worker:
    def __init__(self, cfg, llm):
        self.cfg = cfg
        self.db_path = cfg.db_path
        self.llm = llm
        from .core.stt import Transcriber    # after _service_profile_fixups (HF_HOME)
        self.transcriber = Transcriber(model_size=cfg.stt_model_size)
        self.segments: queue.Queue = queue.Queue(maxsize=64)
        self.stop = threading.Event()
        self.home_area = cfg.radio_home_area or ""

    # -- audio --------------------------------------------------------------------
    def on_segment(self, stream: str, pcm: bytes, started: datetime, ended: datetime) -> None:
        try:
            self.segments.put_nowait((stream, pcm, started, ended))
        except queue.Full:
            logger.warning("transcription queue full; dropping a %s segment", stream)

    def transcribe_loop(self) -> None:
        """One thread owns the whisper model -- Transcriber's lazy load is not thread-safe."""
        while not self.stop.is_set():
            try:
                stream, pcm, started, ended = self.segments.get(timeout=1)
            except queue.Empty:
                continue
            try:
                t0 = time.time()
                text = self.transcriber.transcribe(_wav_bytes(pcm))
                dt = time.time() - t0
                if not text:
                    continue
                radio.record_transcript(self.db_path, stream, started, ended, text)
                logger.info("%s %.0fs -> %.1fs: %s", stream, (ended - started).total_seconds(), dt, text[:120])
                if stream == radio.STREAM_SCANNER:
                    self._triage_scanner(text, started)
            except Exception:
                logger.exception("transcription failed for a %s segment", stream)

    def _triage_scanner(self, text: str, at: datetime) -> None:
        if self.llm is None:
            return
        verdict = radio.classify_scanner(self.llm, text, self.home_area)
        if not verdict or not verdict.get("important"):
            return
        key = "scanner:" + sha1(f"{at.isoformat()}|{text}".encode()).hexdigest()[:16]
        radio.add_item(self.db_path, key, "scanner", verdict.get("severity", "info"),
                       verdict.get("summary") or text[:140], body=f"Heard: {text[:300]}",
                       stream=radio.STREAM_SCANNER, at=at,
                       meta={"category": verdict.get("category"), "location": verdict.get("location")})

    def conditions_loop(self) -> None:
        interval = max(120, int(self.cfg.radio_conditions_interval_seconds))
        while not self.stop.is_set():
            self.stop.wait(interval)
            if self.stop.is_set() or self.llm is None:
                continue
            try:
                text = radio.transcript_text(self.db_path, radio.STREAM_WEATHER, minutes=12)
                cond = radio.extract_conditions(self.llm, text, self.home_area)
                if not cond:
                    continue
                radio.record_conditions(self.db_path, cond)
                logger.info("conditions: %s", cond.get("summary"))
                hazards = (cond.get("hazards") or "").strip()
                # Two gates, because the hazard line is model prose and fails in two
                # directions. hazard_is_real drops the all-clears ("no hazardous weather
                # is expected" was being texted to him AS a hazard). hazard_already_filed
                # drops the same standing hazard reworded on the next pass, which is what
                # turned one coastal flood watch into nineteen texts in three hours.
                if hazards and radio.hazard_is_real(hazards):
                    if radio.hazard_already_filed(self.db_path, hazards):
                        logger.debug("hazard unchanged, not filing again: %s", hazards[:80])
                    else:
                        signature = radio.hazard_signature(hazards)
                        key = "weather_hazard:" + sha1(
                            "|".join(signature).encode()).hexdigest()[:12]
                        radio.add_item(self.db_path, key, "weather_hazard", "notice",
                                       f"NOAA weather radio mentions: {hazards[:200]}",
                                       stream=radio.STREAM_WEATHER,
                                       meta={"signature": signature})
            except Exception:
                logger.exception("conditions extraction failed")

    # -- the Pis --------------------------------------------------------------------
    def poll_loop(self) -> None:
        from .core.ssh_ops import SSHOpsClient
        ssh = SSHOpsClient(_resolve_key_paths(self.cfg.ssh_hosts or {}))
        last_eas = last_rf = last_prune = 0.0
        while not self.stop.is_set():
            now = time.time()
            if now - last_eas >= self.cfg.radio_eas_poll_seconds:
                last_eas = now
                self._poll_eas(ssh)
            if now - last_rf >= self.cfg.radio_rf_poll_seconds:
                last_rf = now
                self._poll_rf(ssh)
            if now - last_prune >= 3600:
                last_prune = now
                try:
                    radio.prune(self.db_path)
                except Exception:
                    logger.exception("prune failed")
            self.stop.wait(5)

    def _poll_eas(self, ssh) -> None:
        host = self.cfg.radio_eas_host
        if not host or host not in (self.cfg.ssh_hosts or {}):
            return
        try:
            res = ssh.run_command(host, f"tail -n 40 {self.cfg.radio_eas_events_path} 2>/dev/null; true", timeout=30)
        except Exception as e:
            logger.warning("EAS poll failed: %s", e)
            return
        radio.set_state(self.db_path, "poll:eas", "ok")
        for line in (res.get("output") or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = __import__("json").loads(line)
            except ValueError:
                continue
            self._ingest_eas(ev)

    def _ingest_eas(self, ev: dict) -> None:
        raw = ev.get("raw") or ""
        at = datetime.fromisoformat(ev["at"]) if ev.get("at") else _now()
        expires = None
        try:
            hh, mm = (ev.get("purge_hhmm") or "0:0").split(":")
            expires = at + timedelta(hours=int(hh), minutes=int(mm))
        except (ValueError, AttributeError):
            pass
        areas = ", ".join(ev.get("area_names") or ev.get("areas") or []) or "unspecified area"
        severity = ev.get("severity", "notice")
        affects = bool(ev.get("affects_me"))
        # A warning for a county the owner is not in is context, not a push.
        if not affects and severity == "urgent":
            severity = "notice"
        if ev.get("event_code") in ("RWT", "RMT", "NPT", "DMO"):
            severity = "info"
        title = f"{ev.get('event', 'EAS alert')} for {areas}"
        if expires:
            title += f", until {expires.astimezone().strftime('%H:%M')}"
        new = radio.add_item(self.db_path, "eas:" + (raw or ev["at"]), "eas", severity, title,
                             body="Decoded from NOAA Weather Radio's SAME header.",
                             stream=radio.STREAM_WEATHER, at=at,
                             meta={**{k: ev.get(k) for k in ("event_code", "areas", "area_names",
                                                             "affects_me", "sender")},
                                   "expires_at": expires.isoformat() if expires else None})
        if new:
            logger.warning("EAS: %s [%s]", title, severity)

    def _poll_rf(self, ssh) -> None:
        host = self.cfg.radio_rf_host
        if not host or host not in (self.cfg.ssh_hosts or {}):
            return
        try:
            res = ssh.run_command(host, self.cfg.radio_rf_baseline_cmd, timeout=90)
        except Exception as e:
            logger.warning("RF baseline poll failed: %s", e)
            return
        if not res.get("ok"):
            logger.warning("RF baseline command failed: %s", (res.get("output") or "")[:200])
            return
        rep = radio.parse_json_object(res.get("output"))
        if not rep or "devices" not in rep:
            logger.warning("RF baseline output was not a report")
            return
        radio.set_state(self.db_path, "poll:rf", "ok")
        radio.set_state(self.db_path, "rf_baseline", rep)
        for a in rep.get("alerts", []):
            kind = {"vehicle-watch": "rf_vehicle", "unknown-new": "rf_new",
                    "traffic-spike": "rf_traffic"}.get(a.get("kind"), "rf_new")
            # Re-raise a watched vehicle as it escalates (every 3 more visits), not on
            # every poll.
            step = int(a.get("escalation") or 0) // 3 if kind == "rf_vehicle" else 0
            key = f"{a.get('key')}:{step}"
            if radio.add_item(self.db_path, key, kind, a.get("severity", "info"),
                              a.get("text", "RF sensor alert"), meta=a):
                logger.info("RF: %s", a.get("text"))

    # -- lifecycle --------------------------------------------------------------------
    def run(self) -> None:
        listeners = [
            StreamListener(radio.STREAM_WEATHER, self.cfg.radio_weather_url, self.db_path, self.on_segment, "continuous"),
            StreamListener(radio.STREAM_SCANNER, self.cfg.radio_scanner_url, self.db_path, self.on_segment, "bursts"),
        ]
        threads = [threading.Thread(target=self.transcribe_loop, name="stt", daemon=True),
                   threading.Thread(target=self.conditions_loop, name="conditions", daemon=True),
                   threading.Thread(target=self.poll_loop, name="poll", daemon=True)]
        for t in listeners + threads:
            t.start()
        logger.info("radio worker up: weather=%s scanner=%s eas_host=%s rf_host=%s llm=%s",
                    self.cfg.radio_weather_url, self.cfg.radio_scanner_url, self.cfg.radio_eas_host,
                    self.cfg.radio_rf_host, "on" if self.llm else "OFF (no local_llm_host)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            for l in listeners:
                l.stop.set()


def main() -> int:
    setup_logging("jarvis-radio")
    _service_profile_fixups()
    cfg = load_config()
    db.init_db(cfg.db_path)
    radio.init_radio_db(cfg.db_path)
    llm = None
    if cfg.local_llm_host:
        from .core.llm import LLMClient
        # Longer than the fast path's 20 s: a 12-minute weather transcript is a real prompt.
        llm = LLMClient(cfg.local_llm_host, cfg.local_llm_model, timeout=90)
    Worker(cfg, llm).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
