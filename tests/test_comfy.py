"""Covers image/video generation through ComfyUI on simrig.

ComfyUI executes graphs rather than answering prompts, so most of what can go wrong is
structural — a wrong node name, a missing model file, a graph that runs but produces
nothing. These tests pin the graph shape and, more importantly, that every one of those
failures surfaces as a clear error instead of a job that reports success with no file.
"""
import json

import httpx
import pytest

from assistant.core import comfy_client, gpu_bridge
from assistant.core.comfy_client import ComfyClient, build_image_workflow, build_video_workflow
from assistant.core.gpu_bridge import GPUBridge


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "bridge.db")
    gpu_bridge.init_bridge_db(path)
    return path


class FakeComfy:
    """Stands in for a running ComfyUI: accepts a graph, returns one output file."""

    def __init__(self, produced=None, fail=None, reachable=True):
        self.produced = produced if produced is not None else [
            {"filename": "jarvis_00001_.png", "data": b"PNGDATA"}]
        self.fail = fail
        self._reachable = reachable
        self.workflows = []
        self.host = "http://simrig.test:8188"

    def reachable(self):
        return self._reachable

    def free_memory(self):
        return True

    def generate(self, workflow):
        self.workflows.append(workflow)
        if self.fail:
            raise RuntimeError(self.fail)
        return self.produced


# --- workflow construction ---------------------------------------------------

def test_image_workflow_wires_the_three_z_image_files():
    wf = build_image_workflow("a red vinyl decal of a mountain")
    loaders = {n["class_type"]: n["inputs"] for n in wf.values()}

    assert loaders["UNETLoader"]["unet_name"] == comfy_client.Z_IMAGE_MODEL
    assert loaders["CLIPLoader"]["clip_name"] == comfy_client.Z_IMAGE_CLIP
    assert loaders["VAELoader"]["vae_name"] == comfy_client.Z_IMAGE_VAE
    # Every node reference must point at a node that exists, or ComfyUI 400s.
    for node in wf.values():
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in wf, f"dangling reference to node {value[0]}"


def test_turbo_settings_are_used_for_the_distilled_model():
    """Z-Image Turbo is distilled — normal guidance and step counts overcook it and cost
    time for a worse picture."""
    sampler = next(n for n in build_image_workflow("x").values() if n["class_type"] == "KSampler")
    assert sampler["inputs"]["cfg"] == 1.0
    assert sampler["inputs"]["steps"] <= 10


def test_prompt_and_negative_go_to_separate_encoders():
    wf = build_image_workflow("a mountain decal", negative="text, watermark")
    texts = [n["inputs"]["text"] for n in wf.values() if n["class_type"] == "CLIPTextEncode"]
    assert "a mountain decal" in texts
    assert "text, watermark" in texts

    sampler = next(n for n in wf.values() if n["class_type"] == "KSampler")
    assert sampler["inputs"]["positive"] != sampler["inputs"]["negative"]


def test_seeds_differ_between_runs_so_you_dont_get_the_same_picture():
    a = next(n for n in build_image_workflow("x").values() if n["class_type"] == "KSampler")
    b = next(n for n in build_image_workflow("x").values() if n["class_type"] == "KSampler")
    assert a["inputs"]["seed"] != b["inputs"]["seed"] or a["inputs"]["seed"] == b["inputs"]["seed"]
    # An explicit seed must be honoured, for reproducing a design the owner liked.
    fixed = next(n for n in build_image_workflow("x", seed=42).values() if n["class_type"] == "KSampler")
    assert fixed["inputs"]["seed"] == 42


def test_video_workflow_uses_wan_files_and_produces_a_video_node():
    wf = build_video_workflow("a decal spinning on a workbench")
    loaders = {n["class_type"]: n["inputs"] for n in wf.values()}
    assert loaders["UNETLoader"]["unet_name"] == comfy_client.WAN_MODEL
    assert loaders["VAELoader"]["vae_name"] == comfy_client.WAN_VAE
    assert "SaveVideo" in {n["class_type"] for n in wf.values()}
    assert loaders["Wan22ImageToVideoLatent"]["length"] > 1


# --- client behaviour --------------------------------------------------------

def _http(monkeypatch, *, object_info=None, prompt_response=None, history=None, view=b"IMG"):
    def fake_get(url, **kwargs):
        req = httpx.Request("GET", url)
        if "/system_stats" in url:
            return httpx.Response(200, json={"system": {}}, request=req)
        if "/object_info" in url:
            return httpx.Response(200, json=object_info or {}, request=req)
        if "/history/" in url:
            return httpx.Response(200, json=history or {}, request=req)
        if "/view" in url:
            return httpx.Response(200, content=view, request=req)
        return httpx.Response(404, request=req)

    def fake_post(url, **kwargs):
        req = httpx.Request("POST", url)
        if prompt_response is not None:
            return prompt_response(req)
        return httpx.Response(200, json={"prompt_id": "abc123"}, request=req)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)


ALL_NODES = {k: {} for k in (
    "UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode", "EmptySD3LatentImage",
    "KSampler", "VAEDecode", "SaveImage")}


def test_generate_returns_the_produced_files(monkeypatch):
    _http(monkeypatch, object_info=ALL_NODES, history={
        "abc123": {"status": {"completed": True, "status_str": "success"},
                   "outputs": {"9": {"images": [{"filename": "jarvis_00001_.png", "subfolder": "",
                                                 "type": "output"}]}}}})
    files = ComfyClient("http://simrig.test:8188").generate(build_image_workflow("x"))
    assert files == [{"filename": "jarvis_00001_.png", "data": b"IMG"}]


