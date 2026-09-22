"""Rendering art directions so they can be judged as pictures.

The behaviour under test is mostly about what happens when the renderer does NOT work.
The art director's output is expensive -- a web-searching model call per concept -- and
the rule these pin is that a failed render costs the pictures and nothing else. The card
still gets filed, the directions are still readable, and the failure is said out loud.
"""
from assistant.core import art_render


class FakeBridge:
    """A GPU bridge that records what it was asked for and answers from a script."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run_sync(self, agent, task_type, prompt, images=None, options=None, timeout=900,
                 fmt=None):
        self.calls.append({"agent": agent, "task_type": task_type, "prompt": prompt,
                           "options": options or {}, "timeout": timeout})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _done(*paths):
    import json
    return {"status": "done", "result": json.dumps({"files": list(paths), "count": len(paths)})}


DIRECTIONS = [
    {"label": "Neon night", "rationale": "for the late crowd", "image_prompt": "neon squirrel"},
    {"label": "Woodcut", "rationale": "for the market stall", "image_prompt": "woodcut squirrel"},
    {"label": "Flat vector", "rationale": "cheapest to cut", "image_prompt": "flat squirrel"},
]


class TestDimensions:
    def test_a_square_aspect_is_square(self):
        assert art_render.dimensions_for("1:1") == (1024, 1024)

    def test_a_portrait_aspect_is_taller_than_it_is_wide(self):
        width, height = art_render.dimensions_for("4:5")
        assert height > width

    def test_a_landscape_aspect_is_wider_than_it_is_tall(self):
        width, height = art_render.dimensions_for("3:2")
        assert width > height

    def test_both_sides_are_multiples_of_32(self):
        """The VAE downsamples by 8 and the patchifier wants an even latent, so an odd
        size is either rejected outright or silently cropped."""
        for aspect in ("1:1", "4:5", "3:2", "16:9", "9:16", "4:3", "2:3", "7:3"):
            width, height = art_render.dimensions_for(aspect)
            assert width % 32 == 0 and height % 32 == 0, aspect

    def test_the_area_stays_near_the_budget_whatever_the_shape(self):
        """Why this is computed and not a table: every aspect has to cost about the same
        render time, or a 16:9 brief quietly becomes the slow one."""
        for aspect in ("1:1", "4:5", "3:2", "16:9", "4:3"):
            width, height = art_render.dimensions_for(aspect)
            assert 0.8 <= (width * height) / art_render.PIXEL_BUDGET <= 1.25, aspect

    def test_an_unreadable_aspect_falls_back_to_square(self):
        """The art director writes this as free text and will eventually write something
        no table has. Guessing square is never wrong enough to fail a render over."""
        for junk in (None, "", "square-ish", "portrait", "0:0", "-1:4"):
            assert art_render.dimensions_for(junk) == (1024, 1024), junk

    def test_an_extreme_aspect_is_clamped_rather_than_refused(self):
        width, height = art_render.dimensions_for("100:1")
        assert art_render.MIN_SIDE <= height <= art_render.MAX_SIDE
        assert art_render.MIN_SIDE <= width <= art_render.MAX_SIDE

    def test_alternative_separators_are_understood(self):
        assert art_render.dimensions_for("4x5") == art_render.dimensions_for("4:5")
        assert art_render.dimensions_for("4/5") == art_render.dimensions_for("4:5")


class TestRenderOne:
    def test_a_successful_render_returns_the_saved_path(self):
        bridge = FakeBridge([_done("generated/job7_jarvis_001.png")])
        path, error = art_render.render_one(bridge, "a squirrel")
        assert path == "generated/job7_jarvis_001.png"
        assert error is None

    def test_the_aspect_reaches_the_renderer_as_pixels(self):
        bridge = FakeBridge([_done("a.png")])
        art_render.render_one(bridge, "a squirrel", aspect="4:5")
        assert bridge.calls[0]["options"] == dict(
            zip(("width", "height"), art_render.dimensions_for("4:5")))

    def test_the_negative_prompt_is_passed_through(self):
        bridge = FakeBridge([_done("a.png")])
        art_render.render_one(bridge, "a squirrel", negative="blurry, watermark")
        assert bridge.calls[0]["options"]["negative"] == "blurry, watermark"

    def test_it_goes_on_the_shared_queue_as_an_image_job(self):
        """Not a direct ComfyUI call: these have to sit behind the same VRAM headroom
        check and gaming reservation as everything else, or a render backlog is why the
        card is busy when he sits down to race."""
        bridge = FakeBridge([_done("a.png")])
        art_render.render_one(bridge, "a squirrel")
        assert bridge.calls[0]["task_type"] == "image_generation"

    def test_a_failed_job_is_reported_not_raised(self):
        bridge = FakeBridge([{"status": "failed", "error": "ComfyUI is not responding"}])
        path, error = art_render.render_one(bridge, "a squirrel")
        assert path is None
        assert "not responding" in error

    def test_a_job_still_queued_behind_a_reservation_is_reported_not_raised(self):
        bridge = FakeBridge([{"status": "timeout", "error": "job did not finish within 600s"}])
        path, error = art_render.render_one(bridge, "a squirrel")
        assert path is None and error

    def test_a_thrown_exception_becomes_an_error_string(self):
        bridge = FakeBridge([RuntimeError("the bridge is on fire")])
        path, error = art_render.render_one(bridge, "a squirrel")
        assert path is None
        assert "on fire" in error

    def test_success_with_no_file_is_still_a_failure(self):
        bridge = FakeBridge([{"status": "done", "result": '{"files": [], "count": 0}'}])
        path, error = art_render.render_one(bridge, "a squirrel")
        assert path is None and error

    def test_an_unparseable_result_is_a_failure_not_a_crash(self):
        bridge = FakeBridge([{"status": "done", "result": "not json at all"}])
        path, error = art_render.render_one(bridge, "a squirrel")
        assert path is None and error


class TestReservation:
    """He reserves simrig when he sits down to race, and the queue holds every job until
    he releases it. Most work never notices; this does."""

    def test_a_reserved_card_reports_why(self):
        class Bridge:
            def get_mode(self):
                return {"mode": "reserved", "reason": "racing"}
        assert art_render.reserved_reason(Bridge()) == "racing"

    def test_an_available_card_reports_nothing(self):
        class Bridge:
            def get_mode(self):
                return {"mode": "available"}
        assert art_render.reserved_reason(Bridge()) is None

    def test_no_bridge_is_not_a_reservation(self):
        """Having no GPU at all is a different situation from the GPU being busy, and it
        must not stop the agent working text-only."""
        assert art_render.reserved_reason(None) is None

    def test_a_bridge_that_cannot_answer_is_not_a_reservation(self):
        class Bridge:
            def get_mode(self):
                raise RuntimeError("simrig is off")
        assert art_render.reserved_reason(Bridge()) is None


class TestRenderDirections:
    def test_each_direction_becomes_an_option_with_its_picture(self):
        bridge = FakeBridge([_done("one.png"), _done("two.png"), _done("three.png")])
        options, failures = art_render.render_directions(bridge, DIRECTIONS)
        assert failures == []
        assert [o["label"] for o in options] == ["Neon night", "Woodcut", "Flat vector"]
        assert [o["media_path"] for o in options] == ["one.png", "two.png", "three.png"]

    def test_the_prompt_that_made_each_picture_is_kept_with_it(self):
        """Approving an option has to be able to say what to render again at production
        size, and which direction the listing and social copy describe."""
        bridge = FakeBridge([_done("one.png"), _done("two.png"), _done("three.png")])
        options, _ = art_render.render_directions(bridge, DIRECTIONS)
        assert [o["body"] for o in options] == [d["image_prompt"] for d in DIRECTIONS]

    def test_the_rationale_becomes_the_caption(self):
        bridge = FakeBridge([_done("one.png")])
        options, _ = art_render.render_directions(bridge, DIRECTIONS[:1])
        assert options[0]["description"] == "for the late crowd"

    def test_one_failed_render_does_not_cost_the_others(self):
        bridge = FakeBridge([_done("one.png"),
                             {"status": "failed", "error": "out of memory"},
                             _done("three.png")])
        options, failures = art_render.render_directions(bridge, DIRECTIONS)
        assert len(options) == 3, "the direction is still choosable, it just has no picture"
        assert options[1]["media_path"] is None
        assert [o["media_path"] for o in (options[0], options[2])] == ["one.png", "three.png"]

    def test_a_failure_is_named_rather_than_hidden(self):
        """A card showing two pictures where it offered three directions has to say why,
        or it reads as a bug."""
        bridge = FakeBridge([_done("one.png"),
                             {"status": "failed", "error": "out of memory"},
                             _done("three.png")])
        _, failures = art_render.render_directions(bridge, DIRECTIONS)
        assert len(failures) == 1
        assert "Woodcut" in failures[0] and "out of memory" in failures[0]

    def test_no_bridge_at_all_still_produces_readable_options(self):
        """The old text-only behaviour, kept as the floor: without a GPU configured the
        agent must still file the directions rather than lose the work."""
        options, failures = art_render.render_directions(None, DIRECTIONS)
        assert len(options) == 3
        assert all(o["media_path"] is None for o in options)
        assert len(failures) == 3

    def test_it_renders_no_more_than_the_limit(self):
        """One render per direction is the cost; a model that returns eight directions
        must not quietly book eight slots on the card."""
        bridge = FakeBridge([_done("one.png"), _done("two.png")])
        options, _ = art_render.render_directions(bridge, DIRECTIONS, limit=2)
        assert len(options) == 2
        assert len(bridge.calls) == 2

    def test_a_direction_with_no_prompt_is_skipped_and_costs_no_render(self):
        bridge = FakeBridge([_done("one.png")])
        options, _ = art_render.render_directions(
            bridge, [{"label": "Empty"}, DIRECTIONS[0]])
        assert [o["label"] for o in options] == ["Neon night"]
        assert len(bridge.calls) == 1

    def test_an_unlabelled_direction_still_gets_a_name(self):
        bridge = FakeBridge([_done("one.png")])
        options, _ = art_render.render_directions(bridge, [{"image_prompt": "a squirrel"}])
        assert options[0]["label"] == "Direction 1"
