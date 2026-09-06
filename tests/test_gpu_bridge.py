"""Covers the simrig GPU bridge: routing, queueing, VRAM admission, and the reservation
that hands the card back when the owner is gaming or racing.

The reservation tests matter most. simrig is his gaming and sim-racing machine before
it's an inference server, and an agent that keeps a 9GB model resident mid-race is worse
than one that never ran at all.
"""
import json

import httpx
import pytest

from assistant.core import gpu_bridge
from assistant.core.gpu_bridge import GPUBridge


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "bridge.db")
    gpu_bridge.init_bridge_db(path)
    return path


@pytest.fixture
def bridge(db_path):
    return GPUBridge(db_path, "http://simrig.test:11434", poll_seconds=1)


class FakeOllama:
    """Records calls and answers the endpoints the bridge uses."""

    def __init__(self, loaded=None, response="ok"):
        self.loaded = loaded or []
        self.response = response
        self.generate_calls = []
        self.unloaded = []

    def install(self, monkeypatch):
        def fake_get(url, **kwargs):
            if url.endswith("/api/ps"):
                body = {"models": [{"name": n, "size_vram": int(g * 1e9)} for n, g in self.loaded]}
            elif url.endswith("/api/tags"):
                body = {"models": [{"name": "qwen3-vl:8b", "size": int(6.1e9)},
                                   {"name": "qwen3:8b", "size": int(5.2e9)}]}
            else:
                body = {"version": "0.33.2"}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

        def fake_post(url, **kwargs):
            payload = kwargs.get("json") or {}
            if payload.get("keep_alive") == 0:
                self.unloaded.append(payload["model"])
                self.loaded = [m for m in self.loaded if m[0] != payload["model"]]
                return httpx.Response(200, json={}, request=httpx.Request("POST", url))
            self.generate_calls.append(payload)
            return httpx.Response(200, json={"response": self.response},
                                  request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "post", fake_post)
        return self


# --- routing -----------------------------------------------------------------

def test_callers_ask_for_a_task_not_a_model(bridge, monkeypatch):
    """The whole point of the router: agents name the work, config names the model."""
    fake = FakeOllama().install(monkeypatch)
    job = bridge.run_sync("art_director", "vision", "describe this", images=["b64"])

    assert job["status"] == "done"
    assert fake.generate_calls[0]["model"] == "qwen3-vl:8b"
    assert fake.generate_calls[0]["images"] == ["b64"]


def test_model_choice_is_config_overridable(db_path, monkeypatch):
    """Models age in weeks — swapping one must not touch agent code."""
    fake = FakeOllama().install(monkeypatch)
    bridge = GPUBridge(db_path, "http://simrig.test:11434",
                       task_models={"vision": {"model": "newer-vl:12b", "engine": "ollama", "vram_gb": 9.0}})
    bridge.run_sync("agent", "vision", "look")
    assert fake.generate_calls[0]["model"] == "newer-vl:12b"


def test_unconfigured_engine_fails_honestly(bridge, monkeypatch):
    """Image generation isn't Ollama work and ComfyUI isn't installed — the caller must
    be told that, not handed a silent empty result."""
    FakeOllama().install(monkeypatch)
    job = bridge.run_sync("art_director", "image_generation", "a decal")
    assert job["status"] == "failed"
    assert "ComfyUI" in job["error"]


def test_context_is_set_large_enough_for_an_image(bridge, monkeypatch):
    """Ollama defaults to a 4096 context whatever the model supports, and one image costs
    ~4000 prompt tokens — leaving ~45 for the answer and returning nothing. Confirmed
    against the real qwen3-vl before this was set."""
    fake = FakeOllama().install(monkeypatch)
    bridge.run_sync("agent", "vision", "describe", images=["b64"])
    options = fake.generate_calls[0]["options"]
    assert options["num_ctx"] >= 16384
    # Thinking models reason at length before answering; a tight cap truncates them
    # mid-thought and produces no answer at all.
    assert options["num_predict"] >= 4096


def test_thinking_only_output_is_surfaced_not_swallowed(bridge, monkeypatch):
    """qwen3-vl splits output into `thinking` and `response`. If the cap lands mid-thought
    the answer never appears — return the reasoning and say it was cut, rather than
    handing the caller an empty string that looks like success."""
    def fake_post(url, **kwargs):
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "response": "", "thinking": "I can see a red circle", "done_reason": "length"})

    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json={"models": []}, request=httpx.Request("GET", url)))
    monkeypatch.setattr(httpx, "post", fake_post)

    job = bridge.run_sync("agent", "vision", "describe", images=["b64"])
    assert job["status"] == "done"
    assert "red circle" in job["result"]
    assert "truncated" in job["result"]


def test_a_genuinely_empty_response_fails_rather_than_looking_successful(bridge, monkeypatch):
    def fake_post(url, **kwargs):
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"response": "", "thinking": "", "done_reason": "stop"})

    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json={"models": []}, request=httpx.Request("GET", url)))
    monkeypatch.setattr(httpx, "post", fake_post)

    job = bridge.run_sync("agent", "text", "work")
    assert job["status"] == "failed"
    assert "returned nothing" in job["error"]


def test_short_keep_alive_so_the_card_frees_itself(bridge, monkeypatch):
    """A 24h keep_alive left 9GB resident on his gaming card for nothing — the incident
    that prompted this whole subsystem."""
    fake = FakeOllama().install(monkeypatch)
    bridge.run_sync("agent", "text", "tag this")
    assert fake.generate_calls[0]["keep_alive"] == "5m"


