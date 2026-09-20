#!/usr/bin/env python
"""Orpheus text-to-speech microservice (CPU SNAC decode + Ollama token gen).

Turns text into a 24 kHz mono WAV using the Orpheus GGUF model hosted on the
simrig Ollama server for audio-token generation and a locally-warm SNAC decoder
for waveform synthesis. Intended to give Jarvis's phone line a natural human
voice; the phone bridge falls back to Piper on any non-2xx response.

Run:
    .venv-tts\\Scripts\\python voip_node\\orpheus_tts.py --port 8130

Endpoints:
    POST /tts     {"text": "...", "voice": "leo"}  -> audio/wav (24 kHz mono)
    GET  /health  -> {"status": "ok", ...}

IMPORTANT: run with the isolated .venv-tts interpreter (torch 2.14 cpu + snac).
The main .venv has no torch. This service does NOT touch config.json, the main
venv, git, or any running Jarvis service.
"""

import argparse
import io
import json
import logging
import re
import sys
import threading
import time
import urllib.request
import urllib.error
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import torch

# --- config / constants --------------------------------------------------

MODEL = "legraphista/Orpheus:latest"
VOICES = {"tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe"}
DEFAULT_VOICE = "leo"
SAMPLE_RATE = 24000
SNAC_MODEL_ID = "hubertsiuzdak/snac_24khz"
CODES_PER_FRAME = 7
# audio tokens carry code = N - 10; the 7 slots in a frame are offset by
# slot*4096, so control tokens (N < 10 -> negative code) are dropped and each
# code is taken mod 4096 to strip the slot offset.
TOKEN_BASE = 10
CODEBOOK_SIZE = 4096

log = logging.getLogger("orpheus_tts")

# One shared SNAC model, warmed once at startup. torch CPU decode is serialized
# through this lock so concurrent requests can't re-enter the same module.
_snac = None
_decode_lock = threading.Lock()

# runtime-configurable (set from argv in main)
OLLAMA_URL = "http://192.168.0.139:11434/api/generate"
OLLAMA_TIMEOUT = 120


# --- Orpheus token generation --------------------------------------------

