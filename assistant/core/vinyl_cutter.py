"""Contour and cut-path generation for the Silhouette Cameo (Blue Ridge Custom Co).

This replaces `server/sticker-sheet-generator.js` from the retired print-station system. That
implementation was measurably broken: on real output **every cut path contained a straight
segment spanning 50-65% of the sticker's width** -- the blade driving straight across the
design instead of around it. Five separate causes were found, and each one is answered by a
specific decision here. They are written down because they are all easy to reintroduce.

**1. The tracer only appended un-visited cells.** Backtracking along a thin feature therefore
emitted a straight chord jump instead of retracing. Answered by using OpenCV's Suzuki-Abe
border following (`cv2.findContours`), which returns a genuinely closed boundary.

**2. It traced on an 800 px downscale of a 300 DPI sheet.** That is ~0.27 mm of quantisation
against a 0.25 mm offset -- the error was larger than the offset itself. Answered by
`extract_contours` tracing at **full resolution**, always. There is no downscale parameter,
deliberately.

**3. It offset curves by naive normal displacement with no self-intersection cleanup**, so
every concave corner grew a bowtie. Answered by offsetting with **pyclipper**
(`ClipperOffset`, round joins, closed-polygon end type). Removing self-intersections is part
of Clipper's offset algorithm rather than a cleanup pass bolted on after it, which is the
whole reason to use it.

**4. No winding normalisation.** `polygonArea()` existed in that file and was never called, so
a contour that happened to come back reversed offset *inward* and cut through the artwork.
Answered by `normalise_winding`, which is not optional and runs before every offset.

**5. Background removal failed silently and returned the original opaque image**, so the
tracer saw a rectangle and returned its bounding box. This is why stickers sometimes cut as
squares. Answered by `remove_background` raising `VinylCutterError`. It also rejects a
result that is still fully opaque, because that is what the silent failure actually looked
like -- an exception that never fired is not much better than no exception.

**Registration marks go on the printed PNG only, never in the cut SVG.** The Cameo reads them
optically off the print; a mark in the cut file gets cut. The surviving broken output on disk
has them in the SVG, so this is a real mistake that was made before, and `build_cut_svg` has
no way to add them.

Calibration constants come from the owner's Obsidian note
`03-Areas/Silhouette Cameo - Cutting Calibration.md`. They were arrived at by cutting real
material and nothing else records them, so they are transcribed rather than re-derived.

**Jarvis does not drive the blade.** This module generates files. Sending a job to the
hardware stays a manual step; see `vendor/inkscape-silhouette/` for the driver that would do
it. The only thing read from that driver here is its media table, for validation.
"""

from __future__ import annotations

import logging
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

VENDOR_DRIVER_PATH = Path(__file__).resolve().parent.parent.parent / "vendor" / "inkscape-silhouette"

# --- calibration ----------------------------------------------------------------------
# All of the following is transcribed from the Obsidian calibration note. Do not "clean up"
# these numbers; they came off real cuts.

SHEET_WIDTH_MM = 215.9    # US Letter
SHEET_HEIGHT_MM = 279.4
PRINT_DPI = 300           # 2550 x 3300 px at Letter

REGMARK_MARGIN_MM = 10.0      # mark origin inset from each page edge
REGMARK_SQUARE_MM = 5.0       # filled corner square
REGMARK_ARM_MM = 20.0         # length of each L arm
REGMARK_STROKE_MM = 0.5       # arm thickness
REGMARK_CLEARANCE_MM = 2.0    # keep artwork this far off the marks

# Mark-to-mark spacing is derived, not hardcoded, so it re-derives if the paper size changes.
# On Letter with 10 mm margins this gives the 195.9 x 259.4 mm recorded in the note.
REGMARK_SPACING_X_MM = SHEET_WIDTH_MM - 2 * REGMARK_MARGIN_MM
REGMARK_SPACING_Y_MM = SHEET_HEIGHT_MM - 2 * REGMARK_MARGIN_MM

STICKER_GAP_MM = 3.175        # 0.125 in between stickers
MIN_TEXT_HEIGHT_MM = 6.35     # 0.25 in -- below this vinyl will not weed or cut reliably

