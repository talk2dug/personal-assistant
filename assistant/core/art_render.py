"""Rendering an art direction so it can be judged as a picture rather than a paragraph.

The art director used to file "Art direction: <product>" with the image prompt in the
body and nothing to look at. The owner's complaint was exact: "I also need to see the art
work and not just text. If they made something I need to be able to see it." A prompt is
not artwork, and approving one is approving a guess about what it will produce.

So the order is flipped. The director proposes several distinct visual directions, each
is rendered on the card in simrig *before* anything is filed, and the card he gets is a
pick-one between actual images. What he approves is what he saw.

Rendering goes through gpu_bridge's queue rather than driving ComfyUI directly, so these
jobs sit behind the same VRAM headroom check and gaming reservation as everything else.
A backlog of art renders must never be the reason the card is busy when he sits down to
race.

Nothing here raises. A render that fails has to degrade the card back to text, because
the alternative is losing the direction the model already wrote and showing him nothing
at all -- which is strictly worse than the behaviour this replaces.
"""
import json
import logging
import math
import re

logger = logging.getLogger(__name__)

# Z-Image Turbo is trained around 1MP. Asking for much more costs time and tends to
# duplicate the subject; much less loses the detail that survives being printed.
PIXEL_BUDGET = 1024 * 1024

# Both sides must be multiples of 32: the VAE downsamples by 8 and the patchifier wants an
# even latent, so an odd size is either rejected or silently cropped.
SIZE_MULTIPLE = 32
MIN_SIDE, MAX_SIDE = 512, 1536

# Three is the useful number. One is not a choice, and more than three stops being a
# decision and becomes another queue to get through -- which is the problem being fixed.
DEFAULT_DIRECTIONS = 3

# Measured, with headroom: the six image jobs on record ran 7.8s, 10s and 25.5s (the last
# a cold start loading the model off disk). 300s is more than ten times the worst of
# those, so hitting it means something is wrong rather than something is slow -- and it
# matters, because a full pipeline tick is three concepts of three directions, each of
# which waits this long before giving up.
RENDER_TIMEOUT = 300

_ASPECT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[:x/]\s*(\d+(?:\.\d+)?)\s*$", re.IGNORECASE)


def _snap(value: float) -> int:
    snapped = int(round(value / SIZE_MULTIPLE)) * SIZE_MULTIPLE
    return max(MIN_SIDE, min(MAX_SIDE, snapped))


def dimensions_for(aspect: str | None, pixel_budget: int = PIXEL_BUDGET) -> tuple[int, int]:
    """Pixel dimensions for an aspect like "4:5", at roughly a fixed area.

    Computed rather than looked up in a table, because the art director writes the aspect
    as free text and will eventually write one a table does not have. Anything
    unparseable falls back to square, which is never wrong enough to be worth failing a
    render over.
    """
    ratio = 1.0
    match = _ASPECT_RE.match(aspect or "")
    if match:
        width_part, height_part = float(match.group(1)), float(match.group(2))
        if width_part > 0 and height_part > 0:
            ratio = width_part / height_part
    height = math.sqrt(pixel_budget / ratio)
    return _snap(height * ratio), _snap(height)


def render_one(bridge, prompt: str, aspect: str | None = None, negative: str | None = None,
               agent: str = "art_director",
               timeout: int = RENDER_TIMEOUT) -> tuple[str | None, str | None]:
    """Renders one image. Returns (path, error) -- exactly one of which is set.

    The job goes on the shared queue, so "the card is reserved for racing" shows up here
    as a timeout with the job still queued. That is the right outcome: it will render
    later, and meanwhile the direction is still filed as text.
    """
    width, height = dimensions_for(aspect)
    options = {"width": width, "height": height}
    if negative:
        options["negative"] = negative
    try:
        job = bridge.run_sync(agent, "image_generation", prompt, options=options, timeout=timeout)
    except Exception as e:
        logger.warning("art render could not be submitted: %s", e)
        return None, str(e)

    if job.get("status") != "done":
        return None, job.get("error") or f"render {job.get('status')}"
    try:
        files = json.loads(job.get("result") or "{}").get("files") or []
    except (TypeError, ValueError):
        files = []
    if not files:
        return None, "the renderer reported success but produced no file"
    return files[0], None


def render_directions(bridge, directions: list[dict], aspect: str | None = None,
                      negative: str | None = None, agent: str = "art_director",
                      limit: int = DEFAULT_DIRECTIONS,
                      timeout: int = RENDER_TIMEOUT) -> tuple[list[dict], list[str]]:
    """The director's proposed directions, rendered, as review options.

    Returns (options, failures). A direction whose render failed is still returned as an
    option with no media -- he can still read it and pick it, and the failure is reported
    on the card rather than silently dropping one of three choices.
    """
    options: list[dict] = []
    failures: list[str] = []
    for position, direction in enumerate(directions[:limit]):
        prompt = (direction.get("image_prompt") or "").strip()
        if not prompt:
            continue
        label = (direction.get("label") or "").strip() or f"Direction {position + 1}"

        if bridge is None:
            path, error = None, "no GPU bridge is configured"
        else:
            path, error = render_one(bridge, prompt, aspect, negative, agent, timeout)
        if error:
            failures.append(f"{label}: {error}")

        options.append({
            "label": label,
            "description": direction.get("rationale") or direction.get("description"),
            "media_path": path,
            # The prompt that made this picture, kept with it: approving an option has to
            # be able to say what to render again at production size, and which direction
            # the store and social copy should be written against.
            "body": prompt,
        })
    return options, failures


def reserved_reason(bridge) -> str | None:
    """Why the card is being kept free right now, if it is.

    The owner reserves simrig when he sits down to race, and the queue holds every job
    until he releases it. For most work that is invisible -- the job waits and runs later.
    For this it is not, because the art director would block for its whole timeout on
    every render and then file the text-only cards it was built to stop filing. Better to
    do nothing: the concepts stay unbriefed, so the next tick picks them up exactly as if
    this run had never happened.
    """
    if bridge is None:
        return None
    try:
        mode = bridge.get_mode() or {}
    except Exception:
        return None
    if mode.get("mode") != "reserved":
        return None
    return mode.get("reason") or "reserved"
