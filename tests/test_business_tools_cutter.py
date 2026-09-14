"""The vinyl-cutter tools on BusinessClient (assistant/core/business_tools.py).

Blue Ridge Custom Co's cutting work is a business concern, so these live alongside the other
business tools rather than in their own integration.

The load-bearing test here is `test_no_tool_can_drive_the_cutter`. File generation is safe
and testable; sending a job to a blade is not something to wire into an unattended chat turn,
so the capability is deliberately files-only. That is a decision worth defending in code
rather than leaving as an intention in a commit message -- the driver that *could* drive the
machine is sitting in vendor/inkscape-silhouette, so nothing but this boundary keeps them
apart.
"""

import numpy as np
import pytest

from assistant.core import business_tools

pytest.importorskip("cv2")


@pytest.fixture
def client(tmp_path):
    return business_tools.BusinessClient(
        str(tmp_path / "jarvis.db"), owner_user_id=1, media_dir=str(tmp_path / "generated"),
    )


@pytest.fixture
def sticker_dir(tmp_path):
    """Two transparent-background PNGs, the shape real sticker art has."""
    import cv2

    d = tmp_path / "art"
    d.mkdir()
    size = 400
    yy, xx = np.mgrid[0:size, 0:size]
    for i, (cx, cy, r) in enumerate([(200, 200, 150), (200, 200, 120)]):
        img = np.zeros((size, size, 4), np.uint8)
        img[:, :, :3] = 60
        img[:, :, 3] = np.where((xx - cx) ** 2 + (yy - cy) ** 2 <= r ** 2, 255, 0)
        cv2.imwrite(str(d / f"s{i}.png"), cv2.cvtColor(img, cv2.COLOR_RGBA2BGRA))
    return d


# --- the safety boundary ----------------------------------------------------------------


def test_no_tool_can_drive_the_cutter():
    """Files only. No tool may send a job to the machine.

    Checked by name across the whole business surface rather than trusting the two cutter
    entries, so adding a `send_to_cutter` anywhere in this module fails here first.
    """
    names = {t["function"]["name"] for t in business_tools.BUSINESS_TOOLS}
    forbidden = ("cut", "plot", "blade", "send_to")
    offenders = [
        n for n in names
        if any(w in n for w in forbidden) and n not in {"generate_sticker_cut_sheet", "cutter_settings"}
    ]
    assert offenders == [], f"a tool that may drive hardware appeared: {offenders}"


def test_the_cutter_tools_are_registered_on_the_business_surface():
    names = {t["function"]["name"] for t in business_tools.BUSINESS_TOOLS}
    assert {"generate_sticker_cut_sheet", "cutter_settings"} <= names


def test_the_generate_tool_tells_the_model_it_does_not_cut():
    """The model must not report a job as cutting. Say so where it will actually be read."""
    tool = next(t for t in business_tools.CUTTER_TOOLS
                if t["function"]["name"] == "generate_sticker_cut_sheet")
    description = tool["function"]["description"].lower()
    assert "does not cut" in description or "not cut anything" in description
    assert "himself" in description or "manual" in description


def test_the_system_note_says_the_sheet_must_print_at_full_scale():
    """Fit-to-page moves the registration marks and the Cameo then cannot find them."""
    note = business_tools.BUSINESS_SYSTEM_NOTE
    assert "100% scale" in note
    assert "never say a job is cutting" in note


# --- cutter_settings ---------------------------------------------------------------------


def test_cutter_settings_reports_both_calibrated_recipes(client):
    result = client.call_tool("cutter_settings", {})
    recipes = result["blade_recipes"]

    assert recipes["sticker"] == {"tool": "autoblade", "speed": 4, "pressure": 15,
                                  "depth": 6, "registration_marks": True}
    assert recipes["vinyl"] == {"tool": "autoblade", "speed": 3, "pressure": 10,
                                "depth": 2, "registration_marks": False}


