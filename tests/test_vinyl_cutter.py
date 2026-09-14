"""Tests for the vinyl cut-path engine (assistant/core/vinyl_cutter.py).

The engine replaces a predecessor that was measurably broken: every cut path it produced
contained a straight segment spanning 50-65% of the sticker's width -- the blade driving
across the design instead of around it. Five causes were diagnosed, and most of what is here
exists to hold one of them shut.

**The central test is `test_cut_path_stays_the_offset_away_from_the_artwork`.** Two things
about how it is built matter more than the assertion itself, because both were arrived at by
having the obvious version fail.

*Why not "no segment longer than a fraction of the bounding-box diagonal".* That sounds like
it catches a straight chord, but a rectangular sticker's own edge is 0.707 of its diagonal,
and the owner's corpus contains exactly that -- `Scan_20251229 (7)_013.png` measures 0.693.
The naive check therefore fails on correct output, while a chord across a square sticker slips
under it. `test_a_correct_path_on_a_square_sticker_has_a_long_segment_and_that_is_fine` pins
that down.

*What is used instead.* An outward offset by `d` is the Minkowski sum with a disc of radius
`d`, so every point of the result sits at distance exactly `d` from the artwork, whatever the
shape. A chord across the design reads 0. `test_the_clearance_check_catches_an_injected_chord`
injects the original bug and proves the check fires, rather than passing vacuously -- an
earlier draft of that control was silently useless because the chord it injected spanned a
concave bay of empty space instead of crossing any artwork, so it now verifies that the chord
actually lands on the artwork before asserting anything.

**A known limitation, recorded deliberately.** On a mathematically sharp cusp -- two circles
meeting at a tangent point -- the shape narrows below one pixel, and the rasterised alpha
holds pixels that `findContours` cannot enclose in a polygon. The cut path is then correct
with respect to the traced geometry but reads ~1 mm optimistic against the raw mask. Real
artwork does not do this (the corpus measures within 0.09 mm), so the fixtures here use
organic shapes rather than synthetic cusps; it is written down so the next person does not
rediscover it as a phantom bug.

Tests needing the owner's 238-image corpus skip when `D:\\Vinyl Stuff` is absent, so CI still
runs everything checkable from synthetic geometry -- which is all of the important parts.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from assistant.core import vinyl_cutter as vc

SVG_NS = "{http://www.w3.org/2000/svg}"
STICKER_CORPUS = Path("D:/Vinyl Stuff/extracted_stickers")
needs_corpus = pytest.mark.skipif(
    not STICKER_CORPUS.is_dir(),
    reason="owner's sticker corpus on D: is not present (e.g. on CI)",
)

PPM = 300 / 25.4  # px per mm at the calibrated 300 DPI

# ~3 px of rasterisation slack at 300 DPI. Measured, not guessed: real stickers land within
# 0.09 mm and a hard-cornered synthetic rectangle within 0.17 mm, while the bug being guarded
# against misses by the full 2 mm offset. There is an order of magnitude between the two.
TOLERANCE_MM = 3.0 / PPM


# --- fixtures: synthetic artwork ---------------------------------------------------------


def _rgba_from_mask(mask: np.ndarray) -> np.ndarray:
    """Wrap a boolean mask as an RGBA image with alpha marking the artwork."""
    h, w = mask.shape
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[:, :, :3] = 40
    img[:, :, 3] = np.where(mask, 255, 0).astype(np.uint8)
    return img


@pytest.fixture
def blob():
    """Three overlapping discs: organic outline with genuine concave notches.

    Chosen to stand in for a real sticker. The concave junctions where the discs meet are
    where naive normal-displacement offsetting grew bowties, so a regression there shows up
    here -- but the boundary stays smooth, with no sub-pixel cusp to confuse the measurement.
    """
    size = 600
    yy, xx = np.mgrid[0:size, 0:size]
    return _rgba_from_mask(
        ((xx - 230) ** 2 + (yy - 250) ** 2 <= 130 ** 2)
        | ((xx - 360) ** 2 + (yy - 250) ** 2 <= 110 ** 2)
        | ((xx - 300) ** 2 + (yy - 380) ** 2 <= 120 ** 2)
    )


@pytest.fixture
def ring():
    """An annulus -- the simplest thing with a genuine interior hole.

    Holes are not cosmetic: the counters in letters like O and A have to cut, or vinyl
    lettering fills in solid.
    """
    size = 500
    yy, xx = np.mgrid[0:size, 0:size]
    r2 = (xx - 250) ** 2 + (yy - 250) ** 2
    return _rgba_from_mask((r2 <= 200 ** 2) & (r2 >= 90 ** 2))


@pytest.fixture
def disc():
    """A plain solid disc. Convex, so any chord across it is guaranteed to cross artwork."""
    size = 600
    yy, xx = np.mgrid[0:size, 0:size]
    return _rgba_from_mask((xx - 300) ** 2 + (yy - 300) ** 2 <= 180 ** 2)


@pytest.fixture
def square_sticker():
    """A solid rectangle. Correct output for this has one very long straight segment."""
    mask = np.zeros((600, 600), dtype=bool)
    mask[100:500, 150:450] = True
    return _rgba_from_mask(mask)


def _paths_and_clearances(img, offset_mm):
    contours = vc.extract_contours(img, px_per_mm=PPM)
    paths = vc.offset_contours(contours, offset_mm=offset_mm)
    return paths, vc.cut_path_clearances_mm(img, paths, px_per_mm=PPM)


# --- signed area and winding (cause 4) ---------------------------------------------------


def test_signed_area_sign_tracks_winding_direction():
    square = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]])
    assert vc.signed_area(square) == pytest.approx(100.0)
    assert vc.signed_area(square[::-1]) == pytest.approx(-100.0)


def test_signed_area_of_a_degenerate_path_is_zero():
    assert vc.signed_area(np.array([[0.0, 0.0], [1.0, 1.0]])) == 0.0


def test_normalise_winding_makes_outers_positive_and_holes_negative():
    """The whole point of cause 4: Clipper needs consistent winding to offset the right way."""
    square = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]])
    outer = vc.Contour(points=square[::-1].copy(), is_hole=False)   # starts wrong
    hole = vc.Contour(points=square.copy(), is_hole=True)           # starts wrong

    fixed = vc.normalise_winding([outer, hole])

    assert fixed[0].signed_area() > 0, "outer boundary must end up positively wound"
    assert fixed[1].signed_area() < 0, "hole must end up wound opposite to the outer"


def test_normalise_winding_leaves_already_correct_contours_alone():
    square = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]])
    before = vc.Contour(points=square.copy(), is_hole=False)
    after = vc.normalise_winding([before])[0]
    np.testing.assert_allclose(after.points, square)


# --- tracing --------------------------------------------------------------------------


def test_tracing_a_ring_finds_the_hole(ring):
    """RETR_CCOMP has to report the interior boundary, not just the silhouette."""
    contours = vc.extract_contours(ring, px_per_mm=PPM)
    assert len(contours) == 2
    assert sum(c.is_hole for c in contours) == 1


def test_tracing_keeps_full_resolution(blob):
    """Cause 2 was tracing a 300 DPI sheet at 800 px. Steps should be ~1 pixel, not ~1 mm."""
    contours = vc.extract_contours(blob, px_per_mm=PPM)
    outer = max(contours, key=lambda c: abs(c.signed_area()))
    assert vc.max_segment_length(outer.points) < 2.0 / PPM


def test_despeckling_drops_specks_below_the_preset_floor():
    """potrace's turdSize: vinyl wants 10, stickers 2.

    The speck is sized so the two presets genuinely disagree about it -- its traced polygon
    area falls between the two floors.
    """
    mask = np.zeros((200, 200), dtype=bool)
    mask[50:150, 50:150] = True
    mask[10:14, 10:14] = True  # 4x4 px -> traced polygon area 9
    img = _rgba_from_mask(mask)

    assert len(vc.extract_contours(img, px_per_mm=PPM, preset=vc.PRESET_VINYL)) == 1
    assert len(vc.extract_contours(img, px_per_mm=PPM, preset=vc.PRESET_STICKER)) == 2


def test_tracing_a_non_rgba_image_is_refused():
    with pytest.raises(vc.VinylCutterError, match="RGBA"):
        vc.extract_contours(np.zeros((10, 10, 3), np.uint8), px_per_mm=PPM)


# --- offsetting (causes 3 and 4) ---------------------------------------------------------


def test_offsetting_grows_the_shape_outward(blob):
    contours = vc.extract_contours(blob, px_per_mm=PPM)
    before = sum(abs(c.signed_area()) for c in contours)
    after = sum(abs(vc.signed_area(p)) for p in vc.offset_contours(contours, offset_mm=2.0))
    assert after > before, "an outward offset that shrinks the shape cuts through the artwork"


def test_offsetting_still_grows_when_the_input_winding_is_reversed(blob):
    """Cause 4, stated as behaviour: reversed input must not offset inward.

    The predecessor had a polygonArea() helper it never called, so a contour that came back
    reversed quietly offset the wrong way and the blade cut into the design.
    """
    contours = vc.extract_contours(blob, px_per_mm=PPM)
    flipped = [vc.Contour(points=c.points[::-1].copy(), is_hole=c.is_hole) for c in contours]

    normal = sum(abs(vc.signed_area(p)) for p in vc.offset_contours(contours, offset_mm=2.0))
    reversed_ = sum(abs(vc.signed_area(p)) for p in vc.offset_contours(flipped, offset_mm=2.0))

    assert reversed_ == pytest.approx(normal, rel=0.02)


def test_offsetting_a_ring_shrinks_its_hole(ring):
    """Outward from the artwork means the counter gets smaller, not larger.

    The material is the region between the two boundaries; growing it must eat into the hole.
    """
    contours = vc.extract_contours(ring, px_per_mm=PPM)
    hole_before = min(abs(c.signed_area()) for c in contours)
    paths = vc.offset_contours(contours, offset_mm=1.5)
    assert len(paths) == 2
    assert min(abs(vc.signed_area(p)) for p in paths) < hole_before


def test_offset_output_has_no_self_intersections(blob):
    """Cause 3: naive normal displacement grew a bowtie at every concave corner.

    Clipper removes self-intersections inside the offset algorithm, which is the reason it is
    used at all -- a bowtie here means someone swapped it back for displacement.
    """
    for path in vc.offset_contours(vc.extract_contours(blob, px_per_mm=PPM), offset_mm=2.0):
        sampled = path[::5] if len(path) > 400 else path
        assert vc.is_simple_polygon(sampled), "offset path crosses itself"


def test_a_non_positive_offset_is_refused(blob):
    with pytest.raises(vc.VinylCutterError, match="must be positive"):
        vc.offset_contours(vc.extract_contours(blob, px_per_mm=PPM), offset_mm=0.0)


def test_simplification_bigger_than_a_quarter_of_the_offset_is_refused(blob):
    """Cause 2's other half: the old server simplified 0.5 mm while offsetting 0.25 mm.

    A tolerance that can move a point further than the offset being applied destroys the
    geometry it is meant to preserve, so it is rejected rather than accepted.
    """
    with pytest.raises(vc.VinylCutterError, match="too close to offset_mm"):
        vc.offset_contours(vc.extract_contours(blob, px_per_mm=PPM),
                           offset_mm=2.0, simplify_tolerance_mm=0.6)


# --- the straight-chord regression, and proof that it fires -----------------------------


@pytest.mark.parametrize("offset_mm", [1.0, 2.0, 3.0])
def test_cut_path_stays_the_offset_away_from_the_artwork(blob, offset_mm):
    """THE regression test for the bug the owner actually hit.

    Every point of a correct outward offset lies at distance `offset_mm` from the artwork.
    A blade driving across the design misses by the full width of the sticker.
    """
    _, clearances = _paths_and_clearances(blob, offset_mm)

    assert clearances.min() > offset_mm - TOLERANCE_MM, (
        f"a cut point came within {clearances.min():.3f} mm of the artwork, expected "
        f"{offset_mm} mm -- this is the straight-chord bug"
    )
    assert abs(clearances - offset_mm).max() < TOLERANCE_MM
    assert (clearances < offset_mm / 2).sum() == 0


def test_the_clearance_check_catches_an_injected_chord(disc):
    """Negative control: reproduce the original bug and confirm the check fails on it.

    A test that has never failed proves nothing. The old tracer only appended un-visited
    cells, so backtracking produced a straight jump across the shape. That is simulated by
    replacing half the path with a single chord.

    The control validates itself first: an earlier version of this test was useless because
    the chord it injected crossed a concave bay of empty space and never touched artwork, so
    it produced statistics identical to a correct path. The midpoint is checked to be on the
    artwork before anything is concluded.
    """
    paths, good = _paths_and_clearances(disc, 2.0)
    path = max(paths, key=len)
    half = len(path) // 2

    broken = np.vstack([path[:half], path[:1]])  # straight back across the disc

    midpoint = (path[0] + path[half]) / 2.0
    mid_px = np.rint(midpoint * PPM).astype(int)
    assert disc[mid_px[1], mid_px[0], 3] >= 128, (
        "control is invalid: the injected chord does not cross artwork"
    )

    bad = vc.cut_path_clearances_mm(disc, [broken], px_per_mm=PPM)

    assert good.min() > 2.0 - TOLERANCE_MM, "sanity: the unbroken path is fine"
    assert bad.min() < 0.5, "the check must notice a chord laid across the artwork"
    assert (bad < 1.0).mean() > 0.1, "and must see it as a sustained run, not one stray point"


def test_sampling_only_vertices_would_miss_the_chord(disc):
    """Why `cut_path_clearances_mm` densifies instead of reading the vertex list.

    Both ends of a bad segment sit on the real boundary at the correct clearance; only its
    middle crosses the artwork. This asserts the distinction is real, so nobody optimises the
    densify step away and leaves a check that cannot fail.
    """
    paths, _ = _paths_and_clearances(disc, 2.0)
    path = max(paths, key=len)
    broken = np.vstack([path[: len(path) // 2], path[:1]])

    vertices_only = vc.cut_path_clearances_mm(
        disc, [broken], px_per_mm=PPM, sample_step_mm=1e9,
    )
    densified = vc.cut_path_clearances_mm(disc, [broken], px_per_mm=PPM)

    assert vertices_only.min() > 1.5, "vertex-only sampling sees nothing wrong"
    assert densified.min() < 0.5, "sampling along the path sees the chord"


def test_a_correct_path_on_a_square_sticker_has_a_long_segment_and_that_is_fine(square_sticker):
    """Why the naive bounding-box-diagonal check was rejected as the regression test.

    A rectangle's own edge is ~0.707 of its diagonal, and the real corpus contains stickers
    like this, so a threshold low enough to catch a chord would fail them. The clearance
    invariant holds here regardless, which is the point.
    """
    paths, clearances = _paths_and_clearances(square_sticker, 2.0)
    longest = max(vc.max_segment_length(p) / vc.bbox_diagonal(p) for p in paths)

    assert longest > 0.5, "a rectangle legitimately has a very long straight segment"
    assert clearances.min() > 2.0 - TOLERANCE_MM, "yet it never approaches the artwork"


def test_densify_respects_its_step_and_closes_the_path():
    square = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]])
    dense = vc.densify(square, 0.5)
    assert vc.max_segment_length(dense) <= 0.5 + 1e-9
    assert len(dense) == 80


# --- background removal (cause 5) --------------------------------------------------------


def test_an_opaque_image_is_never_silently_returned():
    """Cause 5, exactly. rembg failed, the error was swallowed, the original came back
    opaque, the tracer saw a rectangle, and stickers cut as squares.
    """
    opaque = np.zeros((50, 50, 4), dtype=np.uint8)
    opaque[:, :, 3] = 255
    with pytest.raises(vc.VinylCutterError):
        vc.remove_background(opaque)


def test_a_fully_transparent_image_is_also_refused():
    with pytest.raises(vc.VinylCutterError):
        vc.remove_background(np.zeros((50, 50, 4), dtype=np.uint8))


def test_a_usable_alpha_channel_is_passed_through(blob):
    assert vc.remove_background(blob) is blob


def test_a_nonsense_shape_is_refused():
    with pytest.raises(vc.VinylCutterError, match="RGB or RGBA"):
        vc.remove_background(np.zeros((10, 10), dtype=np.uint8))


# --- SVG output --------------------------------------------------------------------------


def test_cut_svg_is_well_formed_and_sized_in_real_millimetres(blob):
    paths = vc.offset_contours(vc.extract_contours(blob, px_per_mm=PPM), offset_mm=2.0)
    root = ET.fromstring(vc.build_cut_svg(paths))

    assert root.get("width").endswith("mm")
    assert root.get("height").endswith("mm")
    assert float(root.get("width")[:-2]) == pytest.approx(vc.SHEET_WIDTH_MM)
    assert float(root.get("height")[:-2]) == pytest.approx(vc.SHEET_HEIGHT_MM)
    # The viewBox must carry the same units as width/height or everything scales wrong.
    assert root.get("viewBox").split()[2:] == [
        f"{vc.SHEET_WIDTH_MM:.4f}", f"{vc.SHEET_HEIGHT_MM:.4f}",
    ]


def test_each_contour_becomes_its_own_subpath(ring):
    """So the blade lifts between shapes instead of dragging from one to the next."""
    paths = vc.offset_contours(vc.extract_contours(ring, px_per_mm=PPM), offset_mm=1.5)
    root = ET.fromstring(vc.build_cut_svg(paths))
    assert len(list(root.iter(f"{SVG_NS}path"))) == len(paths) == 2


def test_every_subpath_is_explicitly_closed(blob):
    paths = vc.offset_contours(vc.extract_contours(blob, px_per_mm=PPM), offset_mm=2.0)
    root = ET.fromstring(vc.build_cut_svg(paths))
    for node in root.iter(f"{SVG_NS}path"):
        assert node.get("d").rstrip().endswith("Z"), "an open path leaves the sticker attached"


def test_the_cut_svg_contains_no_registration_marks(blob):
    """They go on the printed PNG; the Cameo reads them optically off the print.

    The surviving broken output from the old system has them inside the cut SVG, where the
    blade cuts them. `build_cut_svg` has no parameter that could add them -- this asserts the
    resulting file, not merely the intent.
    """
    paths = vc.offset_contours(vc.extract_contours(blob, px_per_mm=PPM), offset_mm=2.0)
    svg = vc.build_cut_svg(paths)

    assert list(ET.fromstring(svg).iter(f"{SVG_NS}rect")) == [], "regmarks are rects; none here"
    assert "regmark" not in svg.lower()


# --- registration marks and sheet geometry ----------------------------------------------


def test_regmark_origins_match_the_calibrated_geometry():
    origins = vc.regmark_origins_mm()
    assert set(origins) == {"tl", "tr", "bl"}, "three L-marks only; there is no fourth"
    assert origins["tl"] == (10.0, 10.0)


def test_mark_to_mark_spacing_matches_the_note_and_re_derives_from_page_size():
    """195.9 x 259.4 mm on Letter. Derived, so a different paper size stays correct."""
    assert vc.REGMARK_SPACING_X_MM == pytest.approx(195.9)
    assert vc.REGMARK_SPACING_Y_MM == pytest.approx(259.4)

    origins = vc.regmark_origins_mm()
    assert origins["tr"][0] - origins["tl"][0] == pytest.approx(195.9)
    assert origins["bl"][1] - origins["tl"][1] == pytest.approx(259.4)


def test_a4_page_re_derives_different_spacing():
    origins = vc.regmark_origins_mm(width_mm=210.0, height_mm=297.0)
    assert origins["tr"][0] - origins["tl"][0] == pytest.approx(190.0)
    assert origins["bl"][1] - origins["tl"][1] == pytest.approx(277.0)


def test_the_content_area_clears_the_registration_marks():
    """A nominal 0.5 in margin is not enough: the 20 mm arms run along the page edges."""
    x, y, w, h = vc.content_area_mm()
    assert x >= vc.REGMARK_MARGIN_MM + vc.REGMARK_SQUARE_MM
    assert x + w <= vc.SHEET_WIDTH_MM - vc.REGMARK_MARGIN_MM - vc.REGMARK_SQUARE_MM
    assert y + h <= vc.SHEET_HEIGHT_MM - vc.REGMARK_MARGIN_MM - vc.REGMARK_SQUARE_MM


def test_calibration_constants_match_the_obsidian_note():
    """These came off real cuts and nothing else records them. Pin them."""
    assert (vc.SHEET_WIDTH_MM, vc.SHEET_HEIGHT_MM) == (215.9, 279.4)
    assert vc.PRINT_DPI == 300
    assert (vc.REGMARK_SQUARE_MM, vc.REGMARK_ARM_MM, vc.REGMARK_STROKE_MM) == (5.0, 20.0, 0.5)
    assert vc.REGMARK_MARGIN_MM == 10.0
    assert vc.MIN_TEXT_HEIGHT_MM == pytest.approx(6.35)


def test_the_unresolved_cut_offset_defaults_to_the_credible_value():
    """0.25 mm and 2 mm both existed in the dead system. 2 mm is the documented choice.

    0.25 mm could not survive its own pipeline: that code path quantised at ~0.27 mm and
    simplified at ~0.5 mm, both larger than the offset it claimed to apply.
    """
    assert vc.DEFAULT_CUT_OFFSET_MM == 2.0

    # The discrepancy must stay written down beside the value; a bare 2.0 with no explanation
    # is how the disagreement got lost the first time. (A string literal after a module-level
    # assignment is not readable at runtime, so the source is checked instead.)
    source = Path(vc.__file__).read_text(encoding="utf-8")
    assert "0.25" in source and "8x" in source


def test_blade_recipes_match_the_note():
    assert (vc.RECIPE_STICKER.speed, vc.RECIPE_STICKER.pressure, vc.RECIPE_STICKER.depth) == (4, 15, 6)
    assert vc.RECIPE_STICKER.regmarks is True
    assert (vc.RECIPE_VINYL.speed, vc.RECIPE_VINYL.pressure, vc.RECIPE_VINYL.depth) == (3, 10, 2)
    assert vc.RECIPE_VINYL.regmarks is False
    assert vc.RECIPE_VINYL.tool == "autoblade"


def test_a_pressure_off_the_machines_scale_is_refused():
    """The Cameo's own scale maxes at 33."""
    with pytest.raises(ValueError, match=r"1\.\.33"):
        vc.BladeRecipe(name="bad", speed=1, pressure=40, depth=1, regmarks=False)


