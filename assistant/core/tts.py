"""Text-to-speech via Piper — Jarvis's voice for the standalone devices.

The web UI speaks through the browser's own speechSynthesis, which a headless Pi can't
use, so speech has to be synthesised somewhere. It runs here on the laptop rather than
on each device for two reasons: every device then shares one voice and one place to
change it, and simrig's GPU is deliberately not involved — that card gets reserved when
the owner is racing, and an assistant that goes mute mid-race is worse than one with a
plainer voice. Piper is CPU-only and measured at ~4.7x realtime here, which is quick
enough that the round trip is dominated by the LLM, not the speech.

Model loads lazily, same reasoning as Transcriber: a missing voice file must disable
speech, never block startup.
"""
import io
import logging
import re
import wave
from typing import Iterator

logger = logging.getLogger(__name__)

# Chunks shorter than this get glued onto the next one. Synthesising "Yes." on its own
# costs a whole model invocation and produces an audio file barely longer than the gap
# it introduces -- the seam is more noticeable than the latency it saves.
MIN_CHUNK_CHARS = 25

# Past this, a "sentence" is really a paragraph and waiting for all of it defeats the
# point, so it gets broken at the nearest clause boundary instead.
MAX_CHUNK_CHARS = 240

# Abbreviations whose full stop does NOT end a sentence. Without these, "Mr. Swayze" and
# "approx. 40 minutes" each split mid-phrase and the voice takes a breath in the wrong
# place -- which is far more audible than it sounds on paper.
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "eg", "ie",
    "approx", "est", "dept", "inc", "ltd", "co", "no", "fig", "al", "am", "pm",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}

_SENTENCE_END = re.compile(r"(?<=[.!?])[\s]+")
_CLAUSE_SPLIT = re.compile(r"(?<=[,;:])\s+")


def _ends_sentence(fragment: str) -> bool:
    """Whether a fragment ending in . ! or ? is really the end of a sentence."""
    stripped = fragment.rstrip()
    if not stripped or stripped[-1] not in ".!?":
        return False
    if stripped[-1] in "!?":
        return True
    # "...costs $4.50" / "version 1.2" -- a digit either side of the dot is a number.
    if len(stripped) >= 2 and stripped[-2].isdigit():
        return False
    last_word = re.split(r"[\s(]", stripped[:-1])[-1].lower().strip(".,;:\"'")
    if last_word in _ABBREVIATIONS:
        return False
    # A single initial ("J." in "J. Swayze") is not a sentence end either.
    if len(last_word) == 1 and last_word.isalpha():
        return False
    return True


