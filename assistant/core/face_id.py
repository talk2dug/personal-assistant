"""Face embedding for identity, via InsightFace.

Runs on the same GPU as detector.py's YOLO model (targeting the RTX 3060 the vision
feature is built for), and lives in the same isolated .venv-vision -- torch/insightface/
onnxruntime-gpu are not in the assistant's main requirements.txt for the same reason
ultralytics/torch aren't (see detector.py's module docstring): they're heavy,
CUDA-specific packages the assistant's own runtime must not depend on just to import
this module. See requirements-vision.txt at the repo root for what to install where.

Embeddings are handed back as plain list[float] (L2-normalised, JSON-serialisable), not
numpy arrays -- everywhere else this codebase touches a face vector (vision.py's
known_people/unknown_faces tables) treats it as JSON, and this module is the only place
numpy needs to be involved at all.
"""
import logging
import threading
import time

import numpy as np

log = logging.getLogger(__name__)


class FaceDetection:
    __slots__ = ("embedding", "box", "det_score")

    def __init__(self, embedding: list, box: tuple, det_score: float):
        self.embedding = embedding      # list[float], L2-normalised, JSON-safe
        self.box = box                  # (x1, y1, x2, y2) in pixels
        self.det_score = det_score

    def __repr__(self):
        return f"<face det={self.det_score:.2f} {self.box}>"


class FaceIdentifier:
    """InsightFace, loaded once and reused.

    Thread-safe by lock rather than by an instance per thread -- same reasoning as
    detector.Detector: several cameras sharing one GPU should queue rather than each
    hold their own CUDA context.
    """

    def __init__(self, model_name: str = "buffalo_l", device: str | None = None,
                 det_size: tuple = (640, 640)):
        self.model_name = model_name
        self.det_size = det_size
        self._device = device
        self._app = None
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        if self._app is not None:
            return
        from insightface.app import FaceAnalysis  # lazy: only present in .venv-vision
        import torch

        if self._device is None:
            self._device = "cuda:0" if torch.cuda.is_available() else "cpu"
        # onnxruntime (what InsightFace runs on) takes a provider list, not a torch-style
        # device string, and a ctx_id rather than a device name.
        providers = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                    if self._device.startswith("cuda") else ["CPUExecutionProvider"])
        ctx_id = 0 if self._device.startswith("cuda") else -1
        log.info("loading InsightFace %s on %s", self.model_name, self._device)
        t0 = time.time()
        app = FaceAnalysis(name=self.model_name, providers=providers)
        app.prepare(ctx_id=ctx_id, det_size=self.det_size)
        self._app = app
        log.info("face identifier ready in %.1fs", time.time() - t0)

    @property
    def device(self) -> str:
        self._ensure_loaded()
        return self._device or "cpu"

    def embed_faces(self, frame: np.ndarray) -> list[FaceDetection]:
        """Every face InsightFace finds in one frame, each with its normalised
        embedding. Empty list means no face was visible -- a person can be detected by
        YOLO with their back turned or too far away for a face to register here."""
        self._ensure_loaded()
        with self._lock:
            # InsightFace wraps OpenCV and expects BGR; frames elsewhere in this codebase
            # are RGB (see detector.py/vision.py's decode_jpeg), so convert here rather
            # than push that detail on every caller.
            faces = self._app.get(frame[:, :, ::-1])
        out: list[FaceDetection] = []
        for face in faces:
            embedding = getattr(face, "normed_embedding", None)
            if embedding is None:
                continue
            box = tuple(int(v) for v in face.bbox.tolist())
            out.append(FaceDetection(embedding.tolist(), box, float(face.det_score)))
        return out
