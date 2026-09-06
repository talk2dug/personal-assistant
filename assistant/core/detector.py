"""Object detection for the house cameras: is that a person, or the dog?

Runs YOLO on the laptop's RTX 3060. The Coral USB accelerator on UbuntuServer001 would be
the more elegant home for this — 2W instead of 40 — but its Python runtime pairing
(libedgetpu 16.0 wants TFLite 2.5, which wants Python 3.9) turned into an afternoon, and
the point of the exercise is house logic rather than a TPU. The interface here is
deliberately narrow so swapping the backend later touches one class.

Only the classes the house actually reasons about are kept. COCO has eighty; a chair
being detected in the living room is not an event, and filtering at the source keeps the
event log meaningful instead of merely full.

This module lives in the assistant package but imports torch lazily, because torch is
installed only in .venv-vision — the assistant's own runtime must not depend on it.
"""
import logging
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

# What the house cares about. 'person' drives presence and identity; the animals matter
# because "something moved" at 3am is a very different fact depending on which it was.
CLASSES_OF_INTEREST = {
    "person": "person",
    "dog": "pet",
    "cat": "pet",
    "bird": "pet",
}

# Below this a detection is noise. Deliberately higher for pets: a dog at distance and a
# person crouching look similar to a small model, and calling a person "the dog" is the
# expensive mistake here -- it is the one that would suppress an alert that mattered.
MIN_CONFIDENCE = {"person": 0.45, "pet": 0.55}


class Detection:
    __slots__ = ("label", "kind", "confidence", "box")

    def __init__(self, label: str, kind: str, confidence: float, box: tuple):
        self.label = label          # 'person', 'dog', 'cat'
        self.kind = kind            # 'person' or 'pet'
        self.confidence = confidence
        self.box = box              # (x1, y1, x2, y2) in pixels

    def __repr__(self):
        return f"<{self.label} {self.confidence:.2f} {self.box}>"

    def crop(self, frame: np.ndarray, pad: float = 0.1) -> np.ndarray:
        """The detection's region, padded a little -- a face crop cut exactly to a
        person box tends to clip the top of the head."""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = self.box
        px, py = (x2 - x1) * pad, (y2 - y1) * pad
        x1 = max(0, int(x1 - px)); y1 = max(0, int(y1 - py))
        x2 = min(w, int(x2 + px)); y2 = min(h, int(y2 + py))
        return frame[y1:y2, x1:x2]


class Detector:
    """YOLO, loaded once and reused.

    Thread-safe by lock rather than by a model per thread: the model is ~6MB of weights
    but holds CUDA context, and several cameras sharing one GPU should queue rather than
    each allocate their own. Detection is ~10ms, so the lock is not a bottleneck at the
    frame rates a motion gate produces.
    """

    def __init__(self, model_name: str = "yolo11n.pt", device: str | None = None,
                 imgsz: int = 640):
        self.model_name = model_name
        self.imgsz = imgsz
        self._lock = threading.Lock()
        self._model = None
        self._device = device
        self._names: dict[int, str] = {}

    def _ensure_loaded(self):
        if self._model is not None:
            return
        from ultralytics import YOLO           # lazy: torch lives in .venv-vision only
        import torch

        if self._device is None:
            self._device = "cuda:0" if torch.cuda.is_available() else "cpu"
        log.info("loading %s on %s", self.model_name, self._device)
        t0 = time.time()
        self._model = YOLO(self.model_name)
        self._model.to(self._device)
        self._names = self._model.names
        # One warm-up pass: the first inference includes CUDA kernel compilation and is
        # ten times slower than steady state, which would otherwise look like a stall on
        # the very first motion event of the day.
        self._model.predict(np.zeros((self.imgsz, self.imgsz, 3), dtype=np.uint8),
                            imgsz=self.imgsz, verbose=False, device=self._device)
        log.info("detector ready in %.1fs", time.time() - t0)

    @property
    def device(self) -> str:
        self._ensure_loaded()
        return self._device or "cpu"

    def detect(self, frame: np.ndarray) -> list[Detection]:
        """Everything of interest in one frame. Empty list means an empty room."""
        self._ensure_loaded()
        with self._lock:
            results = self._model.predict(frame, imgsz=self.imgsz, verbose=False,
                                          device=self._device)
        out: list[Detection] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            for box in boxes:
                label = self._names.get(int(box.cls[0]), "")
                kind = CLASSES_OF_INTEREST.get(label)
                if kind is None:
                    continue
                conf = float(box.conf[0])
                if conf < MIN_CONFIDENCE.get(kind, 0.5):
                    continue
                xy = box.xyxy[0].tolist()
                out.append(Detection(label, kind, conf, tuple(int(v) for v in xy)))
        return out

    def summarise(self, detections: list[Detection]) -> dict:
        """Collapse a frame's detections into the facts house logic asks for."""
        people = [d for d in detections if d.kind == "person"]
        pets = [d for d in detections if d.kind == "pet"]
        return {
            "person_count": len(people),
            "pet_count": len(pets),
            "pets": sorted({d.label for d in pets}),
            "has_person": bool(people),
            "has_pet": bool(pets),
            "top_person_confidence": max((d.confidence for d in people), default=0.0),
        }
