"""Guards the vendored Silhouette Cameo driver at vendor/inkscape-silhouette.

This is a *preservation* test, not a feature test. The driver (inkscape-silhouette v1.29 by
Jürgen Weigert) was recovered from `D:\\Vinyl Stuff\\StickerSheets`, where it was gitignored,
and the server that ran it is dead — that disk held the only copy in this project's history.
These tests exist so it cannot quietly rot or be "tidied up" out of existence again.

Three things are checked, each for a specific reason:

**It still imports without Inkscape or wxPython.** Graphtec/Strategy/Geometry are the parts
worth keeping and they depend only on the standard library plus `usb`. If someone's refactor
makes them need `inkex`, that's a real regression and this catches it.

**The MEDIA and CAMEO_MATS tables are intact.** These are the pressure/speed/depth
calibration data — the actual crown jewel, and the thing that would be most expensive to
reconstruct.

**Copyright headers are byte-for-byte present.** This directory is GPL-2.0 inside an
otherwise non-GPL public repository. Stripping or reformatting a header would be a licensing
problem, not a style choice, so it is asserted rather than trusted.
"""

import re
from pathlib import Path

import pytest

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "inkscape-silhouette"


@pytest.fixture(autouse=True)
def _vendor_on_path(monkeypatch):
    """Put the vendored tree on sys.path the same way vinyl_cutter does, and take it back off.

    monkeypatch restores sys.path after each test so importing the driver here can't leak a
    path entry into the rest of the suite.
    """
    monkeypatch.syspath_prepend(str(VENDOR))


# --- the tree is actually there -------------------------------------------------------


def test_the_hardware_modules_are_all_present():
    """The four modules that justify keeping any of this at all."""
    for name in ("Graphtec.py", "Strategy.py", "StrategyMinTraveling.py", "Geometry.py"):
        assert (VENDOR / "silhouette" / name).is_file(), f"missing {name}"


def test_the_inkscape_plumbing_was_kept_too():
    """Deliberately kept despite nothing importing some of it -- see this dir's README.

    The salvage rule was that losing something is the expensive outcome. If a future cleanup
    wants these gone, that should be a decision someone makes on purpose, not a silent drift.
    """
    for name in ("MultiFrame.py", "ColorSeparation.py", "Dialog.py", "beutil.py",
                 "convert2dashes.py", "read_dump.py"):
        assert (VENDOR / "silhouette" / name).is_file(), f"missing {name}"
    assert (VENDOR / "sendto_silhouette.py").is_file()


def test_the_vendored_pyusb_fallback_is_gone():
    """We removed it on purpose in favour of a declared pyusb dependency.

    Graphtec.py only ever sys.path.append()ed it, so it was a fallback behind an installed
    pyusb. If it reappears, someone has re-vendored a BSD-3 tree inside a GPL-2.0 directory.
    """
    assert not (VENDOR / "silhouette" / "pyusb-1.0.2").exists()


# --- it still works -------------------------------------------------------------------


def test_the_driver_imports_without_inkscape_or_wx():
    """Headless import. Needs pyusb (declared in requirements.txt) but no GUI stack."""
    from silhouette.Graphtec import SilhouetteCameo

    assert SilhouetteCameo is not None


def test_the_media_calibration_table_survived():
    """MEDIA rows are (id, pressure, speed, depth, colour, description).

    The spot-check is a real row rather than just a length, so a table that got truncated or
    reordered fails rather than passing on count alone.
    """
    from silhouette.Graphtec import MEDIA

    assert len(MEDIA) == 27
    by_id = {row[0]: row for row in MEDIA}
    assert by_id[100] == (100, 27, 10, 1, "yellow", "Card without Craft Paper Backing")
    # The owner's two blade recipes live in the 1..33 pressure range this table is scaled to.
    pressures = [row[1] for row in MEDIA if row[1] is not None]
    assert max(pressures) <= 33


def test_the_cutting_mat_definitions_survived():
    from silhouette.Graphtec import CAMEO_MATS

    assert "cameo_12x12" in CAMEO_MATS
    assert "no_mat" in CAMEO_MATS
    assert len(CAMEO_MATS) == 7


def test_the_path_strategy_modules_import():
    """Travel optimisation -- the other genuinely reusable half of the driver."""
    from silhouette.Geometry import XY_a, dist_sq
    from silhouette.Strategy import MatFree

    assert MatFree is not None
    assert dist_sq(XY_a((0.0, 0.0)), XY_a((3.0, 4.0))) == pytest.approx(25.0)


# --- licensing ------------------------------------------------------------------------


def test_graphtec_copyright_header_is_verbatim():
    text = (VENDOR / "silhouette" / "Graphtec.py").read_text(encoding="utf-8", errors="replace")
    head = text[:1200]
    assert "# (c) 2013,2014 jw@suse.de" in head
    assert "# (c) 2016 juewei@fabmail.org" in head
    assert "# (c) 2016 Alexander Wenger" in head
    assert "# (c) 2017 Johann Gail" in head
    assert "# Distribute under GPLv2 or ask." in head


def test_sendto_silhouette_copyright_header_is_verbatim():
    text = (VENDOR / "sendto_silhouette.py").read_text(encoding="utf-8", errors="replace")
    head = text[:1200]
    assert "(C) 2013 jw@suse.de. Licensed under CC-BY-SA-3.0 or GPL-2.0 at your choice." in head
    assert "(C) 2014 - 2023  juewei@fabmail.org and contributors" in head


def test_the_pinned_upstream_version_is_the_one_we_documented():
    text = (VENDOR / "sendto_silhouette.py").read_text(encoding="utf-8", errors="replace")
    assert re.search(r'__version__\s*=\s*"1\.29"', text)


def test_the_gpl2_license_text_is_present_and_is_actually_gpl2():
    """Not GPLv3 -- the headers say v2, so v2 is what has to be shipped alongside them."""
    licence = (VENDOR / "LICENSE").read_text(encoding="utf-8", errors="replace")
    assert "GNU GENERAL PUBLIC LICENSE" in licence
    assert "Version 2, June 1991" in licence
    assert "Version 3" not in licence
    assert "NO WARRANTY" in licence


def test_the_readme_records_the_provenance():
    """Attribution and the vendoring record are the point of the README; keep them load-bearing."""
    readme = (VENDOR / "README.md").read_text(encoding="utf-8", errors="replace")
    for required in ("inkscape-silhouette",
                     "https://github.com/fablabnbg/inkscape-silhouette",
                     "1.29",
                     "Weigert",
                     "GPL-2.0",
                     "2026-09-13"):
        assert required in readme, f"README.md no longer records {required!r}"
