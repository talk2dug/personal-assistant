"""Face recognition for kiosk identity gating, on InsightFace/ArcFace.

Lazy-loaded the same way detector.py loads torch/ultralytics: insightface (and its
onnxruntime dependency) lives in .venv-vision only, never in the assistant's own shared
venv, so importing this module costs nothing until a camera actually needs it.

Two-stage pipeline, deliberately not one call:

  1. detector.py's YOLO already found "a person" and handed back a crop -- running
     InsightFace's own face detector (bundled with the buffalo_l model pack) on that
     crop, not the full frame, means the identity stage only ever runs over frames that
     already have a person in them. An empty room never reaches this module at all.
  2. Within the crop, InsightFace finds and aligns the actual face, then embeds it with
     ArcFace (512-d, L2-normalised). Comparison is cosine similarity against every
     stored sample for a known person, and the *best* one wins -- one photo of you in
     hallway light and one in kitchen light are genuinely far apart in embedding space,
     and taking the max instead of the mean is what keeps a single bad-lighting sample
     from dragging down a good match instead of just sitting there unused.

Fail-closed is the caller's job, not this module's: match() returns the honest best
result and its score, and never invents a default. A camera InsightFace can't see a face
in, or a face with no match above threshold, gets None back -- not a guess. See
identity_gate.py for where that None turns into an actual access decision.
"""
import json
import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

# ArcFace embeddings are L2-normalised; cosine similarity between the same identity
# across ordinary lighting/angle/expression changes is well clear of 0.4 in InsightFace's
# own published benchmarks, and different identities cluster well under 0.3. 0.38 sits in
# that gap with room either side, but this number MUST be recalibrated against real
# enrollment photos from the actual kiosk cameras before it gates anything real -- a
# threshold tuned on a bench, not the hallway camera at 11pm, is exactly the kind of
# number that looks right until it isn't. See config.py's identity_match_threshold.
DEFAULT_MATCH_THRESHOLD = 0.38

# InsightFace's own face-detection confidence floor. Below this the "face" is often a
# hand, a photo on a wall, or a face turned too far to embed reliably -- worth rejecting
# before it ever reaches the embedding/match step.
MIN_DET_SCORE = 0.55


class FaceObservation:
    """One face InsightFace found: where, how confidently detected, and its embedding."""

    __slots__ = ("embedding", "det_score", "box")

    def __init__(self, embedding: np.ndarray, det_score: float, box: tuple):
        self.embedding = embedding
        self.det_score = det_score
        self.box = box

    def __repr__(self):
        return f"<FaceObservation det={self.det_score:.2f} {self.box}>"


class FaceMatch:
    """The best known_people match for an observed face, and how confident it is."""

    __slots__ = ("person_key", "name", "score")

    def __init__(self, person_key: str, name: str, score: float):
        self.person_key = person_key
        self.name = name
        self.score = score

    def __repr__(self):
        return f"<FaceMatch {self.person_key} {self.score:.3f}>"


def embed_to_json(embedding: np.ndarray) -> str:
    """How an embedding is stored: a JSON list of floats, same shape known_people's
    'embeddings' column already expects (a JSON list of these)."""
    return json.dumps(np.asarray(embedding, dtype=np.float64).tolist())


def embedding_from_json(raw: str) -> np.ndarray:
    return np.asarray(json.loads(raw), dtype=np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


class FaceRecognizer:
    """InsightFace's buffalo_l pack, loaded once and reused.

    Same threading posture as detector.Detector: one shared model behind a lock rather
    than one per thread, since it holds GPU/ONNXRuntime session state that several
    cameras sharing one box should queue for rather than each allocate their own.
    Detection+embedding on a small crop is on the order of tens of milliseconds, so the
    lock is not a bottleneck at the frame rates a motion gate produces.
    """

    def __init__(self, model_name: str = "buffalo_l", device: str | None = None,
                 det_size: tuple[int, int] = (640, 640),
                 match_threshold: float = DEFAULT_MATCH_THRESHOLD):
        self.model_name = model_name
        self.det_size = det_size
        self.match_threshold = match_threshold
        self._device = device
        self._lock = threading.Lock()
        self._app = None

    def _ensure_loaded(self):
        if self._app is not None:
            return
        # lazy: insightface and onnxruntime live in .venv-vision only, same reasoning as
        # detector.py's torch import -- the assistant's own runtime must not depend on it.
        from insightface.app import FaceAnalysis

        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] \
            if self._device and self._device.startswith("cuda") else ["CPUExecutionProvider"]
        log.info("loading InsightFace %s ...", self.model_name)
        app = FaceAnalysis(name=self.model_name, providers=providers)
        app.prepare(ctx_id=0 if providers[0] == "CUDAExecutionProvider" else -1, det_size=self.det_size)
        self._app = app
        log.info("face recognizer ready (%s)", self.model_name)

    def observe(self, image: np.ndarray) -> list[FaceObservation]:
        """Every face InsightFace can find in `image` (a full frame or a person crop),
        confident enough to be worth comparing. Most callers want best_face() instead --
        this exists for enrollment and diagnostics, where seeing more than one face (or
        none) is worth knowing about rather than silently collapsed to one answer."""
        self._ensure_loaded()
        with self._lock:
            faces = self._app.get(image)
        out: list[FaceObservation] = []
        for f in faces:
            det_score = float(getattr(f, "det_score", 0.0))
            if det_score < MIN_DET_SCORE:
                continue
            box = tuple(int(v) for v in f.bbox.tolist())
            embedding = np.asarray(f.normed_embedding, dtype=np.float32)
            out.append(FaceObservation(embedding=embedding, det_score=det_score, box=box))
        return out

    def best_face(self, image: np.ndarray) -> FaceObservation | None:
        """The single most-confident face in the image, or None. What identity gating
        and enrollment both actually want -- multiple faces at a kiosk is exactly the
        case the caller should notice rather than have silently picked for it."""
        faces = self.observe(image)
        if not faces:
            return None
        return max(faces, key=lambda f: f.det_score)

    def match(self, embedding: np.ndarray, known_people: list[dict]) -> FaceMatch | None:
        """Best match across every known person's stored samples, or None if nothing
        clears match_threshold. `known_people` is vision.list_known_people()'s shape:
        each row's 'embeddings' is a JSON list of embedding vectors (vision.py never
        hands out raw numpy, so this accepts the JSON-strings-in-dicts shape directly).
        """
        best: FaceMatch | None = None
        for person in known_people:
            try:
                samples = json.loads(person.get("embeddings") or "[]")
            except (TypeError, ValueError):
                continue
            if not samples:
                continue
            score = max(cosine_similarity(embedding, s) for s in samples)
            if score >= self.match_threshold and (best is None or score > best.score):
                best = FaceMatch(person_key=person["key"], name=person["name"], score=score)
        return best
