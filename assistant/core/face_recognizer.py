"""Face recognition for kiosk cameras: turns a person crop into a 512-d ArcFace
embedding that assistant/core/vision.py can match against its known_people table.

Kiosk webcams only, for now (Phase 1 decision #1 -- see core/identity.py's docstring).
The simrig and laptop cameras that exist elsewhere in the house are a different trust
boundary -- a shared workspace everyone walks past, not a household terminal someone
is addressing directly -- and folding them into person-identification before that
boundary is actually decided would either enroll faces nobody agreed to enroll, or
silently do nothing on hardware nobody meant to wire up. Widening this is a config
change (a camera's `role` becoming 'kiosk'), not a rewrite, once that's decided.

Runs on InsightFace's buffalo_l pack (RetinaFace detector + ArcFace recognition,
512-d embeddings) -- the same pragmatic choice as detector.py's YOLO: an established,
pretrained model rather than training anything for a household of one enrolled person.
Lives in the same .venv-vision as torch/ultralytics (see requirements-vision.txt) so
the assistant's own runtime never depends on onnxruntime/insightface either -- this
module's own top-level imports are deliberately just numpy/threading/logging so it can
still be *imported* (though not used) from a process that doesn't have InsightFace
installed, e.g. vision.py's match_face() when it's loaded in the web process.
"""
import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

EMBEDDING_DIM = 512

# Cosine similarity at or above this counts as the same person. Deliberately
# conservative (fewer false accepts, more false rejects) because the two failure modes
# are not symmetric here: wrongly recognising *someone else* as the owner hands them
# personal/financial tools, while a false reject just falls back to shared-only data
# for one turn -- annoying, never unsafe. Tune per-deployment lighting if real use
# shows it's too strict; do not loosen it to fix a single bad camera angle.
MATCH_THRESHOLD = 0.50

# Below this, InsightFace's own detector doesn't trust the face enough to bother --
# matches detector.py's MIN_CONFIDENCE reasoning: calling a blurry doorway shadow "a
# face" is the expensive mistake here, not missing a real one for one frame.
MIN_DET_SCORE = 0.55


class FaceEmbedding:
    __slots__ = ("embedding", "bbox", "det_score")

    def __init__(self, embedding: np.ndarray, bbox: tuple, det_score: float):
        self.embedding = embedding      # L2-normalised, float32, shape (EMBEDDING_DIM,)
        self.bbox = bbox                # (x1, y1, x2, y2) in the image handed to analyze()
        self.det_score = det_score

    def __repr__(self):
        return f"<face det={self.det_score:.2f} box={self.bbox}>"


class FaceRecognizer:
    """InsightFace, loaded once and reused -- same lock-not-model-per-thread reasoning
    as detector.Detector, and for the same reason: several kiosk cameras sharing one
    process should queue rather than each load their own copy of the model.
    """

    def __init__(self, model_name: str = "buffalo_l", ctx_id: int | None = None,
                 det_size: tuple = (640, 640)):
        self.model_name = model_name
        self.det_size = det_size
        self._ctx_id = ctx_id
        self._lock = threading.Lock()
        self._app = None

    def _ensure_loaded(self):
        if self._app is not None:
            return
        from insightface.app import FaceAnalysis   # lazy: only present in .venv-vision

        ctx_id = self._ctx_id
        if ctx_id is None:
            # -1 = CPU. A handful of kiosk snapshots a minute has none of the
            # throughput pressure YOLO does, so CPU is the honest default; passing an
            # explicit ctx_id is how whoever deploys this opts into the GPU box's spare
            # CUDA context (the laptop's RTX 3060, per detector.py) instead.
            try:
                import onnxruntime
                providers = onnxruntime.get_available_providers()
            except Exception:
                providers = []
            ctx_id = 0 if "CUDAExecutionProvider" in providers else -1
        log.info("loading InsightFace %s (ctx_id=%d)", self.model_name, ctx_id)
        app = FaceAnalysis(name=self.model_name)
        app.prepare(ctx_id=ctx_id, det_size=self.det_size)
        self._app = app
        self._ctx_id = ctx_id
        log.info("face recognizer ready")

    def analyze(self, frame: np.ndarray) -> list[FaceEmbedding]:
        """Every face InsightFace's own detector finds in a full frame that's worth
        trusting (det_score >= MIN_DET_SCORE)."""
        self._ensure_loaded()
        with self._lock:
            faces = self._app.get(frame)
        out: list[FaceEmbedding] = []
        for f in faces:
            if float(f.det_score) < MIN_DET_SCORE:
                continue
            emb = getattr(f, "normed_embedding", None)
            if emb is None:
                # Older/newer insightface builds vary on whether the normalised
                # embedding is exposed directly -- normalise ourselves rather than
                # depend on a specific minor version's attribute surface.
                emb = f.embedding / (np.linalg.norm(f.embedding) + 1e-8)
            out.append(FaceEmbedding(np.asarray(emb, dtype=np.float32),
                                     tuple(int(v) for v in f.bbox), float(f.det_score)))
        return out

    def best_face(self, crop: np.ndarray) -> "FaceEmbedding | None":
        """The single most-confident face in a person crop -- what vision_watch.py
        actually wants: 'who is the person this box belongs to', not a census of
        everyone visible in a cropped image."""
        if crop is None or crop.size == 0:
            return None
        faces = self.analyze(crop)
        if not faces:
            return None
        return max(faces, key=lambda f: f.det_score)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Both vectors are expected pre-normalised (FaceEmbedding.embedding always is),
    so this is a plain dot product -- kept as a named function anyway so that
    assumption is documented at the one place every caller relies on it."""
    return float(np.dot(a, b))
