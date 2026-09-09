"""Face identity: turns a person-detection crop into 'which known person is this, if
any', using InsightFace embeddings matched against assistant.core.vision's known_people
table.

This is the "recognition" half vision.py's own module docstring describes as "a separate,
later stage" from detection: detector.Detector says *a person* is in the frame; this
module only ever runs on the crop of that detection, and only answers *which* person, if
it can. It never runs on a whole frame looking for faces on its own.

InsightFace (and the GPU it needs) live in .venv-vision alongside torch/ultralytics --
same reasoning as detector.py's lazy torch import, and for the same reason: the regular
assistant process (Telegram/web) must never need a GPU runtime just to answer a chat
message. This module is only ever imported by assistant/core/vision_worker.py, which in
turn is only ever run by assistant/vision_main.py under that separate virtualenv.
"""
import json
import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

# Cosine similarity below this is "not the same person". InsightFace's own buffalo_l
# embeddings typically put same-identity pairs above ~0.5 and different-identity pairs
# below ~0.3 for a frontal, reasonably lit face; this splits the difference so a sibling
# or a look-alike doesn't get read as the owner, while still tolerating the angle/light
# variation a camera on a wall actually sees.
MATCH_THRESHOLD = 0.42

# Below this InsightFace's own detector isn't confident there's a real face in the crop
# at all (motion blur, someone turned away, a hand in front of their face) -- treating
# a low-confidence "maybe a face" as a non-match is safer than feeding a garbage
# embedding into matching and risking a false accept.
MIN_DETECTION_SCORE = 0.5


class FaceEmbedder:
    """InsightFace, loaded once and reused -- same reasoning as detector.Detector: the
    model holds a GPU context, so several cameras share one instance behind a lock rather
    than each opening their own."""

    def __init__(self, device: str | None = None, det_size: tuple[int, int] = (640, 640),
                model_pack: str = "buffalo_l"):
        self._lock = threading.Lock()
        self._app = None
        self._device = device
        self._det_size = det_size
        self._model_pack = model_pack

    def _ensure_loaded(self) -> None:
        if self._app is not None:
            return
        from insightface.app import FaceAnalysis  # lazy: only present in .venv-vision
        import torch

        device = self._device or ("cuda" if torch.cuda.is_available() else "cpu")
        ctx_id = 0 if device.startswith("cuda") else -1
        log.info("loading InsightFace (%s) on ctx_id=%d", self._model_pack, ctx_id)
        app = FaceAnalysis(name=self._model_pack)
        app.prepare(ctx_id=ctx_id, det_size=self._det_size)
        self._app = app
        log.info("face embedder ready")

    def embed(self, crop: np.ndarray) -> tuple[list, tuple, float] | None:
        """The embedding for the largest, most confident face in `crop`.

        Returns (embedding, bbox, det_score), or None if no face clears
        MIN_DETECTION_SCORE. bbox/det_score matter beyond logging: a caller enrolling a
        new person or storing a fresh sample should refuse a low-confidence detection
        rather than poison known_people.embeddings with a bad vector.
        """
        self._ensure_loaded()
        with self._lock:
            faces = self._app.get(crop)
        candidates = [f for f in faces if float(getattr(f, "det_score", 0.0)) >= MIN_DETECTION_SCORE]
        if not candidates:
            return None
        face = max(candidates, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        embedding = face.normed_embedding
        return embedding.tolist(), tuple(int(v) for v in face.bbox), float(face.det_score)


def cosine_similarity(a: list, b: list) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    denom = (np.linalg.norm(va) * np.linalg.norm(vb)) or 1e-9
    return float(np.dot(va, vb) / denom)


def match_known_person(
    embedding: list, known_people: list[dict], threshold: float = MATCH_THRESHOLD,
) -> tuple[str, float] | None:
    """The best match for `embedding` across every enrolled person's stored samples, if
    any clears `threshold`. Returns (person_key, similarity_score) or None.

    Deliberately compares against every stored sample per person rather than a centroid:
    vision.py stores more than one embedding per person precisely because the same face
    at the door in daylight and indoors at night sit far apart in embedding space, and
    averaging them first would blur exactly the variation multiple samples exist to
    cover.
    """
    best_key, best_score = None, threshold
    for person in known_people:
        try:
            samples = json.loads(person.get("embeddings") or "[]")
        except (TypeError, ValueError):
            continue
        for sample in samples:
            if not sample:
                continue
            score = cosine_similarity(embedding, sample)
            if score > best_score:
                best_key, best_score = person["key"], score
    if best_key is None:
        return None
    return best_key, best_score