DEFAULT_CUT_OFFSET_MM = 2.0
"""How far outside the artwork the blade runs.

**This value was genuinely unresolved and 2 mm is a documented decision, not a coin flip.**
Two figures existed in the dead system and disagreed by 8x: the server used
`DEFAULT_OFFSET_MM = 0.25`, while `StickerSheets/index.js` -- the potrace + clipper prototype
that had the right approach but was never wired up -- used `offsetMm: 2`.

2 mm is the credible one. The server traced on an 800 px downscale of a 300 DPI sheet, about
0.27 mm of quantisation error, and simplified with a ~0.5 mm tolerance. Both of those are
larger than a 0.25 mm offset, so that offset was never physically meaningful on that code
path -- it was a number that could not survive its own pipeline.

It is a parameter on every entry point here rather than a constant, so a test cut can settle
it without a code change. If a real cut shows 2 mm is too generous, change the caller, and
record the result back in the Obsidian note.
"""


@dataclass(frozen=True)
class TracePreset:
    """Tracing parameters. The two presets are different jobs, not tuning of one job."""

    name: str
    min_area_px: float
    """Despeckle floor -- potrace's `turdSize`. Contours smaller than this are dropped."""
    smooth_mm: float
    """Boundary smoothing radius, standing in for potrace's `alphaMax`/`optCurve`.

    0 means no smoothing at all, which is what vinyl wants (blocky, hard edges).

    Kept deliberately small where it is non-zero. potrace's curve optimisation smooths the
    *fitted curve*; the morphological close/open used here is a cruder proxy that operates on
    the mask, and at any meaningful radius it deletes real detail rather than rounding it --
    0.3 mm was enough to erase a 4 px feature outright. Losing geometry is the exact failure
    mode this module was written to end, so this stays at roughly one pixel: enough to take
    the staircase off a rasterised edge, not enough to remove anything that was really there.
    """
    alpha_threshold: int = 128
    """Alpha level treated as solid. potrace's `threshold`."""


# From the note: vinyl wants turdSize 10 / optCurve false / alphaMax 0 / threshold 128;
# stickers want turdSize 2 / optCurve true / alphaMax 1.0.
PRESET_VINYL = TracePreset(name="vinyl", min_area_px=10.0, smooth_mm=0.0, alpha_threshold=128)
PRESET_STICKER = TracePreset(name="sticker", min_area_px=2.0, smooth_mm=0.10)
PRESETS = {"vinyl": PRESET_VINYL, "sticker": PRESET_STICKER}


@dataclass(frozen=True)
class BladeRecipe:
    """Speed/pressure/depth for the Cameo. Two distinct profiles, both using `autoblade`."""

    name: str
    speed: int
    pressure: int
    depth: int
    regmarks: bool
    tool: str = "autoblade"

    def __post_init__(self) -> None:
        # The driver's own scale maxes at 33; >= 19 triggers track-enhancing on the Cameo.
        if not 1 <= self.pressure <= 33:
            raise ValueError(f"pressure {self.pressure} outside the Cameo's 1..33 scale")


RECIPE_STICKER = BladeRecipe(name="sticker print-and-cut", speed=4, pressure=15, depth=6, regmarks=True)
RECIPE_VINYL = BladeRecipe(name="vinyl die-cut", speed=3, pressure=10, depth=2, regmarks=False)
RECIPES = {"sticker": RECIPE_STICKER, "vinyl": RECIPE_VINYL}


class VinylCutterError(RuntimeError):
    """Raised when cut-path generation cannot honestly continue.

    Deliberately loud. The predecessor's defining bug was a failure that returned plausible
    output instead of raising, and nobody noticed until stickers came out square.
    """


# --- geometry primitives ---------------------------------------------------------------

CLIPPER_SCALE = 1000
"""Clipper works in integers; 1 unit = 1 micron at this scale.

The old prototype used 100 (0.01 mm). 1000 keeps quantisation ~2 orders of magnitude below
the smallest offset anyone would reasonably ask for, which is the mistake in cause 2 above,
just in a different place.
"""


@dataclass
class Contour:
    """One closed boundary, in millimetres, in sheet space.

    `points` is (N, 2) float. `is_hole` marks an interior boundary -- the counter of an O or
    an A. Those matter: for vinyl lettering they have to cut, or the letter fills in.
    """

    points: np.ndarray
    is_hole: bool = False
    _area: float | None = field(default=None, repr=False)

    def signed_area(self) -> float:
        return signed_area(self.points)