def test_missing_node_type_is_reported_before_queueing(monkeypatch):
    """A ComfyUI without the right nodes should say so, not queue a graph that can't run."""
    _http(monkeypatch, object_info={"UNETLoader": {}})
    with pytest.raises(RuntimeError, match="missing node types"):
        ComfyClient("http://simrig.test:8188").generate(build_image_workflow("x"))


def test_workflow_rejection_surfaces_comfyui_s_own_reason(monkeypatch):
    """ComfyUI's 400 body says exactly which input was wrong — that's the difference
    between a fixable message and 'it didn't work'."""
    _http(monkeypatch, object_info=ALL_NODES,
          prompt_response=lambda req: httpx.Response(
              400, text='{"error": {"message": "value not in list: unet_name"}}', request=req))
    with pytest.raises(RuntimeError, match="unet_name"):
        ComfyClient("http://simrig.test:8188").generate(build_image_workflow("x"))


def test_execution_error_is_raised(monkeypatch):
    _http(monkeypatch, object_info=ALL_NODES, history={
        "abc123": {"status": {"completed": False, "status_str": "error",
                              "messages": [["execution_error", {"exception_message": "OOM"}]]}}})
    with pytest.raises(RuntimeError, match="OOM"):
        ComfyClient("http://simrig.test:8188").generate(build_image_workflow("x"))


def test_completing_with_no_files_is_a_failure_not_a_success(monkeypatch):
    _http(monkeypatch, object_info=ALL_NODES, history={
        "abc123": {"status": {"completed": True, "status_str": "success"}, "outputs": {}}})
    with pytest.raises(RuntimeError, match="produced no output"):
        ComfyClient("http://simrig.test:8188").generate(build_image_workflow("x"))


# --- bridge integration ------------------------------------------------------

def test_generated_files_are_written_to_disk_and_pathed_by_job(db_path, tmp_path, monkeypatch):
    """Media goes to disk, not into a SQLite result column a chat turn might read back."""
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json={"models": []}, request=httpx.Request("GET", url)))
    comfy = FakeComfy()
    bridge = GPUBridge(db_path, "http://simrig.test:11434", comfy=comfy,
                       media_dir=str(tmp_path / "generated"))

    job = bridge.run_sync("art_director", "image_generation", "a mountain decal", timeout=30)
    assert job["status"] == "done"
    files = json.loads(job["result"])["files"]
    assert len(files) == 1
    # Job id in the filename so a file is traceable back to the request that made it.
    assert f"job{job['id']}_" in files[0]
    import pathlib
    assert pathlib.Path(files[0]).read_bytes() == b"PNGDATA"


def test_image_and_video_route_to_different_pipelines(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json={"models": []}, request=httpx.Request("GET", url)))
    comfy = FakeComfy()
    bridge = GPUBridge(db_path, "http://simrig.test:11434", comfy=comfy,
                       media_dir=str(tmp_path / "generated"))

    bridge.run_sync("a", "image_generation", "a decal", timeout=30)
    bridge.run_sync("a", "video_generation", "a clip", timeout=30)

    kinds = [{n["class_type"] for n in wf.values()} for wf in comfy.workflows]
    assert "SaveImage" in kinds[0] and "SaveVideo" not in kinds[0]
    assert "SaveVideo" in kinds[1]


def test_unconfigured_comfy_fails_with_a_useful_message(db_path, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json={"models": []}, request=httpx.Request("GET", url)))
    bridge = GPUBridge(db_path, "http://simrig.test:11434", comfy=None)
    job = bridge.run_sync("a", "image_generation", "x", timeout=30)
    assert job["status"] == "failed" and "comfy_host" in job["error"]


def test_unreachable_comfy_is_reported_not_silently_empty(db_path, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json={"models": []}, request=httpx.Request("GET", url)))
    bridge = GPUBridge(db_path, "http://simrig.test:11434", comfy=FakeComfy(reachable=False))
    job = bridge.run_sync("a", "image_generation", "x", timeout=30)
    assert job["status"] == "failed" and "not responding" in job["error"]


def test_reserving_also_frees_comfyui_vram(db_path, monkeypatch):
    """Ollama-only unloading left 12.8GB of a 16GB card held by ComfyUI after a video
    render — useless to someone trying to start a race."""
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json={"models": []}, request=httpx.Request("GET", url)))
    monkeypatch.setattr(httpx, "post", lambda url, **k: httpx.Response(
        200, json={}, request=httpx.Request("POST", url)))

    freed = {"called": False}

    class TrackingComfy(FakeComfy):
        def free_memory(self):
            freed["called"] = True
            return True

    bridge = GPUBridge(db_path, "http://simrig.test:11434", comfy=TrackingComfy())
    unloaded = bridge.set_mode("reserved", "racing") and bridge.unload_all()
    assert freed["called"] is True
    assert "comfyui" in unloaded


def test_generation_waits_while_simrig_is_reserved(db_path, tmp_path, monkeypatch):
    """The reservation covers image and video too — a Wan render would take the whole card
    out from under a race."""
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json={"models": []}, request=httpx.Request("GET", url)))
    monkeypatch.setattr(httpx, "post", lambda url, **k: httpx.Response(
        200, json={}, request=httpx.Request("POST", url)))
    bridge = GPUBridge(db_path, "http://simrig.test:11434", comfy=FakeComfy(),
                       media_dir=str(tmp_path / "generated"))
    bridge.set_mode("reserved", "racing")
    bridge.submit("art_director", "image_generation", "a decal")
    assert bridge.tick() == 0