# --- layout ------------------------------------------------------------------------------


def test_layout_keeps_every_sticker_inside_the_content_area():
    sizes = [(30.0, 40.0)] * 12
    cx, cy, cw, ch = vc.content_area_mm()
    for place, (w, h) in zip(vc.layout_stickers(sizes), sizes):
        if place is None:
            continue
        _, x, y = place
        assert cx <= x and x + w <= cx + cw + 1e-9
        assert cy <= y and y + h <= cy + ch + 1e-9


def test_laid_out_stickers_do_not_overlap():
    sizes = [(25.0, 35.0), (40.0, 20.0), (30.0, 30.0), (50.0, 45.0), (20.0, 60.0)]
    boxes = [(p[1], p[2], w, h) for p, (w, h) in zip(vc.layout_stickers(sizes), sizes) if p]
    assert len(boxes) == len(sizes)
    for i, (x1, y1, w1, h1) in enumerate(boxes):
        for x2, y2, w2, h2 in boxes[i + 1:]:
            apart = x1 + w1 <= x2 or x2 + w2 <= x1 or y1 + h1 <= y2 or y2 + h2 <= y1
            assert apart, "stickers overlap on the sheet"


def test_something_too_big_for_the_sheet_is_reported_not_dropped():
    places = vc.layout_stickers([(10.0, 10.0), (9999.0, 9999.0)])
    assert places[0] is not None
    assert places[1] is None


