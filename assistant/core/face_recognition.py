"""Face recognition for the house cameras: turns a YOLO person-crop into an identity.

Runs insightface (the 'buffalo_l' bundle: RetinaFace for detection, ArcFace for the
embedding) on the same RTX 3060 detector.py uses, in the same .venv-vision -- see that
module's docstring for why torch/insightface never touch the assistant's own runtime.
This module owns face *detection-within-a-crop* and *embedding*; matching an embedding
against known_people is vision.py's job (find_known_match), so this stays a pure
"pixels in, vector out" component the same shape as detector.py.

insightface's FaceAnalysis bundles its own face detector with the recognition model, so
it runs on the YOLO person crop rather than the full frame -- YOLO already found the
person cheaply, and asking insightface to re-scan an entire room frame for faces on
every tick would cost more for no benefit its own crop-local search doesn't already get
for free.
"""
import logging
import threading
import time

import numpy as np

log = logging.getLogger(__name__)


class Face:
    __slots__ = ("embedding", "bbox", "det_score")

    def __init__(self, embedding: list[float], bbox: tuple, det_score: float):
        self.embedding = embedding      # insightface's L2-normalised ArcFace embedding
        self.bbox = bbox                # (x1, y1, x2, y2) within the crop, in pixels
        self.det_score = det_score

    def __repr__(self):
        return f"<Face det={self.det_score:.2f} {self.bbox}>"


class FaceRecognizer:
    """insightface's FaceAnalysis app, loaded once and reused -- identical shape to
    detector.Detector: a lock rather than one model per thread (the model holds CUDA
    context, not just weights), lazy heavy imports so this module is safe to import
    anywhere, and a warm-up pass so the first real face of the day isn't the one that
    pays for CUDA kernel compilation.
    """

    def __init__(self, model_name: str = "buffalo_l", device: str | None = None,
                 det_size: tuple = (320, 320)):
        self.model_name = model_name
        self.det_size = det_size
        self._lock = threading.Lock()
        self._app = None
        self._device = device

    def _ensure_loaded(self):
        if self._app is not None:
            return
        from insightface.app import FaceAnalysis     # lazy: only present in .venv-vision
        import torch

        if self._device is None:
            self._device = "cuda:0" if torch.cuda.is_available() else "cpu"
        ctx_id = 0 if self._device.startswith("cuda") else -1
        providers = (["CUDAExecutionProvider", "CPUExecutionProvider"] if ctx_id == 0
                    else ["CPUExecutionProvider"])
        log.info("loading insightface %s on %s", self.model_name, self._device)
        t0 = time.time()
        self._app = FaceAnalysis(name=self.model_name, providers=providers)
        self._app.prepare(ctx_id=ctx_id, det_size=self.det_size)
        # Warm-up: same reasoning as Detector -- the first real inference otherwise eats
        # CUDA kernel compilation time and looks like a stall on the first person seen.
        self._app.get(np.zeros((self.det_size[1], self.det_size[0], 3), dtype=np.uint8))
        log.info("face recognizer ready in %.1fs", time.time() - t0)

    @property
    def device(self) -> str:
        self._ensure_loaded()
        return self._device or "cpu"

    def faces_in(self, crop: np.ndarray) -> list["Face"]:
        """Every face insightface finds in this crop, each with a 512-d embedding."""
        if crop is None or crop.size == 0:
            return []
        self._ensure_loaded()
        # Frames are RGB throughout vision.py (decode_jpeg converts on the way in);
        # insightface follows cv2's BGR convention, same as every other place this
        # codebase touches cv2 directly. Getting this backwards doesn't error -- it just
        # quietly degrades match quality, which is the kind of bug that's invisible until
        # someone asks why the house keeps not recognising them.
        bgr = crop[:, :, ::-1]
        with self._lock:
            found = self._app.get(bgr)
        out = []
        for f in found:
            out.append(Face(
                embedding=f.normed_embedding.tolist(),
                bbox=tuple(int(v) for v in f.bbox.tolist()),
                det_score=float(f.det_score),
            ))
        return out

    def best_face(self, crop: np.ndarray) -> "Face | None":
        """The single most useful face in a crop -- highest detection confidence, on the
        (reasonable) assumption a YOLO person box contains at most one face worth
        identifying. Returns None when nobody is facing the camera, which is routine
        (someone with their back turned) rather than an error."""
        faces = self.faces_in(crop)
        if not faces:
            return None
        return max(faces, key=lambda f: f.det_score)