def signed_area(points: np.ndarray) -> float:
    """Shoelace formula. Sign gives winding direction; magnitude gives area.

    The predecessor had this function and never called it, which is cause 4. Calling it is
    the entire fix.
    """
    if len(points) < 3:
        return 0.0
    x = points[:, 0]
    y = points[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def normalise_winding(contours: list[Contour]) -> list[Contour]:
    """Force outer boundaries to one winding and holes to the opposite.

    This is what makes the offset go the right way. Clipper expands a positively-wound
    polygon and contracts a negatively-wound one, so with outers positive and holes negative,
    a single offset pass grows the material outward *and* shrinks the counters inward -- both
    of which are "away from the artwork", which is what a cut line wants.

    A reversed contour without this step offsets inward and the blade cuts through the
    design.
    """
    out: list[Contour] = []
    for c in contours:
        area = c.signed_area()
        wants_positive = not c.is_hole
        if (area < 0) == wants_positive:
            c = Contour(points=c.points[::-1].copy(), is_hole=c.is_hole)
        out.append(c)
    return out


def max_segment_length(points: np.ndarray) -> float:
    """Longest single straight hop in a closed path, in the path's own units.

    The straight-chord bug is exactly a large value here, so this is the measurement the
    regression test is built on.
    """
    if len(points) < 2:
        return 0.0
    d = np.diff(np.vstack([points, points[:1]]), axis=0)
    return float(np.hypot(d[:, 0], d[:, 1]).max())


def bbox_diagonal(points: np.ndarray) -> float:
    if len(points) == 0:
        return 0.0
    lo = points.min(axis=0)
    hi = points.max(axis=0)
    return float(math.hypot(*(hi - lo)))


def is_simple_polygon(points: np.ndarray, *, tolerance: float = 1e-9) -> bool:
    """True if no two non-adjacent edges cross -- i.e. no bowties.

    O(n^2), so this is a verification tool for tests and diagnostics, not something the
    generation path calls on every sticker.
    """
    n = len(points)
    if n < 4:
        return True
    p = points
    q = np.roll(points, -1, axis=0)

    def _orient(a, b, c) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    for i in range(n):
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            d1 = _orient(p[i], q[i], p[j])
            d2 = _orient(p[i], q[i], q[j])
            d3 = _orient(p[j], q[j], p[i])
            d4 = _orient(p[j], q[j], q[i])
            if ((d1 > tolerance) != (d2 > tolerance)) and ((d3 > tolerance) != (d4 > tolerance)):
                return False
    return True


# --- background removal ----------------------------------------------------------------


def remove_background(image: np.ndarray) -> np.ndarray:
    """Return an RGBA image whose alpha marks the artwork. Raises rather than guessing.

    If the input already carries a usable alpha channel -- which every image in the owner's
    `extracted_stickers` corpus does -- that is trusted and returned. Otherwise `rembg` is
    tried, and if it is missing or produces something still fully opaque, this raises.

    **Never returns the input unchanged on failure.** Cause 5: the predecessor swallowed
    rembg errors and handed back the original opaque image, so the tracer saw a rectangle and
    emitted its bounding box. Square stickers were the visible symptom of an exception that
    was caught and dropped.
    """
    if image.ndim != 3 or image.shape[2] not in (3, 4):
        raise VinylCutterError(f"expected an RGB or RGBA image, got shape {image.shape}")

    if image.shape[2] == 4 and _alpha_is_meaningful(image[:, :, 3]):
        return image

    try:
        from rembg import remove  # noqa: PLC0415 -- heavy and optional, see module docstring
    except ImportError as exc:
        raise VinylCutterError(
            "image has no usable alpha channel and rembg is not installed, so the background "
            "cannot be removed. Refusing to trace an opaque image: it would trace the frame "
            "and cut a rectangle."
        ) from exc

    try:
        result = remove(image)
    except Exception as exc:
        raise VinylCutterError(f"rembg failed to remove the background: {exc}") from exc

    if result is None or result.ndim != 3 or result.shape[2] != 4:
        raise VinylCutterError("rembg returned something that is not an RGBA image")
    if not _alpha_is_meaningful(result[:, :, 3]):
        raise VinylCutterError(
            "rembg ran but returned a fully-opaque image, which is indistinguishable from it "
            "having done nothing. Tracing this would cut a rectangle."
        )
    return result


def _alpha_is_meaningful(alpha: np.ndarray) -> bool:
    """An alpha channel is useful only if it actually separates figure from ground."""
    if alpha.size == 0:
        return False
    transparent = int((alpha < 128).sum())
    opaque = int((alpha >= 128).sum())
    return transparent > 0 and opaque > 0


# --- tracing ---------------------------------------------------------------------------


def extract_contours(
    image: np.ndarray,
    *,
    px_per_mm: float,
    preset: TracePreset = PRESET_STICKER,
) -> list[Contour]:
    """Trace every boundary in an RGBA image at full resolution.

    Uses `cv2.findContours` with **RETR_CCOMP**, which returns all contours plus a two-level
    hierarchy -- outer boundaries and their holes. Holes are kept and marked, because the
    counters in letters like O and A have to cut or the lettering fills in.

    **There is no downscale option.** Tracing a 300 DPI sheet at 800 px was cause 2, and a
    parameter that reintroduces it is not worth the convenience.

    Returns contours in millimetres, positioned relative to the image's own top-left.
    """
    cv2 = _import_cv2()

    if image.shape[2] != 4:
        raise VinylCutterError("extract_contours needs RGBA; call remove_background first")

    alpha = image[:, :, 3]
    mask = (alpha >= preset.alpha_threshold).astype(np.uint8) * 255

    if preset.smooth_mm > 0:
        # Morphological close then open, at a radius well under the cut offset. This rounds
        # pixel staircases without moving the boundary far enough to matter.
        radius = max(1, int(round(preset.smooth_mm * px_per_mm)))
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)

    # CHAIN_APPROX_NONE keeps every boundary pixel. Approximation here would be the same
    # class of mistake as cause 2 -- trading geometric truth for point count.
    found, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hierarchy is None or len(found) == 0:
        return []

    hierarchy = hierarchy[0]
    contours: list[Contour] = []
    for i, raw in enumerate(found):
        pts = raw.reshape(-1, 2).astype(np.float64)
        if len(pts) < 3:
            continue
        if abs(signed_area(pts)) < preset.min_area_px:
            continue
        is_hole = hierarchy[i][3] != -1
        contours.append(Contour(points=pts / px_per_mm, is_hole=is_hole))

    logger.debug(
        "traced %d contours (%d holes) with preset %s",
        len(contours), sum(c.is_hole for c in contours), preset.name,
    )
    return contours


