"""GPU bridge to simrig — a shared, queued, model-routed inference resource.

Not everything an agent does needs a frontier model. Reading a product photo, tagging a
design, drafting throwaway variations, describing a mockup — those are jobs a local model
on simrig's RTX 5070 Ti does perfectly well and for free, while Claude's subscription
usage is better spent on research and reasoning. This is the seam that lets agents
offload that work.

Three things make it more than an HTTP client:

**A router.** Callers ask for a *task type* ("vision", "text"), not a model name. The
bridge decides which model serves it. That keeps model choice a config decision — models
move fast, and the agent code shouldn't have to change when a better one lands.

**A queue.** The card has 16GB. Two agents each pulling a different 9GB model at once
doesn't half the speed, it thrashes or OOMs. Jobs run one at a time unless there is
genuine VRAM headroom for a second, which is exactly how a shared piece of office
equipment works: you wait until it's free. Every job's state is persisted, so a UI can
show the queue and so a crash doesn't lose the record of what was running.

**A reservation.** simrig is also the user's gaming and racing machine. When he says he's
gaming, the bridge stops issuing work and unloads whatever is resident, and queued jobs
simply wait. Nothing about that should require him to remember to turn it back on for
the agents — it just resumes.

This module owns its own small schema rather than living in db.py or business_db.py: it
is infrastructure shared by any caller, not personal-assistant state and not
business-domain state. Same discipline as both — short-lived WAL connections, all access
behind functions.
"""
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS gpu_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    task_type TEXT NOT NULL,
    model TEXT,
    prompt TEXT,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    result TEXT,
    error TEXT,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gpu_jobs_status ON gpu_jobs(status, queued_at);