# --- the vendored driver ----------------------------------------------------------------


def test_the_media_table_is_readable_from_the_vendored_driver():
    """Ties Phase 2 back to the preserved Phase 1 driver without touching hardware."""
    media = vc.load_driver_media()
    assert len(media) == 27
    assert all(row[1] is None or row[1] <= 33 for row in media)


def test_reading_the_media_table_does_not_leave_the_vendor_path_on_sys_path():
    import sys
    before = list(sys.path)
    vc.load_driver_media()
    assert sys.path == before


# --- end to end --------------------------------------------------------------------------


def test_generating_a_sheet_writes_both_files(tmp_path, blob, ring):
    """The deliverable: a printable PNG with marks and a cut SVG without them."""
    import cv2
    from PIL import Image

    srcs = []
    for i, img in enumerate((blob, ring)):
        p = tmp_path / f"s{i}.png"
        cv2.imwrite(str(p), cv2.cvtColor(img, cv2.COLOR_RGBA2BGRA))
        srcs.append(p)

    result = vc.generate_cut_sheet(srcs, tmp_path / "out", name="t")

    assert result["placed"] == 2
    assert result["skipped"] == []
    assert result["offset_mm"] == 2.0

    png = Path(result["print_png"])
    svg = Path(result["cut_svg"])
    assert png.is_file() and svg.is_file()

    with Image.open(png) as im:
        assert im.size == (2550, 3300), "Letter at 300 DPI"

    root = ET.fromstring(svg.read_text(encoding="utf-8"))
    assert len(list(root.iter(f"{SVG_NS}path"))) == result["cut_paths"] == 3
    assert list(root.iter(f"{SVG_NS}rect")) == []