def _import_cv2():
    try:
        import cv2  # noqa: PLC0415 -- 44 MB; kept out of import time for the whole app
    except ImportError as exc:
        raise VinylCutterError(
            "opencv-python-headless is required for contour tracing (declared in "
            "requirements.txt)"
        ) from exc
    return cv2


# --- offsetting ------------------------------------------------------------------------


def offset_contours(
    contours: list[Contour],
    *,
    offset_mm: float = DEFAULT_CUT_OFFSET_MM,
    simplify_tolerance_mm: float = 0.0,
) -> list[np.ndarray]:
    """Grow the traced boundaries outward into a cut path, using Clipper.

    Round joins and closed-polygon end type. Clipper removes self-intersections as part of
    the offset rather than afterwards, which is the reason it is here and naive normal
    displacement is not -- that was cause 3, and it put a bowtie at every concave corner.

    Winding is normalised first, unconditionally (cause 4).

    `simplify_tolerance_mm` defaults to **off**. The old server simplified with a ~0.5 mm
    tolerance while offsetting 0.25 mm, so its simplification could move a point further than
    the offset it was trying to apply. Any value here is therefore checked against the
    offset and rejected if it is not comfortably smaller.
    """
    if offset_mm <= 0:
        raise VinylCutterError(f"offset_mm must be positive, got {offset_mm}")
    if simplify_tolerance_mm < 0:
        raise VinylCutterError("simplify_tolerance_mm cannot be negative")
    if simplify_tolerance_mm > offset_mm / 4:
        raise VinylCutterError(
            f"simplify_tolerance_mm={simplify_tolerance_mm} is too close to offset_mm="
            f"{offset_mm}. Simplification that moves points as far as the offset itself is "
            "how the previous implementation lost its cut geometry; keep it under a quarter."
        )
    if not contours:
        return []

    pyclipper = _import_pyclipper()
    contours = normalise_winding(contours)

    co = pyclipper.PyclipperOffset()
    for c in contours:
        path = [(int(round(x * CLIPPER_SCALE)), int(round(y * CLIPPER_SCALE))) for x, y in c.points]
        co.AddPath(path, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)

    solution = co.Execute(offset_mm * CLIPPER_SCALE)

    if simplify_tolerance_mm > 0:
        solution = pyclipper.CleanPolygons(solution, simplify_tolerance_mm * CLIPPER_SCALE)

    out: list[np.ndarray] = []
    for path in solution:
        if len(path) < 3:
            continue
        out.append(np.array(path, dtype=np.float64) / CLIPPER_SCALE)
    return out


def _import_pyclipper():
    try:
        import pyclipper  # noqa: PLC0415
    except ImportError as exc:
        raise VinylCutterError(
            "pyclipper is required for cut-path offsetting (declared in requirements.txt)"
        ) from exc
    return pyclipper


# --- SVG emission ----------------------------------------------------------------------