-- Single-row table holding the bridge's availability. A row rather than a config value
-- because it changes at runtime, from conversation ("I'm racing"), and must survive a
-- restart — otherwise a service restart mid-session would hand the GPU back to the
-- agents while the user is still in a race.
CREATE TABLE IF NOT EXISTS gpu_bridge_mode (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'available' CHECK (mode IN ('available', 'reserved')),
    reason TEXT,
    changed_at TEXT NOT NULL
);
INSERT OR IGNORE INTO gpu_bridge_mode (id, mode, reason, changed_at)
VALUES (1, 'available', NULL, datetime('now'));
"""

# Which model serves which kind of work. Task type is the contract with callers; the
# model behind it is a config decision, because this list ages in weeks.
#
# Chosen Sept 2026 for a 16GB card, from what Ollama actually ships:
#   vision   qwen3-vl:8b   — current Qwen vision family, strong OCR/document/photo
#                            understanding, ~6GB so it leaves room for a second job.
#   text     qwen3:8b      — already on the box, fine for tagging/summarising/classifying.
#   heavy    qwen3.6:27b   — already on the box at 16.5GB, so it necessarily runs alone.
# Image and video generation are NOT Ollama work — they need ComfyUI (Flux/Z-Image/Qwen-
# Image for stills, Wan 2.2 or LTX for video), which isn't installed on simrig yet. They
# are declared here so callers get an honest "not configured" rather than a crash, and so
# wiring a second engine later is a config change rather than a redesign.
#
# num_ctx is set explicitly on every route, and that is not optional. Ollama's default
# context is 4096 regardless of what the model supports, and a single 512px image costs
# ~4000 prompt tokens — so a vision call with the default silently leaves ~45 tokens for
# the answer and returns an empty response with done_reason "length". Confirmed exactly
# that against qwen3-vl:8b on a real product image before setting these. (Same failure
# mode bit this project once before, on the email-scan incident.)
DEFAULT_TASK_MODELS = {
    # num_predict is generous because these are thinking models: qwen3-vl reasons at
    # length in a `thinking` field before emitting the actual answer, so a tight cap
    # truncates it mid-reasoning and yields no answer at all. Verified on a real sticker
    # sheet — 1024 died mid-thought, 4096 completes.
    "vision": {"model": "qwen3-vl:8b", "engine": "ollama", "vram_gb": 7.0,
               "num_ctx": 16384, "num_predict": 4096},
    "text": {"model": "qwen3:8b", "engine": "ollama", "vram_gb": 6.0,
             "num_ctx": 8192, "num_predict": 2048},
    "heavy": {"model": "qwen3.6:27b", "engine": "ollama", "vram_gb": 17.0,
              "num_ctx": 16384, "num_predict": 2048},
    # ComfyUI, not Ollama — a different server on the same box and a different protocol
    # (queue a graph, poll history, fetch files). vram_gb is what each pipeline actually
    # holds, and both are large enough that they effectively run alone, which is the
    # correct behaviour: an image job and a video job together would thrash the card.
    "image_generation": {"engine": "comfyui", "pipeline": "image", "vram_gb": 13.0},
    "video_generation": {"engine": "comfyui", "pipeline": "video", "vram_gb": 14.0},
}

TOTAL_VRAM_GB = 16.0
# Leave room for the desktop compositor and Ollama's own overhead. Without this a second
# job gets admitted on paper and then OOMs on the card.
VRAM_SAFETY_GB = 2.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_bridge_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


class GPUBridge:
    def __init__(
        self, db_path: str, host: str, task_models: dict | None = None,
        max_concurrent: int = 2, poll_seconds: int = 5, request_timeout: int = 600,
        comfy=None, media_dir: str | None = None,
    ):
        self.db_path = db_path
        self.host = host.rstrip("/")
        self.comfy = comfy
        self.media_dir = media_dir
        self.task_models = {**DEFAULT_TASK_MODELS, **(task_models or {})}
        self.max_concurrent = max_concurrent
        self.poll_seconds = poll_seconds
        self.request_timeout = request_timeout
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._running_lock = threading.Lock()
        self._running: set[int] = set()

    # -- availability --------------------------------------------------------

    def get_mode(self) -> dict:
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute("SELECT * FROM gpu_bridge_mode WHERE id = 1").fetchone()
            return dict(row) if row else {"mode": "available", "reason": None}

    def set_mode(self, mode: str, reason: str | None = None) -> dict:
        """'reserved' hands simrig back to the user; 'available' returns it to the agents.

        Reserving also unloads whatever is resident. Ollama holds a model in VRAM after a
        request (keep_alive), so without an explicit unload the user would start a race
        with several GB of the card still occupied by a model nobody is using.
        """
        if mode not in ("available", "reserved"):
            raise ValueError("mode must be 'available' or 'reserved'")
        with closing(_connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE gpu_bridge_mode SET mode = ?, reason = ?, changed_at = ? WHERE id = 1",
                (mode, reason, _now()),
            )
            conn.commit()
        if mode == "reserved":
            freed = self.unload_all()
            logger.info("GPU bridge reserved (%s); unloaded %s", reason or "no reason given", freed)
        else:
            logger.info("GPU bridge available again")
        return self.get_mode()

    def is_available(self) -> bool:
        return self.get_mode()["mode"] == "available"

    # -- simrig introspection ------------------------------------------------

    def loaded_models(self) -> list[dict]:
        try:
            resp = httpx.get(f"{self.host}/api/ps", timeout=10.0)
            resp.raise_for_status()
            return [
                {"model": m["name"], "vram_gb": round(m.get("size_vram", 0) / 1e9, 1)}
                for m in resp.json().get("models") or []
            ]
        except Exception as e:
            logger.debug("gpu bridge /api/ps failed: %s", e)
            return []

    def installed_models(self) -> list[dict]:
        try:
            resp = httpx.get(f"{self.host}/api/tags", timeout=15.0)
            resp.raise_for_status()
            return [
                {"model": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1)}
                for m in resp.json().get("models") or []
            ]
        except Exception:
            return []

    def reachable(self) -> bool:
        try:
            return httpx.get(f"{self.host}/api/version", timeout=5.0).status_code == 200
        except Exception:
            return False

    def unload_all(self) -> list[str]:
        """Frees the card: evicts Ollama's resident models and ComfyUI's too.

        Both halves matter. ComfyUI keeps its last pipeline in VRAM after a render, so
        unloading only Ollama left 12.8GB of a 16GB card occupied after a video job —
        useless for someone trying to start a race.
        """
        unloaded = []
        for entry in self.loaded_models():
            try:
                httpx.post(
                    f"{self.host}/api/generate",
                    json={"model": entry["model"], "keep_alive": 0}, timeout=60.0,
                )
                unloaded.append(entry["model"])
            except Exception as e:
                logger.warning("could not unload %s: %s", entry["model"], e)
        if self.comfy is not None and self.comfy.free_memory():
            unloaded.append("comfyui")
        return unloaded

    def free_vram_gb(self) -> float:
        """Headroom, derived from what Ollama says it has resident.

        Deliberately not nvidia-smi: that needs SSH, and what matters for admitting
        another job is how much of the card Ollama is already holding, which /api/ps
        answers directly and cheaply.
        """
        used = sum(m["vram_gb"] for m in self.loaded_models())
        return max(0.0, TOTAL_VRAM_GB - used)

    # -- queue ---------------------------------------------------------------

    def submit(
        self, agent: str, task_type: str, prompt: str, images: list[str] | None = None,
        options: dict | None = None,
    ) -> int:
        payload = {"images": images or [], "options": options or {}}
        route = self.task_models.get(task_type) or {}
        with closing(_connect(self.db_path)) as conn:
            cur = conn.execute(
                """INSERT INTO gpu_jobs (agent, task_type, model, prompt, payload, queued_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (agent, task_type, route.get("model"), prompt, json.dumps(payload), _now()),
            )
            conn.commit()
            return cur.lastrowid

    def job(self, job_id: int) -> dict | None:
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute("SELECT * FROM gpu_jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def jobs(self, status: str | None = None, limit: int = 25) -> list[dict]:
        query = "SELECT * FROM gpu_jobs"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with closing(_connect(self.db_path)) as conn:
            return [dict(r) for r in conn.execute(query, params)]

    def cancel(self, job_id: int) -> bool:
        with closing(_connect(self.db_path)) as conn:
            cur = conn.execute(
                "UPDATE gpu_jobs SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'queued'",
                (_now(), job_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def status(self) -> dict:
        """Everything a status page or Jarvis needs to describe the bridge in one call."""
        mode = self.get_mode()
        loaded = self.loaded_models()
        with closing(_connect(self.db_path)) as conn:
            counts = {
                row["status"]: row["n"] for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM gpu_jobs GROUP BY status")
            }
        return {
            "host": self.host,
            "reachable": self.reachable(),
            "mode": mode["mode"],
            "reason": mode.get("reason"),
            "since": mode.get("changed_at"),
            "loaded_models": loaded,
            "vram_used_gb": round(sum(m["vram_gb"] for m in loaded), 1),
            "vram_free_gb": round(self.free_vram_gb(), 1),
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "done": counts.get("done", 0),
            "failed": counts.get("failed", 0),
            "waiting_jobs": self.jobs(status="queued", limit=10),
            "comfyui": {
                "configured": self.comfy is not None,
                "reachable": bool(self.comfy and self.comfy.reachable()),
                "host": getattr(self.comfy, "host", None),
            },
            "task_routing": {
                task: {
                    "model": route.get("model") or route.get("pipeline"),
                    "engine": route.get("engine"),
                    "available": (
                        bool(route.get("model")) if route.get("engine") == "ollama"
                        else bool(self.comfy and self.comfy.reachable())
                    ),
                }
                for task, route in self.task_models.items()
            },
        }

    # -- execution -----------------------------------------------------------

    def _can_admit(self, route: dict) -> bool:
        """Whether there's room for this job right now.

        A job whose model is already resident costs no extra VRAM, so it's always
        admissible — that's the common case of several vision jobs in a row and it would
        be daft to serialise them behind a headroom check they don't need.
        """
        # Counted from the database, not an in-process set: the Telegram process runs the
        # worker while the web process can also drive a synchronous job, and concurrency
        # has to mean "jobs on the card" across both, not per process. Claiming is already
        # cross-process safe via the conditional UPDATE in _claim_next.
        with closing(_connect(self.db_path)) as conn:
            running = conn.execute("SELECT COUNT(*) AS n FROM gpu_jobs WHERE status = 'running'").fetchone()["n"]
        if running >= self.max_concurrent:
            return False
        if running == 0:
            return True
        model = route.get("model")
        if any(m["model"] == model for m in self.loaded_models()):
            return True
        return self.free_vram_gb() - route.get("vram_gb", 8.0) >= VRAM_SAFETY_GB

    def _execute(self, job: dict) -> None:
        job_id = job["id"]
        route = self.task_models.get(job["task_type"]) or {}
        started = time.time()
        try:
            payload = json.loads(job["payload"] or "{}")

            if route.get("engine") == "comfyui":
                result = self._execute_comfy(job, route, payload)
                with closing(_connect(self.db_path)) as conn:
                    conn.execute(
                        """UPDATE gpu_jobs SET status = 'done', result = ?, finished_at = ?, duration_ms = ?
                           WHERE id = ?""",
                        (result, _now(), int((time.time() - started) * 1000), job_id),
                    )
                    conn.commit()
                logger.info("gpu job %s (%s/%s) done in %.1fs", job_id, job["agent"], job["task_type"],
                            time.time() - started)
                return

            if route.get("engine") != "ollama" or not route.get("model"):
                raise RuntimeError(f"no engine is configured for task type '{job['task_type']}'")
            body = {
                "model": route["model"],
                "prompt": job["prompt"],
                "stream": False,
                # Short keep_alive on purpose: this is a shared machine the user games on.
                # Holding a model resident for hours after one tagging job is exactly the
                # 9GB of idle VRAM that prompted building this.
                "keep_alive": "5m",
                "options": {
                    "temperature": 0.2,
                    "num_ctx": route.get("num_ctx", 8192),
                    "num_predict": route.get("num_predict", 1024),
                    **(payload.get("options") or {}),
                },
            }
            if payload.get("images"):
                body["images"] = payload["images"]

            resp = httpx.post(f"{self.host}/api/generate", json=body, timeout=self.request_timeout)
            resp.raise_for_status()
            data = resp.json()
            result = (data.get("response") or "").strip()
            if not result:
                # Thinking models (qwen3-vl among them) split output into `thinking` and
                # `response`. If a cap is hit mid-reasoning the answer never materialises
                # and only `thinking` has content — surface it rather than returning
                # nothing, and say plainly that it was truncated.
                thinking = (data.get("thinking") or "").strip()
                if thinking:
                    truncated = data.get("done_reason") == "length"
                    result = thinking + ("\n\n[truncated: hit the output limit]" if truncated else "")
                else:
                    raise RuntimeError(
                        f"model returned nothing (done_reason={data.get('done_reason')}, "
                        f"prompt_tokens={data.get('prompt_eval_count')})"
                    )

            with closing(_connect(self.db_path)) as conn:
                conn.execute(
                    """UPDATE gpu_jobs SET status = 'done', result = ?, finished_at = ?, duration_ms = ?
                       WHERE id = ?""",
                    (result, _now(), int((time.time() - started) * 1000), job_id),
                )
                conn.commit()
            logger.info("gpu job %s (%s/%s) done in %.1fs", job_id, job["agent"], job["task_type"],
                        time.time() - started)
        except Exception as e:
            logger.warning("gpu job %s failed: %s", job_id, e)
            with closing(_connect(self.db_path)) as conn:
                conn.execute(
                    """UPDATE gpu_jobs SET status = 'failed', error = ?, finished_at = ?, duration_ms = ?
                       WHERE id = ?""",
                    (str(e), _now(), int((time.time() - started) * 1000), job_id),
                )
                conn.commit()
        finally:
            with self._running_lock:
                self._running.discard(job_id)

    def _execute_comfy(self, job: dict, route: dict, payload: dict) -> str:
        """Runs an image or video generation and writes the files to media_dir.

        Returns the saved paths rather than the bytes: generated media is measured in
        megabytes and belongs on disk, not in a SQLite result column that a digest or a
        chat turn might try to read back.
        """
        if self.comfy is None:
            raise RuntimeError(
                "ComfyUI is not configured — set comfy_host in config to enable image and video generation"
            )
        if not self.comfy.reachable():
            raise RuntimeError(f"ComfyUI is not responding at {self.comfy.host}")

        from . import comfy_client

        options = payload.get("options") or {}
        if route.get("pipeline") == "video":
            workflow = comfy_client.build_video_workflow(job["prompt"], **options)
        else:
            workflow = comfy_client.build_image_workflow(job["prompt"], **options)

        produced = self.comfy.generate(workflow)

        import pathlib

        out_dir = pathlib.Path(self.media_dir or "generated")
        out_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for item in produced:
            # Job id in the name so a file is always traceable back to the request that
            # made it, and so two jobs can't collide on ComfyUI's own filename counter.
            target = out_dir / f"job{job['id']}_{item['filename']}"
            target.write_bytes(item["data"])
            saved.append(str(target))
        return json.dumps({"files": saved, "count": len(saved)})

    def _claim_next(self) -> dict | None:
        """Takes the oldest queued job this bridge has room for, marking it running in the
        same transaction so two workers can't claim the same one."""
        with closing(_connect(self.db_path)) as conn:
            for row in conn.execute(
                "SELECT * FROM gpu_jobs WHERE status = 'queued' ORDER BY id LIMIT 10"
            ).fetchall():
                job = dict(row)
                route = self.task_models.get(job["task_type"]) or {}
                if not self._can_admit(route):
                    continue
                cur = conn.execute(
                    "UPDATE gpu_jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                    (_now(), job["id"]),
                )
                conn.commit()
                if cur.rowcount:
                    return job
        return None

    def tick(self) -> int:
        """One scheduling pass. Returns how many jobs it started."""
        if not self.is_available():
            return 0
        started = 0
        while True:
            job = self._claim_next()
            if job is None:
                return started
            with self._running_lock:
                self._running.add(job["id"])
            threading.Thread(target=self._execute, args=(job,), daemon=True,
                             name=f"gpu-job-{job['id']}").start()
            started += 1

    def start_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return

        def _loop():
            while not self._stop.wait(self.poll_seconds):
                try:
                    self.tick()
                except Exception:
                    logger.exception("gpu bridge worker tick failed")

        self._stop.clear()
        self._worker = threading.Thread(target=_loop, daemon=True, name="gpu-bridge-worker")
        self._worker.start()
        logger.info("GPU bridge worker started against %s", self.host)

    def stop_worker(self) -> None:
        self._stop.set()

    # -- convenience ---------------------------------------------------------

    def run_sync(
        self, agent: str, task_type: str, prompt: str, images: list[str] | None = None,
        options: dict | None = None, timeout: int = 900,
    ) -> dict:
        """Submits and waits. For callers that genuinely need the answer inline.

        Still goes through the queue rather than jumping it — the whole point of the queue
        is that it holds even when someone is in a hurry, and reservations are respected.
        """
        job_id = self.submit(agent, task_type, prompt, images, options)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.tick()
            job = self.job(job_id)
            if job and job["status"] in ("done", "failed", "cancelled"):
                return job
            time.sleep(2)
        return {"id": job_id, "status": "timeout",
                "error": f"job did not finish within {timeout}s (it stays queued)"}
