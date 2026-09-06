"""Speech-to-text via a local Whisper model (faster-whisper / CTranslate2). Runs
entirely on this machine — no audio ever leaves the LAN. The model loads lazily on
first transcription request rather than at startup, same reasoning as
build_phone_context: a slow/missing model download must only disable voice input,
never block or crash the whole assistant.
"""
import io
import logging

from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, model_size: str = "small.en", device: str = "cpu", compute_type: str = "int8"):
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model: WhisperModel | None = None

    def _ensure_loaded(self) -> WhisperModel:
        if self._model is None:
            logger.info("Loading Whisper model %s (%s/%s)...", self._model_size, self._device, self._compute_type)
            self._model = WhisperModel(self._model_size, device=self._device, compute_type=self._compute_type)
        return self._model

    def transcribe(self, audio_bytes: bytes) -> str:
        """audio_bytes is whatever container the browser's MediaRecorder produced
        (webm/opus, mp4/aac, etc.) — faster-whisper decodes it via PyAV, so no format
        conversion is needed on our end. vad_filter trims the silence/breath padding
        a short push-to-talk clip typically has, which otherwise confuses the model."""
        model = self._ensure_loaded()
        segments, _ = model.transcribe(io.BytesIO(audio_bytes), language="en", vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()