def path_to_svg_d(points: np.ndarray, *, precision: int = 3) -> str:
    """One closed subpath. Coordinates are millimetres."""
    if len(points) < 3:
        return ""
    head = f"M {points[0][0]:.{precision}f} {points[0][1]:.{precision}f}"
    rest = " ".join(f"L {x:.{precision}f} {y:.{precision}f}" for x, y in points[1:])
    return f"{head} {rest} Z"


def build_cut_svg(
    paths: list[np.ndarray],
    *,
    width_mm: float = SHEET_WIDTH_MM,
    height_mm: float = SHEET_HEIGHT_MM,
    one_subpath_per_contour: bool = True,
) -> str:
    """Render cut paths to an SVG sized in real millimetres.

    Each contour becomes its own `<path>` element so the blade lifts between shapes instead
    of dragging from one to the next.

    **This function cannot emit registration marks and that is intentional.** They belong on
    the printed PNG; the Cameo reads them optically off the print. The surviving broken
    output from the old system has them inside the cut SVG, where they get cut.
    """
    svg = ET.Element("svg", {
        "xmlns": "http://www.w3.org/2000/svg",
        "version": "1.1",
        "width": f"{width_mm:.4f}mm",
        "height": f"{height_mm:.4f}mm",
        "viewBox": f"0 0 {width_mm:.4f} {height_mm:.4f}",
    })
    title = ET.SubElement(svg, "title")
    title.text = "Cut lines"
    desc = ET.SubElement(svg, "desc")
    desc.text = "Cut paths only. Registration marks live on the printed sheet, not here."

    group = ET.SubElement(svg, "g", {
        "id": "cut",
        "fill": "none",
        "stroke": "#000000",
        "stroke-width": "0.1",
    })

    if one_subpath_per_contour:
        for i, pts in enumerate(paths):
            d = path_to_svg_d(pts)
            if d:
                ET.SubElement(group, "path", {"id": f"cut-{i}", "d": d})
    else:
        d = " ".join(filter(None, (path_to_svg_d(p) for p in paths)))
        if d:
            ET.SubElement(group, "path", {"id": "cut-all", "d": d})

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(svg, encoding="unicode")


# --- registration marks (printed sheet only) --------------------------------------------


def regmark_origins_mm(
    *,
    width_mm: float = SHEET_WIDTH_MM,
    height_mm: float = SHEET_HEIGHT_MM,
    margin_mm: float = REGMARK_MARGIN_MM,
) -> dict[str, tuple[float, float]]:
    """The three L-mark origins. Three marks only -- top-left, top-right, bottom-left.

    There is no fourth mark; the Cameo derives the fourth corner. Spacing is computed from
    the page size so it re-derives correctly if the paper ever changes, rather than carrying
    195.9 x 259.4 around as magic numbers.
    """
    return {
        "tl": (margin_mm, margin_mm),
        "tr": (width_mm - margin_mm, margin_mm),
        "bl": (margin_mm, height_mm - margin_mm),
    }


def content_area_mm(
    *,
    width_mm: float = SHEET_WIDTH_MM,
    height_mm: float = SHEET_HEIGHT_MM,
) -> tuple[float, float, float, float]:
    """Rectangle (x, y, w, h) that artwork may occupy without fouling the marks.

    The marks sit 10 mm in with a 5 mm square and 20 mm arms running along the page edges, so
    a nominal 0.5 in print margin is *not* enough on the mark edges -- artwork would land on
    the arms and the Cameo would misread them.
    """
    inset = REGMARK_MARGIN_MM + REGMARK_SQUARE_MM + REGMARK_CLEARANCE_MM
    return (inset, inset, width_mm - 2 * inset, height_mm - 2 * inset)


def draw_regmarks(draw, px_per_mm: float, *, width_mm: float = SHEET_WIDTH_MM,
                  height_mm: float = SHEET_HEIGHT_MM) -> None:
    """Paint the three L-marks onto a PIL draw context, in black.

    Arms always run *into* the page from their corner, which is what the scanner expects.
    """
    def rect(x_mm: float, y_mm: float, w_mm: float, h_mm: float) -> None:
        draw.rectangle(
            [x_mm * px_per_mm, y_mm * px_per_mm,
             (x_mm + w_mm) * px_per_mm, (y_mm + h_mm) * px_per_mm],
            fill=(0, 0, 0, 255),
        )

    s, arm, t = REGMARK_SQUARE_MM, REGMARK_ARM_MM, REGMARK_STROKE_MM
    origins = regmark_origins_mm(width_mm=width_mm, height_mm=height_mm)

    ox, oy = origins["tl"]
    rect(ox, oy, s, s)
    rect(ox + s, oy, arm, t)
    rect(ox, oy + s, t, arm)

    ox, oy = origins["tr"]
    rect(ox - s, oy, s, s)
    rect(ox - s - arm, oy, arm, t)
    rect(ox - t, oy + s, t, arm)

    ox, oy = origins["bl"]
    rect(ox, oy - s, s, s)
    rect(ox + s, oy - t, arm, t)
    rect(ox, oy - s - arm, t, arm)