def test_generating_a_sheet_with_no_usable_image_raises(tmp_path):
    import cv2
    opaque = np.full((40, 40, 4), 255, dtype=np.uint8)
    p = tmp_path / "opaque.png"
    cv2.imwrite(str(p), cv2.cvtColor(opaque, cv2.COLOR_RGBA2BGRA))
    with pytest.raises(vc.VinylCutterError, match="no usable images"):
        vc.generate_cut_sheet([p], tmp_path / "out")


# --- against the owner's real corpus -----------------------------------------------------


@needs_corpus
def test_real_stickers_never_produce_a_chord_across_the_design():
    """The actual failure, measured on the actual images that exhibited it.

    Runs a spread of the 238-image corpus rather than a single file, because the original bug
    was shape-dependent -- it appeared where the boundary forced the tracer to backtrack.
    """
    import cv2

    files = sorted(STICKER_CORPUS.glob("*.png"))[::20]
    assert files, "corpus directory present but empty"

    checked = 0
    for f in files:
        raw = cv2.imread(str(f), cv2.IMREAD_UNCHANGED)
        if raw is None or raw.ndim != 3 or raw.shape[2] != 4:
            continue
        img = vc.remove_background(cv2.cvtColor(raw, cv2.COLOR_BGRA2RGBA))
        paths, clearances = _paths_and_clearances(img, 2.0)
        assert paths, f"{f.name} traced to nothing"
        assert clearances.min() > 2.0 - TOLERANCE_MM, (
            f"{f.name}: cut came within {clearances.min():.3f} mm of the artwork"
        )
        checked += 1
    assert checked >= 5


@needs_corpus
def test_the_known_rectangular_sticker_is_still_handled_correctly():
    """`Scan_20251229 (7)_013.png` is the one that measures 0.693 on the naive check.

    It is a real rectangle, so it is the case proving the naive bounding-box test would have
    been wrong. The clearance invariant holds on it.
    """
    import cv2

    f = STICKER_CORPUS / "Scan_20251229 (7)_013.png"
    if not f.is_file():
        pytest.skip("that particular sticker is not present")

    img = vc.remove_background(
        cv2.cvtColor(cv2.imread(str(f), cv2.IMREAD_UNCHANGED), cv2.COLOR_BGRA2RGBA)
    )
    paths, clearances = _paths_and_clearances(img, 2.0)
    naive = max(vc.max_segment_length(p) / vc.bbox_diagonal(p) for p in paths)

    assert naive > 0.5, "still the rectangle that defeats the naive check"
    assert clearances.min() > 2.0 - TOLERANCE_MM, "and still a correct cut path"