def test_cutter_settings_can_be_asked_for_one_material(client):
    assert set(client.call_tool("cutter_settings", {"material": "vinyl"})["blade_recipes"]) == {"vinyl"}


def test_cutter_settings_reports_the_registration_geometry(client):
    marks = client.call_tool("cutter_settings", {})["registration_marks"]
    assert marks["count"] == 3
    assert "no fourth" in marks["positions"]
    assert marks["mark_to_mark_mm"] == [195.9, 259.4]
    assert "printed sheet only" in marks["note"]


def test_cutter_settings_carries_the_machine_limits(client):
    """Pressure >= 19 triggers track-enhancing; the scale maxes at 33. Worth knowing first."""
    limits = client.call_tool("cutter_settings", {})["machine_limits"]
    assert limits["max_pressure"] == 33
    assert limits["track_enhancing_at_pressure"] == 19


# --- generate_sticker_cut_sheet ----------------------------------------------------------


def test_generating_from_a_folder_writes_both_files(client, sticker_dir):
    from pathlib import Path

    result = client.call_tool("generate_sticker_cut_sheet",
                              {"source_dir": str(sticker_dir), "name": "run1"})

    assert "error" not in result
    assert result["placed"] == 2
    assert Path(result["print_png"]).is_file()
    assert Path(result["cut_svg"]).is_file()


def test_the_result_carries_the_blade_settings_and_the_manual_next_step(client, sticker_dir):
    result = client.call_tool("generate_sticker_cut_sheet", {"source_dir": str(sticker_dir)})

    assert result["blade_settings"]["pressure"] == 15, "sticker recipe by default"
    assert result["blade_settings"]["registration_marks"] is True
    assert "does not drive the cutter" in result["next_step"]
    assert "100% scale" in result["next_step"]


def test_asking_for_vinyl_switches_the_recipe_and_drops_the_marks(client, sticker_dir):
    result = client.call_tool("generate_sticker_cut_sheet",
                              {"source_dir": str(sticker_dir), "material": "vinyl"})

    assert result["blade_settings"]["pressure"] == 10
    assert result["blade_settings"]["registration_marks"] is False
    assert result["preset"] == "vinyl"


def test_the_offset_defaults_to_the_settled_two_millimetres(client, sticker_dir):
    result = client.call_tool("generate_sticker_cut_sheet", {"source_dir": str(sticker_dir)})
    assert result["offset_mm"] == 2.0


def test_an_explicit_offset_is_honoured(client, sticker_dir):
    result = client.call_tool("generate_sticker_cut_sheet",
                              {"source_dir": str(sticker_dir), "offset_mm": 3.5})
    assert result["offset_mm"] == 3.5


def test_output_lands_under_the_configured_media_dir(client, sticker_dir, tmp_path):
    result = client.call_tool("generate_sticker_cut_sheet", {"source_dir": str(sticker_dir)})
    assert str(tmp_path / "generated" / "cut_sheets") in result["print_png"]


def test_a_missing_folder_is_an_error_not_a_crash(client):
    assert "error" in client.call_tool("generate_sticker_cut_sheet",
                                       {"source_dir": "C:/nope/not/here"})


def test_being_given_nothing_to_lay_out_is_an_error(client):
    result = client.call_tool("generate_sticker_cut_sheet", {})
    assert "no images" in result["error"]


def test_an_opaque_image_is_reported_rather_than_cut_as_a_rectangle(client, tmp_path):
    """The original failure, surfaced through the tool boundary.

    rembg failing silently and returning an opaque image is why stickers sometimes cut as
    squares. The engine raises; this checks the tool reports it instead of swallowing it.
    """
    import cv2

    d = tmp_path / "opaque"
    d.mkdir()
    img = np.full((80, 80, 4), 255, np.uint8)
    cv2.imwrite(str(d / "flat.png"), cv2.cvtColor(img, cv2.COLOR_RGBA2BGRA))

    result = client.call_tool("generate_sticker_cut_sheet", {"source_dir": str(d)})
    assert "error" in result