def densify(path: np.ndarray, step_mm: float) -> np.ndarray:
    """Resample a closed path so no gap between consecutive points exceeds `step_mm`.

    Necessary because a straight chord is invisible at its own endpoints. Both ends of a bad
    segment sit on the legitimate boundary at the correct clearance; only its middle crosses
    the artwork. Checking vertices alone therefore cannot see the very bug this module exists
    to prevent -- the samples have to be laid along the segments.
    """
    if len(path) < 2 or step_mm <= 0:
        return path
    closed = np.vstack([path, path[:1]])
    seg = np.diff(closed, axis=0)
    lengths = np.hypot(seg[:, 0], seg[:, 1])

    chunks: list[np.ndarray] = []
    for i, length in enumerate(lengths):
        n = max(1, int(np.ceil(length / step_mm)))
        t = np.linspace(0.0, 1.0, n, endpoint=False)[:, None]
        chunks.append(closed[i] + t * seg[i])
    return np.vstack(chunks)


def cut_path_clearances_mm(
    image: np.ndarray,
    paths: list[np.ndarray],
    *,
    px_per_mm: float,
    alpha_threshold: int = 128,
    sample_step_mm: float | None = None,
    ignore_subpixel_fringe: bool = True,
) -> np.ndarray:
    """Distance from every point along the cut path to the nearest artwork pixel, in mm.

    Samples **along** the path, not just at its vertices -- see `densify` for why that
    distinction decides whether this catches the straight-chord bug at all.

    This is the honest way to check a cut path, and it exists because the obvious check does
    not work. "No segment longer than some fraction of the bounding-box diagonal" sounds like
    it catches the straight-chord bug, but a legitimately rectangular sticker has a straight
    edge worth 0.707 of its own diagonal -- and the corpus on disk contains exactly that, so
    the naive check both misses real bugs and fails on correct output.

    The invariant that actually holds: an outward offset of a region by `d` is its Minkowski
    sum with a disc of radius `d`, so **every point on the resulting boundary is at distance
    exactly `d` from the artwork**. That is true regardless of how convex, concave, rectangular
    or organic the shape is.

    A blade driving straight across the design violates it enormously -- a chord over the
    artwork reads a clearance of 0, not `d`. Returned as an array so callers can assert on
    the distribution rather than a single number.
    """
    cv2 = _import_cv2()
    if image.shape[2] != 4:
        raise VinylCutterError("cut_path_clearances_mm needs the RGBA image that was traced")

    solid = (image[:, :, 3] >= alpha_threshold).astype(np.uint8)

    if ignore_subpixel_fringe:
        # Scanned artwork has antialiased edges that taper to features one pixel wide -- the
        # corpus is full of them, e.g. the wings on `Scan_20251229 (15)_013.png`. A polygon
        # cannot enclose a feature thinner than a pixel, so the tracer legitimately cuts
        # across them and the raw alpha then sits a little outside the traced boundary.
        # Measuring against that raw alpha reports a phantom violation that is an artefact of
        # the ruler, not the cut path.
        #
        # A 3x3 opening removes exactly those one-pixel fringes and leaves everything the
        # tracer can actually represent. It moves the reference by at most ~1 px (0.085 mm at
        # 300 DPI), which is two orders of magnitude below the 2 mm miss this function exists
        # to detect, so it cannot hide the real bug: a chord across the solid body of a
        # sticker is untouched by it.
        kernel = np.ones((3, 3), np.uint8)
        opened = cv2.morphologyEx(solid, cv2.MORPH_OPEN, kernel)
        if opened.any():
            solid = opened

    if not paths:
        return np.array([])

    step = sample_step_mm if sample_step_mm is not None else 1.0 / px_per_mm
    paths = [densify(p, step) for p in paths]

    # The cut path is grown outward, so vertices legitimately sit outside the original frame
    # -- always, for any sticker whose artwork reaches the edge of its own image, which is
    # most of them. The mask is padded to contain them rather than the lookup being clamped:
    # clamping would report the border pixel's distance for an outside vertex, which is
    # smaller than the truth, and would manufacture exactly the failure this is looking for.
    src_h, src_w = solid.shape
    all_px = np.vstack(paths) * px_per_mm
    lo = all_px.min(axis=0)
    hi = all_px.max(axis=0)
    pad = int(np.ceil(max(
        0.0,
        -lo[0], -lo[1],
        hi[0] - (src_w - 1), hi[1] - (src_h - 1),
    ))) + 4

    padded = np.pad(solid, pad, mode="constant", constant_values=0)
    # DIST_MASK_PRECISE, not the 3x3/5x5 chamfer approximations: those carry a couple of
    # percent of error, which at a 2 mm offset is the same order as the tolerance being
    # asserted against. An approximate ruler makes an exact invariant untestable.
    dist_px = cv2.distanceTransform(
        (1 - padded).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE,
    )

    h, w = dist_px.shape
    out: list[np.ndarray] = []
    for path in paths:
        px = np.rint(path * px_per_mm).astype(np.int64) + pad
        xs = px[:, 0]
        ys = px[:, 1]
        if xs.min() < 0 or ys.min() < 0 or xs.max() >= w or ys.max() >= h:
            raise VinylCutterError("internal: padding failed to contain the cut path")
        out.append(dist_px[ys, xs] / px_per_mm)
    return np.concatenate(out)


