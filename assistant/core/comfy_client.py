"""ComfyUI client — image and video generation on simrig.

ComfyUI has no "generate an image from this prompt" endpoint. It executes *graphs*: you
POST a workflow (a dict of nodes wired together), it queues it, and you poll history for
the outputs. That is powerful and completely unlike Ollama's request/response, so the
graph-building lives here rather than leaking into gpu_bridge.

Workflows are built in code, not loaded from exported JSON files. An exported ComfyUI
workflow is UI state — node positions, widget indices, frontend metadata — and it breaks
whenever the node set changes. The API-format graphs below are the minimum each model
actually needs, which is both smaller and far easier to reason about when something
fails.

Two pipelines, chosen Sept 2026 for a 16GB card:
  Z-Image Turbo (Alibaba Tongyi) for stills — open weights, no licence gate or HF token,
    BF16 fits 16GB, and it converges in ~8 steps rather than 30+.
  Wan 2.2 TI2V-5B (Apache 2.0) for video — text-to-video at ~8GB, leaving headroom.
"""
import json
import logging
import time
import uuid

import httpx

logger = logging.getLogger(__name__)

Z_IMAGE_MODEL = "z_image_turbo_bf16.safetensors"
Z_IMAGE_CLIP = "qwen_3_4b.safetensors"
Z_IMAGE_VAE = "ae.safetensors"

WAN_MODEL = "wan2.2_ti2v_5B_fp16.safetensors"
WAN_CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
WAN_VAE = "wan2.2_vae.safetensors"

DEFAULT_NEGATIVE = (
    "blurry, low quality, distorted, deformed, bad anatomy, extra limbs, watermark, "
    "signature, text artifacts, jpeg artifacts, muddy colours"
)


def build_image_workflow(
    prompt: str, negative: str = DEFAULT_NEGATIVE, width: int = 1024, height: int = 1024,
    steps: int = 8, cfg: float = 1.0, seed: int | None = None,
) -> dict:
    """Z-Image Turbo text-to-image, in ComfyUI's API graph format.

    cfg is 1.0 and steps ~8 on purpose: Turbo is a distilled model: normal guidance
    scales and step counts overcook it and cost time for a worse picture.
    """
    seed = seed if seed is not None else int(time.time() * 1000) % (2 ** 31)
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": Z_IMAGE_MODEL, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": Z_IMAGE_CLIP, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": Z_IMAGE_VAE}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 0]}},
        "6": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "KSampler",
              "inputs": {
                  "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler",
                  "scheduler": "simple", "denoise": 1.0,
                  "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0],
              }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "jarvis", "images": ["8", 0]}},
    }


def build_video_workflow(
    prompt: str, negative: str = DEFAULT_NEGATIVE, width: int = 704, height: int = 704,
    length: int = 49, steps: int = 20, cfg: float = 5.0, fps: int = 16, seed: int | None = None,
) -> dict:
    """Wan 2.2 TI2V-5B text-to-video.

    length is in frames, so 49 at 16fps is about three seconds — long enough for a
    product clip and short enough not to tie the card up for half an hour.
    """
    seed = seed if seed is not None else int(time.time() * 1000) % (2 ** 31)
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": WAN_MODEL, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": WAN_CLIP, "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": WAN_VAE}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 0]}},
        "6": {"class_type": "Wan22ImageToVideoLatent",
              "inputs": {"width": width, "height": height, "length": length,
                         "batch_size": 1, "vae": ["3", 0]}},
        "7": {"class_type": "KSampler",
              "inputs": {
                  "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "uni_pc",
                  "scheduler": "simple", "denoise": 1.0,
                  "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0],
              }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "CreateVideo", "inputs": {"images": ["8", 0], "fps": fps}},
        "10": {"class_type": "SaveVideo",
               "inputs": {"filename_prefix": "jarvis", "format": "mp4", "codec": "h264",
                          "video": ["9", 0]}},
    }


