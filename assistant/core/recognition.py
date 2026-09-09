"""Face recognition: turns a person crop into an embedding, and matches it against
known_people.

insightface does the heavy lifting (detection + alignment + embedding via its buffalo_l
model). Loaded lazily and lives in .venv-vision alongside torch/ultralytics -- the
assistant's own runtime must never import this module's third-party dependencies
eagerly, same discipline detector.py already follows for torch.

Matching is cosine similarity against every stored embedding for a person, taking the
best rather than an average: a person enrolled from a daylight photo and a night photo
should match on whichever one the current frame actually resembles, not a blend of both
that resembles neither.
"""
import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

# Cosine similarity at/above this counts as the same person. insightface's own docs and
# buffalo_l's typical operating point put a genuine match comfortably above 0.5. This is
# a starting point, not a tuned constant -- it wants real data from this house's actual
# cameras before anyone trusts it to gate anything sensitive.
MATCH_THRESHOLD = 0.50


class FaceEmbedder:
    """insightface's FaceAnalysis app, loaded once and reused -- same one-lock-per-GPU-
    context discipline as detector.Detector, since it also holds CUDA state."""

    def __init__(self, model_name: str = "buffalo_l", device: str | None = None,
                 det_size: tuple = (640, 640)):
        self.model_name = model_name
        self.det_size = det_size
        self._device = device
        self._lock = threading.Lock()
        self._app = None

    def _ensure_loaded(self):
        if self._app is not None:
            return
        from insightface.app import FaceAnalysis   # lazy: lives in .venv-vision only
        import torch

        if self._device is None:
            self._device = "cuda:0" if torch.cuda.is_available() else "cpu"
        ctx_id = 0 if self._device.startswith("cuda") else -1
        log.info("loading insightface %s (ctx_id=%d)", self.model_name, ctx_id)
        self._app = FaceAnalysis(name=self.model_name)
        self._app.prepare(ctx_id=ctx_id, det_size=self.det_size)
        log.info("face embedder ready")

    def embed(self, crop: np.ndarray) -> np.ndarray | None:
        """The single best face in a crop, as a normalised embedding -- or None if no
        face was actually findable in it (a person detected from behind, say)."""
        self._ensure_loaded()
        if crop is None or crop.size == 0:
            return None
        with self._lock:
            # insightface wants BGR; everything upstream in this codebase (detector.py,
            # vision.py's decode_jpeg) works in RGB, so flip at the boundary rather than
            # push BGR into modules that have no other reason to know about it.
            faces = self._app.get(crop[:, :, ::-1])
        if not faces:
            return None
        # The largest face in the crop, on the assumption a person-detector crop has at
        # most one person worth naming in it; a second, smaller face is someone in the
        # background rather than the subject of the detection.
        best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        vec = best.normed_embedding
        return vec.astype(np.float32) if vec is not None else None


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def best_match(embedding: np.ndarray, known_people: list[dict]) -> tuple[dict | None, float]:
    """The best-matching known person for one embedding, and the similarity score.
    known_people rows carry 'embeddings' as a JSON list of float lists (vision.py's
    schema) -- decoded here rather than asking every caller to know that encoding."""
    import json

    best_person, best_score = None, 0.0
    for person in known_people:
        try:
            stored = json.loads(person.get("embeddings") or "[]")
        except (ValueError, TypeError):
            continue
        for vec in stored:
            score = cosine_similarity(embedding, np.asarray(vec, dtype=np.float32))
            if score > best_score:
                best_score, best_person = score, person
    return best_person, best_score


def identify(embedding: np.ndarray, known_people: list[dict]) -> tuple[dict | None, float]:
    """best_match, gated by MATCH_THRESHOLD. Callers that want the raw score regardless
    of whether it clears the bar (e.g. to log a near-miss) should call best_match
    directly instead."""
    person, score = best_match(embedding, known_people)
    if person is not None and score >= MATCH_THRESHOLD:
        return person, score
    return None, score