def ollama_generate(text, voice):
    """Return (response_text, elapsed_ms). Raises on transport/HTTP error."""
    prompt = f"<|begin_of_text|>{voice}: {text}<|eot_id|>"
    body = {
        "model": MODEL,
        "prompt": prompt,
        "raw": True,
        "stream": False,
        "options": {"num_ctx": 4096, "temperature": 0.6},
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return resp.get("response", ""), (time.time() - t0) * 1000.0


_TOKEN_RE = re.compile(r"<custom_token_(\d+)>")


def tokens_to_codebooks(response_text):
    """Parse <custom_token_N> tokens into SNAC's three codebook lists.

    Drops leading/interspersed control tokens (N < TOKEN_BASE), groups the rest
    into frames of 7, and fans each frame out across the 3 codebooks. Returns
    (c1, c2, c3) as python int lists, or None if there is not even one frame.
    """
    raw = [int(m) for m in _TOKEN_RE.findall(response_text)]
    audio = [n for n in raw if n >= TOKEN_BASE]
    nframes = len(audio) // CODES_PER_FRAME
    if nframes == 0:
        return None
    audio = audio[: nframes * CODES_PER_FRAME]
    c1, c2, c3 = [], [], []
    for f in range(nframes):
        s = audio[f * CODES_PER_FRAME : f * CODES_PER_FRAME + CODES_PER_FRAME]
        code = [(x - TOKEN_BASE) % CODEBOOK_SIZE for x in s]
        c1.append(code[0])
        c2.extend([code[1], code[4]])
        c3.extend([code[2], code[3], code[5], code[6]])
    return c1, c2, c3


def snac_decode(c1, c2, c3):
    """Decode three codebooks to a float32 mono waveform in [-1, 1]."""
    with _decode_lock:
        with torch.inference_mode():
            codes = [
                torch.tensor(c1, dtype=torch.int64).unsqueeze(0),
                torch.tensor(c2, dtype=torch.int64).unsqueeze(0),
                torch.tensor(c3, dtype=torch.int64).unsqueeze(0),
            ]
            audio = _snac.decode(codes)
    return audio.squeeze().cpu().numpy().astype(np.float32)


# --- text handling --------------------------------------------------------

def split_sentences(text):
    """Split into sentences on terminal punctuation; keep it simple/robust."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def synth(text, voice):
    """Generate + decode PCM float for the whole reply.

    Returns (float_waveform, timings). Raises RuntimeError if any sentence
    yields no usable audio so the caller returns 5xx and the phone falls back
    to Piper.
    """
    sentences = split_sentences(text)
    if not sentences:
        raise RuntimeError("empty text")

    chunks = []
    timings = []
    for i, sent in enumerate(sentences):
        try:
            resp_text, gen_ms = ollama_generate(sent, voice)
        except Exception as e:  # transport, timeout, HTTP error
            raise RuntimeError(f"ollama generate failed on sentence {i}: {e}")
        books = tokens_to_codebooks(resp_text)
        if books is None:
            raise RuntimeError(
                f"no audio tokens for sentence {i} (garbled/empty generation)"
            )
        t0 = time.time()
        wav = snac_decode(*books)
        dec_ms = (time.time() - t0) * 1000.0
        frames = len(books[0])
        dur = len(wav) / SAMPLE_RATE
        log.info(
            "sentence %d/%d: gen=%dms decode=%dms frames=%d dur=%.2fs %r",
            i + 1, len(sentences), round(gen_ms), round(dec_ms), frames, dur,
            sent[:60],
        )
        timings.append({"gen_ms": round(gen_ms), "decode_ms": round(dec_ms),
                        "frames": frames, "dur_s": round(dur, 2)})
        chunks.append(wav)

    full = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    return full, timings


def to_wav_bytes(wav_float):
    pcm = np.clip(wav_float, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm16.tobytes())
    return buf.getvalue()


# --- HTTP handler ---------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # route access logs through logging
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            self._send_json(200, {
                "status": "ok",
                "model": MODEL,
                "snac_loaded": _snac is not None,
                "sample_rate": SAMPLE_RATE,
                "voices": sorted(VOICES),
                "default_voice": DEFAULT_VOICE,
            })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/tts":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception as e:
            self._send_json(400, {"error": f"bad request body: {e}"})
            return

        text = (payload.get("text") or "").strip()
        voice = (payload.get("voice") or DEFAULT_VOICE).strip().lower()
        if voice not in VOICES:
            log.warning("unknown voice %r, falling back to %s", voice, DEFAULT_VOICE)
            voice = DEFAULT_VOICE
        if not text:
            self._send_json(400, {"error": "missing 'text'"})
            return

        t0 = time.time()
        try:
            wav_float, timings = synth(text, voice)
        except RuntimeError as e:
            log.error("tts failed: %s", e)
            self._send_json(502, {"error": str(e)})  # 5xx -> caller uses Piper
            return
        except Exception as e:
            log.exception("tts unexpected error")
            self._send_json(500, {"error": f"internal error: {e}"})
            return

        wav_bytes = to_wav_bytes(wav_float)
        total_ms = (time.time() - t0) * 1000.0
        dur = len(wav_float) / SAMPLE_RATE
        log.info("tts done voice=%s sentences=%d total=%dms audio=%.2fs bytes=%d",
                 voice, len(timings), round(total_ms), dur, len(wav_bytes))

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav_bytes)))
        self.send_header("X-Audio-Duration", f"{dur:.2f}")
        self.send_header("X-Voice", voice)
        self.end_headers()
        self.wfile.write(wav_bytes)


# --- startup --------------------------------------------------------------

def load_snac():
    global _snac
    t0 = time.time()
    from snac import SNAC
    _snac = SNAC.from_pretrained(SNAC_MODEL_ID).eval()
    log.info("SNAC loaded in %d ms", round((time.time() - t0) * 1000))


def main():
    global OLLAMA_URL, OLLAMA_TIMEOUT
    ap = argparse.ArgumentParser(description="Orpheus TTS microservice")
    ap.add_argument("--port", type=int, default=8130)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--ollama", default=OLLAMA_URL,
                    help="Ollama /api/generate URL")
    ap.add_argument("--ollama-timeout", type=int, default=OLLAMA_TIMEOUT)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    OLLAMA_URL = args.ollama
    OLLAMA_TIMEOUT = args.ollama_timeout

    load_snac()  # warm before we start accepting requests

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("Orpheus TTS listening on http://%s:%d  (ollama=%s)",
             args.host, args.port, OLLAMA_URL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
