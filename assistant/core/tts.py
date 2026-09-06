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

logger = logging.getLogger(__name__)


class Speaker:
    def __init__(self, voice_path: str | None = None, length_scale: float | None = 1.04):
        self.voice_path = voice_path
        # Slightly slower than default: Jarvis is measured, and it also survives a cheap
        # speaker better than a rushed delivery does.
        self.length_scale = length_scale
        self._voice = None

    def available(self) -> bool:
        return bool(self.voice_path)

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