def _hard_wrap(text: str) -> list[str]:
    """Break on word boundaries, used only when nothing better exists."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    out, acc = [], ""
    for word in text.split():
        if acc and len(acc) + len(word) + 1 > MAX_CHUNK_CHARS:
            out.append(acc)
            acc = word
        else:
            acc = f"{acc} {word}".strip() if acc else word
    if acc:
        out.append(acc)
    return out


def split_for_speech(text: str) -> list[str]:
    """Break cleaned text into chunks that can each be spoken on their own.

    This exists so speech can START while the rest is still being synthesised. The whole
    voice round trip was strictly sequential -- transcribe, think, synthesise the entire
    reply, then finally play it -- so a long answer stayed silent for its whole synthesis
    time. Splitting on sentences means the first one can be playing while the second is
    still being made.

    The seams have to fall where a person would pause, which is the entire difficulty:
    a break after "Mr." or inside "$63.41" is instantly audible as a machine reading
    badly, and is worse than the delay it saved.
    """
    text = (text or "").strip()
    if not text:
        return []

    # First pass: real sentence boundaries.
    raw, buf = [], ""
    for piece in _SENTENCE_END.split(text):
        buf = f"{buf} {piece}".strip() if buf else piece
        if _ends_sentence(buf):
            raw.append(buf)
            buf = ""
    if buf.strip():
        raw.append(buf.strip())

    # Second pass: break anything still too long, and merge anything too short.
    chunks: list[str] = []
    for sentence in raw:
        if len(sentence) <= MAX_CHUNK_CHARS:
            candidates = [sentence]
        else:
            candidates, acc = [], ""
            for clause in _CLAUSE_SPLIT.split(sentence):
                if acc and len(acc) + len(clause) + 1 > MAX_CHUNK_CHARS:
                    candidates.append(acc.strip())
                    acc = clause
                else:
                    acc = f"{acc} {clause}".strip() if acc else clause
            if acc.strip():
                candidates.append(acc.strip())
            # Last resort: text with no punctuation at all has neither sentence nor
            # clause boundaries to break on, and would otherwise come back as one
            # enormous chunk -- exactly the "stays silent for ages" case this whole
            # function exists to prevent. Break on whitespace instead; an unpunctuated
            # wall of words has no good seam anyway.
            candidates = [c for part in candidates for c in _hard_wrap(part)]

        for candidate in candidates:
            if chunks and len(chunks[-1]) < MIN_CHUNK_CHARS:
                chunks[-1] = f"{chunks[-1]} {candidate}".strip()
            else:
                chunks.append(candidate)

    return [c for c in chunks if c.strip()]


class Speaker:
    """One voice, wherever Jarvis is speaking from.

    Jack, 2026-09-21: *"i really want to look at how we can use the same voice in all
    instances"* and *"i want it to feel as if Jarvis moved to the device im talking to
    him on."* He had three voices. The phone used Orpheus, the Pi terminals used Piper,
    and the web UI used the BROWSER's own speechSynthesis -- which is not merely a third
    voice but a different voice on every browser and every OS he opened it on. Nothing
    reads as one person moving between rooms when he changes accent per screen.

    So the choice of voice lives here, once, and every surface asks this class. Orpheus
    first because it is the one that sounds like a person; Piper underneath it because a
    plainer voice is enormously better than silence, and because the fallback is what
    makes it safe to prefer the slower engine at all.

    Measured locally, both on this box (there is no network in this path -- Orpheus
    listens on 127.0.0.1):
        Piper     ~4.7x realtime
        Orpheus   ~2.0x realtime warm, ~7.4s on the first call after it goes cold
    Both outrun playback, so with split_for_speech chunking the only real cost of the
    better voice is time-to-first-word, about +0.7s. The cold start is the part that
    actually hurts, and it is why the scheduler pings this every few minutes.
    """

    def __init__(self, voice_path: str | None = None, length_scale: float | None = 1.04,
                 orpheus_url: str | None = None, orpheus_voice: str = "dan",
                 orpheus_timeout: float = 30.0):
        self.voice_path = voice_path
        # Slightly slower than default: Jarvis is measured, and it also survives a cheap
        # speaker better than a rushed delivery does.
        self.length_scale = length_scale
        self.orpheus_url = orpheus_url
        self.orpheus_voice = orpheus_voice
        self.orpheus_timeout = orpheus_timeout
        self._voice = None

    def available(self) -> bool:
        return bool(self.voice_path) or bool(self.orpheus_url)

    def warm(self) -> bool:
        """Synthesise something tiny so the next real reply is not the cold one.

        A 7.4s first word after an idle hour reads as Jarvis being slow, not as a model
        loading, and it lands precisely when he has not spoken to it in a while -- the
        worst possible moment for the illusion of presence.
        """
        if not self.orpheus_url:
            return False
        try:
            self._orpheus("Ready.")
            return True
        except Exception:
            logger.debug("tts: could not warm the natural voice", exc_info=True)
            return False

    def _orpheus(self, spoken: str) -> bytes:
        import requests

        response = requests.post(
            self.orpheus_url, json={"text": spoken, "voice": self.orpheus_voice},
            timeout=self.orpheus_timeout)
        response.raise_for_status()
        if not response.content:
            raise ValueError("the natural voice returned no audio")
        return response.content

    def _ensure_loaded(self):
        if self._voice is None:
            from piper import PiperVoice

            logger.info("Loading Piper voice %s...", self.voice_path)
            self._voice = PiperVoice.load(self.voice_path)
        return self._voice

    @staticmethod
    def clean(text: str) -> str:
        """Strips markdown so it isn't read aloud.

        Same problem the browser had — "**Chime Checking**" is spoken as "asterisk
        asterisk Chime Checking asterisk asterisk". The web UI solves this in JS; a
        device client never touches that code, so the server has to do it too.
        """
        if not text:
            return ""
        cleaned = re.sub(r"```[\s\S]*?```", " ", text)
        cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
        cleaned = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", cleaned)
        cleaned = re.sub(r"^\s{0,3}#{1,6}\s+", "", cleaned, flags=re.M)
        cleaned = re.sub(r"^\s*>\s?", "", cleaned, flags=re.M)
        cleaned = re.sub(r"^\s*[-*+]\s+", "", cleaned, flags=re.M)
        cleaned = re.sub(r"^\s*\d+\.\s+", "", cleaned, flags=re.M)
        cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
        cleaned = re.sub(r"__([^_]+)__", r"\1", cleaned)
        cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
        cleaned = re.sub(r"(^|\s)_([^_]+)_(?=\s|$)", r"\1\2", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        return cleaned.strip()

    def synthesize(self, text: str) -> bytes:
        """Returns a complete WAV file. Bytes rather than a stream because the replies
        are a few seconds long and the client wants a file it can hand straight to an
        audio player."""
        spoken = self.clean(text)
        if not spoken:
            raise ValueError("nothing to say")
        return self._synthesize_one(spoken)

    def synthesize_stream(self, text: str) -> Iterator[bytes]:
        """One complete WAV per speakable chunk, yielded as each is ready.

        The point is time-to-first-sound, not total throughput -- the caller can start
        playing chunk one while chunk two is still being made, so a long answer begins
        speaking almost immediately instead of after its whole synthesis.

        Each chunk is a self-contained WAV rather than raw PCM on purpose: every client
        here already knows how to play a WAV (the kiosks hand bytes straight to an audio
        player), and a bare PCM stream would mean teaching all of them the sample format.
        The cost is a 44-byte header per chunk, which is nothing next to the audio.

        A chunk that fails to synthesise is skipped rather than aborting the rest -- a
        reply that loses one sentence is better than one that goes silent halfway.
        """
        spoken = self.clean(text)
        if not spoken:
            raise ValueError("nothing to say")
        chunks = split_for_speech(spoken)
        if not chunks:
            return
        for chunk in chunks:
            try:
                yield self._synthesize_one(chunk)
            except Exception:
                logger.exception("tts: skipping a chunk that failed to synthesise")

    def _synthesize_one(self, spoken: str) -> bytes:
        """Synthesise already-cleaned text. Shared by synthesize and synthesize_stream so
        the two can never drift in pacing or format -- and now so they can never drift in
        VOICE either, which is the whole point of routing every surface through here.
        """
        if self.orpheus_url:
            try:
                return self._orpheus(spoken)
            except Exception:
                # Never fail the reply over the nicer voice. Logged rather than silent:
                # "why does he sound different today" should be answerable from the log,
                # since a fallback that hides itself is how three voices went unnoticed.
                logger.warning("tts: natural voice unavailable, falling back to Piper",
                               exc_info=True)
            if not self.voice_path:
                raise RuntimeError("the natural voice failed and no fallback is configured")
        voice = self._ensure_loaded()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            try:
                from piper import SynthesisConfig

                voice.synthesize_wav(
                    spoken, wav, syn_config=SynthesisConfig(length_scale=self.length_scale))
            except ImportError:
                # Older piper-tts has no SynthesisConfig; the default pacing is fine.
                voice.synthesize_wav(spoken, wav)
        return buffer.getvalue()
