"""kitchen_vision.analyze_recipe_photo: a JPEG in, a job through the GPU bridge, a
forgivingly-parsed recipe draft out. Tests a fake bridge rather than a real GPUBridge --
what matters here is the JSON-extraction and validation logic, not the queue itself
(that's gpu_bridge.py's own test coverage)."""
from assistant.core import kitchen_vision


class FakeBridge:
    def __init__(self, job):
        self._job = job
        self.calls = []

    def run_sync(self, agent, task_type, prompt, images=None, options=None, timeout=900):
        self.calls.append({"agent": agent, "task_type": task_type, "prompt": prompt, "images": images})
        return self._job


def test_clean_json_response_parses_correctly():
    bridge = FakeBridge({"status": "done", "result": (
        '{"title": "Weeknight Chili", "servings": 6, '
        '"ingredients": [{"name": "ground beef", "quantity": "1", "unit": "lb"}], '
        '"steps": ["Brown the beef.", "Simmer 20 minutes."]}'
    )})

    draft = kitchen_vision.analyze_recipe_photo(bridge, b"fake-jpeg-bytes")

    assert draft["parsed"] is True
    assert draft["title"] == "Weeknight Chili"
    assert draft["servings"] == 6
    assert draft["ingredients"] == [{"name": "ground beef", "quantity": "1", "unit": "lb"}]
    assert draft["steps"] == ["Brown the beef.", "Simmer 20 minutes."]
    # Confirms the image actually got base64-encoded and routed to the vision task type.
    assert bridge.calls[0]["task_type"] == "vision"
    assert bridge.calls[0]["images"] == [__import__("base64").b64encode(b"fake-jpeg-bytes").decode()]


def test_json_wrapped_in_prose_is_still_extracted():
    bridge = FakeBridge({"status": "done", "result": (
        "Sure, here's the recipe I found in the photo:\n\n"
        '{"title": "Pancakes", "servings": null, '
        '"ingredients": [{"name": "flour", "quantity": "2", "unit": "cups"}], '
        '"steps": ["Mix.", "Cook on a griddle."]}\n\n'
        "Let me know if you need anything else!"
    )})

    draft = kitchen_vision.analyze_recipe_photo(bridge, b"fake-jpeg-bytes")

    assert draft["parsed"] is True
    assert draft["title"] == "Pancakes"
    assert draft["servings"] is None


def test_unparseable_response_reports_failure_without_dropping_the_text():
    bridge = FakeBridge({"status": "done", "result": "I couldn't make out the handwriting clearly."})

    draft = kitchen_vision.analyze_recipe_photo(bridge, b"fake-jpeg-bytes")

    assert draft["parsed"] is False
    assert draft["raw_text"] == "I couldn't make out the handwriting clearly."
    assert draft["error"]


def test_response_missing_ingredients_or_steps_reports_failure():
    bridge = FakeBridge({"status": "done", "result": '{"title": "Empty", "ingredients": [], "steps": []}'})

    draft = kitchen_vision.analyze_recipe_photo(bridge, b"fake-jpeg-bytes")

    assert draft["parsed"] is False
    assert "missing" in draft["error"]


def test_failed_job_reports_the_bridge_error():
    bridge = FakeBridge({"status": "failed", "error": "model returned nothing (done_reason=length)"})

    draft = kitchen_vision.analyze_recipe_photo(bridge, b"fake-jpeg-bytes")

    assert draft["parsed"] is False
    assert "done_reason=length" in draft["error"]


def test_inventory_photo_parses_name_quantity_and_unit():
    bridge = FakeBridge({"status": "done", "result": '{"name": "rice", "quantity": 2.5, "unit": "cups"}'})

    draft = kitchen_vision.analyze_inventory_photo(bridge, b"fake-jpeg-bytes")

    assert draft == {"parsed": True, "name": "rice", "quantity": 2.5, "unit": "cups"}


def test_inventory_photo_with_item_hint_tells_the_model_the_name():
    bridge = FakeBridge({"status": "done", "result": '{"name": "milk", "quantity": 0.5, "unit": "gal"}'})

    kitchen_vision.analyze_inventory_photo(bridge, b"fake-jpeg-bytes", item_hint="milk")

    assert 'already known to be "milk"' in bridge.calls[0]["prompt"]


def test_inventory_photo_missing_quantity_reports_failure():
    bridge = FakeBridge({"status": "done", "result": '{"name": "rice"}'})

    draft = kitchen_vision.analyze_inventory_photo(bridge, b"fake-jpeg-bytes")

    assert draft["parsed"] is False
    assert "missing" in draft["error"]


def test_receipt_parses_multiple_line_items():
    bridge = FakeBridge({"status": "done", "result": (
        '{"store": "Kroger", "items": ['
        '{"name": "bananas", "quantity": 1.34, "unit": "lb"}, '
        '{"name": "milk", "quantity": 1, "unit": "gal"}]}'
    )})

    draft = kitchen_vision.analyze_receipt_photo(bridge, b"fake-jpeg-bytes")

    assert draft["parsed"] is True
    assert draft["store"] == "Kroger"
    assert draft["items"] == [
        {"name": "bananas", "quantity": 1.34, "unit": "lb"},
        {"name": "milk", "quantity": 1.0, "unit": "gal"},
    ]


def test_receipt_with_no_readable_items_returns_empty_list_not_failure():
    bridge = FakeBridge({"status": "done", "result": '{"store": null, "items": []}'})

    draft = kitchen_vision.analyze_receipt_photo(bridge, b"fake-jpeg-bytes")

    assert draft["parsed"] is True
    assert draft["store"] is None
    assert draft["items"] == []


def test_receipt_failed_job_reports_the_bridge_error():
    bridge = FakeBridge({"status": "failed", "error": "model returned nothing (done_reason=length)"})

    draft = kitchen_vision.analyze_receipt_photo(bridge, b"fake-jpeg-bytes")

    assert draft["parsed"] is False
    assert "done_reason=length" in draft["error"]