def _import_pil():
    try:
        from PIL import Image, ImageDraw  # noqa: PLC0415
    except ImportError as exc:
        raise VinylCutterError("Pillow is required to render the print sheet") from exc
    return Image, ImageDraw


# --- the vendored driver ----------------------------------------------------------------


def load_driver_media() -> list[tuple]:
    """Read the MEDIA table out of the vendored inkscape-silhouette driver.

    This is the only thing Jarvis takes from `vendor/inkscape-silhouette/` at runtime, and it
    is read-only: the table records real pressure/speed/depth values per material, which is
    worth validating a blade recipe against. Nothing here opens a USB device.

    The import is lazy and the path insertion is undone afterwards, so a missing driver
    degrades to a clear error instead of breaking this module's import.
    """
    import sys  # noqa: PLC0415

    if not (VENDOR_DRIVER_PATH / "silhouette" / "Graphtec.py").is_file():
        raise VinylCutterError(f"vendored Silhouette driver not found at {VENDOR_DRIVER_PATH}")

    path_str = str(VENDOR_DRIVER_PATH)
    added = path_str not in sys.path
    if added:
        sys.path.insert(0, path_str)
    try:
        from silhouette.Graphtec import MEDIA  # noqa: PLC0415
    except ImportError as exc:
        raise VinylCutterError(
            f"could not import the vendored Silhouette driver: {exc}. It needs pyusb."
        ) from exc
    finally:
        if added and path_str in sys.path:
            sys.path.remove(path_str)
    return list(MEDIA)


# --- sheet layout -----------------------------------------------------------------------


@dataclass
class PlacedSticker:
    """One sticker positioned on the sheet, with its cut geometry already in sheet space."""

    source: str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    cut_paths: list[np.ndarray] = field(default_factory=list, repr=False)


def layout_stickers(
    sizes_mm: list[tuple[float, float]],
    *,
    width_mm: float = SHEET_WIDTH_MM,
    height_mm: float = SHEET_HEIGHT_MM,
    gap_mm: float = STICKER_GAP_MM,
) -> list[tuple[int, float, float] | None]:
    """Shelf-pack sticker sizes into the usable content area.

    Returns one entry per input: `(index, x_mm, y_mm)` if it fitted, `None` if it did not, so
    the caller can report what was left over rather than silently dropping it.

    The area used is `content_area_mm()`, not the nominal print margin -- artwork placed in
    the nominal margin would sit on the registration-mark arms and stop the Cameo reading
    them.
    """
    cx, cy, cw, ch = content_area_mm(width_mm=width_mm, height_mm=height_mm)

    placements: list[tuple[int, float, float] | None] = [None] * len(sizes_mm)
    # Tallest-first packs noticeably tighter than input order and costs nothing.
    order = sorted(range(len(sizes_mm)), key=lambda i: -sizes_mm[i][1])

    shelf_x = cx
    shelf_y = cy
    shelf_h = 0.0
    for i in order:
        w, h = sizes_mm[i]
        if w > cw or h > ch:
            continue  # cannot ever fit on this sheet
        if shelf_x + w > cx + cw:
            shelf_y += shelf_h + gap_mm
            shelf_x = cx
            shelf_h = 0.0
        if shelf_y + h > cy + ch:
            continue  # sheet is full
        placements[i] = (i, shelf_x, shelf_y)
        shelf_x += w + gap_mm
        shelf_h = max(shelf_h, h)
    return placements


# --- top level --------------------------------------------------------------------------


