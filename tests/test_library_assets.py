"""The Design Library's shared shelf for the eight asset categories that aren't Designs
(design_assets.py) or Local imports (media_scan.py) -- decal icons, cut files, footage,
mockups, backgrounds, human models, rooms, STL models.
"""
import pytest

from assistant.core import library_assets


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "library.db")
    library_assets.init_library_assets(p)
    return p


class TestCataloguing:
    def test_an_asset_can_be_found_again(self, path):
        library_assets.add(path, 1, "stl_model", "/d/stl/core-tile.stl", title="8x8 core tile")
        found = library_assets.catalogue(path, 1)
        assert len(found) == 1
        assert found[0]["title"] == "8x8 core tile"
        assert found[0]["category"] == "stl_model"

    def test_a_bad_category_is_refused(self, path):
        with pytest.raises(ValueError):
            library_assets.add(path, 1, "not_a_real_category", "/x")

    def test_metadata_round_trips_as_a_dict(self, path):
        library_assets.add(path, 1, "footage", "/d/clip.mp4",
                           metadata={"duration_s": 14, "resolution": "1080x1920"})
        asset = library_assets.catalogue(path, 1)[0]
        assert asset["metadata"] == {"duration_s": 14, "resolution": "1080x1920"}

    def test_missing_metadata_is_an_empty_dict_not_none(self, path):
        library_assets.add(path, 1, "room", "/d/room.jpg")
        assert library_assets.catalogue(path, 1)[0]["metadata"] == {}

    def test_filtering_by_category(self, path):
        library_assets.add(path, 1, "room", "/d/room.jpg", title="Living room")
        library_assets.add(path, 1, "background", "/d/bg.jpg", title="Oak workbench")
        rooms = library_assets.catalogue(path, 1, category="room")
        assert [a["title"] for a in rooms] == ["Living room"]

    def test_search_matches_title(self, path):
        library_assets.add(path, 1, "human_model", "/d/m1.jpg", title="Casual standing")
        library_assets.add(path, 1, "human_model", "/d/m2.jpg", title="Pinup leaning")
        assert [a["title"] for a in library_assets.catalogue(path, 1, search="casual")] == \
            ["Casual standing"]

    def test_newest_first(self, path):
        for i in range(3):
            library_assets.add(path, 1, "mockup", f"/d/m{i}.jpg", title=f"mockup {i}")
        assert [a["title"] for a in library_assets.catalogue(path, 1)] == \
            ["mockup 2", "mockup 1", "mockup 0"]

    def test_another_owners_assets_are_not_listed(self, path):
        library_assets.add(path, 1, "decal_icon", "/d/icon.svg", title="mine")
        assert library_assets.catalogue(path, 99) == []


class TestCategoryCounts:
    def test_every_category_is_present_even_at_zero(self, path):
        counts = library_assets.category_counts(path, 1)
        assert set(counts) == set(library_assets.CATEGORIES)
        assert all(n == 0 for n in counts.values())

    def test_a_retired_asset_does_not_count(self, path):
        asset_id = library_assets.add(path, 1, "cut_file", "/d/cut.svg")
        library_assets.set_status(path, 1, asset_id, "retired")
        assert library_assets.category_counts(path, 1)["cut_file"] == 0

    def test_counts_are_per_category(self, path):
        library_assets.add(path, 1, "footage", "/d/f1.mp4")
        library_assets.add(path, 1, "footage", "/d/f2.mp4")
        library_assets.add(path, 1, "stl_model", "/d/s1.stl")
        counts = library_assets.category_counts(path, 1)
        assert counts["footage"] == 2 and counts["stl_model"] == 1


class TestStatus:
    def test_using_an_asset_is_counted_not_consumed(self, path):
        asset = library_assets.add(path, 1, "mockup", "/d/m.jpg")
        library_assets.mark_used(path, 1, asset)
        library_assets.mark_used(path, 1, asset)
        again = library_assets.get(path, 1, asset)
        assert again["times_used"] == 2 and again["status"] == "used"

    def test_bulk_status_change(self, path):
        ids = [library_assets.add(path, 1, "decal_icon", f"/d/{i}.svg") for i in range(3)]
        changed = library_assets.set_status_bulk(path, 1, ids, "retired")
        assert changed == 3
        assert all(a["status"] == "retired" for a in library_assets.catalogue(path, 1, status="retired"))

    def test_a_nonsense_status_is_refused(self, path):
        asset = library_assets.add(path, 1, "room", "/d/r.jpg")
        with pytest.raises(ValueError):
            library_assets.set_status(path, 1, asset, "printed")

    def test_another_owner_cannot_mark_it_used(self, path):
        asset = library_assets.add(path, 1, "background", "/d/b.jpg")
        assert library_assets.mark_used(path, 99, asset) is False