class ComfyClient:
    def __init__(self, host: str, timeout: int = 1800):
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.client_id = str(uuid.uuid4())

    def reachable(self) -> bool:
        try:
            return httpx.get(f"{self.host}/system_stats", timeout=5.0).status_code == 200
        except Exception:
            return False

    def free_memory(self) -> bool:
        """Asks ComfyUI to drop its resident models and free VRAM.

        Essential for the gaming/racing reservation: ComfyUI keeps the last pipeline in
        VRAM after a render, so a Wan video job leaves ~10GB of a 16GB card occupied
        indefinitely. Unloading Ollama alone is not enough — observed exactly that, with
        12.8GB still held after a video render.
        """
        try:
            resp = httpx.post(
                f"{self.host}/free", json={"unload_models": True, "free_memory": True}, timeout=60.0,
            )
            return resp.status_code < 400
        except Exception as e:
            logger.warning("could not free ComfyUI memory: %s", e)
            return False

    def object_info(self, node_type: str | None = None) -> dict:
        """What nodes this ComfyUI actually has. Used to check a workflow's nodes exist
        before queueing it, so a missing node is a clear error rather than a graph that
        silently produces nothing."""
        url = f"{self.host}/object_info" + (f"/{node_type}" if node_type else "")
        resp = httpx.get(url, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    def missing_nodes(self, workflow: dict) -> list[str]:
        try:
            available = set(self.object_info().keys())
        except Exception:
            return []
        return sorted({n["class_type"] for n in workflow.values() if n["class_type"] not in available})

    def queue(self, workflow: dict) -> str:
        resp = httpx.post(
            f"{self.host}/prompt", json={"prompt": workflow, "client_id": self.client_id}, timeout=60.0,
        )
        if resp.status_code == 400:
            # ComfyUI returns a structured validation error here; surfacing it verbatim
            # is the difference between a fixable message and "it didn't work".
            raise RuntimeError(f"ComfyUI rejected the workflow: {resp.text[:600]}")
        resp.raise_for_status()
        return resp.json()["prompt_id"]

    def wait(self, prompt_id: str, poll_seconds: float = 2.0) -> dict:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            resp = httpx.get(f"{self.host}/history/{prompt_id}", timeout=30.0)
            resp.raise_for_status()
            history = resp.json()
            if prompt_id in history:
                entry = history[prompt_id]
                status = entry.get("status") or {}
                if status.get("status_str") == "error" or not status.get("completed", True):
                    messages = status.get("messages") or []
                    raise RuntimeError(f"ComfyUI execution failed: {json.dumps(messages)[:600]}")
                return entry
            time.sleep(poll_seconds)
        raise RuntimeError(f"ComfyUI job did not finish within {self.timeout}s")

    @staticmethod
    def outputs(entry: dict) -> list[dict]:
        """Flattens the per-node outputs into a plain list of produced files."""
        files = []
        for node_output in (entry.get("outputs") or {}).values():
            for key in ("images", "gifs", "videos"):
                for item in node_output.get(key) or []:
                    if isinstance(item, dict) and item.get("filename"):
                        files.append(item)
        return files

    def download(self, item: dict) -> bytes:
        resp = httpx.get(
            f"{self.host}/view",
            params={"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                    "type": item.get("type", "output")},
            timeout=300.0,
        )
        resp.raise_for_status()
        return resp.content

    def generate(self, workflow: dict) -> list[dict]:
        """Runs a workflow to completion and returns [{filename, data}]."""
        missing = self.missing_nodes(workflow)
        if missing:
            raise RuntimeError(
                f"this ComfyUI is missing node types {missing} — the workflow can't run as written"
            )
        prompt_id = self.queue(workflow)
        logger.info("comfyui job queued: %s", prompt_id)
        entry = self.wait(prompt_id)
        produced = self.outputs(entry)
        if not produced:
            raise RuntimeError("ComfyUI finished but produced no output files")
        return [{"filename": item["filename"], "data": self.download(item)} for item in produced]