def generate_cut_sheet(
    image_paths: list[str | Path],
    output_dir: str | Path,
    *,
    name: str = "sheet",
    offset_mm: float = DEFAULT_CUT_OFFSET_MM,
    preset: TracePreset = PRESET_STICKER,
    dpi: int = PRINT_DPI,
    sheet_width_mm: float = SHEET_WIDTH_MM,
    sheet_height_mm: float = SHEET_HEIGHT_MM,
) -> dict:
    """Generate a printable sheet (PNG, with registration marks) and a cut file (SVG, without).

    This is the whole deliverable: two files the owner prints and then loads into the Cameo by
    hand. **Nothing here talks to the cutter.**

    Returns a summary dict including anything that did not fit or could not be traced, so a
    caller can say so rather than quietly producing a sheet with fewer stickers than asked
    for.
    """
    cv2 = _import_cv2()
    Image, ImageDraw = _import_pil()

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    px_per_mm = dpi / 25.4

    loaded: list[tuple[str, np.ndarray]] = []
    skipped: list[dict] = []
    for p in image_paths:
        p = Path(p)
        try:
            raw = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
            if raw is None:
                raise VinylCutterError(f"could not read image {p}")
            if raw.ndim == 3 and raw.shape[2] == 4:
                rgba = cv2.cvtColor(raw, cv2.COLOR_BGRA2RGBA)
            elif raw.ndim == 3:
                rgba = cv2.cvtColor(raw, cv2.COLOR_BGR2RGBA)
            else:
                rgba = cv2.cvtColor(raw, cv2.COLOR_GRAY2RGBA)
            loaded.append((str(p), remove_background(rgba)))
        except VinylCutterError as exc:
            # One unusable image should not lose the whole sheet, but it is reported loudly.
            logger.warning("skipping %s: %s", p, exc)
            skipped.append({"source": str(p), "reason": str(exc)})

    if not loaded:
        raise VinylCutterError("no usable images: nothing to lay out")

    sizes = [(img.shape[1] / px_per_mm, img.shape[0] / px_per_mm) for _, img in loaded]
    placements = layout_stickers(sizes, width_mm=sheet_width_mm, height_mm=sheet_height_mm)

    sheet_px = (int(round(sheet_width_mm * px_per_mm)), int(round(sheet_height_mm * px_per_mm)))
    canvas = Image.new("RGBA", sheet_px, (255, 255, 255, 255))

    placed: list[PlacedSticker] = []
    for (source, img), size, place in zip(loaded, sizes, placements):
        if place is None:
            skipped.append({"source": source, "reason": "did not fit on the sheet"})
            continue
        _, x_mm, y_mm = place

        contours = extract_contours(img, px_per_mm=px_per_mm, preset=preset)
        if not contours:
            skipped.append({"source": source, "reason": "traced to nothing"})
            continue
        shifted = [Contour(points=c.points + np.array([x_mm, y_mm]), is_hole=c.is_hole)
                   for c in contours]
        cut_paths = offset_contours(shifted, offset_mm=offset_mm)

        canvas.alpha_composite(
            Image.fromarray(img, "RGBA"),
            (int(round(x_mm * px_per_mm)), int(round(y_mm * px_per_mm))),
        )
        placed.append(PlacedSticker(
            source=source, x_mm=x_mm, y_mm=y_mm,
            width_mm=size[0], height_mm=size[1], cut_paths=cut_paths,
        ))

    if not placed:
        raise VinylCutterError("nothing could be placed on the sheet")

    # Registration marks: printed sheet only. build_cut_svg has no way to add them.
    draw_regmarks(ImageDraw.Draw(canvas), px_per_mm,
                  width_mm=sheet_width_mm, height_mm=sheet_height_mm)

    print_path = output_dir / f"{name}_PRINT.png"
    canvas.convert("RGB").save(print_path, dpi=(dpi, dpi))

    all_paths = [p for s in placed for p in s.cut_paths]
    svg = build_cut_svg(all_paths, width_mm=sheet_width_mm, height_mm=sheet_height_mm)
    cut_path = output_dir / f"{name}_CUT.svg"
    cut_path.write_text(svg, encoding="utf-8")

    logger.info("generated %s (%d stickers, %d cut paths, %d skipped)",
                name, len(placed), len(all_paths), len(skipped))

    return {
        "print_png": str(print_path),
        "cut_svg": str(cut_path),
        "placed": len(placed),
        "cut_paths": len(all_paths),
        "skipped": skipped,
        "offset_mm": offset_mm,
        "preset": preset.name,
        "dpi": dpi,
    }