# --- reservation (gaming / racing) -------------------------------------------

def test_reserving_unloads_models_and_stops_dispatch(bridge, monkeypatch):
    fake = FakeOllama(loaded=[("qwen3-vl:8b", 6.5)]).install(monkeypatch)

    bridge.set_mode("reserved", "racing")
    assert fake.unloaded == ["qwen3-vl:8b"]
    assert bridge.is_available() is False

    bridge.submit("trend_scout", "text", "summarise")
    assert bridge.tick() == 0, "no job may start while simrig is reserved"
    assert bridge.jobs(status="queued")[0]["agent"] == "trend_scout"


def test_queued_work_resumes_when_released(bridge, monkeypatch):
    fake = FakeOllama().install(monkeypatch)
    bridge.set_mode("reserved", "gaming")
    job_id = bridge.submit("agent", "text", "work")
    assert bridge.tick() == 0

    bridge.set_mode("available", "done gaming")
    assert bridge.tick() == 1
    for _ in range(50):
        if bridge.job(job_id)["status"] == "done":
            break
        import time
        time.sleep(0.05)
    assert bridge.job(job_id)["status"] == "done"
    assert fake.generate_calls


def test_reservation_survives_a_restart(db_path, monkeypatch):
    """Persisted deliberately: a service restart mid-race must not hand the card back."""
    FakeOllama().install(monkeypatch)
    GPUBridge(db_path, "http://simrig.test:11434").set_mode("reserved", "racing")
    assert GPUBridge(db_path, "http://simrig.test:11434").is_available() is False


def test_run_sync_respects_a_reservation_rather_than_jumping_the_queue(bridge, monkeypatch):
    FakeOllama().install(monkeypatch)
    bridge.set_mode("reserved", "racing")
    job = bridge.run_sync("agent", "text", "urgent", timeout=3)
    assert job["status"] == "timeout"
    assert bridge.jobs(status="queued")


# --- queue and VRAM admission ------------------------------------------------

def _mark_running(db_path, job_id):
    """A genuinely in-flight job. The fakes return instantly, so a real concurrency test
    has to pin one job in 'running' rather than racing the executor."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE gpu_jobs SET status = 'running' WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()


def test_second_job_is_refused_without_vram_headroom(bridge, db_path, monkeypatch):
    """16GB card with a 6.5GB vision model resident and a job in flight: a 17GB heavy
    model cannot join it, so it waits its turn."""
    FakeOllama(loaded=[("qwen3-vl:8b", 6.5)]).install(monkeypatch)
    _mark_running(db_path, bridge.submit("a", "vision", "one"))

    bridge.submit("b", "heavy", "two")
    assert bridge.tick() == 0          # 16 - 6.5 - 17 is far under the safety margin
    assert len(bridge.jobs(status="queued")) == 1


def test_a_small_second_job_is_admitted_when_there_is_room(bridge, db_path, monkeypatch):
    """The flip side — headroom is a real check, not a blanket refusal of concurrency."""
    FakeOllama(loaded=[("qwen3-vl:8b", 6.5)]).install(monkeypatch)
    _mark_running(db_path, bridge.submit("a", "vision", "one"))

    bridge.submit("b", "text", "two")  # 6GB into 9.5GB free, above the 2GB margin
    assert bridge.tick() == 1


def test_a_job_whose_model_is_already_resident_is_always_admitted(bridge, db_path, monkeypatch):
    """Several vision jobs in a row cost no extra VRAM — serialising them behind a
    headroom check they don't need would be daft."""
    FakeOllama(loaded=[("qwen3-vl:8b", 6.5)]).install(monkeypatch)
    _mark_running(db_path, bridge.submit("a", "vision", "one"))
    bridge.submit("b", "vision", "two")
    assert bridge.tick() == 1


def test_max_concurrent_is_enforced(db_path, monkeypatch):
    FakeOllama(loaded=[("qwen3:8b", 1.0)]).install(monkeypatch)
    bridge = GPUBridge(db_path, "http://simrig.test:11434", max_concurrent=1)
    _mark_running(db_path, bridge.submit("a", "text", "one"))
    bridge.submit("b", "text", "two")
    assert bridge.tick() == 0


def test_cancelling_a_queued_job(bridge, monkeypatch):
    FakeOllama().install(monkeypatch)
    job_id = bridge.submit("a", "text", "work")
    assert bridge.cancel(job_id) is True
    assert bridge.job(job_id)["status"] == "cancelled"
    assert bridge.tick() == 0


def test_failures_are_recorded_against_the_job(bridge, monkeypatch):
    def boom(url, **kwargs):
        raise httpx.ConnectError("simrig is off")

    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(
        200, json={"models": []}, request=httpx.Request("GET", url)))
    monkeypatch.setattr(httpx, "post", boom)
    job = bridge.run_sync("agent", "text", "work", timeout=10)
    assert job["status"] == "failed" and "simrig is off" in job["error"]


def test_status_reports_everything_a_ui_needs(bridge, monkeypatch):
    FakeOllama(loaded=[("qwen3-vl:8b", 6.5)]).install(monkeypatch)
    bridge.submit("trend_scout", "vision", "look")
    status = bridge.status()

    assert status["mode"] == "available"
    assert status["queued"] == 1
    assert status["vram_used_gb"] == 6.5
    assert status["vram_free_gb"] == pytest.approx(9.5)
    assert status["task_routing"]["vision"]["available"] is True
    # Declared but not installed, so the UI can show it greyed rather than missing.
    assert status["task_routing"]["image_generation"]["available"] is False
